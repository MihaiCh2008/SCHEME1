"""Watch the wallets of traders the user follows, and report their new trades.

Each poll asks the RPC node for the transactions that mention each wallet since the last one seen
(the cursor), reads them oldest first and keeps the ones ``parse_trade`` accepts.

- The first poll of a wallet only remembers where "now" is: trades from before the desk started are
  never copied.
- Busy wallets get a lot of spam, so a poll pages through at most ``max_per_poll`` transactions; if
  there were more, the oldest are skipped and a note says so.
- If the node can't return a transaction yet, the cursor stops before it, so it's read on the next poll.
- A failing wallet doesn't stop the others; its cursor stays where it was.

Cost: 1 credit per page of signatures plus 1 per transaction read (Helius free plan: 1M credits/month).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from scheme1.contracts import TraderTrade
from scheme1.copytrade.parse import parse_trade
from scheme1.data.providers.base import ProviderError
from scheme1.safety.solana import SolanaRpc, b58decode


@dataclass(frozen=True)
class Trader:
    name: str
    wallet: str

    def __post_init__(self) -> None:
        try:
            ok = len(b58decode(self.wallet)) == 32
        except ValueError:
            ok = False
        if not ok:
            raise ValueError(f"{self.name}: {self.wallet!r} is not a Solana address")


def load_traders(path: Path | str) -> list[Trader]:
    """One trader per line: ``name address``, or just the address (named trader1, trader2, ... by position).
    ``#`` starts a comment."""
    traders = []
    for line in Path(path).read_text().splitlines():
        parts = line.split("#", 1)[0].split()
        if not parts:
            continue
        name, wallet = (parts[0], parts[1]) if len(parts) > 1 else (f"trader{len(traders) + 1}", parts[0])
        traders.append(Trader(name, wallet))
    if len({t.wallet for t in traders}) != len(traders):
        raise ValueError("the same wallet is listed twice")
    if len({t.name for t in traders}) != len(traders):
        raise ValueError("two traders have the same name")
    return traders


@dataclass
class PollResult:
    trades: list[TraderTrade] = field(default_factory=list)
    cursors: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    reads: int = 0  # RPC calls made (≈ Helius credits)


class TraderWatcher:
    def __init__(self, rpc: SolanaRpc, traders: list[Trader], max_per_poll: int = 300):
        self.rpc = rpc
        self.traders = traders
        self.max_per_poll = max_per_poll

    def poll(self, cursors: dict[str, str]) -> PollResult:
        result = PollResult(cursors=dict(cursors))
        for t in self.traders:
            try:
                self._poll_one(t, result)
            except ProviderError as err:
                result.notes.append(f"{t.name}: could not read the wallet ({err.reason}); trying again next time")
        return result

    def _poll_one(self, t: Trader, result: PollResult) -> None:
        cursor = result.cursors.get(t.wallet)
        page = self.rpc.signatures(t.wallet, until=cursor, limit=100)
        result.reads += 1
        if cursor is None:
            if page:
                result.cursors[t.wallet] = page[0]["signature"]
            result.notes.append(f"{t.name}: watching from now on")
            return
        newest_first = list(page)
        while len(page) == 100 and len(newest_first) < self.max_per_poll:
            page = self.rpc.signatures(t.wallet, until=cursor, before=newest_first[-1]["signature"], limit=100)
            result.reads += 1
            newest_first += page
        if len(page) == 100:
            result.notes.append(f"{t.name}: more than {self.max_per_poll} new transactions; the oldest were skipped")
        for entry in reversed(newest_first[: self.max_per_poll]):
            if entry.get("err") is None:
                tx = self.rpc.transaction(entry["signature"])
                result.reads += 1
                if tx is None:
                    result.notes.append(f"{t.name}: a transaction isn't available yet; reading it next time")
                    return
                trade = parse_trade(tx, t.wallet, t.name)
                if trade is not None:
                    result.trades.append(trade)
            result.cursors[t.wallet] = entry["signature"]
