from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scheme1.accounting import AccountingError, Ledger, fee_for, round_trips
from scheme1.contracts import Fill, Order, OrderReason, OrderStatus, OrderType, Side

T = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
SYM = "solana:Pool1111"
D = Decimal


def fill(side, qty, price, fee=None, i=0, symbol=SYM, order_id="ord_1"):
    qty, price = D(str(qty)), D(str(price))
    fee = fee_for(qty, price, D("0.25")) if fee is None else D(str(fee))
    t = T + timedelta(minutes=5 * i)
    return Fill(order_id=order_id, symbol=symbol, side=side, quantity=qty, price=price, reference_price=price,
                fee=fee, candle_open_time=t, filled_at=t, liquidity="taker")


def test_round_trip_profit_is_net_of_both_fees():
    led = Ledger(D(1000))
    led.apply(fill(Side.BUY, 100, 1, i=0))          # cost 100 + fee 0.25
    assert led.cash == D("899.75") and led.quantity(SYM) == 100
    pos, = led.positions()
    assert pos.avg_entry_price == D("1.0025")       # break-even before the sell fee
    led.apply(fill(Side.SELL, 100, "1.1", i=1))     # proceeds 110 - fee 0.275
    assert led.quantity(SYM) == 0 and led.positions() == ()
    assert led.realized_total == D("9.475")         # 110 - 0.275 - 100.25
    assert led.cash == D("1009.475") and led.fees_paid == D("0.525")


def test_partial_sell_keeps_the_right_cost_basis():
    led = Ledger(D(1000))
    led.apply(fill(Side.BUY, 100, 2, fee=0))
    led.apply(fill(Side.SELL, 40, 3, fee=0, i=1))
    pos, = led.positions()
    assert pos.quantity == 60 and pos.avg_entry_price == 2 and pos.realized_pnl == 40
    led.apply(fill(Side.SELL, 60, 1, fee=0, i=2))
    assert led.realized_total == D(40 - 60) and led.cash == D(1000 - 20)


def test_snapshot_values_positions_at_their_marks():
    led = Ledger(D(1000))
    led.apply(fill(Side.BUY, 100, 1, fee=0))
    snap = led.snapshot(T, {SYM: D("1.5"), "other": D(9)})
    assert snap.equity == D(1050) and snap.cash == D(900) and snap.marks == {SYM: D("1.5")}
    with pytest.raises(AccountingError):
        led.snapshot(T, {})  # an open position with no price can't be valued


def test_invariants_stop_bad_fills_and_change_nothing():
    led = Ledger(D(100))
    with pytest.raises(AccountingError, match="needs"):
        led.apply(fill(Side.BUY, 200, 1))           # not enough cash
    with pytest.raises(AccountingError, match="only 0 held"):
        led.apply(fill(Side.SELL, 1, 1))            # selling what isn't held (no short sales)
    assert led.cash == 100 and led.fees_paid == 0 and led.positions() == ()


def test_rebuild_from_fills_matches_exactly():
    fills = [fill(Side.BUY, "123.456789", "0.000003090858", i=0), fill(Side.SELL, "50", "0.0000031", i=1),
             fill(Side.SELL, "73.456789", "0.0000029", i=2), fill(Side.BUY, 10, 2, i=3)]
    live = Ledger(D(1000))
    for f in fills:
        live.apply(f)
    snap = live.snapshot(T, {SYM: D(2)})
    again = Ledger.rebuild(D(1000), fills)
    assert again.matches(snap) and again.realized_total == live.realized_total
    assert not Ledger.rebuild(D(1000), fills[:-1]).matches(snap)


def test_initial_cash_must_be_positive():
    with pytest.raises(ValueError):
        Ledger(D(0))


def _order(order_id, reason):
    return Order(order_id=order_id, intent_id=None, decision_id="dec_1", symbol=SYM, side=Side.SELL,
                 order_type=OrderType.MARKET, reason=reason, quantity=D(1), reference_price=D(1),
                 status=OrderStatus.OPEN, created_at=T, updated_at=T, not_before=T)


def test_round_trips_report_each_trade():
    fills = [fill(Side.BUY, 100, 1, fee=0, i=0, order_id="b1"),
             fill(Side.SELL, 60, "1.05", fee=0, i=1, order_id="s1"),
             fill(Side.SELL, 40, "1.02", fee=0, i=2, order_id="s2"),
             fill(Side.BUY, 10, 2, fee=0, i=3, order_id="b2")]
    orders = {"s1": _order("s1", OrderReason.TAKE_PROFIT), "s2": _order("s2", OrderReason.STOP_LOSS)}
    first, second = round_trips(fills, orders)
    assert first.pnl == D("3.8") and first.pnl_pct == D("3.8") and first.closed_at == T + timedelta(minutes=10)
    assert first.exit_reasons == ["take_profit", "stop_loss"]
    assert second.closed_at is None and second.pnl is None and second.quantity == 10


def test_capital_moves_keep_the_books_balanced_and_replay_exactly():
    led = Ledger(D(500))
    led.apply(fill(Side.BUY, 100, 1))
    led.move_capital(D(-50))                       # rebalancing takes 50 of the free cash away
    led.apply(fill(Side.SELL, 100, "1.2", i=1))
    assert led.initial_cash == D(450) and led.cash == D(450) + led.realized_total
    with pytest.raises(AccountingError):
        led.move_capital(D(-10_000))               # more than the free cash
    again = Ledger.rebuild(D(500), [fill(Side.BUY, 100, 1), D(-50), fill(Side.SELL, 100, "1.2", i=1)])
    assert again.cash == led.cash and again.realized_total == led.realized_total
