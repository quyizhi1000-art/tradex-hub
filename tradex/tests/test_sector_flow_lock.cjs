const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const app = fs.readFileSync(require.resolve("../src/tradex/dashboard/watch/app.js"), "utf8");
const { lock } = vm.runInNewContext(`
  const text = (value, fallback) => typeof value === "string" ? value : fallback;
  ${app.slice(app.indexOf("  function focusSectorFlowSeries("), app.indexOf("  function renderSectorFlowLegend("))}
  ({ lock: setSectorFlowSeriesLock })
`);

function node(attributes = {}) {
  return { attributes, setAttribute(name, value) { this.attributes[name] = value; } };
}

function chart() {
  const groups = ["visible", "neighbor"].map(sectorFlow => {
    const children = {
      "[data-sector-flow-line]": node(),
      "[data-sector-flow-hit-target]": node({ "pointer-events": "stroke" }),
      "[data-sector-flow-endpoint]": node({ stroke: "#071016" }),
      "[data-sector-flow-endpoint-label]": node(),
    };
    return Object.assign(node(), {
      dataset: { sectorFlow, defaultStrokeOpacity: "0.96", defaultStrokeWidth: "2" },
      children,
      querySelector(selector) { return children[selector]; },
    });
  });
  return { dataset: {}, groups, querySelectorAll() { return groups; } };
}

test("locking excludes the entire neighboring series, including its hit path and endpoint outline", () => {
  const svg = chart();
  lock(svg, "visible");
  assert.equal(svg.dataset.lockedSectorFlow, "visible");
  assert.equal(svg.groups[0].attributes.display, "inline");
  // Hiding the ancestor excludes even children with explicit pointer-events: stroke.
  assert.equal(svg.groups[1].attributes.display, "none");
  assert.equal(svg.groups[0].children["[data-sector-flow-line]"].attributes["stroke-width"], "2.9");
});

test("switching through the legend and clearing the lock restore every series and its default appearance", () => {
  const svg = chart();
  lock(svg, "visible");
  lock(svg, "neighbor");
  assert.equal(svg.groups[0].attributes.display, "none");
  assert.equal(svg.groups[1].attributes.display, "inline");
  lock(svg);
  assert.equal(svg.dataset.lockedSectorFlow, undefined);
  for (const group of svg.groups) {
    assert.equal(group.attributes.display, "inline");
    assert.equal(group.children["[data-sector-flow-line]"].attributes["stroke-width"], "2");
    assert.equal(group.children["[data-sector-flow-line]"].attributes["stroke-opacity"], "0.96");
    assert.equal(group.children["[data-sector-flow-endpoint]"].attributes["fill-opacity"], "1");
    assert.equal(group.children["[data-sector-flow-endpoint-label]"].attributes["fill-opacity"], "1");
  }
});

test("redraw preserves a valid lock and restores all series when the locked sector disappears", () => {
  const svg = chart();
  lock(svg, "visible");
  const redrawn = chart();
  lock(redrawn, svg.dataset.lockedSectorFlow);
  assert.equal(redrawn.groups[1].attributes.display, "none");
  redrawn.groups.shift();
  lock(redrawn, redrawn.dataset.lockedSectorFlow);
  assert.equal(redrawn.dataset.lockedSectorFlow, undefined);
  assert.equal(redrawn.groups[0].attributes.display, "inline");
});
