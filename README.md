# SCHEME1 Trading Desk

A multi-agent trading desk you can open in a browser: analyst agents vote, a bull/bear debate weighs them, a signal model calls **BUY / SELL / HOLD** with a probability, a risk desk approves or vetoes, and a paper broker works limit orders. The dashboard shows each stage live.

Everything runs on a **simulated market** with a **paper broker**. Nothing here connects to an exchange or places real orders, and none of it is trading advice.

## Run it

Open `index.html` in a browser. Or serve the folder:

```sh
npm start            # python3 -m http.server 8080, then open http://localhost:8080
npm test             # brain unit tests (Node 18+)
```

## The brain (`brain.js`)

No dependencies. Works in the browser (`window.Brain`) and in Node (`require('./brain.js')`).

| Stage | What it does |
| --- | --- |
| Watch | Simulated market for 5 symbols with trending and choppy regimes, clustered volatility and a 12-level order book. |
| Analysts | Four agents score each symbol from -1 (sell) to +1 (buy): **Technical** (trend and momentum), **Order flow** (book imbalance), **Reversion** (stretch and RSI), **Regime** (30-tick drift). |
| Debate | Adds up the confidence-weighted bull and bear cases and names a winner, or calls it split. |
| Signal | An online logistic regression over the agent votes gives p(up) over the next 8 ticks. It trades only when the expected move beats fees and the edge clears `minEdge`. |
| Risk | Vetoes on vol spikes, cooldowns, position limits, or an 8% drawdown (which flattens and halts the desk). Sizes orders with quarter-Kelly, capped at 8% of equity. |
| Execute | Paper broker with resting limit orders that fill only when price trades through them, maker fees, and average-cost P&L. |
| Report | Every prediction is graded 8 ticks later and the model learns from it. Hit rate, Brier score and a calibration curve are tracked. |

It takes microseconds per decision, and the dashboard shows the measured time.

### Plug in your own agent

Any function that returns a score can join the desk. In the browser console (the dashboard exposes `window.desk`) or in Node:

```js
desk.addAgent({
  id: 'my-agent',
  name: 'My agent',
  role: 'what it watches',
  evaluate({ features, symbol, price, history, book }) {
    const score = Math.tanh(features.mom - features.stretch); // -1 sell … +1 buy
    return { score, confidence: 0.6, note: 'why it voted' };
  },
});
```

A new agent starts at a small weight, and the signal model raises or lowers it based on how its votes line up with what the price actually did. An agent that throws an error gets a 0 vote and can't crash the desk.

## The body (`index.html`)

A single page with no build step. It has the orbital signal engine (callouts for each new order), market tape with sparklines, order-book depth, volatility bars, evidence matrix, agent votes and learned weights, risk gauge with a min-edge slider, calibration chart, live code trace, event log, and stats (hit rate, Brier score, decision time, paper P&L, equity curve).

## Next steps

- Swap the simulated `Market` for real data (for example OpenBB or an exchange websocket) behind the same `features()` interface.
- Put the signal behind a freqtrade or NautilusTrader strategy as a pre-trade gate, in paper mode first.
