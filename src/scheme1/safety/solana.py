"""Read-only Solana on-chain data: mint authorities, Token-2022 extensions and largest holders.

Uses three standard JSON-RPC methods (no keys, no signing, no transactions):
``getAccountInfo`` (jsonParsed mint), ``getTokenLargestAccounts`` (top 20 token accounts) and
``getMultipleAccounts`` (owners of those accounts).

The RPC URL comes from ``SCHEME1_SOLANA_RPC_URL`` (default: the public mainnet endpoint).
Provider URLs such as Helius carry an API key, so the URL is never logged or put in an error.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Callable

from scheme1.contracts import HolderBalance, TokenOnchainInfo
from scheme1.data.http import HttpError, PacedClient, RetryPolicy, UrllibTransport
from scheme1.data.providers.base import ProviderError

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "spl-token",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PS1H7nPZo8q1bD": "spl-token-2022",
}

# ------------------------------------------------------------------ addresses

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        idx = _B58.find(ch)
        if idx < 0:
            raise ValueError(f"not base58: {ch!r}")
        n = n * 58 + idx
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body


def is_on_curve(address: str) -> bool:
    """True for a normal wallet key, False for a program-derived address (PDA).

    Same test as Solana's ``Pubkey::is_on_curve`` (curve25519-dalek ``CompressedEdwardsY::decompress``):
    y is reduced mod p and the point is valid when (y^2 - 1) / (d*y^2 + 1) has a square root mod p.
    """
    raw = b58decode(address)
    if len(raw) != 32:
        raise ValueError(f"expected 32 bytes, got {len(raw)}")
    y = (int.from_bytes(raw, "little") & ((1 << 255) - 1)) % _P
    y2 = y * y % _P
    # d*y^2 + 1 is never 0: that would need -1/d to be a square, and d is not a square mod p.
    x2 = (y2 - 1) * pow((_D * y2 + 1) % _P, _P - 2, _P) % _P
    return x2 == 0 or pow(x2, (_P - 1) // 2, _P) == 1


# ------------------------------------------------------------------------ RPC


class SolanaRpc:
    name = "solana_rpc"

    def __init__(self, client: PacedClient | None = None, url: str | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._url = url or os.environ.get("SCHEME1_SOLANA_RPC_URL") or DEFAULT_RPC_URL
        # The public endpoint allows roughly 100 requests per 10 s per IP; 2 per second stays well clear.
        self._client = client or PacedClient(UrllibTransport(), min_interval_s=0.5, retry=RetryPolicy(),
                                             headers={"User-Agent": "scheme1-paper-desk/0.1"})
        self._now = now
        self._next_id = 0

    def _call(self, method: str, params: list[Any]) -> Any:
        self._next_id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}).encode()
        try:
            resp = self._client.post(self._url, body)
        except HttpError as err:  # err never contains the URL
            raise ProviderError(self.name, err.reason, f"{method}: {err.detail} (status={err.status})") from err
        try:
            payload = json.loads(resp.body)
        except ValueError as err:
            raise ProviderError(self.name, "bad_payload", f"{method}: {err}") from err
        if not isinstance(payload, dict) or "error" in payload or "result" not in payload:
            detail = payload.get("error") if isinstance(payload, dict) else payload
            raise ProviderError(self.name, "rpc_error", f"{method}: {str(detail)[:200]}")
        return payload["result"]

    def token_info(self, mint: str) -> TokenOnchainInfo:
        if len(b58decode(mint)) != 32:  # mints can be PDAs, so only the length is checked
            raise ValueError("not a 32-byte Solana address")
        try:
            account = self._call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])["value"]
            if account is None:
                raise ProviderError(self.name, "not_found", f"no account {mint}")
            program = TOKEN_PROGRAMS.get(account.get("owner"))
            parsed = account["data"]["parsed"]
            if program is None or parsed.get("type") != "mint":
                raise ProviderError(self.name, "not_a_mint", f"{mint} is not a token mint")
            info = parsed["info"]

            largest = self._call("getTokenLargestAccounts", [mint])["value"]
            addresses = [a["address"] for a in largest]
            amounts = {a["address"]: int(a["amount"]) for a in largest}
            owners: dict[str, int] = {}
            if addresses:
                accounts = self._call("getMultipleAccounts", [addresses, {"encoding": "jsonParsed"}])["value"]
                for address, acc in zip(addresses, accounts):
                    acc_info = acc["data"]["parsed"]["info"] if acc else None
                    if not acc_info or acc_info.get("mint") != mint:
                        raise ProviderError(self.name, "bad_payload", f"token account {address} missing or for another mint")
                    owners[acc_info["owner"]] = owners.get(acc_info["owner"], 0) + amounts[address]
        except (KeyError, TypeError, ValueError, AttributeError) as err:
            raise ProviderError(self.name, "bad_payload", f"{type(err).__name__}: {err}") from err

        holders = sorted(
            (HolderBalance(owner=o, amount=a, program_controlled=not is_on_curve(o)) for o, a in owners.items()),
            key=lambda h: h.amount, reverse=True,
        )
        return TokenOnchainInfo(
            mint=mint, retrieved_at=self._now(), token_program=program,
            supply=int(info["supply"]), decimals=int(info["decimals"]),
            mint_authority=info.get("mintAuthority"), freeze_authority=info.get("freezeAuthority"),
            extensions=tuple(e.get("extension", "?") for e in info.get("extensions", []) or []),
            holders=tuple(holders), accounts_seen=len(addresses),
            smallest_account_seen=min(amounts.values(), default=0),
        )
