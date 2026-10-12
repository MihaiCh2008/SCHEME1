# Future stage: live trading (not started, not allowed yet)

**Status:** idea only. The current system is simulation-only (ARCHITECTURE.md "Hard rule", DECISIONS.md D9). This page records how live trading could work later and what must be true before anyone writes code for it. No live-trading code may be added to this repository until every gate below is met and the user has decided to go ahead.

## How it would work

Bots don't drive Axiom, FOMO or Phantom. They trade on Solana directly:

1. A **dedicated bot wallet**: a new keypair holding only money you can afford to lose completely. You can import it into Phantom to watch it.
2. **Swaps built by the program**, usually through Jupiter's swap API (which routes across Raydium, Orca, Meteora and pump.fun pools).
3. **Transactions sent through an RPC provider** (Helius, QuickNode, ...), often via Jito bundles to reduce front-running.

In this codebase, it would be a `LiveBroker` that takes the place of the paper broker after the risk engine. Decision, risk and accounting stay the same, so paper and live results mean the same thing.

Not options:
- **Axiom / FOMO:** no official API. Using them means impersonating your logged-in session, which is fragile, probably against their terms and a custody risk (D9).
- **Phantom:** a human approves every transaction; it can't sign for a bot.

## Gates (all must be met first)

| # | Gate | Evidence needed |
| --- | --- | --- |
| 1 | The strategy has an edge **after realistic costs** | Weeks to months of live paper trading, plus backtests that charge for AMM price impact, priority fees, failed transactions, front-running and signal-to-execution delay |
| 2 | Token-safety checks are **verified, not assumed** | Mint/freeze authority, holder concentration, liquidity and pool age all come from working data sources. A token missing any of them stays blocked. |
| 3 | Hard limits in code, outside any agent | Max per trade, max per day, a daily loss stop, a total loss stop and a kill switch, all tested |
| 4 | Key security | Bot key never in the repo or logs; encrypted at rest; on a machine you control; the wallet holds only the risk budget |
| 5 | Provider terms allow it | Check whether the data provider's terms allow trading decisions from its API, and whether the 24-hour data rule (D11) is compatible with live operation |
| 6 | Legal | You are 18 or older, it is legal where you live, and you report taxes on gains |
| 7 | Staged rollout | Start with a tiny budget; scale only after live results match paper results |
