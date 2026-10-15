from datetime import datetime, timezone
from decimal import Decimal

import pytest

from scheme1.broker import ProtectiveLevels, check_candle
from data_helpers import make_candle

T = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
LEVELS = ProtectiveLevels.from_entry(Decimal(100), stop_loss_pct=Decimal(3), take_profit_pct=Decimal(5))
NO_SLIP = Decimal(0)


def c(o, h, lo, cl, volume="10"):
    return make_candle(T, open=Decimal(o), high=Decimal(h), low=Decimal(lo), close=Decimal(cl), volume=Decimal(volume))


def test_levels_from_entry():
    assert LEVELS.stop_price == 97 and LEVELS.take_profit_price == 105


@pytest.mark.parametrize("candle,reason,gapped,price", [
    (c(100, 101, 96, 100), "stop_loss", False, 97),     # dipped through the stop and recovered: still stopped
    (c(100, 106, 99, 104), "take_profit", False, 105),  # touched the take profit
    (c(90, 95, 80, 85), "stop_loss", True, 90),         # opened below the stop: sells at the open, worse
    (c(110, 112, 108, 111), "take_profit", True, 110),  # opened above the take profit: sells at the open
    (c(100, 106, 96, 100), "stop_loss", False, 97),     # both touched: assume the stop came first
    (c(97, 98, 96, 97), "stop_loss", True, 97),         # opening exactly at the stop counts
])
def test_exit_rules(candle, reason, gapped, price):
    e = check_candle(candle, LEVELS, NO_SLIP)
    assert (e.reason, e.gapped, e.reference_price) == (reason, gapped, Decimal(price))
    assert e.candle_open_time == T


def test_no_exit_inside_the_band():
    assert check_candle(c(100, 104, 98, 102), LEVELS, NO_SLIP) is None


def test_no_fill_without_volume():
    assert check_candle(c(90, 90, 90, 90, volume="0"), LEVELS, NO_SLIP) is None


def test_slippage_lowers_the_sell_price():
    e = check_candle(c(100, 101, 96, 100), LEVELS, Decimal(1))
    assert e.reference_price == 97 and e.fill_price == Decimal("96.03")


def test_bad_entry_price_is_refused():
    with pytest.raises(ValueError):
        ProtectiveLevels.from_entry(Decimal(0), Decimal(3), Decimal(5))
