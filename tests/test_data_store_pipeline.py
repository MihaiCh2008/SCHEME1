import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from scheme1.contracts import DataKind, DataStatus, Interval
from scheme1.data.http import HttpResponse, PacedClient, RetryPolicy
from scheme1.data.pipeline import ingest, stored_freshness
from scheme1.data.providers.fixture import FixtureProvider
from scheme1.data.providers.geckoterminal import GeckoTerminalProvider
from scheme1.data.store import CandleStore
from data_helpers import SYMBOL, FakeClock, ScriptedTransport

FIXTURE = Path(__file__).parent / "fixtures" / "fixture_pool_5m.json"
T0 = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)  # first fixture candle
STEP = timedelta(minutes=5)
AFTER_LAST = T0 + 12 * STEP + timedelta(seconds=30)  # all 12 fixture candles closed


def test_fixture_ingest_stores_raw_report_and_clean(tmp_path):
    store = CandleStore(tmp_path / "desk.db")
    result = ingest(FixtureProvider(FIXTURE, now=lambda: AFTER_LAST), store, SYMBOL, Interval.M5, AFTER_LAST)
    assert result.report.status is DataStatus.OK and result.clean_count == 12 and result.error is None

    stored = store.clean_candles(SYMBOL, Interval.M5)
    assert len(stored) == 12 and stored[0].open_time == T0
    assert stored[0].open == Decimal(json.loads(FIXTURE.read_text())["rows"][0][1])  # exact decimal text kept
    assert store.raw_payload(result.report.batch_id) == FIXTURE.read_bytes()
    report, error = store.latest_report(SYMBOL, Interval.M5)
    assert report == result.report and error is None
    store.close()

    # the data is labelled as fixture data in storage
    db = sqlite3.connect(tmp_path / "desk.db")
    assert db.execute("SELECT kind FROM raw_batches").fetchone()[0] == DataKind.FIXTURE.value


def test_end_and_limit_select_a_window_and_open_candle_is_not_stored():
    store = CandleStore()
    now = T0 + 5 * STEP + timedelta(seconds=30)  # candle 5 (18:25) still open
    provider = FixtureProvider(FIXTURE, now=lambda: now)
    result = ingest(provider, store, SYMBOL, Interval.M5, now, end=T0 + 6 * STEP, limit=4)
    assert result.report.status is DataStatus.OK
    assert [c.open_time for c in store.clean_candles(SYMBOL, Interval.M5)] == [T0 + i * STEP for i in (2, 3, 4)]


def test_provider_failure_is_stored_as_unavailable():
    store = CandleStore()
    r429 = HttpResponse(429, b'{"status":{"error_code":429}}', {"retry-after": "0"})
    client = PacedClient(ScriptedTransport(r429, r429), min_interval_s=0, retry=RetryPolicy(max_attempts=2),
                         clock=FakeClock())
    gt = GeckoTerminalProvider(client, now=lambda: AFTER_LAST)
    pool = "solana:5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9"
    result = ingest(gt, store, pool, Interval.M5, AFTER_LAST)
    assert result.report.status is DataStatus.UNAVAILABLE and "rate_limited" in result.error
    report, error = store.latest_report(pool, Interval.M5)
    assert report.status is DataStatus.UNAVAILABLE and "rate_limited" in error
    assert store.clean_candles(pool, Interval.M5) == ()


def test_revisions_are_counted_and_latest_values_win(tmp_path):
    store = CandleStore()
    ingest(FixtureProvider(FIXTURE, now=lambda: AFTER_LAST), store, SYMBOL, Interval.M5, AFTER_LAST)

    doc = json.loads(FIXTURE.read_text())
    revised_close = doc["rows"][-1][2]  # provider revised the last close up to the candle's high
    doc["rows"][-1][4] = revised_close
    revised_file = tmp_path / "revised.json"
    revised_file.write_text(json.dumps(doc))
    result = ingest(FixtureProvider(revised_file, now=lambda: AFTER_LAST), store, SYMBOL, Interval.M5, AFTER_LAST)
    assert result.report.status is DataStatus.OK and result.revised_count == 1
    assert store.clean_candles(SYMBOL, Interval.M5)[-1].close == Decimal(revised_close)


def test_gap_between_batches_is_detected():
    store = CandleStore()
    first_now = T0 + 4 * STEP + timedelta(seconds=30)
    ingest(FixtureProvider(FIXTURE, now=lambda: first_now), store, SYMBOL, Interval.M5, first_now, end=T0 + 4 * STEP)
    # next batch skips candles 4 and 5
    result = ingest(FixtureProvider(FIXTURE, now=lambda: AFTER_LAST), store, SYMBOL, Interval.M5, AFTER_LAST, limit=6)
    assert result.report.status is DataStatus.DEGRADED
    assert [i.code.value for i in result.report.issues] == ["gap"]


def test_failed_write_rolls_back_everything():
    store = CandleStore()
    provider = FixtureProvider(FIXTURE, now=lambda: AFTER_LAST)
    fetched = provider.fetch_candles(SYMBOL, Interval.M5)
    from scheme1.data.validate import validate_batch
    result = validate_batch(fetched.batch, AFTER_LAST)
    store.save_ingest(result.report, SYMBOL, Interval.M5, batch=fetched.batch, raw=fetched.raw, clean=result.clean)
    with pytest.raises(sqlite3.IntegrityError):  # same batch id again
        store.save_ingest(result.report, SYMBOL, Interval.M5, batch=fetched.batch, raw=fetched.raw,
                          clean=result.clean[:1])
    assert len(store.statuses(SYMBOL, Interval.M5)) == 1


def test_stored_freshness():
    store = CandleStore()
    assert stored_freshness(store, SYMBOL, Interval.M5, AFTER_LAST) is DataStatus.UNAVAILABLE
    ingest(FixtureProvider(FIXTURE, now=lambda: AFTER_LAST), store, SYMBOL, Interval.M5, AFTER_LAST)
    assert stored_freshness(store, SYMBOL, Interval.M5, AFTER_LAST) is DataStatus.OK
    assert stored_freshness(store, SYMBOL, Interval.M5, AFTER_LAST + timedelta(hours=1)) is DataStatus.STALE
