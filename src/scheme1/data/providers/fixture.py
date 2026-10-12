"""Offline provider that serves candles from a JSON file shipped with the repo.

Its data is always labelled ``DataKind.FIXTURE`` and must never be shown as market data.

File format::

    {"symbol": "solana:<pool>", "interval": "5m", "rows": [[unix_s, "open", "high", "low", "close", "volume"], ...]}
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from scheme1.contracts import CandleBatch, DataKind, Interval, Provenance
from scheme1.data.providers._rows import parse_rows
from scheme1.data.providers.base import FetchResult, ProviderError


class FixtureProvider:
    name = "fixture"

    def __init__(self, path: Path | str, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._path = Path(path)
        self._now = now

    def fetch_candles(
        self, symbol: str, interval: Interval, *, limit: int = 300, end: datetime | None = None
    ) -> FetchResult:
        raw = self._path.read_bytes()
        data = json.loads(raw, parse_float=Decimal)
        if data.get("symbol") != symbol or data.get("interval") != interval.value:
            raise ProviderError(self.name, "not_found", f"fixture has {data.get('symbol')} {data.get('interval')}")
        rows = data["rows"]
        if end is not None:
            cutoff = int(end.timestamp())
            # rows without a readable timestamp are kept so the parser reports them
            rows = [r for r in rows if not (isinstance(r, list) and r and isinstance(r[0], int)) or r[0] < cutoff]
        rows = rows[-limit:]
        if not rows:
            raise ProviderError(self.name, "empty", "no fixture rows in range")
        retrieved_at = self._now()
        candles, issues = parse_rows(rows, symbol, interval, retrieved_at)
        provenance = Provenance(
            provider=self.name, kind=DataKind.FIXTURE, retrieved_at=retrieved_at,
            request={"path": self._path.name, "volume_unit": str(data.get("volume_unit", "usd"))},
            content_sha256=hashlib.sha256(raw).hexdigest(),
        )
        # candles keep file order, so the validator can catch out-of-order fixtures
        batch = CandleBatch(symbol=symbol, interval=interval, provenance=provenance, candles=candles, parse_issues=issues)
        return FetchResult(batch=batch, raw=raw)
