"""Turn one Solana transaction into a trade by a followed wallet, or ``None`` if it isn't one.

Works from balance changes only, so it doesn't care which DEX or trading bot was used (pump.fun curve,
PumpSwap, Raydium, Jupiter, Meteora, ...):

- The wallet must have **signed** the transaction. Busy wallets appear in many other people's
  transactions (dust spam, bots); those are not their trades.
- The transaction must have succeeded.
- Quote side: SOL (native and wrapped), USDC or USDT; a stablecoin wins over SOL, whose change may be just
  the network fee. Exactly one other token must change in the wallet.
- BUY: that token went up and a quote went down. SELL: the token went down and a quote went up.
  Anything else (airdrop, transfer, multi-token route) is not a trade.
Amounts are exact: raw integer amounts and decimals, never the rounded ``uiAmount``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from scheme1.contracts import Side, TraderTrade

WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}
LAMPORTS = Decimal(10) ** 9


def _token_deltas(meta: dict[str, Any], wallet: str) -> dict[str, Decimal]:
    delta: dict[str, Decimal] = defaultdict(Decimal)
    for sign, key in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for b in meta.get(key) or []:
            if b.get("owner") != wallet:
                continue
            amount = b["uiTokenAmount"]
            delta[b["mint"]] += sign * Decimal(amount["amount"]) / Decimal(10) ** int(amount["decimals"])
    return {mint: d for mint, d in delta.items() if d != 0}


def parse_trade(tx: dict[str, Any] | None, wallet: str, trader: str) -> TraderTrade | None:
    if not tx or not tx.get("meta") or tx["meta"].get("err") is not None or tx.get("blockTime") is None:
        return None
    meta, message = tx["meta"], tx["transaction"]["message"]
    keys = message["accountKeys"]
    index = next((i for i, k in enumerate(keys) if k.get("pubkey") == wallet), None)
    if index is None or not keys[index].get("signer"):
        return None  # mentioned in someone else's transaction

    deltas = _token_deltas(meta, wallet)
    sol = Decimal(meta["postBalances"][index] - meta["preBalances"][index]) / LAMPORTS + deltas.pop(WSOL, Decimal(0))
    quotes = {"SOL": sol, **{name: deltas.pop(mint, Decimal(0)) for mint, name in STABLES.items()}}
    if len(deltas) != 1:
        return None
    (mint, change), = deltas.items()
    if change > 0:
        paid = [(q, -d) for q, d in quotes.items() if d < 0]
        side = Side.BUY
    else:
        paid = [(q, d) for q, d in quotes.items() if d > 0]
        side = Side.SELL
    if not paid:
        return None  # tokens moved without a matching payment: a transfer or an airdrop
    # Every transaction costs a little SOL in fees, so when a stablecoin moved, that was the real payment.
    quote, quote_amount = max(paid, key=lambda qa: (qa[0] != "SOL", qa[1]))
    return TraderTrade(trader=trader, wallet=wallet, signature=tx["transaction"]["signatures"][0], slot=tx["slot"],
                       block_time=datetime.fromtimestamp(tx["blockTime"], timezone.utc), mint=mint, side=side,
                       token_amount=abs(change), quote=quote, quote_amount=quote_amount)
