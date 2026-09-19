const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/tradex/dashboard/watch/app.js'), 'utf8');
function harness() {
  const elements = new Map();
  const element = (tag, cls, textContent) => ({tag, textContent, children: [], hidden: false,
    attributes: {}, querySelector() {return null;}, setAttribute(k, v) { this.attributes[k] = v; },
    append(...items) {this.children.push(...items);}, replaceChildren() {this.children = [];} });
  const c = {state: {stockSelectionIndustry: null}, createElement: element,
    byId(id) {if (!elements.has(id)) elements.set(id, element()); return elements.get(id);},
    asArray: v => Array.isArray(v) ? v : [], objectValue: v => v && typeof v === 'object' ? v : {},
    text: (v, fallback) => v || fallback, formatCount: String, formatLevel: String,
    replaceTextList() {}, renderStockSelectionExclusions() {}};
  vm.createContext(c);
  for (const name of ['finiteNumber', 'stockSelectionNameLink', 'stockSelectionIndustryName',
    'stockSelectionDisplayCandidates', 'stockSelectionVisibleCandidates', 'appendMacdPriceEvidence', 'renderMacdPending', 'renderMacdJScreen']) {
    const match = source.match(new RegExp(`  function ${name}\\([\\s\\S]*?(?=\\n  function )`));
    assert.ok(match, name); vm.runInContext(match[0], c);
  }
  return c;
}
const payload = {contract: 'stock_macd_j_screen.v1', schema_version: 1, quality: 'degraded',
  board_eligible_count: 10, evaluated_count: 8, matched_count: 2, same_day_count: 1, prior_3_sessions_count: 1,
  candidates: ['same_day', 'prior_3_sessions'].map((signal_group, index) => ({
    instrument_id: `60000${index}.SH`, name: `样本${index}`, reference_close: 10, industry_block_name: '银行',
    signal_group, gap_sessions: index, signal_trade_date: '2026-09-16', j_turn_date: index ? '2026-09-15' : '2026-09-16',
    zero_axis_zone: index ? 'below_zero' : 'above_zero', j_trough: 10, j_turn_value: 18, low_j_tags: ['J<20'],
    evidence: [{trade_date: '2026-09-15', dif: -.1, dea: 0, k: 30, d: 35, j: 20},
      {trade_date: '2026-09-16', dif: .1, dea: 0, k: 40, d: 35, j: 50}],
  }))};
test('group filter, zero-axis labels, evidence and quote links are visible', () => {
  const c = harness(); c.renderMacdJScreen(payload);
  assert.equal(c.byId('stock-macd-j-table-body').children.length, 2);
  const rendered = JSON.stringify(c.byId('stock-macd-j-table-body'));
  for (const text of ['零轴上方', '零轴下方', 'J<20', '2026-09-15', 'DIF', 'https://stockpage.10jqka.com.cn/600000/']) assert.ok(rendered.includes(text), text);
  for (const group of ['same_day', 'prior_3_sessions']) {
    c.byId('stock-macd-j-group').value = group; c.renderMacdJScreen(payload);
    assert.equal(c.byId('stock-macd-j-table-body').children.length, 1);
    assert.ok(JSON.stringify(c.byId('stock-macd-j-table-body')).includes(group === 'same_day' ? '样本0' : '样本1'));
  }
  c.state.stockSelectionIndustry = '煤炭'; c.renderMacdJScreen(payload);
  assert.match(JSON.stringify(c.byId('stock-macd-j-table-body')), /没有命中股票/);
});
test('older missing archive clears rows; unavailable is distinct from zero matches', () => {
  const c = harness(); c.renderMacdJScreen(payload); c.renderMacdJScreen({});
  assert.equal(c.byId('stock-macd-j-table-body').children.length, 0);
  assert.equal(c.byId('stock-macd-j-content').hidden, true);
  assert.equal(c.byId('stock-macd-j-empty').hidden, false);
  c.renderMacdJScreen({...payload, quality: 'unavailable', candidates: []});
  assert.match(JSON.stringify(c.byId('stock-macd-j-table-body')), /不能判定/);
});

test('v4 shows separate pending warnings and price evidence with inclusive MA5 rule', () => {
  const c=harness();
  const row={...payload.candidates[0],high_60:84.7,high_60_date:'2026-08-20',drawdown_60_pct:-31.157,ma5:58.31,volume_ratio:1.5};
  c.renderMacdJScreen({...payload,screen_version:'macd-j-upturn-main-board.v4',candidates:[row],pending_count:1,pending_candidates:[{...row,name:'预警样本'}]});
  assert.match(c.byId('stock-macd-j-rule').textContent,/价格≥MA5/);
  assert.match(JSON.stringify(c.byId('stock-macd-j-table-body')),/84.7/);
  assert.doesNotMatch(JSON.stringify(c.byId('stock-macd-j-table-body')),/预警样本/);
  assert.match(JSON.stringify(c.byId('stock-macd-j-pending')),/预警样本/);
  assert.match(JSON.stringify(c.byId('stock-macd-j-pending')),/连续两天/);
});

test('v5 includes prior-two-session rows and labels the expanded warning window', () => {
  const c=harness();
  c.byId('stock-macd-j-group').value='prior_3_sessions';
  const row={...payload.candidates[1],signal_group:'prior_2_sessions',gap_sessions:2};
  c.renderMacdJScreen({...payload,screen_version:'macd-j-upturn-main-board.v5',candidates:[row],pending_count:1,pending_candidates:[row]});
  assert.equal(c.byId('stock-macd-j-table-body').children.length,1);
  assert.match(c.byId('stock-macd-j-rule').textContent,/提前1～2日/);
  assert.match(JSON.stringify(c.byId('stock-macd-j-pending')),/此前2个交易日内/);
});
