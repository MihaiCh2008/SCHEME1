# Project Status

_Last updated: 2026-11-17 (Stage 7)_

| # | Stage | Status |
| --- | --- | --- |
| 1 | Repository research and feasibility | **Done**: [docs/STAGE1_AUDIT.md](docs/STAGE1_AUDIT.md) |
| 2 | Architecture and interfaces | **Done (proposal)**: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); contracts in `src/scheme1/contracts.py` |
| 3 | Data pipeline | **Done**: [docs/STAGE3_DATA.md](docs/STAGE3_DATA.md); code in `src/scheme1/data/` |
| 4 | Deterministic quantitative logic | **Done**: [docs/STAGE4_FEATURES.md](docs/STAGE4_FEATURES.md); features in `src/scheme1/features/`, token safety in `src/scheme1/safety/` |
| 4b | On-chain token checks | **Verified live, all 4 checks run** (Helius free plan). BONK fails holder concentration and stays blocked by choice (D13) |
| 5 | Research agents | **Phase 1 done** (deterministic): [docs/STAGE5_AGENTS.md](docs/STAGE5_AGENTS.md); code in `src/scheme1/agents/`. Phase 2 (optional LLM) not started |
| 6 | Decision and risk engines | **Done**: [docs/STAGE6_DECISION_RISK.md](docs/STAGE6_DECISION_RISK.md); code in `src/scheme1/decision/` and `src/scheme1/risk/`. Quick-trade strategy with take profit, stop loss and time limit (D15); trailing stop checked inside each candle (D16, D17). Safety review 2026-11-17: exits no longer blocked by bad data |
| 7 | Paper execution and accounting | **Done**: [docs/STAGE7_PAPER.md](docs/STAGE7_PAPER.md); code in `src/scheme1/broker/`, `src/scheme1/accounting/`, `src/scheme1/desk/`. Paper broker, ledger, desk states, hash-chained audit log, 5% daily loss limit (D18, D19). Run it with `python -m scheme1.desk.cli run --pool <address>` |
| 8 | Backtesting | Not started |
| 9 | End-to-end proof | Not started |
| 10 | Performance and reliability | Not started |
| 11 | Frontend | Not started (old JS prototype in `prototype/` uses synthetic data and is not part of this system) |
| 12 | Acceptance and documentation | Not started |

## Test results

Command (from the repo root, inside `.venv`):

```
python -m pytest
```

Result on 2026-11-17, Python 3.13.16: **234 passed, 0 failed, 0 skipped** (23 contract, 44 data-pipeline, 12 feature, 15 safety, 26 on-chain, 14 agent, 35 decision and risk, 18 protective-exit, 7 ledger, 17 broker, 9 audit-store, 12 desk, 2 desk-command tests). No test uses the network.

Live check on 2026-10-12 from the cloud session: `python -m scheme1.data.cli --pool 5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9` ingested 299 closed 5m candles and 999 closed 1m candles of BONK/SOL, status ok, no gaps.

Live check on 2026-10-15: `python -m scheme1.agents.cli --pool 5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9` ran all six agents on BONK/SOL 5m candles. All outputs OK and every citation valid; market and technical bearish, risk −1 (holder concentration fails, D13). Decision: HOLD; no order proposed.

Live check on 2026-11-17: `python -m scheme1.desk.cli run --pool 5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9 --steps 1` ran the full paper desk on BONK/SOL: HOLD (token failed safety checks), equity 1000.00. Safety checks on WIF, POPCAT, Fartcoin and MEW pools: all blocked by the holder rule (Fartcoin closest: top 10 wallets 31.3%). Found and fixed a wrong Token-2022 program id (see docs/STAGE7_PAPER.md section 7).

## Missing access and limitations

- **GeckoTerminal and DexScreener APIs work from the cloud session** (verified 2026-10-12).
- **Terms read on 2026-10-12** (text supplied by the user): GeckoTerminal Website Terms, CoinGecko API Terms of Service and DEX Screener Terms. Result: personal local use, 24-hour retention of GeckoTerminal data (now enforced in code), "Powered by CoinGecko" attribution, no redistribution. See DECISIONS.md D11 and docs/STAGE3_DATA.md section 1a. The DexScreener API terms page (if any) is unread.
- **Recommended:** make this repository private. It's public; it no longer contains provider data (the one real response remains in git history at commit 7be4292), but the docs describe CoinGecko's API, and API Terms 4.1.7.6 restrict public statements about it.
- **Recommended:** keep `desk.db` on a disk with full-disk encryption (API Terms 6.1.2 asks for strong encryption of stored data).
- GeckoTerminal's free limit is about 10 calls/minute (a 429 was observed). That caps how many pools can be watched.
- **Centralized-exchange APIs are blocked in the cloud session.** `api.binance.com`, `data-api.binance.vision`, `data.binance.vision`, `api.exchange.coinbase.com`, `api.kraken.com`, `api.bybit.com` and `www.okx.com` all got 403 from the environment proxy. PyPI and GitHub work. Real-data steps must run on your PC, or the environment must allow the chosen host.
- **Solana RPC (`api.mainnet-beta.solana.com`) is reachable from the cloud session** (verified 2026-10-12). Live run of `python -m scheme1.safety.cli --pool 5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9`: mint authority revoked, freeze authority revoked, `spl-token` with no extensions, matching the parser exactly. Other public RPCs tried (publicnode, ankr, drpc) are blocked by the proxy.
- **The public endpoint refuses `getTokenLargestAccounts`** with HTTP 429 on every call, for every token tried (BONK, WIF), even after waiting. So `holder_concentration` stays unverified and **every token is still blocked**. A failed holder lookup no longer discards the mint checks.
- **Helius free plan works** (1M credits/month, no card; verified 2026-10-12). The environment sets `SCHEME1_SOLANA_RPC_URL` to a Helius URL and allows `mainnet.helius-rpc.com`. A full BONK check takes about 5 seconds. Live result: mint authority revoked, freeze authority revoked, no extensions, **holder concentration FAIL**: largest wallet 8.83%, top 10 wallets 38.41% (limit 30%), program-controlled 0.99%. So BONK is blocked.
- No LLM provider is configured. None is needed until Stage 5 phase 2.
- No code from the five audited repositories has been installed or executed.

## Open questions

See section 5 of [docs/STAGE1_AUDIT.md](docs/STAGE1_AUDIT.md).

## Scope change (2026-10-12)

Target is now **Solana memecoins, paper trading only** (see DECISIONS.md D9). Stage 3 verified both DEX-data endpoints and chose GeckoTerminal for candles (D10). DexScreener has no candle endpoint.

## Future (not scheduled)

Live trading through a dedicated bot wallet is documented as an idea with entry gates in [docs/future/LIVE_TRADING.md](docs/future/LIVE_TRADING.md). It is out of scope until those gates are met.

## New direction from the user (2026-11-17), waiting for answers

Two strategies with separate money (proposed split 50/50, then shifting slowly to the better one after 30 trades each, each keeping at least 20%):
- **A. Bot trades on FOMO**: buy when a coin suddenly pumps with big volume (no price-jump limit, D20).
- **B. Copy traders** the user follows on the pump.fun app: when one of their wallets buys, the bot buys too (smaller size, exit when the trader sells or on a wider safety stop).

Waiting for: the traders' wallet addresses; whether new pump.fun coins (< 24 h) are allowed for copy trades (changes D15 and the 24-hour pool-age rule). The holder rule is decided (b, D21).

## Holder rule (decided 2026-11-17)

Option **b** (D21): known exchange wallets are left out of the holder check. Live result: **Fartcoin/SOL is now tradable** (top 10 wallets 27.3% after leaving out Gate.io and MEXC). BONK, WIF, POPCAT and MEW still fail on non-exchange wallets. In the first live desk pass on Fartcoin the agents were bullish (market +0.95), but the bot held because nobody had traded in that pool for 15 minutes (data stale): correct, no fresh price.

**REMINDER for the user (asked 2026-11-17): in Stage 8, test option c (top-10 limit 40%) against option b and keep whichever makes more money.**

## Next stage

1. **Stage 8: backtesting.** Must include the user's reminder: compare holder rule b (D21) with c (40%). Also compare the entry price-jump limit off / 2% / 5% (D20).
    Feed stored or fetched history through the same `PaperDesk.on_candle` step, one closed candle at a time, and report trades, equity curve and metrics (with `insufficient_sample` when there are too few trades). Open problem: there is no historical safety data, so a backtest must either assume a fixed safety report or skip that check, and say so.
2. Later, optional: Stage 5 phase 2 (an LLM agent behind the same contract). Needs a provider decision first.
