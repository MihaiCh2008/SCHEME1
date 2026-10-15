"""Run the research agents on one pool, with live data.

    python -m scheme1.agents.cli --pool <solana pool address> --interval 5m

Candles are kept in memory only (nothing is written to disk). Research output only: it places no order.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from scheme1.agents import AgentInputs, run_agents
from scheme1.contracts import Interval
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
    finally:
        store.close()
    if snapshot is None:
        print(f"no closed candles: {result.error or result.report.status.value}")
        return 1
    safety = check_pool(symbol)

    print(f"Market data: GeckoTerminal, DexScreener, Solana RPC. {ATTRIBUTION}")
    print(f"{symbol} {interval.value} as_of={snapshot.as_of.isoformat()} data={result.report.status.value} "
          f"tradable={safety.tradable}")
    for out in run_agents(AgentInputs(snapshot, result.report, safety)):
        print(f"\n[{out.role}] {out.status.value} {out.stance.value} score={out.score:+.2f}"
              + (f"  error: {out.error}" if out.error else ""))
        for e in out.evidence:
            print(f"  - {e.kind.value:14s} {e.statement}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
