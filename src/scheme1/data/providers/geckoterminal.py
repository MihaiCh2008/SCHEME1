"""GeckoTerminal public API (keyless) for Solana DEX pool candles.

Verified from the cloud session on 2026-10-12 (see docs/STAGE3_DATA.md):
- ``GET /api/v2/networks/{network}/pools/{pool}/ohlcv/{minute|hour|day}``; aggregates 1/5/15 minute,
  1/4/12 hour, 1 day; ``limit`` up to 1000; ``before_timestamp`` in unix seconds.
- Rows are ``[unix_s, open, high, low, close, volume]``, newest first; volume is in USD when ``currency=usd``.
- The newest row can still be open. Responses are cached for about 1 minute.
- Public limit is about 10 calls/minute. A 429 carries ``retry-after: 0``, so we back off on our own.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable
from urllib.parse import urlencode

from scheme1.contracts import CandleBatch, DataKind, Interval, Provenance
from scheme1.data.http import HttpError, PacedClient, RetryPolicy, Transport, UrllibTransport
from scheme1.data.providers._rows import parse_rows
from scheme1.data.providers.base import FetchResult, ProviderError

BASE_URL = "https://api.geckoterminal.com/api/v2"
# CoinGecko API Terms 4.4: show this wherever the data is used.
ATTRIBUTION = "Powered by CoinGecko"
API_VERSION_HEADER = "application/json;version=20230203"
MAX_LIMIT = 1000

_TIMEFRAMES: dict[Interval, tuple[str, int]] = {
    Interval.M1: ("minute", 1),
    Interval.M5: ("minute", 5),
    Interval.M15: ("minute", 15),
    Interval.H1: ("hour", 1),
    Interval.H4: ("hour", 4),
    Interval.D1: ("day", 1),
}

# Solana addresses are base58, 32-44 chars. Checking this also keeps arbitrary text out of the URL path.
_SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
SUPPORTED_NETWORKS = frozenset({"solana"})


def pool_symbol(network: str, pool_address: str) -> str:
    """Canonical symbol for a DEX pool, e.g. ``solana:5zpy...``."""
    return f"{network}:{pool_address}"


def split_symbol(symbol: str) -> tuple[str, str]:
    network, sep, address = symbol.partition(":")
    if not sep or network not in SUPPORTED_NETWORKS or not _SOLANA_ADDRESS.match(address):
        raise ValueError(f"expected 'solana:<pool address>', got {symbol!r}")
    return network, address


class GeckoTerminalProvider:
    name = "geckoterminal"

    def __init__(
        self,
        client: PacedClient | None = None,
        *,
        transport: Transport | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        min_interval_s: float = 7.5,  # 8 calls/minute, margin under the ~10/minute limit (API Terms 4.2)
        retry: RetryPolicy = RetryPolicy(),
    ):
        self._client = client or PacedClient(
            transport or UrllibTransport(), min_interval_s=min_interval_s, retry=retry,
            headers={"Accept": API_VERSION_HEADER, "User-Agent": "scheme1-paper-desk/0.1"},
        )
        self._now = now

    def build_url(self, symbol: str, interval: Interval, limit: int, end: datetime | None) -> str:
        network, address = split_symbol(symbol)
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be 1..{MAX_LIMIT}")
        timeframe, aggregate = _TIMEFRAMES[interval]
        query = {
            "aggregate": str(aggregate),
            "limit": str(limit),
            "currency": "usd",
            "token": "base",
            # Ask for empty intervals so a missing row means missing data, not "no trades".
            "include_empty_intervals": "true",
        }
        if end is not None:
            query["before_timestamp"] = str(int(end.timestamp()))
        return f"{BASE_URL}/networks/{network}/pools/{address}/ohlcv/{timeframe}?{urlencode(query)}"

    def fetch_candles(
        self, symbol: str, interval: Interval, *, limit: int = 300, end: datetime | None = None
    ) -> FetchResult:
        url = self.build_url(symbol, interval, limit, end)
        try:
            resp = self._client.get(url)
        except HttpError as err:
            raise ProviderError(self.name, err.reason, f"{err.detail} (status={err.status}, attempts={err.attempts})") from err
        retrieved_at = self._now()

        try:
            payload = json.loads(resp.body, parse_float=Decimal)
            rows = payload["data"]["attributes"]["ohlcv_list"]
            if not isinstance(rows, list):
                raise TypeError("ohlcv_list is not a list")
        except (ValueError, KeyError, TypeError) as err:
            raise ProviderError(self.name, "bad_payload", f"{type(err).__name__}: {err}") from err
        if not rows:
            raise ProviderError(self.name, "empty", "provider returned no candles")

        candles, issues = parse_rows(rows, symbol, interval, retrieved_at)
        candles = tuple(sorted(candles, key=lambda c: c.open_time))  # provider sends newest first
        timeframe, aggregate = _TIMEFRAMES[interval]
        provenance = Provenance(
            provider=self.name,
            kind=DataKind.CURRENT if end is None else DataKind.HISTORICAL,
            retrieved_at=retrieved_at,
            request={
                "url": url,
                "timeframe": timeframe,
                "aggregate": str(aggregate),
                "price_currency": "usd",
                "volume_unit": "usd",
            },
            content_sha256=hashlib.sha256(resp.body).hexdigest(),
        )
        batch = CandleBatch(symbol=symbol, interval=interval, provenance=provenance, candles=candles, parse_issues=issues)
        return FetchResult(batch=batch, raw=resp.body)
