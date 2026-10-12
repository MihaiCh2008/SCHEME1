# Test fixtures

| File | What it is |
| --- | --- |
| `fixture_pool_5m.json` | **Synthetic.** 12 made-up 5m candles for a made-up pool. Not market data. Used by `FixtureProvider` tests. |
| `geckoterminal_bonk_sol_5m_recorded.json` | **Recorded real response**, unmodified: GeckoTerminal OHLCV for the BONK/SOL Orca pool `5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9`, `minute?aggregate=5&limit=5&currency=usd&token=base`, fetched 2026-10-12 about 18:21 UTC. The newest row (18:20) was still open when recorded. Used offline to test parsing against the real response shape. |
