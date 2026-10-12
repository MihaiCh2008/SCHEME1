"""Turn raw OHLCV rows into ``Candle`` objects, reporting bad rows instead of raising."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

from scheme1.contracts import Candle, DataIssue, Interval, IssueCode


def _dec(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
        # floats are refused on purpose: callers parse JSON with parse_float=Decimal
        raise InvalidOperation(f"not an exact number: {value!r}")
    d = Decimal(value)
    if not d.is_finite():
        raise InvalidOperation(f"not finite: {value!r}")
    return d


def parse_rows(
    rows: Sequence[Any], symbol: str, interval: Interval, retrieved_at: datetime
) -> tuple[tuple[Candle, ...], tuple[DataIssue, ...]]:
    """Each row is ``[open_time_unix_seconds, open, high, low, close, volume]``.

    A candle counts as closed only if its whole interval ended at or before ``retrieved_at``.
    """
    candles: list[Candle] = []
    issues: list[DataIssue] = []
    for index, row in enumerate(rows):
        try:
            if not isinstance(row, (list, tuple)) or len(row) != 6:
                raise InvalidOperation(f"expected 6 fields, got {row!r}")
            ts = row[0]
            if isinstance(ts, bool) or not isinstance(ts, (int, Decimal)) or Decimal(ts) != int(ts):
                raise InvalidOperation(f"bad timestamp {ts!r}")
            open_time = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            o, h, low, c, v = (_dec(x) for x in row[1:])
        except (InvalidOperation, ValueError, OverflowError, OSError) as err:
            issues.append(DataIssue(code=IssueCode.MALFORMED, detail=f"row {index}: {err}"[:500]))
            continue
        if min(o, h, low, c) <= 0:
            issues.append(DataIssue(code=IssueCode.NONPOSITIVE_PRICE, open_time=open_time,
                                    detail=f"row {index}: o={o} h={h} l={low} c={c}"))
            continue
        if v < 0:
            issues.append(DataIssue(code=IssueCode.NEGATIVE_VOLUME, open_time=open_time, detail=f"row {index}: v={v}"))
            continue
        if h < max(o, c, low) or low > min(o, c, h):
            issues.append(DataIssue(code=IssueCode.INCONSISTENT_OHLC, open_time=open_time,
                                    detail=f"row {index}: o={o} h={h} l={low} c={c}"))
            continue
        candles.append(Candle(
            symbol=symbol, interval=interval, open_time=open_time, open=o, high=h, low=low, close=c, volume=v,
            closed=open_time + interval.duration <= retrieved_at,
        ))
    return tuple(candles), tuple(issues)
