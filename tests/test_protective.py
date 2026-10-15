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


# ------------------------------------------------------------ trailing stop

from datetime import timedelta  # noqa: E402

from scheme1.broker import follow  # noqa: E402

TRAIL = ProtectiveLevels.from_entry(Decimal(100), Decimal(3), Decimal(5), trail_pct=Decimal(2))


def seq(*ohlc):
    return [make_candle(T + i * timedelta(minutes=5), open=Decimal(str(o)), high=Decimal(str(h)),
                        low=Decimal(str(lo)), close=Decimal(str(cl)), volume=Decimal(10))
            for i, (o, h, lo, cl) in enumerate(ohlc)]


def test_trailing_stop_follows_the_price_up_and_locks_in_profit():
    # 100 -> 102 -> 104, then a fall: the stop has moved to 104 * 0.98 = 101.92
    exit_, levels = follow(seq((100, 100, 99, 100), (100, 102, 100, 102), (102, 104, 102, 104),
                               (104, 104, 101, 101.5)), TRAIL, NO_SLIP)
    assert exit_.reason == "stop_loss" and not exit_.gapped
    assert exit_.reference_price == Decimal("101.92")  # a profit, not a loss
    assert levels.highest == 104


def test_trailing_stop_never_moves_down():
    _, levels = follow(seq((100, 103, 100, 103), (103, 103, 101.5, 101.5)), TRAIL, NO_SLIP)
    assert levels.stop_price == Decimal("100.94")  # 103 * 0.98, kept after the lower candle


def test_small_dip_does_not_end_the_trade():
    exit_, levels = follow(seq((100, 101, 98.5, 100), (100, 101.5, 99.6, 101)), TRAIL, NO_SLIP)
    assert exit_ is None and levels.stop_price == Decimal("99.47")  # 101.5 * 0.98


def test_a_candles_own_high_does_not_protect_its_low():
    # one candle goes up to 104 and down to 98: the stop in force was the initial 97, so no exit
    exit_, levels = follow(seq((100, 104, 98, 99)), TRAIL, NO_SLIP)
    assert exit_ is None and levels.stop_price == Decimal("101.92")


def test_take_profit_still_wins_on_a_strong_rise():
    exit_, _ = follow(seq((100, 102, 100, 102), (102, 106, 102, 105)), TRAIL, NO_SLIP)
    assert exit_.reason == "take_profit" and exit_.reference_price == 105


def test_fixed_stop_does_not_trail():
    _, levels = follow(seq((100, 104, 100, 104)), LEVELS, NO_SLIP)
    assert levels.stop_price == 97


def test_bad_trail_is_refused():
    with pytest.raises(ValueError):
        ProtectiveLevels.from_entry(Decimal(100), Decimal(3), Decimal(5), trail_pct=Decimal(0))
