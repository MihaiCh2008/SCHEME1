"""Run the paper desk on live data, and look at or control it. Simulation only: no wallet, no real orders.

    python -m scheme1.desk.cli run [--pool <pool address> ...] [--traders copy_traders.txt] [--steps N]
    python -m scheme1.desk.cli status
    python -m scheme1.desk.cli pause --reason "..." | resume | kill --reason "..." | reset | verify

``run`` has two parts (D23):
- **Bot pools** (``--pool``): after each 5-minute candle closes, fetch candles, check token safety and run
  one desk step per new candle. Decisions are made on the newest candle only; candles missed while the
  desk was off only run through the broker. A new desk starts from the newest candle, never the past.
- **Copy trading** (``--traders``, one ``name address`` per line): every minute, read the followed
  wallets' new trades and copy them, then run the copy positions' 1-minute candles (trailing stop,
  24-hour limit). Trades from before the desk started are never copied.
Ctrl+C stops it safely: every step is saved whole or not at all.

All GeckoTerminal requests share one paced client (8 a minute). Everything is stored in ``--db``
(default desk.db): candles (24 hours), the audit log and the desk state.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from scheme1.contracts import DataStatus, Interval, SafetyReport, Side
from scheme1.copytrade import CopyMarket, PollResult, TraderWatcher, exit_price, load_traders, resolve_buy
from scheme1.data.pipeline import ingest
from scheme1.data.providers.base import MarketDataProvider
from scheme1.data.providers.geckoterminal import (
    ATTRIBUTION, GeckoTerminalProvider, geckoterminal_client, pool_symbol,
)
from scheme1.data.store import CandleStore
from scheme1.desk.desk import CandleInputs, CopyStep, DeskConfig, PaperDesk, StepResult
from scheme1.desk.store import DeskStore
from scheme1.features import LOOKBACK
from scheme1.features.from_store import snapshot_at

_EPS = timedelta(microseconds=1)
MINUTE = timedelta(minutes=1)


def run_step(desk: PaperDesk, candles: CandleStore, provider: MarketDataProvider, symbols: list[str],
             interval: Interval, now: datetime, safety_for: Callable[[str], SafetyReport]) -> list[StepResult]:
    """One pass over the bot pools: fetch, then one desk step per new closed candle."""
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


def copy_step(desk: PaperDesk, watcher: TraderWatcher | None, candles: CandleStore, provider: MarketDataProvider,
              resolve: Callable[[str], tuple[CopyMarket | None, str]], price_of: Callable[[str], Decimal | None],
              now: datetime) -> tuple[list[CopyStep], list[StepResult], list[str]]:
    """One copy pass: read the followed wallets and copy their trades, then run the copy positions' new
    1-minute candles."""
    copies: list[CopyStep] = []
    steps: list[StepResult] = []
    notes: list[str] = []
    if watcher is not None:
        polled: PollResult = watcher.poll(desk.cursors)
        notes += polled.notes
        for trade in sorted(polled.trades, key=lambda t: (t.block_time, t.slot)):
            if trade.side is Side.BUY:
                if desk.copy_precheck(trade):  # skipped anyway: don't spend data requests on it
                    copies.append(desk.on_trader_trade(trade, now, None, market_note="not looked up"))
                else:
                    market, note = resolve(trade.mint)
                    copies.append(desk.on_trader_trade(trade, now, market, market_note=note))
            else:
                sym = desk.copy_position_for(trade)
                copies.append(desk.on_trader_trade(trade, now, exit_price=price_of(sym) if sym else None))
        desk.save_cursors(polled.cursors, {t.wallet: t.name for t in getattr(watcher, "traders", [])})
    for sym in desk.copy_symbols():
        fetched = ingest(provider, candles, sym, Interval.M1, now, limit=15)
        if fetched.report.status is DataStatus.UNAVAILABLE:
            notes.append(f"{_short(sym)}: no candles ({fetched.error})")
            continue
        last = desk.last_candle.get(sym)
        guard = desk.broker.guard(sym)
        start = (datetime.fromisoformat(last) + MINUTE if last
                 else guard.opened_at.replace(second=0, microsecond=0) if guard else now - 2 * MINUTE)
        for candle in candles.clean_candles(sym, Interval.M1, start=start, end=now - MINUTE + _EPS):
            steps.append(desk.on_candle(CandleInputs(candle, fetched.report, watched=False), now))
    return copies, steps, notes


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
    elif not r.fills and not r.notes:
        return []  # a quiet minute of a copy position: nothing to say
    else:
        lines.append(f"{head}")
    if r.verdict is not None:
        failed = [c.name for c in r.verdict.checks if not c.passed]
        lines.append("  risk: approved" if r.verdict.approved else f"  risk: rejected ({', '.join(failed)})")
    for f in r.fills:
        lines.append(f"  {f.side.value} {f.quantity} at {f.price:.10g} (fee {f.fee})")
    lines += [f"  {n}" for n in r.notes]
    if r.snapshot is not None and r.decision is not None:
        lines.append(f"  bot sleeve: equity {r.snapshot.equity:.2f}, cash {r.snapshot.cash:.2f}")
    return lines


def describe_copy(c: CopyStep) -> list[str]:
    t = c.trade
    head = (f"{t.block_time:%H:%M} UTC {t.trader} {t.side.value} {t.quote_amount:.4g} {t.quote} of "
            f"{t.mint[:6]}… → {c.action}")
    lines = [head + (f" ({'; '.join(c.reasons)})" if c.reasons else "")]
    for f in c.fills:
        lines.append(f"  {f.side.value} {f.quantity} at {f.price:.10g} (fee {f.fee}) in {_short(f.symbol)}")
    return lines


def _print(lines: list[str]) -> None:
    if lines:
        print("\n".join(lines))


def _sleep_until_next(step: timedelta, delay_s: float) -> None:
    seconds = step.total_seconds()
    time.sleep(seconds - time.time() % seconds + delay_s)


def cmd_run(args: argparse.Namespace) -> int:
    from scheme1.safety.cli import check_pool  # network: imported only when running
    from scheme1.safety.pools import DexScreenerPools, GeckoTerminalPools
    from scheme1.safety.solana import SolanaRpc

    interval = Interval(args.interval)
    symbols = [pool_symbol("solana", p) for p in args.pool or []]
    traders = load_traders(args.traders) if args.traders else []
    if not symbols and not traders:
        print("nothing to do: give --pool and/or --traders")
        return 2
    desk = PaperDesk(DeskStore(args.db), DeskConfig(initial_cash=Decimal(args.cash)))
    candles = CandleStore(args.db)
    client = geckoterminal_client()
    provider, gecko, dex, rpc = GeckoTerminalProvider(client), GeckoTerminalPools(client), DexScreenerPools(), SolanaRpc()
    watcher = TraderWatcher(rpc, traders) if traders else None
    copy_safety = desk.config.copy.safety

    def resolve(mint: str) -> tuple[CopyMarket | None, str]:
        return resolve_buy(mint, gecko, dex, rpc, copy_safety, datetime.now(timezone.utc))

    print(f"Paper desk (simulation only). Market data: GeckoTerminal, DexScreener, Solana RPC. {ATTRIBUTION}")
    print(f"state {desk.state.value}, start cash {desk.initial_cash} (bot / copy: "
          + " / ".join(f"{led.initial_cash:.2f}" for led in desk.ledgers.values())
          + f"), {len(symbols)} bot pool(s) every {interval.value}, {len(traders)} trader(s) every minute")
    done, last_bot = 0, None
    tick = MINUTE if traders else interval.duration
    try:
        while args.steps is None or done < args.steps:
            if done or args.wait_first:
                _sleep_until_next(tick, args.delay)
            now = datetime.now(timezone.utc)
            if watcher is not None or desk.copy_symbols():
                copies, steps, notes = copy_step(desk, watcher, candles, provider, resolve,
                                                 lambda sym: exit_price(sym, dex, gecko), now)
                for line in notes:
                    print(f"  {line}")
                for c in copies:
                    print("\n".join(describe_copy(c)))
                for r in steps:
                    _print(describe(r))
            step_s = interval.duration.total_seconds()
            boundary = now.timestamp() // step_s  # which bot candle period we are in
            if symbols and boundary != last_bot:
                for r in run_step(desk, candles, provider, symbols, interval, now,
                                  lambda sym: check_pool(sym, gecko, dex, rpc)):
                    _print(describe(r))
                last_bot = boundary
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
    print(f"total equity {s['equity']:.2f} ({change:+.2f}% since start {start}), peak {s['peak']:.2f}")
    if s["traders"]:
        print("following: " + ", ".join(s["traders"].values()))
    for name, sl in s["sleeves"].items():
        trades = sl["trades"]
        closed = [t for t in trades if t.closed_at]
        wins = [t for t in closed if t.pnl > 0]
        print(f"\n[{name}] {sl['share'] * 100:.0f}% of the money: equity {sl['equity']:.2f} "
              f"(capital {sl['capital']:.2f}), cash {sl['cash']:.2f}, fees paid {sl['fees_paid']:.2f}; "
              f"trades: {len(closed)} closed ({len(wins)} won), {len(trades) - len(closed)} open")
        for p in sl["positions"]:
            g = p["guard"]
            mark = f"{p['mark']:.10g}" if p["mark"] is not None else "no price"
            tp = g.levels.take_profit_price if g else None
            exits = (f"; stop {g.levels.stop_price:.10g}" + (f", take profit {tp:.10g}" if tp else "")
                     + (f", copying {s['traders'].get(g.copied_from, g.copied_from[:6])}" if g.copied_from else "")
                     if g else "")
            print(f"  holding {p['quantity']} {_short(p['symbol'])} bought at {p['avg_entry_price']:.10g} "
                  f"(now {mark}){exits}")
        for t in closed[-10:]:
            print(f"  {t.opened_at:%m-%d %H:%M} → {t.closed_at:%H:%M} {_short(t.symbol)} {t.pnl:+.2f} "
                  f"({t.pnl_pct:+.2f}%) {', '.join(t.exit_reasons)}")
    for o in s["open_orders"]:
        print(f"open order: {o.side.value} {o.remaining} {_short(o.symbol)} ({o.reason.value}, {o.sleeve})")
    problems = desk.check()
    print("\nintegrity: " + ("ok" if not problems else "; ".join(problems)))
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
    run.add_argument("--pool", action="append", help="bot pool: Solana pool address (repeat for more)")
    run.add_argument("--traders", help="file with the wallets to copy, one 'name address' per line")
    run.add_argument("--interval", default="5m", choices=[i.value for i in Interval], help="bot pools' candles")
    run.add_argument("--cash", default="1000", help="virtual start cash in USD (only used by a new desk)")
    run.add_argument("--steps", type=int, default=None, help="stop after this many passes (default: run until Ctrl+C)")
    run.add_argument("--delay", type=float, default=20.0, help="seconds to wait after a candle closes")
    run.add_argument("--wait-first", action="store_true", help="wait for the next candle close before the first pass")
    sub.add_parser("status", help="show equity, sleeves, positions, orders and trades")
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
