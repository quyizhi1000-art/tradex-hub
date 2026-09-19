const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/tradex/dashboard/watch/app.js"), "utf8");
function render(recovery, gaps = 1) {
  const elements = new Map();
  const classes = new Set();
  const byId = id => {
    if (!elements.has(id)) elements.set(id, {});
    return elements.get(id);
  };
  const context = {
    byId, state: {},
    text: (value, fallback) => typeof value === "string" && value ? value : fallback,
    formatTimestamp: value => value.slice(11, 19),
    shanghaiClock: () => ({ date: "2026-09-16", minuteOfDay: 1000 }),
    document: { querySelector: () => ({ classList: {
      add: value => classes.add(value), remove: (...values) => values.forEach(value => classes.delete(value)),
    } }) },
  };
  const start = source.indexOf("  function collectionRecoveryErrorMessage(");
  const end = source.indexOf("  function renderIntradayTrajectoryRepair(", start);
  vm.runInNewContext(source.slice(start, end) + "\nrenderCollectionRecoveryStatus", context)({
    collection_completeness: { expected_minute_buckets: 239, accepted_real: 239 - gaps, trade_date: "2026-09-16" },
    daily_recovery: {
      status: "needs_attention", unavailable_gaps: 1, remaining_gaps: gaps,
      accepted_before: 238, attempted_slots: 1, failed_attempts: 1,
      latest_attempt_progress_total: 6, completed_at: "2026-09-16T16:24:11+08:00",
      latest_failure_error_code: "HistoricalTrajectoryNotPublished",
      latest_failure_minute_bucket: "2026-09-16T09:30:00+08:00",
      ...recovery,
    },
  });
  return { byId, classes };
}

test("unpublished-only completion keeps the gap visible and disables retries", () => {
  const { byId, classes } = render({});
  assert.equal(byId("collection-recovery-accepted").textContent, "238 / 239");
  assert.equal(byId("collection-recovery-gaps").textContent, "1");
  assert.equal(byId("collection-recovery-status").textContent, "追补已结束 · 1 分钟无历史源");
  assert.equal(byId("collection-recovery-button").disabled, true);
  assert.equal(byId("collection-recovery-button").textContent, "无可追补分钟");
  assert.doesNotMatch(byId("collection-recovery-progress").textContent, /批内/);
  assert.match(byId("collection-recovery-detail").textContent, /未标记为完整/);
  assert.deepEqual([...classes], ["is-unavailable"]);
});

test("mixed gaps and legacy responses retain manual recovery", () => {
  for (const [recovery, gaps] of [[{}, 2], [{ unavailable_gaps: undefined }, 1]]) {
    const { byId, classes } = render(recovery, gaps);
    assert.equal(byId("collection-recovery-button").disabled, false);
    assert.equal(byId("collection-recovery-button").textContent, "重新检查并追补");
    assert(classes.has("is-attention"));
  }
});

test("batch failure and running recovery retain their distinct states", () => {
  const failed = render({ status: "failed", last_error_code: "RuntimeError", last_error_message: "batch error" });
  assert.equal(failed.byId("collection-recovery-button").disabled, false);
  assert.match(failed.byId("collection-recovery-error").textContent, /批次失败/);
  const running = render({ status: "running" });
  assert.equal(running.byId("collection-recovery-button").disabled, true);
  assert.match(running.byId("collection-recovery-progress").textContent, /批内/);
  assert(running.classes.has("is-retrying"));
});

test("retryable batch failures show the owned automatic retry instead of requiring a manual click", () => {
  const result = render({status: "failed", last_error_code: "TimeoutError", last_error_message: "fixture deadline", next_retry_at: "2026-09-16T16:25:11+08:00", manual_action_required: false});
  assert.match(result.byId("collection-recovery-status").textContent, /自动重试/);
  assert.match(result.byId("collection-recovery-error").textContent, /16:25:11/);
  assert.doesNotMatch(result.byId("collection-recovery-status").textContent, /手动/);
});

test("cache completion stays visibly pending until publication and real gaps remain retryable", () => {
  const start = source.indexOf("  function renderIntradayTrajectoryRepair(");
  const end = source.indexOf("  async function fetchIntradayTrajectoryRepair(", start);
  for (const remaining of [0, 2]) {
    const elements = new Map();
    const byId = id => {
      if (!elements.has(id)) elements.set(id, {});
      return elements.get(id);
    };
    const context = {
      byId,
      state: { collectionStatus: { latest_accepted_real: {} },
        trajectoryRepair: { status: "partial", target_count: 88, remaining_targets: remaining,
          last_error: "已发布至 11:27；目标 11:29" } },
      text: (value, fallback) => value || fallback,
      shanghaiClock: () => ({ minuteOfDay: 720 }),
    };
    vm.runInNewContext(source.slice(start, end) + "\nrenderIntradayTrajectoryRepair()", context);
    assert.match(byId("trajectory-repair-status").textContent, /已发布至 11:27；目标 11:29/);
    assert.equal(byId("trajectory-repair-button").disabled, remaining === 0);
    if (remaining === 0) {
      assert.match(byId("trajectory-repair-status").textContent, /等待发布验收/);
      assert.equal(byId("trajectory-repair-button").textContent, "等待自动发布");
    } else {
      assert.equal(byId("trajectory-repair-button").textContent, "重试真实缺口");
    }
  }
});
