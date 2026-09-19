const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const path = require('node:path');
(async () => {
  const base = 'http://127.0.0.1:8765';
  const js = await (await fetch(base + '/watch/sector-catalog.js')).text();
  const hash = value => crypto.createHash('sha256').update(value).digest('hex');
  assert.equal(hash(js), hash(fs.readFileSync('tradex/src/tradex/dashboard/watch/sector-catalog.js')));
  const context = {module:{exports:{}}};
  vm.runInNewContext(js, context);
  const catalog = await (await fetch(base + '/api/market-watch/sector-catalog')).json();
  const status = await (await fetch(base + '/api/market-watch/collection-status')).json();
  const label = context.module.exports.recoveryLabel(catalog.recovery, catalog.catalog_revision);
  assert.match(label, /分钟覆盖已验证.*1000\/1000/);
  const report = {checked_at:new Date().toISOString(), served_catalog_js_sha256:hash(js), label,
    catalog_revision:catalog.catalog_revision, recovery:catalog.recovery, collector:status};
  fs.writeFileSync(path.join(__dirname,'automatic_runtime.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify({...report,collector:undefined}));
})().catch(error=>{console.error(error);process.exitCode=1;});
