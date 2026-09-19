const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/tradex/dashboard/watch/app.js'), 'utf8');
function harness() {
  const elements = new Map();
  const c = {
    state: { stockSelectionHistory: { dates: [], catalog: { strategies: [
      { strategy_id: 'a', strategy_version: 'v1', result_contract: 'a' },
      { strategy_id: 'b', strategy_version: 'v1', result_contract: 'b' },
    ] } } },
    minutes: 18 * 60 + 40,
    shanghaiClock: () => ({ date: '2026-09-17', minutes: c.minutes }),
    asArray: v => Array.isArray(v) ? v : [],
    objectValue: v => v && typeof v === 'object' ? v : {},
    text: (v, fallback = '--') => typeof v === 'string' && v ? v : fallback,
    byId: id => { if (!elements.has(id)) elements.set(id, {}); return elements.get(id); },
    renderStockSelectionDateOptions() {}, renderStockSelectionStrategyTabs() {},
    selectStockSelectionTab() {}, formatCount: v => v || 0,
    STOCK_SELECTION_GENERATE_ENDPOINT: '/api/daily-stock-selection',
    validateStockSelectionGeneration: v => v,
    pollStockSelectionGeneration: async () => {},
  };
  vm.createContext(c);
  for (const name of ['stockSelectionDefinitions', 'reviewScheduleMinutes',
    'updateStockSelectionSchedule', 'renderStockSelectionHistory', 'generateStockSelection']) {
    const match = source.match(new RegExp(`  (?:async )?function ${name}\\([\\s\\S]*?(?=\\n  (?:async )?function )`));
    assert.ok(match, name); vm.runInContext(match[0], c);
  }
  return c;
}

test('manual retry stays available after automatic time when only some strategies exist', () => {
  const c = harness();
  c.state.stockSelectionHistory.dates = [{ trade_date: '2026-09-17', strategy_count: 1 }];
  c.state.stockSelectionTradeDate = '2026-09-16';
  c.updateStockSelectionSchedule();
  assert.equal(c.byId('stock-selection-generate-button').disabled, false);
  assert.equal(c.byId('stock-selection-generate-button').textContent, '手动补生成今日结果');
  c.state.stockSelectionHistory.dates[0].strategy_count = 2;
  c.updateStockSelectionSchedule();
  assert.equal(c.byId('stock-selection-generate-button').disabled, true);
});

test('manual time gate and active job prevent duplicate generation', () => {
  const c = harness();
  c.minutes = 17 * 60 + 59;
  c.updateStockSelectionSchedule();
  assert.equal(c.byId('stock-selection-generate-button').disabled, true);
  c.minutes = 18 * 60;
  c.updateStockSelectionSchedule();
  assert.equal(c.byId('stock-selection-generate-button').disabled, false);
  c.state.stockSelectionGenerateInFlight = true;
  c.updateStockSelectionSchedule();
  assert.equal(c.byId('stock-selection-generate-button').disabled, true);
});

test('partial completion remains visibly incomplete and retryable', () => {
  const c = harness();
  c.renderStockSelectionHistory({ ...c.state.stockSelectionHistory, trade_date: '2026-09-17',
    results: [{ strategy_id: 'a', strategy_version: 'v1' }] });
  assert.match(c.byId('stock-selection-status').textContent, /仍有 1 项策略未生成/);
  assert.equal(c.byId('stock-selection-generate-button').disabled, false);
});

test('manual click submits once and shows today after browsing a historical date', async () => {
  const c = harness();
  c.state.stockSelectionTradeDate = '2026-09-16';
  let resolve;
  const calls = [];
  c.fetch = (url, options) => {
    calls.push({ url, options });
    return new Promise(r => { resolve = r; });
  };
  const request = c.generateStockSelection();
  await c.generateStockSelection();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[0].options.body, '{}');
  resolve({ ok: true, json: async () => ({ state: 'queued' }) });
  await request;
  assert.equal(c.state.stockSelectionTradeDate, '2026-09-17');
});
