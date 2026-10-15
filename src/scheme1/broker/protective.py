"""Protective exits: a trailing stop and a take profit attached to every position as soon as it is opened.

Trailing stop: the stop starts ``stop_loss_pct`` below the entry. After each candle it moves up to
``trail_pct`` below the highest high so far, and it never moves down. So a small dip doesn't end a
trade, but a real turn down does, and once the price has risen enough a loss is no longer possible.
The stop is raised only *after* a candle is checked: within one candle it is unknown whether the
high came before the low, so that candle's high can't protect a fall in the same candle.

Instead of waiting for a candle to close, each candle after the entry is checked against its low and
high, so a stop fills at the stop price within the candle. Rules, in this order:

1. No volume in the candle -> nothing fills (ARCHITECTURE.md section 3); the levels stay for the next one.
2. Opens at or below the stop (price gapped down) -> sells at the open, worse than the stop.
3. Opens at or above the take profit (gapped up) -> sells at the open.
4. Low reaches the stop and high reaches the take profit in the same candle -> the order inside the
   candle is unknown, so the stop is assumed (the worse case).
5. Low reaches the stop -> sells at the stop price.  6. High reaches the take profit -> sells at it.

The fill price is the reference price minus slippage, and a taker fee applies (charged by the broker).

On a real DEX there are no exchange-held stop orders: a bot has to watch the price and sell. These
rules simulate a watcher that reacts within the candle, so live results would be somewhat worse.
Nothing here can prevent a gap: a token that halves between two trades still sells at half.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from scheme1.contracts import Candle


@dataclass(frozen=True)
class ProtectiveLevels:
    stop_price: Decimal
    take_profit_price: Decimal
    trail_pct: Decimal | None = None  # None = fixed stop
    highest: Decimal = Decimal(0)  # highest high seen since entry

    @classmethod
    def from_entry(cls, entry_price: Decimal, stop_loss_pct: Decimal, take_profit_pct: Decimal,
                   trail_pct: Decimal | None = None) -> ProtectiveLevels:
        if entry_price <= 0:
            raise ValueError("entry price must be positive")
        if trail_pct is not None and not 0 < trail_pct < 100:
            raise ValueError("trail must be between 0 and 100 percent")
        return cls(stop_price=entry_price * (1 - stop_loss_pct / 100),
                   take_profit_price=entry_price * (1 + take_profit_pct / 100),
                   trail_pct=trail_pct, highest=entry_price)

    def after(self, candle: Candle) -> ProtectiveLevels:
        """Levels for the next candle, once ``candle`` closed without an exit. The stop never moves down."""
        if self.trail_pct is None or candle.volume == 0:
            return self
        highest = max(self.highest, candle.high)
        stop = max(self.stop_price, highest * (1 - self.trail_pct / 100))
        return ProtectiveLevels(stop_price=stop, take_profit_price=self.take_profit_price,
                                trail_pct=self.trail_pct, highest=highest)


@dataclass(frozen=True)
class ProtectiveExit:
    reason: Literal["stop_loss", "take_profit"]
    gapped: bool  # True when the candle opened past the level, so the fill is at the open
    reference_price: Decimal  # before slippage
    fill_price: Decimal  # after slippage
    candle_open_time: datetime


def check_candle(candle: Candle, levels: ProtectiveLevels, slippage_pct: Decimal) -> ProtectiveExit | None:
    if candle.volume == 0:
        return None
    stop, tp = levels.stop_price, levels.take_profit_price
    if candle.open <= stop:
        reason, gapped, ref = "stop_loss", True, candle.open
    elif candle.open >= tp:
        reason, gapped, ref = "take_profit", True, candle.open
    elif candle.low <= stop:  # also when the high reached the take profit in the same candle
        reason, gapped, ref = "stop_loss", False, stop
    elif candle.high >= tp:
        reason, gapped, ref = "take_profit", False, tp
    else:
        return None
    return ProtectiveExit(reason=reason, gapped=gapped, reference_price=ref,
                          fill_price=ref * (1 - slippage_pct / 100), candle_open_time=candle.open_time)


def follow(candles: list[Candle], levels: ProtectiveLevels,
           slippage_pct: Decimal) -> tuple[ProtectiveExit | None, ProtectiveLevels]:
    """Check candles in order (oldest first, all after the entry). Returns the first exit, if any,
    and the levels in force at the end, ready for the next candle."""
    for candle in sorted(candles, key=lambda c: c.open_time):
        exit_ = check_candle(candle, levels, slippage_pct)
        if exit_ is not None:
            return exit_, levels
        levels = levels.after(candle)
    return None, levels
