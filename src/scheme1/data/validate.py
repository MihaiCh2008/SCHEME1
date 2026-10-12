"""Candle validation and freshness.

Status precedence: UNAVAILABLE > STALE > DEGRADED > OK.
- UNAVAILABLE: no usable closed candle.
- STALE: the newest closed candle ended more than ``max_age`` before ``now``.
- DEGRADED: any recorded issue. An open newest candle is normal and is not recorded as one.

Clean output = closed, correctly labelled, aligned, de-duplicated candles, oldest first.
Outlier candles stay in the clean output (dropping them would create gaps) but make the batch DEGRADED.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from scheme1.contracts import Candle, CandleBatch, DataIssue, DataStatus, Interval, IssueCode, ValidationReport

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class ValidatorConfig:
    # Memecoins move a lot, so these only catch broken data, not ordinary pumps.
    max_bar_range: Decimal = Decimal(20)  # high / low within one candle
    max_close_jump: Decimal = Decimal(20)  # close / previous close (either direction)
    # Allowed age of the newest closed candle = one interval + this slack (provider cache and latency).
    freshness_slack: timedelta = timedelta(seconds=150)

    def max_age(self, interval: Interval) -> timedelta:
        return interval.duration + self.freshness_slack


@dataclass(frozen=True)
class ValidationResult:
    report: ValidationReport
    clean: tuple[Candle, ...]


def is_aligned(open_time: datetime, interval: Interval) -> bool:
    # GeckoTerminal buckets are aligned to the unix epoch (4h bars open at 00/04/08/... UTC), checked 2026-10-12.
    return (open_time - _EPOCH) % interval.duration == timedelta(0)


def freshness_status(newest_closed_open_time: datetime | None, interval: Interval, now: datetime,
                     config: ValidatorConfig = ValidatorConfig()) -> DataStatus:
    if newest_closed_open_time is None:
        return DataStatus.UNAVAILABLE
    age = now - (newest_closed_open_time + interval.duration)
    return DataStatus.STALE if age > config.max_age(interval) else DataStatus.OK


def validate_batch(
    batch: CandleBatch,
    now: datetime,
    config: ValidatorConfig = ValidatorConfig(),
    previous: Candle | None = None,
) -> ValidationResult:
    """``previous`` is the newest stored clean candle before this batch; it lets us see gaps between batches."""
    issues: list[DataIssue] = list(batch.parse_issues)
    step = batch.interval.duration

    for a, b in zip(batch.candles, batch.candles[1:]):
        if b.open_time < a.open_time:
            issues.append(DataIssue(code=IssueCode.OUT_OF_ORDER, open_time=b.open_time,
                                    detail=f"{b.open_time.isoformat()} came after {a.open_time.isoformat()}"))
            break

    by_time: dict[datetime, Candle] = {}
    conflicted: set[datetime] = set()
    for c in batch.candles:
        if c.symbol != batch.symbol or c.interval != batch.interval:
            issues.append(DataIssue(code=IssueCode.WRONG_SYMBOL, open_time=c.open_time,
                                    detail=f"candle is {c.symbol} {c.interval}, batch is {batch.symbol} {batch.interval}"))
            continue
        if not is_aligned(c.open_time, batch.interval):
            issues.append(DataIssue(code=IssueCode.MISALIGNED, open_time=c.open_time,
                                    detail=f"not on a {batch.interval.value} boundary"))
            continue
        seen = by_time.get(c.open_time)
        if seen is not None:
            same = seen == c
            issues.append(DataIssue(code=IssueCode.DUPLICATE, open_time=c.open_time,
                                    detail="identical copy dropped" if same else "conflicting values; candle dropped"))
            if not same:
                conflicted.add(c.open_time)
            continue
        by_time[c.open_time] = c
    for t in conflicted:  # we can't tell which copy is right, so neither is used
        del by_time[t]

    ordered = sorted(by_time.values(), key=lambda c: c.open_time)
    clean: list[Candle] = []
    for c in ordered:
        if not c.closed:
            if c is not ordered[-1]:
                issues.append(DataIssue(code=IssueCode.INCOMPLETE, open_time=c.open_time,
                                        detail="open candle that is not the newest"))
            # the newest candle being open is normal; it is just not used
            continue
        clean.append(c)

    if clean and previous is not None and previous.open_time < clean[0].open_time:
        _check_gap(previous, clean[0], step, issues)
    for a, b in zip(clean, clean[1:]):
        _check_gap(a, b, step, issues)

    prev_close = previous.close if previous is not None and clean and previous.open_time < clean[0].open_time else None
    for c in clean:
        if c.high / c.low > config.max_bar_range:
            issues.append(DataIssue(code=IssueCode.OUTLIER, open_time=c.open_time,
                                    detail=f"high/low = {c.high / c.low:.4g} > {config.max_bar_range}"))
        if prev_close is not None:
            jump = max(c.close / prev_close, prev_close / c.close)
            if jump > config.max_close_jump:
                issues.append(DataIssue(code=IssueCode.OUTLIER, open_time=c.open_time,
                                        detail=f"close moved {jump:.4g}x from previous close"))
        prev_close = c.close

    newest = clean[-1].open_time if clean else None
    status = freshness_status(newest, batch.interval, now, config)
    if status is DataStatus.OK and issues:
        status = DataStatus.DEGRADED

    report = ValidationReport(
        batch_id=batch.batch_id, status=status, issues=tuple(issues), candles_in=len(batch.candles) + len(batch.parse_issues),
        candles_out=len(clean), newest_closed_open_time=newest, checked_at=now,
    )
    return ValidationResult(report=report, clean=tuple(clean))


def _check_gap(a: Candle, b: Candle, step: timedelta, issues: list[DataIssue]) -> None:
    missing = (b.open_time - a.open_time) // step - 1
    if missing > 0:
        issues.append(DataIssue(code=IssueCode.GAP, open_time=a.open_time + step,
                                detail=f"{missing} missing candle(s) before {b.open_time.isoformat()}"))
