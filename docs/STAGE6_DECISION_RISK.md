# Stage 6: Decision and risk engines

_Built and checked 2026-10-15._

The chain for one closed candle: agents → `decide()` → `authorize()` → `evaluate_risk()`. Nothing is bought or sold here; the paper broker is Stage 7.

## 1. Decision engine (`scheme1.decision.decide`)

Strategy `quick-1.0.0`, for trades of minutes to a few hours (DECISIONS.md D15). The first rule that applies wins:

1. **Data not OK → HOLD.** With a position open there is no exit either, since the price can't be trusted. `rejection_reasons` says so.
2. **Position open → exit rules.** SELL on any of:
   - take profit: last close ≥ entry + 5%;
   - stop loss: last close ≤ entry − 3%;
   - time limit: held ≥ 2 hours;
   - the token is no longer tradable;
   - the combined score is ≤ −0.35.
   Otherwise HOLD.
3. **No position → BUY only if all hold:** the token is tradable, the risk agent raised no blocking risk, market and technical both gave an opinion, and the combined score is ≥ +0.35. Otherwise HOLD, listing every reason that failed.

**Combined score** = weighted mean of market (0.35), technical (0.35) and bull + bear (0.3). Invalid or abstaining agents are left out and the weights are renormalized. **Confidence** = |score| × (1 − disagreement). Disagreement is +0.5 when market and technical point opposite ways, plus half of min(bull, |bear|) when both advocates are strong. It is an uncalibrated research score.

`StrategyConfig` refuses settings that can't make money: take profit must be larger than the round-trip cost (1% assumed), and the exit score must be below the buy threshold.

**Known limit:** exits are checked on candle closes and a SELL fills at the next open (ARCHITECTURE.md section 3). So a loss can be larger than 3% when the price gaps down.

## 2. Order authorizer (`scheme1.decision.authorize`)

- BUY: 10% of equity (or less if cash is lower), minus a 2% buffer for fee and slippage, divided by the reference price, rounded down.
- SELL: the whole position.
- HOLD, or a size that rounds to zero: no intent.
- Market orders only.

## 3. Risk engine (`scheme1.risk.evaluate_risk`)

Config `r1.0.0`. Every check runs every time and all results are stored. Any failure rejects.

| Check | Rejects when | Buys | Exits |
| --- | --- | --- | --- |
| `desk_state` | desk not NORMAL (exits also allowed when DEGRADED or PAUSED; never in EMERGENCY_STOP) | ✓ | ✓ |
| `data_quality` | `ValidationReport.status` is not OK | ✓ | ✓ |
| `data_age` | newest candle older than 10 minutes | ✓ | ✓ |
| `short_sale` | a sell exceeds the held quantity | ✓ | ✓ |
| `duplicate_or_conflict` | an open order exists on the symbol | ✓ | ✓ |
| `drawdown` | equity more than 20% below peak (exits always allowed) | ✓ | ✓ |
| `cash` | cost + 0.25% fee + 1% slippage buffer exceeds cash | ✓ | |
| `position_size` | the position would exceed 10% of equity | ✓ | |
| `gross_exposure` | all positions together would exceed 50% of equity | ✓ | |
| `concentration` | the largest asset would exceed 15% of equity | ✓ | |
| `token_safety` | the token failed any safety check | ✓ | |
| `frequency` | 20 orders today, or an order on the symbol in the last 15 minutes | ✓ | |

**Contract change (schema 1.4.0):** `RiskVerdict.exit_only`. Before, nothing could be approved unless the desk was NORMAL, so a stop loss could be locked in by a data problem on another token. Now an exit can pass when the desk is DEGRADED or PAUSED. EMERGENCY_STOP still blocks everything.

**Not built:** `correlated_exposure` (needs rolling correlations between held tokens). Until it exists, the 50% gross-exposure cap is the only limit on many similar memecoins at once.

## 4. Checks

30 tests in `tests/test_decision_risk.py`:
- a strong uptrend on a safe token buys;
- each missing condition gives HOLD with the right reason: unsafe token, stale data, flat or falling market, risk flag, warm-up;
- disagreement lowers confidence, and invalid agents are left out;
- each exit rule sells, and a position with no exit trigger is held;
- bad data blocks exits and says so;
- losing settings are refused;
- sizing works, and sell closes the whole position;
- each risk limit rejects on its own, every check is listed;
- exits pass when paused or in drawdown, but never in EMERGENCY_STOP;
- no short sales.

The contract test also covers `exit_only`.

## 5. Live check (2026-10-15)

`python -m scheme1.agents.cli --pool 5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9` now also prints the decision and, if an order is proposed, the risk verdict against an empty virtual portfolio of 1,000. BONK: **HOLD**, because it failed the safety check (D13), the risk agent flagged it (−1.00), and the combined score was −0.18 (below +0.35). No order was proposed.

## 6. Open points

- All numbers (5%, 3%, 2 h, 0.35, 10%, 20%, 1% cost) are first guesses until backtesting (Stage 8).
- The desk state machine (when NORMAL becomes DEGRADED, the kill switch) is built with the broker in Stage 7.
