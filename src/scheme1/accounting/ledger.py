"""Portfolio accounting for the paper desk: cash, positions, fees and realized P&L, rebuilt from fills.

Rules
- One quote currency: USD (GeckoTerminal prices and volumes are in USD).
- A buy costs quantity × fill price, plus the fee. The position's cost basis includes the buy fee, so
  ``avg_entry_price`` is the break-even price before the sell fee.
- A sell brings in quantity × fill price, minus the fee. Realized P&L = what came in − the cost basis of
  the part sold, so it is net of every fee.
- Money is rounded to ``MONEY_STEP``: costs and fees up, proceeds down (never in our favour).
- Invariants, checked on every fill before anything changes: cash never goes below zero, nothing is sold
  that isn't held, and cash + open cost basis − realized P&L == initial cash (nothing appears or vanishes).
  A violation raises ``AccountingError`` and the desk goes to EMERGENCY_STOP (ARCHITECTURE.md section 2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Iterable

from scheme1.contracts import Fill, Order, OrderReason, PortfolioSnapshot, Position, Side

MONEY_STEP = Decimal("0.00000001")
ZERO = Decimal(0)


class AccountingError(RuntimeError):
    """The books don't add up. The desk must stop trading until a person has looked."""


def fee_for(quantity: Decimal, price: Decimal, fee_pct: Decimal) -> Decimal:
    """Taker fee for a fill, rounded up."""
    return (quantity * price * fee_pct / 100).quantize(MONEY_STEP, rounding=ROUND_UP)


@dataclass
class _Holding:
    quantity: Decimal = ZERO
    cost_basis: Decimal = ZERO  # what the open quantity cost, buy fees included
    realized_pnl: Decimal = ZERO  # from partial sells of the open position; reset when a new one opens


class Ledger:
    def __init__(self, initial_cash: Decimal):
        if initial_cash <= 0:
            raise ValueError("initial cash must be positive")
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.fees_paid = ZERO
        self.realized_total = ZERO
        self._holdings: dict[str, _Holding] = {}

    # ------------------------------------------------------------------ fills

    def apply(self, fill: Fill) -> None:
        h = self._holdings.get(fill.symbol, _Holding())
        if fill.side is Side.BUY:
            cost = (fill.quantity * fill.price).quantize(MONEY_STEP, rounding=ROUND_UP)
            cash = self.cash - cost - fill.fee
            if cash < 0:
                raise AccountingError(f"buy of {fill.quantity} {fill.symbol} needs {cost + fill.fee}, "
                                      f"only {self.cash} cash (fill {fill.fill_id})")
            fresh = h.quantity == 0
            new = _Holding(quantity=h.quantity + fill.quantity, cost_basis=h.cost_basis + cost + fill.fee,
                           realized_pnl=ZERO if fresh else h.realized_pnl)
            realized_total = self.realized_total
        else:
            if fill.quantity > h.quantity:
                raise AccountingError(f"sell of {fill.quantity} {fill.symbol} but only {h.quantity} held "
                                      f"(fill {fill.fill_id})")
            proceeds = (fill.quantity * fill.price).quantize(MONEY_STEP, rounding=ROUND_DOWN)
            removed = (h.cost_basis if fill.quantity == h.quantity
                       else (h.cost_basis * fill.quantity / h.quantity).quantize(MONEY_STEP))
            pnl = proceeds - fill.fee - removed
            cash = self.cash + proceeds - fill.fee
            if cash < 0:
                raise AccountingError(f"sell fee larger than cash (fill {fill.fill_id})")
            new = _Holding(quantity=h.quantity - fill.quantity, cost_basis=h.cost_basis - removed,
                           realized_pnl=h.realized_pnl + pnl)
            realized_total = self.realized_total + pnl

        holdings = dict(self._holdings)
        holdings[fill.symbol] = new
        basis = sum((x.cost_basis for x in holdings.values()), ZERO)
        if cash + basis - realized_total != self.initial_cash:
            raise AccountingError(f"books don't balance after fill {fill.fill_id}: cash {cash} + basis {basis} "
                                  f"- realized {realized_total} != initial {self.initial_cash}")
        self.cash, self.realized_total, self._holdings = cash, realized_total, holdings
        self.fees_paid += fill.fee

    # ---------------------------------------------------------------- reading

    def quantity(self, symbol: str) -> Decimal:
        return self._holdings.get(symbol, _Holding()).quantity

    def open_symbols(self) -> list[str]:
        return sorted(s for s, h in self._holdings.items() if h.quantity > 0)

    def positions(self) -> tuple[Position, ...]:
        return tuple(Position(symbol=s, quantity=h.quantity, avg_entry_price=h.cost_basis / h.quantity,
                              realized_pnl=h.realized_pnl)
                     for s, h in sorted(self._holdings.items()) if h.quantity > 0)

    def snapshot(self, as_of: datetime, marks: dict[str, Decimal]) -> PortfolioSnapshot:
        missing = [s for s in self.open_symbols() if s not in marks]
        if missing:
            raise AccountingError(f"no price to value {', '.join(missing)}")
        equity = self.cash + sum((self._holdings[s].quantity * marks[s] for s in self.open_symbols()), ZERO)
        return PortfolioSnapshot(as_of=as_of, cash=self.cash, positions=self.positions(),
                                 marks={s: marks[s] for s in self.open_symbols()}, equity=equity,
                                 fees_paid=self.fees_paid)

    def matches(self, snapshot: PortfolioSnapshot) -> bool:
        """True when cash and every position agree exactly with a stored snapshot."""
        return snapshot.cash == self.cash and snapshot.positions == self.positions()

    @classmethod
    def rebuild(cls, initial_cash: Decimal, fills: Iterable[Fill]) -> Ledger:
        ledger = cls(initial_cash)
        for fill in fills:
            ledger.apply(fill)
        return ledger


# --------------------------------------------------------------- trade report

@dataclass
class RoundTrip:
    """One trade from the first buy to the sell that brings the position back to zero."""

    symbol: str
    opened_at: datetime
    closed_at: datetime | None = None  # None while still open
    quantity: Decimal = ZERO  # total bought
    cost: Decimal = ZERO  # total paid, buy fees included
    proceeds: Decimal = ZERO  # total received, sell fees taken off
    exit_reasons: list[str] = field(default_factory=list)

    @property
    def pnl(self) -> Decimal | None:
        return self.proceeds - self.cost if self.closed_at else None

    @property
    def pnl_pct(self) -> Decimal | None:
        return self.pnl / self.cost * 100 if self.closed_at and self.cost else None


def round_trips(fills: Iterable[Fill], orders: dict[str, Order]) -> list[RoundTrip]:
    """Group fills (in the order they happened) into trades. ``orders`` gives each sell's reason."""
    trips: list[RoundTrip] = []
    current: dict[str, RoundTrip] = {}
    held: dict[str, Decimal] = {}
    for f in fills:
        qty = held.get(f.symbol, ZERO)
        if f.side is Side.BUY:
            trip = current.get(f.symbol)
            if trip is None:
                trip = current[f.symbol] = RoundTrip(symbol=f.symbol, opened_at=f.filled_at)
                trips.append(trip)
            trip.quantity += f.quantity
            trip.cost += (f.quantity * f.price).quantize(MONEY_STEP, rounding=ROUND_UP) + f.fee
            held[f.symbol] = qty + f.quantity
        else:
            trip = current.get(f.symbol)
            if trip is None:
                raise AccountingError(f"sell fill {f.fill_id} with no open trade")
            trip.proceeds += (f.quantity * f.price).quantize(MONEY_STEP, rounding=ROUND_DOWN) - f.fee
            order = orders.get(f.order_id)
            reason = order.reason.value if order else OrderReason.SIGNAL.value
            if reason not in trip.exit_reasons:
                trip.exit_reasons.append(reason)
            held[f.symbol] = qty - f.quantity
            if held[f.symbol] == 0:
                trip.closed_at = f.filled_at
                del current[f.symbol]
    return trips
