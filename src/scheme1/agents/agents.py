"""Phase-1 research agents: deterministic, no network, no LLM.

Each agent reads ``AgentInputs`` and returns a stance, a score in [-1, 1] and evidence.
``run_agents`` wraps every agent the same way: it times it, turns a crash into ``INVALID``,
and rejects (``INVALID``) any output whose evidence cites something that is not in the inputs.

Roles
- market:    12-candle momentum and position in the 48-candle range, damped when volume fades.
- technical: EMA trend, close vs EMA21 and RSI.
- data_news: data quality only (there is no news source). Never directional: always NEUTRAL, score 0.
- bull/bear: advocates. Each collects only the signals on its side; the other side's signals are ignored.
- risk:      score <= 0 as a penalty (0 = no concern). BEARISH when a blocking risk is present.
Scores are research scores, not probabilities (ARCHITECTURE.md section 5).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from scheme1.agents.inputs import AgentInputs
from scheme1.agents.signals import (
    AgentConfig, Signal, fact, momentum, note, overbought, oversold, price_vs_ema, range_position, rsi, trend,
    volume_confirmation, weighted,
)
from scheme1.contracts import AgentOutput, AgentStatus, DataStatus, Evidence, EvidenceKind, SafetyStatus, Stance

AGENT_VERSION = "a1.0.0"

Result = tuple[Stance, float, tuple[Evidence, ...]]


@dataclass(frozen=True)
class Agent:
    agent_id: str
    role: str
    run: Callable[[AgentInputs, AgentConfig], Result]
    version: str = AGENT_VERSION


def _stance(score: float, dead_zone: float = 0.1) -> Stance:
    if score > dead_zone:
        return Stance.BULLISH
    if score < -dead_zone:
        return Stance.BEARISH
    return Stance.NEUTRAL


def _abstain(why: str, *refs: str) -> Result:
    return Stance.ABSTAIN, 0.0, (note(why, *refs),)


def _evidence(signals: list[Signal]) -> tuple[Evidence, ...]:
    seen: set[str] = set()
    out = []
    for s in signals:
        for e in s.evidence:
            if e.statement not in seen:  # several signals cite the same fact
                seen.add(e.statement)
                out.append(e)
    return tuple(out)


# ----------------------------------------------------------------------- agents

def market(x: AgentInputs, cfg: AgentConfig) -> Result:
    score, used = weighted([(momentum(x, cfg), 0.6), (range_position(x, cfg), 0.4)])
    if not used:
        return _abstain("not enough candles for momentum or range yet", "bars_available")
    evidence = list(_evidence(used))
    ratio = x.f("volume_ratio_12_48")
    if ratio is not None and ratio <= cfg.volume_fading:
        score *= 0.5
        evidence += [fact("volume_ratio_12_48", ratio),
                     note("trading activity is fading, so the move counts half", "volume_ratio_12_48")]
    return _stance(score), score, tuple(evidence)


def technical(x: AgentInputs, cfg: AgentConfig) -> Result:
    score, used = weighted([(trend(x, cfg), 0.4), (price_vs_ema(x, cfg), 0.3), (rsi(x, cfg), 0.3)])
    if not used:
        return _abstain("not enough candles for EMA or RSI yet", "bars_available")
    return _stance(score), score, _evidence(used)


def data_quality(x: AgentInputs, cfg: AgentConfig) -> Result:
    status = x.validation.status
    evidence = [fact("validation.status", status.value), fact("validation.issues", len(x.validation.issues)),
                fact("feature.warmup_complete", x.features.warmup_complete),
                fact("bars_available", x.f("bars_available"))]
    if status is not DataStatus.OK:
        evidence.append(note(f"data is {status.value}: only HOLD is allowed", "validation.status"))
    if not x.features.warmup_complete:
        evidence.append(note("feature warm-up is not complete; some features are missing",
                             "feature.warmup_complete"))
    zero = x.f("zero_volume_frac_48")
    if zero is not None:
        evidence.append(fact("zero_volume_frac_48", zero))
        if zero > cfg.max_zero_volume_frac:
            evidence.append(note("many candles had no trades; prices may be unreliable", "zero_volume_frac_48"))
    return Stance.NEUTRAL, 0.0, tuple(evidence)


def _advocate(x: AgentInputs, cfg: AgentConfig, sign: int) -> Result:
    candidates = [momentum, range_position, trend, price_vs_ema, rsi, volume_confirmation,
                  oversold if sign > 0 else overbought]
    signals = [s for s in (fn(x, cfg) for fn in candidates) if s is not None and s.strength * sign > 0]
    if not signals:
        side = "bullish" if sign > 0 else "bearish"
        return Stance.NEUTRAL, 0.0, (note(f"no {side} signal in the current features", "bars_available"),)
    signals.sort(key=lambda s: abs(s.strength), reverse=True)
    score = sum(s.strength for s in signals) / len(signals)
    return (Stance.BULLISH if sign > 0 else Stance.BEARISH), score, _evidence(signals)


def bull(x: AgentInputs, cfg: AgentConfig) -> Result:
    return _advocate(x, cfg, +1)


def bear(x: AgentInputs, cfg: AgentConfig) -> Result:
    return _advocate(x, cfg, -1)


def risk(x: AgentInputs, cfg: AgentConfig) -> Result:
    penalties: list[float] = []
    evidence: list[Evidence] = []
    if not x.safety.tradable:
        bad = [c for c in x.safety.checks if c.status is not SafetyStatus.PASS]
        penalties.append(-1.0)
        evidence.append(fact("safety.tradable", False))
        evidence += [Evidence(kind=EvidenceKind.FACT, statement=f"{c.name}: {c.status.value} ({c.observed})"[:600],
                              refs=(f"safety.{c.name}",)) for c in bad]
        evidence.append(note("token failed safety checks: it must not be bought", "safety.tradable"))
    checks = [
        ("atr_pct_14", cfg.max_atr_pct, "candles swing a lot: stops and fills are expensive", lambda v, lim: v > lim),
        ("zero_volume_frac_48", cfg.max_zero_volume_frac, "illiquid: many candles had no trades",
         lambda v, lim: v > lim),
        ("drawdown_48", cfg.deep_drawdown, "price is far below its recent high", lambda v, lim: v < lim),
    ]
    for key, limit, why, breached in checks:
        v = x.f(key)
        if v is not None and breached(v, limit):
            penalties.append(-0.6)
            evidence += [fact(key, v), note(f"{why} (limit {limit:g})", key)]
    if not evidence:
        evidence.append(note("no risk flag raised by safety, volatility, liquidity or drawdown",
                             "safety.tradable", "atr_pct_14", "zero_volume_frac_48", "drawdown_48"))
    score = min(penalties, default=0.0)
    return (Stance.BEARISH if score <= -0.5 else Stance.NEUTRAL), score, tuple(evidence)


DEFAULT_AGENTS = (
    Agent("market.v1", "market", market),
    Agent("technical.v1", "technical", technical),
    Agent("data_quality.v1", "data_news", data_quality),
    Agent("bull.v1", "bull", bull),
    Agent("bear.v1", "bear", bear),
    Agent("risk.v1", "risk", risk),
)


# ----------------------------------------------------------------------- runner

def citation_errors(evidence: tuple[Evidence, ...], citable: dict[str, object]) -> list[str]:
    """Problems with what the evidence cites: unknown references, or a FACT citing a missing value."""
    errors = []
    for e in evidence:
        for ref in e.refs:
            if ref not in citable:
                errors.append(f"unknown reference {ref!r}")
            elif e.kind is EvidenceKind.FACT and citable[ref] is None:
                errors.append(f"fact cites {ref!r}, which has no value")
    return errors


def run_agent(agent: Agent, x: AgentInputs, cfg: AgentConfig = AgentConfig(),
              clock: Callable[[], float] = time.perf_counter) -> AgentOutput:
    start = clock()
    base = dict(agent_id=agent.agent_id, agent_version=agent.version, role=agent.role)
    try:
        stance, score, evidence = agent.run(x, cfg)
        errors = citation_errors(evidence, x.citable())
        if errors:
            raise ValueError("; ".join(errors))
        return AgentOutput(**base, status=AgentStatus.OK, stance=stance, score=score, evidence=evidence,
                           latency_ms=(clock() - start) * 1000)
    except Exception as err:  # a broken agent is recorded and discarded, never trusted
        return AgentOutput(**base, status=AgentStatus.INVALID, error=f"{type(err).__name__}: {err}"[:500],
                           latency_ms=(clock() - start) * 1000)


def run_agents(x: AgentInputs, agents: tuple[Agent, ...] = DEFAULT_AGENTS,
               cfg: AgentConfig = AgentConfig()) -> tuple[AgentOutput, ...]:
    return tuple(run_agent(a, x, cfg) for a in agents)
