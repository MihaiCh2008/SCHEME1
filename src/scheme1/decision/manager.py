"""Decision engine: weighs the agent outputs into one research ``Decision``.

Order of rules (the first that applies wins):
1. Data not OK -> HOLD (the ``Decision`` contract enforces this too). With a position open this
   also means no exit, because the price can't be trusted; ``rejection_reasons`` says so.
2. A position is open -> exit rules: take profit, stop loss, time limit, token no longer tradable,
   or the combined score turned clearly bearish -> SELL. Otherwise HOLD.
3. No position -> BUY only if the token is tradable, the risk agent raised no blocking risk,
   market and technical both gave an opinion, and the combined score clears ``buy_threshold``.
   Otherwise HOLD.

Combined score = weighted mean of the market and technical scores and the bull/bear balance.
Votes are not counted as proof: bull and bear are advocates and only shift the score.
Confidence = |score| × (1 − disagreement). Disagreement grows when market and technical point
opposite ways and when bull and bear are both strong. It is an uncalibrated research score.

Exits are checked on candle closes, and a SELL fills at the next candle's open (ARCHITECTURE.md
section 3). So a loss can be larger than ``stop_loss_pct`` when the price gaps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from scheme1.agents import AgentInputs
from scheme1.contracts import (
    AgentOutput, AgentStatus, Confidence, DataStatus, Decision, Evidence, Signal, Stance,
)

STRATEGY_VERSION = "quick-1.0.0"
MODEL_VERSION = "deterministic-a1.0.0"


@dataclass(frozen=True)
class StrategyConfig:
    buy_threshold: float = 0.35  # combined score needed to enter
    exit_score: float = -0.35  # combined score that closes a position early
    take_profit_pct: Decimal = Decimal(5)
    stop_loss_pct: Decimal = Decimal(3)
    max_hold: timedelta = timedelta(hours=2)
    round_trip_cost_pct: Decimal = Decimal("1.0")  # fees + slippage, buy and sell together (a guess)
    weights: tuple[tuple[str, float], ...] = (("market", 0.35), ("technical", 0.35), ("advocates", 0.3))
    version: str = STRATEGY_VERSION

    def __post_init__(self) -> None:
        if self.take_profit_pct <= self.round_trip_cost_pct:
            raise ValueError("take profit must be larger than the round-trip cost, or every win loses money")
        if not 0 < self.stop_loss_pct < 100:
            raise ValueError("stop loss must be between 0 and 100 percent")
        if self.exit_score >= self.buy_threshold:
            raise ValueError("exit_score must be below buy_threshold")


@dataclass(frozen=True)
class OpenPosition:
    """What the decision engine needs to know about a position it holds."""

    entry_price: Decimal
    opened_at: datetime
    quantity: Decimal


def _ok(outputs: tuple[AgentOutput, ...]) -> dict[str, AgentOutput]:
    return {o.role: o for o in outputs if o.status is AgentStatus.OK}


def combine(outputs: tuple[AgentOutput, ...], cfg: StrategyConfig) -> tuple[float, float, list[str]]:
    """Combined score, confidence and notes. Abstaining or invalid agents are left out and weights renormalized."""
    ok = _ok(outputs)
    notes = [f"{o.role} agent {o.status.value}: {o.error}" for o in outputs if o.status is not AgentStatus.OK]
    parts: list[tuple[float, float]] = []
    for role, weight in cfg.weights:
        if role == "advocates":
            if "bull" in ok and "bear" in ok:
                parts.append((ok["bull"].score + ok["bear"].score, weight))  # bear score is <= 0
        elif role in ok and ok[role].stance is not Stance.ABSTAIN:
            parts.append((ok[role].score, weight))
    if not parts:
        return 0.0, 0.0, notes + ["no usable agent opinion"]
    score = max(-1.0, min(1.0, sum(s * w for s, w in parts) / sum(w for _, w in parts)))

    disagreement = 0.0
    m, t = ok.get("market"), ok.get("technical")
    if m and t and m.score * t.score < 0:
        disagreement += 0.5
        notes.append("market and technical disagree")
    if "bull" in ok and "bear" in ok:
        contested = min(ok["bull"].score, -ok["bear"].score)
        if contested > 0.3:
            notes.append("bull and bear are both strong")
        disagreement += contested / 2
    return score, abs(score) * max(0.0, 1 - disagreement), notes


def _evidence(outputs: tuple[AgentOutput, ...], role: str) -> tuple[Evidence, ...]:
    return next((o.evidence for o in outputs if o.role == role and o.status is AgentStatus.OK), ())


def decide(x: AgentInputs, outputs: tuple[AgentOutput, ...], now: datetime, last_close: Decimal,
           position: OpenPosition | None = None, cfg: StrategyConfig = StrategyConfig()) -> Decision:
    score, confidence, notes = combine(outputs, cfg)
    ok = _ok(outputs)
    reasons: list[str] = []
    signal = Signal.HOLD
    data_ok = x.validation.status is DataStatus.OK

    if not data_ok:
        reasons.append(f"data is {x.validation.status.value}")
        if position:
            reasons.append("position open but no exit: the price can't be trusted")
    elif position:
        change_pct = (last_close / position.entry_price - 1) * 100
        held = now - position.opened_at
        exits = [
            (change_pct >= cfg.take_profit_pct, f"take profit: {change_pct:+.2f}% >= +{cfg.take_profit_pct}%"),
            (change_pct <= -cfg.stop_loss_pct, f"stop loss: {change_pct:+.2f}% <= -{cfg.stop_loss_pct}%"),
            (held >= cfg.max_hold, f"time limit: held {held} >= {cfg.max_hold}"),
            (not x.safety.tradable, "token is no longer tradable"),
            (score <= cfg.exit_score, f"combined score {score:+.2f} <= {cfg.exit_score:+.2f}"),
        ]
        hits = [why for hit, why in exits if hit]
        if hits:
            signal = Signal.SELL
            notes = hits + notes
        else:
            reasons.append(f"holding: {change_pct:+.2f}%, held {held}, score {score:+.2f}")
    else:
        if not x.safety.tradable:
            reasons.append("token failed safety checks")
        if "risk" not in ok:
            reasons.append("risk agent gave no output")
        elif ok["risk"].stance is Stance.BEARISH:
            reasons.append(f"risk agent flagged a blocking risk (score {ok['risk'].score:+.2f})")
        for role in ("market", "technical"):
            if role not in ok or ok[role].stance is Stance.ABSTAIN:
                reasons.append(f"{role} agent has no opinion")
        if score < cfg.buy_threshold:
            reasons.append(f"combined score {score:+.2f} < buy threshold {cfg.buy_threshold:+.2f}")
        if not reasons:
            signal = Signal.BUY

    explanation = f"{signal.value}: " + "; ".join(notes if signal is not Signal.HOLD else reasons + notes)
    return Decision(
        created_at=now, symbol=x.features.symbol, interval=x.features.interval, signal=signal,
        strategy_version=cfg.version, model_version=MODEL_VERSION, features=x.features,
        bull_evidence=_evidence(outputs, "bull"), bear_evidence=_evidence(outputs, "bear"),
        risks=_evidence(outputs, "risk"),
        agent_outputs=outputs, data_status=x.validation.status,
        data_age_seconds=max(0.0, (now - x.features.as_of).total_seconds()),
        confidence=Confidence(value=round(confidence, 6), kind="uncalibrated_research_score"),
        explanation=explanation[:2000], rejection_reasons=tuple(reasons) if signal is Signal.HOLD else (),
    )
