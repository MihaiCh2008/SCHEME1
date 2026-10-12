"""Build a feature snapshot from stored clean candles (the same path for backtests and paper trading)."""

from __future__ import annotations

from datetime import datetime, timedelta

from scheme1.contracts import FeatureSnapshot, Interval
from scheme1.data.store import CandleStore
from scheme1.features.engine import LOOKBACK, compute_features

_EPS = timedelta(microseconds=1)  # makes the store's exclusive ``end`` include open_time == at - interval


def snapshot_at(store: CandleStore, symbol: str, interval: Interval, at: datetime) -> FeatureSnapshot | None:
    """Features known at ``at``: only candles that had closed by then are read."""
    # open_time <= at - interval  <=>  close_time <= at
    rows = store.clean_candles_with_batches(symbol, interval, end=at - interval.duration + _EPS, limit=LOOKBACK)
    if not rows:
        return None
    candles = [c for c, _ in rows]
    return compute_features(candles, symbol, interval, at, input_batch_ids=tuple(b for _, b in rows))
