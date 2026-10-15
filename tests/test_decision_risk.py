from datetime import timedelta
from decimal import Decimal

import pytest

from scheme1.agents import run_agents
from scheme1.contracts import (
    AgentOutput, AgentStatus, DataStatus, DeskState, PortfolioSnapshot, Position, Signal, Stance,
)
from scheme1.decision import OpenPosition, SizingConfig, StrategyConfig, authorize, combine, decide
from scheme1.risk import RiskConfig, RiskContext, evaluate_risk
from test_agents import DOWN, NOW, UP, inputs, snap

PRICE = Decimal("0.00002")
FLAT = dict(UP, ret_12=0.0, ema_ratio_9_21=0.0, close_vs_ema_21=0.0, rsi_14=50.0, range_pos_48=0.5,
            volume_ratio_12_48=1.0)


def run(x, position=None, price=PRICE, cfg=StrategyConfig()):
    return decide(x, run_agents(x), NOW, price, position, cfg)


def position(change_pct=0, held=timedelta(minutes=30)):
    entry = PRICE / (1 + Decimal(change_pct) / 100)
    return OpenPosition(entry_price=entry, opened_at=NOW - held, quantity=Decimal(1000))


# ------------------------------------------------------------------- entries

def test_strong_uptrend_on_a_safe_token_buys():
    d = run(inputs(snap(UP)))
    assert d.signal is Signal.BUY and d.rejection_reasons == ()
    assert d.confidence.kind == "uncalibrated_research_score" and 0 < d.confidence.value <= 1
    assert d.bull_evidence and len(d.agent_outputs) == 6


@pytest.mark.parametrize("x,reason", [
    (inputs(snap(UP), tradable=False), "safety"),
    (inputs(snap(UP), status=DataStatus.STALE), "data is stale"),
    (inputs(snap(FLAT)), "buy threshold"),
    (inputs(snap(DOWN)), "buy threshold"),
    (inputs(snap(UP, atr_pct_14=0.09)), "risk agent"),
    (inputs(snap({"bars_available": 5.0}, warmup=False)), "no opinion"),
])
def test_no_buy_without_every_condition(x, reason):
    d = run(x)
    assert d.signal is Signal.HOLD
    assert any(reason in r for r in d.rejection_reasons), d.rejection_reasons


def test_disagreement_lowers_confidence():
    x = inputs(snap(UP))
    outs = run_agents(x)
    _, agreed, _ = combine(outs, StrategyConfig())
    flipped = tuple(o.model_copy(update={"score": -0.5, "stance": Stance.BEARISH}) if o.role == "technical" else o
                    for o in outs)
    _, contested, notes = combine(flipped, StrategyConfig())
    assert contested < agreed and "market and technical disagree" in notes


def test_invalid_agents_are_left_out():
    x = inputs(snap(UP))
    broken = tuple(AgentOutput(agent_id=o.agent_id, agent_version=o.agent_version, role=o.role,
                               status=AgentStatus.INVALID, error="boom", latency_ms=0)
                   if o.role == "risk" else o for o in run_agents(x))
    d = decide(x, broken, NOW, PRICE)
    assert d.signal is Signal.HOLD and "risk agent gave no output" in d.rejection_reasons


# --------------------------------------------------------------------- exits

@pytest.mark.parametrize("pos,x,why", [
    (position(change_pct=6), inputs(snap(FLAT)), "take profit"),
    (position(change_pct=-4), inputs(snap(FLAT)), "stop loss"),
    (position(held=timedelta(hours=2)), inputs(snap(FLAT)), "time limit"),
    (position(), inputs(snap(FLAT), tradable=False), "no longer tradable"),
    (position(), inputs(snap(DOWN)), "combined score"),
])
def test_exit_rules_sell(pos, x, why):
    d = run(x, pos)
    assert d.signal is Signal.SELL and why in d.explanation


def test_position_is_held_when_no_exit_rule_hits():
    d = run(inputs(snap(UP)), position(change_pct=2))
    assert d.signal is Signal.HOLD and "holding" in d.rejection_reasons[0]


def test_bad_data_blocks_exits_and_says_so():
    d = run(inputs(snap(FLAT), status=DataStatus.DEGRADED), position(change_pct=-10))
    assert d.signal is Signal.HOLD
    assert any("no exit" in r for r in d.rejection_reasons)


def test_strategy_config_rejects_losing_settings():
    with pytest.raises(ValueError):
        StrategyConfig(take_profit_pct=Decimal("0.8"))  # below the 1% round-trip cost
    with pytest.raises(ValueError):
        StrategyConfig(exit_score=0.5)


# -------------------------------------------------------------------- sizing

def portfolio(cash=Decimal(1000), qty=Decimal(0), equity=None):
    positions = (Position(symbol=snap().symbol, quantity=qty, avg_entry_price=PRICE),) if qty else ()
    eq = equity if equity is not None else cash + qty * PRICE
    return PortfolioSnapshot(as_of=NOW, cash=cash, positions=positions, marks={snap().symbol: PRICE}, equity=eq,
                             fees_paid=Decimal(0))


def test_buy_is_sized_from_equity_and_sell_closes_everything():
    buy = authorize(run(inputs(snap(UP))), portfolio(), PRICE, NOW)
    assert buy.side.value == "BUY" and buy.quantity * PRICE == pytest.approx(Decimal(98), rel=Decimal("1e-6"))
    sell = authorize(run(inputs(snap(DOWN)), position()), portfolio(qty=Decimal(5000)), PRICE, NOW)
    assert sell.side.value == "SELL" and sell.quantity == 5000
    assert authorize(run(inputs(snap(FLAT))), portfolio(), PRICE, NOW) is None
    assert authorize(run(inputs(snap(UP))), portfolio(cash=Decimal(0), equity=Decimal(0)), PRICE, NOW) is None


# ---------------------------------------------------------------------- risk

def ctx(pf=None, **kw):
    base = dict(portfolio=pf or portfolio(), desk_state=DeskState.NORMAL, peak_equity=Decimal(1000),
                data_status=DataStatus.OK, data_age=timedelta(minutes=1), token_tradable=True,
                reference_price=PRICE, now=NOW)
    base.update(kw)
    return RiskContext(**base)


def buy_intent(pf=None):
    return authorize(run(inputs(snap(UP))), pf or portfolio(), PRICE, NOW)


def sell_intent(qty=Decimal(5000)):
    return authorize(run(inputs(snap(DOWN)), position()), portfolio(qty=qty), PRICE, NOW)


def failed(verdict):
    return {c.name for c in verdict.checks if not c.passed}


def test_normal_buy_is_approved_with_every_check_listed():
    v = evaluate_risk(buy_intent(), ctx())
    assert v.approved, failed(v)
    assert {c.name for c in v.checks} == {"desk_state", "data_quality", "data_age", "short_sale",
                                          "duplicate_or_conflict", "drawdown", "cash", "position_size",
                                          "gross_exposure", "concentration", "token_safety", "frequency"}


@pytest.mark.parametrize("kw,check", [
    (dict(desk_state=DeskState.PAUSED), "desk_state"),
    (dict(data_status=DataStatus.DEGRADED), "data_quality"),
    (dict(data_age=timedelta(minutes=30)), "data_age"),
    (dict(token_tradable=False), "token_safety"),
    (dict(peak_equity=Decimal(2000)), "drawdown"),
    (dict(orders_today=20), "frequency"),
    (dict(last_order_at={snap().symbol: NOW - timedelta(minutes=5)}), "frequency"),
    (dict(open_order_symbols=frozenset({snap().symbol})), "duplicate_or_conflict"),
])
def test_each_buy_limit_rejects(kw, check):
    v = evaluate_risk(buy_intent(), ctx(**kw))
    assert not v.approved and check in failed(v)


def test_size_and_cash_limits():
    small = RiskConfig(max_position_pct=Decimal(5))
    assert "position_size" in failed(evaluate_risk(buy_intent(), ctx(), small))
    poor = portfolio(cash=Decimal(50), equity=Decimal(1000))
    assert "cash" in failed(evaluate_risk(buy_intent(portfolio()), ctx(poor)))


def test_exits_pass_when_paused_or_in_drawdown_but_never_in_emergency_stop():
    pf = portfolio(qty=Decimal(5000))
    for state in (DeskState.PAUSED, DeskState.DEGRADED):
        v = evaluate_risk(sell_intent(), ctx(pf, desk_state=state, peak_equity=Decimal(5000)))
        assert v.approved and v.exit_only, failed(v)
    v = evaluate_risk(sell_intent(), ctx(pf, desk_state=DeskState.EMERGENCY_STOP))
    assert not v.approved and "desk_state" in failed(v)


def test_cannot_sell_more_than_held():
    v = evaluate_risk(sell_intent(Decimal(5000)), ctx(portfolio(qty=Decimal(100))))
    assert not v.approved and "short_sale" in failed(v)
