'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Brain = require('../brain.js');

test('same seed gives the same tape and decisions', () => {
  const a = new Brain.Desk({ seed: 3, warmup: 50 });
  const b = new Brain.Desk({ seed: 3, warmup: 50 });
  for (let i = 0; i < 100; i++) { a.tick(); b.tick(); }
  assert.equal(a.market.get('BTC').price, b.market.get('BTC').price);
  assert.equal(a.snapshot().equity, b.snapshot().equity);
});

test('probabilities stay in [0,1] and actions are valid', () => {
  const d = new Brain.Desk({ seed: 11, warmup: 100 });
  for (let i = 0; i < 300; i++) {
    for (const r of d.tick().decisions) {
      assert.ok(r.p >= 0 && r.p <= 1);
      assert.ok(['BUY', 'SELL', 'HOLD'].includes(r.action));
      assert.ok(r.conf >= 0 && r.conf <= 1);
    }
  }
});

test('model learns an edge on the simulated tape', () => {
  const d = new Brain.Desk({ seed: 5, warmup: 4000 });
  const s = d.snapshot();
  assert.ok(s.lifetimeHitRate > 0.55, `hit rate ${s.lifetimeHitRate}`);
  assert.ok(s.brier < 0.25, `brier ${s.brier}`);
  assert.ok(s.weights.technical > 0, 'trend agent should earn positive weight');
});

test('paper broker accounting: round trip at one price costs only fees', () => {
  const b = new Brain.PaperBroker({ cash: 1000, fee: 0.001 });
  b.fill('X', 2, 100);
  b.fill('X', -2, 100);
  assert.equal(b.position('X').qty, 0);
  assert.ok(Math.abs(b.cash - (1000 - 0.4)) < 1e-9);
  b.fill('X', 1, 100);
  b.fill('X', -3, 110);
  assert.equal(b.position('X').qty, -2);
  assert.equal(b.position('X').avg, 110);
  assert.ok(Math.abs(b.realized - 10) < 1e-9);
});

test('risk desk vetoes when halted and sizes with Kelly otherwise', () => {
  const d = new Brain.Desk({ seed: 1, warmup: 20 });
  const f = { volZ: 0 };
  const ok = d.risk.check({ action: 'BUY', conf: 0.7, symbol: 'BTC', f, broker: d.broker, market: d.market, t: 1000 });
  assert.equal(ok.approved, true);
  assert.ok(Math.abs(ok.sizePct - 0.08) < 1e-9);
  d.broker.halted = true;
  const no = d.risk.check({ action: 'BUY', conf: 0.7, symbol: 'BTC', f, broker: d.broker, market: d.market, t: 2000 });
  assert.equal(no.approved, false);
});

test('custom agents plug in, get a weight, and cannot crash the desk', () => {
  const d = new Brain.Desk({ seed: 2, warmup: 20 });
  d.addAgent({ id: 'mine', name: 'Mine', evaluate: () => ({ score: 0.5, confidence: 0.9, note: 'hi' }) });
  d.addAgent({ id: 'broken', evaluate: () => { throw new Error('boom'); } });
  const r = d.tick().decisions[0];
  assert.ok('mine' in d.model.w);
  assert.equal(r.votes.find((v) => v.id === 'mine').score, 0.5);
  assert.equal(r.votes.find((v) => v.id === 'broken').score, 0);
  assert.throws(() => d.addAgent({ id: 'bias', evaluate() {} }));
});

test('trading run stays solvent and logs activity', () => {
  const d = new Brain.Desk({ seed: 9 });
  for (let i = 0; i < 1500; i++) d.tick();
  const s = d.snapshot();
  assert.ok(Number.isFinite(s.equity) && s.equity > 0);
  assert.ok(d.log.length > 0);
  console.log(`  equity ${s.equity.toFixed(0)}, hit ${(s.hitRate * 100).toFixed(1)}%, brier ${s.brier.toFixed(3)}, calls ${s.calls}, dd ${(s.drawdown * 100).toFixed(1)}%, halted ${s.halted}, weights ${JSON.stringify(Object.fromEntries(Object.entries(s.weights).map(([k, v]) => [k, +v.toFixed(2)])))}`);
});
