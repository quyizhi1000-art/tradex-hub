const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/tradex/dashboard/watch/app.js'), 'utf8');

function harness() {
  const elements = new Map();
  const element = (tag, cls, textContent) => ({tag, cls, textContent, children: [],
    append(...items) { this.children.push(...items); }, replaceChildren() { this.children = []; },
    setAttribute() {}, addEventListener() {}, querySelector() {}, open: false});
  const c = {state: {stockSelectionIndustry: null, stockSelectionChangeFilter: 'all'},
    byId(id) {if (!elements.has(id)) elements.set(id, element()); return elements.get(id);},
    createElement: element, asArray: v => Array.isArray(v) ? v : [],
    objectValue: v => v && typeof v === 'object' ? v : {}, text: (v, alt = '--') => v || alt,
    formatLevel: String, formatCount: String,
    renderStockSelectionStrategyContent() {}, stockSelectionPanelForContract() { return null; },
  };
  vm.createContext(c);
  for (const name of ['finiteNumber', 'stockSelectionNameLink', 'stockSelectionIndustryName',
    'stockSelectionDisplayCandidates', 'stockSelectionVisibleCandidates', 'stockSelectionDefinitions',
    'stockSelectionEvidenceValue', 'stockSelectionInsight', 'renderStockSelectionFollowup',
    'renderStockSelectionStrategy']) {
    const match = source.match(new RegExp(`  function ${name}\\([\\s\\S]*?(?=\\n  (?:async )?function )`));
    assert.ok(match, name); vm.runInContext(match[0], c);
  }
  return c;
}

test('change and industry filters intersect; new is not counted as changed', () => {
  const c = harness();
  const rows = [
    {instrument_id: '600001.SH', industry_block_name: '银行'},
    {instrument_id: '600002.SH', industry_block_name: '银行'},
    {instrument_id: '600003.SH', industry_block_name: '煤炭'},
  ];
  c.state.stockSelectionComparison = {rows: {
    '600001.SH': {status: 'changed'}, '600002.SH': {status: 'new'}, '600003.SH': {status: 'changed'},
  }};
  c.state.stockSelectionChangeFilter = 'changed';
  assert.equal(c.stockSelectionVisibleCandidates(rows).length, 2);
  c.state.stockSelectionIndustry = '银行';
  assert.equal(c.stockSelectionVisibleCandidates(rows)[0].instrument_id, '600001.SH');
  c.state.stockSelectionChangeFilter = 'new';
  assert.equal(c.stockSelectionVisibleCandidates(rows)[0].instrument_id, '600002.SH');
});

function archive() {
  return {trade_date: '2026-09-17', catalog: {strategies: [{strategy_id: 'sample', title: '样本策略', result_contract: 'sample.v1'}]},
    insights: {contract: 'stock_selection_insights.v1', schema_version: 1, trade_date: '2026-09-17',
      window_dates: ['2026-09-11', '2026-09-14', '2026-09-15', '2026-09-16', '2026-09-17'],
      strategies: {sample: {strategy_version: 'v1', comparison: {status: 'available', counts: {}, rows: {}},
        followup: {quality: 'complete', rows: [{instrument_id: '600001.SH', name: '样本',
          archive_records: ['2026-09-14', '2026-09-16'].map(trade_date => ({trade_date, candidate: {reference_close: 10, score: 80}})),
          limit_up_records: ['2026-09-15', '2026-09-17'].map(trade_date => ({trade_date, archive_dates: ['2026-09-14'], source: {provider: 'fixture'}})),
        }]}}}}};
}

test('tertiary table renders one stock with independent repeated archive and limit-up evidence', () => {
  const c = harness();
  c.state.stockSelectionHistory = archive(); c.state.stockSelectionStrategyId = 'sample';
  c.renderStockSelectionFollowup();
  const rows = c.byId('stock-selection-followup-body').children;
  assert.equal(rows.length, 1);
  assert.equal(rows[0].children[2].textContent, '2');
  assert.equal(rows[0].children[4].textContent, '2');
  for (const value of ['2026-09-14', '2026-09-16', '2026-09-15', '2026-09-17', '综合分 80']) {
    assert.ok(JSON.stringify(rows).includes(value), value);
  }
  c.state.stockSelectionHistory.insights.strategies.sample.followup = {quality: 'partial', rows: [], missing_limit_up_dates: ['2026-09-17']};
  c.renderStockSelectionFollowup();
  assert.match(c.byId('stock-selection-followup-quality').textContent, /涨停名单缺失：2026-09-17/);
  assert.match(JSON.stringify(c.byId('stock-selection-followup-body')), /数据缺失不等于没有涨停/);
});

test('date and strategy switch reset change filter; date mismatch fails closed', () => {
  const c = harness(); const data = archive();
  c.state.stockSelectionInsightScope = '2026-09-16:sample'; c.state.stockSelectionChangeFilter = 'new';
  c.renderStockSelectionStrategy(data, 'sample');
  assert.equal(c.state.stockSelectionChangeFilter, 'all');
  c.state.stockSelectionChangeFilter = 'changed'; c.renderStockSelectionStrategy(data, 'sample');
  assert.equal(c.state.stockSelectionChangeFilter, 'changed');
  data.insights.trade_date = '2026-09-16';
  assert.deepEqual(Object.keys(c.stockSelectionInsight(data, 'sample')), []);
  c.renderStockSelectionStrategy(data, 'sample');
  assert.equal(c.state.stockSelectionChangeFilter, 'all');
});

test('changed evidence detail is attached to evidence cell; new badge stays with stock name', () => {
  const c = harness(); const data = archive();
  const rows = ['changed', 'new'].map((status, i) => {
    const name = c.createElement('td'), evidence = c.createElement('td');
    const row = {lastElementChild: evidence, classList: {add() {}}};
    const link = {href: `https://stockpage.10jqka.com.cn/60000${i + 1}/`,
      closest: selector => selector === 'tr' ? row : name};
    data.insights.strategies.sample.comparison.rows[`60000${i + 1}.SH`] = {
      status, changes: status === 'changed' ? [{field: 'anchor_low', before: 10, after: 11}] : [],
    };
    return {name, evidence, link};
  });
  c.stockSelectionPanelForContract = () => ({querySelectorAll: () => rows.map(row => row.link)});
  c.renderStockSelectionStrategy(data, 'sample');
  assert.equal(rows[0].name.children.length, 0);
  assert.match(JSON.stringify(rows[0].evidence), /证据有变化.*查看 1 项证据变化.*本轮底线 10 → 11/);
  assert.match(JSON.stringify(rows[1].name), /新增/);
  assert.equal(rows[1].evidence.children.length, 0);
});
