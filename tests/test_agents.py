from datetime import datetime, timedelta, timezone

import pytest

from scheme1.agents import DEFAULT_AGENTS, Agent, AgentInputs, citation_errors, run_agent, run_agents
from scheme1.agents.signals import fact, note
from scheme1.contracts import (
    AgentStatus, DataStatus, EvidenceKind, FeatureSnapshot, Interval, SafetyCheck, SafetyReport, SafetyStatus,
    Stance, ValidationReport,
)
from scheme1.features import FEATURE_KEYS, compute_features
from data_helpers import SYMBOL
from test_features import at_close, random_walk

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

UP = dict(bars_available=64.0, ret_1=0.01, ret_3=0.03, ret_12=0.12, vol_12=0.02, vol_48=0.02,
          ema_ratio_9_21=0.03, close_vs_ema_21=0.05, rsi_14=66.0, atr_pct_14=0.02, volume_usd_12=50_000.0,
          volume_ratio_12_48=1.8, range_pos_48=0.9, drawdown_48=-0.02, zero_volume_frac_48=0.0)
DOWN = dict(UP, ret_1=-0.01, ret_3=-0.03, ret_12=-0.12, ema_ratio_9_21=-0.03, close_vs_ema_21=-0.05,
            rsi_14=34.0, range_pos_48=0.1, drawdown_48=-0.2)


def snap(values=None, warmup=True, **kw):
    v = {k: None for k in FEATURE_KEYS}
    v.update(values if values is not None else UP)
    v.update(kw)
    return FeatureSnapshot(symbol=SYMBOL, interval=Interval.M5, as_of=NOW, feature_set_version="f1.0.0",
                           values=v, input_batch_ids=("batch_1",), warmup_complete=warmup)


def validation(status=DataStatus.OK):
    return ValidationReport(batch_id="batch_1", status=status, candles_in=64, candles_out=64, checked_at=NOW)


def safety(tradable=True):
    status = SafetyStatus.PASS if tradable else SafetyStatus.FAIL
    checks = (SafetyCheck(name="mint_authority_revoked", status=SafetyStatus.PASS, observed="revoked", limit="revoked"),
              SafetyCheck(name="holder_concentration", status=status, observed="top 10 wallets 38.41%",
                          limit="top 10 <= 30%"))
    return SafetyReport(symbol=SYMBOL, checked_at=NOW, checks=checks, safety_config_version="s1.1.0",
                        tradable=tradable)


def inputs(features=None, status=DataStatus.OK, tradable=True):
    return AgentInputs(features or snap(), validation(status), safety(tradable))


def by_role(outputs):
    return {o.role: o for o in outputs}


def test_all_agents_run_and_cite_only_real_inputs():
    x = inputs()
    outs = run_agents(x)
    assert [o.role for o in outs] == ["market", "technical", "data_news", "bull", "bear", "risk"]
    assert all(o.status is AgentStatus.OK for o in outs)
    assert all(o.evidence for o in outs)
    citable = x.citable()
    for o in outs:
        assert citation_errors(o.evidence, citable) == []
        assert all(e.refs for e in o.evidence)  # even interpretations say what they rest on


def test_uptrend_is_bullish_and_downtrend_bearish():
    up = by_role(run_agents(inputs(snap(UP))))
    assert up["market"].stance is up["technical"].stance is Stance.BULLISH
    assert up["market"].score > 0.5 and up["technical"].score > 0.3
    assert up["bull"].score > 0 and up["bear"].stance is Stance.NEUTRAL and up["bear"].score == 0

    down = by_role(run_agents(inputs(snap(DOWN))))
    assert down["market"].stance is down["technical"].stance is Stance.BEARISH
    assert down["bear"].score < 0 and down["bull"].score == 0


def test_same_inputs_give_the_same_outputs():
    x = inputs()
    strip = lambda outs: [o.model_copy(update={"latency_ms": 0}) for o in outs]
    assert strip(run_agents(x)) == strip(run_agents(x))


def test_warm_up_makes_directional_agents_abstain():
    x = inputs(snap({"bars_available": 5.0, "ret_1": 0.01, "ret_3": 0.02}, warmup=False))
    r = by_role(run_agents(x))
    assert r["market"].stance is r["technical"].stance is Stance.ABSTAIN
    assert r["market"].status is AgentStatus.OK
    assert any("warm-up" in e.statement for e in r["data_news"].evidence)


def test_data_agent_is_never_directional_and_flags_bad_data():
    r = by_role(run_agents(inputs(status=DataStatus.DEGRADED)))["data_news"]
    assert r.stance is Stance.NEUTRAL and r.score == 0
    assert any("only HOLD" in e.statement for e in r.evidence)
    illiquid = by_role(run_agents(inputs(snap(UP, zero_volume_frac_48=0.5))))["data_news"]
    assert any("no trades" in e.statement for e in illiquid.evidence)


def test_risk_agent_blocks_unsafe_tokens_and_flags_volatility():
    r = by_role(run_agents(inputs(tradable=False)))["risk"]
    assert r.stance is Stance.BEARISH and r.score == -1.0
    assert any(e.refs == ("safety.holder_concentration",) and e.kind is EvidenceKind.FACT for e in r.evidence)
    assert all(e.refs != ("safety.mint_authority_revoked",) for e in r.evidence)  # passed checks not listed

    wild = by_role(run_agents(inputs(snap(UP, atr_pct_14=0.09))))["risk"]
    assert wild.score == -0.6 and wild.stance is Stance.BEARISH
    calm = by_role(run_agents(inputs()))["risk"]
    assert calm.score == 0 and calm.stance is Stance.NEUTRAL


def test_fading_volume_halves_the_market_score():
    full = by_role(run_agents(inputs(snap(UP, volume_ratio_12_48=1.0))))["market"].score
    faded = by_role(run_agents(inputs(snap(UP, volume_ratio_12_48=0.4))))["market"].score
    assert faded == pytest.approx(full / 2)


def test_overbought_goes_to_the_bear_and_oversold_to_the_bull():
    hot = by_role(run_agents(inputs(snap(UP, rsi_14=85.0))))
    assert any("RSI above 70" in e.statement and e.kind is EvidenceKind.HYPOTHESIS for e in hot["bear"].evidence)
    cold = by_role(run_agents(inputs(snap(DOWN, rsi_14=15.0))))
    assert any("RSI below 30" in e.statement for e in cold["bull"].evidence)


@pytest.mark.parametrize("run,error", [
    (lambda x, cfg: (Stance.BULLISH, 0.5, (note("made up", "sentiment_score"),)), "unknown reference"),
    (lambda x, cfg: (Stance.BULLISH, 0.5, (fact("vol_48", 0.1),)), "has no value"),
    (lambda x, cfg: 1 / 0, "ZeroDivisionError"),
    (lambda x, cfg: (Stance.BULLISH, 3.0, ()), "score"),  # outside [-1, 1]
])
def test_broken_agents_are_marked_invalid(run, error):
    x = inputs(snap(UP, vol_48=None))
    out = run_agent(Agent("broken.v0", "market", run), x)
    assert out.status is AgentStatus.INVALID and out.stance is Stance.ABSTAIN and out.score == 0
    assert error in out.error and out.evidence == ()


def test_inputs_must_be_for_the_same_pool():
    other = safety().model_copy(update={"symbol": "solana:Other"})
    with pytest.raises(ValueError):
        AgentInputs(snap(), validation(), other)


def test_agents_run_on_real_feature_output():
    candles = random_walk(80)
    features = compute_features(candles, SYMBOL, Interval.M5, at_close(79))
    for seed_out in run_agents(AgentInputs(features, validation(), safety())):
        assert seed_out.status is AgentStatus.OK, seed_out.error
    assert len(DEFAULT_AGENTS) == 6
