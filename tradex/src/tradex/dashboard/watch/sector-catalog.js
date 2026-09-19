/* Independent, revision-bound reader for the Collector's full sector directory. */
(() => {
  "use strict";
  const ENDPOINT = "/api/market-watch/sector-catalog";
  const GROUP_SIZE = 12;
  const PAGE_SIZE = 30;
  const timeParts = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  function matching(entries, query, filter, selected) {
    const q = query.trim().toLowerCase();
    return entries.filter(e => {
      if (q && ![e.name, e.provider_sector_code, ...e.aliases].some(v => v.toLowerCase().includes(q))) return false;
      return filter === "all" || (filter === "hot" && e.hot_state !== "none")
        || (filter === "unclassified" && !e.roles.length)
        || (filter === "missing" && e.missing_minutes.length)
        || (filter === "absent" && !e.present_latest)
        || (filter === "unsupported" && e.curve_support === "unverified")
        || (filter === "selected" && selected.has(e.sector_key));
    });
  }
  function sortByChange(entries, direction) {
    if (!direction) return entries;
    return [...entries].sort((a, b) => {
      const av = Number.isFinite(a.change_pct), bv = Number.isFinite(b.change_pct);
      if (av !== bv) return av ? -1 : 1;
      return av ? (a.change_pct - b.change_pct) * (direction === "ascending" ? 1 : -1) : 0;
    });
  }
  function validateDetail(data, catalog, keys) {
    if (data.contract !== "sector_catalog_detail.v1" || data.catalog_revision !== catalog.catalog_revision
      || data.source_revision !== catalog.source_revision || data.trade_date !== catalog.trade_date
      || data.sectors.length !== keys.length) throw new Error("目录和曲线版本不一致");
    data.sectors.forEach((s, index) => {
      const entry = catalog.entries.find(e => e.sector_key === keys[index]);
      const points = s.points;
      if (s.sector_key !== keys[index] || points.length !== entry.point_count
        || (points.length && (points[0].provider_as_of !== entry.first_provider_as_of
          || points.at(-1).provider_as_of !== entry.last_provider_as_of))
        || points.some((p, i) => !Number.isFinite(p.cumulative_cny)
          || p.provider_as_of.slice(0, 10) !== catalog.trade_date
          || (i && Date.parse(p.provider_as_of) <= Date.parse(points[i - 1].provider_as_of)))) {
        throw new Error("真实分钟点校验失败");
      }
    });
  }
  function recoveryLabel(recovery, revision, now = Date.now()) {
    if (!recovery || recovery.catalog_revision !== revision) return "自动回填状态待核验";
    const checked = Date.parse(recovery.checked_at);
    if (!Number.isFinite(checked) || now - checked > 180000) return "自动回填检查已超时，等待采集器恢复";
    const labels = { complete: "分钟覆盖已验证", pending: "自动回填待处理", running: "自动回填处理中",
      backoff: "数据源未补齐，自动退避重试", unavailable: "缺口超出当前自动补采能力", failed: "回填或发布失败，将自动重试" };
    const progress = `${recovery.total_series - recovery.missing_series}/${recovery.total_series}`;
    const retry = recovery.next_retry_at ? ` · 下次 ${timeParts.format(new Date(recovery.next_retry_at))}` : "";
    return `${labels[recovery.state] || "自动回填状态待核验"} · ${progress}${retry}`;
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { matching, sortByChange, validateDetail, recoveryLabel };
  if (typeof document === "undefined") return;
  const byId = suffix => document.getElementById(`sector-catalog-${suffix}`);
  let catalog = null, selected = new Set(), page = 0, chartPage = 0, generation = 0, refreshing = false;
  let auto = true;
  let changeOrder = null;
  try {
    const stored = JSON.parse(localStorage.getItem("tradex.sectorCatalog.selection.v1") || "null");
    if (stored && Array.isArray(stored.keys)) { selected = new Set(stored.keys); auto = Boolean(stored.auto); }
  } catch { /* Malformed local preference falls back to automatic selection. */ }
  function save() {
    try { localStorage.setItem("tradex.sectorCatalog.selection.v1", JSON.stringify({ keys: [...selected], auto })); } catch { /* Browsing continues without persistence. */ }
  }
  function element(tag, text) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; }
  function setDefaults() {
    const hot = catalog.entries.filter(e => e.hot_state === "active" && e.point_count)
      .sort((a, b) => (b.change_pct ?? -Infinity) - (a.change_pct ?? -Infinity));
    // This limits default display only. The whole catalog and all curves remain accessible.
    hot.slice(0, GROUP_SIZE).forEach(e => selected.add(e.sector_key));
    save();
  }
  function renderTable() {
    if (!catalog) return;
    const entries = sortByChange(matching(catalog.entries, byId("search").value, byId("filter").value, selected), changeOrder);
    const pages = Math.max(1, Math.ceil(entries.length / PAGE_SIZE));
    page = Math.min(page, pages - 1);
    const body = byId("rows"); body.replaceChildren();
    for (const e of entries.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)) {
      const row = element("tr"); if (e.hot_state !== "none") row.className = "is-hot";
      const checkCell = element("td"), input = element("input"); input.type = "checkbox";
      input.checked = selected.has(e.sector_key); input.setAttribute("aria-label", `跟踪${e.name}`);
      input.addEventListener("change", () => {
        auto = false;
        if (input.checked) selected.add(e.sector_key); else selected.delete(e.sector_key);
        chartPage = 0; save(); renderChart();
        if (byId("filter").value === "selected") renderTable();
      });
      checkCell.append(input);
      const name = element("td", e.name);
      const roles = e.roles.map(r => r === "offense" ? "进攻" : "防御").join(" / ") || "待分类";
      name.append(element("small", `${e.taxonomy === "concept" ? "概念" : "行业"} · ${roles} · ${e.provider_sector_code}`));
      const change = element("td", Number.isFinite(e.change_pct) ? `${e.change_pct > 0 ? "+" : ""}${e.change_pct.toFixed(2)}%` : "--");
      const coverage = element("td", e.point_count ? `${e.point_count} 个真实点 · 缺 ${e.missing_minutes.length} 分钟` : "暂无真实轨迹");
      coverage.title = e.missing_minutes.join("、");
      if (!e.present_latest) coverage.append(element("small", "最新快照缺失，待核对"));
      if (e.curve_support === "unverified") coverage.append(element("small", "同源分钟补采能力待核实"));
      if (e.hot_state !== "none") coverage.append(element("small", `${e.hot_state === "active" ? "热点" : "当日退榜留存"}：${e.hot_reasons.join("；")}`));
      row.append(checkCell, name, change, coverage); body.append(row);
    }
    if (!entries.length) { const row = element("tr"), cell = element("td", "没有匹配条目；可切换全部板块或修改搜索。"); cell.colSpan = 4; row.append(cell); body.append(row); }
    byId("page").textContent = `共 ${entries.length} 个 · ${page + 1} / ${pages} 页`;
    byId("prev").disabled = page === 0; byId("next").disabled = page + 1 >= pages;
  }
  function draw(series) {
    window.TradexSectorFlowChart.renderCatalog({ sectors: series });
  }
  async function renderChart(retry = true) {
    if (!catalog) return;
    const token = ++generation, bound = catalog;
    const keys = bound.entries.filter(e => selected.has(e.sector_key)).map(e => e.sector_key);
    const pages = Math.max(1, Math.ceil(keys.length / GROUP_SIZE)); chartPage = Math.min(chartPage, pages - 1);
    const group = keys.slice(chartPage * GROUP_SIZE, (chartPage + 1) * GROUP_SIZE);
    byId("chart-page").textContent = `${keys.length} 个已选 · ${chartPage + 1}/${pages} 组`;
    byId("chart-prev").disabled = chartPage === 0; byId("chart-next").disabled = chartPage + 1 >= pages;
    byId("chart").replaceChildren(); byId("legend").replaceChildren();
    if (!group.length) { byId("chart-status").textContent = "请从全量目录勾选板块。"; return; }
    byId("chart-status").textContent = "读取本组真实分钟轨迹…";
    try {
      const query = new URLSearchParams({ catalog_revision: bound.catalog_revision, sector_keys: group.join(",") });
      const response = await fetch(`${ENDPOINT}/trajectory?${query}`, { cache: "no-store" });
      if (token !== generation) return;
      if (response.status === 409 && retry) { await refresh(false); if (token === generation) await renderChart(false); return; }
      if (!response.ok) throw new Error(`曲线读取失败（${response.status}）`);
      const data = await response.json(); validateDetail(data, bound, group);
      if (token !== generation || bound.catalog_revision !== catalog.catalog_revision) return;
      draw(data.sectors);
      const missing = group.filter(k => bound.entries.find(e => e.sector_key === k).missing_minutes.length).length;
      byId("chart-status").textContent = `${bound.trade_date} · ${group.length} 条曲线，其中 ${missing} 条存在分钟缺口（连线规则与资金轨迹图一致）。`;
    } catch (error) { if (token === generation) byId("chart-status").textContent = error.message; }
  }
  async function refresh(drawAfter = true) {
    if (refreshing) return; refreshing = true;
    try {
      const response = await fetch(ENDPOINT, { cache: "no-store" });
      if (!response.ok) throw new Error(`目录暂不可用（${response.status}），等待 Collector 生成`);
      const data = await response.json();
      if (data.contract !== "sector_catalog.v1" || data.counts.total !== data.entries.length
        || new Set(data.entries.map(e => e.sector_key)).size !== data.entries.length) throw new Error("目录完整性校验失败");
      const changed = catalog?.catalog_revision !== data.catalog_revision;
      if (catalog && catalog.trade_date !== data.trade_date && auto) selected.clear();
      catalog = data;
      if (auto) setDefaults();
      const date = new Date(data.as_of);
      const quoteTime = data.quote_as_of ? timeParts.format(new Date(data.quote_as_of)) : timeParts.format(date);
      byId("status").textContent = `${data.trade_date}（上海） · 资金曲线截至 ${timeParts.format(date)} · 板块行情及热点判断截至 ${quoteTime} · ${recoveryLabel(data.recovery, data.catalog_revision)}`;
      const names = { total: "目录总数", observed: "当日发现", mapped: "已纳入进攻/防御", unclassified: "待分类", with_curve: "有真实轨迹", history_missing: "分钟历史缺失", curve_unverified: "补采能力待核实", missing_latest: "最新快照缺失", hot: "当日热点" };
      byId("counts").replaceChildren(...Object.entries(names).map(([key, label]) => element("span", `${label} ${data.counts[key]}`)));
      renderTable();
      refreshing = false;
      if (drawAfter && (changed || !byId("chart").childNodes.length)) await renderChart();
    } catch (error) {
      byId("status").textContent = `${error.message}${catalog ? `；保留 ${catalog.trade_date} 已验证目录` : ""}`;
    } finally { refreshing = false; }
  }
  byId("search").addEventListener("input", () => { page = 0; if (byId("search").value) byId("filter").value = "all"; renderTable(); });
  byId("filter").addEventListener("change", () => { page = 0; renderTable(); });
  byId("change-sort").addEventListener("click", () => {
    changeOrder = changeOrder === "descending" ? "ascending" : "descending";
    byId("change-heading").setAttribute("aria-sort", changeOrder);
    byId("change-sort").textContent = `涨幅 ${changeOrder === "descending" ? "↓" : "↑"}`;
    page = 0; renderTable();
  });
  byId("prev").addEventListener("click", () => { page--; renderTable(); });
  byId("next").addEventListener("click", () => { page++; renderTable(); });
  byId("chart-prev").addEventListener("click", () => { chartPage--; renderChart(); });
  byId("chart-next").addEventListener("click", () => { chartPage++; renderChart(); });
  byId("default").addEventListener("click", () => { if (!catalog) return; auto = true; setDefaults(); chartPage = 0; renderTable(); renderChart(); });
  byId("clear").addEventListener("click", () => { auto = false; selected.clear(); save(); renderTable(); renderChart(); });
  byId("refresh").addEventListener("click", () => refresh());
  refresh(); setInterval(() => { if (!document.hidden) refresh(); }, 60000);
})();
