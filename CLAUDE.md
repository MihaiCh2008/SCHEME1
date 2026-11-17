# SCHEME1

Self-hosted research and **paper-trading** desk for Solana memecoins. Python ≥3.11, pydantic, stdlib only otherwise.

## Start of every session
1. Read `PROJECT_STATUS.md` (stage table, test count, live checks, next stage) and the newest entries of `DECISIONS.md`.
2. Set up and run the tests: `python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev]' && .venv/bin/python -m pytest -q`.
3. Work on branch `claude/twitter-feature-request-wff62r` unless the user says otherwise.

## Hard rules
- **Simulation only.** No private keys, wallet signing, exchange credentials, order submission or withdrawal code. Live trading is out of scope (`docs/future/LIVE_TRADING.md`, DECISIONS.md D9).
- **Fail closed.** Missing or untrusted data means `unverified`/`HOLD`, never a guess. A token is tradable only if every safety check passes (D12, D13). The holder check leaves out known exchange wallets (D21, list in `src/scheme1/safety/exchange_wallets.py`, keep its license file).
- **No look-ahead.** Only closed candles; a decision at candle *t* fills at the open of *t+1* or later (`docs/ARCHITECTURE.md` section 3).
- **Money is `Decimal`**, statistics are `float` (D5). Contracts in `src/scheme1/contracts.py` are frozen; changing a persisted field bumps `SCHEMA_VERSION`.
- **Provider terms:** GeckoTerminal data is kept at most 24 hours and shown with "Powered by CoinGecko" (D11). Don't commit provider responses.
- **Secrets:** the Solana RPC URL (with its Helius key) comes from `SCHEME1_SOLANA_RPC_URL` in the environment settings. Never print it, log it, or write it to a file.
- Unit tests never use the network; real-data checks are run by hand and recorded in `PROJECT_STATUS.md`.

## Strategy (as decided by the user)
Quick trades of minutes to a few hours, no sniping of new launches (D15). Entry needs a safe token, no blocking risk and a combined agent score ≥ 0.35. Exits: take profit +5%, trailing stop starting −3% and then 2% below the highest price, 2-hour time limit, early exit when the score drops to 0 (D16, D17). Money split 50/50 into two sleeves: **bot** (the strategy above) and **copy** (copying the wallets in `copy_traders.txt`, rules in D22, D23), each with its own ledger and limits. No buys during feature warm-up. Daily loss limit 5% per UTC day (D18). Broker rules in D19; buys go through even after a price jump, because the user trades on FOMO (D20). All numbers are unvalidated until backtesting (Stage 8).

## Layout
`src/scheme1/`: `data/` (GeckoTerminal candles, validation, SQLite store) · `features/` · `safety/` (pool and on-chain checks) · `agents/` (six deterministic agents) · `decision/` (manager, sizing) · `risk/` · `broker/` (paper broker, protective exits) · `accounting/` (ledger) · `desk/` (per-candle step, sleeves, states, audit log, `python -m scheme1.desk.cli`) · `copytrade/` (reading followed wallets, copy rules). Each stage has a doc in `docs/STAGE*.md`.

## Working with the user
- Explain in simple, short English (the user is B2/C1). Use tables and examples.
- Ask before choices that change the strategy or risk rules, and record each one as a new `DECISIONS.md` entry.
- After each step: update `PROJECT_STATUS.md` (stage table, test count), commit, push, and say what's next.
