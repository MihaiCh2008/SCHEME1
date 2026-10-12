# Architecture Decisions

Each entry records the choice, the alternatives considered, and why. Evidence for the repository decisions is in [docs/STAGE1_AUDIT.md](docs/STAGE1_AUDIT.md).

## D1. Build our own deterministic core instead of adopting one framework
- **Choice:** write validation, features, decisions, risk, paper broker, accounting and backtest in our own small Python package.
- **Alternatives:** Freqtrade (complete bot), NautilusTrader (engine), QuantDinger (complete app).
- **Why:** Freqtrade is GPL-3.0, so importing it relicenses our code. NautilusTrader's `develop` is a v2 release candidate mid-migration from v1. QuantDinger needs Postgres, Redis and Celery and ships its frontend as a prebuilt image. More importantly, the accounting and risk rules must be ours to test and audit line by line.

## D2. ccxt as the first market-data adapter
- **Choice:** ccxt (MIT) behind our own `MarketDataProvider` interface, plus a fixture provider for offline tests.
- **Alternatives:** OpenBB 5.0 (its only keyless crypto historical source in-repo is Nasdaq, with no interval parameter), raw exchange REST.
- **Why:** one keyless library covers Binance, Kraken and Coinbase OHLCV. Freqtrade relies on the same library.

## D3. TradingAgents as a reference, not a dependency
- **Choice:** copy its role design (analysts, bull/bear, manager, risk) into deterministic agents first. Add an optional LLM adapter later.
- **Why:** it needs a paid LLM per decision, its main output is prose plus a 5-tier rating, and its own `backtest.py` says it has no cash or fill model.

## D4. No "Jev" dependency; any external AI gate is fail-closed
- **Choice:** don't depend on TypeSafe Jev. If you provide a key later, it can be one optional agent. When it's unavailable, it **blocks new entries** instead of allowing them.
- **Why:** QuantDinger's integration is documented as "fail-open" (`ai_decision_filter.py`), which conflicts with the requirement to stop actions when inputs are unavailable.

## D5. Decimal for money, float for statistics
- Prices, quantities, cash and fees are `Decimal`, so accounting invariants hold exactly. Indicators are `float`.

## D6. SQLite with a hash-chained audit log
- **Alternatives:** Postgres (QuantDinger), files.
- **Why:** zero setup on your PC, transactional, and easy to back up. The hash chain makes tampering or corruption detectable at startup.

## D7. Long-only spot simulation in v1
- Shorting needs borrow and funding models we can't verify yet, so SELL only reduces a held position.

## D8. Old JavaScript prototype kept in `prototype/`
- It runs on a synthetic market and is not part of the new system. It's kept for visual reference only.
