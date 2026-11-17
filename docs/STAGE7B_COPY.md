# Stage 7b: Two sleeves and copy trading

_Built 2026-11-17. Simulation only: the desk reads public blockchain data and never signs or sends anything._

The user asked for two strategies with separate money (DECISIONS.md D22, D23):
- **bot**: the desk's own FOMO trades on watched pools (Stages 5–7).
- **copy**: copy the trades of the wallets the user follows on the pump.fun app (`copy_traders.txt`).

## 1. The split (`scheme1.desk.desk`)

| | bot | copy |
| --- | --- | --- |
| Start | 50% of the money | 50% |
| Size per trade | 10% of its equity | 5% of its equity |
| Coins | watched pools, 24 h old, liquidity ≥ $25k (two sources) | the coins the traders buy; no age rule, liquidity ≥ $5k (one source is enough) |
| Entry | FOMO signal at a 5-minute close, filled at the next open | right away, at the current pool price |
| Exits | take profit +5%, trailing stop (−3%, then 2% under the high), 2 h, score ≤ 0 | the copied trader sells; trailing stop (−20%, then 25% under the high); 24 h; no take profit |
| Costs per fill | fee 0.25% + slippage 0.25% | fee 1% + slippage 1% (pump.fun's fee is about 1%) |
| Daily loss limit | 5% of its equity | 5% of its equity |
| Max positions | gross exposure ≤ 50% | 5 |

**Logic:**
1. **Two ledgers.** Each sleeve has its own cash, positions, profit and limits, so one can't eat the other's money. A token is held by one sleeve at a time.
2. **50/50 at the start**, because nobody knows yet which works better.
3. **Smaller copy trades**: pump.fun coins swing ±50% fast, so a smaller size means a similar risk.
4. **Results decide (rebalancing):** once both sleeves have **30 closed trades** since the last rebalance and **30 days** have passed, up to **10% of total equity** moves from the sleeve with the lower return to the one with the higher return. Each always keeps at least **20%**. Only free cash moves. Every move is a `transfer` record in the audit log, and a restart replays it.
5. The 20% drawdown pause and the kill switch act on the whole desk.

## 2. Reading the traders (`scheme1.copytrade`)

- Every minute: `getSignaturesForAddress` for each wallet since the last one seen, then `getTransaction` for each new one (version 1 transactions exist since 2026, so the code asks for them). About **300,000 Helius credits a month** for 4 traders (1 credit per call; the free plan has 1 million).
- **Only trades the trader signed.** Busy wallets get a lot of spam: in the live check, 40 of trader 1's last 40 transactions were other people's.
- A trade = exactly one token up and SOL/USDC/USDT down (BUY), or the reverse (SELL). A stablecoin wins over SOL, because every transaction also costs a little SOL in fees (trader 3 trades in USDC). Amounts are exact.
- The first read of a wallet only remembers "now": trades from before the desk started are never copied.
- Each wallet's position is saved (`trader_cursors`), so a restart continues where it stopped.

## 3. Copying (`PaperDesk.on_trader_trade`)

**A followed trader buys** → the desk buys right away when all of these hold:
- the desk is NORMAL (not paused or stopped)
- the trade is at least **0.1 SOL** (or $10)
- we don't hold the token, and fewer than **5** copy positions are open
- the token's most traded pool passes the **copy safety checks**: mint and freeze authority revoked, safe token extensions, holders (exchanges and pool vaults left out), base token agrees, liquidity ≥ $5k
- the risk engine approves (copy sleeve's limits)

The fill price is the **higher** of GeckoTerminal's and DexScreener's current price, + 1% slippage, + 1% fee. The trailing stop and time limit start with the **first full minute after the buy**: the buy's own minute also has prices from before the buy.

**The same trader sells** (any amount) → we sell the whole position right away, at the **lower** current price − 1% slippage. If no price can be read, it sells at the next 1-minute open. Another trader's sell doesn't count.

**The bot's own decision is skipped** for a token the copy sleeve holds.

## 4. Running it

```
python -m scheme1.desk.cli run --traders copy_traders.txt --pool <bot pool>   # both sleeves
python -m scheme1.desk.cli status        # both sleeves, positions (with the trader they copy), trades
```

All GeckoTerminal requests share one paced client, **8 a minute** (Stage 4 used two separate clients, which could together pass the ~10/minute limit). Per minute: the copy positions' 1-minute candles (≤ 5), one lookup per new copy buy, and the bot pools every 5 minutes.

`copy_traders.txt` is in the repository, which is public: anyone can see which wallets are followed.

## 5. Checks

New tests (266 in total, no network):

| File | Tests | What |
| --- | --- | --- |
| `test_copytrade_watch.py` | 14 | pump.fun buy in SOL, sell paid in wrapped SOL, USDC buy (fee isn't the payment), not signed, failed, transfers, two tokens; first poll marks "now", spam paging and the cap, a transaction not ready yet, one broken wallet, the trader file |
| `test_copytrade_desk.py` | 15 | copy buy with the copy sleeve's costs; every reason to skip; one token per sleeve; 5 positions max; the copied trader's sell; another trader's sell is ignored; sell without a price; trailing stop on 1-minute candles (the buy's own minute ignored); 24 h limit; sleeves after a restart; the bot skips a copy-held token; rebalancing and its replay; weights |
| `test_desk_cli.py` | +1 | the copy loop: no lookups for a skipped buy, cursors saved, candles of the position, sell |
| `test_ledger.py` | +1 | capital moves keep the books balanced and replay exactly |
| `test_desk.py` | +1 | a late live decision never fills at an older price (see section 7) |

## 6. Live check (2026-11-17)

- **Reading:** the last 40 transactions of each wallet: trader 3 trades in USDC, $300–500 a trade, and once sold everything within 4 minutes; trader 4 buys pump.fun coins for 1 SOL; traders 1 and 2 had no own trades among them (only spam).
- **Safety on trader 4's recent coins:** all 4 had mint and freeze authority revoked and only metadata extensions (Token-2022, readable since the program-id fix in Stage 7); 3 of 4 passed the holder check; liquidity $3k–28k.
- **Desk run:** see PROJECT_STATUS.md.

## 7. Known limits

- The desk sees a trade up to about a minute late (polling). On pump.fun the price may already have moved a lot; the fill uses the price at that moment, so this delay is in the results.
- Immediate fills have no volume cap: copy trades are small (about $25) next to the pools ($5k+).
- When the trader sells only part, we sell everything.
- If the working state is lost, a copy position's protective exits are rebuilt, but it no longer knows which trader it copied (so the trader's sell is not followed for it).
- **Fixed during the live check:** a live decision made long after its candle closed (the desk started mid-period: decision at 21:23 for a candle that closed at 21:20) could fill at the open of 21:20, a price from before the decision. Now a decision fills at the next open only when it is made within 60 seconds of the close (`DeskConfig.fill_grace`); otherwise at the first open after the decision. Backtests are unchanged (decisions there are made exactly at the close).
- All numbers are first guesses until Stage 8 (backtesting).
