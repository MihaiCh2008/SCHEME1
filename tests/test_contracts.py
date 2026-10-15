from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from scheme1.contracts import (
    Candle, Confidence, DataStatus, Decision, DeskState, Evidence, EvidenceKind,
    FeatureSnapshot, Interval, OrderIntent, OrderStatus, OrderType, RiskCheck,
    RiskVerdict, Side, Signal, can_transition,
)

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def candle(**kw):
    base = dict(symbol="BTC/USDT", interval=Interval.H1, open_time=T0, open=Decimal("100"),
                high=Decimal("110"), low=Decimal("95"), close=Decimal("105"), volume=Decimal("3"))
    base.update(kw)
    return Candle(**base)


def snapshot(**kw):
    base = dict(symbol="BTC/USDT", interval=Interval.H1, as_of=T0 + timedelta(hours=1),
                feature_set_version="f1", values={"rsi_14": 55.0}, input_batch_ids=("b1",), warmup_complete=True)
    base.update(kw)
    return FeatureSnapshot(**base)


def decision(**kw):
    base = dict(created_at=T0 + timedelta(hours=1, seconds=5), symbol="BTC/USDT", interval=Interval.H1,
                signal=Signal.BUY, strategy_version="s1", model_version="m1", features=snapshot(),
                data_status=DataStatus.OK, data_age_seconds=5,
                confidence=Confidence(value=0.6, kind="uncalibrated_research_score"), explanation="x")
    base.update(kw)
    return Decision(**base)


def test_candle_valid_and_close_time():
    c = candle()
    assert c.close_time == T0 + timedelta(hours=1)


@pytest.mark.parametrize("kw", [
    {"open_time": datetime(2024, 1, 1)},                       # naive timestamp
    {"open_time": datetime(2024, 1, 1, tzinfo=timezone(timedelta(hours=2)))},  # not UTC
    {"high": Decimal("101")},                                   # high below close
    {"low": Decimal("106")},                                    # low above close/open
    {"open": Decimal("0")},                                     # non-positive price
    {"volume": Decimal("-1")},                                  # negative volume
])
def test_candle_rejects_invalid(kw):
    with pytest.raises(ValidationError):
        candle(**kw)


def test_contracts_are_frozen_and_strict():
    c = candle()
    with pytest.raises(ValidationError):
        c.close = Decimal("1")
    with pytest.raises(ValidationError):
        candle(extra_field=1)


def test_fact_must_cite_input():
    with pytest.raises(ValidationError):
        Evidence(kind=EvidenceKind.FACT, statement="RSI is 55")
    Evidence(kind=EvidenceKind.FACT, statement="RSI is 55", refs=("rsi_14",))
    Evidence(kind=EvidenceKind.HYPOTHESIS, statement="momentum may persist")


def test_decision_rules():
    assert decision().signal is Signal.BUY
    with pytest.raises(ValidationError, match="only HOLD"):
        decision(data_status=DataStatus.STALE)
    decision(data_status=DataStatus.STALE, signal=Signal.HOLD)
    with pytest.raises(ValidationError, match="older than its own features"):
        decision(created_at=T0)
    with pytest.raises(ValidationError, match="different symbol"):
        decision(symbol="ETH/USDT")


def test_confidence_calibration_needs_reference():
    with pytest.raises(ValidationError):
        Confidence(value=0.7, kind="calibrated_probability")
    with pytest.raises(ValidationError):
        Confidence(value=1.2, kind="uncalibrated_research_score")


def test_order_intent_limit_price_rule():
    kw = dict(decision_id="d", symbol="BTC/USDT", side=Side.BUY, quantity=Decimal("1"), created_at=T0)
    OrderIntent(order_type=OrderType.MARKET, **kw)
    OrderIntent(order_type=OrderType.LIMIT, limit_price=Decimal("100"), **kw)
    with pytest.raises(ValidationError):
        OrderIntent(order_type=OrderType.LIMIT, **kw)
    with pytest.raises(ValidationError):
        OrderIntent(order_type=OrderType.MARKET, limit_price=Decimal("100"), **kw)
    with pytest.raises(ValidationError):
        OrderIntent(order_type=OrderType.MARKET, **{**kw, "quantity": Decimal("0")})


def test_risk_verdict_cannot_approve_with_failed_check_or_bad_state():
    ok = RiskCheck(name="max_position", passed=True, observed="1", limit="2")
    bad = RiskCheck(name="data_age", passed=False, observed="9000s", limit="7200s")
    kw = dict(intent_id="i", decision_id="d", risk_config_version="r1", evaluated_at=T0)
    RiskVerdict(approved=True, desk_state=DeskState.NORMAL, checks=(ok,), **kw)
    with pytest.raises(ValidationError):
        RiskVerdict(approved=True, desk_state=DeskState.NORMAL, checks=(ok, bad), **kw)
    with pytest.raises(ValidationError):
        RiskVerdict(approved=True, desk_state=DeskState.PAUSED, checks=(ok,), **kw)
    RiskVerdict(approved=False, desk_state=DeskState.EMERGENCY_STOP, checks=(ok, bad), **kw)
    RiskVerdict(approved=True, desk_state=DeskState.DEGRADED, checks=(ok,), exit_only=True, **kw)
    with pytest.raises(ValueError):
        RiskVerdict(approved=True, desk_state=DeskState.EMERGENCY_STOP, checks=(ok,), exit_only=True, **kw)


def test_order_transitions():
    assert can_transition(OrderStatus.NEW, OrderStatus.OPEN)
    assert can_transition(OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)
    assert not can_transition(OrderStatus.FILLED, OrderStatus.CANCELED)
    assert not can_transition(OrderStatus.NEW, OrderStatus.FILLED)
    for terminal in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
        assert not any(can_transition(terminal, s) for s in OrderStatus)
