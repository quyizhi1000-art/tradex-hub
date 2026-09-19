const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const base = 'tradex/src/tradex/dashboard/watch/';
const html = fs.readFileSync(base + 'index.html', 'utf8');
const app = fs.readFileSync(base + 'app.js', 'utf8');
const head = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const appearance = app.slice(0, app.indexOf('\n(() => {', app.indexOf('})();') + 5));
const key = 'tradex.marketWatch.appearance.v1';
const themes = ['paper', 'mist', 'sage', 'graphite', 'classic'];
const layouts = ['comfortable', 'compact', 'wide'];
function boot(stored, blocked = false) {
  const values = new Map([[key, stored], ['tradex.marketWatch.muted.v1', 'true']]);
  const buttons = [...themes.map(theme => ({dataset: {appearanceTheme: theme}})), ...layouts.map(layout => ({dataset: {appearanceLayout: layout}}))];
  buttons.forEach(button => {
    button.attributes = {};
    button.setAttribute = (key, value) => button.attributes[key] = value;
    button.closest = () => button;
  });
  const handlers = {};
  const nodes = new Map(['appearance-current', 'appearance-status', 'appearance-button'].map(id => [id, {setAttribute(key, value) { this[key] = value; }}]));
  nodes.set('appearance-panel', {
    contains: button => buttons.includes(button),
    querySelectorAll: selector => buttons.filter(button => selector.includes('theme') ? button.dataset.appearanceTheme : button.dataset.appearanceLayout),
    addEventListener: (name, handler) => handlers[name] = handler,
  });
  const root = {dataset: {}};
  const context = {document: {documentElement: root, getElementById: id => nodes.get(id)}, localStorage: {
    getItem: name => { if (blocked) throw Error('blocked'); return values.get(name); },
    setItem: (name, value) => { if (blocked) throw Error('blocked'); values.set(name, value); },
  }};
  vm.runInNewContext(head, context);
  vm.runInNewContext(appearance, context);
  return {root, nodes, buttons, values, handlers, click: index => handlers.click({target: buttons[index]})};
}
for (const theme of themes) for (const layout of layouts) {
  const page = boot(null);
  page.click(themes.indexOf(theme));
  page.click(themes.length + layouts.indexOf(layout));
  assert.equal(page.root.dataset.theme, theme);
  assert.equal(page.root.dataset.layout, layout);
  assert.equal(page.buttons.filter(button => button.attributes['aria-pressed'] === 'true').length, 2);
  const restored = boot(page.values.get(key));
  assert.equal(restored.root.dataset.theme, theme);
  assert.equal(restored.root.dataset.layout, layout);
  assert.equal(page.values.get('tradex.marketWatch.muted.v1'), 'true');
}
for (const value of [null, 'invalid json', 'null', '42', '{"theme":"bogus","layout":"bogus"}']) {
  const page = boot(value);
  assert.equal(page.root.dataset.theme, 'paper');
  assert.equal(page.root.dataset.layout, 'comfortable');
}
const blocked = boot(null, true);
blocked.click(1);
assert.equal(blocked.root.dataset.theme, 'mist');
assert.match(blocked.nodes.get('appearance-status').textContent, /暂不允许保存/);
blocked.handlers.toggle({newState: 'open'});
assert.equal(blocked.nodes.get('appearance-button')['aria-expanded'], 'true');
blocked.handlers.toggle({newState: 'closed'});
assert.equal(blocked.nodes.get('appearance-button')['aria-expanded'], 'false');
const before = fs.readFileSync('work/appearance_20260917/before/app.js', 'utf8').replace(/\r\n/g, '\n');
assert.equal(app.slice(appearance.length).trim().replace(/\r\n/g, '\n'), before.trim());
assert.equal(new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1])).size, [...html.matchAll(/\bid="([^"]+)"/g)].length);
assert(html.indexOf('id="appearance-button"') < html.indexOf('class="toolbar"'));
console.log('PASS: 15 appearance/layout combinations, persistence/reload, invalid and blocked storage, selected state, popover state, unique IDs, existing market behavior unchanged.');
