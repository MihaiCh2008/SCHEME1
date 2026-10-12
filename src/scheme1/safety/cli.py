"""Token-safety report for one pool.

    python -m scheme1.safety.cli --pool <solana pool address>

Set SCHEME1_SOLANA_RPC_URL to use your own RPC provider (for example a free Helius or QuickNode URL).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from scheme1.contracts import PoolSnapshot, TokenOnchainInfo
from scheme1.data.providers.base import ProviderError
from scheme1.data.providers.geckoterminal import ATTRIBUTION, pool_symbol
from scheme1.safety.checks import evaluate
from scheme1.safety.pools import DexScreenerPools, GeckoTerminalPools
from scheme1.safety.solana import SolanaRpc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pool", required=True, help="Solana pool address (not the token mint)")
    args = parser.parse_args(argv)
    symbol = pool_symbol("solana", args.pool)

    snaps: list[PoolSnapshot] = []
    for source in (GeckoTerminalPools(), DexScreenerPools()):
        try:
            snaps.append(source.snapshot(symbol))
        except ProviderError as err:
            print(f"warning: {err}")
    onchain: TokenOnchainInfo | None = None
    mints = {s.base_token_mint for s in snaps if s.base_token_mint}
    if len(mints) == 1:
        try:
            onchain = SolanaRpc().token_info(mints.pop())
        except ProviderError as err:
            print(f"warning: {err}")

    report = evaluate(symbol, snaps, datetime.now(timezone.utc), onchain=onchain)
    print(f"Market data: GeckoTerminal, DexScreener, Solana RPC. {ATTRIBUTION}")
    print(f"pool {args.pool}: {'TRADABLE' if report.tradable else 'BLOCKED'}")
    for c in report.checks:
        print(f"  {c.status.value:10s} {c.name:26s} {c.observed}" + (f"  ({c.detail})" if c.detail else ""))
    return 0 if report.tradable else 1


if __name__ == "__main__":
    raise SystemExit(main())
