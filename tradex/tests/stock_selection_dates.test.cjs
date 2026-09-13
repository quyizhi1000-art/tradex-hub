const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/tradex/dashboard/watch/app.js'), 'utf8');
function harness() {
  const elements = new Map();
  const pending = [];
  const rendered = [];
  const panels = [{ hidden: false }];
  const tabs = [{ disabled: false }];
  const context = {
    state: { stockSelectionTradeDate: null, stockSelectionFetchInFlight: false,
      stockSelectionRequestId: 0 },
    asArray: (value) => Array.isArray(value) ? value : [],
    objectValue: (value) => value && typeof value === 'object' ? value : {},
    text: (value, fallback = '--') => typeof value === 'string' && value ? value : fallback,
    byId: (id) => {
      if (!elements.has(id)) elements.set(id, {
        children: [], hidden: false, replaceChildren() { this.children = []; },
        append(child) { this.children.push(child); },
      });
      return elements.get(id);
    },
    createElement: (_tag, _class, textContent) => ({ textContent }),
    document: { querySelectorAll: (selector) => selector.includes('result-contract') ? panels : tabs },
    STOCK_SELECTION_RESULTS_ENDPOINT: '/api/stock-selection/results',
    fetch: (url) => new Promise((resolve) => pending.push({ url, resolve })),
    validateStockSelectionHistory: (payload) => payload,
    renderStockSelectionHistory: (payload) => { rendered.push(payload); panels[0].hidden = false; },
    updateStockSelectionSchedule: () => {},
  };
  vm.createContext(context);
  for (const name of ['renderStockSelectionDateOptions', 'fetchStockSelectionHistory']) {
    const match = source.match(new RegExp(`  (?:async )?function ${name}\\([\\s\\S]*?(?=\\n  (?:async )?function )`));
    assert.ok(match, name);
    vm.runInContext(match[0], context);
  }
  const reply = (index, day, ok = true) => pending[index].resolve({
    ok, status: ok ? 200 : 503,
    json: async () => ({ trade_date: day, results: [{ trade_date: day }], error: 'unavailable' }),
  });
  return { context, pending, rendered, panels, tabs, reply };
}

test('default follows latest archive while explicit historical selection stays pinned', () => {
  const { context: c } = harness();
  c.renderStockSelectionDateOptions({ trade_date: '2026-09-04', dates: [{ trade_date: '2026-09-04' }] });
  assert.equal(c.state.stockSelectionTradeDate, null);
  c.renderStockSelectionDateOptions({ trade_date: '2026-09-07', dates: [{ trade_date: '2026-09-07' }, { trade_date: '2026-09-04' }] });
  assert.equal(c.state.stockSelectionTradeDate, null);
  assert.match(c.byId('stock-selection-date-select').children[0].textContent, /2026-09-07/);
  c.state.stockSelectionTradeDate = '2026-09-04';
  c.renderStockSelectionDateOptions({ trade_date: '2026-09-04', dates: [{ trade_date: '2026-09-07' }, { trade_date: '2026-09-04' }] });
  assert.equal(c.state.stockSelectionTradeDate, '2026-09-04');
});

test('date switch supersedes pending refresh and late old response cannot overwrite it', async () => {
  const h = harness();
  const old = h.context.fetchStockSelectionHistory({ tradeDate: '2026-09-04', silent: true });
  const current = h.context.fetchStockSelectionHistory({ tradeDate: '2026-09-07' });
  assert.equal(h.pending.length, 2);
  assert.equal(h.panels[0].hidden, true);
  h.reply(1, '2026-09-07');
  await current;
  h.reply(0, '2026-09-04');
  await old;
  assert.deepEqual(h.rendered.map(x => x.trade_date), ['2026-09-07']);
});

test('failed or mismatched date never leaves previous candidates visible', async () => {
  for (const ok of [false, true]) {
    const h = harness();
    const request = h.context.fetchStockSelectionHistory({ tradeDate: '2026-09-07' });
    h.reply(0, '2026-09-04', ok);
    await request;
    assert.equal(h.rendered.length, 0);
    assert.equal(h.panels[0].hidden, true);
    assert.equal(h.tabs[0].disabled, true);
  }
});
