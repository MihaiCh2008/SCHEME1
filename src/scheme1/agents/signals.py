"""Deterministic signals the agents are built from.

Each signal reads a few features and returns a strength in [-1, 1] (positive = bullish) with the
evidence behind it, or ``None`` when an input is not computable. Price moves are divided by the
candle volatility, so the same rule means the same thing on 1m and 1h candles and on quiet and wild tokens.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scheme1.agents.inputs import AgentInputs
from scheme1.contracts import Evidence, EvidenceKind

AGENT_CONFIG_VERSION = "a1.0.0"


@dataclass(frozen=True)
class AgentConfig:
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    volume_surge: float = 1.5  # volume_ratio_12_48 at or above this confirms a move
    volume_fading: float = 0.5  # at or below this halves the market score
    max_atr_pct: float = 0.05  # average candle range above 5% of price is high risk
    max_zero_volume_frac: float = 0.2  # more than 20% of candles without trades is illiquid
    deep_drawdown: float = -0.3  # 30% below the 48-candle high
    version: str = AGENT_CONFIG_VERSION


@dataclass(frozen=True)
class Signal:
    name: str
    strength: float  # -1..1, positive = bullish
    evidence: tuple[Evidence, ...]


def fact(key: str, value: float | bool | str) -> Evidence:
    shown = f"{value:.4g}" if isinstance(value, float) else str(value)
    return Evidence(kind=EvidenceKind.FACT, statement=f"{key} = {shown}", refs=(key,))


def note(statement: str, *refs: str, kind: EvidenceKind = EvidenceKind.INTERPRETATION) -> Evidence:
    return Evidence(kind=kind, statement=statement, refs=refs)


def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, x))


def _candle_vol(x: AgentInputs) -> tuple[str, float] | None:
    """Per-candle volatility to scale moves by: the longer window if available."""
    for key in ("vol_48", "vol_12"):
        v = x.f(key)
        if v:
            return key, v
    return None


# --------------------------------------------------------------- market signals

def momentum(x: AgentInputs, cfg: AgentConfig) -> Signal | None:
    ret, vol = x.f("ret_12"), x.f("vol_12")
    if ret is None or not vol:
        return None
    z = ret / (vol * math.sqrt(12))
    s = math.tanh(z)
    word = "up" if ret > 0 else "down"
    return Signal("momentum", s, (
        fact("ret_12", ret), fact("vol_12", vol),
        note(f"price is {word} {abs(z):.2f} standard deviations over 12 candles", "ret_12", "vol_12"),
    ))


def range_position(x: AgentInputs, cfg: AgentConfig) -> Signal | None:
    pos = x.f("range_pos_48")
    if pos is None:
        return None
    where = "upper" if pos >= 0.5 else "lower"
    return Signal("range_position", 2 * pos - 1, (
        fact("range_pos_48", pos),
        note(f"close is in the {where} part of the 48-candle range", "range_pos_48"),
    ))


# ------------------------------------------------------------ technical signals

def trend(x: AgentInputs, cfg: AgentConfig) -> Signal | None:
    ratio, scale = x.f("ema_ratio_9_21"), _candle_vol(x)
    if ratio is None or scale is None:
        return None
    vol_key, vol = scale
    s = math.tanh(ratio / (2 * vol))
    word = "above" if ratio > 0 else "below"
    return Signal("trend", s, (
        fact("ema_ratio_9_21", ratio), fact(vol_key, vol),
        note(f"fast average (EMA9) is {word} slow average (EMA21)", "ema_ratio_9_21"),
    ))


def price_vs_ema(x: AgentInputs, cfg: AgentConfig) -> Signal | None:
    gap, scale = x.f("close_vs_ema_21"), _candle_vol(x)
    if gap is None or scale is None:
        return None
    vol_key, vol = scale
    s = math.tanh(gap / (2 * vol * math.sqrt(21)))
    word = "above" if gap > 0 else "below"
    return Signal("price_vs_ema", s, (
        fact("close_vs_ema_21", gap), fact(vol_key, vol),
        note(f"close is {word} EMA21", "close_vs_ema_21"),
    ))


def rsi(x: AgentInputs, cfg: AgentConfig) -> Signal | None:
    r = x.f("rsi_14")
    if r is None:
        return None
    side = "buyers" if r >= 50 else "sellers"
    return Signal("rsi", (r - 50) / 50, (
        fact("rsi_14", r),
        note(f"RSI says {side} were stronger over the last 14 candles", "rsi_14"),
    ))


# ---------------------------------------------------- advocate-only signals

def overbought(x: AgentInputs, cfg: AgentConfig) -> Signal | None:
    r = x.f("rsi_14")
    if r is None or r <= cfg.rsi_overbought:
        return None
    return Signal("overbought", -(r - cfg.rsi_overbought) / (100 - cfg.rsi_overbought), (
        fact("rsi_14", r),
        note(f"RSI above {cfg.rsi_overbought:g}: the move may be stretched and could pull back", "rsi_14",
             kind=EvidenceKind.HYPOTHESIS),
    ))


def oversold(x: AgentInputs, cfg: AgentConfig) -> Signal | None:
    r = x.f("rsi_14")
    if r is None or r >= cfg.rsi_oversold:
        return None
    return Signal("oversold", (cfg.rsi_oversold - r) / cfg.rsi_oversold, (
        fact("rsi_14", r),
        note(f"RSI below {cfg.rsi_oversold:g}: selling may be exhausted and could bounce", "rsi_14",
             kind=EvidenceKind.HYPOTHESIS),
    ))


def volume_confirmation(x: AgentInputs, cfg: AgentConfig) -> Signal | None:
    """A volume surge strengthens whichever way price moved; it says nothing on its own."""
    ratio, ret = x.f("volume_ratio_12_48"), x.f("ret_12")
    if ratio is None or ret is None or ret == 0 or ratio < cfg.volume_surge:
        return None
    direction = 1.0 if ret > 0 else -1.0
    word = "rise" if ret > 0 else "fall"
    return Signal("volume_confirmation", direction * min(1.0, (ratio - 1) / 2), (
        fact("volume_ratio_12_48", ratio), fact("ret_12", ret),
        note(f"recent volume is {ratio:.2f}x the usual level, backing the {word}", "volume_ratio_12_48", "ret_12"),
    ))


def weighted(signals: list[tuple[Signal | None, float]]) -> tuple[float, list[Signal]]:
    """Weighted mean of the signals that could be computed (weights renormalized)."""
    present = [(s, w) for s, w in signals if s is not None]
    if not present:
        return 0.0, []
    total = sum(w for _, w in present)
    return _clamp(sum(s.strength * w for s, w in present) / total), [s for s, _ in present]
