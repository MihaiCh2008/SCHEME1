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

## D9. Scope change: Solana memecoins, paper trading only (2026-10-12)
- **Choice:** target Solana memecoins. Data comes from public DEX data APIs (GeckoTerminal and/or DexScreener; to be verified in Stage 3). Execution stays simulated. The user trades by hand on Axiom or FOMO if they choose.
- **Why not Axiom/FOMO as integrations:** neither publishes an official developer API (searched 2026-10-12). Community Axiom libraries authenticate with the user's web-session cookie, which is fragile, possibly against Axiom's terms, and a custody risk. Real-money execution is also out of scope by requirement.
- **Design consequences:**
  - Token-safety checks become part of data validation and risk: minimum pool liquidity, token age, top-holder concentration, mint/freeze authority. Each needs a verified data source before it can be enforced. Until then, the token counts as unverified and is blocked.
  - The paper broker must model AMM price impact from pool liquidity, not just candle volume.
  - Backtests must address survivorship bias (dead tokens disappear from listings).
  - Long-only. Base and BNB chains are out of scope until Solana works end to end.
- Supersedes the exchange choice in D2. ccxt stays available for the major exchanges but is not the first provider.

## D10. GeckoTerminal as the candle provider for Solana pools (2026-10-12)
- **Choice:** GeckoTerminal public API (keyless) behind `MarketDataProvider`. Paced to stay under its limit of about 10 calls/minute, with our own 60 s back-off after a 429 because its `retry-after` header is `0`.
- **Alternatives:** DexScreener has no candle endpoint (verified: 404), so it can't feed features or backtests. CoinGecko's paid on-chain API has the same data with higher limits; it's an option if the free limit becomes the bottleneck.
- **Consequences:** symbols are `solana:<pool address>`; candle volume is in USD; the newest candle is often still open and is dropped by the validator. DexScreener stays a candidate second source for liquidity and pool age, since the two providers already disagree on pool age for BONK/SOL.
- Evidence: [docs/STAGE3_DATA.md](docs/STAGE3_DATA.md).

## D11. Provider terms: personal, local use; 24-hour retention (2026-10-12)
- **Choice:** SCHEME1 is a personal tool. Provider data stays on the user's machine and is not committed, published or served beyond localhost. GeckoTerminal data is kept at most 24 hours since it was last fetched, and can be deleted in one command. "Powered by CoinGecko" is shown wherever the data is shown. Test fixtures that mimic a provider are synthetic.
- **Why:** the CoinGecko API Terms of Service (5 Sept 2025), which govern the GeckoTerminal API, discourage storage. They allow it only with a refresh at least every 24 hours and strong security (6.1), and otherwise forbid storing or deriving (6.2). They also require attribution (4.4), respecting rate limits (4.2) and deletion when access ends (6.1.4). The GeckoTerminal Website Terms (11 Aug 2025) grant personal, non-commercial use. DEX Screener's terms (26 Apr 2025) forbid unreasonable load and implied endorsement.
- **Alternatives:** keep a long-term local archive for backtests. Rejected: not permitted on the free API. A paid plan or a written agreement with CoinGecko (an "Executed Agreement", clause 11.4) could change this.
- **Consequences:** backtests fetch history at run time instead of reading an archive. Survivorship bias can't be fixed with our own archive. Long-term storage of derived records is an open question (see docs/STAGE3_DATA.md section 1a, item 8).

## D12. Token safety: two providers, conservative merge, unverified blocks (2026-10-12)
- **Choice:**
  - Liquidity uses the lower of the GeckoTerminal and DexScreener figures, and is unverified if they differ by more than 1.5×.
  - Pool age uses the youngest reported creation date.
  - The base-token mint must match across both.
  - From Solana RPC: mint and freeze authority must be revoked; Token-2022 extensions must be on a metadata-only allow-list; the largest wallet must hold ≤ 10% and the top 10 wallets ≤ 30% of supply.
  - Program-derived owners (pool vaults, bonding curves, lockers) are identified with the Ed25519 curve test and excluded from concentration.
  - Missing or stale data makes a check `unverified`, which blocks like `fail`.
- **Why:** the providers already disagree on pool age by months for BONK/SOL, and a single bad figure must not unlock trading. Mint and freeze authority, dangerous Token-2022 extensions and whale wallets are the common ways memecoin holders lose everything. Blocking by default follows D9.
- **Alternatives:** third-party rug-check services. Rejected for now: another dependency with its own terms, and their scores can't be audited line by line (the same reason as D1).
- **Consequence:** no token is tradable until the RPC checks run. The RPC is blocked from the cloud session today.

## D13. Holder concentration: keep the strict rule; exchange wallets are not excluded (2026-10-12)
- **Choice:** keep D12's limits (largest wallet ≤ 10%, top 10 wallets ≤ 30%). Only program-derived owners are excluded. Normal wallets count, including exchange wallets.
- **Why:** first live run with Helius: BONK fails (largest wallet 8.83%, top 10 wallets 38.41%). Its largest holders are probably exchange wallets (not confirmed), but the curve test can't tell an exchange from a whale. Missing a good token is cheaper than buying a dumped one, and the desk is paper-only (D9).
- **Alternatives:** (b) a reviewed list of known exchange wallets, excluded like program-controlled owners; (c) a higher top-10 limit. Both deferred; can be revisited once paper results show how many tokens are blocked.
- **Consequence:** large tokens with exchange-held supply, such as BONK, are blocked. Fewer pools are tradable.
- **Also:** the public RPC refuses `getTokenLargestAccounts` (HTTP 429), so `SCHEME1_SOLANA_RPC_URL` must point to a provider such as the Helius free plan.

## D14. Phase-1 agents: volatility-scaled signals; manager moves to Stage 6 (2026-10-15)
- **Choice:** six deterministic agents (market, technical, data quality, bull, bear, risk). Price signals are divided by candle volatility. The data agent uses the contract's `data_news` role and is never directional. The risk agent only subtracts (score ≤ 0). Any output citing an unknown input, or a FACT citing a missing value, is `INVALID`. The manager is built with the decision engine in Stage 6.
- **Why:** memecoin volatility differs by 10× or more between tokens and intervals; raw-return thresholds would mean different things on each. Keeping the manager out of Stage 5 means agents can't see each other, so bull and bear stay independent.
- **Alternatives:** fixed percentage thresholds (rejected: interval-dependent); a news agent (no news source; deferred).
- **Consequence:** weights and limits (`AgentConfig` a1.0.0) are unvalidated guesses until backtesting (Stage 8).
