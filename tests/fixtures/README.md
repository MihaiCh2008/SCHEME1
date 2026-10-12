# Test fixtures

| File | What it is |
| --- | --- |
| `fixture_pool_5m.json` | **Synthetic.** 12 made-up 5m candles for a made-up pool. Not market data. Used by `FixtureProvider` tests. |
| `geckoterminal_shape_5m.json` | **Synthetic.** Made-up prices in exactly the layout of a real GeckoTerminal OHLCV response (`minute?aggregate=5&limit=5`, newest row first, floats with ~16 significant digits, newest row still open at 18:21:22 UTC). Used to test parsing offline. No real GeckoTerminal data is committed, because their terms don't allow redistributing it (see docs/STAGE3_DATA.md). |
