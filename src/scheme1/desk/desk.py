"""The paper desk: one step per closed candle, from data to simulated fills (ARCHITECTURE.md section 1).

``PaperDesk.on_candle(inputs, now)`` runs, in one transaction:
1. Skip a candle that was already processed (the live loop may see one twice).
2. Data flag: the symbol's data status sets or clears DEGRADED.
3. Broker: resting orders, time limit and protective exits on this candle (``scheme1.broker.paper``).
4. Ledger: apply the fills. Books that don't balance → EMERGENCY_STOP.
5. Mark the symbol to the candle close, take a portfolio snapshot, track peak and day-start equity.
   Equity more than ``max_drawdown_pct`` below its peak → PAUSED.
6. Research, unless the desk is in EMERGENCY_STOP, an exit is already pending, or there are no features:
   agents → ``decide()`` → ``authorize()`` → ``evaluate_risk()`` → ``broker.submit()`` when approved.
   An order made here can first fill in the next candle.
7. Save the working state.

Desk states, strongest first:
- EMERGENCY_STOP: kill switch, broken books or a broken audit log. All orders are canceled and nothing
  trades. Only ``reset()`` clears it, and only when the log and the books check out.
- PAUSED: by the operator, or on the drawdown limit. Only ``resume()`` clears it.
- DEGRADED: automatic while any watched symbol's data is not OK; clears by itself.
- NORMAL.
Outside NORMAL there are no new entries, but exits keep working (D15).

A restart rebuilds the books from the fills in the audit log and compares them with the last stored
snapshot. Any difference → EMERGENCY_STOP (ARCHITECTURE.md section 7). Simulation only: nothing here
can reach a wallet or an exchange.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from scheme1.accounting import AccountingError, Ledger, round_trips
from scheme1.agents import DEFAULT_AGENTS, Agent, AgentConfig, AgentInputs, run_agents
from scheme1.broker import BrokerConfig, ExitPlan, PaperBroker, PositionGuard, ProtectiveLevels
from scheme1.contracts import (
    Candle, DataStatus, Decision, DeskState, FeatureSnapshot, Fill, Order, OrderReason, PortfolioSnapshot,
    Position, RiskVerdict, SafetyReport, Side, ValidationReport, new_id,
)
from scheme1.decision import OpenPosition, SizingConfig, StrategyConfig, authorize, decide
from scheme1.desk.store import DeskStore, slim_decision
from scheme1.risk import RiskConfig, RiskContext, evaluate_risk


@dataclass(frozen=True)
class DeskConfig:
    initial_cash: Decimal = Decimal(1000)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    agents: AgentConfig = field(default_factory=AgentConfig)
    mark_max_age: timedelta = timedelta(hours=24)  # copied prices aren't kept longer (D11)

    def __post_init__(self) -> None:
        if self.broker.round_trip_cost_pct > self.strategy.round_trip_cost_pct:
            raise ValueError("the strategy assumes lower trading costs than the broker charges")
        if self.risk.fee_pct < self.broker.fee_pct or self.risk.slippage_buffer_pct < self.broker.slippage_pct:
            raise ValueError("the risk cash check assumes lower costs than the broker charges")

    @property
    def exits(self) -> ExitPlan:
        s = self.strategy
        return ExitPlan(stop_loss_pct=s.stop_loss_pct, take_profit_pct=s.take_profit_pct, trail_pct=s.trail_pct,
                        max_hold=s.max_hold)


@dataclass(frozen=True)
class CandleInputs:
    """Everything one step needs for one newly closed candle."""

    candle: Candle
    validation: ValidationReport
    safety: SafetyReport
    features: FeatureSnapshot | None = None  # None: not computable, so no decision this candle


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
    snapshot: PortfolioSnapshot | None = None
    notes: list[str] = field(default_factory=list)


class PaperDesk:
    def __init__(self, store: DeskStore, config: DeskConfig | None = None,
                 agents: tuple[Agent, ...] = DEFAULT_AGENTS, now: datetime | None = None):
        self.store = store
        self.config = config or DeskConfig()
        self.agents = agents
        self.broker = PaperBroker(self.config.broker, self.config.exits)
        self._load(now or datetime.now(timezone.utc))

    # ----------------------------------------------------------------- state

    @property
    def state(self) -> DeskState:
        if self.flags["emergency"]:
            return DeskState.EMERGENCY_STOP
        if self.flags["paused"]:
            return DeskState.PAUSED
        if self.flags["degraded"]:
            return DeskState.DEGRADED
        return DeskState.NORMAL

    def _load(self, now: datetime) -> None:
        s = self.store
        chain_ok, chain_msg = s.verify_chain()
        opened = s.records("desk_opened")
        problems: list[str] = [] if chain_ok else [f"audit log: {chain_msg}"]
        if opened:
            self.initial_cash = Decimal(opened[0]["initial_cash"])
        else:
            if s.records():
                problems.append("audit log has records but no opening record")
            self.initial_cash = self.config.initial_cash
        start = str(self.initial_cash)
        self.flags: dict[str, Any] = s.get_state("flags", {"paused": None, "emergency": None, "degraded": []})
        self.equity: dict[str, Any] = s.get_state("equity", {"peak": start, "day": None, "day_start": start,
                                                             "last": start})
        self.marks: dict[str, dict[str, str]] = s.get_state("marks", {})
        self.last_candle: dict[str, str] = s.get_state("last_candle", {})
        self.orders_day: dict[str, Any] = s.get_state("orders_day", {"day": None, "count": 0})
        self.last_order_at: dict[str, str] = s.get_state("last_order_at", {})
        guards = {g["symbol"]: PositionGuard.from_dict(g) for g in s.get_state("guards", [])}

        fills = s.fills()
        try:
            self.ledger = Ledger.rebuild(self.initial_cash, fills)
        except AccountingError as err:
            problems.append(f"books: {err}")
            self.ledger = Ledger(self.initial_cash)
        snaps = s.records("snapshot")
        if snaps and not self._books_match(snaps[-1]):
            problems.append("books rebuilt from the fills don't match the last stored snapshot")

        notes = []
        orders = s.orders()
        for sym in self.ledger.open_symbols():
            if sym not in guards:  # lost working state: protect the position again from its entry
                guards[sym] = self._rebuild_guard(sym, fills, orders)
                notes.append(f"protective exits for {sym} rebuilt from its entry fill")
        self.broker.restore([o for o in orders.values() if o.is_open], list(guards.values()))
        self._drop_stale_marks(now)

        if not opened or problems or notes:
            with s.transaction():
                if not opened and not problems:
                    s.append("desk_opened", new_id("desk"), now, {
                        "initial_cash": start, "strategy": self.config.strategy.version,
                        "risk": self.config.risk.version, "agents": self.config.agents.version})
                for n in notes:
                    self._event("guard_rebuilt", now, n)
                if problems and not self.flags["emergency"]:
                    self._emergency("; ".join(problems), now, [])
                self._save_state()

    def _books_match(self, snap: dict[str, Any]) -> bool:
        positions = tuple(Position.model_validate(p) for p in snap["positions"])
        return Decimal(snap["cash"]) == self.ledger.cash and positions == self.ledger.positions()

    def _rebuild_guard(self, sym: str, fills: list[Fill], orders: dict[str, Order]) -> PositionGuard:
        held, entry = Decimal(0), None
        for f in fills:
            if f.symbol != sym:
                continue
            if f.side is Side.BUY and held == 0:
                entry = f
            held += f.quantity if f.side is Side.BUY else -f.quantity
        assert entry is not None  # an open position always has a first buy
        e = self.config.exits
        order = orders.get(entry.order_id)
        return PositionGuard(symbol=sym, decision_id=order.decision_id if order else "unknown",
                             opened_at=entry.candle_open_time, entry_price=entry.price,
                             levels=ProtectiveLevels.from_entry(entry.price, e.stop_loss_pct, e.take_profit_pct,
                                                                e.trail_pct),
                             max_hold=e.max_hold)

    def _save_state(self) -> None:
        s = self.store
        s.put_state("flags", self.flags)
        s.put_state("equity", self.equity)
        s.put_state("marks", self.marks)
        s.put_state("last_candle", self.last_candle)
        s.put_state("orders_day", self.orders_day)
        s.put_state("last_order_at", self.last_order_at)
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
            before = self.state
            self._set_degraded(sym, inp.validation.status is not DataStatus.OK)
            self._change_state(before, c.open_time, f"data for {sym} is {inp.validation.status.value}")

            before = self.state
            br = self.broker.process_candle(c, self.ledger.cash, self.ledger.quantity(sym), self.state)
            res.notes += br.notes
            applied: list[Fill] = []
            try:
                for f in br.fills:
                    self.ledger.apply(f)
                    applied.append(f)
            except AccountingError as err:
                self._record(br.orders, applied)
                res.orders += br.orders
                res.fills += applied
                self._emergency(str(err), c.close_time, res.orders)
            else:
                self._record(br.orders, br.fills)
                res.orders += br.orders
                res.fills += br.fills
            for o in br.orders:
                if o.reason is not OrderReason.SIGNAL:  # an exit starts the cooldown too
                    self._note_order(sym, o.created_at, count=False)

            self.marks[sym] = {"price": str(c.close), "at": c.close_time.isoformat()}
            self._drop_stale_marks(now)
            snap, notes = self._snapshot(c.close_time)
            res.snapshot = snap
            res.notes += notes
            self.store.append("snapshot", new_id("snap"), snap.as_of,
                              snap.model_dump(mode="json", exclude={"marks"}))
            self._track_equity(snap)
            self._change_state(before, c.close_time, "after the candle's fills")

            if self.state is not DeskState.EMERGENCY_STOP and inp.features is not None \
                    and not self.broker.pending_exit(sym):
                self._research(inp, snap, now, res)
            elif self.broker.pending_exit(sym):
                res.notes.append("exit pending: no new decision")

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
        verdict = evaluate_risk(intent, RiskContext(
            portfolio=snap, desk_state=self.state, peak_equity=Decimal(self.equity["peak"]),
            day_start_equity=Decimal(self.equity["day_start"]), data_status=inp.validation.status,
            data_age=now - inp.features.as_of, token_tradable=inp.safety.tradable, reference_price=c.close,
            now=now, orders_today=self._orders_today(now),
            last_order_at={s: datetime.fromisoformat(t) for s, t in self.last_order_at.items()},
            open_order_symbols=frozenset(o.symbol for o in self.broker.open_orders())), cfg.risk)
        res.verdict = verdict
        self.store.append("intent", intent.intent_id, now, intent, symbol=c.symbol)
        self.store.append("risk_verdict", verdict.verdict_id, now, verdict, symbol=c.symbol)
        if verdict.approved:
            order = self.broker.submit(intent, verdict, c.close, not_before=c.close_time, now=now)
            self._record([order], [])
            res.orders.append(order)
            self._note_order(c.symbol, now, count=True)

    def _open_position(self, sym: str, candle: Candle) -> OpenPosition | None:
        qty = self.ledger.quantity(sym)
        if qty == 0:
            return None
        g = self.broker.guard(sym)
        if g is not None:
            return OpenPosition(entry_price=g.entry_price, opened_at=g.opened_at, quantity=qty)
        pos = next(p for p in self.ledger.positions() if p.symbol == sym)
        return OpenPosition(entry_price=pos.avg_entry_price, opened_at=candle.open_time, quantity=qty)

    def _snapshot(self, at: datetime) -> tuple[PortfolioSnapshot, list[str]]:
        marks = {sym: Decimal(m["price"]) for sym, m in self.marks.items()}
        notes = []
        for p in self.ledger.positions():
            if p.symbol not in marks:
                marks[p.symbol] = p.avg_entry_price
                notes.append(f"no recent price for {p.symbol}: valued at its entry price")
        return self.ledger.snapshot(at, marks), notes

    def _track_equity(self, snap: PortfolioSnapshot) -> None:
        day = snap.as_of.date().isoformat()  # UTC day
        if self.equity["day"] != day:
            self.equity["day"], self.equity["day_start"] = day, self.equity["last"]
        self.equity["last"] = str(snap.equity)
        peak = max(Decimal(self.equity["peak"]), snap.equity)
        self.equity["peak"] = str(peak)
        drawdown = (peak - snap.equity) / peak * 100
        if drawdown > self.config.risk.max_drawdown_pct and not self.flags["paused"]:
            self.flags["paused"] = f"drawdown {drawdown:.2f}% from peak (limit {self.config.risk.max_drawdown_pct}%)"
            self._event("paused", snap.as_of, self.flags["paused"])

    def _set_degraded(self, sym: str, bad: bool) -> None:
        current = set(self.flags["degraded"])
        current.add(sym) if bad else current.discard(sym)
        self.flags["degraded"] = sorted(current)

    def _orders_today(self, now: datetime) -> int:
        return self.orders_day["count"] if self.orders_day["day"] == now.date().isoformat() else 0

    def _note_order(self, sym: str, at: datetime, count: bool) -> None:
        prev = self.last_order_at.get(sym)
        if prev is None or at > datetime.fromisoformat(prev):
            self.last_order_at[sym] = at.isoformat()
        if count:
            self.orders_day = {"day": at.date().isoformat(), "count": self._orders_today(at) + 1}

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
        """Clear a pause. The drawdown peak restarts from today's equity, so it doesn't pause again at once."""
        with self.store.transaction():
            self.flags["paused"] = None
            self.equity["peak"] = self.equity["last"]
            self._event("resumed", now, f"peak reset to {self.equity['last']}")
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
        """Integrity check: the audit log chain, and the books rebuilt from fills vs the last snapshot."""
        problems = []
        ok, msg = self.store.verify_chain()
        if not ok:
            problems.append(f"audit log: {msg}")
        try:
            rebuilt = Ledger.rebuild(self.initial_cash, self.store.fills())
        except AccountingError as err:
            return problems + [f"books: {err}"]
        snaps = self.store.records("snapshot")
        if rebuilt.cash != self.ledger.cash or rebuilt.positions() != self.ledger.positions():
            problems.append("books in memory differ from the fills")
        if snaps and not self._books_match(snaps[-1]):
            problems.append("books don't match the last stored snapshot")
        return problems

    # ---------------------------------------------------------------- reading

    def status(self) -> dict[str, Any]:
        snap, notes = self._snapshot(datetime.now(timezone.utc))
        orders = self.store.orders()
        return {
            "state": self.state.value, "flags": self.flags, "initial_cash": self.initial_cash,
            "cash": self.ledger.cash, "equity": snap.equity, "peak": Decimal(self.equity["peak"]),
            "day_start": Decimal(self.equity["day_start"]), "fees_paid": self.ledger.fees_paid,
            "realized": self.ledger.realized_total,
            "positions": [{"symbol": p.symbol, "quantity": p.quantity, "avg_entry_price": p.avg_entry_price,
                           "mark": snap.marks.get(p.symbol), "guard": self.broker.guard(p.symbol)}
                          for p in snap.positions],
            "open_orders": self.broker.open_orders(), "trades": round_trips(self.store.fills(), orders),
            "notes": notes,
        }
