const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/tradex/dashboard/watch/app.js'), 'utf8');

function harness() {
  const nodes = new Map(), storage = {};
  const node = () => ({hidden: true, children: [], attributes: {}, listeners: {},
    append(...v) {this.children.push(...v);}, replaceChildren() {this.children=[];},
    setAttribute(k,v) {this.attributes[k]=v;}, addEventListener(k,v) {this.listeners[k]=v;},
    querySelector() {return null;}});
  const c = {
    state: {}, byId: id => {if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id);},
    createElement: (tag, cls, textContent) => ({...node(), tag, cls, textContent}),
    objectValue: v => v && typeof v === 'object' ? v : {},
    formatCount: String, formatLevel: String, finiteNumber: v => Number.isFinite(v) ? v : null,
    stockSelectionNameLink: r => ({tag:'a',textContent:r.name}),
    text: v => String(v), asArray: v => Array.isArray(v) ? v : [],
    shanghaiClock: () => ({date: '2026-09-17'}), formatTimestamp: v => v,
    loadStoredArray: key => storage[key] || [], storeJson: (key, value) => {storage[key] = value;},
    Notification: () => assert.fail('page-only alert must never use system notifications'),
  };
  vm.createContext(c);
  const match = source.match(/  function renderIntradayMacdJ\([\s\S]*?(?=\n  (?:async )?function )/);
  vm.runInContext(match[0], c);
  for (const name of ['stockSelectionIndustryName', 'stockSelectionIndustryPayload',
    'stockSelectionDisplayCandidates', 'renderIntradayMacdJIndustryFilters', 'selectIntradayMacdJScan']) {
    vm.runInContext(source.match(new RegExp(`  function ${name}\\([\\s\\S]*?(?=\\n  (?:async )?function )`))[0], c);
  }
  const payload = {trade_date:'2026-09-17', status:'monitoring', message:'scan', records:[{
    name:'测试', instrument_id:'600000.SH', active:true, consecutive_samples:2, alerted_at:'10:01',
    price:10, dif:.2, dea:.1, j:30, observed_at:'10:01',
  }]};
  return {c, payload};
}

test('confirmed signal prompts once per stock/day even when storage is unavailable', () => {
  const {c,payload} = harness();
  c.storeJson = () => {};
  c.renderIntradayMacdJ(payload);
  assert.equal(c.byId('intraday-macd-j-toast').hidden, false);
  c.byId('intraday-macd-j-toast').hidden = true;
  c.renderIntradayMacdJ(payload);
  assert.equal(c.byId('intraday-macd-j-toast').hidden, true);
});

test('stale, closed and historical signals do not prompt', () => {
  for (const mode of ['stale','closed','old']) {
    const {c,payload} = harness();
    if (mode === 'old') payload.trade_date = '2026-09-16';
    else payload.status = mode;
    c.renderIntradayMacdJ(payload);
    assert.equal(c.byId('intraday-macd-j-toast').hidden, true, mode);
  }
});

test('withdrawal clears the visible prompt', () => {
  const {c,payload} = harness();
  c.renderIntradayMacdJ(payload);
  payload.records[0].active = false;
  c.renderIntradayMacdJ(payload);
  assert.equal(c.byId('intraday-macd-j-toast').hidden, true);
});

test('explicit close scan has a distinct page prompt and close label', () => {
  const {c,payload} = harness();
  payload.status = 'close_scanned'; payload.scan_kind = 'manual_close';
  c.renderIntradayMacdJ(payload);
  assert.equal(c.byId('intraday-macd-j-toast').hidden, false);
  assert.match(c.byId('intraday-macd-j-toast-text').textContent, /手动收盘扫描/);
});

test('formal close displays canonical count and clears intraday prompt without another alert', () => {
  const {c,payload} = harness();
  c.renderIntradayMacdJ(payload);
  Object.assign(payload, {status:'close_confirmed', scan_kind:'close_confirmation',
    matched_count:56, eligible_count:3052, evaluated_count:3036, generated_at:'20:36',
    message:'收盘确认：与选股中心同日正式存档一致，命中 56 只'});
  c.renderIntradayMacdJ(payload);
  assert.equal(c.byId('intraday-macd-j-toast').hidden, true);
  assert.match(c.byId('intraday-macd-j-coverage').textContent, /命中 56 只/);
  assert.match(c.byId('intraday-macd-j-status').textContent, /收盘确认/);
});

test('scan industry tags filter independently, preserve unknowns and reset between scans', () => {
  const {c} = harness();
  const records = [
    {instrument_id:'600000.SH',name:'银行样本',low_j_tags:['J<20']},
    {instrument_id:'600001.SH',name:'汽车样本'},
    {instrument_id:'600002.SH',name:'未知样本'},
  ];
  c.state.stockSelectionIndustry = '选股中心原筛选';
  c.state.intradayMacdJIndustryDisplay = {contract:'selection_industry_display.v1',schema_version:1,
    basis:'current_ths_industry',as_of:'2026-09-17',names_by_instrument:{'600000.SH':'银行','600001.SH':'汽车整车'}};
  c.state.intradayMacdJScans = ['10:00','10:15'].map(time => ({trade_date:'2026-09-17',
    last_scan_slot:time,status:'monitoring',scan_candidates:records}));
  const tree = () => {
    const walk = n => [n,...(n.children || []).flatMap(walk)];
    return walk(c.byId('intraday-macd-j-history-result'));
  };
  const rows = () => tree().find(n => n.tag === 'tbody').children;
  c.selectIntradayMacdJScan(0);
  assert.equal(rows().length,3);
  for (const label of ['行业板块','全部 3','银行 1','汽车整车 1','未分类 1','J<20']) {
    assert.ok(tree().some(n => n.textContent === label),label);
  }
  tree().find(n => n.tag === 'button' && n.textContent === '银行 1').listeners.click();
  assert.equal(rows().length,1);
  assert.ok(JSON.stringify(rows()).includes('银行样本'));
  assert.equal(c.state.stockSelectionIndustry,'选股中心原筛选');
  c.selectIntradayMacdJScan(0);
  assert.equal(rows().length,1);
  c.selectIntradayMacdJScan(1);
  assert.equal(rows().length,3);
  tree().find(n => n.tag === 'button' && n.textContent === '未分类 1').listeners.click();
  assert.ok(JSON.stringify(rows()).includes('未知样本'));
  c.state.intradayMacdJIndustryDisplay = {};
  c.selectIntradayMacdJScan(0);
  assert.equal(rows().length,3);
  assert.ok(tree().some(n => n.textContent === '未分类 3'));
  assert.equal(records[0].industry_block_name,undefined);
});

test('scan and followup dialogs dismiss on primary press only and suppress propagation', () => {
  for (const id of ['intraday-macd-j-history-dialog','stock-selection-followup-dialog']) {
    const parent = {open:true};
    const dialog = {open:true, closeCount:0, close() {this.open=false;this.closeCount++;},
      addEventListener(type, listener) {assert.equal(type,'pointerdown');this.press=listener;}};
    const start = source.indexOf(`    byId("${id}").addEventListener("pointerdown",`);
    assert.ok(start >= 0,`${id} backdrop handler registered`);
    const end = source.indexOf('\n    });',start) + '\n    });'.length;
    vm.runInNewContext(source.slice(start,end), {byId: key => {
      assert.equal(key,id);return dialog;
    }});
    for (const tagName of ['DIV','TABLE','BUTTON','SELECT','DETAILS']) {
      dialog.press({target:{tagName},button:0});
      assert.equal(dialog.open,true,`${tagName} content must remain open`);
    }
    dialog.press({target:dialog,button:2,isPrimary:true});
    dialog.press({target:dialog,button:0,isPrimary:false});
    assert.equal(dialog.open,true);
    let prevented=false,stopped=false;
    dialog.press({target:dialog,button:0,isPrimary:true,
      preventDefault(){prevented=true;},stopPropagation(){stopped=true;}});
    assert.equal(dialog.open,false);
    assert.equal(dialog.closeCount,1);
    assert.ok(prevented && stopped);
    assert.equal(parent.open,true);
  }
});
