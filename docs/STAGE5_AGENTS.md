# Stage 5: Research agents (phase 1, deterministic)

_Built and checked 2026-10-15._

## 1. What they are

Six agents in `scheme1.agents`. Each one reads `AgentInputs` (a `FeatureSnapshot`, a `ValidationReport` and a `SafetyReport` for the same pool) and returns an `AgentOutput`: stance, score in [-1, 1] and evidence. No network, no LLM, no randomness: the same inputs always give the same output. Scores are research scores, not probabilities (ARCHITECTURE.md section 5). Agents place no orders.

| Agent | Role | Reads | Output |
| --- | --- | --- | --- |
| `market.v1` | `market` | `ret_12`, `vol_12`, `range_pos_48`, `volume_ratio_12_48` | 0.6 × momentum + 0.4 × range position; halved when volume ratio ≤ 0.5 |
| `technical.v1` | `technical` | `ema_ratio_9_21`, `close_vs_ema_21`, `rsi_14`, `vol_48` | 0.4 × EMA trend + 0.3 × close vs EMA21 + 0.3 × RSI |
| `data_quality.v1` | `data_news` | validation status and issues, warm-up, `bars_available`, `zero_volume_frac_48` | Always NEUTRAL, score 0. Flags non-OK data ("only HOLD") and illiquid candles |
| `bull.v1` | `bull` | all signals, plus "oversold" (RSI < 30) | Mean of the bullish signals only |
| `bear.v1` | `bear` | all signals, plus "overbought" (RSI > 70) | Mean of the bearish signals only |
| `risk.v1` | `risk` | `safety.*`, `atr_pct_14`, `zero_volume_frac_48`, `drawdown_48` | Penalty ≤ 0: −1 if not tradable, −0.6 per breached limit (worst one counts) |

The contract has no "data quality" role, so the data agent uses `data_news`. There is no news source.

**Signals** (`scheme1.agents.signals`). Price moves are divided by the candle volatility: momentum is `tanh(ret_12 / (vol_12 · √12))`, the EMA trend is `tanh(ema_ratio / (2 · vol))`. So one rule means the same on 1m and 1h candles, and on quiet and wild tokens. A signal whose inputs are `None` is skipped and the weights are renormalized. With no usable signal (warm-up), market and technical ABSTAIN.

Limits live in `AgentConfig` (version `a1.0.0`): RSI 70/30, volume surge 1.5×, fading 0.5×, ATR 5% of price, 20% zero-volume candles, drawdown −30%.

## 2. Citation rule

`run_agents()` wraps every agent the same way. An output becomes `INVALID` (stance ABSTAIN, score 0, evidence dropped, error kept) when:
- evidence cites a reference that is not in the inputs, or a FACT cites a feature whose value is `None`;
- the agent crashes;
- the output breaks the contract (for example a score outside [-1, 1]).

Citable references: feature keys (`rsi_14`), `feature.warmup_complete`, `validation.status`, `validation.issues`, `safety.tradable`, `safety.<check name>`. Every evidence item, including interpretations, names what it rests on.

## 3. Checks

14 tests in `tests/test_agents.py`:
- all agents run and cite only real inputs; same inputs give the same outputs;
- uptrend → market and technical bullish, bear neutral; downtrend the reverse;
- warm-up → ABSTAIN; non-OK data is flagged; the data agent is never directional;
- an unsafe token → risk −1 with the failed check cited; high ATR → −0.6;
- fading volume halves the market score; overbought goes to the bear, oversold to the bull;
- an unknown reference, a fact citing a missing value, a crash and an out-of-range score each give `INVALID`;
- the agents run cleanly on real `compute_features` output.

## 4. Live check (2026-10-15, cloud session)

`python -m scheme1.agents.cli --pool 5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9` (BONK/SOL, 5m). Candles are kept in memory only.

- data OK, 64 candles, no issues;
- market bearish −0.22 (down 0.48 σ over 12 candles, lower part of the range, volume fading);
- technical bearish −0.12 (EMA9 below EMA21, RSI 42);
- bull neutral 0, bear −0.25;
- risk −1.00: holder concentration fails (D13), so the token must not be bought.

All six outputs were OK, with every reference valid.

## 5. Not done yet

- **Manager / decision engine**: weighing the agents into a `Decision` is Stage 6.
- **Phase 2 (optional LLM)**: not started. No provider is configured. It would sit behind the same `AgentOutput` contract and the same citation check, plus a check that quoted numbers match the snapshot.
- The weights and limits are first guesses. They have not been tested on history; that's Stage 8 (backtesting).
