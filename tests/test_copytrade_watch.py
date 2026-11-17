from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from scheme1.contracts import Side
from scheme1.copytrade import Trader, TraderWatcher, load_traders, parse_trade
from scheme1.copytrade.parse import WSOL
from scheme1.data.providers.base import ProviderError

WALLET = "FQpu9hW7C5p7irsXYRDHjtBKFc6ooNWni2TX1S6VHxRz"  # one of the user's traders (public address)
OTHER = "DrJ6SnDXkEsPeGdmSs93v5rwWumv5QMvAGSZjAyWSd5o"
MEME = "DPmHerVsAmpSyRzXrFJqKXkzmUbkWzTmkMUqZAfnpump"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
T = 1_763_412_000  # 2025-11-17 20:40 UTC


def bal(owner, mint, raw, decimals=6):
    return {"owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(raw), "decimals": decimals}}


def tx(sol_change=0, pre=(), post=(), signer=True, err=None, sig="sig1", wallet=WALLET):
    """A jsonParsed transaction shaped like the ones the RPC node returns (version 1)."""
    keys = [{"pubkey": "Payer1111111111111111111111111111111111111", "signer": True},
            {"pubkey": wallet, "signer": signer}]
    return {"slot": 380_000_000, "blockTime": T, "version": 1,
            "transaction": {"signatures": [sig], "message": {"accountKeys": keys, "instructions": []}},
            "meta": {"err": err, "fee": 5000, "preBalances": [1, 5_000_000_000],
                     "postBalances": [1, 5_000_000_000 + sol_change],
                     "preTokenBalances": list(pre), "postTokenBalances": list(post)}}


def test_pump_fun_buy_with_sol():
    t = parse_trade(tx(-1_002_000_000, post=[bal(WALLET, MEME, 3_483_877_123_456)]), WALLET, "trader4")
    assert t.side is Side.BUY and t.mint == MEME and t.token_amount == D("3483877.123456")
    assert t.quote == "SOL" and t.quote_amount == D("1.002") and t.trader == "trader4"
    assert t.block_time == datetime.fromtimestamp(T, timezone.utc)


def test_sell_paid_in_wrapped_sol_counts_as_sol():
    t = parse_trade(tx(-5_000, pre=[bal(WALLET, MEME, 796_344_000_000)],
                       post=[bal(WALLET, WSOL, 955_000_000, 9)]), WALLET, "t")
    assert t.side is Side.SELL and t.quote == "SOL" and t.quote_amount == D("0.954995")


def test_buy_with_usdc_and_other_peoples_balances_are_ignored():
    t = parse_trade(tx(-5_000, pre=[bal(WALLET, USDC, 50_000_000), bal(OTHER, MEME, 9)],
                       post=[bal(WALLET, MEME, 100_000_000), bal(OTHER, MEME, 1)]), WALLET, "t")
    assert t.side is Side.BUY and t.quote == "USDC" and t.quote_amount == 50 and t.token_amount == 100


@pytest.mark.parametrize("transaction", [
    tx(-1_000_000_000, post=[bal(WALLET, MEME, 1)], signer=False),            # someone else's transaction
    tx(-1_000_000_000, post=[bal(WALLET, MEME, 1)], err={"InstructionError": [0, "x"]}),  # failed
    tx(-5_000, pre=[bal(WALLET, MEME, 100)]),                                  # sent away: no payment back
    tx(-5_000, pre=[bal(WALLET, MEME, 100)], post=[bal(WALLET, OTHER, 7)]),    # token-to-token: two tokens
    tx(0),                                                                     # nothing changed
    None,
])
def test_things_that_are_not_trades(transaction):
    assert parse_trade(transaction, WALLET, "t") is None


class FakeRpc:
    """Signatures newest first, like getSignaturesForAddress; transactions by signature."""

    def __init__(self, sigs, txs, broken=()):
        self.sigs, self.txs, self.broken, self.calls = sigs, txs, set(broken), 0

    def signatures(self, address, *, until=None, before=None, limit=100):
        self.calls += 1
        if address in self.broken:
            raise ProviderError("solana_rpc", "rate_limited", "429")
        names = self.sigs.get(address, [])
        stop = names.index(until) if until in names else len(names)
        start = names.index(before) + 1 if before in names else 0
        return [{"signature": s, "err": {"x": 1} if s.startswith("bad") else None} for s in names[start:stop]][:limit]

    def transaction(self, signature):
        self.calls += 1
        return self.txs.get(signature)


def buy_tx(sig, wallet=WALLET):
    return tx(-1_000_000_000, post=[bal(wallet, MEME, 5)], sig=sig, wallet=wallet)


def test_first_poll_only_marks_now_then_new_trades_are_reported_oldest_first():
    sigs = {WALLET: ["s2", "s1", "old"]}
    rpc = FakeRpc(sigs, {"s1": buy_tx("s1"), "s2": tx(-5_000, sig="s2", signer=False)})
    w = TraderWatcher(rpc, [Trader("trader4", WALLET)])
    first = w.poll({})
    assert first.trades == [] and first.cursors == {WALLET: "s2"} and "from now on" in first.notes[0]
    sigs[WALLET] = ["s4", "bad3", "s2", "s1", "old"]
    rpc.txs["s4"] = buy_tx("s4")
    second = w.poll(first.cursors)
    assert [t.signature for t in second.trades] == ["s4"] and second.cursors[WALLET] == "s4"


def test_spam_is_paged_and_capped():
    spam = [f"spam{i}" for i in range(250)]
    rpc = FakeRpc({WALLET: spam + ["start"]}, {s: tx(-5_000, sig=s, signer=False) for s in spam})
    result = TraderWatcher(rpc, [Trader("t", WALLET)], max_per_poll=150).poll({WALLET: "start"})
    assert result.trades == [] and result.cursors[WALLET] == "spam0" and "skipped" in result.notes[0]
    assert result.reads == 2 + 150   # two pages, then only the newest 150 transactions


def test_a_missing_transaction_is_read_next_time():
    rpc = FakeRpc({WALLET: ["s3", "s2", "start"]}, {"s2": buy_tx("s2")})
    w = TraderWatcher(rpc, [Trader("t", WALLET)])
    first = w.poll({WALLET: "start"})
    assert [t.signature for t in first.trades] == ["s2"] and first.cursors[WALLET] == "s2"
    rpc.txs["s3"] = buy_tx("s3")
    assert [t.signature for t in w.poll(first.cursors).trades] == ["s3"]


def test_one_broken_wallet_does_not_stop_the_others():
    rpc = FakeRpc({OTHER: ["n1", "start"]}, {"n1": buy_tx("n1", wallet=OTHER)}, broken={WALLET})
    w = TraderWatcher(rpc, [Trader("a", WALLET), Trader("b", OTHER)])
    result = w.poll({WALLET: "w0", OTHER: "start"})
    assert [t.trader for t in result.trades] == ["b"] and result.cursors[WALLET] == "w0"
    assert "could not read" in result.notes[0]


def test_trader_list_file(tmp_path):
    f = tmp_path / "traders.txt"
    f.write_text(f"# my traders\nfast_guy {WALLET}  # pump.fun\n\n{OTHER}\n")
    assert load_traders(f) == [Trader("fast_guy", WALLET), Trader("trader2", OTHER)]
    for bad in (f"{WALLET}\n{WALLET}\n", f"x {WALLET}\nx {OTHER}\n"):   # same wallet twice, same name twice
        f.write_text(bad)
        with pytest.raises(ValueError):
            load_traders(f)
    with pytest.raises(ValueError):
        Trader("x", "0xabc")
