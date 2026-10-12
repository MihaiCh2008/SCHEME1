"""Feature engine.

Rules
- No look-ahead: only candles whose close time is at or before ``at`` are used, and
  ``FeatureSnapshot.as_of`` is the close time of the last candle used.
- No computing across gaps: only the trailing run of consecutive candles counts.
- Fixed window: every feature reads at most the last ``LOOKBACK`` candles, so the same
  last ``LOOKBACK`` candles always give the same values, however much older history was loaded.
- ``None`` means "not computable": too few candles for that feature, or a zero denominator.
  ``warmup_complete`` is true only when the run holds ``LOOKBACK`` candles.

Prices are converted to float here: features are statistics, not money (D5).
"""

from __future__ import annotations

import math
from datetime import datetime
from statistics import fmean, stdev
from typing import Sequence

from scheme1.contracts import Candle, FeatureSnapshot, Interval

FEATURE_SET_VERSION = "f1.0.0"
LOOKBACK = 64  # candles; covers the longest feature (48 returns) and EMA-21 settling

FEATURE_KEYS = (
    "bars_available",       # length of the trailing gap-free run used (capped at LOOKBACK)
    "ret_1", "ret_3", "ret_12",            # log return over n candles
    "vol_12", "vol_48",                   # sample std-dev of 1-candle log returns
    "ema_ratio_9_21",       # EMA9 / EMA21 - 1
    "close_vs_ema_21",      # close / EMA21 - 1
    "rsi_14",               # Wilder RSI, 0..100
    "atr_pct_14",           # Wilder ATR / close
    "volume_usd_12",        # sum of candle volume over 12 candles (USD for GeckoTerminal)
    "volume_ratio_12_48",   # mean volume of last 12 / mean volume of last 48
    "range_pos_48",         # (close - low48) / (high48 - low48), 0..1
    "drawdown_48",          # close / max(high48) - 1, <= 0
    "zero_volume_frac_48",  # share of the last 48 candles with no trades
)


def compute_features(
    candles: Sequence[Candle],
    symbol: str,
    interval: Interval,
    at: datetime,
    input_batch_ids: tuple[str, ...] = (),
) -> FeatureSnapshot | None:
    """Return the snapshot known at ``at``, or ``None`` if no closed candle is available by then."""
    usable = sorted(
        (c for c in candles
         if c.closed and c.symbol == symbol and c.interval == interval and c.close_time <= at),
        key=lambda c: c.open_time,
    )
    if not usable:
        return None
    run = _trailing_run(usable, interval)[-LOOKBACK:]
    values = _values(run)
    return FeatureSnapshot(
        symbol=symbol,
        interval=interval,
        as_of=run[-1].close_time,
        feature_set_version=FEATURE_SET_VERSION,
        values=values,
        input_batch_ids=tuple(sorted(set(input_batch_ids))),
        warmup_complete=len(run) == LOOKBACK,
    )


def _trailing_run(candles: list[Candle], interval: Interval) -> list[Candle]:
    start = len(candles) - 1
    while start > 0 and candles[start].open_time - candles[start - 1].open_time == interval.duration:
        start -= 1
    return candles[start:]


def _values(run: list[Candle]) -> dict[str, float | None]:
    close = [float(c.close) for c in run]
    high = [float(c.high) for c in run]
    low = [float(c.low) for c in run]
    volume = [float(c.volume) for c in run]
    log_ret = [math.log(b / a) for a, b in zip(close, close[1:])]
    n = len(run)

    def ret(k: int) -> float | None:
        return math.log(close[-1] / close[-1 - k]) if n > k else None

    def vol(k: int) -> float | None:
        return stdev(log_ret[-k:]) if len(log_ret) >= k else None

    ema9, ema21 = _ema(close, 9), _ema(close, 21)
    hi48, lo48 = (max(high[-48:]), min(low[-48:])) if n >= 48 else (None, None)
    atr = _wilder_atr(high, low, close, 14)
    vol_mean_48 = fmean(volume[-48:]) if n >= 48 else None

    return {
        "bars_available": float(n),
        "ret_1": ret(1),
        "ret_3": ret(3),
        "ret_12": ret(12),
        "vol_12": vol(12),
        "vol_48": vol(48),
        "ema_ratio_9_21": ema9 / ema21 - 1 if ema9 is not None and ema21 is not None else None,
        "close_vs_ema_21": close[-1] / ema21 - 1 if ema21 is not None else None,
        "rsi_14": _wilder_rsi(close, 14),
        "atr_pct_14": atr / close[-1] if atr is not None else None,
        "volume_usd_12": sum(volume[-12:]) if n >= 12 else None,
        "volume_ratio_12_48": (fmean(volume[-12:]) / vol_mean_48) if vol_mean_48 else None,
        "range_pos_48": (close[-1] - lo48) / (hi48 - lo48) if hi48 is not None and hi48 > lo48 else None,
        "drawdown_48": close[-1] / hi48 - 1 if hi48 is not None else None,
        "zero_volume_frac_48": sum(1 for v in volume[-48:] if v == 0) / 48 if n >= 48 else None,
    }


def _ema(xs: list[float], period: int) -> float | None:
    """EMA seeded with the simple mean of the first ``period`` values of the window."""
    if len(xs) < period:
        return None
    k = 2 / (period + 1)
    value = fmean(xs[:period])
    for x in xs[period:]:
        value = x * k + value * (1 - k)
    return value


def _wilder_rsi(close: list[float], period: int) -> float | None:
    if len(close) < period + 1:
        return None
    changes = [b - a for a, b in zip(close, close[1:])]
    gain = fmean(max(c, 0.0) for c in changes[:period])
    loss = fmean(max(-c, 0.0) for c in changes[:period])
    for c in changes[period:]:
        gain = (gain * (period - 1) + max(c, 0.0)) / period
        loss = (loss * (period - 1) + max(-c, 0.0)) / period
    if gain == 0 and loss == 0:
        return None  # flat: RSI is undefined, not 50
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def _wilder_atr(high: list[float], low: list[float], close: list[float], period: int) -> float | None:
    if len(close) < period + 1:
        return None
    tr = [max(h - lo, abs(h - pc), abs(lo - pc)) for h, lo, pc in zip(high[1:], low[1:], close)]
    atr = fmean(tr[:period])
    for t in tr[period:]:
        atr = (atr * (period - 1) + t) / period
    return atr
