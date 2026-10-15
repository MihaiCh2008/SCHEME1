"""Run the research agents, the decision engine and the risk engine on one pool, with live data.

    python -m scheme1.agents.cli --pool <solana pool address> --interval 5m

Candles are kept in memory only (nothing is written to disk). The risk check uses an empty paper
portfolio of 1,000 (virtual). Nothing is bought or sold.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal

from scheme1.agents import AgentInputs, run_agents
from scheme1.contracts import DeskState, Interval, PortfolioSnapshot
from scheme1.decision import authorize, decide
from scheme1.risk import RiskContext, evaluate_risk
from scheme1.data.pipeline import ingest
from scheme1.data.providers.geckoterminal import ATTRIBUTION, GeckoTerminalProvider, pool_symbol
from scheme1.data.store import CandleStore
from scheme1.features.from_store import snapshot_at
from scheme1.safety.cli import check_pool


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pool", required=True, help="Solana pool address (not the token mint)")
    parser.add_argument("--interval", default="5m", choices=[i.value for i in Interval])
    args = parser.parse_args(argv)
    symbol, interval = pool_symbol("solana", args.pool), Interval(args.interval)

    now = datetime.now(timezone.utc)
    store = CandleStore(":memory:")
    try:
        result = ingest(GeckoTerminalProvider(), store, symbol, interval, now, limit=100)
        snapshot = snapshot_at(store, symbol, interval, now)
        last = store.latest_clean_before(symbol, interval, now)
    finally:
        store.close()
    if snapshot is None or last is None:
        print(f"no closed candles: {result.error or result.report.status.value}")
        return 1
    safety = check_pool(symbol)

    print(f"Market data: GeckoTerminal, DexScreener, Solana RPC. {ATTRIBUTION}")
    print(f"{symbol} {interval.value} as_of={snapshot.as_of.isoformat()} data={result.report.status.value} "
          f"tradable={safety.tradable}")
    x = AgentInputs(snapshot, result.report, safety)
    outputs = run_agents(x)
    for out in outputs:
        print(f"\n[{out.role}] {out.status.value} {out.stance.value} score={out.score:+.2f}"
              + (f"  error: {out.error}" if out.error else ""))
        for e in out.evidence:
            print(f"  - {e.kind.value:14s} {e.statement}")

    decision = decide(x, outputs, now, last.close)
    print(f"\nDECISION {decision.signal.value} (confidence {decision.confidence.value:.2f}, "
          f"{decision.confidence.kind}, last close {last.close})")
    print(f"  {decision.explanation}")
    cash = Decimal(1000)
    portfolio = PortfolioSnapshot(as_of=now, cash=cash, positions=(), marks={}, equity=cash, fees_paid=Decimal(0))
    intent = authorize(decision, portfolio, last.close, now)
    if intent is None:
        print("RISK not needed: no order proposed")
        return 0
    verdict = evaluate_risk(intent, RiskContext(
        portfolio=portfolio, desk_state=DeskState.NORMAL, peak_equity=cash, data_status=result.report.status,
        data_age=now - snapshot.as_of, token_tradable=safety.tradable, reference_price=last.close, now=now))
    print(f"RISK {'APPROVED' if verdict.approved else 'REJECTED'} {intent.side.value} {intent.quantity}")
    for c in verdict.checks:
        print(f"  {'pass' if c.passed else 'FAIL'} {c.name:22s} {c.observed}  ({c.limit})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
