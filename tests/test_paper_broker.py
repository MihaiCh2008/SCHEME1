from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scheme1.accounting import Ledger
from scheme1.broker import BrokerConfig, ExitPlan, PaperBroker, PositionGuard
from scheme1.contracts import (
    DeskState, OrderIntent, OrderReason, OrderStatus, OrderType, RiskCheck, RiskVerdict, Side,
)
from data_helpers import SYMBOL, make_candle

D = Decimal
T0 = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)
DECIDED = T0 + STEP  # close of candle 0 = open of candle 1
PLAN = ExitPlan(stop_loss_pct=D(3), take_profit_pct=D(5), trail_pct=D(2), max_hold=timedelta(hours=2))
BIG = "1000000"  # USD volume: the 5% cap never binds


def c(i, o, h=None, lo=None, cl=None, vol=BIG):
    o = D(str(o))
    h = D(str(h)) if h is not None else o
    lo = D(str(lo)) if lo is not None else o
    cl = D(str(cl)) if cl is not None else o
    return make_candle(T0 + i * STEP, open=o, high=max(h, o, cl), low=min(lo, o, cl), close=cl, volume=D(vol))


def approve(side=Side.BUY, qty=1, now=DECIDED, order_type=OrderType.MARKET, limit=None):
    intent = OrderIntent(decision_id="dec_1", symbol=SYMBOL, side=side, order_type=order_type, quantity=D(qty),
                         limit_price=limit, created_at=now)
    verdict = RiskVerdict(intent_id=intent.intent_id, decision_id="dec_1", approved=True,
                          desk_state=DeskState.NORMAL, risk_config_version="t", evaluated_at=now,
                          checks=(RiskCheck(name="all", passed=True, observed="", limit=""),),
                          exit_only=side is Side.SELL)
    return intent, verdict


class Desk:
    """Runs candles through a broker and a ledger, like the paper desk does."""

    def __init__(self, cash="10000", config=BrokerConfig()):
        self.broker = PaperBroker(config, PLAN)
        self.ledger = Ledger(D(cash))
        self.orders, self.fills, self.notes = [], [], []

    def buy(self, qty=1, ref=100, now=DECIDED):
        intent, verdict = approve(Side.BUY, qty, now)
        return self.broker.submit(intent, verdict, D(str(ref)), not_before=DECIDED, now=now)

    def run(self, *candles, state=DeskState.NORMAL):
        for candle in candles:
            res = self.broker.process_candle(candle, self.ledger.cash, self.ledger.quantity(SYMBOL), state)
            for f in res.fills:
                self.ledger.apply(f)
            self.orders += res.orders
            self.fills += res.fills
            self.notes += res.notes
        return self

    def last(self, order_id):
        return [o for o in self.orders if o.order_id == order_id][-1]


# --------------------------------------------------------------------- entries

def test_buy_fills_at_the_next_open_with_slippage_and_fee():
    d = Desk()
    order = d.buy(qty=1, ref=100)
    assert order.status is OrderStatus.OPEN and order.ttl_candles == 1
    d.run(c(0, 99), c(1, 100, 100.5, 99.9, 100.2))   # candle 0 opened before the decision: no fill from it
    fill, = d.fills
    assert fill.candle_open_time == T0 + STEP and fill.reference_price == 100
    assert fill.price == D("100.25") and fill.fee == D("0.25062500")
    assert d.last(order.order_id).status is OrderStatus.FILLED
    g = d.broker.guard(SYMBOL)
    assert g.entry_price == D("100.25") and g.opened_at == T0 + STEP
    assert g.levels.take_profit_price == D("105.2625")
    # the -3% stop (97.2425) covered the entry candle; after it, the stop trails 2% below its high
    assert g.levels.stop_price == D("100.5") * D("0.98")


def test_volume_cap_gives_a_partial_fill_and_the_rest_of_the_entry_expires():
    d = Desk()
    order = d.buy(qty=1, ref=100)
    d.run(c(1, 100, vol="1000"))                     # 5% of $1000 = $50 = 0.5 units at 100
    assert d.fills[0].quantity == D("0.5")
    final = d.last(order.order_id)
    assert final.status is OrderStatus.EXPIRED and final.filled_quantity == D("0.5")
    assert "first candle" in final.detail and d.broker.open_orders() == []


def test_fomo_buy_fills_even_after_a_price_jump():
    d = Desk()
    d.buy(qty=1, ref=100)
    d.run(c(1, 110))                                  # pumped 10% since the decision: still buys (D20)
    assert d.fills[0].reference_price == 110 and d.fills[0].price == D("110.275")


@pytest.mark.parametrize("config,candle,detail", [
    (BrokerConfig(entry_tolerance_pct=D(2)), c(1, 103), "3.00% above the decision price"),  # optional limit
    (BrokerConfig(), c(1, 100, vol="0"), "no trades"),
])
def test_entry_is_dropped_when_it_cannot_fill_in_its_first_candle(config, candle, detail):
    d = Desk(config=config)
    order = d.buy(qty=1, ref=100)
    d.run(candle)
    final = d.last(order.order_id)
    assert d.fills == [] and final.status is OrderStatus.EXPIRED and detail in final.detail
    assert d.broker.guard(SYMBOL) is None


def test_buy_never_spends_more_cash_than_there_is():
    d = Desk(cash="50")
    d.buy(qty=1, ref=100)
    d.run(c(1, 100))
    assert D("0.49") < d.fills[0].quantity < D("0.5")
    assert 0 <= d.ledger.cash < D("0.01")


# ----------------------------------------------------------------------- exits

def test_stop_loss_can_trigger_in_the_entry_candle():
    d = Desk()
    d.buy(qty=1, ref=100)
    d.run(c(1, 100, 100.5, 95, 96))
    buy, sell = d.fills
    assert sell.side is Side.SELL and sell.reference_price == D("97.2425")
    assert sell.price == D("97.2425") * D("0.9975") and sell.filled_at == T0 + 2 * STEP
    exit_order = d.last(sell.order_id)
    assert exit_order.reason is OrderReason.STOP_LOSS and exit_order.decision_id == "dec_1"
    assert d.ledger.quantity(SYMBOL) == 0 and d.broker.guard(SYMBOL) is None
    assert d.ledger.realized_total < 0


def test_trailing_stop_then_take_profit():
    d = Desk()
    d.buy(qty=1, ref=100)
    d.run(c(1, 100, 101, 99.9, 101), c(2, 101, 103, 100.5, 102.5))
    assert d.broker.guard(SYMBOL).levels.stop_price == D(103) * D("0.98")     # trailed up from 97.24
    d.run(c(3, 102.5, 106, 102, 105))                                       # reaches 105.2625
    sell = d.fills[-1]
    assert d.last(sell.order_id).reason is OrderReason.TAKE_PROFIT and sell.reference_price == D("105.2625")
    assert d.ledger.realized_total > 0


def test_time_limit_sells_at_the_open():
    d = Desk()
    d.buy(qty=1, ref=100)
    flat = [c(i, 100, 100.5, 99.5) for i in range(1, 25)]                   # 24 candles = 2 hours after entry
    d.run(*flat)
    assert d.ledger.quantity(SYMBOL) == 1                                   # still held after 1:55
    d.run(c(25, 100.1))
    sell = d.fills[-1]
    assert d.last(sell.order_id).reason is OrderReason.TIME_LIMIT
    assert sell.candle_open_time == T0 + 25 * STEP and sell.price == D("100.1") * D("0.9975")
    assert any("time limit" in n for n in d.notes)


def test_an_exit_too_big_for_the_candle_rests_and_finishes_later():
    d = Desk()
    d.buy(qty=10, ref=100)
    d.run(c(1, 100), c(2, 99, 99, 90, 91, vol="10000"))                    # stop hit; $500 cap = ~5.09 units
    stop_order = [o for o in d.orders if o.reason is OrderReason.STOP_LOSS][-1]
    assert stop_order.status is OrderStatus.PARTIALLY_FILLED and d.ledger.quantity(SYMBOL) > 0
    d.run(c(3, 92, 93, 91, 92, vol="1000000"))                            # the rest sells at the open
    assert d.last(stop_order.order_id).status is OrderStatus.FILLED
    assert d.ledger.quantity(SYMBOL) == 0 and d.fills[-1].reference_price == 92
    assert len([o for o in d.orders if o.reason is not OrderReason.SIGNAL and o.status is OrderStatus.OPEN]) == 1


def test_signal_sell_fills_at_the_open_and_cancels_when_nothing_is_held():
    d = Desk()
    d.buy(qty=1, ref=100)
    d.run(c(1, 100))
    intent, verdict = approve(Side.SELL, qty=1, now=T0 + 2 * STEP)
    sell = d.broker.submit(intent, verdict, D(100), not_before=T0 + 2 * STEP, now=T0 + 2 * STEP)
    assert sell.ttl_candles is None
    d.run(c(2, 101))
    assert d.fills[-1].price == D(101) * D("0.9975") and d.ledger.quantity(SYMBOL) == 0
    intent, verdict = approve(Side.SELL, qty=1, now=T0 + 3 * STEP)
    late = d.broker.submit(intent, verdict, D(100), not_before=T0 + 3 * STEP, now=T0 + 3 * STEP)
    d.run(c(3, 101))
    assert d.last(late.order_id).status is OrderStatus.CANCELED


# ------------------------------------------------------------------ desk states

def test_desk_not_normal_cancels_entries():
    d = Desk()
    order = d.buy(qty=1, ref=100)
    d.run(c(1, 100), state=DeskState.DEGRADED)
    assert d.last(order.order_id).status is OrderStatus.CANCELED and d.fills == []


def test_paused_desk_still_runs_the_stop_loss():
    d = Desk()
    d.buy(qty=1, ref=100)
    d.run(c(1, 100))
    d.run(c(2, 99, 99, 90, 91), state=DeskState.PAUSED)
    assert d.ledger.quantity(SYMBOL) == 0 and d.last(d.fills[-1].order_id).reason is OrderReason.STOP_LOSS


def test_emergency_stop_cancels_everything_and_trades_nothing():
    d = Desk()
    d.buy(qty=1, ref=100)
    d.run(c(1, 100))
    intent, verdict = approve(Side.SELL, qty=1, now=T0 + 2 * STEP)
    d.broker.submit(intent, verdict, D(100), not_before=T0 + 2 * STEP, now=T0 + 2 * STEP)
    d.run(c(2, 99, 99, 50, 60), state=DeskState.EMERGENCY_STOP)
    assert d.broker.open_orders() == [] and d.ledger.quantity(SYMBOL) == 1
    assert d.orders[-1].status is OrderStatus.CANCELED and d.orders[-1].detail == "emergency stop"


# ------------------------------------------------------------------- the rest

def test_submit_needs_its_own_approved_verdict_and_limit_orders_are_refused():
    intent, verdict = approve()
    other, _ = approve()
    broker = PaperBroker(BrokerConfig(), PLAN)
    with pytest.raises(ValueError):
        broker.submit(other, verdict, D(100), DECIDED, DECIDED)
    with pytest.raises(ValueError):
        broker.submit(intent, verdict.model_copy(update={"approved": False}), D(100), DECIDED, DECIDED)
    intent, verdict = approve(order_type=OrderType.LIMIT, limit=D(99))
    assert broker.submit(intent, verdict, D(100), DECIDED, DECIDED).status is OrderStatus.REJECTED


def test_guards_survive_a_restart():
    d = Desk()
    d.buy(qty=1, ref=100)
    d.run(c(1, 100, 104, 100, 103))
    g = d.broker.guard(SYMBOL)
    assert PositionGuard.from_dict(g.to_dict()) == g
    fresh = PaperBroker(BrokerConfig(), PLAN)
    fresh.restore([], [PositionGuard.from_dict(g.to_dict())])
    res = fresh.process_candle(c(2, 103, 103, 101, 101), d.ledger.cash, d.ledger.quantity(SYMBOL), DeskState.NORMAL)
    assert res.fills and res.fills[0].reference_price == D(104) * D("0.98")   # the trailed stop came back


def test_books_balance_after_every_scenario():
    d = Desk()
    d.buy(qty=3, ref=100)
    d.run(c(1, 100, 102, 99, 101), c(2, 101, 104, 100, 103), c(3, 103, 103.5, 99, 100))
    assert d.ledger.quantity(SYMBOL) == 0
    assert d.ledger.cash == d.ledger.initial_cash + d.ledger.realized_total
