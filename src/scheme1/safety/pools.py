"""Pool snapshots from GeckoTerminal and DexScreener.

Verified 2026-10-12:
- GeckoTerminal ``GET /api/v2/networks/solana/pools/{pool}``: ``data.attributes.reserve_in_usd`` (string),
  ``data.attributes.pool_created_at`` (ISO 8601), base mint in ``data.relationships.base_token.data.id``
  as ``solana_<mint>``. Shares the ~10 calls/minute budget with candle requests.
- DexScreener ``GET /latest/dex/pairs/solana/{pair}``: ``pairs[0].liquidity.usd`` (number),
  ``pairs[0].pairCreatedAt`` (unix milliseconds), ``pairs[0].baseToken.address``.
- Prices (copy trades, 2026-11-17): GeckoTerminal ``attributes.base_token_price_usd``, DexScreener ``priceUsd``.
- GeckoTerminal ``GET /api/v2/networks/solana/tokens/{mint}/pools`` lists a token's pools with the same
  attributes; ``main_pool`` picks the one with the most 24-hour volume where the token is the base.
  DexScreener reports no liquidity for pump.fun bonding curves.
Neither response is stored (provider-data retention, D11).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from scheme1.contracts import PoolSnapshot
from scheme1.data.http import HttpError, PacedClient, RetryPolicy, UrllibTransport
from scheme1.data.providers.base import ProviderError
from scheme1.data.providers.geckoterminal import API_VERSION_HEADER, BASE_URL, split_symbol

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/pairs"
_UA = {"User-Agent": "scheme1-paper-desk/0.1"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get(client: PacedClient, provider: str, url: str) -> tuple[bytes, Any]:
    try:
        body = client.get(url).body
    except HttpError as err:
        raise ProviderError(provider, err.reason, f"{err.detail} (status={err.status})") from err
    try:
        return body, json.loads(body, parse_float=Decimal)
    except ValueError as err:
        raise ProviderError(provider, "bad_payload", str(err)) from err


def _price(value: Any) -> Decimal | None:
    d = _decimal(value)
    return d if d is not None and d > 0 else None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() and d >= 0 else None


class GeckoTerminalPools:
    name = "geckoterminal"

    def __init__(self, client: PacedClient | None = None, now: Callable[[], datetime] = _now):
        self._client = client or PacedClient(UrllibTransport(), min_interval_s=7.5, retry=RetryPolicy(),
                                             headers={"Accept": API_VERSION_HEADER, **_UA})
        self._now = now

    def snapshot(self, symbol: str) -> PoolSnapshot:
        network, pool = split_symbol(symbol)
        body, payload = _get(self._client, self.name, f"{BASE_URL}/networks/{network}/pools/{pool}")
        try:
            data = payload["data"]
            attrs = data["attributes"]
            if attrs.get("address") != pool:
                raise ProviderError(self.name, "wrong_pool", f"asked {pool}, got {attrs.get('address')}")
            mint_id = (data.get("relationships", {}).get("base_token", {}).get("data") or {}).get("id", "")
        except (KeyError, TypeError, AttributeError) as err:
            raise ProviderError(self.name, "bad_payload", f"{type(err).__name__}: {err}") from err
        return self._to_snapshot(symbol, network, attrs, mint_id, body)

    def main_pool(self, mint: str, network: str = "solana") -> PoolSnapshot:
        """The token's most traded pool (24-hour volume) in which it is the base token."""
        body, payload = _get(self._client, self.name, f"{BASE_URL}/networks/{network}/tokens/{mint}/pools?page=1")
        best: tuple[Decimal, dict, str] | None = None
        try:
            for pool in payload["data"]:
                attrs = pool["attributes"]
                base = (pool.get("relationships", {}).get("base_token", {}).get("data") or {}).get("id", "")
                if base != f"{network}_{mint}":
                    continue
                volume = _decimal((attrs.get("volume_usd") or {}).get("h24")) or Decimal(0)
                if best is None or volume > best[0]:
                    best = (volume, attrs, base)
        except (KeyError, TypeError, AttributeError) as err:
            raise ProviderError(self.name, "bad_payload", f"{type(err).__name__}: {err}") from err
        if best is None:
            raise ProviderError(self.name, "not_found", f"no pool with {mint} as the base token")
        _, attrs, base = best
        return self._to_snapshot(f"{network}:{attrs['address']}", network, attrs, base, body)

    def _to_snapshot(self, symbol: str, network: str, attrs: dict, mint_id: str, body: bytes) -> PoolSnapshot:
        return PoolSnapshot(
            symbol=symbol, provider=self.name, retrieved_at=self._now(),
            base_token_mint=mint_id.removeprefix(f"{network}_") if mint_id.startswith(f"{network}_") else None,
            liquidity_usd=_decimal(attrs.get("reserve_in_usd")),
            pool_created_at=_parse_iso(attrs.get("pool_created_at")),
            base_price_usd=_price(attrs.get("base_token_price_usd")),
            content_sha256=hashlib.sha256(body).hexdigest(),
        )


class DexScreenerPools:
    name = "dexscreener"

    def __init__(self, client: PacedClient | None = None, now: Callable[[], datetime] = _now):
        # DexScreener's limits are higher, but one call per second is plenty and stays far from "unreasonable load".
        self._client = client or PacedClient(UrllibTransport(), min_interval_s=1.0, retry=RetryPolicy(), headers=_UA)
        self._now = now

    def snapshot(self, symbol: str) -> PoolSnapshot:
        network, pool = split_symbol(symbol)
        body, payload = _get(self._client, self.name, f"{DEXSCREENER_URL}/{network}/{pool}")
        pairs = payload.get("pairs") if isinstance(payload, dict) else None
        if not pairs:
            raise ProviderError(self.name, "not_found", f"no pair {pool}")
        try:
            pair = pairs[0]
            if pair.get("pairAddress") != pool or pair.get("chainId") != network:
                raise ProviderError(self.name, "wrong_pool", f"asked {pool}, got {pair.get('pairAddress')}")
            created_ms = pair.get("pairCreatedAt")
            created = (datetime.fromtimestamp(int(created_ms) / 1000, tz=timezone.utc)
                       if isinstance(created_ms, (int, Decimal)) and not isinstance(created_ms, bool) else None)
            return PoolSnapshot(
                symbol=symbol, provider=self.name, retrieved_at=self._now(),
                base_token_mint=(pair.get("baseToken") or {}).get("address"),
                liquidity_usd=_decimal((pair.get("liquidity") or {}).get("usd")),
                pool_created_at=created,
                base_price_usd=_price(pair.get("priceUsd")),
                content_sha256=hashlib.sha256(body).hexdigest(),
            )
        except (TypeError, AttributeError, ValueError, OverflowError) as err:
            raise ProviderError(self.name, "bad_payload", f"{type(err).__name__}: {err}") from err


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
