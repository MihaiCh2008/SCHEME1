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

## D15. Quick-trade strategy with fixed exits; exits allowed when the desk is degraded (2026-10-15)
- **Choice:** strategy `quick-1.0.0` holds for minutes to a few hours: take profit +5%, stop loss −3%, time limit 2 hours, early exit when the combined score drops to −0.35. Entry needs a safe token, no blocking risk and a combined score of at least +0.35. `RiskVerdict.exit_only` lets an exit pass when the desk is DEGRADED or PAUSED; EMERGENCY_STOP blocks everything (schema 1.4.0).
- **Why:** the user wants quick in-and-out trades. Sniping new launches was rejected: most new tokens are scams (our 24-hour pool-age rule blocks them), paid low-latency bots win those races, and the free data is 1-minute candles at best. A round trip on a memecoin DEX costs roughly 0.5–2%, so the take profit must clear that (enforced in `StrategyConfig`). Blocking exits whenever the desk is not NORMAL could lock in a loss.
- **Alternatives:** sniping (rejected, above); holding for days (not what the user wants); trailing stops (later, after backtesting).
- **Consequence:** stops are checked on candle closes, so gaps can make a loss larger than 3%. All the numbers are guesses until Stage 8.

## D16. Stop loss and take profit checked inside each candle (2026-10-15)
- **Choice:** every position gets protective levels at entry. Each later candle is checked on its low and high: the stop fills at the stop price, or at the open when the candle gapped past it. When one candle reaches both levels, the stop is assumed. Zero-volume candles fill nothing. The close-based exit in `decide()` stays as a backup.
- **Why:** checking only candle closes and selling at the next open let a −3% stop become a much larger loss. The user asked for this to be fixed.
- **Alternatives:** check closes on 1-minute candles (better, but still waits for a close); assume the take profit when both levels are hit (rejected: optimistic, and it would flatter backtests).
- **Consequence:** gaps can't be prevented. Real DEX trading has no exchange-held stops, so a live bot would do somewhat worse than this simulation.

## D17. Trailing stop, and exit as soon as the agents stop being bullish (2026-10-15)
- **Choice:** strategy `quick-1.1.0`. The stop starts 3% below the entry, then follows the price up: after each candle it is 2% below the highest high since entry, and it never moves down. The take profit stays at +5%. The early-exit score moves from −0.35 to 0, so a position closes as soon as the combined score is no longer bullish.
- **Why:** the user's idea: hold while the price is rising, and exit as soon as it turns down. Exiting on the very first dip was rejected: prices always wiggle, so almost every trade would end within minutes, and the ~1% round-trip cost would eat everything. A 2% trail ignores small dips but exits on a real turn, and once the price is about 2% up, a loss is no longer possible.
- **Alternatives:** exit on any drop below the entry (rejected, above); a trail based on ATR (adapts to each token; worth testing in Stage 8).
- **Consequence:** exits happen sooner and more often. 2% may be too tight for very volatile memecoins; backtesting will tell.

## D18. Daily loss limit: 5% per UTC day (2026-11-17)
- **Choice:** no new buys once equity is more than 5% below the equity at the start of the UTC day (risk check `daily_loss`, config `r1.2.0`). Exits are always allowed. The limit resets by itself at 00:00 UTC.
- **Why:** the user asked for it after the Stage 6 review. The 20% drawdown pause only acts after a long losing run; a daily limit stops a bad day early.
- **Alternatives:** 3% (stricter); the user's local midnight instead of UTC (UTC keeps backtests and live runs identical).
- **Consequence:** after a 5% loss, the rest of the UTC day only manages open positions.

## D19. Paper broker rules (2026-11-17)
- **Choice:**
  - Market orders only. Entries fill at the next candle's open + 0.25% slippage, with a 0.25% fee on every fill (a round trip costs ~1%, as the strategy assumes; `DeskConfig` refuses higher broker costs).
  - All fills in one candle use at most 5% of its USD volume. No volume → no fill.
  - **An entry lives one candle** and is skipped if the open is more than 2% above the decision price. **An exit never expires.**
  - The trailing stop, take profit and time limit are run by the broker on every candle from the entry candle on, without going back through `decide()` or the risk engine.
  - No buys until the feature warm-up is complete (64 candles), strategy `quick-1.2.0`.
  - The audit log keeps only our own outputs; feature values and agent evidence are kept 24 hours (STAGE3_DATA.md 1a item 8).
- **Why:** entries that wait would buy later, at prices the decision never saw; the 2% limit stops the bot from chasing a jump, like a DEX slippage setting. Exits must never be dropped or blocked. The first synthetic test bought after 14 candles on RSI alone, which breaks the fail-closed rule.
- **Alternatives:** resting entries for several candles (rejected: stale decisions); exits through the risk engine (rejected: a data problem could lock in a loss, see the Stage 6 review).
- **Consequence:** some signals won't fill (no volume, price jumped). Fills ignore our own price impact beyond the 5% cap and 0.25% slippage.

## D20. FOMO entries: no price-jump limit on buys (2026-11-17)
- **Choice:** a buy fills at the next open even if the price jumped since the decision. `BrokerConfig.entry_tolerance_pct` is now off (`None`) by default; it stays as an option so Stage 8 can compare 2%, 5% and off. This replaces the 2% limit in D19.
- **Why:** the user wants the bot to trade on FOMO: buy into a sudden pump with big volume. A 2% limit would skip exactly those entries.
- **Alternatives:** 5% limit (proposed; the user chose no limit).
- **Consequence:** the bot can buy near the top of a spike. The exits (trailing stop, take profit, time limit) are what limits the damage.

## D21. Holder check leaves out known exchange wallets (option b) (2026-11-17)
- **Choice:** wallets labeled as centralized exchanges are excluded from holder concentration, like pool vaults. The list is `scheme1.safety.exchange_wallets`: 112 Solana addresses from Dune Analytics Spellbook (commit 475a7e1), kept under Spellbook's Business Source License 1.1 (license file next to it). Limits unchanged: largest wallet ≤ 10%, top 10 wallets ≤ 30%. Safety config `s1.2.0`.
- **Why:** the user chose option b. With the strict rule, none of the five big memecoins tested could be traded; their largest "wallets" include exchange hot wallets that hold coins for many customers. Live 2026-11-17: Fartcoin (Gate.io 5.6% and MEXC 1.2% excluded → top 10 wallets 27.3%) now passes; BONK, WIF, POPCAT and MEW still fail on non-exchange wallets.
- **Alternatives:** (a) keep the strict rule; (c) raise the top-10 limit to 40%. **The user asked to be reminded to test (c) against (b) in Stage 8 and keep whichever makes more money.**
- **Consequence:** an address wrongly labeled as an exchange would let a whale through, so addresses are only added with a source. Exchanges missing from Dune's list (for example some Korean or US broker wallets) still count as whales, which is the safe side.

## D22. Copy trades may buy new pump.fun coins (2026-11-17)
- **Choice:** for copy trades (strategy B: buying when a trader the user follows buys), coins younger than 24 hours are allowed. The 24-hour pool-age rule (D12) and the "no new launches" part of D15 don't apply to copy trades. The bot's own FOMO trades (strategy A) keep both rules. The other safety checks for copy trades (mint and freeze authority, token extensions, holders, liquidity) will be set when B is built, and asked about where they change a rule.
- **Why:** the user confirmed it ("it's ok"). The traders they follow on the pump.fun app mostly buy new coins, so with the 24-hour rule copy trading would never trigger.
- **Alternatives:** keep the 24-hour rule for copy trades too (rejected by the user).
- **Consequence:** copy trades will meet many more rugs and scams. Paper trading measures whether following these traders makes money before anything else is considered.
