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
| Terms of use | Website Terms (11 Aug 2025) read, supplied by the user. The **API Terms of Service** they defer to are still unread (blocked here). See section 1a. | Website Terms (26 Apr 2025) read, supplied by the user. A separate API terms page, if there is one, is unread. See section 1a. |

Other findings:
- **The two providers disagree on pool age.** For the BONK/SOL Orca pool, GeckoTerminal says `pool_created_at = 2023-07-05` and DexScreener says `pairCreatedAt = 2022-12-25`. A token-age safety check (D9) must not trust a single source.
- `include_empty_intervals=true` is documented. What it fills an empty interval with (presumably zero volume and the previous close) **could not be observed**: the two quiet pools tried had too little history. We send `true`, so a missing row should mean missing data rather than "no trades". The validator treats any missing row as a `GAP` either way.

**Choice:** GeckoTerminal is the candle provider (it is the only one of the two with candles). DexScreener stays a candidate cross-check for liquidity and pool age in the token-safety work. See DECISIONS.md D10.

## 1a. Terms of use (read 2026-10-12, text supplied by the user)

Our reading of the terms, not legal advice.

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
1. Personal, non-commercial, local use only. The data and the desk database stay on your own machine. Never publish, sell or host the stored data or dashboards built from it. The dashboard (Stage 11) must stay bound to localhost.
2. **No provider data in this public repository.** The real GeckoTerminal response committed in Stage 3 was replaced with a synthetic file in the same layout. `*.db` files are already git-ignored. **The removed file is still in git history** (commit 7be4292). Removing it from history needs a force-push; that's your call.
3. Local storage of fetched candles (`raw_batches`, `clean_candles`) is what this design needs. Clause 4.1 only allows it where the API Terms permit it, so **the CoinGecko API Terms of Service must be read before relying on stored history.** If they forbid retention, the store needs a retention limit.
4. Use the documented API with conservative pacing (already about 9 calls/min, with a 60 s back-off on 429). Never scrape the website.
5. Neither provider guarantees the data. That backs up the existing rule that bad or stale data blocks entries, and the DexScreener cross-check.
6. Don't use either provider's name or logo in a way that suggests endorsement. Plain attribution ("data: GeckoTerminal") in the local UI is fine.

## 2. What was built

| Module | Does |
| --- | --- |
| `scheme1.data.http` | Stdlib HTTP client. Spaces calls (`min_interval_s`, default 6.5 s ≈ 9 calls/min). Bounded retries: timeouts, network errors and 5xx back off 2/4/8 s; 429 waits at least 60 s (or a longer `retry-after`); other 4xx fail at once. |
| `scheme1.data.providers.base` | `MarketDataProvider` protocol, `FetchResult(batch, raw)`, `ProviderError`. |
| `scheme1.data.providers.geckoterminal` | URL building (pool address checked as base58, so no arbitrary text reaches the URL), exact decimal parsing (`parse_float=Decimal`), closed/open marking, provenance with `sha256(raw body)`. |
| `scheme1.data.providers.fixture` | Offline JSON provider. Its data is always labelled `DataKind.FIXTURE`. |
| `scheme1.data.validate` | Validator and freshness rules (below). |
| `scheme1.data.store` | SQLite (WAL): `raw_batches` (exact bytes and provenance), `validation_reports` (including provider failures and their error text), `clean_candles` (one row per symbol/interval/open_time; revisions counted). One transaction per ingest. |
| `scheme1.data.pipeline` | `ingest()`: fetch, validate, store. A provider failure is stored and returned as `UNAVAILABLE`; it never raises. `stored_freshness()`. |
| `scheme1.data.cli` | `python -m scheme1.data.cli --pool <address> --interval 5m --db desk.db` |

Symbols for DEX pools are `solana:<pool address>`. Note that this is the **pool** address, not the token mint.

Contract change: `SCHEMA_VERSION` 1.0.0 → **1.1.0**. `CandleBatch.parse_issues` was added (rows that could not become a `Candle` are reported, not silently lost), along with two issue codes, `INCONSISTENT_OHLC` and `MALFORMED`.

### Validation rules

Status precedence: `UNAVAILABLE` > `STALE` > `DEGRADED` > `OK`.

| Check | Effect |
| --- | --- |
| Row unparseable / price ≤ 0 / volume < 0 / high or low inconsistent | Row dropped at parse time and reported. Batch becomes DEGRADED. |
| Wrong symbol or interval, misaligned open time | Candle dropped. DEGRADED. |
| Out of order | Reported, then sorted. DEGRADED. |
| Duplicate open time | An identical copy is dropped. If the copies conflict, **both** are dropped (we can't tell which is right). DEGRADED. |
| Newest candle still open | Dropped, **no issue** (this is normal). |
| An open candle that isn't the newest | Dropped. DEGRADED. |
| Gap (inside the batch, or against the newest stored candle before it) | DEGRADED. |
| Outlier: one bar's high/low > 20×, or close jumps > 20× from the previous close | Candle **kept** (dropping it would create a gap) but DEGRADED. The thresholds are set so ordinary memecoin pumps don't trip them. |
| Newest closed candle ended more than one interval + 150 s ago | STALE. |
| No usable closed candle | UNAVAILABLE. |

DEGRADED, STALE and UNAVAILABLE all block new entries downstream (the `Decision` contract already forces HOLD when data is not OK).

## 3. Evidence

- `python -m pytest`: 55 passed (14 contract tests, 41 new data tests). None of the tests touch the network: HTTP is a scripted fake and sleeps go through a fake clock. The tests cover timeouts, 429 with `retry-after: 0`, 5xx exhaustion, non-retryable 400, malformed and empty payloads, bad rows, every validator rule, store rollback, revisions and gaps between batches.
- `tests/fixtures/geckoterminal_shape_5m.json` copies the exact layout of a real GeckoTerminal response with synthetic numbers, so the parser is tested against the real shape without redistributing their data.
- Live run from the cloud session on 2026-10-12, BONK/SOL pool `5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9`:
  - `--interval 5m --limit 300`: status ok, 300 in, 299 clean (newest open bar dropped), no issues.
  - `--interval 1m --limit 1000`: status ok, 1000 in, 999 clean, no gaps.

## 4. Known limitations

- About 10 calls/minute is shared by everything that uses the public API from the same IP. Watching many pools at 1m is not possible on the free tier; the scheduler (later stage) must budget calls.
- USD prices from GeckoTerminal depend on its SOL/USD conversion. `currency=token` (price in SOL) is available if needed later.
- Survivorship bias (D9) is not addressed yet: we only fetch pools we name. It's a backtest-stage concern, but historical pool lists need to be recorded from now on if we want them.
- Token-safety data (mint/freeze authority, holder concentration) is not covered by either API checked here.
