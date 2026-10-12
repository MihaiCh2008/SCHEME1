"""Typed contracts between SCHEME1 modules.

Every object that crosses a module boundary is defined here, so each stage can be
tested on its own and every persisted record has a known shape and schema version.

Conventions
- All timestamps are timezone-aware UTC. Naive datetimes are rejected.
- Prices, quantities, cash and fees are ``Decimal``, so accounting is exact.
  Feature values are ``float``, since they are statistics, not money.
- Models are frozen. A stage produces a new object; it never edits an old one.
- ``SCHEMA_VERSION`` changes whenever a persisted field changes meaning.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.3.0"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be in UTC")
    return value.astimezone(timezone.utc)


UTCDateTime = Annotated[datetime, AfterValidator(_utc)]
Price = Annotated[Decimal, Field(gt=0)]
Quantity = Annotated[Decimal, Field(ge=0)]
UnitScore = Annotated[float, Field(ge=-1.0, le=1.0)]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


# ---------------------------------------------------------------- market data


class Interval(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def duration(self) -> timedelta:
        return {
            "1m": timedelta(minutes=1),
            "5m": timedelta(minutes=5),
            "15m": timedelta(minutes=15),
            "1h": timedelta(hours=1),
            "4h": timedelta(hours=4),
            "1d": timedelta(days=1),
        }[self.value]


class DataKind(StrEnum):
    """Where data came from. Shown everywhere data is shown."""

    FIXTURE = "fixture"  # test data shipped with the repo, never real market data
    HISTORICAL = "historical"  # closed candles retrieved from a provider
    DELAYED = "delayed"  # provider data known to lag real time
    CURRENT = "current"  # latest provider data, freshness checked


class Candle(Contract):
    """One OHLCV bar. ``open_time`` identifies it; it covers [open_time, open_time + interval)."""

    symbol: str
    interval: Interval
    open_time: UTCDateTime
    open: Price
    high: Price
    low: Price
    close: Price
    volume: Quantity
    closed: bool = True

    @property
    def close_time(self) -> datetime:
        return self.open_time + self.interval.duration

    @model_validator(mode="after")
    def _ohlc_consistent(self) -> Candle:
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below open, close or low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is above open, close or high")
        return self


class Provenance(Contract):
    provider: str
    kind: DataKind
    retrieved_at: UTCDateTime
    request: dict[str, str] = Field(default_factory=dict)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class IssueCode(StrEnum):
    DUPLICATE = "duplicate"
    GAP = "gap"
    MISALIGNED = "misaligned"
    OUT_OF_ORDER = "out_of_order"
    INCOMPLETE = "incomplete"
    NONPOSITIVE_PRICE = "nonpositive_price"
    NEGATIVE_VOLUME = "negative_volume"
    OUTLIER = "outlier"
    WRONG_SYMBOL = "wrong_symbol"
    INCONSISTENT_OHLC = "inconsistent_ohlc"  # e.g. high below close
    MALFORMED = "malformed"  # row could not be parsed at all


class DataIssue(Contract):
    code: IssueCode
    open_time: UTCDateTime | None = None
    detail: str


class CandleBatch(Contract):
    """Raw candles exactly as received, plus where they came from."""

    batch_id: str = Field(default_factory=lambda: new_id("batch"))
    symbol: str
    interval: Interval
    provenance: Provenance
    candles: tuple[Candle, ...]
    # Rows the provider sent that could not become a ``Candle`` (bad prices, broken OHLC).
    # They are kept here so the validator can report them instead of losing them silently.
    parse_issues: tuple[DataIssue, ...] = ()


class DataStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"  # usable for research, blocks new orders
    STALE = "stale"  # newest closed candle older than the freshness limit
    UNAVAILABLE = "unavailable"  # provider failed or returned nothing usable


class ValidationReport(Contract):
    batch_id: str
    status: DataStatus
    issues: tuple[DataIssue, ...] = ()
    candles_in: int = Field(ge=0)
    candles_out: int = Field(ge=0)
    newest_closed_open_time: UTCDateTime | None = None
    checked_at: UTCDateTime


# ---------------------------------------------------------------- token safety


class PoolSnapshot(Contract):
    """What one provider reports about a DEX pool at one moment. ``None`` = the provider did not say."""

    symbol: str  # "solana:<pool address>"
    provider: str
    retrieved_at: UTCDateTime
    base_token_mint: str | None = None
    liquidity_usd: Annotated[Decimal, Field(ge=0)] | None = None
    pool_created_at: UTCDateTime | None = None
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HolderBalance(Contract):
    """Raw token amount held by one owner, summed over that owner's largest token accounts."""

    owner: str
    amount: int = Field(ge=0)
    program_controlled: bool  # owner is a program-derived address (pool vault, bonding curve, locker)


class TokenOnchainInfo(Contract):
    """Mint state and largest holders, read from a Solana RPC node."""

    mint: str
    retrieved_at: UTCDateTime
    token_program: Literal["spl-token", "spl-token-2022"]
    supply: int = Field(ge=0)  # raw units (10**decimals per token)
    decimals: int = Field(ge=0)
    mint_authority: str | None
    freeze_authority: str | None
    extensions: tuple[str, ...] = ()  # Token-2022 extension names
    holders: tuple[HolderBalance, ...]  # from the largest accounts the RPC returns (up to 20), largest first
    accounts_seen: int = Field(ge=0)
    smallest_account_seen: int = Field(ge=0)  # upper bound for any holder not in the list


class SafetyStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"  # verified, and outside the limit
    UNVERIFIED = "unverified"  # no trustworthy data; blocks exactly like FAIL


class SafetyCheck(Contract):
    name: str
    status: SafetyStatus
    observed: str
    limit: str
    detail: str = ""


class SafetyReport(Contract):
    """Token-safety verdict for one pool. A token is tradable only if every check passed."""

    symbol: str
    checked_at: UTCDateTime
    checks: tuple[SafetyCheck, ...]
    safety_config_version: str
    tradable: bool

    @model_validator(mode="after")
    def _tradable_means_all_passed(self) -> SafetyReport:
        all_passed = bool(self.checks) and all(c.status is SafetyStatus.PASS for c in self.checks)
        if self.tradable != all_passed:
            raise ValueError("tradable must be true exactly when every check passed")
        return self


# ------------------------------------------------------------------- features


class FeatureSnapshot(Contract):
    """Feature values known at ``as_of`` (the close time of the last candle used)."""

    symbol: str
    interval: Interval
    as_of: UTCDateTime
    feature_set_version: str
    values: dict[str, float | None]  # None = not computable yet (warm-up, zero denominator)
    input_batch_ids: tuple[str, ...]
    warmup_complete: bool


# --------------------------------------------------------------------- agents


class EvidenceKind(StrEnum):
    FACT = "fact"  # restates an input value; must cite it
    INTERPRETATION = "interpretation"
    HYPOTHESIS = "hypothesis"


class Evidence(Contract):
    kind: EvidenceKind
    statement: str = Field(min_length=1, max_length=600)
    refs: tuple[str, ...] = ()  # feature keys or record ids this rests on

    @model_validator(mode="after")
    def _facts_cite_inputs(self) -> Evidence:
        if self.kind is EvidenceKind.FACT and not self.refs:
            raise ValueError("a fact must reference at least one input")
        return self


class Stance(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    ABSTAIN = "abstain"


class AgentStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"  # output failed validation and was discarded


class AgentOutput(Contract):
    agent_id: str
    agent_version: str
    role: Literal["market", "technical", "data_news", "bull", "bear", "risk", "manager"]
    status: AgentStatus
    stance: Stance = Stance.ABSTAIN
    score: UnitScore = 0.0
    evidence: tuple[Evidence, ...] = ()
    error: str | None = None
    latency_ms: float = Field(ge=0)


# ------------------------------------------------------------------ decisions


class Signal(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class Confidence(Contract):
    """``value`` is a research score unless ``kind`` says it was calibrated out of sample."""

    value: float = Field(ge=0.0, le=1.0)
    kind: Literal["uncalibrated_research_score", "calibrated_probability"]
    calibration_ref: str | None = None

    @model_validator(mode="after")
    def _calibrated_needs_ref(self) -> Confidence:
        if self.kind == "calibrated_probability" and not self.calibration_ref:
            raise ValueError("a calibrated probability must reference its calibration run")
        return self


class Decision(Contract):
    """A research signal. It is not an order and authorizes nothing by itself."""

    schema_version: str = SCHEMA_VERSION
    decision_id: str = Field(default_factory=lambda: new_id("dec"))
    created_at: UTCDateTime
    symbol: str
    interval: Interval
    signal: Signal
    strategy_version: str
    model_version: str
    features: FeatureSnapshot
    bull_evidence: tuple[Evidence, ...] = ()
    bear_evidence: tuple[Evidence, ...] = ()
    risks: tuple[Evidence, ...] = ()
    agent_outputs: tuple[AgentOutput, ...] = ()
    data_status: DataStatus
    data_age_seconds: float = Field(ge=0)
    confidence: Confidence
    explanation: str
    rejection_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Decision:
        if self.features.symbol != self.symbol or self.features.interval != self.interval:
            raise ValueError("feature snapshot is for a different symbol or interval")
        if self.features.as_of > self.created_at:
            raise ValueError("decision is older than its own features")
        if self.data_status is not DataStatus.OK and self.signal is not Signal.HOLD:
            raise ValueError("only HOLD is allowed when data is not OK")
        return self


# ------------------------------------------------------------- risk and orders


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderIntent(Contract):
    """A proposed simulated order, derived from a decision. Must pass risk before it exists."""

    intent_id: str = Field(default_factory=lambda: new_id("int"))
    decision_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Annotated[Decimal, Field(gt=0)]
    limit_price: Price | None = None
    created_at: UTCDateTime

    @model_validator(mode="after")
    def _limit_has_price(self) -> OrderIntent:
        if (self.order_type is OrderType.LIMIT) != (self.limit_price is not None):
            raise ValueError("limit orders need a limit price; market orders must not have one")
        return self


class DeskState(StrEnum):
    NORMAL = "normal"
    DEGRADED = "degraded"  # data problems: exits and cancels only
    PAUSED = "paused"  # operator pause: no new orders
    EMERGENCY_STOP = "emergency_stop"  # kill switch: cancel everything, no new orders


class RiskCheck(Contract):
    name: str
    passed: bool
    observed: str
    limit: str
    detail: str = ""


class RiskVerdict(Contract):
    verdict_id: str = Field(default_factory=lambda: new_id("risk"))
    intent_id: str | None
    decision_id: str
    approved: bool
    desk_state: DeskState
    checks: tuple[RiskCheck, ...]
    risk_config_version: str
    evaluated_at: UTCDateTime

    @model_validator(mode="after")
    def _approved_means_all_passed(self) -> RiskVerdict:
        if self.approved and not all(c.passed for c in self.checks):
            raise ValueError("cannot approve with a failed check")
        if self.approved and self.desk_state is not DeskState.NORMAL:
            raise ValueError("cannot approve new orders unless the desk is NORMAL")
        return self


class OrderStatus(StrEnum):
    NEW = "NEW"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset({OrderStatus.OPEN, OrderStatus.REJECTED}),
    OrderStatus.OPEN: frozenset({OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED}),
    OrderStatus.PARTIALLY_FILLED: frozenset({OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED}),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
}


def can_transition(old: OrderStatus, new: OrderStatus) -> bool:
    return new in ORDER_TRANSITIONS[old]


class Fill(Contract):
    fill_id: str = Field(default_factory=lambda: new_id("fill"))
    order_id: str
    symbol: str
    side: Side
    quantity: Annotated[Decimal, Field(gt=0)]
    price: Price  # after slippage
    reference_price: Price  # before slippage (candle open or limit)
    fee: Quantity
    candle_open_time: UTCDateTime  # the candle whose prices made this fill possible
    filled_at: UTCDateTime
    liquidity: Literal["taker", "maker"]


class Position(Contract):
    symbol: str
    quantity: Decimal  # long-only spot in v1: never negative, enforced by the broker
    avg_entry_price: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)


class PortfolioSnapshot(Contract):
    as_of: UTCDateTime
    cash: Decimal
    positions: tuple[Position, ...]
    marks: dict[str, Decimal]  # symbol -> price used for valuation
    equity: Decimal
    fees_paid: Decimal
