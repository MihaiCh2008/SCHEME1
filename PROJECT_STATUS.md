# Project Status

_Last updated: 2026-10-12_

| # | Stage | Status |
| --- | --- | --- |
| 1 | Repository research and feasibility | **Done**: [docs/STAGE1_AUDIT.md](docs/STAGE1_AUDIT.md) |
| 2 | Architecture and interfaces | **Done (proposal)**: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); contracts in `src/scheme1/contracts.py` |
| 3 | Data pipeline | Not started |
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

Result on 2026-10-12, Python 3.13.16: **14 passed, 0 failed, 0 skipped** (`tests/test_contracts.py`).

## Missing access and limitations

- **Exchange APIs are blocked in the cloud session.** `api.binance.com`, `data-api.binance.vision`, `data.binance.vision`, `api.exchange.coinbase.com`, `api.kraken.com`, `api.bybit.com` and `www.okx.com` all got 403 from the environment proxy. PyPI and GitHub work. Real-data steps must run on your PC, or the environment must allow the chosen host.
- No LLM provider is configured. None is needed until Stage 5 phase 2.
- No code from the five audited repositories has been installed or executed.

## Open questions

See section 5 of [docs/STAGE1_AUDIT.md](docs/STAGE1_AUDIT.md).

## Next stage

Stage 3: market-data pipeline. The provider interface, a fixture provider (offline, labelled test data), a ccxt provider (tests use mocked responses for timeouts and rate limits), the validator, raw/clean storage, and freshness checks.
