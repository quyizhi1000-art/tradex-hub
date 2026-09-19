const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const path = require('node:path');

async function main() {
  const base = 'http://127.0.0.1:8765';
  const get = async url => { const r = await fetch(base + url); assert.equal(r.status, 200); return r; };
  const app = await (await get('/watch/app.js')).text();
  const digest = v => crypto.createHash('sha256').update(v).digest('hex');
  assert.equal(digest(app), digest(fs.readFileSync('tradex/src/tradex/dashboard/watch/app.js', 'utf8')));
  const shared = vm.runInNewContext(`
    const SHANGHAI_TIME_PARTS_FORMATTER = new Intl.DateTimeFormat('en-GB', {timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
    const SECTOR_FLOW_LUNCH_GAP_MINUTES=18, SECTOR_FLOW_DAY_SPAN_MINUTES=279, SECTOR_FLOW_SESSION_SPAN_MINUTES=263, MAX_SECTOR_FLOW_SAMPLE_GAP_MINUTES=5;
    const asArray = v => Array.isArray(v) ? v : [];
    const finiteNumber = v => Number.isFinite(v) ? v : null;
    const text = (v,fallback) => typeof v === 'string' ? v : fallback;
    ${app.slice(app.indexOf('  function sectorFlowPointValue('), app.indexOf('  function hasRenderableSectorFlow('))}
    ({segments:sectorFlowSegments, path:sectorFlowPathData})
  `);
  const catalog = await (await get('/api/market-watch/sector-catalog')).json();
  const checks = [];
  for (let offset=0; offset<catalog.entries.length; offset+=32) {
    const batch = catalog.entries.slice(offset, offset+32);
    const q = new URLSearchParams({catalog_revision:catalog.catalog_revision, sector_keys:batch.map(e=>e.sector_key).join(',')});
    const detail = await (await get('/api/market-watch/sector-catalog/trajectory?' + q)).json();
    assert.equal(detail.catalog_revision, catalog.catalog_revision);
    for (const item of detail.sectors) {
      const segments = shared.segments(item, 'cumulative');
      const svg = shared.path(segments, x=>x, y=>y);
      const moves = (svg.match(/M/g)||[]).length;
      assert.equal(segments.flat().length, item.points.length, item.sector_key + ' dropped points');
      assert.equal(segments.length, 1, item.sector_key + ' unexpected morning breaks');
      assert.equal(moves, 1, item.sector_key + ' disconnected chart path');
      checks.push({sector_key:item.sector_key, points:item.points.length, session_segments:segments.length, continuous_paths:moves});
    }
  }
  const final = await (await get('/api/market-watch/sector-catalog')).json();
  assert.equal(final.catalog_revision, catalog.catalog_revision);
  const report = {catalog_revision:catalog.catalog_revision, served_app_sha256:digest(app), verified_curves:checks.length, disconnected_curves:0, curves:checks};
  fs.writeFileSync(process.argv[2] || path.join(__dirname,'catalog_render_verification.json'), JSON.stringify(report,null,2));
  console.log(JSON.stringify({...report,curves:undefined}));
}
main().catch(e=>{ console.error(e); process.exitCode=1; });
