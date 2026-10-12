import math
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scheme1.contracts import Interval
from scheme1.data.store import CandleStore
from scheme1.features import FEATURE_KEYS, LOOKBACK, compute_features
from scheme1.features.from_store import snapshot_at
from data_helpers import SYMBOL, make_batch, make_candle

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)
M5 = Interval.M5


def candle(i, close, open_=None, high=None, low=None, volume="100", **kw):
    close = Decimal(str(close))
    open_ = Decimal(str(open_)) if open_ is not None else close
    high = Decimal(str(high)) if high is not None else max(open_, close)
    low = Decimal(str(low)) if low is not None else min(open_, close)
    return make_candle(T0 + i * STEP, open=open_, high=high, low=low, close=close, volume=Decimal(volume), **kw)


def random_walk(n, seed=7):
    rng = random.Random(seed)
    out, p = [], 1e-5
    for i in range(n):
        o = p
        p = p * math.exp(rng.gauss(0, 0.03))
        hi, lo = max(o, p) * (1 + rng.random() * 0.02), min(o, p) * (1 - rng.random() * 0.02)
        out.append(candle(i, f"{p:.12e}", f"{o:.12e}", f"{hi:.12e}", f"{lo:.12e}", volume=f"{rng.random() * 1000:.4f}"))
    return out


def at_close(i):
    """The moment candle i closes."""
    return T0 + (i + 1) * STEP


def feats(candles, at):
    return compute_features(candles, SYMBOL, M5, at)


def test_all_keys_present():
    snap = feats(random_walk(80), at_close(79))
    assert tuple(snap.values) == FEATURE_KEYS
    assert snap.warmup_complete and snap.values["bars_available"] == LOOKBACK
    assert snap.feature_set_version == "f1.0.0"


def test_nothing_closed_yet_returns_none():
    cs = random_walk(5)
    assert feats(cs, T0) is None                      # first candle has not closed
    assert feats([candle(0, 1, closed=False)], at_close(0)) is None


def test_as_of_is_close_of_last_candle_used():
    cs = random_walk(20)
    snap = feats(cs, at_close(9) + timedelta(minutes=2))  # mid-way through candle 10
    assert snap.as_of == at_close(9)
    assert snap.values["bars_available"] == 10


def test_no_look_ahead_future_candles_change_nothing():
    cs = random_walk(120)
    t = at_close(80)
    base = feats(cs[:81], t)
    assert feats(cs, t) == base
    # wildly different future, and an open candle at the edge: still identical
    future = [candle(i, "999", volume="1e9") for i in range(81, 120)]
    assert feats(cs[:81] + future, t) == base
    snap = feats(cs[:81] + [candle(81, "5", closed=False)], at_close(81))
    assert snap.as_of == at_close(80) and snap.values == base.values


def test_same_last_window_gives_same_values_regardless_of_older_history():
    cs = random_walk(200)
    t = at_close(199)
    assert feats(cs, t).values == feats(cs[-LOOKBACK:], t).values


def test_warm_up():
    cs = random_walk(13)
    v = feats(cs, at_close(12)).values
    snap = feats(cs, at_close(12))
    assert not snap.warmup_complete
    assert v["ret_12"] is not None and v["vol_12"] is not None and v["rsi_14"] is None
    assert v["vol_48"] is None and v["range_pos_48"] is None and v["ema_ratio_9_21"] is None
    assert v["ret_1"] == pytest.approx(math.log(float(cs[12].close) / float(cs[11].close)))


def test_gap_resets_the_run():
    cs = random_walk(70)
    del cs[30]
    snap = feats(cs, at_close(69))
    assert snap.values["bars_available"] == 39 and not snap.warmup_complete


def test_steady_growth_values():
    cs = [candle(i, 1.01 ** i, open_=1.01 ** max(i - 1, 0)) for i in range(LOOKBACK)]
    v = feats(cs, at_close(LOOKBACK - 1)).values
    assert v["ret_1"] == pytest.approx(math.log(1.01))
    assert v["ret_12"] == pytest.approx(12 * math.log(1.01))
    assert v["vol_12"] == pytest.approx(0, abs=1e-12)
    assert v["rsi_14"] == 100.0
    assert v["drawdown_48"] == pytest.approx(0, abs=1e-12)
    assert v["range_pos_48"] == pytest.approx(1)
    assert v["ema_ratio_9_21"] > 0 and v["close_vs_ema_21"] > 0


def test_rsi_matches_wilder_reference():
    # StockCharts' worked Wilder RSI example: first RSI(14) = 70.53, the next one = 66.32
    closes = ["44.3389", "44.0902", "44.1497", "43.6124", "44.3278", "44.8264", "45.0955", "45.4245", "45.8433",
              "46.0826", "45.8931", "46.0328", "45.6140", "46.2820", "46.2820", "46.0028"]
    cs = [candle(i, c) for i, c in enumerate(closes)]
    assert feats(cs[:15], at_close(14)).values["rsi_14"] == pytest.approx(70.53, abs=0.01)
    assert feats(cs, at_close(15)).values["rsi_14"] == pytest.approx(66.32, abs=0.01)


def test_zero_denominators_give_none_not_errors():
    flat = [candle(i, 2, volume="0") for i in range(LOOKBACK)]
    v = feats(flat, at_close(LOOKBACK - 1)).values
    assert v["rsi_14"] is None and v["range_pos_48"] is None and v["volume_ratio_12_48"] is None
    assert v["vol_48"] == 0 and v["zero_volume_frac_48"] == 1 and v["atr_pct_14"] == 0


def test_other_symbols_are_ignored():
    cs = random_walk(20) + [make_candle(T0 + 25 * STEP, symbol="solana:Other111111111111111111111111111111111")]
    assert feats(cs, at_close(30)).as_of == at_close(19)


def test_snapshot_from_store_uses_only_closed_candles_and_cites_batches():
    store = CandleStore()
    cs = random_walk(100)
    batch_a, batch_b = make_batch(cs[:60]), make_batch(cs[60:])
    from scheme1.data.validate import validate_batch
    for b in (batch_a, batch_b):
        r = validate_batch(b, b.candles[-1].close_time)
        store.save_ingest(r.report, SYMBOL, M5, batch=b, raw=b"{}", clean=r.clean)

    snap = snapshot_at(store, SYMBOL, M5, at_close(70))
    assert snap.as_of == at_close(70)
    assert snap == compute_features(cs[:71], SYMBOL, M5, at_close(70), (batch_a.batch_id, batch_b.batch_id))
    # one microsecond before candle 70 closes, it is not used
    assert snapshot_at(store, SYMBOL, M5, at_close(70) - timedelta(microseconds=1)).as_of == at_close(69)
    # the last 64 candles (36..99) span both batches; candles 0..59 alone come only from batch A
    assert set(snapshot_at(store, SYMBOL, M5, at_close(99)).input_batch_ids) == {batch_a.batch_id, batch_b.batch_id}
    assert snapshot_at(store, SYMBOL, M5, at_close(59)).input_batch_ids == (batch_a.batch_id,)
