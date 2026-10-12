# SCHEME1

A self-hosted research and **paper-trading** desk for crypto. Market data goes through validation, quantitative features, research agents, a decision, independent risk checks and a simulated broker, and every step is persisted for audit.

**Simulation only.** There is no real-money execution path, no exchange credentials and no withdrawals. Nothing here is trading advice.

## Where things are

| Path | What |
| --- | --- |
| [PROJECT_STATUS.md](PROJECT_STATUS.md) | Which stages are done, real test results, blockers |
| [docs/STAGE1_AUDIT.md](docs/STAGE1_AUDIT.md) | Audit of TradingAgents, OpenBB, Freqtrade, NautilusTrader, QuantDinger |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Data flow, module contracts, fill and risk rules |
| [DECISIONS.md](DECISIONS.md) | Why each architectural choice was made |
| `src/scheme1/` | Python package (contracts so far) |
| `tests/` | pytest suite |
| `prototype/` | Earlier JS visual prototype on a **synthetic** market (not part of this system) |

## Setup

Requires Python 3.11 or newer.

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```
