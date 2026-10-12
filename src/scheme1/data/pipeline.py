"""Fetch -> validate -> store, for one symbol and interval."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from scheme1.contracts import DataStatus, Interval, ValidationReport, new_id
from scheme1.data.providers.base import MarketDataProvider, ProviderError
from scheme1.data.store import CandleStore
from scheme1.data.validate import ValidatorConfig, freshness_status, validate_batch


@dataclass(frozen=True)
class IngestResult:
    report: ValidationReport
    clean_count: int
    revised_count: int
    error: str | None = None


def ingest(
    provider: MarketDataProvider,
    store: CandleStore,
    symbol: str,
    interval: Interval,
    now: datetime,
    *,
    limit: int = 300,
    end: datetime | None = None,
    config: ValidatorConfig = ValidatorConfig(),
) -> IngestResult:
    """Never raises for provider problems: a failed fetch is stored and returned as UNAVAILABLE."""
    try:
        fetched = provider.fetch_candles(symbol, interval, limit=limit, end=end)
    except ProviderError as err:
        report = ValidationReport(batch_id=new_id("batch"), status=DataStatus.UNAVAILABLE, candles_in=0,
                                  candles_out=0, checked_at=now)
        store.save_ingest(report, symbol, interval, error=str(err))
        return IngestResult(report=report, clean_count=0, revised_count=0, error=str(err))

    batch = fetched.batch
    first = min((c.open_time for c in batch.candles), default=None)
    previous = store.latest_clean_before(symbol, interval, first) if first is not None else None
    result = validate_batch(batch, now, config, previous=previous)
    revised = store.save_ingest(result.report, symbol, interval, batch=batch, raw=fetched.raw, clean=result.clean)
    return IngestResult(report=result.report, clean_count=len(result.clean), revised_count=revised)


def stored_freshness(store: CandleStore, symbol: str, interval: Interval, now: datetime,
                     config: ValidatorConfig = ValidatorConfig()) -> DataStatus:
    """Freshness of what is already stored, for callers that did not just ingest."""
    newest = store.clean_candles(symbol, interval, limit=1)
    return freshness_status(newest[0].open_time if newest else None, interval, now, config)
