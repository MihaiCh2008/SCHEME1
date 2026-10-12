/*
 * SCHEME1 brain: the decision engine behind the trading desk.
 *
 * Pipeline per symbol, per tick:
 *   market data -> analyst agents -> bull/bear debate -> signal model -> risk desk -> paper broker
 *
 * The signal model is an online logistic regression over the agents' votes. Every
 * prediction is checked against what the price actually did `horizon` ticks later and
 * the weights are updated, so the desk learns which agents to trust and reports its own
 * hit rate, Brier score and calibration.
 *
 * Everything runs on a simulated market and a paper broker. No real orders, no real money.
 * Works in the browser (window.Brain) and in Node (require('./brain.js')).
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.Brain = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // ---------- helpers ----------
  function mulberry32(seed) {
    return function () {
      seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function gauss(rng) {
    let u = 0;
    while (u === 0) u = rng();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rng());
  }
  const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
  const sigmoid = (z) => 1 / (1 + Math.exp(-z));
  const now = () => (typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now());
  const finite = (x, fallback = 0) => (Number.isFinite(x) ? x : fallback);

  // ---------- simulated market ----------
  // Starting prices are simulation seeds, not live quotes.
  const DEFAULT_SYMBOLS = [
    { symbol: 'BTC', price: 61840, vol: 0.0016 },
    { symbol: 'ETH', price: 2410, vol: 0.0022 },
    { symbol: 'SOL', price: 142.6, vol: 0.0035 },
    { symbol: 'BNB', price: 568.2, vol: 0.002 },
    { symbol: 'DOGE', price: 0.1124, vol: 0.004 },
  ];

  class Market {
    constructor({ symbols = DEFAULT_SYMBOLS, seed = 7 } = {}) {
      this.rng = mulberry32(seed);
      this.t = 0;
      this.assets = symbols.map((s) => ({
        symbol: s.symbol,
        price: s.price,
        open: s.price,
        baseVol: s.vol,
        vol: s.vol,
        drift: 0,
        regime: 'chop',
        regimeLeft: 0,
        flow: 0,
        lastRet: 0,
        history: [s.price],
        absRets: [],
        emaF: s.price,
        emaS: s.price,
        avgGain: 0,
        avgLoss: 0,
        book: null,
      }));
      for (const a of this.assets) this.buildBook(a);
    }

    get(symbol) {
      return this.assets.find((a) => a.symbol === symbol);
    }

    step() {
      this.t++;
      const rng = this.rng;
      for (const a of this.assets) {
        // Regimes: trends and chop of random length.
        if (a.regimeLeft <= 0) {
          const r = rng();
          a.regime = r < 0.3 ? 'up' : r < 0.6 ? 'down' : 'chop';
          a.regimeLeft = 40 + Math.floor(rng() * 160);
        }
        a.regimeLeft--;
        const target = a.regime === 'up' ? 0.35 : a.regime === 'down' ? -0.35 : 0;
        a.drift += (target - a.drift) * 0.05;
        // Order flow is persistent and leans with the regime, so the book carries a little information.
        a.flow = 0.85 * a.flow + 0.15 * (1.5 * a.drift + 0.8 * gauss(rng));
        const shock = gauss(rng);
        // Volatility clusters, with rare spikes.
        a.vol = 0.97 * a.vol + 0.03 * a.baseVol * (0.55 + 0.85 * Math.abs(shock));
        if (rng() < 0.002) a.vol *= 2.2;
        const ret = a.vol * (0.2 * a.drift + 0.12 * a.flow + shock);
        const prev = a.price;
        a.price = prev * Math.exp(ret);
        a.lastRet = ret;
        a.history.push(a.price);
        if (a.history.length > 600) a.history.shift();
        a.absRets.push(Math.abs(ret));
        if (a.absRets.length > 120) a.absRets.shift();
        a.emaF += (2 / 9) * (a.price - a.emaF);
        a.emaS += (2 / 33) * (a.price - a.emaS);
        const gain = Math.max(0, a.price - prev);
        const loss = Math.max(0, prev - a.price);
        a.avgGain += (gain - a.avgGain) / 14;
        a.avgLoss += (loss - a.avgLoss) / 14;
        this.buildBook(a);
      }
    }

    buildBook(a) {
      const mid = a.price;
      const tick = mid * a.vol * 0.25;
      const spread = tick * 2;
      const imb = clamp(Math.tanh(1.2 * a.flow) + 0.25 * gauss(this.rng), -0.95, 0.95);
      const bids = [];
      const asks = [];
      for (let i = 0; i < 12; i++) {
        const depth = 1 + 0.12 * i;
        bids.push({ price: mid - spread / 2 - i * tick, size: Math.max(0.05, (1 + this.rng() * 0.8) * depth * (1 + 0.45 * imb)) });
        asks.push({ price: mid + spread / 2 + i * tick, size: Math.max(0.05, (1 + this.rng() * 0.8) * depth * (1 - 0.45 * imb)) });
      }
      a.book = { mid, spread, bids, asks };
    }
  }

  // ---------- features ----------
  function features(a) {
    const p = a.price;
    const v = Math.max(a.vol, 1e-9);
    const h = a.history;
    const p5 = h[Math.max(0, h.length - 6)];
    const p30 = h[Math.max(0, h.length - 31)];
    const rsi = a.avgLoss === 0 ? (a.avgGain === 0 ? 50 : 100) : 100 - 100 / (1 + a.avgGain / a.avgLoss);
    let bid = 0;
    let ask = 0;
    for (let i = 0; i < 5; i++) { bid += a.book.bids[i].size; ask += a.book.asks[i].size; }
    const f = {
      mom: (a.emaF - a.emaS) / (p * v * 3),
      ret5: Math.log(p / p5) / (v * Math.sqrt(5)),
      ret30: Math.log(p / p30) / (v * Math.sqrt(30)),
      stretch: Math.log(p / a.emaS) / (v * 4),
      rsi: (rsi - 50) / 50,
      imb: (bid - ask) / (bid + ask),
      volZ: v / a.baseVol - 1,
    };
    for (const k in f) f[k] = clamp(finite(f[k]), -4, 4);
    return f;
  }

  // ---------- analyst agents ----------
  // An agent is { id, name, role, evaluate(ctx) -> { score: -1..1, confidence: 0..1, note } }.
  // ctx = { features, symbol, price, history, book }.
  const DEFAULT_AGENTS = [
    {
      id: 'technical', name: 'Technical', role: 'Trend and momentum',
      evaluate({ features: f }) {
        const s = Math.tanh(0.9 * f.mom + 0.35 * f.ret5);
        return { score: s, confidence: 0.35 + 0.65 * Math.abs(s), note: `trend ${s >= 0 ? 'up' : 'down'}, momentum ${f.mom.toFixed(2)}σ` };
      },
    },
    {
      id: 'flow', name: 'Order flow', role: 'Book imbalance',
      evaluate({ features: f }) {
        const s = Math.tanh(1.6 * f.imb);
        return { score: s, confidence: 0.3 + 0.7 * Math.abs(s), note: `book ${f.imb >= 0 ? 'bid' : 'ask'}-heavy ${(Math.abs(f.imb) * 100).toFixed(0)}%` };
      },
    },
    {
      id: 'reversion', name: 'Reversion', role: 'Stretch and RSI',
      evaluate({ features: f }) {
        const s = -Math.tanh(0.6 * f.stretch) * (Math.abs(f.rsi) > 0.4 ? 1 : 0.6);
        return { score: s, confidence: 0.25 + 0.6 * Math.abs(s), note: `stretch ${f.stretch.toFixed(2)}σ, RSI ${(50 + f.rsi * 50).toFixed(0)}` };
      },
    },
    {
      id: 'regime', name: 'Regime', role: '30-tick drift',
      evaluate({ features: f }) {
        const s = Math.tanh(0.7 * f.ret30);
        const calm = f.volZ < 0.5;
        return { score: s, confidence: (calm ? 0.5 : 0.25) + 0.4 * Math.abs(s), note: `${calm ? 'calm' : 'choppy'} tape, 30-tick drift ${f.ret30.toFixed(2)}σ` };
      },
    },
  ];

  function runAgent(agent, ctx) {
    try {
      const out = agent.evaluate(ctx) || {};
      return {
        id: agent.id,
        name: agent.name,
        score: clamp(finite(Number(out.score)), -1, 1),
        confidence: clamp(finite(Number(out.confidence), 0.5), 0, 1),
        note: String(out.note || ''),
      };
    } catch (err) {
      return { id: agent.id, name: agent.name, score: 0, confidence: 0, note: `error: ${err && err.message}`, error: true };
    }
  }

  // ---------- research debate ----------
  function debate(votes) {
    let bull = 0;
    let bear = 0;
    for (const v of votes) {
      if (v.score > 0) bull += v.score * v.confidence;
      else bear += -v.score * v.confidence;
    }
    const consensus = (bull - bear) / (bull + bear + 1e-9);
    const winner = Math.abs(consensus) < 0.15 ? 'split' : consensus > 0 ? 'bull' : 'bear';
    return { bull, bear, consensus, winner };
  }

  // ---------- signal model: online logistic regression ----------
  class SignalModel {
    constructor({ lr = 0.01, l2 = 1e-4 } = {}) {
      this.lr = lr;
      this.l2 = l2;
      this.w = { bias: 0, debate: 0.3 };
    }
    ensure(id, init = 0.3) {
      if (!(id in this.w)) this.w[id] = init;
    }
    vector(votes, deb) {
      const x = { bias: 1, debate: deb.consensus };
      for (const v of votes) x[v.id] = v.score;
      return x;
    }
    predict(x) {
      let z = 0;
      for (const k in x) z += (this.w[k] || 0) * x[k];
      return sigmoid(z);
    }
    update(x, y) {
      const g = this.predict(x) - y;
      for (const k in x) {
        if (!(k in this.w)) continue;
        this.w[k] -= this.lr * (g * x[k] + (k === 'bias' ? 0 : this.l2 * this.w[k]));
      }
    }
  }

  // ---------- paper broker ----------
  class PaperBroker {
    constructor({ cash = 100000, fee = 0.0002, ttl = 6 } = {}) {
      this.startCash = cash;
      this.cash = cash;
      this.fee = fee;
      this.ttl = ttl;
      this.positions = {};
      this.orders = [];
      this.realized = 0;
      this.fees = 0;
      this.peak = cash;
      this.halted = false;
      this.nextId = 1;
    }
    position(symbol) {
      return this.positions[symbol] || (this.positions[symbol] = { qty: 0, avg: 0 });
    }
    equity(market) {
      let e = this.cash;
      for (const s in this.positions) e += this.positions[s].qty * market.get(s).price;
      return e;
    }
    exposure(symbol, market) {
      const eq = this.equity(market);
      return eq > 0 ? (this.position(symbol).qty * market.get(symbol).price) / eq : 0;
    }
    grossExposure(market) {
      const eq = this.equity(market);
      let g = 0;
      for (const s in this.positions) g += Math.abs(this.positions[s].qty * market.get(s).price);
      return eq > 0 ? g / eq : 0;
    }
    drawdown(market) {
      const eq = this.equity(market);
      this.peak = Math.max(this.peak, eq);
      return 1 - eq / this.peak;
    }
    cancel(symbol) {
      const gone = this.orders.filter((o) => o.symbol === symbol);
      this.orders = this.orders.filter((o) => o.symbol !== symbol);
      return gone;
    }
    place({ symbol, side, qty, limit, t }) {
      this.cancel(symbol);
      const order = { id: this.nextId++, symbol, side, qty: Math.abs(qty), limit, t, ttl: this.ttl };
      this.orders.push(order);
      return order;
    }
    fill(symbol, signedQty, px) {
      const pos = this.position(symbol);
      const fee = Math.abs(signedQty) * px * this.fee;
      this.cash -= signedQty * px + fee;
      this.fees += fee;
      const q = pos.qty;
      if (q === 0 || Math.sign(q) === Math.sign(signedQty)) {
        pos.avg = (pos.avg * Math.abs(q) + px * Math.abs(signedQty)) / (Math.abs(q) + Math.abs(signedQty));
        pos.qty = q + signedQty;
      } else {
        const closed = Math.min(Math.abs(q), Math.abs(signedQty));
        this.realized += (px - pos.avg) * closed * Math.sign(q);
        pos.qty = q + signedQty;
        if (Math.abs(pos.qty) < 1e-12) { pos.qty = 0; pos.avg = 0; }
        else if (Math.sign(pos.qty) !== Math.sign(q)) pos.avg = px;
      }
    }
    // Resting limit orders fill when the price trades through them; otherwise they expire.
    onTick(market) {
      const events = [];
      const keep = [];
      for (const o of this.orders) {
        const price = market.get(o.symbol).price;
        const hit = o.side === 'BUY' ? price <= o.limit : price >= o.limit;
        if (hit) {
          this.fill(o.symbol, o.side === 'BUY' ? o.qty : -o.qty, o.limit);
          events.push({ type: 'fill', order: o });
        } else if (--o.ttl <= 0) {
          events.push({ type: 'expire', order: o });
        } else keep.push(o);
      }
      this.orders = keep;
      return events;
    }
    flattenAll(market) {
      this.orders = [];
      for (const s in this.positions) {
        const q = this.positions[s].qty;
        if (q !== 0) this.fill(s, -q, market.get(s).price);
      }
    }
  }

  // ---------- risk desk ----------
  class RiskDesk {
    constructor(cfg = {}) {
      this.cfg = Object.assign({ maxDrawdown: 0.08, maxPosPct: 0.2, maxOrderPct: 0.08, volCeiling: 1.2, cooldown: 4, kellyFraction: 0.25 }, cfg);
      this.lastOrder = {};
    }
    check({ action, conf, symbol, f, broker, market, t }) {
      const c = this.cfg;
      if (action === 'HOLD') return { approved: false, sizePct: 0, reasons: ['no trade: hold'] };
      const reasons = [];
      if (broker.halted) reasons.push('halted: drawdown limit hit');
      if (f.volZ > c.volCeiling) reasons.push(`vol spike ${((f.volZ + 1) * 100).toFixed(0)}% of normal`);
      if (t - (this.lastOrder[symbol] ?? -Infinity) < c.cooldown) reasons.push('cooldown');
      const kelly = (2 * conf - 1) * c.kellyFraction;
      const sizePct = Math.min(c.maxOrderPct, Math.max(0, kelly));
      if (sizePct < 0.005) reasons.push('edge too small to size');
      const cur = broker.exposure(symbol, market);
      const dir = action === 'BUY' ? 1 : -1;
      if (Math.sign(cur) === dir && Math.abs(cur) >= c.maxPosPct - 1e-9) reasons.push('position limit');
      return { approved: reasons.length === 0, sizePct, reasons };
    }
    target(action, sizePct, currentExposure) {
      const dir = action === 'BUY' ? 1 : -1;
      if (Math.sign(currentExposure) === dir) return dir * Math.min(this.cfg.maxPosPct, Math.abs(currentExposure) + sizePct);
      return dir * sizePct;
    }
  }

  // ---------- the desk ----------
  class Desk {
    constructor(opts = {}) {
      this.cfg = Object.assign({ seed: 7, horizon: 8, minEdge: 0.1, symbols: DEFAULT_SYMBOLS, warmup: 400 }, opts);
      this.market = new Market({ symbols: this.cfg.symbols, seed: this.cfg.seed });
      this.model = new SignalModel(opts.model);
      this.broker = new PaperBroker(opts.broker);
      this.risk = new RiskDesk(opts.risk);
      this.agents = [];
      for (const a of DEFAULT_AGENTS) this.addAgent(a, { silent: true });
      this.t = 0;
      this.pending = [];
      this.log = [];
      this.recent = {};
      this.equityCurve = [];
      this.stats = { resolved: 0, brierSum: 0, calls: 0, hits: 0, window: [], calib: Array.from({ length: 10 }, () => ({ n: 0, ups: 0 })), lastLatency: 0, avgLatency: 0 };
      // Learn on the simulated tape before trading so the first screen is meaningful.
      for (let i = 0; i < this.cfg.warmup; i++) this.tick({ trade: false });
      this.equityCurve = [];
      this.log = [];
      this.note('report', '*', `model pre-trained on ${this.cfg.warmup} simulated ticks, hit rate ${(this.hitRate() * 100).toFixed(1)}%`);
    }

    addAgent(agent, { silent = false } = {}) {
      if (!agent || typeof agent.id !== 'string' || typeof agent.evaluate !== 'function') {
        throw new Error('agent needs a string id and an evaluate(ctx) function');
      }
      if (agent.id === 'bias' || agent.id === 'debate') throw new Error(`agent id "${agent.id}" is reserved`);
      this.agents = this.agents.filter((a) => a.id !== agent.id);
      this.agents.push({ id: agent.id, name: agent.name || agent.id, role: agent.role || 'custom', evaluate: agent.evaluate, custom: !DEFAULT_AGENTS.includes(agent) });
      this.model.ensure(agent.id, DEFAULT_AGENTS.includes(agent) ? 0.3 : 0.2);
      if (!silent) this.note('report', '*', `agent "${agent.name || agent.id}" joined the desk`);
      return this;
    }

    removeAgent(id) {
      this.agents = this.agents.filter((a) => a.id !== id);
      return this;
    }

    note(stage, symbol, text, level = 'info') {
      this.log.push({ t: this.t, stage, symbol, text, level });
      if (this.log.length > 200) this.log.shift();
    }

    hitRate() {
      const w = this.stats.window;
      return w.length ? w.reduce((s, x) => s + x, 0) / w.length : 0.5;
    }

    brier() {
      return this.stats.resolved ? this.stats.brierSum / this.stats.resolved : 0.25;
    }

    tick({ trade = true } = {}) {
      this.market.step();
      this.t++;
      if (trade) {
        for (const ev of this.broker.onTick(this.market)) {
          const o = ev.order;
          if (ev.type === 'fill') this.note('execute', o.symbol, `filled ${o.side} ${fmtQty(o.qty)} @ ${fmtPx(o.limit)}`, o.side === 'BUY' ? 'buy' : 'sell');
          else this.note('execute', o.symbol, `order #${o.id} expired unfilled`, 'muted');
        }
      }
      this.resolve();
      const decisions = this.market.assets.map((a) => this.decide(a, trade));
      if (trade) {
        const eq = this.broker.equity(this.market);
        this.equityCurve.push(eq);
        if (this.equityCurve.length > 600) this.equityCurve.shift();
        const dd = this.broker.drawdown(this.market);
        if (!this.broker.halted && dd > this.risk.cfg.maxDrawdown) {
          this.broker.halted = true;
          this.broker.flattenAll(this.market);
          this.note('risk', '*', `drawdown ${(dd * 100).toFixed(1)}% breached the ${(this.risk.cfg.maxDrawdown * 100).toFixed(0)}% limit; flattened and halted`, 'sell');
        }
      }
      return { t: this.t, decisions };
    }

    decide(a, trade) {
      const t0 = now();
      const f = features(a);
      const ctx = { features: f, symbol: a.symbol, price: a.price, history: a.history, book: a.book };
      const votes = this.agents.map((ag) => runAgent(ag, ctx));
      const deb = debate(votes);
      const x = this.model.vector(votes, deb);
      const p = this.model.predict(x);
      const edge = Math.abs(2 * p - 1);
      const expectedMove = a.vol * Math.sqrt(this.cfg.horizon) * 0.8;
      const cost = 2 * this.broker.fee;
      let action = 'HOLD';
      if (edge >= this.cfg.minEdge && edge * expectedMove > cost) action = p > 0.5 ? 'BUY' : 'SELL';
      const conf = action === 'HOLD' ? 1 - edge : Math.max(p, 1 - p);
      const risk = this.risk.check({ action, conf, symbol: a.symbol, f, broker: this.broker, market: this.market, t: this.t });
      let order = null;
      if (trade && risk.approved) {
        const eq = this.broker.equity(this.market);
        const cur = this.broker.exposure(a.symbol, this.market);
        const target = this.risk.target(action, risk.sizePct, cur);
        const qty = ((target - cur) * eq) / a.price;
        if (Math.abs(qty * a.price) > eq * 0.002) {
          const side = qty > 0 ? 'BUY' : 'SELL';
          const limit = side === 'BUY' ? a.price - a.book.spread * 0.25 : a.price + a.book.spread * 0.25;
          order = this.broker.place({ symbol: a.symbol, side, qty, limit, t: this.t });
          this.risk.lastOrder[a.symbol] = this.t;
        }
      }
      const latency = now() - t0;
      this.stats.lastLatency = latency;
      this.stats.avgLatency = this.stats.avgLatency ? 0.98 * this.stats.avgLatency + 0.02 * latency : latency;
      this.pending.push({ t: this.t, symbol: a.symbol, x, p, action, price: a.price });

      const rec = { t: this.t, symbol: a.symbol, price: a.price, features: f, votes, debate: deb, p, action, conf, risk, order, latency };
      const list = this.recent[a.symbol] || (this.recent[a.symbol] = []);
      list.push(rec);
      if (list.length > 24) list.shift();

      if (trade && action !== 'HOLD') {
        this.note('signal', a.symbol, `${action} p(up)=${p.toFixed(2)}; ${deb.winner === 'split' ? 'debate split' : deb.winner + ' case won'}`, action === 'BUY' ? 'buy' : 'sell');
        if (order) this.note('risk', a.symbol, `approved ${(risk.sizePct * 100).toFixed(1)}% of equity; ${order.side} ${fmtQty(order.qty)} limit ${fmtPx(order.limit)}`);
        else if (!risk.approved && !risk.reasons.includes('cooldown')) this.note('risk', a.symbol, `vetoed: ${risk.reasons.join(', ')}`, 'muted');
      }
      return rec;
    }

    // Grade predictions whose horizon has passed and train the model on them.
    resolve() {
      const H = this.cfg.horizon;
      const s = this.stats;
      while (this.pending.length && this.t - this.pending[0].t >= H) {
        const pr = this.pending.shift();
        const asset = this.market.get(pr.symbol);
        if (!asset) continue;
        const y = asset.price > pr.price ? 1 : 0;
        this.model.update(pr.x, y);
        s.resolved++;
        s.brierSum += (pr.p - y) ** 2;
        const bin = s.calib[Math.min(9, Math.floor(pr.p * 10))];
        bin.n++;
        bin.ups += y;
        if (pr.action !== 'HOLD') {
          const hit = (pr.action === 'BUY') === (y === 1) ? 1 : 0;
          s.calls++;
          s.hits += hit;
          s.window.push(hit);
          if (s.window.length > 300) s.window.shift();
        }
        if (s.resolved % 500 === 0 && this.log) {
          this.note('report', '*', `checkpoint: ${s.resolved} predictions graded, hit rate ${(this.hitRate() * 100).toFixed(1)}%, Brier ${this.brier().toFixed(3)}`);
        }
      }
    }

    snapshot() {
      const m = this.market;
      const eq = this.broker.equity(m);
      return {
        t: this.t,
        equity: eq,
        pnl: eq - this.broker.startCash,
        realized: this.broker.realized,
        fees: this.broker.fees,
        drawdown: 1 - eq / this.broker.peak,
        gross: this.broker.grossExposure(m),
        halted: this.broker.halted,
        hitRate: this.hitRate(),
        lifetimeHitRate: this.stats.calls ? this.stats.hits / this.stats.calls : 0.5,
        brier: this.brier(),
        calls: this.stats.calls,
        resolved: this.stats.resolved,
        latency: this.stats.avgLatency,
        weights: Object.assign({}, this.model.w),
        calib: this.stats.calib.map((b) => ({ n: b.n, ups: b.ups })),
      };
    }
  }

  function fmtPx(p) {
    return p >= 1000 ? p.toFixed(1) : p >= 10 ? p.toFixed(2) : p >= 1 ? p.toFixed(3) : p.toFixed(5);
  }
  function fmtQty(q) {
    const a = Math.abs(q);
    return a >= 1000 ? a.toFixed(0) : a >= 1 ? a.toFixed(2) : a.toFixed(4);
  }

  return { Desk, Market, SignalModel, PaperBroker, RiskDesk, DEFAULT_AGENTS, DEFAULT_SYMBOLS, features, debate, fmtPx, fmtQty, mulberry32 };
});
