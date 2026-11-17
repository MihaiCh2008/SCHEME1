"""Paper broker: fills approved orders against later candles (ARCHITECTURE.md section 3). Simulation only.

``process_candle`` is called once per closed candle and symbol, oldest first. Inside one candle:

1. Desk not NORMAL: open BUY orders are canceled; exits carry on. EMERGENCY_STOP: everything is canceled
   and nothing trades.
2. Orders whose ``not_before`` has come fill at the candle's **open**. Nothing is filled from a candle that
   opened before the order existed, so there is no look-ahead.
   - BUY at open × (1 + slippage), never for more than the cash allows. A price jump since the decision
     doesn't stop it: the strategy buys on FOMO (D20). ``entry_tolerance_pct`` can still be set for a test,
     then a buy is skipped when the open is that far above the decision price (like a DEX slippage limit).
   - SELL at open × (1 − slippage), never more than is held.
3. **Time limit:** a position held for ``max_hold`` or longer at the open is sold from the open.
4. **Protective exits** (trailing stop, take profit; ``scheme1.broker.protective``) are checked from the
   entry candle on, since the entry fills at the open and the rest of the candle comes after it.
   Then the trailing stop moves up.
5. A BUY that has seen ``entry_ttl_candles`` candles (default 1) without filling completely expires:
   an entry that can't fill now is dropped. A SELL never expires.

Every fill pays the taker fee. All fills on a symbol in one candle together use at most
``participation_pct`` of the candle's USD volume; a candle without volume fills nothing. Whatever an exit
couldn't sell rests and fills at the next candles' opens.

Protective and time-limit exits are run by the broker itself, not by ``decide()``: they were planned when
the entry was approved, they only reduce risk, and they must work even when the data is degraded.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from scheme1.accounting import MONEY_STEP, fee_for
from scheme1.broker.protective import ProtectiveLevels, check_candle
from scheme1.contracts import (
    Candle, DeskState, Fill, Order, OrderIntent, OrderReason, OrderStatus, OrderType, RiskVerdict, Side,
    can_transition,
)

ZERO = Decimal(0)


@dataclass(frozen=True)
class BrokerConfig:
    fee_pct: Decimal = Decimal("0.25")  # DEX pool fee, paid on every fill
    slippage_pct: Decimal = Decimal("0.25")  # the price moves against every market fill
    participation_pct: Decimal = Decimal(5)  # share of a candle's USD volume we may trade
    entry_tolerance_pct: Decimal | None = None  # off (D20); set it to skip buys after a jump this big
    entry_ttl_candles: int = 1
    quantity_step: Decimal = Decimal("0.000001")

    def __post_init__(self) -> None:
        if self.entry_ttl_candles < 1:
            raise ValueError("entry orders need at least one candle")
        if not 0 < self.participation_pct <= 100:
            raise ValueError("participation must be between 0 and 100 percent")

    @property
    def round_trip_cost_pct(self) -> Decimal:
        return 2 * (self.fee_pct + self.slippage_pct)


@dataclass(frozen=True)
class ExitPlan:
    """The exits set when a position opens (from the strategy config, D16, D17)."""

    stop_loss_pct: Decimal
    take_profit_pct: Decimal
    trail_pct: Decimal | None
    max_hold: timedelta


@dataclass(frozen=True)
class PositionGuard:
    """Protective exits for one open position."""

    symbol: str
    decision_id: str  # the decision that opened the position
    opened_at: datetime
    entry_price: Decimal  # first entry fill price, after slippage
    levels: ProtectiveLevels
    max_hold: timedelta

    def to_dict(self) -> dict[str, str | None]:
        return {"symbol": self.symbol, "decision_id": self.decision_id, "opened_at": self.opened_at.isoformat(),
                "entry_price": str(self.entry_price), "stop_price": str(self.levels.stop_price),
                "take_profit_price": str(self.levels.take_profit_price),
                "trail_pct": None if self.levels.trail_pct is None else str(self.levels.trail_pct),
                "highest": str(self.levels.highest), "max_hold_s": str(self.max_hold.total_seconds())}

    @classmethod
    def from_dict(cls, d: dict) -> PositionGuard:
        levels = ProtectiveLevels(stop_price=Decimal(d["stop_price"]), take_profit_price=Decimal(d["take_profit_price"]),
                                  trail_pct=None if d["trail_pct"] is None else Decimal(d["trail_pct"]),
                                  highest=Decimal(d["highest"]))
        return cls(symbol=d["symbol"], decision_id=d["decision_id"], opened_at=datetime.fromisoformat(d["opened_at"]),
                   entry_price=Decimal(d["entry_price"]), levels=levels,
                   max_hold=timedelta(seconds=float(d["max_hold_s"])))


@dataclass
class BrokerResult:
    orders: list[Order] = field(default_factory=list)  # every new order version, in order
    fills: list[Fill] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class PaperBroker:
    def __init__(self, config: BrokerConfig, exits: ExitPlan):
        self.config = config
        self.exits = exits
        self._orders: dict[str, Order] = {}  # open orders only
        self._guards: dict[str, PositionGuard] = {}

    # ------------------------------------------------------------- reading

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self._orders.values() if symbol is None or o.symbol == symbol]

    def pending_exit(self, symbol: str) -> bool:
        return any(o.side is Side.SELL for o in self.open_orders(symbol))

    def guard(self, symbol: str) -> PositionGuard | None:
        return self._guards.get(symbol)

    def guards(self) -> list[PositionGuard]:
        return list(self._guards.values())

    def restore(self, orders: list[Order], guards: list[PositionGuard]) -> None:
        """Load the open orders and guards saved before a restart."""
        self._orders = {o.order_id: o for o in orders if o.is_open}
        self._guards = {g.symbol: g for g in guards}

    # ------------------------------------------------------------ ordering

    def submit(self, intent: OrderIntent, verdict: RiskVerdict, reference_price: Decimal,
               not_before: datetime, now: datetime) -> Order:
        """Accept an order the risk engine approved. It can first fill in the candle opening at ``not_before``."""
        if not verdict.approved or verdict.intent_id != intent.intent_id:
            raise ValueError("only an intent with its own approved risk verdict can become an order")
        order = Order(intent_id=intent.intent_id, decision_id=intent.decision_id, symbol=intent.symbol,
                      side=intent.side, order_type=intent.order_type, reason=OrderReason.SIGNAL,
                      quantity=intent.quantity, reference_price=reference_price, status=OrderStatus.NEW,
                      created_at=now, updated_at=now, not_before=not_before,
                      ttl_candles=self.config.entry_ttl_candles if intent.side is Side.BUY else None)
        if intent.order_type is not OrderType.MARKET:
            return self._update(order, now, status=OrderStatus.REJECTED, detail="v1 supports market orders only")
        order = self._update(order, now, status=OrderStatus.OPEN)
        self._orders[order.order_id] = order
        return order

    def cancel(self, now: datetime, detail: str, symbol: str | None = None,
               side: Side | None = None) -> list[Order]:
        canceled = []
        for o in self.open_orders(symbol):
            if side is None or o.side is side:
                canceled.append(self._update(o, now, status=OrderStatus.CANCELED, detail=detail))
                del self._orders[o.order_id]
        return canceled

    # --------------------------------------------------------------- candles

    def process_candle(self, candle: Candle, cash: Decimal, held: Decimal, desk_state: DeskState) -> BrokerResult:
        """Run one closed candle for its symbol. ``cash`` and ``held`` come from the ledger before this candle."""
        res = BrokerResult()
        sym, t_open = candle.symbol, candle.open_time
        if desk_state is DeskState.EMERGENCY_STOP:
            res.orders += self.cancel(t_open, "emergency stop", symbol=sym)
            return res
        if desk_state is not DeskState.NORMAL:
            res.orders += self.cancel(t_open, f"desk is {desk_state.value}: no new entries", symbol=sym, side=Side.BUY)

        state = _CandleState(budget=candle.volume * self.config.participation_pct / 100, cash=cash, held=held)
        for order in sorted(self.open_orders(sym), key=lambda o: o.created_at):
            if order.not_before > t_open:
                continue  # decided after this candle opened: its first chance is a later candle
            self._at_open(order, candle, state, res)

        guard = self._guards.get(sym)
        if guard and state.held > 0 and not self.pending_exit(sym) and t_open - guard.opened_at >= guard.max_hold:
            order = self._new_exit(guard, OrderReason.TIME_LIMIT, state.held, candle.open, t_open, res)
            res.notes.append(f"time limit: held {t_open - guard.opened_at}")
            self._at_open(order, candle, state, res)

        guard = self._guards.get(sym)
        if guard and state.held > 0 and not self.pending_exit(sym):
            hit = check_candle(candle, guard.levels, self.config.slippage_pct)
            if hit is None:
                self._guards[sym] = replace(guard, levels=guard.levels.after(candle))
            else:
                reason = OrderReason(hit.reason)
                order = self._new_exit(guard, reason, state.held, hit.reference_price, t_open, res)
                res.notes.append(f"{reason.value} at {hit.reference_price}" + (" (price gapped)" if hit.gapped else ""))
                qty = self._cap(state.held, state.budget, hit.reference_price)
                if qty > 0:  # otherwise it rests and sells from the next open
                    res.orders.append(self._apply_fill(order, candle, hit.reference_price, hit.fill_price, qty,
                                                       candle.close_time, state, res))
        if state.held == 0:
            self._guards.pop(sym, None)
        return res

    # --------------------------------------------------------------- helpers

    def _at_open(self, order: Order, candle: Candle, state: _CandleState, res: BrokerResult) -> None:
        """Try to fill ``order`` at the candle's open; record its final version for this candle."""
        cfg = self.config
        at = max(candle.open_time, order.created_at)  # live: the order may be a few seconds younger than the open
        order = self._update(order, at, candles_seen=order.candles_seen + 1)
        self._orders[order.order_id] = order
        detail = ""
        if order.side is Side.SELL and state.held == 0:
            order = self._update(order, at, status=OrderStatus.CANCELED, detail="nothing left to sell")
        elif candle.volume == 0:
            detail = "no trades in the candle"
        elif order.side is Side.BUY:
            tolerance = cfg.entry_tolerance_pct
            if tolerance is not None and candle.open > order.reference_price * (1 + tolerance / 100):
                move = (candle.open / order.reference_price - 1) * 100
                detail = f"price opened {move:.2f}% above the decision price (limit {tolerance}%)"
            else:
                price = candle.open * (1 + cfg.slippage_pct / 100)
                qty = min(self._cap(order.remaining, state.budget, candle.open), self._affordable(state.cash, price))
                if qty > 0:
                    order = self._apply_fill(order, candle, candle.open, price, qty, at, state, res)
                else:
                    detail = "no volume or cash left in the candle"
        else:
            qty = self._cap(min(order.remaining, state.held), state.budget, candle.open)
            if qty > 0:
                order = self._apply_fill(order, candle, candle.open, candle.open * (1 - cfg.slippage_pct / 100), qty,
                                         at, state, res)
        if order.is_open and order.ttl_candles is not None and order.candles_seen >= order.ttl_candles:
            order = self._update(order, at, status=OrderStatus.EXPIRED,
                                 detail=detail or "rest of the entry dropped: not filled in its first candle")
        if order.is_open:
            self._orders[order.order_id] = order
        else:
            self._orders.pop(order.order_id, None)
        res.orders.append(order)

    def _apply_fill(self, order: Order, candle: Candle, reference: Decimal, price: Decimal, qty: Decimal,
                    at: datetime, state: _CandleState, res: BrokerResult) -> Order:
        fee = fee_for(qty, price, self.config.fee_pct)
        fill = Fill(order_id=order.order_id, symbol=order.symbol, side=order.side, quantity=qty, price=price,
                    reference_price=reference, fee=fee, candle_open_time=candle.open_time, filled_at=at,
                    liquidity="taker")
        res.fills.append(fill)
        filled = order.filled_quantity + qty
        avg = ((order.avg_fill_price or ZERO) * order.filled_quantity + price * qty) / filled
        status = OrderStatus.FILLED if filled == order.quantity else OrderStatus.PARTIALLY_FILLED
        order = self._update(order, at, filled_quantity=filled, avg_fill_price=avg, status=status)
        state.budget -= qty * reference
        if order.side is Side.BUY:
            state.cash -= self._buy_cost(qty, price)
            state.held += qty
            if order.symbol not in self._guards:
                e = self.exits
                self._guards[order.symbol] = PositionGuard(
                    symbol=order.symbol, decision_id=order.decision_id, opened_at=candle.open_time, entry_price=price,
                    levels=ProtectiveLevels.from_entry(price, e.stop_loss_pct, e.take_profit_pct, e.trail_pct),
                    max_hold=e.max_hold)
        else:
            state.cash += (qty * price).quantize(MONEY_STEP, rounding=ROUND_DOWN) - fee
            state.held -= qty
        if order.is_open:
            self._orders[order.order_id] = order
        else:
            self._orders.pop(order.order_id, None)
        return order

    def _new_exit(self, guard: PositionGuard, reason: OrderReason, qty: Decimal, reference: Decimal,
                  at: datetime, res: BrokerResult) -> Order:
        order = Order(intent_id=None, decision_id=guard.decision_id, symbol=guard.symbol, side=Side.SELL,
                      order_type=OrderType.MARKET, reason=reason, quantity=qty, reference_price=reference,
                      status=OrderStatus.NEW, created_at=at, updated_at=at, not_before=at, ttl_candles=None)
        order = self._update(order, at, status=OrderStatus.OPEN)
        self._orders[order.order_id] = order
        res.orders.append(order)
        return order

    def _cap(self, wanted: Decimal, budget_usd: Decimal, price: Decimal) -> Decimal:
        """``wanted`` limited by the candle's remaining volume budget, rounded down to the quantity step."""
        by_volume = budget_usd / price if budget_usd > 0 else ZERO
        return max(ZERO, min(wanted, by_volume)).quantize(self.config.quantity_step, rounding=ROUND_DOWN)

    def _affordable(self, cash: Decimal, price: Decimal) -> Decimal:
        step = self.config.quantity_step
        qty = (max(ZERO, cash - 2 * MONEY_STEP) / (price * (1 + self.config.fee_pct / 100))).quantize(
            step, rounding=ROUND_DOWN)
        while qty > 0 and self._buy_cost(qty, price) > cash:
            qty -= step
        return qty

    def _buy_cost(self, qty: Decimal, price: Decimal) -> Decimal:
        """What the ledger will charge for a buy: cost rounded up, plus the fee."""
        return (qty * price).quantize(MONEY_STEP, rounding=ROUND_UP) + fee_for(qty, price, self.config.fee_pct)

    @staticmethod
    def _update(order: Order, at: datetime, **changes) -> Order:
        new_status = changes.get("status", order.status)
        if new_status is not order.status and not can_transition(order.status, new_status):
            raise ValueError(f"order {order.order_id}: {order.status.value} -> {new_status.value} is not allowed")
        return Order.model_validate({**order.model_dump(), **changes, "updated_at": max(at, order.updated_at)})


@dataclass
class _CandleState:
    budget: Decimal  # USD volume still available to us in this candle
    cash: Decimal
    held: Decimal
