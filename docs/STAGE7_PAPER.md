# Stage 7: Paper execution and accounting

_Built and checked 2026-11-17. Simulation only: there is no wallet, no key and no order-sending code anywhere._

One **desk step** per closed candle and pool runs the whole chain from Stages 3–6 and then simulates the fills:

```
candle closes → broker (resting orders, time limit, protective exits) → ledger → mark to close → snapshot
             → agents → decide() → authorize() → risk engine → new order (fills from the next candle on)
```

The step is written to SQLite in **one transaction**: it is stored whole or not at all.

## 1. Paper broker (`scheme1.broker.paper`)

Fill rules (ARCHITECTURE.md section 3, plus the choices in DECISIONS.md D19):

| Rule | Value |
| --- | --- |
| When an order can first fill | the candle that opens after the decision (no look-ahead) |
| Buy price | candle open + 0.25% slippage |
| Sell price | candle open − 0.25% slippage (protective exits: the stop or take-profit price − 0.25%) |
| Fee | 0.25% of every fill (DEX pool fee). A round trip costs about 1%, as the strategy assumes |
| Volume limit | all our fills in one candle together ≤ 5% of the candle's USD volume |
| No volume | a candle with no trades fills nothing |
| Entry price protection | **off** (D20): the strategy buys on FOMO, so a buy goes through even after a price jump. It can be switched on for tests (then a buy is skipped when the open is more than X% above the decision price) |
| Entry lifetime | 1 candle: what can't fill in the first candle is dropped (EXPIRED) |
| Exit lifetime | forever: a sell rests until it's filled or there's nothing left to sell |
| Cash | a buy never spends more than the cash there is |

**Exits run by the broker itself** (not by `decide()`), every candle, from the entry candle on:
- **Trailing stop and take profit** inside each candle (D16, D17). The stop starts 3% below the entry price. After each candle it moves up to 2% below the highest price since entry. Since the entry candle's high is at least its open, **from the second candle on the stop is at most 2% below the highest price**: the −3% only covers the entry candle.
- **Time limit:** a position held 2 hours or more is sold at the next candle's open.
- An exit too big for the candle's volume sells the rest at the next opens.

These exits were planned when the entry was approved and only reduce risk, so they don't go through the risk engine again, and they work even when the data is degraded (see the Stage 6 safety review).

## 2. Ledger (`scheme1.accounting`)

- Cash, positions, fees and profit in USD, as `Decimal`.
- A position's cost includes the buy fee; **profit is counted after both fees**.
- Money is rounded to 0.00000001: costs and fees up, proceeds down (never in our favour).
- Checked on every fill, before anything changes: cash never below zero, no selling what isn't held, and **cash + open cost − realized profit = start cash**. A broken check puts the desk in EMERGENCY_STOP.
- `round_trips()` turns fills into trades (bought → sold back to zero) with profit, % and exit reason.

## 3. Desk (`scheme1.desk.desk`)

**States** (strongest first). Outside NORMAL there are no new entries, but exits keep working.

| State | When | Cleared by |
| --- | --- | --- |
| EMERGENCY_STOP | kill switch, books that don't balance, a broken audit log, or books that don't match the last snapshot after a restart. Cancels all orders; nothing trades | `reset`, only if the log and the books check out |
| PAUSED | the operator, or equity 20% below its peak | `resume` (the peak restarts from current equity) |
| DEGRADED | a watched pool's data isn't OK | itself, when the data is OK again |
| NORMAL | otherwise | |

**Daily loss limit (D18):** no new buys once equity is 5% below where it was when the UTC day began (00:00 UTC = 01:00 or 02:00 in Central Europe). Exits are always allowed. It resets by itself the next day.

**No buys during warm-up:** `decide()` buys only when every feature can be computed (64 candles). Found in this stage: without it, the first synthetic test bought after 14 candles, on RSI alone.

**Restart:** the desk rebuilds the books from the fills in the audit log and compares them with the last stored snapshot. If the working state was lost, the protective exits of open positions are rebuilt from their entry fill.

## 4. Storage (`scheme1.desk.store`)

- **Audit log:** every desk event, decision, order intent, risk verdict, order version, fill and portfolio snapshot, in order. Each row carries `sha256(previous hash | row)`, so changing or deleting any row is detected (`verify`), and the database itself refuses UPDATE and DELETE on it.
- **What's kept for good** follows docs/STAGE3_DATA.md section 1a, item 8: only our own outputs (signals and their reasons, risk verdicts, orders, fills, equity). Feature values and agent evidence (numbers derived from GeckoTerminal data) are kept **24 hours**, like the candles. Snapshots are stored without the candle prices.
- **Working state** (flags, last prices, protective levels, peaks) is overwritten in place and only needed to resume.

## 5. Command

```
python -m scheme1.desk.cli run --pool <pool address> [--pool ...]   # runs until Ctrl+C
python -m scheme1.desk.cli status        # equity, positions, open orders, trades, integrity
python -m scheme1.desk.cli pause --reason "..." | resume | kill --reason "..." | reset | verify
```

`run` waits for each 5-minute candle to close (+20 s), fetches candles, checks token safety and runs a step for each new candle. Decisions are made on the newest candle only. Candles missed while the desk was off only run through the broker. A new desk starts from the newest candle and never replays the past. `--db` (default `desk.db`) holds everything.

## 6. Checks

New tests (232 in total after D20, all passing, no network):

| File | Tests | What |
| --- | --- | --- |
| `test_ledger.py` | 7 | fees, partial sells, invariants, exact rebuild from fills, trade report |
| `test_paper_broker.py` | 17 | next-open fills, volume cap, price-jump protection, cash cap, stop in the entry candle, trailing then take profit, time limit, leftover exits, desk states, kill switch, restart of the trailing stop |
| `test_desk_store.py` | 9 | order, rollback on failure, append-only, tampering (edit, delete, broken link), 24 h purge |
| `test_desk.py` | 12 | a full trade end to end; a restart mid-trade gives exactly the same result; repeated candle; bad data; kill and reset; tampering and mismatched books stop the desk; drawdown pause; lost state; retention; exits while paused |
| `test_desk_cli.py` | 2 | the live loop (newest candle first, catch-up without old decisions); status and controls |
| contracts, risk, decision | +12 | order states, daily loss limit, warm-up rule, real Token-2022 mint |

**Synthetic run** (steady uptrend, then a drop): first buy only after warm-up, filled at the next open; the trailing stop sold at +0.36% after costs; books balanced; 153 audit records, chain intact.

## 7. Live check (2026-11-17, cloud session)

- `run --pool <BONK/SOL> --steps 1`: **HOLD (token failed safety checks)**, equity 1000.00. Works end to end on live data.
- Safety check on well-known memecoin pools, to find one the desk could trade:

| Pool | Holder check (limit: top 10 ≤ 30%, largest ≤ 10%) | Result |
| --- | --- | --- |
| WIF/SOL | top 10 45.5%, largest 17.1% | blocked |
| POPCAT/SOL | top 10 39.5%, largest 13.0% | blocked |
| Fartcoin/SOL | top 10 **31.3%**, largest 6.2% | blocked (just above the limit) |
| MEW/SOL | top 10 65.4%, largest 38.1% | blocked |
| BONK/SOL | top 10 38.4%, largest 8.8% | blocked |
| TTWO/SOL (tokenized stock) | top 10 12.8% | blocked: mint and freeze authority kept, 5 dangerous extensions |

So with the strict holder rule (D13), **none** of these can be traded. **Update (D21, option b):** with known exchange wallets left out, Fartcoin/SOL passes (top 10 wallets 27.3% after leaving out Gate.io 5.6% and MEXC 1.2%); the others still fail. First desk pass on it: agents bullish, but HOLD because nobody had traded in the pool for 15 minutes (stale data). The pools that are hours old are blocked by the 24-hour age rule on purpose. This is a decision for the user (see PROJECT_STATUS.md).

**Bug found and fixed:** the Token-2022 program id in `scheme1.safety.solana` was wrong since Stage 4b (`…1PS1H7nPZo8q1bD` instead of `TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb`). Every Token-2022 token read as "not a mint" and was blocked with the misleading reason "RPC unavailable". It never let an unsafe token through. The test now uses the shape of the real TTWO response.

## 8. Known limits

- Fills use candle prices. Real DEX trades also move the price by their own size (price impact); the 5% volume cap and 0.25% slippage only approximate that.
- No limit orders (v1: market orders only).
- `correlated_exposure` is still not built (Stage 6).
- Candles missed while the desk was off are run through the broker as if it had been watching, which a stopped bot couldn't do.
- All numbers are first guesses until Stage 8 (backtesting).
