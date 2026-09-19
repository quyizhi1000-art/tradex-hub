/* One library page; consumers get their primary name from the same API/service. */
(() => {
  "use strict";
  function matching(items, {query = "", sector = "", tag = "", status = "all"} = {}) {
    const needle = query.trim().toLowerCase();
    return items.filter(item => {
      const verified = item.status === "verified";
      if (status === "verified" && !verified || status === "pending" && verified) return false;
      if (sector === "pending" && verified || sector && sector !== "pending" && item.primary_sector_name !== sector) return false;
      if (tag.startsWith("source:") && !(item.research?.candidate_concepts || []).some(c => c.name === tag.slice(7))) return false;
      if (tag && !tag.startsWith("source:") && !(item.tags || []).includes(tag) && !(item.chain || []).includes(tag)) return false;
      return !needle || [item.name, item.instrument_id, item.primary_sector_name,
        ...(item.chain || []), ...(item.tags || []), ...(item.research?.candidate_concepts || []).map(c => c.name)].filter(Boolean).join(" ").toLowerCase().includes(needle);
    });
  }
  function safeSource(url) {
    try { const parsed = new URL(url); return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : null; }
    catch { return null; }
  }
  function reviewLabel(item) {
    if (item.status === "verified") return "已归属";
    if (item.status === "stale") return "待复核 · 已到期";
    if (item.status === "disputed") return "待复核 · 证据冲突";
    return item.reviewed_on ? "审阅待完成" : "尚未审阅";
  }
  if (typeof module !== "undefined") module.exports = {matching, safeSource, reviewLabel};
  if (typeof document === "undefined") return;
  const el = id => document.getElementById(`smart-sector-${id}`);
  const state = {payload:null, sector:"", page:0, request:0};
  const pageSize = 40;
  const node = (tag, text, className) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (className) n.className = className; return n; };
  function detail(item) {
    const box = el("detail"); box.replaceChildren(node("h3", `${item.name} · ${item.instrument_id}`));
    box.append(node("p", `市场主归属：${item.primary_sector_name || reviewLabel(item)}　产业链细分：${(item.chain || []).join(" / ") || "—"}`));
    box.append(node("p", item.rationale || "尚未完成个股审阅；完成后将从该股已有行业或概念中选出唯一主归属。"));
    box.append(node("p", `主营背景：${item.business_background || "暂无资料"}`));
    if (item.business_summary) box.append(node("p", item.business_summary));
    box.append(node("p", `复核日期：${item.reviewed_on || "尚未完成"}　下次复核：${item.review_due || "—"}`));
    if (item.business_review_basis) box.append(node("p", `业务核对依据：${item.business_review_basis === "official_disclosure" ? "已读取公司原始披露" : "同花顺解析与公司业务资料交叉核对"}`));
    if (item.research) {
      const research = item.research;
      box.append(node("h4", "来源概念 · 尚未作为归属结论"));
      const names = [...new Set((research.candidate_concepts || []).map(c => `${c.name}（${c.source}）`))];
      box.append(node("p", names.join("、") || "尚未取得可解析的概念清单。"));
      if (item.status !== "verified") box.append(node("p", (research.review_gaps || []).join("；")));
      for (const source of research.source_links || []) {
        const url = safeSource(source.url); if (!url) continue;
        const a = node("a", `查看${source.publisher}原始页面`); a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
        const line = node("p"); line.append(a, node("small", `　读取于 ${source.retrieved_at}`)); box.append(line);
      }
    }
    const list = node("ul");
    for (const evidence of item.evidence || []) {
      const li = node("li"), url = safeSource(evidence.source_url);
      if (url) { const a = node("a", `${evidence.publisher} · ${evidence.title}`); a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer"; li.append(a); }
      else li.append(node("span", evidence.title));
      li.append(node("small", `　${evidence.published_at || "来源未标注发布日期"}`));
      li.append(node("p", (evidence.assertions || []).join("；"))); list.append(li);
    }
    if (list.children.length) box.append(list);
    box.scrollIntoView({block:"nearest"});
  }
  function render() {
    const payload = state.payload;
    if (!payload) return;
    const items = matching(payload.items, {query:el("search").value, sector:state.sector, tag:el("tag").value, status:el("status").value});
    const pages = Math.max(1, Math.ceil(items.length / pageSize)); state.page = Math.min(state.page, pages - 1);
    const body = el("rows"); body.replaceChildren();
    for (const item of items.slice(state.page * pageSize, (state.page + 1) * pageSize)) {
      const tr = node("tr"), stock = node("td", item.name); stock.append(node("small", item.instrument_id)); tr.append(stock);
      tr.append(node("td", item.primary_sector_name || reviewLabel(item), item.status === "verified" ? "" : "smart-sector-pending"));
      tr.append(node("td", (item.chain || []).join(" / ") || "—"));
      const tags = node("td"); for (const tag of item.tags || []) tags.append(node("span", tag, "smart-sector-tag")); tr.append(tags);
      tr.append(node("td", reviewLabel(item)));
      const action = node("td"), button = node("button", "查看依据", "button button-secondary"); button.type = "button"; button.addEventListener("click", () => detail(item)); action.append(button); tr.append(action); body.append(tr);
    }
    if (!items.length) { const tr = node("tr"), td = node("td", "没有符合条件的股票。可清空搜索或切换归属范围。"); td.colSpan = 6; tr.append(td); body.append(tr); }
    el("page").textContent = `${items.length}只 · 第${state.page + 1} / ${pages}页`;
    el("prev").disabled = state.page === 0; el("next").disabled = state.page + 1 >= pages;
    for (const b of el("sectors").querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.sector === state.sector));
  }
  async function load() {
    const request = ++state.request; el("summary").textContent = "正在读取归属目录…"; el("refresh").disabled = true;
    try {
      const response = await fetch("/api/smart-sector-library", {cache:"no-store"});
      const payload = await response.json();
      if (!response.ok || payload.contract !== "smart_sector_catalog.v2" || !Array.isArray(payload.items)) throw new Error(payload.error || "归属目录暂不可用");
      if (request !== state.request) return;
      state.payload = payload;
      const unreviewed = payload.items.filter(item => reviewLabel(item) === "尚未审阅").length;
      const needsReview = payload.items.filter(item => item.status !== "verified" && reviewLabel(item) !== "尚未审阅").length;
      el("summary").textContent = `全市场 ${payload.total}只　·　已归属 ${payload.verified_total}只　·　尚未审阅 ${unreviewed}只　·　审阅待完成或待复核 ${needsReview}只　·　当前归属 ${payload.as_of}。主归属用于其他页面；概念标签在本页单独查看。`;
      if (payload.research_progress?.published_at) {
        const p = payload.research_progress;
        el("summary").append(node("div", `资料进度：同花顺已读取 ${p.ths_read} / ${p.total}只，补充资料 ${p.collected} / ${p.total}只。采集不等于已核验。进度发布于 ${p.published_at}`));
        if (p.source_states?.ths?.startsWith("paused")) el("summary").append(node("div", "同花顺资料读取已暂停，已保存的资料可继续审阅；未读取的股票需补齐资料后审阅。"));
      }
      const nav = el("sectors"); nav.replaceChildren();
      for (const [key, label] of [["", `全部股票 · ${payload.total}`], ["pending", `待审阅 / 复核 · ${payload.pending_total}`], ...payload.sectors.map(s => [s.name, `${s.name} · ${s.count}`])]) {
        const b = node("button", label); b.type = "button"; b.dataset.sector = key; b.addEventListener("click", () => { state.sector = key; state.page = 0; render(); }); nav.append(b);
      }
      const prior = el("tag").value; el("tag").replaceChildren(new Option("全部标签", ""));
      const tags = [...new Set(payload.items.flatMap(item => [...(item.tags || []), ...(item.chain || [])]))].sort((a,b) => a.localeCompare(b,"zh-CN"));
      for (const tag of tags) el("tag").append(new Option(tag, tag)); if (tags.includes(prior)) el("tag").value = prior;
      const sourceTags = [...new Set(payload.items.flatMap(item => (item.research?.candidate_concepts || []).map(c => c.name)))].sort((a,b) => a.localeCompare(b,"zh-CN"));
      const group = node("optgroup"); group.label = "来源概念 · 待核验";
      for (const tag of sourceTags) group.append(new Option(tag, `source:${tag}`)); el("tag").append(group);
      if (prior.startsWith("source:") && sourceTags.includes(prior.slice(7))) el("tag").value = prior;
      el("detail").replaceChildren(node("p", "选择“查看依据”了解某只股票的归属判断。")); render();
    } catch (error) {
      if (request !== state.request) return;
      state.payload = null; el("rows").replaceChildren(); el("sectors").replaceChildren();
      el("summary").textContent = `读取失败：${error.message}。请点击“刷新目录”重试。`;
    } finally { if (request === state.request) el("refresh").disabled = false; }
  }
  el("open").addEventListener("click", () => { el("dialog").showModal(); load(); });
  el("close").addEventListener("click", () => el("dialog").close());
  el("refresh").addEventListener("click", load);
  for (const id of ["search", "tag", "status"]) el(id).addEventListener(id === "search" ? "input" : "change", () => {state.page = 0; render();});
  el("prev").addEventListener("click", () => {state.page--; render();});
  el("next").addEventListener("click", () => {state.page++; render();});
})();
