"""The paper desk: one step per closed candle, from data to simulated fills (ARCHITECTURE.md section 1).

``PaperDesk.on_candle(inputs, now)`` runs, in one transaction:
1. Skip a candle that was already processed (the live loop may see one twice).
2. Data flag (watched bot pools only): the pool's data status sets or clears DEGRADED.
3. Broker: resting orders, time limit and protective exits on this candle (``scheme1.broker.paper``).
4. Ledger: apply the fills. Books that don't balance → EMERGENCY_STOP.
5. Mark the symbol to the candle close, take a portfolio snapshot, track peak and day-start equity.
   Total equity more than ``max_drawdown_pct`` below its peak → PAUSED.
6. Research (bot sleeve, watched pools only), unless the desk is in EMERGENCY_STOP, an exit is already
   pending, there are no features, or the copy sleeve holds the token:
   agents → ``decide()`` → ``authorize()`` → ``evaluate_risk()`` → ``broker.submit()`` when approved.
   An order made here can first fill in the next candle.
7. Rebalance the split if it is due (below), then save the working state.

**Two sleeves (D23).** The money is split into two ledgers, 50/50 at the start:
- ``bot``: the desk's own FOMO strategy on watched pools (Stages 5–6).
- ``copy``: copies the buys of traders the user follows (``on_trader_trade``).
Each sleeve has its own cash, positions, profit, daily loss limit, risk limits, costs and exits, so one
can't eat the other's money. A token is held by one sleeve at a time. **Rebalancing:** once both sleeves
have 30 closed trades since the last rebalance and 30 days have passed, up to 10% of total equity moves
from the sleeve with the lower return to the one with the higher return; each keeps at least 20%.

**Copy trades** (``on_trader_trade``, D22, D23), in one transaction:
- A followed trader BUYS a token → buy it right away at the current pool price (+ slippage, fee), if: the
  desk is NORMAL, the trade is big enough (0.1 SOL / $10), we don't hold the token, fewer than 5 copy
  positions are open, the token passes the copy safety checks, and the risk engine approves.
- That trader SELLS it → sell our whole position right away (other traders' sells don't count).
- The trailing stop and 24-hour limit run on the token's 1-minute candles, like any position.

Desk states, strongest first:
- EMERGENCY_STOP: kill switch, broken books or a broken audit log. All orders are canceled and nothing
  trades. Only ``reset()`` clears it, and only when the log and the books check out.
- PAUSED: by the operator, or on the drawdown limit. Only ``resume()`` clears it.
- DEGRADED (bot sleeve only): automatic while any watched pool's data is not OK; clears by itself.
- NORMAL.
Outside NORMAL there are no new entries, but exits keep working (D15).

A restart rebuilds each sleeve's books from the fills and transfers in the audit log and compares them
with the last stored snapshot. Any difference → EMERGENCY_STOP (ARCHITECTURE.md section 7). Simulation
only: nothing here can reach a wallet or an exchange.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any

from scheme1.accounting import MONEY_STEP, AccountingError, Ledger, round_trips
from scheme1.agents import DEFAULT_AGENTS, Agent, AgentConfig, AgentInputs, run_agents
from scheme1.broker import BrokerConfig, BrokerResult, ExitPlan, PaperBroker, PositionGuard, ProtectiveLevels
from scheme1.contracts import (
    Candle, DataStatus, Decision, DeskState, FeatureSnapshot, Fill, Order, OrderIntent, OrderReason, OrderType,
    PortfolioSnapshot, Position, RiskVerdict, SafetyReport, SafetyStatus, Side, TraderTrade, ValidationReport, new_id,
)
from scheme1.copytrade.strategy import CopyConfig, CopyMarket
from scheme1.decision import OpenPosition, SizingConfig, StrategyConfig, authorize, decide
from scheme1.desk.store import DeskStore, clean, slim_decision
from scheme1.risk import RiskConfig, RiskContext, evaluate_risk

SLEEVES = ("bot", "copy")
ZERO = Decimal(0)


@dataclass(frozen=True)
class RebalanceConfig:
    min_trades: int = 30  # closed trades per sleeve since the last rebalance
    every: timedelta = timedelta(days=30)
    step: Decimal = Decimal("0.10")  # share of total equity moved at most
    floor: Decimal = Decimal("0.20")  # each sleeve keeps at least this share


@dataclass(frozen=True)
class DeskConfig:
    initial_cash: Decimal = Decimal(1000)
    weights: tuple[tuple[str, Decimal], ...] = (("bot", Decimal("0.5")), ("copy", Decimal("0.5")))
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    agents: AgentConfig = field(default_factory=AgentConfig)
    copy: CopyConfig = field(default_factory=CopyConfig)
    rebalance: RebalanceConfig = field(default_factory=RebalanceConfig)
    mark_max_age: timedelta = timedelta(hours=24)  # copied prices aren't kept longer (D11)
    # A live decision made this soon after its candle closed fills at the next open, as a bot reacting that
    # fast would roughly get. A later one fills at the first open after the decision, never at an older price.
    fill_grace: timedelta = timedelta(seconds=60)

    def __post_init__(self) -> None:
        w = dict(self.weights)
        if set(w) != set(SLEEVES) or sum(w.values()) != 1 or min(w.values()) < self.rebalance.floor:
            raise ValueError(f"weights must cover {SLEEVES}, add up to 1 and each be at least {self.rebalance.floor}")
        if self.broker.round_trip_cost_pct > self.strategy.round_trip_cost_pct:
            raise ValueError("the strategy assumes lower trading costs than the broker charges")
        if self.risk.fee_pct < self.broker.fee_pct or self.risk.slippage_buffer_pct < self.broker.slippage_pct:
            raise ValueError("the risk cash check assumes lower costs than the broker charges")

    @property
    def exits(self) -> dict[str, ExitPlan]:
        s = self.strategy
        return {"bot": ExitPlan(stop_loss_pct=s.stop_loss_pct, take_profit_pct=s.take_profit_pct,
                                trail_pct=s.trail_pct, max_hold=s.max_hold),
                "copy": self.copy.exits}

    @property
    def brokers(self) -> dict[str, BrokerConfig]:
        return {"bot": self.broker, "copy": self.copy.broker}

    def risk_for(self, sleeve: str) -> RiskConfig:
        return self.risk if sleeve == "bot" else self.copy.risk


@dataclass(frozen=True)
class CandleInputs:
    """Everything one step needs for one newly closed candle."""

    candle: Candle
    validation: ValidationReport
    safety: SafetyReport | None = None
    features: FeatureSnapshot | None = None  # None: no decision this candle
    watched: bool = True  # a bot pool; False for a copy position's token (no data flag, no research)


@dataclass
class StepResult:
    symbol: str
    candle_open_time: datetime
    skipped: bool = False
    desk_state: DeskState = DeskState.NORMAL
    decision: Decision | None = None
    verdict: RiskVerdict | None = None
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    snapshot: PortfolioSnapshot | None = None  # the bot sleeve's
    notes: list[str] = field(default_factory=list)


@dataclass
class CopyStep:
    trade: TraderTrade
    action: str = "ignored"  # bought, sold, selling (from the next open), skipped, ignored
    reasons: list[str] = field(default_factory=list)
    symbol: str | None = None
    verdict: RiskVerdict | None = None
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)


class PaperDesk:
    def __init__(self, store: DeskStore, config: DeskConfig | None = None,
                 agents: tuple[Agent, ...] = DEFAULT_AGENTS, now: datetime | None = None):
        self.store = store
        self.config = config or DeskConfig()
        self.agents = agents
        self.broker = PaperBroker(self.config.brokers, self.config.exits)
        self._load(now or datetime.now(timezone.utc))

    # ----------------------------------------------------------------- state

    def state_for(self, sleeve: str) -> DeskState:
        if self.flags["emergency"]:
            return DeskState.EMERGENCY_STOP
        if self.flags["paused"]:
            return DeskState.PAUSED
        if sleeve == "bot" and self.flags["degraded"]:
            return DeskState.DEGRADED
        return DeskState.NORMAL

    @property
    def state(self) -> DeskState:
        return self.state_for("bot")

    @property
    def ledger(self) -> Ledger:
        """The bot sleeve's ledger."""
        return self.ledgers["bot"]

    def copy_symbols(self) -> list[str]:
        """Pools the copy sleeve holds or has orders in: the live loop fetches their 1-minute candles."""
        held = set(self.ledgers["copy"].open_symbols())
        return sorted(held | {o.symbol for o in self.broker.open_orders() if o.sleeve == "copy"})

    def _load(self, now: datetime) -> None:
        s = self.store
        chain_ok, chain_msg = s.verify_chain()
        opened = s.records("desk_opened")
        problems: list[str] = [] if chain_ok else [f"audit log: {chain_msg}"]
        if opened:
            self.initial_cash = Decimal(opened[0]["initial_cash"])
            weights = {k: Decimal(v) for k, v in opened[0].get("weights", {}).items()}
            if set(weights) != set(SLEEVES):
                problems.append("this desk was made before the money was split into sleeves: start a new database")
                weights = dict(self.config.weights)
        else:
            if s.records():
                problems.append("audit log has records but no opening record")
            self.initial_cash = self.config.initial_cash
            weights = dict(self.config.weights)
        self.weights0 = weights
        capital = {sl: self.initial_cash * weights[sl] for sl in SLEEVES}
        empty_equity = {sl: {"peak": str(capital[sl]), "day": None, "day_start": str(capital[sl]),
                             "last": str(capital[sl])} for sl in SLEEVES}
        self.flags: dict[str, Any] = s.get_state("flags", {"paused": None, "emergency": None, "degraded": []})
        self.equity: dict[str, Any] = s.get_state("equity", {"peak": str(self.initial_cash), "sleeves": empty_equity})
        self.marks: dict[str, dict[str, str]] = s.get_state("marks", {})
        self.last_candle: dict[str, str] = s.get_state("last_candle", {})
        self.orders_day: dict[str, Any] = s.get_state("orders_day", {"day": None, "counts": {}})
        self.last_order_at: dict[str, str] = s.get_state("last_order_at", {})
        self.cursors: dict[str, str] = s.get_state("trader_cursors", {})
        self.trader_names: dict[str, str] = s.get_state("trader_names", {})
        self.copy_mints: dict[str, str] = s.get_state("copy_mints", {})  # pool symbol -> token mint
        self.rebalance_state: dict[str, Any] = s.get_state("rebalance", {"last_at": None, "equity_at_last": {}})
        guards = {g["symbol"]: PositionGuard.from_dict(g) for g in s.get_state("guards", [])}

        orders = s.orders()
        events: dict[str, list[Fill | Decimal]] = {sl: [] for sl in SLEEVES}
        for r in s.records_of("fill", "transfer"):
            if r["_kind"] == "fill":
                fill = Fill.model_validate(clean(r))
                order = orders.get(fill.order_id)
                if order is None:
                    problems.append(f"fill {fill.fill_id} has no order")
                    continue
                events[order.sleeve].append(fill)
            else:
                events[r["from"]].append(-Decimal(r["amount"]))
                events[r["to"]].append(Decimal(r["amount"]))
        self.ledgers: dict[str, Ledger] = {}
        for sl in SLEEVES:
            try:
                self.ledgers[sl] = Ledger.rebuild(capital[sl], events[sl])
            except AccountingError as err:
                problems.append(f"books ({sl}): {err}")
                self.ledgers[sl] = Ledger(capital[sl])
        snaps = s.records("snapshot")
        if snaps and not self._books_match(snaps[-1]):
            problems.append("books rebuilt from the fills don't match the last stored snapshot")

        notes = []
        fills = [f for sl in SLEEVES for f in events[sl] if isinstance(f, Fill)]
        for sl in SLEEVES:
            for sym in self.ledgers[sl].open_symbols():
                if sym not in guards:  # lost working state: protect the position again from its entry
                    guards[sym] = self._rebuild_guard(sym, sl, fills, orders)
                    notes.append(f"protective exits for {sym} ({sl}) rebuilt from its entry fill")
        self.broker.restore([o for o in orders.values() if o.is_open], list(guards.values()))
        self._drop_stale_marks(now)

        if not opened or problems or notes:
            with s.transaction():
                if not opened and not problems:
                    s.append("desk_opened", new_id("desk"), now, {
                        "initial_cash": str(self.initial_cash), "weights": {k: str(v) for k, v in weights.items()},
                        "strategy": self.config.strategy.version, "copy": self.config.copy.version,
                        "risk": self.config.risk.version, "agents": self.config.agents.version})
                for n in notes:
                    self._event("guard_rebuilt", now, n)
                if problems and not self.flags["emergency"]:
                    self._emergency("; ".join(problems), now, [])
                self._save_state()

    def _books_match(self, snap: dict[str, Any]) -> bool:
        sleeves = snap.get("sleeves")
        if not isinstance(sleeves, dict) or set(sleeves) != set(SLEEVES):
            return False
        return all(Decimal(sleeves[sl]["cash"]) == self.ledgers[sl].cash
                   and tuple(Position.model_validate(p) for p in sleeves[sl]["positions"]) == self.ledgers[sl].positions()
                   for sl in SLEEVES)

    def _rebuild_guard(self, sym: str, sleeve: str, fills: list[Fill], orders: dict[str, Order]) -> PositionGuard:
        held, entry = Decimal(0), None
        for f in sorted(fills, key=lambda f: f.filled_at):
            order = orders.get(f.order_id)
            if f.symbol != sym or order is None or order.sleeve != sleeve:
                continue
            if f.side is Side.BUY and held == 0:
                entry = f
            held += f.quantity if f.side is Side.BUY else -f.quantity
        assert entry is not None  # an open position always has a first buy
        e = self.config.exits[sleeve]
        order = orders.get(entry.order_id)
        return PositionGuard(symbol=sym, decision_id=order.decision_id if order else "unknown",
                             opened_at=entry.candle_open_time, entry_price=entry.price,
                             levels=ProtectiveLevels.from_entry(entry.price, e.stop_loss_pct, e.take_profit_pct,
                                                                e.trail_pct),
                             max_hold=e.max_hold, sleeve=sleeve)

    def _save_state(self) -> None:
        held = {sym for led in self.ledgers.values() for sym in led.open_symbols()}
        held |= {o.symbol for o in self.broker.open_orders()}
        self.copy_mints = {sym: mint for sym, mint in self.copy_mints.items() if sym in held}
        s = self.store
        s.put_state("flags", self.flags)
        s.put_state("equity", self.equity)
        s.put_state("marks", self.marks)
        s.put_state("last_candle", self.last_candle)
        s.put_state("orders_day", self.orders_day)
        s.put_state("last_order_at", self.last_order_at)
        s.put_state("trader_cursors", self.cursors)
        s.put_state("trader_names", self.trader_names)
        s.put_state("copy_mints", self.copy_mints)
        s.put_state("rebalance", self.rebalance_state)
        s.put_state("guards", [g.to_dict() for g in self.broker.guards()])

    def _drop_stale_marks(self, now: datetime) -> None:
        limit = now - self.config.mark_max_age
        self.marks = {sym: m for sym, m in self.marks.items() if datetime.fromisoformat(m["at"]) >= limit}

    # -------------------------------------------------------------- recording

    def _event(self, name: str, at: datetime, detail: str) -> None:
        self.store.append("desk_event", new_id("ev"), at, {"event": name, "detail": detail,
                                                           "state": self.state.value})

    def _record(self, orders: list[Order], fills: list[Fill]) -> None:
        for o in orders:
            self.store.append("order", o.order_id, o.updated_at, o, symbol=o.symbol)
        for f in fills:
            self.store.append("fill", f.fill_id, f.filled_at, f, symbol=f.symbol)

    def _emergency(self, reason: str, now: datetime, orders_out: list[Order]) -> None:
        self.flags["emergency"] = reason
        canceled = self.broker.cancel(now, "emergency stop")
        self._record(canceled, [])
        orders_out += canceled
        self._event("emergency_stop", now, reason)

    def _change_state(self, before: DeskState, at: datetime, why: str) -> None:
        if self.state is not before:
            self._event("state_change", at, f"{before.value} -> {self.state.value}: {why}")

    def _settle(self, res: BrokerResult, at: datetime, orders_out: list[Order], fills_out: list[Fill]) -> None:
        """Apply a broker result to the sleeves' ledgers and record it. Broken books → EMERGENCY_STOP."""
        sleeve_of = {o.order_id: o.sleeve for o in res.orders}
        applied: list[Fill] = []
        try:
            for f in res.fills:
                self.ledgers[sleeve_of.get(f.order_id, "bot")].apply(f)
                applied.append(f)
        except AccountingError as err:
            self._record(res.orders, applied)
            orders_out += res.orders
            fills_out += applied
            self._emergency(str(err), at, orders_out)
            return
        self._record(res.orders, res.fills)
        orders_out += res.orders
        fills_out += res.fills

    # ------------------------------------------------------------------ steps

    def on_candle(self, inp: CandleInputs, now: datetime) -> StepResult:
        c = inp.candle
        sym = c.symbol
        if not c.closed:
            raise ValueError("only closed candles enter the desk")
        if inp.features is not None and (inp.features.symbol != sym or inp.features.as_of != c.close_time):
            raise ValueError("features don't belong to this candle")
        res = StepResult(symbol=sym, candle_open_time=c.open_time)
        last = self.last_candle.get(sym)
        if last is not None and c.open_time <= datetime.fromisoformat(last):
            res.skipped, res.desk_state = True, self.state
            return res

        with self.store.transaction():
            if inp.watched:
                before = self.state
                self._set_degraded(sym, inp.validation.status is not DataStatus.OK)
                self._change_state(before, c.open_time, f"data for {sym} is {inp.validation.status.value}")

            before = self.state
            sleeve = self._owner(sym)
            led = self.ledgers[sleeve]
            br = self.broker.process_candle(c, led.cash, led.quantity(sym), self.state_for(sleeve))
            res.notes += br.notes
            self._settle(br, c.close_time, res.orders, res.fills)
            for o in br.orders:
                if o.reason is not OrderReason.SIGNAL:  # an exit starts the cooldown too
                    self._note_order(o.sleeve, sym, o.created_at, count=False)

            self.marks[sym] = {"price": str(c.close), "at": c.close_time.isoformat()}
            self._drop_stale_marks(now)
            snaps, total, notes = self._snapshots(c.close_time)
            res.snapshot = snaps["bot"]
            res.notes += notes
            if inp.watched or br.fills:
                self._record_snapshot(snaps, total, c.close_time)
            self._track_equity(snaps, total, c.close_time)
            self._change_state(before, c.close_time, "after the candle's fills")

            if inp.watched and inp.features is not None and inp.safety is not None \
                    and self.state is not DeskState.EMERGENCY_STOP:
                if self.broker.pending_exit(sym):
                    res.notes.append("exit pending: no new decision")
                elif self._owner(sym) != "bot":
                    res.notes.append("the copy sleeve holds this token: no bot decision")
                else:
                    self._research(inp, snaps["bot"], now, res)

            self._maybe_rebalance(now)
            self.store.purge_details(now)
            self.last_candle[sym] = c.open_time.isoformat()
            self._save_state()
        res.desk_state = self.state
        return res

    def _research(self, inp: CandleInputs, snap: PortfolioSnapshot, now: datetime, res: StepResult) -> None:
        c, cfg = inp.candle, self.config
        x = AgentInputs(inp.features, inp.validation, inp.safety)
        outputs = run_agents(x, self.agents, cfg.agents)
        decision = decide(x, outputs, now, c.close, self._open_position(c.symbol, c), cfg.strategy)
        res.decision = decision
        self.store.append("decision", decision.decision_id, now, slim_decision(decision), symbol=c.symbol)
        self.store.save_decision_details(decision)
        intent = authorize(decision, snap, c.close, now, cfg.sizing)
        if intent is None:
            return
        verdict = evaluate_risk(intent, self._risk_context("bot", snap, now, inp.validation.status,
                                                           now - inp.features.as_of, inp.safety.tradable, c.close),
                                cfg.risk)
        res.verdict = verdict
        self.store.append("intent", intent.intent_id, now, intent, symbol=c.symbol)
        self.store.append("risk_verdict", verdict.verdict_id, now, verdict, symbol=c.symbol)
        if verdict.approved:
            not_before = c.close_time if now - c.close_time <= cfg.fill_grace else now
            order = self.broker.submit(intent, verdict, c.close, not_before=not_before, now=now)
            self._record([order], [])
            res.orders.append(order)
            self._note_order("bot", c.symbol, now, count=True)

    def _risk_context(self, sleeve: str, snap: PortfolioSnapshot, now: datetime, data_status: DataStatus,
                      data_age: timedelta, tradable: bool, price: Decimal) -> RiskContext:
        e = self.equity["sleeves"][sleeve]
        return RiskContext(
            portfolio=snap, desk_state=self.state_for(sleeve), peak_equity=Decimal(e["peak"]),
            day_start_equity=Decimal(e["day_start"]), data_status=data_status, data_age=max(data_age, timedelta(0)),
            token_tradable=tradable, reference_price=price, now=now, orders_today=self._orders_today(sleeve, now),
            last_order_at={s: datetime.fromisoformat(t) for s, t in self.last_order_at.items()},
            open_order_symbols=frozenset(o.symbol for o in self.broker.open_orders()))

    def _open_position(self, sym: str, candle: Candle) -> OpenPosition | None:
        qty = self.ledgers["bot"].quantity(sym)
        if qty == 0:
            return None
        g = self.broker.guard(sym)
        if g is not None:
            return OpenPosition(entry_price=g.entry_price, opened_at=g.opened_at, quantity=qty)
        pos = next(p for p in self.ledgers["bot"].positions() if p.symbol == sym)
        return OpenPosition(entry_price=pos.avg_entry_price, opened_at=candle.open_time, quantity=qty)

    def _owner(self, sym: str) -> str:
        """The sleeve that holds the token, or has an order in it ("bot" when nobody does)."""
        for sleeve, led in self.ledgers.items():
            if led.quantity(sym) > 0:
                return sleeve
        orders = self.broker.open_orders(sym)
        return orders[0].sleeve if orders else "bot"

    def _snapshots(self, at: datetime) -> tuple[dict[str, PortfolioSnapshot], Decimal, list[str]]:
        prices = {sym: Decimal(m["price"]) for sym, m in self.marks.items()}
        notes, snaps = [], {}
        for sleeve, led in self.ledgers.items():
            marks = dict(prices)
            for p in led.positions():
                if p.symbol not in marks:
                    marks[p.symbol] = p.avg_entry_price
                    notes.append(f"no recent price for {p.symbol}: valued at its entry price")
            snaps[sleeve] = led.snapshot(at, marks)
        return snaps, sum((s.equity for s in snaps.values()), ZERO), notes

    def _record_snapshot(self, snaps: dict[str, PortfolioSnapshot], total: Decimal, at: datetime) -> None:
        self.store.append("snapshot", new_id("snap"), at, {
            "as_of": at.isoformat(), "equity": str(total),
            "sleeves": {sl: snap.model_dump(mode="json", exclude={"marks"}) for sl, snap in snaps.items()}})

    def _track_equity(self, snaps: dict[str, PortfolioSnapshot], total: Decimal, at: datetime) -> None:
        day = at.date().isoformat()  # UTC day
        for sleeve, snap in snaps.items():
            e = self.equity["sleeves"][sleeve]
            if e["day"] != day:
                e["day"], e["day_start"] = day, e["last"]
            e["last"] = str(snap.equity)
            e["peak"] = str(max(Decimal(e["peak"]), snap.equity))
        peak = max(Decimal(self.equity["peak"]), total)
        self.equity["peak"] = str(peak)
        drawdown = (peak - total) / peak * 100
        if drawdown > self.config.risk.max_drawdown_pct and not self.flags["paused"]:
            self.flags["paused"] = f"drawdown {drawdown:.2f}% from peak (limit {self.config.risk.max_drawdown_pct}%)"
            self._event("paused", at, self.flags["paused"])

    def _set_degraded(self, sym: str, bad: bool) -> None:
        current = set(self.flags["degraded"])
        current.add(sym) if bad else current.discard(sym)
        self.flags["degraded"] = sorted(current)

    def _orders_today(self, sleeve: str, now: datetime) -> int:
        if self.orders_day["day"] != now.date().isoformat():
            return 0
        return self.orders_day["counts"].get(sleeve, 0)

    def _note_order(self, sleeve: str, sym: str, at: datetime, count: bool) -> None:
        prev = self.last_order_at.get(sym)
        if prev is None or at > datetime.fromisoformat(prev):
            self.last_order_at[sym] = at.isoformat()
        if count:
            day = at.date().isoformat()
            counts = self.orders_day["counts"] if self.orders_day["day"] == day else {}
            counts[sleeve] = counts.get(sleeve, 0) + 1
            self.orders_day = {"day": day, "counts": counts}

    # ------------------------------------------------------------- copy trades

    def on_trader_trade(self, trade: TraderTrade, now: datetime, market: CopyMarket | None = None,
                        market_note: str = "", exit_price: Decimal | None = None) -> CopyStep:
        """React to one trade of a followed trader. ``market`` (for a buy) and ``exit_price`` (for a sell)
        come from the live loop, which looks them up on the network."""
        step = CopyStep(trade=trade)
        with self.store.transaction():
            self.trader_names[trade.wallet] = trade.trader
            self.store.append("trader_trade", trade.signature, now, trade, symbol=market.symbol if market else None)
            if trade.side is Side.BUY:
                self._copy_buy(trade, market, market_note, now, step)
            else:
                self._copy_sell(trade, exit_price, now, step)
            if step.fills:  # the books changed: store them, so a restart can check itself against them
                snaps, total, _ = self._snapshots(now)
                self._record_snapshot(snaps, total, now)
                self._track_equity(snaps, total, now)
            self.store.append("copy_decision", new_id("copy"), now, {
                "signature": trade.signature, "trader": trade.trader, "mint": trade.mint, "side": trade.side.value,
                "action": step.action, "reasons": step.reasons, "symbol": step.symbol})
            self._save_state()
        return step

    def copy_precheck(self, trade: TraderTrade) -> list[str]:
        """Reasons not to copy a buy that need no market data (the live loop then skips the lookups)."""
        cfg = self.config.copy
        state = self.state_for("copy")
        reasons = []
        if state is not DeskState.NORMAL:
            reasons.append(f"desk is {state.value}: no new entries")
        minimum = cfg.min_buy_for(trade.quote)
        if trade.quote_amount < minimum:
            reasons.append(f"too small to copy ({trade.quote_amount:.4f} {trade.quote}, minimum {minimum})")
        if trade.mint in self.copy_mints.values():
            reasons.append("already holding this token")
        open_copies = set(self.copy_symbols())
        if len(open_copies) >= cfg.max_positions:
            reasons.append(f"already {len(open_copies)} copy positions (maximum {cfg.max_positions})")
        return reasons

    def copy_position_for(self, trade: TraderTrade) -> str | None:
        """The pool of our copy position in the token this trade is about, if we hold one."""
        led = self.ledgers["copy"]
        return next((s for s, m in self.copy_mints.items() if m == trade.mint and led.quantity(s) > 0), None)

    def _copy_buy(self, trade: TraderTrade, market: CopyMarket | None, market_note: str, now: datetime,
                  step: CopyStep) -> None:
        cfg = self.config.copy
        reasons = self.copy_precheck(trade)
        if market is not None and "already holding this token" not in reasons and (
                any(led.quantity(market.symbol) > 0 for led in self.ledgers.values())
                or self.broker.open_orders(market.symbol)):
            reasons.append("already holding this token")
        if market is None:
            reasons.append(market_note or "no market data")
        elif not market.safety.tradable:
            failed = [c.name for c in market.safety.checks if c.status is not SafetyStatus.PASS]
            reasons.append("failed safety checks: " + ", ".join(failed))
        if reasons:
            step.action, step.reasons = "skipped", reasons
            return

        assert market is not None
        step.symbol = market.symbol
        now = max(now, market.priced_at)  # the fill can't be earlier than the price it uses
        snaps, _, _ = self._snapshots(now)
        snap = snaps["copy"]
        budget = min(snap.equity * cfg.position_pct / 100, snap.cash) * (1 - cfg.cost_buffer_pct / 100)
        qty = (budget / market.price_usd).quantize(cfg.broker.quantity_step, rounding=ROUND_DOWN)
        if qty <= 0:
            step.action, step.reasons = "skipped", ["no cash left in the copy sleeve"]
            return
        intent = OrderIntent(decision_id=f"copy_{trade.signature[:24]}", symbol=market.symbol, side=Side.BUY,
                             order_type=OrderType.MARKET, quantity=qty, created_at=now, sleeve="copy")
        verdict = evaluate_risk(intent, self._risk_context("copy", snap, now, DataStatus.OK, now - market.priced_at,
                                                           market.safety.tradable, market.price_usd), cfg.risk)
        step.verdict = verdict
        self.store.append("intent", intent.intent_id, now, intent, symbol=market.symbol)
        self.store.append("risk_verdict", verdict.verdict_id, now, verdict, symbol=market.symbol)
        if not verdict.approved:
            step.action = "skipped"
            step.reasons = ["risk: " + ", ".join(c.name for c in verdict.checks if not c.passed)]
            return
        order = self.broker.submit(intent, verdict, market.price_usd, not_before=now, now=now, copied_from=trade.wallet)
        self._record([order], [])
        step.orders.append(order)
        led = self.ledgers["copy"]
        self._settle(self.broker.fill_now(order, market.price_usd, now, led.cash, led.quantity(market.symbol)),
                     now, step.orders, step.fills)
        self.copy_mints[market.symbol] = trade.mint
        self.marks[market.symbol] = {"price": str(market.price_usd), "at": now.isoformat()}
        self._note_order("copy", market.symbol, now, count=True)
        step.action = "bought" if step.fills else "skipped"
        if not step.fills:
            step.reasons = [step.orders[-1].detail or "nothing filled"]

    def _copy_sell(self, trade: TraderTrade, price: Decimal | None, now: datetime, step: CopyStep) -> None:
        led = self.ledgers["copy"]
        sym = self.copy_position_for(trade)
        if sym is None:
            step.reasons = ["we don't hold this token"]
            return
        step.symbol = sym
        guard = self.broker.guard(sym)
        if not self.config.copy.follow_sells:
            step.reasons = ["following sells is switched off"]
        elif guard is None or guard.copied_from != trade.wallet:
            step.reasons = ["this position copied another trader"]
        elif self.broker.pending_exit(sym):
            step.reasons = ["already selling"]
        else:
            self._settle(self.broker.exit_now(sym, OrderReason.TRADER_EXIT, price, now, led.cash, led.quantity(sym)),
                         now, step.orders, step.fills)
            self._note_order("copy", sym, now, count=False)
            step.action = "sold" if step.fills else "selling"
            if not step.fills:
                step.reasons = ["no current price: sells at the next candle's open"]

    def save_cursors(self, cursors: dict[str, str], names: dict[str, str] | None = None) -> None:
        """Remember where each followed wallet was read up to (and the names the user gave them)."""
        with self.store.transaction():
            self.cursors = dict(cursors)
            self.trader_names.update(names or {})
            self._save_state()

    # -------------------------------------------------------------- rebalance

    def _maybe_rebalance(self, now: datetime) -> None:
        cfg, st = self.config.rebalance, self.rebalance_state
        if st["last_at"] is not None and now - datetime.fromisoformat(st["last_at"]) < cfg.every:
            return
        snaps, total, _ = self._snapshots(now)
        if st["last_at"] is None:
            self.rebalance_state = {"last_at": now.isoformat(),
                                    "equity_at_last": {sl: str(snaps[sl].equity) for sl in SLEEVES}}
            return
        since = datetime.fromisoformat(st["last_at"])
        trades = self.closed_trades(since)
        if any(len(trades[sl]) < cfg.min_trades for sl in SLEEVES):
            return
        returns = {sl: snaps[sl].equity / Decimal(st["equity_at_last"][sl]) - 1
                   if Decimal(st["equity_at_last"][sl]) > 0 else ZERO for sl in SLEEVES}
        winner, loser = sorted(SLEEVES, key=lambda sl: returns[sl], reverse=True)
        amount = ZERO
        if returns[winner] > returns[loser] and total > 0:
            share = snaps[loser].equity / total
            amount = min((share - max(cfg.floor, share - cfg.step)) * total, self.ledgers[loser].cash)
            amount = max(ZERO, amount).quantize(MONEY_STEP, rounding=ROUND_DOWN)
        if amount > 0:
            self.ledgers[loser].move_capital(-amount)
            self.ledgers[winner].move_capital(amount)
            for sleeve, delta in ((loser, -amount), (winner, amount)):
                e = self.equity["sleeves"][sleeve]
                for key in ("peak", "day_start", "last"):
                    e[key] = str(Decimal(e[key]) + delta)
            detail = (f"{amount} moved from {loser} ({returns[loser] * 100:+.2f}%) to {winner} "
                      f"({returns[winner] * 100:+.2f}%) after {len(trades[loser])}/{len(trades[winner])} trades")
            self.store.append("transfer", new_id("xfer"), now, {"from": loser, "to": winner, "amount": str(amount),
                                                                "reason": detail})
            self._event("rebalanced", now, detail)
        snaps, total, _ = self._snapshots(now)
        if amount > 0:
            self._record_snapshot(snaps, total, now)
        self.rebalance_state = {"last_at": now.isoformat(),
                                "equity_at_last": {sl: str(snaps[sl].equity) for sl in SLEEVES}}

    def closed_trades(self, since: datetime | None = None) -> dict[str, list]:
        """Closed round trips per sleeve (closed at or after ``since``)."""
        orders = self.store.orders()
        fills = self.store.fills()
        out = {}
        for sl in SLEEVES:
            own = [f for f in fills if (o := orders.get(f.order_id)) is not None and o.sleeve == sl]
            out[sl] = [t for t in round_trips(own, orders) if t.closed_at and (since is None or t.closed_at >= since)]
        return out

    # --------------------------------------------------------------- operator

    def note_data(self, symbol: str, status: DataStatus, now: datetime, detail: str = "") -> None:
        """Record a data problem when no new candle arrived (for example the provider failed)."""
        with self.store.transaction():
            before = self.state
            self._set_degraded(symbol, status is not DataStatus.OK)
            self._change_state(before, now, f"data for {symbol} is {status.value}" + (f": {detail}" if detail else ""))
            self._save_state()

    def pause(self, reason: str, now: datetime) -> None:
        with self.store.transaction():
            self.flags["paused"] = reason or "paused by the operator"
            self._record(self.broker.cancel(now, "desk paused: no new entries", side=Side.BUY), [])
            self._event("paused", now, self.flags["paused"])
            self._save_state()

    def resume(self, now: datetime) -> None:
        """Clear a pause. The drawdown peaks restart from current equity, so it doesn't pause again at once."""
        with self.store.transaction():
            self.flags["paused"] = None
            self.equity["peak"] = str(sum((Decimal(e["last"]) for e in self.equity["sleeves"].values()), ZERO))
            for e in self.equity["sleeves"].values():
                e["peak"] = e["last"]
            self._event("resumed", now, f"peak reset to {self.equity['peak']}")
            self._save_state()

    def kill(self, reason: str, now: datetime) -> None:
        with self.store.transaction():
            self._emergency(reason or "kill switch", now, [])
            self._save_state()

    def reset(self, now: datetime) -> list[str]:
        """Leave EMERGENCY_STOP if the audit log and the books check out. Returns the problems found, if any."""
        problems = self.check()
        if problems:
            return problems
        with self.store.transaction():
            self.flags["emergency"] = None
            self._event("reset", now, "audit log and books checked")
            self._save_state()
        return []

    def check(self) -> list[str]:
        """Integrity check: the audit log chain, and each sleeve's books rebuilt from its fills and transfers."""
        problems = []
        ok, msg = self.store.verify_chain()
        if not ok:
            problems.append(f"audit log: {msg}")
        orders = self.store.orders()
        events: dict[str, list[Fill | Decimal]] = {sl: [] for sl in SLEEVES}
        for r in self.store.records_of("fill", "transfer"):
            if r["_kind"] == "fill":
                fill = Fill.model_validate(clean(r))
                order = orders.get(fill.order_id)
                if order is None:
                    return problems + [f"fill {fill.fill_id} has no order"]
                events[order.sleeve].append(fill)
            else:
                events[r["from"]].append(-Decimal(r["amount"]))
                events[r["to"]].append(Decimal(r["amount"]))
        for sl in SLEEVES:
            try:
                rebuilt = Ledger.rebuild(self.initial_cash * self.weights0[sl], events[sl])
            except AccountingError as err:
                return problems + [f"books ({sl}): {err}"]
            if rebuilt.cash != self.ledgers[sl].cash or rebuilt.positions() != self.ledgers[sl].positions():
                problems.append(f"books in memory ({sl}) differ from the fills")
        snaps = self.store.records("snapshot")
        if snaps and not self._books_match(snaps[-1]):
            problems.append("books don't match the last stored snapshot")
        return problems

    # ---------------------------------------------------------------- reading

    def status(self) -> dict[str, Any]:
        snaps, total, notes = self._snapshots(datetime.now(timezone.utc))
        orders = self.store.orders()
        fills = self.store.fills()
        sleeves = {}
        for sl, snap in snaps.items():
            led = self.ledgers[sl]
            own = [f for f in fills if (o := orders.get(f.order_id)) is not None and o.sleeve == sl]
            sleeves[sl] = {
                "capital": led.initial_cash, "cash": led.cash, "equity": snap.equity,
                "share": snap.equity / total if total else ZERO, "fees_paid": led.fees_paid,
                "realized": led.realized_total,
                "positions": [{"symbol": p.symbol, "quantity": p.quantity, "avg_entry_price": p.avg_entry_price,
                               "mark": snap.marks.get(p.symbol), "guard": self.broker.guard(p.symbol)}
                              for p in snap.positions],
                "trades": round_trips(own, orders),
            }
        return {
            "state": self.state.value, "flags": self.flags, "initial_cash": self.initial_cash, "equity": total,
            "peak": Decimal(self.equity["peak"]), "sleeves": sleeves, "open_orders": self.broker.open_orders(),
            "traders": {w: self.trader_names.get(w, w[:6]) for w in self.cursors}, "notes": notes,
            # the bot sleeve, as before the split
            "cash": self.ledgers["bot"].cash, "fees_paid": self.ledgers["bot"].fees_paid,
            "realized": self.ledgers["bot"].realized_total, "trades": sleeves["bot"]["trades"],
            "positions": sleeves["bot"]["positions"],
            "day_start": Decimal(self.equity["sleeves"]["bot"]["day_start"]),
        }
