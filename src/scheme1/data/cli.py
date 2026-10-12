"""Fetch candles for one pool into the local database.

    python -m scheme1.data.cli --pool <solana pool address> --interval 5m --db desk.db
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from scheme1.contracts import Interval
from scheme1.data.pipeline import ingest
from scheme1.data.providers.geckoterminal import GeckoTerminalProvider, pool_symbol
from scheme1.data.store import CandleStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pool", required=True, help="Solana pool address (not the token mint)")
    parser.add_argument("--interval", default="5m", choices=[i.value for i in Interval])
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--db", default="desk.db")
    args = parser.parse_args(argv)

    symbol = pool_symbol("solana", args.pool)
    store = CandleStore(args.db)
    try:
        result = ingest(GeckoTerminalProvider(), store, symbol, Interval(args.interval),
                        datetime.now(timezone.utc), limit=args.limit)
    finally:
        store.close()
    r = result.report
    print(f"status={r.status.value} candles_in={r.candles_in} clean={result.clean_count} revised={result.revised_count}")
    print(f"newest_closed_open_time={r.newest_closed_open_time.isoformat() if r.newest_closed_open_time else None}")
    for issue in r.issues:
        print(f"  {issue.code.value}: {issue.detail}")
    if result.error:
        print(f"error: {result.error}")
    return 0 if r.status.value == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
