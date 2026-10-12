"""The interface every market-data source implements."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from scheme1.contracts import CandleBatch, Interval


class ProviderError(Exception):
    """The provider could not deliver usable data. The pipeline turns this into ``DataStatus.UNAVAILABLE``."""

    def __init__(self, provider: str, reason: str, detail: str):
        super().__init__(f"{provider} {reason}: {detail}")
        self.provider = provider
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class FetchResult:
    """A parsed batch plus the exact bytes it was parsed from (``sha256(raw)`` is in the provenance)."""

    batch: CandleBatch
    raw: bytes


class MarketDataProvider(Protocol):
    name: str

    def fetch_candles(
        self, symbol: str, interval: Interval, *, limit: int = 300, end: datetime | None = None
    ) -> FetchResult:
        """Return up to ``limit`` candles whose open time is before ``end`` (default: now), oldest first.

        The newest candle may still be open; it is returned with ``closed=False``.
        Raise ``ProviderError`` instead of returning made-up or partial data.
        """
