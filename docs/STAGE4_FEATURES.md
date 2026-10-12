# Stage 4: Features and token safety

_Built and checked 2026-10-12._

## 1. Feature engine (`scheme1.features`)

`compute_features(candles, symbol, interval, at)` returns a `FeatureSnapshot`. `snapshot_at(store, symbol, interval, at)` builds one from stored clean candles. Backtests and paper trading call the same function.

**Rules**
- **No look-ahead.** A candle is used only if it is closed and its close time is at or before `at`. `as_of` is the close time of the last candle used.
- **No computing across gaps.** Only the trailing run of consecutive candles is used. `bars_available` reports its length.
- **Fixed window.** Every feature reads at most the last `LOOKBACK = 64` candles. The same 64 candles always give the same values, however much older history was loaded.
- **`None` = not computable.** That means too few candles for that feature, or a zero denominator. A flat price gives RSI `None`, not 50. `warmup_complete` is true only with all 64 candles.
- Floats for statistics, Decimal stays the type for money (D5).

**Feature set `f1.0.0`**

| Key | Meaning | Candles needed |
| --- | --- | --- |
| `bars_available` | Length of the gap-free run used (max 64) | 1 |
| `ret_1`, `ret_3`, `ret_12` | Log return over 1/3/12 candles | n+1 |
| `vol_12`, `vol_48` | Sample std-dev of 1-candle log returns | 13 / 49 |
| `ema_ratio_9_21` | EMA9 / EMA21 − 1 (EMAs seeded with the window's first SMA) | 21 |
| `close_vs_ema_21` | Close / EMA21 − 1 | 21 |
| `rsi_14` | Wilder RSI | 15 |
| `atr_pct_14` | Wilder ATR / close | 15 |
| `volume_usd_12` | Volume summed over 12 candles (USD for GeckoTerminal) | 12 |
| `volume_ratio_12_48` | Mean volume of the last 12 / mean of the last 48 | 48 |
| `range_pos_48` | Where the close sits in the 48-candle high-low range, 0..1 | 48 |
| `drawdown_48` | Close / 48-candle high − 1 | 48 |
| `zero_volume_frac_48` | Share of the last 48 candles with no trades (illiquidity) | 48 |

**Checks.** 12 tests:
- Future candles, an open candle at the edge, or a wildly different future never change a snapshot.
- Older history doesn't change the values.
- Gaps reset the run.
- Zero denominators give `None`.
- The store path cites the right batch ids.
- RSI reproduces StockCharts' published Wilder example (70.53, then 66.32) to 0.01.

## 2. Token safety (`scheme1.safety`)

- `GeckoTerminalPools.snapshot()` and `DexScreenerPools.snapshot()` each return a `PoolSnapshot`: base-token mint, liquidity in USD and pool creation time. Each costs one API call. GeckoTerminal shares the ~10/min budget with candles; DexScreener is paced at 1 call per second.
- Snapshots aren't stored (D11).
- `evaluate()` returns a `SafetyReport`. The contract makes `tradable` true **only** if every check is `pass`. `unverified` blocks exactly like `fail`.

| Check | Rule | Status today |
| --- | --- | --- |
| `base_token_agrees` | Both providers name the same mint for the pool | Works |
| `min_liquidity` | **Lower** of the two figures ≥ $25,000. If the figures differ by more than 1.5×, or only one provider answered → unverified | Works |
| `min_pool_age` | **Youngest** reported creation date ≥ 24 h ago (the providers disagree by months on old pools) | Works |
| `mint_authority_revoked` | The token's creator can no longer mint new supply | **Unverified: no data source** |
| `freeze_authority_revoked` | The creator can't freeze holders' tokens (the honeypot trick) | **Unverified: no data source** |
| `holder_concentration` | Top holders (excluding the pool itself) don't own too much | **Unverified: no data source** |

Snapshots older than 10 minutes are ignored. The limits live in `SafetyConfig` (version `s1.0.0`).

**Result: every token is currently blocked**, by design (D9). The last three checks need Solana on-chain data. The public RPC (`api.mainnet-beta.solana.com`) is blocked from the cloud session (HTTP 403 from the proxy). Options: allow that host in the environment, or use a free RPC key (Helius, QuickNode) from your PC.

## 3. Live check (2026-10-12, cloud session)

BONK/SOL pool `5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9`:
- Ingested 100 5m candles (status ok). The snapshot at 18:55 UTC had warm-up complete; for example RSI 46.3, 12-candle volume $9,196 and 48-candle drawdown −1.3%.
- Safety:
  - **base token:** pass (both providers report `DezXAZ…B263`)
  - **liquidity:** pass ($357,451 vs $358,057)
  - **pool age:** pass (youngest date 2023-07-05)
  - **on-chain checks:** all three unverified
  - **result:** not tradable
