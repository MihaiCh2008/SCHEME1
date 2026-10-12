# Stage 3: Market-data pipeline (Solana DEX pools)

_Checked from the cloud session on 2026-10-12._

## 1. Endpoint verification

| Question | GeckoTerminal (`api.geckoterminal.com/api/v2`) | DexScreener (`api.dexscreener.com`) |
| --- | --- | --- |
| Reachable from the cloud session | Yes (HTTP 200) | Yes (HTTP 200) |
| Candle (OHLCV) history | **Yes**: `/networks/solana/pools/{pool}/ohlcv/{minute\|hour\|day}` | **No**: there is no candle endpoint; `/candles/...` returns 404 |
| Intervals | minute 1/5/15, hour 1/4/12, day 1 (other values return 400 `Invalid aggregate`). This covers all six `Interval` values. | n/a |
| Max rows per call | 1000 (`limit`); `before_timestamp` (unix seconds) pages back | n/a |
| Row format | `[unix_s, open, high, low, close, volume]`, **newest first**; volume in USD with `currency=usd` | n/a |
| Newest row | **Can still be open** (an 18:20 5m bar came back at 18:21) | n/a |
| Bucket alignment | Unix epoch (4h bars open at 00/04/08… UTC) | n/a |
| Freshness | Swagger: "cached for 1 minute"; data updated 2–3 s after confirmation | `cache-control: max-age=60` |
| Rate limit | Swagger: "approximately 10 calls/minute, which may fluctuate". **Observed:** HTTP 429 after about 10 calls in under 3 minutes; a second call 45 s later was still 429. | Not verified (docs host blocked). Prior knowledge only: about 300 req/min for pair and search endpoints. |
| 429 response | Body `{"status":{"error_code":429,...}}` with **`retry-after: 0`**, so the header can't be trusted | n/a |
| API versioning | `Accept: application/json;version=20230203` (we send it) | `schemaVersion: "1.0.0"` in the body |
| Liquidity field | Pool `reserve_in_usd`, `locked_liquidity_percentage`, `pool_created_at` | Pair `liquidity.usd/base/quote`, `pairCreatedAt` (ms) |
| Terms of use | Website Terms (11 Aug 2025) and CoinGecko API Terms of Service (5 Sept 2025) read, supplied by the user. See section 1a. | Website Terms (26 Apr 2025) read, supplied by the user. A separate API terms page, if there is one, is unread. See section 1a. |

Other findings:
- **The two providers disagree on pool age.** For the BONK/SOL Orca pool, GeckoTerminal says `pool_created_at = 2023-07-05` and DexScreener says `pairCreatedAt = 2022-12-25`. A token-age safety check (D9) must not trust a single source.
- `include_empty_intervals=true` is documented. What it fills an empty interval with (presumably zero volume and the previous close) **could not be observed**: the two quiet pools tried had too little history. We send `true`, so a missing row should mean missing data rather than "no trades". The validator treats any missing row as a `GAP` either way.

**Choice:** GeckoTerminal is the candle provider (it is the only one of the two with candles). DexScreener stays a candidate cross-check for liquidity and pool age in the token-safety work. See DECISIONS.md D10.

## 1a. Terms of use (read 2026-10-12, text supplied by the user)

Our reading of the terms, not legal advice.

**CoinGecko API Terms of Service** (Gecko Labs Pte. Ltd., version of 5 Sept 2025). These govern the GeckoTerminal API (Website Terms clause 3.3) and take precedence over the website terms for API use (clause 11.4):
- **Clause 3.1:** a licence to use the API to develop, test and support your own application.
- **Clause 4.2:** rate limits must not be exceeded or circumvented, and no "excessive or abusive usage".
- **Clause 4.4:** display "Powered by CoinGecko", legible, at least 10 pt.
- **Clause 4.5:** don't use their brand in a way that suggests endorsement.
- **Clause 4.1.6:** no reselling, sub-licensing or redistributing access.
- **Clause 4.1.7.6:** no public statements or representations about CoinGecko or its products without prior written consent.
- **Clause 6.1:** caching or storage is "not encouraged". If you must store data: refresh the cache at least every 24 hours, and apply "strong encryption and other security measures". **Clause 6.1.4:** if access is ended, delete all stored data.
- **Clause 6.2:** otherwise you may not duplicate, copy, store or "derive from" the data.
- **Clause 7:** applies only if the product is offered to people outside your own use. Then you need a user agreement and privacy policy. That doesn't apply to SCHEME1 as a personal tool.
- **Clause 8:** keep your systems secure; report breaches.
- **Clauses 11.2 and 12:** you must not be in an excluded country or under sanctions. The data is "for general informational purposes only", and CoinGecko is not liable for trading decisions.

**GeckoTerminal Website Terms of Use** (Gecko Labs Pte. Ltd., version of 11 August 2025):
- **Clause 3.3:** the GeckoTerminal API is free "subject to compliance with our API Terms of Service" (coingecko.com/en/api_terms). Those API terms decide what we may store and how. **They are still unread**, because coingecko.com is blocked from the cloud session.
- **Clauses 4.1–4.2:** without written consent or permission in the API Terms, you may not "reproduce, copy, download, … extract, store, distribute, … publish" their content. Any permitted use is a revocable, non-transferable licence "on … devices that are under and securely in your control".
- **Clauses 4.4–4.5:** the general licence is for "Personal Use … not for any commercial purpose".
- **Clause 9.1:** no scraping, data mining or "interfere[nce]" without consent.
- **Clause 1.1:** you must be 18 or older and not in an excluded country.
- **Clauses 3.1–3.2 and 8.2–8.3:** no warranty of accuracy, including "correctness of financial data and trading volume"; the data is "not meant for any reliance".
- **Clause 2.2:** the terms can change without notice.

**DEX Screener Terms and Conditions** (version of 26 April 2025):
- No explicit storage or redistribution clause in the website terms.
- **Abusive Activity:** forbids "an unreasonable or disproportionately large load on Our infrastructure".
- Also forbids implying endorsement or affiliation, and using their name or logo without consent.
- Access can be terminated "for any reason". No accuracy warranty. You must be 18 or older and not in an embargoed country.
- These are the website terms. A separate API terms page (docs.dexscreener.com/api/…) is unread.

**What this means for SCHEME1:**
1. **Personal, local, non-commercial use only.** The data and the desk database stay on your own machine. Never publish, sell or host them. The dashboard (Stage 11) must stay bound to localhost.
2. **24-hour retention (implemented).** `ingest()` first runs `CandleStore.purge_expired(now, 24h)`. It deletes every provider batch not retrieved within 24 hours, together with the clean candles that came from it. Re-fetching a candle moves it to the new batch, which counts as refreshing the cache. Fixture data is ours and is never purged. Validation reports (our own pipeline metadata) are kept.
3. **Delete-on-termination (implemented):** `python -m scheme1.data.cli --purge-all --db desk.db` (`CandleStore.purge_provider`).
4. **Attribution (implemented):** the CLI prints "Powered by CoinGecko" (`geckoterminal.ATTRIBUTION`). The dashboard must show it too, legible and at least 10 pt.
5. **Rate limits:** pacing is now 7.5 s between calls (8/min, below the ~10/min limit), with a 60 s back-off on 429. During the Stage 3 probe we hit one 429, so the probe exceeded the limit once; the pacing exists to prevent that.
6. **Encryption ("should", clause 6.1.2): not implemented in the app.** Recommended: keep `desk.db` on a disk with full-disk encryption (BitLocker on Windows, FileVault on macOS). App-level encryption (SQLCipher) is possible later but adds a native dependency.
7. **No long-term price archive.** The backtest (Stage 8) can't rely on stored months of history. It must fetch the history it needs at run time (paged with `before_timestamp`, up to 1000 candles per call) and let the purge remove it afterwards. This also means we can't build our own archive of pools that later die (the survivorship issue in D9). That limitation has to be reported in backtest results.
8. **Derived records** (feature snapshots and decisions, Stages 4–7) contain values computed from provider data. Clause 6.2 restricts "deriving" except as the terms permit. Clause 3.1 permits using the API in our own application, so computing them is covered. **Keeping them indefinitely is unclear.** Until clarified with CoinGecko, records with provider-derived numbers follow the same 24-hour rule, or store only our own outputs (signal, risk verdict, fills) and drop the copied values.
9. **This repository is public.** It contains no provider data any more (the real response is still in git history at commit 7be4292). This document also describes CoinGecko's API behaviour, which clause 4.1.7.6 might count as a public statement. **Recommendation: make the repository private.** That fixes both.
10. Neither provider guarantees the data. That backs up the rule that bad or stale data blocks entries, and the DexScreener cross-check.

**DEX Screener:** its website terms only forbid unreasonable load and implied endorsement. Its API terms page (if any) is still unread. The same local, personal-use rules apply.
