"""Run the paper desk on live data, and look at or control it. Simulation only: no wallet, no real orders.

    python -m scheme1.desk.cli run --pool <solana pool address> [--pool ...] [--interval 5m] [--steps N]
    python -m scheme1.desk.cli status
    python -m scheme1.desk.cli pause --reason "..." | resume | kill --reason "..." | reset | verify

``run`` waits for each candle to close, fetches the newest candles, checks token safety and runs one desk
step per new candle. Decisions are made on the newest candle only; candles missed while the desk was off
only run through the broker (resting orders and protective exits). A new desk starts from the newest
candle and never replays the past. Ctrl+C stops it safely: every step is saved whole or not at all.

Everything is stored in ``--db`` (default desk.db): candles (24 hours), the audit log and the desk state.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from scheme1.contracts import DataStatus, Interval, SafetyReport
from scheme1.data.pipeline import ingest
from scheme1.data.providers.base import MarketDataProvider
from scheme1.data.providers.geckoterminal import ATTRIBUTION, GeckoTerminalProvider, pool_symbol
from scheme1.data.store import CandleStore
from scheme1.desk.desk import CandleInputs, DeskConfig, PaperDesk, StepResult
from scheme1.desk.store import DeskStore
from scheme1.features import LOOKBACK
from scheme1.features.from_store import snapshot_at

_EPS = timedelta(microseconds=1)


def run_step(desk: PaperDesk, candles: CandleStore, provider: MarketDataProvider, symbols: list[str],
             interval: Interval, now: datetime, safety_for: Callable[[str], SafetyReport]) -> list[StepResult]:
    """One pass over all pools: fetch, then one desk step per new closed candle."""
    results: list[StepResult] = []
    for sym in symbols:
        fetched = ingest(provider, candles, sym, interval, now, limit=LOOKBACK + 16)
        report = fetched.report
        if report.status is DataStatus.UNAVAILABLE:
            desk.note_data(sym, report.status, now, fetched.error or "")
            continue
        last = desk.last_candle.get(sym)
        start = datetime.fromisoformat(last) + interval.duration if last else None
        new = candles.clean_candles(sym, interval, start=start, end=now - interval.duration + _EPS)
        if last is None:
            new = new[-1:]  # a new desk starts from the newest candle; it never replays the past
        if not new:
            if report.status is not DataStatus.OK:
                desk.note_data(sym, report.status, now)
            continue
        safety = safety_for(sym)
        for candle in new:
            features = snapshot_at(candles, sym, interval, candle.close_time) if candle is new[-1] else None
            results.append(desk.on_candle(CandleInputs(candle, report, safety, features), now))
    return results


def _short(symbol: str) -> str:
    address = symbol.split(":", 1)[-1]
    return f"{address[:4]}…{address[-4:]}"


def describe(r: StepResult) -> list[str]:
    head = f"{r.candle_open_time:%H:%M} UTC {_short(r.symbol)} [{r.desk_state.value}]"
    if r.skipped:
        return [f"{head} already done"]
    lines = []
    if r.decision is not None:
        d = r.decision
        why = d.rejection_reasons[0] if d.rejection_reasons else d.explanation.split(": ", 1)[-1]
        lines.append(f"{head} {d.signal.value} ({why})")
    else:
        lines.append(f"{head} no decision")
    if r.verdict is not None:
        failed = [c.name for c in r.verdict.checks if not c.passed]
        lines.append("  risk: approved" if r.verdict.approved else f"  risk: rejected ({', '.join(failed)})")
    for f in r.fills:
        lines.append(f"  {f.side.value} {f.quantity} at {f.price:.10g} (fee {f.fee})")
    lines += [f"  {n}" for n in r.notes]
    if r.snapshot is not None:
        lines.append(f"  equity {r.snapshot.equity:.2f}, cash {r.snapshot.cash:.2f}")
    return lines


def _wait_for_next_close(interval: Interval, delay_s: float) -> None:
    step = interval.duration.total_seconds()
    now = time.time()
    time.sleep(step - now % step + delay_s)


def cmd_run(args: argparse.Namespace) -> int:
    from scheme1.safety.cli import check_pool  # network: imported only when running

    interval = Interval(args.interval)
    symbols = [pool_symbol("solana", p) for p in args.pool]
    desk = PaperDesk(DeskStore(args.db), DeskConfig(initial_cash=Decimal(args.cash)))
    candles = CandleStore(args.db)
    provider = GeckoTerminalProvider()
    print(f"Paper desk (simulation only). Market data: GeckoTerminal, DexScreener, Solana RPC. {ATTRIBUTION}")
    print(f"state {desk.state.value}, start cash {desk.initial_cash}, watching {len(symbols)} pool(s), {interval.value}")
    done = 0
    try:
        while args.steps is None or done < args.steps:
            if done or args.wait_first:
                _wait_for_next_close(interval, args.delay)
            for r in run_step(desk, candles, provider, symbols, interval, datetime.now(timezone.utc), check_pool):
                print("\n".join(describe(r)))
            done += 1
    except KeyboardInterrupt:
        print("stopped; everything up to the last finished step is saved")
    finally:
        candles.close()
        desk.store.close()
    return 0


def _existing_desk(db: str) -> PaperDesk | None:
    """Open a desk that ``run`` already created; never create one here (that would fix the start cash)."""
    store = DeskStore(db)
    if not store.records("desk_opened"):
        store.close()
        print(f"no paper desk in {db} yet: start one with `run`")
        return None
    return PaperDesk(store)


def cmd_status(args: argparse.Namespace) -> int:
    desk = _existing_desk(args.db)
    if desk is None:
        return 1
    s = desk.status()
    start = s["initial_cash"]
    change = (s["equity"] / start - 1) * 100
    print(f"Paper desk (simulation only). {ATTRIBUTION}")
    print(f"state: {s['state']}" + "".join(f"\n  {k}: {v}" for k, v in s["flags"].items() if v))
    print(f"equity {s['equity']:.2f} ({change:+.2f}% since start {start}), cash {s['cash']:.2f}, "
          f"fees paid {s['fees_paid']:.2f}")
    print(f"peak {s['peak']:.2f}, start of today (UTC) {s['day_start']:.2f}")
    for p in s["positions"]:
        g = p["guard"]
        mark = f"{p['mark']:.10g}" if p["mark"] is not None else "no price"
        print(f"  holding {p['quantity']} {_short(p['symbol'])} bought at {p['avg_entry_price']:.10g} (now {mark})"
              + (f"; stop {g.levels.stop_price:.10g}, take profit {g.levels.take_profit_price:.10g}" if g else ""))
    for o in s["open_orders"]:
        print(f"  open order: {o.side.value} {o.remaining} {_short(o.symbol)} ({o.reason.value})")
    trades = s["trades"]
    closed = [t for t in trades if t.closed_at]
    wins = [t for t in closed if t.pnl > 0]
    print(f"trades: {len(closed)} closed ({len(wins)} won), {len(trades) - len(closed)} open")
    for t in closed[-10:]:
        print(f"  {t.opened_at:%m-%d %H:%M} → {t.closed_at:%H:%M} {_short(t.symbol)} {t.pnl:+.2f} "
              f"({t.pnl_pct:+.2f}%) {', '.join(t.exit_reasons)}")
    problems = desk.check()
    print("integrity: " + ("ok" if not problems else "; ".join(problems)))
    desk.store.close()
    return 0 if not problems else 1


def _control(args: argparse.Namespace) -> int:
    desk = _existing_desk(args.db)
    if desk is None:
        return 1
    now = datetime.now(timezone.utc)
    if args.command == "pause":
        desk.pause(args.reason, now)
    elif args.command == "resume":
        desk.resume(now)
    elif args.command == "kill":
        desk.kill(args.reason, now)
    elif args.command == "reset":
        problems = desk.reset(now)
        if problems:
            print("still stopped: " + "; ".join(problems))
            return 1
    elif args.command == "verify":
        problems = desk.check()
        print("ok" if not problems else "; ".join(problems))
        desk.store.close()
        return 0 if not problems else 1
    print(f"state: {desk.state.value}")
    desk.store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="desk.db")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the paper desk on live data")
    run.add_argument("--pool", action="append", required=True, help="Solana pool address (repeat for more)")
    run.add_argument("--interval", default="5m", choices=[i.value for i in Interval])
    run.add_argument("--cash", default="1000", help="virtual start cash in USD (only used by a new desk)")
    run.add_argument("--steps", type=int, default=None, help="stop after this many passes (default: run until Ctrl+C)")
    run.add_argument("--delay", type=float, default=20.0, help="seconds to wait after a candle closes")
    run.add_argument("--wait-first", action="store_true", help="wait for the next candle close before the first pass")
    sub.add_parser("status", help="show equity, positions, orders and trades")
    for name in ("pause", "kill"):
        p = sub.add_parser(name)
        p.add_argument("--reason", default="")
    for name in ("resume", "reset", "verify"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "status":
        return cmd_status(args)
    return _control(args)


if __name__ == "__main__":
    raise SystemExit(main())
