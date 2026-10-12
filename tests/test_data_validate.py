from datetime import datetime, timedelta, timezone
from decimal import Decimal

from scheme1.contracts import DataIssue, DataStatus, Interval, IssueCode
from scheme1.data.validate import ValidatorConfig, freshness_status, validate_batch
from data_helpers import make_batch, make_candle

T0 = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)


def series(n, start=T0, closed_last=True):
    out = [make_candle(start + i * STEP) for i in range(n)]
    if not closed_last:
        out[-1] = make_candle(start + (n - 1) * STEP, closed=False)
    return out


def check(candles, now=None, **kw):
    now = now or candles[-1].open_time + STEP + timedelta(seconds=30)
    return validate_batch(make_batch(candles), now, **kw)


def codes(result):
    return [i.code for i in result.report.issues]


def test_clean_series_is_ok():
    r = check(series(6))
    assert r.report.status is DataStatus.OK and codes(r) == [] and r.report.candles_out == 6
    assert r.report.newest_closed_open_time == T0 + 5 * STEP


def test_open_newest_candle_is_dropped_without_an_issue():
    candles = series(6, closed_last=False)
    r = validate_batch(make_batch(candles), T0 + 5 * STEP + timedelta(seconds=30))
    assert r.report.status is DataStatus.OK and r.report.candles_out == 5 and codes(r) == []
    assert all(c.closed for c in r.clean)


def test_open_candle_in_the_middle_degrades():
    candles = series(4)
    candles[1] = make_candle(T0 + STEP, closed=False)
    r = check(candles)
    assert codes(r) == [IssueCode.INCOMPLETE, IssueCode.GAP] and r.report.status is DataStatus.DEGRADED and r.report.candles_out == 3


def test_gap_degrades():
    candles = series(6)
    del candles[2:4]
    r = check(candles)
    assert codes(r) == [IssueCode.GAP] and r.report.status is DataStatus.DEGRADED
    assert "2 missing" in r.report.issues[0].detail and r.report.issues[0].open_time == T0 + 2 * STEP


def test_gap_against_previously_stored_candle():
    prev = make_candle(T0 - 3 * STEP)
    r = check(series(3), previous=prev)
    assert codes(r) == [IssueCode.GAP]
    r = check(series(3), previous=make_candle(T0 - STEP))
    assert codes(r) == []


def test_identical_duplicate_dropped_conflicting_duplicate_removed():
    candles = series(4)
    r = check(candles[:2] + [candles[1]] + candles[2:])
    assert codes(r) == [IssueCode.DUPLICATE]
    assert r.report.candles_out == 4

    conflicting = make_candle(T0 + STEP, price="2")
    r = check(candles[:2] + [conflicting] + candles[2:])
    assert IssueCode.DUPLICATE in codes(r) and IssueCode.GAP in codes(r)  # neither copy is trusted
    assert T0 + STEP not in [c.open_time for c in r.clean]


def test_out_of_order_is_reported_and_sorted():
    candles = series(4)
    r = check([candles[0], candles[2], candles[1], candles[3]], now=T0 + 4 * STEP + timedelta(seconds=30))
    assert codes(r) == [IssueCode.OUT_OF_ORDER] and r.report.status is DataStatus.DEGRADED
    assert [c.open_time for c in r.clean] == [c.open_time for c in candles]


def test_misaligned_and_wrong_symbol_are_dropped():
    candles = series(3)
    odd = make_candle(T0 + 3 * STEP + timedelta(minutes=1))
    other = make_candle(T0 + 4 * STEP, symbol="solana:Other111111111111111111111111111111111")
    r = check(candles + [odd, other], now=T0 + 3 * STEP + timedelta(seconds=30))
    assert IssueCode.MISALIGNED in codes(r) and IssueCode.WRONG_SYMBOL in codes(r)
    assert r.report.candles_out == 3 and r.report.status is DataStatus.DEGRADED


def test_stale_when_newest_closed_candle_is_too_old():
    candles = series(3)
    newest_close = T0 + 3 * STEP
    limit = ValidatorConfig().max_age(Interval.M5)
    assert check(candles, now=newest_close + limit).report.status is DataStatus.OK
    assert check(candles, now=newest_close + limit + timedelta(seconds=1)).report.status is DataStatus.STALE


def test_stale_beats_degraded():
    candles = series(6)
    del candles[2]
    assert check(candles, now=T0 + timedelta(days=1)).report.status is DataStatus.STALE


def test_outliers_are_kept_but_degrade():
    candles = series(4)
    candles[2] = make_candle(T0 + 2 * STEP, price="1", high=Decimal("50"), low=Decimal("0.99"))
    r = check(candles)
    assert codes(r) == [IssueCode.OUTLIER] and r.report.candles_out == 4 and r.report.status is DataStatus.DEGRADED

    candles = series(4)
    candles[2] = make_candle(T0 + 2 * STEP, price="100")
    r = check(candles)
    assert codes(r) == [IssueCode.OUTLIER, IssueCode.OUTLIER]  # jump up, then back down


def test_normal_memecoin_pump_is_not_an_outlier():
    candles = series(3)
    candles[2] = make_candle(T0 + 2 * STEP, price="5", low=Decimal("1"), high=Decimal("6"), open=Decimal("1"))
    assert codes(check(candles)) == []


def test_nothing_usable_is_unavailable():
    only_open = [make_candle(T0, closed=False)]
    assert validate_batch(make_batch(only_open), T0 + timedelta(seconds=10)).report.status is DataStatus.UNAVAILABLE
    parse_only = make_batch([], retrieved_at=T0, parse_issues=(DataIssue(code=IssueCode.MALFORMED, detail="x"),))
    report = validate_batch(parse_only, T0).report
    assert report.status is DataStatus.UNAVAILABLE and report.candles_in == 1 and report.candles_out == 0


def test_freshness_status_helper():
    assert freshness_status(None, Interval.H1, T0) is DataStatus.UNAVAILABLE
    assert freshness_status(T0, Interval.H1, T0 + timedelta(hours=1, minutes=2)) is DataStatus.OK
    assert freshness_status(T0, Interval.H1, T0 + timedelta(hours=2, minutes=3)) is DataStatus.STALE
