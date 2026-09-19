const test = require("node:test");
const assert = require("node:assert/strict");
const { matching, sortByChange, validateDetail } = require("../src/tradex/dashboard/watch/sector-catalog.js");
const { recoveryLabel } = require("../src/tradex/dashboard/watch/sector-catalog.js");

test("automatic recovery reports backoff and never hides a stalled checker", () => {
  const now = Date.parse("2026-09-17T15:10:00+08:00");
  const recovery = { catalog_revision:"r", checked_at:new Date(now).toISOString(), state:"backoff",
    total_series:1000, missing_series:20, next_retry_at:"2026-09-17T15:11:00+08:00" };
  assert.match(recoveryLabel(recovery,"r",now), /自动退避重试.*980\/1000.*15:11/);
  assert.match(recoveryLabel(recovery,"r",now+180001), /检查已超时/);
  assert.match(recoveryLabel(recovery,"other",now), /待核验/);
  assert.match(recoveryLabel({...recovery,state:"failed"},"r",now), /发布失败/);
  assert.match(recoveryLabel({...recovery,state:"complete",missing_series:0,next_retry_at:null},"r",now), /覆盖已验证.*1000\/1000/);
});

const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync(require.resolve("../src/tradex/dashboard/watch/app.js"), "utf8");
const shared = vm.runInNewContext(`
  const SHANGHAI_TIME_PARTS_FORMATTER = new Intl.DateTimeFormat("en-GB", {timeZone:"Asia/Shanghai",hour:"2-digit",minute:"2-digit",hourCycle:"h23"});
  const SECTOR_FLOW_LUNCH_GAP_MINUTES=18, SECTOR_FLOW_DAY_SPAN_MINUTES=279, SECTOR_FLOW_SESSION_SPAN_MINUTES=263, MAX_SECTOR_FLOW_SAMPLE_GAP_MINUTES=5;
  const asArray = v => Array.isArray(v) ? v : [];
  const finiteNumber = v => Number.isFinite(v) ? v : null;
  const text = (v,fallback) => typeof v === "string" ? v : fallback;
  ${app.slice(app.indexOf("  function sectorFlowPointValue("), app.indexOf("  function hasRenderableSectorFlow("))}
  ({segments:sectorFlowSegments, path:sectorFlowPathData})
`);

test("directory uses the original renderer and its real lunch bridge", () => {
  const source = fs.readFileSync(require.resolve("../src/tradex/dashboard/watch/sector-catalog.js"), "utf8");
  assert.ok(source.includes("window.TradexSectorFlowChart.renderCatalog({ sectors: series })"));
  assert.ok(!source.includes("createElementNS"));
  const points = ["11:29:23", "11:30:00", "13:00:03", "13:01:57"].map(t => ({
    provider_as_of:`2026-09-11T${t}+08:00`, cumulative_cny:1, session_segment:t.startsWith("13") ? "pm" : "am",
  }));
  const segments = shared.segments({points}, "cumulative");
  const path = shared.path(segments, x=>x, y=>y);
  assert.equal((path.match(/M/g)||[]).length, 1);
  assert.equal((path.match(/C/g)||[]).length, 1);
  assert.equal(segments.flat().length, points.length);
});
test("shared renderer preserves confirmed gaps and single observed points", () => {
  const points = ["09:31", "09:32", "09:40", "09:41"].map(t => ({
    provider_as_of:`2026-09-11T${t}:00+08:00`, cumulative_cny:1, session_segment:"am",
  }));
  assert.equal((shared.path(shared.segments({points}, "cumulative"),x=>x,y=>y).match(/M/g)||[]).length, 2);
  assert.equal(shared.segments({points:points.slice(0,1)}, "cumulative")[0].length, 1);
});
test("search reaches all entries beyond former 64 limit and aliases", () => {
  const entries = Array.from({ length: 1000 }, (_, i) => ({ name: `板块${i}`,
    sector_key: `k${i}`, provider_sector_code: String(i), aliases: i === 999 ? ["曾用名"] : [],
    roles: [], hot_state: "none", missing_minutes: [], present_latest: true }));
  assert.equal(matching(entries, "曾用名", "all", new Set())[0].sector_key, "k999");
  assert.equal(matching(entries, "", "unclassified", new Set()).length, 1000);
});
test("change sorting covers all pages, keeps ties stable and missing values last", () => {
  const entries = Array.from({ length: 1000 }, (_, i) => ({ sector_key: `k${i}`, change_pct: i - 500 }));
  entries.push({ sector_key: "missing", change_pct: null }, { sector_key: "tie", change_pct: 499 });
  const desc = sortByChange(entries, "descending"), asc = sortByChange(entries, "ascending");
  assert.deepEqual(desc.slice(0, 2).map(e => e.sector_key), ["k999", "tie"]);
  assert.equal(asc[0].change_pct, -500);
  assert.equal(desc.at(-1).sector_key, "missing");
  assert.equal(asc.at(-1).sector_key, "missing");
  assert.equal(entries[0].sector_key, "k0");
});
test("detail cannot mix revisions or fabricate a missing manifest point", () => {
  const catalog = { catalog_revision: "r", source_revision: "s", trade_date: "2026-09-11",
    entries: [{ sector_key: "k", point_count: 1, first_provider_as_of: "2026-09-11T09:31:00+08:00", last_provider_as_of: "2026-09-11T09:31:00+08:00" }] };
  const detail = { contract: "sector_catalog_detail.v1", catalog_revision: "r", source_revision: "s",
    trade_date: "2026-09-11", sectors: [{ sector_key: "k", points: [{ provider_as_of: "2026-09-11T09:31:00+08:00", cumulative_cny: 12 }] }] };
  validateDetail(detail, catalog, ["k"]);
  assert.throws(() => validateDetail({ ...detail, source_revision: "old" }, catalog, ["k"]));
  assert.throws(() => validateDetail({ ...detail, sectors: [{ sector_key: "k", points: [] }] }, catalog, ["k"]));
});
