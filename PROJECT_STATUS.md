# Project Status

_Last updated: 2026-10-12 (Stage 3)_

| # | Stage | Status |
| --- | --- | --- |
| 1 | Repository research and feasibility | **Done**: [docs/STAGE1_AUDIT.md](docs/STAGE1_AUDIT.md) |
| 2 | Architecture and interfaces | **Done (proposal)**: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); contracts in `src/scheme1/contracts.py` |
| 3 | Data pipeline | **Done**: [docs/STAGE3_DATA.md](docs/STAGE3_DATA.md); code in `src/scheme1/data/` |
| 4 | Deterministic quantitative logic | Not started |
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

Result on 2026-10-12, Python 3.13.16: **55 passed, 0 failed, 0 skipped** (14 contract tests, 41 data-pipeline tests). No test uses the network.

Live check on 2026-10-12 from the cloud session: `python -m scheme1.data.cli --pool 5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9` ingested 299 closed 5m candles and 999 closed 1m candles of BONK/SOL, status ok, no gaps.

## Missing access and limitations

- **GeckoTerminal and DexScreener APIs work from the cloud session** (verified 2026-10-12). Their website terms were read on 2026-10-12 (text supplied by the user): personal, non-commercial use; no redistribution of GeckoTerminal content; no excessive load. See DECISIONS.md D11.
- **Still unread: the CoinGecko API Terms of Service** (coingecko.com/en/api_terms), which govern GeckoTerminal API use and data storage. Please paste them the same way. The DexScreener API terms page is also unread.
- This repository is public. No provider data is committed (the one real response was replaced with synthetic data), but it remains in git history at commit 7be4292.
- GeckoTerminal's free limit is about 10 calls/minute (a 429 was observed). That caps how many pools can be watched.
- **Centralized-exchange APIs are blocked in the cloud session.** `api.binance.com`, `data-api.binance.vision`, `data.binance.vision`, `api.exchange.coinbase.com`, `api.kraken.com`, `api.bybit.com` and `www.okx.com` all got 403 from the environment proxy. PyPI and GitHub work. Real-data steps must run on your PC, or the environment must allow the chosen host.
- No LLM provider is configured. None is needed until Stage 5 phase 2.
- No code from the five audited repositories has been installed or executed.

## Open questions

See section 5 of [docs/STAGE1_AUDIT.md](docs/STAGE1_AUDIT.md).

## Scope change (2026-10-12)

Target is now **Solana memecoins, paper trading only** (see DECISIONS.md D9). Stage 3 verified both DEX-data endpoints and chose GeckoTerminal for candles (D10). DexScreener has no candle endpoint.

## Next stage

Stage 4: deterministic quantitative logic. Build the feature engine on `clean_candles` (returns, volatility, volume and liquidity measures suited to memecoins, plus RSI/EMA-style indicators), with the `FeatureSnapshot.as_of` = last closed candle rule and warm-up handling. Test it for no look-ahead, and add a token-safety data step: pool liquidity and age from GeckoTerminal, cross-checked against DexScreener because the two disagree. A token stays blocked until both checks are verified.
