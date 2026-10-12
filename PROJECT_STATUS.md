# Project Status

_Last updated: 2026-10-12 (Stage 4b live check)_

| # | Stage | Status |
| --- | --- | --- |
| 1 | Repository research and feasibility | **Done**: [docs/STAGE1_AUDIT.md](docs/STAGE1_AUDIT.md) |
| 2 | Architecture and interfaces | **Done (proposal)**: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); contracts in `src/scheme1/contracts.py` |
| 3 | Data pipeline | **Done**: [docs/STAGE3_DATA.md](docs/STAGE3_DATA.md); code in `src/scheme1/data/` |
| 4 | Deterministic quantitative logic | **Done**: [docs/STAGE4_FEATURES.md](docs/STAGE4_FEATURES.md); features in `src/scheme1/features/`, token safety in `src/scheme1/safety/` |
| 4b | On-chain token checks | **Verified live, 3 of 4 checks**: mint authority, freeze authority and extensions pass for BONK; holder concentration needs a non-public RPC (see below) |
| 5 | Research agents | Not started |
| 6 | Decision and risk engines | Not started |
| 7 | Paper execution and accounting | Not started |
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

Result on 2026-10-12, Python 3.13.16: **108 passed, 0 failed, 0 skipped** (14 contract, 44 data-pipeline, 12 feature, 15 safety, 23 on-chain tests). No test uses the network.

Live check on 2026-10-12 from the cloud session: `python -m scheme1.data.cli --pool 5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9` ingested 299 closed 5m candles and 999 closed 1m candles of BONK/SOL, status ok, no gaps.

## Missing access and limitations

- **GeckoTerminal and DexScreener APIs work from the cloud session** (verified 2026-10-12).
- **Terms read on 2026-10-12** (text supplied by the user): GeckoTerminal Website Terms, CoinGecko API Terms of Service and DEX Screener Terms. Result: personal local use, 24-hour retention of GeckoTerminal data (now enforced in code), "Powered by CoinGecko" attribution, no redistribution. See DECISIONS.md D11 and docs/STAGE3_DATA.md section 1a. The DexScreener API terms page (if any) is unread.
- **Recommended:** make this repository private. It's public; it no longer contains provider data (the one real response remains in git history at commit 7be4292), but the docs describe CoinGecko's API, and API Terms 4.1.7.6 restrict public statements about it.
- **Recommended:** keep `desk.db` on a disk with full-disk encryption (API Terms 6.1.2 asks for strong encryption of stored data).
- GeckoTerminal's free limit is about 10 calls/minute (a 429 was observed). That caps how many pools can be watched.
- **Centralized-exchange APIs are blocked in the cloud session.** `api.binance.com`, `data-api.binance.vision`, `data.binance.vision`, `api.exchange.coinbase.com`, `api.kraken.com`, `api.bybit.com` and `www.okx.com` all got 403 from the environment proxy. PyPI and GitHub work. Real-data steps must run on your PC, or the environment must allow the chosen host.
- **Solana RPC (`api.mainnet-beta.solana.com`) is reachable from the cloud session** (verified 2026-10-12). Live run of `python -m scheme1.safety.cli --pool 5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9`: mint authority revoked, freeze authority revoked, `spl-token` with no extensions, matching the parser exactly. Other public RPCs tried (publicnode, ankr, drpc) are blocked by the proxy.
- **The public endpoint refuses `getTokenLargestAccounts`** with HTTP 429 on every call, for every token tried (BONK, WIF), even after waiting. So `holder_concentration` stays unverified and **every token is still blocked**. Fix: set `SCHEME1_SOLANA_RPC_URL` to a free Helius or QuickNode URL (and allow that host in the network settings if running here). A failed holder lookup no longer discards the mint checks. Each run waits about 3 minutes on the 429 retries.
- No LLM provider is configured. None is needed until Stage 5 phase 2.
- No code from the five audited repositories has been installed or executed.

## Open questions

See section 5 of [docs/STAGE1_AUDIT.md](docs/STAGE1_AUDIT.md).

## Scope change (2026-10-12)

Target is now **Solana memecoins, paper trading only** (see DECISIONS.md D9). Stage 3 verified both DEX-data endpoints and chose GeckoTerminal for candles (D10). DexScreener has no candle endpoint.

## Future (not scheduled)

Live trading through a dedicated bot wallet is documented as an idea with entry gates in [docs/future/LIVE_TRADING.md](docs/future/LIVE_TRADING.md). It is out of scope until those gates are met.

## Next stage

1. **Finish 4b live: holder concentration.** Needs an RPC that serves `getTokenLargestAccounts` (Helius or QuickNode free tier via `SCHEME1_SOLANA_RPC_URL`). Then rerun the BONK check and fix any parsing differences in the holder and owner responses.
2. **Stage 5: research agents.** Deterministic market, technical, data-quality, bull, bear and risk agents that read a `FeatureSnapshot`, a `ValidationReport` and a `SafetyReport`, and cite feature keys as evidence.
