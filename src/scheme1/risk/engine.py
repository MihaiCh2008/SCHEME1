"""Risk engine: the last gate before a simulated order exists (ARCHITECTURE.md section 4).

It runs outside the agents, reads its limits from ``RiskConfig`` (agents can't change it), runs
every check every time and returns them all, passes included. Any failed check rejects.

A SELL that only reduces a held position (an exit) skips the checks that only make sense for new
risk (cash, size, exposure, token safety, frequency) and is allowed while the desk is DEGRADED or
PAUSED, so a stop loss is never locked in. EMERGENCY_STOP rejects everything.

Not built yet: ``correlated_exposure`` (needs rolling correlations between held tokens).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from scheme1.contracts import (
    DataStatus, DeskState, OrderIntent, PortfolioSnapshot, RiskCheck, RiskVerdict, Side,
)

RISK_CONFIG_VERSION = "r1.0.0"


@dataclass(frozen=True)
class RiskConfig:
    max_data_age: timedelta = timedelta(minutes=10)
    max_position_pct: Decimal = Decimal(10)  # one position, % of equity
    max_gross_exposure_pct: Decimal = Decimal(50)  # all positions together
    max_asset_pct: Decimal = Decimal(15)  # largest single asset after the order
    max_drawdown_pct: Decimal = Decimal(20)  # from peak equity
    max_orders_per_day: int = 20
    cooldown: timedelta = timedelta(minutes=15)  # between orders on one symbol
    fee_pct: Decimal = Decimal("0.25")
    slippage_buffer_pct: Decimal = Decimal(1)
    version: str = RISK_CONFIG_VERSION


@dataclass(frozen=True)
class RiskContext:
    portfolio: PortfolioSnapshot
    desk_state: DeskState
    peak_equity: Decimal
    data_status: DataStatus
    data_age: timedelta
    token_tradable: bool
    reference_price: Decimal
    now: datetime
    orders_today: int = 0
    last_order_at: dict[str, datetime] = field(default_factory=dict)
    open_order_symbols: frozenset[str] = frozenset()


def _check(name: str, passed: bool, observed: str, limit: str, detail: str = "") -> RiskCheck:
    return RiskCheck(name=name, passed=passed, observed=observed, limit=limit, detail=detail)


def _value(portfolio: PortfolioSnapshot, symbol: str, extra_qty: Decimal, price: Decimal) -> dict[str, Decimal]:
    """Market value of every position after the order (the order's symbol valued at ``price``)."""
    values: dict[str, Decimal] = {}
    for p in portfolio.positions:
        mark = price if p.symbol == symbol else portfolio.marks.get(p.symbol, p.avg_entry_price)
        values[p.symbol] = values.get(p.symbol, Decimal(0)) + p.quantity * mark
    values[symbol] = values.get(symbol, Decimal(0)) + extra_qty * price
    return values


def evaluate_risk(intent: OrderIntent, ctx: RiskContext, cfg: RiskConfig = RiskConfig()) -> RiskVerdict:
    pf, sym = ctx.portfolio, intent.symbol
    held = sum((p.quantity for p in pf.positions if p.symbol == sym), Decimal(0))
    is_exit = intent.side is Side.SELL
    pct = lambda part, whole: part / whole * 100 if whole > 0 else Decimal(100)  # noqa: E731
    checks: list[RiskCheck] = []

    allowed = {DeskState.NORMAL} | ({DeskState.DEGRADED, DeskState.PAUSED} if is_exit else set())
    checks.append(_check("desk_state", ctx.desk_state in allowed, ctx.desk_state.value,
                         "normal" + (" (exits also when degraded or paused)" if is_exit else "")))
    checks.append(_check("data_quality", ctx.data_status is DataStatus.OK, ctx.data_status.value, "ok"))
    checks.append(_check("data_age", ctx.data_age <= cfg.max_data_age, str(ctx.data_age), f"<= {cfg.max_data_age}"))
    checks.append(_check("short_sale", not is_exit or intent.quantity <= held,
                         f"sell {intent.quantity}, held {held}" if is_exit else "buy", "sell <= held"))
    checks.append(_check("duplicate_or_conflict", sym not in ctx.open_order_symbols,
                         "open order exists" if sym in ctx.open_order_symbols else "none", "no open order on the symbol"))
    drawdown = pct(ctx.peak_equity - pf.equity, ctx.peak_equity) if ctx.peak_equity > 0 else Decimal(0)
    checks.append(_check("drawdown", is_exit or drawdown <= cfg.max_drawdown_pct, f"{drawdown:.2f}%",
                         f"<= {cfg.max_drawdown_pct}% (exits always allowed)"))

    if not is_exit:
        cost = intent.quantity * ctx.reference_price * (1 + (cfg.fee_pct + cfg.slippage_buffer_pct) / 100)
        checks.append(_check("cash", cost <= pf.cash, f"needs {cost:.2f}, has {pf.cash:.2f}",
                             "cost incl. fee and slippage <= cash"))
        after = _value(pf, sym, intent.quantity, ctx.reference_price)
        checks.append(_check("position_size", pct(after[sym], pf.equity) <= cfg.max_position_pct,
                             f"{pct(after[sym], pf.equity):.2f}%", f"<= {cfg.max_position_pct}% of equity"))
        gross = sum(after.values(), Decimal(0))
        checks.append(_check("gross_exposure", pct(gross, pf.equity) <= cfg.max_gross_exposure_pct,
                             f"{pct(gross, pf.equity):.2f}%", f"<= {cfg.max_gross_exposure_pct}% of equity"))
        top_sym, top = max(after.items(), key=lambda kv: kv[1])
        checks.append(_check("concentration", pct(top, pf.equity) <= cfg.max_asset_pct,
                             f"{top_sym} {pct(top, pf.equity):.2f}%", f"<= {cfg.max_asset_pct}% of equity"))
        checks.append(_check("token_safety", ctx.token_tradable, str(ctx.token_tradable), "every safety check passed"))
        last = ctx.last_order_at.get(sym)
        cooling = last is not None and ctx.now - last < cfg.cooldown
        checks.append(_check("frequency", ctx.orders_today < cfg.max_orders_per_day and not cooling,
                             f"{ctx.orders_today} orders today; last on symbol {last.isoformat() if last else 'never'}",
                             f"< {cfg.max_orders_per_day}/day and >= {cfg.cooldown} apart"))

    approved = all(c.passed for c in checks)
    return RiskVerdict(intent_id=intent.intent_id, decision_id=intent.decision_id, approved=approved,
                       desk_state=ctx.desk_state, checks=tuple(checks), risk_config_version=cfg.version,
                       evaluated_at=ctx.now, exit_only=is_exit)
