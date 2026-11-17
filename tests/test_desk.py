from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from scheme1.broker import BrokerConfig
from scheme1.contracts import (
    DataStatus, DeskState, Fill, Interval, OrderReason, SafetyCheck, SafetyReport, SafetyStatus, Side, Signal,
    ValidationReport,
)
from scheme1.desk.desk import CandleInputs, DeskConfig, PaperDesk
from scheme1.desk.store import DeskStore
from scheme1.features import compute_features
from scheme1.risk import RiskConfig
from data_helpers import SYMBOL, make_candle

T0 = datetime(2026, 11, 17, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)
SAFE = SafetyReport(symbol=SYMBOL, checked_at=T0, safety_config_version="s1.1.0", tradable=True,
                    checks=(SafetyCheck(name="all", status=SafetyStatus.PASS, observed="", limit=""),))


def uptrend(n=64, p=D(1)):
    """+1.2% / -0.2% candles: a steady rise with some noise. The 64th candle completes the warm-up."""
    out = []
    for i in range(n):
        o, p = p, p * (D("1.012") if i % 2 == 0 else D("0.998"))
        out.append(make_candle(T0 + i * STEP, open=o, high=max(o, p) * D("1.002"), low=min(o, p) * D("0.998"),
                               close=p, volume=D(1000000)))
    return out


def then(candles, *moves):
    """Append candles given as (high %, low %, close %) relative to the previous close; opens at that close."""
    out = list(candles)
    for hi, lo, cl in moves:
        p = out[-1].close
        o, c = p, p * (1 + D(str(cl)) / 100)
        out.append(make_candle(out[-1].open_time + STEP, open=o, high=max(o, c, p * (1 + D(str(hi)) / 100)),
                               low=min(o, c, p * (1 + D(str(lo)) / 100)), close=c, volume=D(1000000)))
    return out


def report(status=DataStatus.OK):
    return ValidationReport(batch_id="batch_1", status=status, candles_in=1, candles_out=1, checked_at=T0)


def step(desk, candles, i, status=DataStatus.OK, safety=SAFE):
    c = candles[i]
    feats = compute_features(candles[: i + 1], SYMBOL, Interval.M5, c.close_time)
    return desk.on_candle(CandleInputs(c, report(status), safety, feats), now=c.close_time)


def run(desk, candles, start=0, stop=None):
    return [step(desk, candles, i) for i in range(start, len(candles) if stop is None else stop)]


FLAT = (0.1, -0.1, 0)
TRADE = then(uptrend(), FLAT, FLAT, (0, -5, -4))  # buy decided at candle 63, filled at 64, stopped out at 66


def test_a_full_trade_from_signal_to_stop():
    desk = PaperDesk(DeskStore(), now=T0)
    results = run(desk, TRADE)
    buy_step = results[63]
    assert buy_step.decision.signal is Signal.BUY and buy_step.verdict.approved
    assert all(r.decision is None or r.decision.signal is Signal.HOLD for r in results[:63])  # warm-up
    entry, = results[64].fills
    assert entry.side is Side.BUY and entry.candle_open_time == buy_step.decision.features.as_of  # next candle
    assert entry.price == TRADE[64].open * D("1.0025")
    exit_, = results[66].fills
    assert exit_.side is Side.SELL
    trade, = desk.status()["trades"]
    assert trade.exit_reasons == ["stop_loss"] and trade.closed_at is not None
    assert desk.ledger.quantity(SYMBOL) == 0 and desk.ledger.cash == D(1000) + trade.pnl
    assert desk.check() == [] and desk.state is DeskState.NORMAL


def test_a_restart_in_the_middle_of_a_trade_changes_nothing(tmp_path):
    straight = PaperDesk(DeskStore(), now=T0)
    run(straight, TRADE)

    path = tmp_path / "desk.db"
    first = PaperDesk(DeskStore(path), now=T0)
    run(first, TRADE, stop=65)                     # position open, trailing stop active
    guard = first.broker.guard(SYMBOL)
    first.store.close()
    second = PaperDesk(DeskStore(path), now=TRADE[64].close_time)
    assert second.broker.guard(SYMBOL) == guard and second.ledger.quantity(SYMBOL) > 0
    run(second, TRADE, start=65)
    assert second.ledger.cash == straight.ledger.cash and second.check() == []
    assert second.store.records("desk_event") == []   # nothing went wrong


def test_the_same_candle_twice_is_skipped():
    desk = PaperDesk(DeskStore(), now=T0)
    run(desk, TRADE, stop=65)
    count = len(desk.store.records())
    again = step(desk, TRADE, 64)
    assert again.skipped and len(desk.store.records()) == count


def test_bad_data_degrades_the_desk_and_blocks_entries_until_it_recovers():
    desk = PaperDesk(DeskStore(), now=T0)
    run(desk, TRADE, stop=63)
    bad = step(desk, TRADE, 63, status=DataStatus.DEGRADED)
    assert desk.state is DeskState.DEGRADED and bad.decision.signal is Signal.HOLD and bad.verdict is None
    good = step(desk, TRADE, 64)
    assert desk.state is DeskState.NORMAL and good.decision.signal is Signal.BUY
    events = [e["detail"] for e in desk.store.records("desk_event")]
    assert events == ["normal -> degraded: data for " + SYMBOL + " is degraded",
                      "degraded -> normal: data for " + SYMBOL + " is ok"]


def test_kill_switch_cancels_orders_and_stops_trading_until_reset():
    desk = PaperDesk(DeskStore(), now=T0)
    run(desk, TRADE, stop=64)                      # buy order waiting for candle 64
    assert desk.broker.open_orders()
    desk.kill("testing the kill switch", TRADE[63].close_time)
    assert desk.state is DeskState.EMERGENCY_STOP and desk.broker.open_orders() == []
    results = run(desk, TRADE, start=64)
    assert all(r.fills == [] and r.decision is None for r in results)
    assert desk.reset(TRADE[-1].close_time) == [] and desk.state is DeskState.NORMAL


def test_tampering_puts_the_desk_in_emergency_stop_on_restart(tmp_path):
    path = tmp_path / "desk.db"
    desk = PaperDesk(DeskStore(path), now=T0)
    run(desk, TRADE)
    desk.store._db.executescript("DROP TRIGGER audit_log_no_update;")
    desk.store._db.execute("UPDATE audit_log SET payload = replace(payload, '\"BUY\"', '\"SELL\"') "
                           "WHERE seq = (SELECT min(seq) FROM audit_log WHERE kind = 'fill')")
    desk.store.close()
    again = PaperDesk(DeskStore(path), now=TRADE[-1].close_time)
    assert again.state is DeskState.EMERGENCY_STOP and "audit log" in again.flags["emergency"]
    assert again.reset(TRADE[-1].close_time) and again.state is DeskState.EMERGENCY_STOP


def test_books_that_dont_match_the_last_snapshot_stop_the_desk(tmp_path):
    path = tmp_path / "desk.db"
    desk = PaperDesk(DeskStore(path), now=T0)
    run(desk, TRADE, stop=65)
    stray = Fill(order_id="ord_stray", symbol=SYMBOL, side=Side.BUY, quantity=D(1), price=D(1), reference_price=D(1),
                 fee=D(0), candle_open_time=T0, filled_at=T0, liquidity="taker")
    with desk.store.transaction():                 # a fill the books never saw, with a valid hash
        desk.store.append("fill", stray.fill_id, T0, stray)
    desk.store.close()
    again = PaperDesk(DeskStore(path), now=TRADE[64].close_time)
    assert again.state is DeskState.EMERGENCY_STOP and "snapshot" in again.flags["emergency"]


def test_drawdown_pauses_the_desk_until_the_operator_resumes():
    config = DeskConfig(risk=RiskConfig(max_drawdown_pct=D("0.01")))   # tiny limit: any dip pauses
    desk = PaperDesk(DeskStore(), config, now=T0)
    run(desk, TRADE, stop=66)
    assert desk.state is DeskState.PAUSED and "drawdown" in desk.flags["paused"]
    desk.resume(TRADE[65].close_time)
    assert desk.state is DeskState.NORMAL and desk.equity["peak"] == desk.equity["last"]


def test_lost_working_state_rebuilds_the_protective_exits(tmp_path):
    path = tmp_path / "desk.db"
    desk = PaperDesk(DeskStore(path), now=T0)
    run(desk, TRADE, stop=65)
    entry_price = desk.broker.guard(SYMBOL).entry_price
    desk.store._db.execute("DELETE FROM desk_state WHERE key = 'guards'")
    desk.store.close()
    again = PaperDesk(DeskStore(path), now=TRADE[64].close_time)
    guard = again.broker.guard(SYMBOL)
    assert guard is not None and guard.entry_price == entry_price and again.state is DeskState.NORMAL
    assert "rebuilt" in again.store.records("desk_event")[-1]["detail"]


def test_only_our_own_outputs_are_kept_for_good():
    desk = PaperDesk(DeskStore(), now=T0)
    results = run(desk, TRADE)
    kept = desk.store.records("decision")[-1]
    assert "values" not in kept and "evidence" not in str(kept) and kept["agents"][0]["stance"]
    assert all("marks" not in s for s in desk.store.records("snapshot"))
    decision = results[-1].decision
    assert desk.store.decision_details(decision.decision_id) == decision      # full details for 24 hours
    later = TRADE[-1].close_time + timedelta(hours=25)
    with desk.store.transaction():
        desk.store.purge_details(later)
    assert desk.store.decision_details(decision.decision_id) is None


def test_exits_are_never_blocked_by_the_desk_state():
    desk = PaperDesk(DeskStore(), now=T0)
    run(desk, TRADE, stop=65)
    desk.pause("operator break", TRADE[64].close_time)
    run(desk, TRADE, start=65)
    assert desk.ledger.quantity(SYMBOL) == 0
    sells = [o for o in desk.store.orders().values() if o.side is Side.SELL]
    assert [o.reason for o in sells] == [OrderReason.STOP_LOSS]


def test_config_refuses_costs_the_strategy_does_not_expect():
    with pytest.raises(ValueError):
        DeskConfig(broker=BrokerConfig(fee_pct=D(1)))
