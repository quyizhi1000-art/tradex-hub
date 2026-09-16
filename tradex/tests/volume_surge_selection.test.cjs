const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/tradex/dashboard/watch/app.js'), 'utf8');
function harness() {
  const elements = new Map();
  const element = (tag, cls, textContent) => ({ tag, textContent, children: [], hidden: false,
    attributes: {}, listeners: {},
    setAttribute(key, value) { this.attributes[key] = value; },
    addEventListener(type, listener) { this.listeners[type] = listener; },
    focus() { this.focused = true; },
    querySelector() { return this.children.find(child => child.attributes['aria-pressed'] === 'true'); },
    append(...items) { this.children.push(...items); },
    replaceChildren() { this.children = []; },
  });
  const context = {
    state: { stockSelectionIndustry: null, stockSelectionIndustryScope: null },
    byId(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
    createElement: element,
    asArray: (v) => Array.isArray(v) ? v : [],
    objectValue: (v) => v && typeof v === 'object' ? v : {},
    text: (v, fallback = '--') => v || fallback,
    formatCount: String, formatLevel: String,
    formatChangePct: String, formatCny: String,
    stringList: (v) => Array.isArray(v) ? v : [],
    replaceTextList() {}, renderStockSelectionExclusions() {},
  };
  vm.createContext(context);
  for (const name of ['finiteNumber', 'stockSelectionNameLink', 'stockSelectionIndustryName', 'stockSelectionDisplayCandidates',
    'stockSelectionIndustryPayload', 'stockSelectionStrategyResult',
    'stockSelectionVisibleCandidates', 'renderStockSelectionIndustryFilters',
    'renderVolumeSurgeScreen', 'renderStockPatternCandidates',
    'renderLimitUpTendencyCandidates', 'renderStockSelectionCandidates']) {
    const match = source.match(new RegExp(`  function ${name}\\([\\s\\S]*?(?=\\n  function )`));
    assert.ok(match, name);
    vm.runInContext(match[0], context);
  }
  return context;
}

test('stock names in all four tables open the correct quote in a separate tab', () => {
  const cases = [
    ['renderStockSelectionCandidates', 'stock-selection-table-body'],
    ['renderLimitUpTendencyCandidates', 'stock-limit-up-tendency-table-body'],
    ['renderStockPatternCandidates', 'stock-pattern-table-body'],
    ['renderVolumeSurgeScreen', 'stock-volume-surge-table-body'],
  ];
  const links = (node) => [ ...(node.tag === 'a' ? [node] : []), ...node.children.flatMap(links) ];
  for (const [renderer, body] of cases) {
    const c = harness();
    const candidates = [{ name: '上汽集团', instrument_id: '600104.SH' },
      { name: '章源钨业', instrument_id: '002378.SZ' }];
    c[renderer](renderer === 'renderVolumeSurgeScreen'
      ? { contract: 'stock_volume_surge_screen.v1', schema_version: 1, candidates } : candidates);
    const rendered = links(c.byId(body));
    assert.deepEqual(rendered.map(link => link.href), [
      'https://stockpage.10jqka.com.cn/600104/', 'https://stockpage.10jqka.com.cn/002378/',
    ], renderer);
    for (const link of rendered) {
      assert.equal(link.target, '_blank');
      assert.equal(link.rel, 'noopener noreferrer');
      assert.match(link.attributes['aria-label'], /新标签页/);
    }
  }
  const c = harness();
  assert.equal(c.stockSelectionNameLink({ name: '代码缺失' }).tag, 'strong');
  assert.equal(c.stockSelectionNameLink({ instrument_id: '600104.SH/invalid' }).tag, 'strong');
});

test('shows dated volume evidence and clears it when switching to an older archive', () => {
  const c = harness();
  c.renderVolumeSurgeScreen({ contract: 'stock_volume_surge_screen.v1', schema_version: 1,
    quality: 'accepted', board_eligible_count: 1, evaluated_count: 1, matched_count: 1,
    candidates: [{ name: '样本', instrument_id: '600000.SH', reference_close: 10.2,
      evidence: [{ trade_date: '2026-09-15', change_pct: 2, volume_multiple: 2,
        volume_shares: 200, prior_5d_average_volume_shares: 100 }] }],
  });
  assert.equal(c.byId('stock-volume-surge-content').hidden, false);
  assert.match(JSON.stringify(c.byId('stock-volume-surge-table-body')), /2026-09-15.*放量 2.00 倍/);
  c.renderVolumeSurgeScreen({});
  assert.equal(c.byId('stock-volume-surge-content').hidden, true);
  assert.equal(c.byId('stock-volume-surge-empty').hidden, false);
  assert.equal(c.byId('stock-volume-surge-table-body').children.length, 0);
});

test('prefers the registered rule version and renders the surviving round floor', () => {
  const c = harness();
  const definition = { strategy_id: 'upward-volume-surge-main-board', strategy_version: 'v2' };
  const old = { strategy_id: definition.strategy_id, strategy_version: 'v1', payload: {} };
  const current = { strategy_id: definition.strategy_id, strategy_version: 'v2', payload: {
    contract: 'stock_volume_surge_screen.v1', schema_version: 1, screen_version: 'upward-volume-surge-main-board.v2',
    candidates: [{ name: '样本', instrument_id: '600104.SH', anchor_trade_date: '2026-09-14',
      anchor_low: 10, minimum_subsequent_close: 10.1, reset_count: 1, evidence: [
        { trade_date: '2026-09-14', change_pct: 2, volume_multiple: 2, volume_shares: 200, prior_5d_average_volume_shares: 100 },
      ] }],
  } };
  const archive = { results: [old, current] };
  assert.equal(c.stockSelectionStrategyResult(archive, definition), current);
  assert.equal(c.stockSelectionStrategyResult({ results: [old] }, definition), old);
  c.renderVolumeSurgeScreen(current.payload);
  const rendered = JSON.stringify(c.byId('stock-volume-surge-table-body'));
  assert.match(rendered, /本轮起点 2026-09-14.*收盘底线 10.*后续最低收盘 10.1.*已重启 1 次/);
  assert.equal(c.byId('stock-volume-surge-table-body').children[0].children[3].textContent, '1');
  assert.deepEqual(archive.results, [old, current]);
});

test('unavailable data is distinct from a verified zero-match result', () => {
  const c = harness();
  for (const quality of ['unavailable', 'accepted']) {
    c.renderVolumeSurgeScreen({ contract: 'stock_volume_surge_screen.v1', schema_version: 1,
      quality, board_eligible_count: 1, evaluated_count: quality === 'accepted' ? 1 : 0,
      matched_count: 0, candidates: [] });
    const rendered = JSON.stringify(c.byId('stock-volume-surge-table-body'));
    assert.match(rendered, quality === 'unavailable' ? /无法完成筛选/ : /没有股票命中规则/);
  }
});

const orderedFixture = () => [
  { instrument_id: '600001.SH', industry: '银行', occurrence_count: 3, rank: 1 },
  { instrument_id: '600002.SH', industry: '软件服务', occurrence_count: 1, rank: 2 },
  { instrument_id: '600003.SH', industry: '银行', occurrence_count: 5, rank: 3 },
  { instrument_id: '600004.SH', industry: ' 软件服务 ', occurrence_count: 2, rank: 4 },
  { instrument_id: '600005.SH', industry: null, occurrence_count: 7, rank: 5 },
  { instrument_id: '600006.SH', industry: '软件服务', occurrence_count: 2, rank: 6 },
  { instrument_id: '600007.SH', industry: ' ', occurrence_count: 1, rank: 7 },
].map(item => ({ ...item, industry_block_name: item.industry, evidence: Array.from({ length: item.occurrence_count }, () => ({
  trade_date: '2026-09-15', change_pct: 2, volume_multiple: 2,
  volume_shares: 200, prior_5d_average_volume_shares: 100,
})) }));

test('all four tables group industries; pattern tables sort hits descending without changing archives', () => {
  const cases = [
    ['renderStockSelectionCandidates', 'stock-selection-table-body', ['600002', '600004', '600006', '600001', '600003', '600005', '600007']],
    ['renderLimitUpTendencyCandidates', 'stock-limit-up-tendency-table-body', ['600002', '600004', '600006', '600001', '600003', '600005', '600007']],
    ['renderStockPatternCandidates', 'stock-pattern-table-body', ['600004', '600006', '600002', '600003', '600001', '600005', '600007']],
    ['renderVolumeSurgeScreen', 'stock-volume-surge-table-body', ['600004', '600006', '600002', '600003', '600001', '600005', '600007']],
  ];
  for (const [renderer, bodyId, expected] of cases) {
    const c = harness();
    const candidates = orderedFixture();
    const original = JSON.stringify(candidates);
    c[renderer](renderer === 'renderVolumeSurgeScreen'
      ? { contract: 'stock_volume_surge_screen.v1', schema_version: 1, candidates }
      : candidates);
    const rows = c.byId(bodyId).children;
    assert.deepEqual(rows.map(row => JSON.stringify(row).match(/600\d{3}/)[0]), expected, renderer);
    assert.equal(JSON.stringify(candidates), original, 'archive data and evidence order are immutable');
    if (renderer === 'renderStockSelectionCandidates' || renderer === 'renderLimitUpTendencyCandidates') {
      assert.deepEqual(rows.map(row => row.children[0].textContent), [2, 4, 6, 1, 3, 5, 7]);
    }
  }
});

test('missing hit count sorts last within industry and empty inputs remain empty', () => {
  const c = harness();
  const records = [{ industry_block_name: '银行', occurrence_count: null },
    { industry_block_name: '银行', occurrence_count: 0 }, { industry_block_name: '银行', occurrence_count: 2 }];
  assert.deepEqual(Array.from(c.stockSelectionDisplayCandidates(records, item => item.occurrence_count),
    item => item.occurrence_count), [2, 0, null]);
  assert.equal(c.stockSelectionDisplayCandidates(null).length, 0);
});

test('industry tag clicks filter all four tables and All restores sorted rows and original ranks', () => {
  for (const [renderer, bodyId] of [
    ['renderStockSelectionCandidates', 'stock-selection-table-body'],
    ['renderLimitUpTendencyCandidates', 'stock-limit-up-tendency-table-body'],
    ['renderStockPatternCandidates', 'stock-pattern-table-body'],
    ['renderVolumeSurgeScreen', 'stock-volume-surge-table-body'],
  ]) {
    const c = harness();
    const candidates = orderedFixture();
    const original = JSON.stringify(candidates);
    c.renderStockSelectionStrategy = () => {
      c.renderStockSelectionIndustryFilters(candidates, '2026-09-16:strategy');
      c[renderer](renderer === 'renderVolumeSurgeScreen'
        ? { contract: 'stock_volume_surge_screen.v1', schema_version: 1, candidates }
        : candidates);
    };
    c.renderStockSelectionStrategy();
    const tags = c.byId('stock-selection-industry-tags');
    assert.deepEqual(tags.children.map(tag => tag.textContent), ['全部 7', '软件服务 3', '银行 2', '未分类 2']);
    const allRows = JSON.stringify(c.byId(bodyId).children);
    tags.children[1].listeners.click();
    assert.equal(c.state.stockSelectionIndustry, '软件服务');
    assert.equal(tags.children[1].attributes['aria-pressed'], 'true');
    assert.equal(tags.children[1].focused, true);
    assert.equal(c.byId(bodyId).children.length, 3, renderer);
    assert.match(c.byId('stock-selection-industry-summary').textContent, /显示 3 \/ 7 只/);
    const ids = c.byId(bodyId).children.map(row => JSON.stringify(row).match(/600\d{3}/)[0]);
    assert.deepEqual(ids, renderer.includes('Pattern') || renderer.includes('Volume')
      ? ['600004', '600006', '600002'] : ['600002', '600004', '600006']);
    tags.children[3].listeners.click();
    assert.equal(c.byId(bodyId).children.length, 2);
    tags.children[0].listeners.click();
    assert.equal(JSON.stringify(c.byId(bodyId).children), allRows);
    assert.equal(JSON.stringify(candidates), original);
  }
});

test('date and strategy changes reset industry selection; refresh retains valid selection', () => {
  const c = harness();
  const candidates = orderedFixture();
  c.renderStockSelectionIndustryFilters(candidates, '2026-09-16:a');
  c.state.stockSelectionIndustry = '银行';
  c.renderStockSelectionIndustryFilters(candidates, '2026-09-16:a');
  assert.equal(c.state.stockSelectionIndustry, '银行');
  c.renderStockSelectionIndustryFilters(candidates, '2026-09-16:b');
  assert.equal(c.state.stockSelectionIndustry, null);
  c.state.stockSelectionIndustry = '银行';
  c.renderStockSelectionIndustryFilters(candidates, '2026-09-15:b');
  assert.equal(c.state.stockSelectionIndustry, null);
  c.state.stockSelectionIndustry = '银行';
  c.renderStockSelectionIndustryFilters([], '2026-09-15:b');
  assert.equal(c.state.stockSelectionIndustry, null);
  assert.equal(c.byId('stock-selection-industry-filter').hidden, true);
});

test('tungsten and rare-earth candidates share one small-metals tag; missing blocks stay unclassified', () => {
  const c = harness();
  const archived = { candidates: [
    { instrument_id: '002378.SZ', name: '章源钨业', industry: '钨', evidence: [{}, {}] },
    { instrument_id: '001280.SZ', name: '中国铀业', industry: '稀土', evidence: [{}] },
    { instrument_id: '600000.SH', industry: '银行', evidence: [{}] },
  ] };
  const before = JSON.stringify(archived);
  const payload = c.stockSelectionIndustryPayload(archived, {
    contract: 'selection_industry_display.v1', schema_version: 1, basis: 'current_ths_industry',
    names_by_instrument: { '002378.SZ': '小金属', '001280.SZ': '小金属' },
  });
  c.renderStockSelectionIndustryFilters(payload.candidates, 'day:volume');
  assert.deepEqual(c.byId('stock-selection-industry-tags').children.map(tag => tag.textContent),
    ['全部 3', '小金属 2', '未分类 1']);
  c.state.stockSelectionIndustry = '小金属';
  assert.deepEqual(Array.from(c.stockSelectionVisibleCandidates(payload.candidates,
    item => item.evidence.length), item => item.instrument_id), ['002378.SZ', '001280.SZ']);
  assert.equal(JSON.stringify(archived), before);
  const missing = c.stockSelectionIndustryPayload(archived, {});
  assert.ok(missing.candidates.every(item => c.stockSelectionIndustryName(item) === '未分类'));
});
