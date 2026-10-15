"""Order authorizer: turns a BUY or SELL ``Decision`` into a simulated ``OrderIntent``.

BUY spends ``position_pct`` of equity (minus a fee and slippage buffer) at the reference price.
SELL closes the whole position. HOLD, a size that rounds to zero, or a SELL with nothing held give
no intent. The intent still has to pass the risk engine. Market orders only in v1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from scheme1.contracts import Decision, OrderIntent, OrderType, PortfolioSnapshot, Side, Signal


@dataclass(frozen=True)
class SizingConfig:
    position_pct: Decimal = Decimal(10)  # share of equity per new position
    cost_buffer_pct: Decimal = Decimal(2)  # kept aside for fee and slippage
    quantity_step: Decimal = Decimal("0.000001")


def held(portfolio: PortfolioSnapshot, symbol: str) -> Decimal:
    return sum((p.quantity for p in portfolio.positions if p.symbol == symbol), Decimal(0))


def authorize(decision: Decision, portfolio: PortfolioSnapshot, reference_price: Decimal, now: datetime,
              cfg: SizingConfig = SizingConfig()) -> OrderIntent | None:
    if decision.signal is Signal.HOLD:
        return None
    if decision.signal is Signal.SELL:
        quantity, side = held(portfolio, decision.symbol), Side.SELL
    else:
        budget = min(portfolio.equity * cfg.position_pct / 100, portfolio.cash) * (1 - cfg.cost_buffer_pct / 100)
        quantity, side = budget / reference_price, Side.BUY
    quantity = quantity.quantize(cfg.quantity_step, rounding=ROUND_DOWN)
    if quantity <= 0:
        return None
    return OrderIntent(decision_id=decision.decision_id, symbol=decision.symbol, side=side,
                       order_type=OrderType.MARKET, quantity=quantity, created_at=now)
