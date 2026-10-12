# Stage 1: Repository Feasibility Audit

**Date:** 2026-10-12
**Method:** shallow clone (`git clone --depth 1`) of each repository's default branch, then reading the license, packaging metadata, source modules and docs. PyPI availability was checked with `pip index versions`. **No code from these repositories was installed or executed.** Statements below are limited to what those files show at the commits listed.

| Repository | Commit inspected | Commit date |
| --- | --- | --- |
| TauricResearch/TradingAgents | `1394a3f72aa4393e1a98f51b382434c4b4c2d972` | 2026-10-03 |
| OpenBB-finance/OpenBB | `ae0268771f761b036996d81bd21522ce79d95415` | 2026-10-02 |
| freqtrade/freqtrade | `f271f0113ca7ca1166ad951a71ddf47b51834740` | 2026-10-12 |
| nautechsystems/nautilus_trader | `01ced2a3e7b7d25ae7b2e5c0c077bcc46192eb25` | 2026-10-12 |
| OpenByteInc/QuantDinger | `ba8cd667acca3e934bbc1cb354922eff413393dc` | 2026-10-07 |

All five were accessible over public HTTPS. No credentials were needed to read them.

---

## 1. Comparison table

| | TradingAgents | OpenBB | Freqtrade | NautilusTrader | QuantDinger |
| --- | --- | --- | --- | --- | --- |
| **License** | Apache-2.0 | Apache-2.0 | **GPL-3.0** | **LGPL-3.0-only** (+ CLA for contributors) | Apache-2.0 code; **separate trademark policy** |
| **Version at HEAD** | 0.6.0 (not on PyPI) | `openbb` 5.0.0 / `openbb-core` 2.0.1 (on PyPI) | 2026.10-dev (PyPI stable 2026.9) | **2.0.0rc7** on `develop` (PyPI stable is 1.231.0, the v1 line) | 5.5.1 |
| **Python** | >=3.11 | >=3.10,<4 | >=3.12 | 2.x: 3.13–3.14 (Rust core, PyO3) | Flask app, Docker-first |
| **Core deps** | langchain-*, langgraph, pandas 3, yfinance, stockstats | openbb-core + ~25 provider/extension packages | ccxt 4.5.85, pandas 3, SQLAlchemy 2.1, ta-lib, FastAPI | Rust toolchain 1.99 for source builds; wheels on PyPI | Flask, PostgreSQL, Redis, Celery, (frontend as a prebuilt image) |
| **What it actually is** | LLM multi-agent research graph that outputs a 5-tier rating | Data platform with provider plugins | Complete crypto bot: data download, strategies, backtest, hyperopt, dry-run, live | Event-driven backtest and live engine | Complete self-hosted trading app |
| **Crypto market data** | yfinance `BTC-USD`-style tickers (daily-oriented) | In this repo, `CryptoHistorical` is implemented **only by the Nasdaq provider** (no interval parameter) | ccxt (100+ exchanges), OHLCV download | Adapters for Binance, Bybit, OKX, Kraken, Coinbase, etc. | Its own data_providers / data_sources |
| **Backtesting** | `backtest.py` scores ratings only; its docstring says it "is not a portfolio simulator" | None (data only) | Yes: candle-based, documented fill assumptions | Yes: event-driven with pluggable fill models | Yes |
| **Paper / simulation** | No order or cash model | No | `dry_run` mode with simulated wallet | Sandbox / backtest venues | Testnet and dry-run deviation services |
| **Integration point** | Python `TradingAgentsGraph.propagate(ticker, date, asset_type)` | Python `obb.*` / REST API | REST API + `freqtrade-client` (also GPL-3.0) | Python strategy/actor API | REST API (Flask) |
| **Infra needed** | LLM API key (OpenAI by default) + per-run token spend | None for keyless providers | SQLite or Postgres | Optional Postgres/Redis | Postgres + Redis + Celery workers |
| **Duplicates** | Our agent layer | Our data layer (and overlaps ccxt for crypto) | Data + backtest + paper broker (nearly everything) | Backtest + paper broker | **Everything**, including a UI |
| **Main concern** | Paid LLM per decision; prose-first output; stock-centric | Very heavy dependency tree for one crypto OHLCV endpoint | GPL-3.0: importing it makes our distributed code GPL | v2 is a release candidate mid-migration; v1 and v2 can't share an environment | Its AI gate ("Jev") is a **closed paid API that fails open** |
| **Decision** | **Reference** (agent roles, schemas). Optional adapter later | **Exclude** for now | **Reference** (fill assumptions). Optional external process later | **Reference** (fill-model concepts). Possible cross-check later | **Reference only** |

---

## 2. Evidence per repository

### 2.1 TradingAgents

- License: Apache-2.0, see [LICENSE](https://github.com/TauricResearch/TradingAgents/blob/1394a3f72aa4393e1a98f51b382434c4b4c2d972/LICENSE).
- Dependencies and Python >=3.11: [pyproject.toml](https://github.com/TauricResearch/TradingAgents/blob/1394a3f72aa4393e1a98f51b382434c4b4c2d972/pyproject.toml). Requires langchain-anthropic, -google-genai, -openai and langgraph.
- Default LLM provider is `"openai"`: [default_config.py](https://github.com/TauricResearch/TradingAgents/blob/1394a3f72aa4393e1a98f51b382434c4b4c2d972/tradingagents/default_config.py). Ollama is referenced in `llm_clients/openai_client.py` and `model_catalog.py`, so a local model is possible, but this was not tested.
- Entry point: `TradingAgentsGraph.propagate(company_name, trade_date, asset_type="stock"|"crypto")` returns `(final_state, signal)`, where signal is one of 5 ratings or `"REVIEW"` when unparseable. See [trading_graph.py](https://github.com/TauricResearch/TradingAgents/blob/1394a3f72aa4393e1a98f51b382434c4b4c2d972/tradingagents/graph/trading_graph.py).
- Structured output exists only for 3 agents (Research Manager, Trader, Portfolio Manager). The file says prose is still the primary artifact. See [agents/schemas.py](https://github.com/TauricResearch/TradingAgents/blob/1394a3f72aa4393e1a98f51b382434c4b4c2d972/tradingagents/agents/schemas.py).
- Agent roles: analysts (market, news, sentiment, fundamentals), bull/bear researchers, research manager, trader, 3 risk debators, portfolio manager. See [tradingagents/agents/](https://github.com/TauricResearch/TradingAgents/tree/1394a3f72aa4393e1a98f51b382434c4b4c2d972/tradingagents/agents).
- `backtest.py` explicitly refuses to be a portfolio simulator ("Turning a rating into a filled order needs a quantity, a fill price and a cash ledger, none of which the system has"). See [backtest.py](https://github.com/TauricResearch/TradingAgents/blob/1394a3f72aa4393e1a98f51b382434c4b4c2d972/tradingagents/backtest.py).
- **Decision: reference.** We copy its role design (bull/bear/manager) and its practice of validating LLM output with pydantic. We don't depend on it, because it needs a paid LLM per decision and its output is a rating, not the decision contract we need. An adapter can be added later behind our `ResearchAgent` interface once you choose an LLM provider.

### 2.2 OpenBB

- License: Apache-2.0, see [LICENSE](https://github.com/OpenBB-finance/OpenBB/blob/ae0268771f761b036996d81bd21522ce79d95415/LICENSE).
- `openbb` 5.0.0 pulls in ~25 packages, including charting, CLI and MCP server. See [openbb_platform/pyproject.toml](https://github.com/OpenBB-finance/OpenBB/blob/ae0268771f761b036996d81bd21522ce79d95415/openbb_platform/pyproject.toml).
- In this repository, the only `CryptoHistorical` fetcher is the Nasdaq one (`require_credentials = False`, no interval parameter). See [nasdaq/models/crypto_historical.py](https://github.com/OpenBB-finance/OpenBB/blob/ae0268771f761b036996d81bd21522ce79d95415/openbb_platform/providers/nasdaq/openbb_nasdaq/models/crypto_historical.py). The providers directory no longer contains yfinance, FMP or Polygon.
- **Decision: exclude for now.** For intraday crypto OHLCV it adds a large dependency tree and only offers one daily-oriented keyless source. ccxt covers this directly. It is worth revisiting for macro data (FRED, ECB, BLS providers) if the agents need it.

### 2.3 Freqtrade

- License: **GPL-3.0**, see [LICENSE](https://github.com/freqtrade/freqtrade/blob/f271f0113ca7ca1166ad951a71ddf47b51834740/LICENSE) and [pyproject.toml](https://github.com/freqtrade/freqtrade/blob/f271f0113ca7ca1166ad951a71ddf47b51834740/pyproject.toml) (`license = {text = "GPLv3"}`). The REST client `ft_client` is also GPLv3.
- Pinned dependencies include `ccxt==4.5.85`, `SQLAlchemy==2.1.3`, `ta-lib==0.8.1`, `fastapi==0.142.2`. See [requirements.txt](https://github.com/freqtrade/freqtrade/blob/f271f0113ca7ca1166ad951a71ddf47b51834740/requirements.txt). `ta-lib` needs a native C library.
- Documented backtest assumptions: entries at the open price, fills at the requested price with **no slippage** if it lies within the candle's high/low, exits at the next candle's open. See [docs/backtesting.md](https://github.com/freqtrade/freqtrade/blob/f271f0113ca7ca1166ad951a71ddf47b51834740/docs/backtesting.md) (around line 561).
- Dry-run handling lives in the exchange layer. See [exchange/exchange.py](https://github.com/freqtrade/freqtrade/blob/f271f0113ca7ca1166ad951a71ddf47b51834740/freqtrade/exchange/exchange.py).
- **Decision: reference.** Importing it would put our code under GPL-3.0 when distributed. It also duplicates the backtester and paper broker we need to control and audit ourselves. We'll use its documented fill assumptions as a baseline and add explicit slippage on top. Running it later as a **separate process** in dry-run mode (through its REST API) is legally clean if you want a second opinion.

### 2.4 NautilusTrader

- License: **LGPL-3.0-only**, see [LICENSE](https://github.com/nautechsystems/nautilus_trader/blob/01ced2a3e7b7d25ae7b2e5c0c077bcc46192eb25/LICENSE). Contributors sign a CLA ([CLA.md](https://github.com/nautechsystems/nautilus_trader/blob/01ced2a3e7b7d25ae7b2e5c0c077bcc46192eb25/CLA.md)).
- `develop` is **2.0.0rc7** (Rust core + PyO3), Python 3.13–3.14. See [python/pyproject.toml](https://github.com/nautechsystems/nautilus_trader/blob/01ced2a3e7b7d25ae7b2e5c0c077bcc46192eb25/python/pyproject.toml) and [README.md](https://github.com/nautechsystems/nautilus_trader/blob/01ced2a3e7b7d25ae7b2e5c0c077bcc46192eb25/README.md). PyPI's latest is 1.231.0 (v1). [MIGRATION_V2.md](https://github.com/nautechsystems/nautilus_trader/blob/01ced2a3e7b7d25ae7b2e5c0c077bcc46192eb25/MIGRATION_V2.md) says v1 and v2 must not share an environment and that v1 gets only security backports for about 3 months.
- Fill models are documented in [docs/concepts/backtesting/fill-models.md](https://github.com/nautechsystems/nautilus_trader/blob/01ced2a3e7b7d25ae7b2e5c0c077bcc46192eb25/docs/concepts/backtesting/fill-models.md).
- **Decision: reference.** It's an excellent engine, but it's mid-migration between major versions, and our first need (one-symbol candle backtest + paper broker with an audit trail) is small. Building on an RC would make our correctness depend on someone else's moving target. Possible future use: a cross-check backtest once v2 is stable.

### 2.5 QuantDinger

- Code license: Apache-2.0, see [LICENSE](https://github.com/OpenByteInc/QuantDinger/blob/ba8cd667acca3e934bbc1cb354922eff413393dc/LICENSE). Name and logo are restricted by [TRADEMARKS.md](https://github.com/OpenByteInc/QuantDinger/blob/ba8cd667acca3e934bbc1cb354922eff413393dc/TRADEMARKS.md).
- Requires PostgreSQL 18, Redis 8 and Celery workers. **The frontend is pulled as a prebuilt image** (`ghcr.io/openbyteinc/quantdinger-frontend`), not built from this repo. See [docker-compose.yml](https://github.com/OpenByteInc/QuantDinger/blob/ba8cd667acca3e934bbc1cb354922eff413393dc/docker-compose.yml) (lines 11–12 and 620–625).
- **About "Jev":** the README section "JEV-powered pre-trade decisions" shows that Jev is an external hosted API from TypeSafe (`docs.typesafe.ai`), called with a Bearer API key. See [README.md](https://github.com/OpenByteInc/QuantDinger/blob/ba8cd667acca3e934bbc1cb354922eff413393dc/README.md) and [ai_decision_filter.py](https://github.com/OpenByteInc/QuantDinger/blob/ba8cd667acca3e934bbc1cb354922eff413393dc/backend_api_python/app/services/ai_decision_filter.py). The module docstring reads "Fail-open AI decision filter for live entry orders". **If Jev and the LLM are unavailable, the order is allowed.**
- **Decision: reference only.** It duplicates the whole system and needs heavy infrastructure. Its gate is fail-open, which conflicts with requirement §4 ("stop any affected action that requires fresh data"). We'll take two ideas: the decision timeline UI, and keeping exits independent of the AI gate.

---

## 3. Environment findings (this cloud session)

| Check | Result |
| --- | --- |
| GitHub clone of all five repos | OK |
| PyPI (`pip index`, `pip download`) | OK |
| `api.binance.com`, `data-api.binance.vision`, `data.binance.vision` | **Blocked**: proxy returned 403 to CONNECT |
| `api.exchange.coinbase.com`, `api.kraken.com`, `api.bybit.com`, `www.okx.com` | **Blocked**: same 403 |
| Local Python | 3.13.16 |

So in this session, no live or historical exchange data can be fetched. Real-data stages must run on your machine, or after the environment's network settings allow the provider host.

## 4. Selected combination

The smallest maintainable set:

- **Our own Python core** for validation, features, decisions, risk, paper broker, backtest and storage. These parts must be deterministic, tested and auditable, so they shouldn't live inside a third-party framework.
- **ccxt** (MIT, verified from the wheel metadata of 4.5.85) as the first market-data adapter, behind our own `MarketDataProvider` interface. It's the same library Freqtrade uses.
- **No LLM dependency** in the first milestone. The research agents are deterministic. An LLM adapter (provider-configurable) comes later and is optional.
- Everything else is used as a design reference, with links above.

## 5. Unresolved questions (need your decision)

1. **Where should real data come from?** This cloud session can't reach any exchange. Options: (a) you run the data stages on your PC, or (b) you allow a host such as `api.binance.com` in this environment's network settings.
2. **Which exchange?** Binance's main API is unavailable in some countries (for example the US). Kraken and Coinbase are keyless alternatives through ccxt.
3. **LLM provider for the research agents** (later stage): none for now, a local model (Ollama, free, runs on your PC), or a paid API (needs your key and budget approval).
4. **Jev:** do you have a TypeSafe API key? If so it can be an *optional* extra agent, but it would be **fail-closed** here, unlike in QuantDinger.
