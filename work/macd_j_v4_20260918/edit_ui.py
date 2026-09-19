from pathlib import Path
root=Path('tradex/src/tradex/dashboard/watch')
p=root/'app.js'
s=p.read_text(encoding='utf-8')
s=s.replace('byId("stock-macd-j-rule").textContent = payload.screen_version === "macd-j-upturn-main-board.v3"', 'byId("stock-macd-j-rule").textContent = payload.screen_version === "macd-j-upturn-main-board.v4"\n      ? "当日金叉 · J 当天或提前1日 · 距60日高点<-20% · 价格≥MA5 · 量比≤1.5 · v4"\n      : payload.screen_version === "macd-j-upturn-main-board.v3"')
s=s.replace('`（J 当日拐头 ${formatCount(payload.same_day_count)} · 此前 1～3 日拐头 ${formatCount(payload.prior_3_sessions_count)}）`','`（J 当日拐头 ${formatCount(payload.same_day_count)} · ${payload.screen_version?.endsWith(".v4") ? "前一交易日" : "此前 1～3 日"}拐头 ${formatCount(payload.prior_3_sessions_count)}）`\n      + ` · 独立待金叉预警 ${formatCount(payload.pending_count || 0)} 只`')
s=s.replace('const eligible = Number(payload.board_eligible_count);\n    const evaluated = Number(payload.evaluated_count);', 'const eligible = Number(payload.board_eligible_count);\n    const evaluated = Number(payload.evaluated_count);',1)
s=s.replace('const group = byId("stock-macd-j-group").value || "all";', '''const priorOption = byId("stock-macd-j-group").querySelector('option[value="prior_3_sessions"]');
    if (priorOption) priorOption.textContent = payload.screen_version?.endsWith(".v4") ? "J 前一交易日拐头" : "J 此前 1～3 个交易日拐头";
    const pendingPanel = byId("stock-macd-j-pending");
    if (pendingPanel) renderMacdPending(pendingPanel, stockSelectionVisibleCandidates(payload.pending_candidates), true);
    const group = byId("stock-macd-j-group").value || "all";''')
s=s.replace('group === "all" || item.signal_group === group','group === "all" || item.signal_group === group || (group === "prior_3_sessions" && item.signal_group === "prior_1_session")')
s=s.replace('const details = createElement("details");\n      const points = asArray(candidate.evidence);','const details = createElement("details");\n      appendMacdPriceEvidence(evidence, candidate);\n      const points = asArray(candidate.evidence);')
s=s.replace('candidate.signal_group === "same_day" ? "J 当日拐头" : "J 此前 1～3 日拐头"','candidate.signal_group === "same_day" ? "J 当日拐头" : candidate.signal_group === "prior_1_session" ? "J 前一交易日拐头" : "J 此前 1～3 日拐头"')
s=s.replace('} else if (scan.screen_version !== "macd-j-upturn-main-board.v3") {','} else if (scan.screen_version !== "macd-j-upturn-main-board.v4") {')
s=s.replace('此记录使用历史规则（仅当天新金叉）；原始结果保留，不与当前 0～2 个交易日规则混算。','此记录使用历史版本规则；原始结果保留，不与当前当日金叉、J最多提前1日及价格量比过滤混算。')
s=s.replace('evidence.append(details);\n      row.append(name,', 'appendMacdPriceEvidence(evidence, record);\n      evidence.append(details);\n      row.append(name,')
s=s.replace('target.append(scroll);\n    state.intradayMacdJRenderedKey', '''target.append(scroll);
    const pendingPanel = createElement("section");
    const pendingRows = stockSelectionIndustryPayload({candidates: scan.pending_candidates}, objectValue(state.intradayMacdJIndustryDisplay)).candidates;
    renderMacdPending(pendingPanel, pendingRows.filter(r => !state.intradayMacdJIndustry || stockSelectionIndustryName(r) === state.intradayMacdJIndustry), isClose);
    target.append(pendingPanel);
    state.intradayMacdJRenderedKey''')
s=s.replace('asArray(scan.scan_candidates).map(row => names[row.instrument_id])','[...asArray(scan.scan_candidates), ...asArray(scan.pending_candidates)].map(row => names[row.instrument_id])')
s=s.replace('const storageKey = `tradex.intradayMacdJ.seen.${payload.trade_date}${closeScan', 'const storageKey = `tradex.intradayMacdJ.seen.${payload.trade_date}.${payload.screen_version || "legacy"}${closeScan')
s=s.replace('只 · 存档 ${formatTimestamp(payload.generated_at, true)}。与选股中心使用同一份结果。','只 · 待金叉预警 ${payload.pending_count || 0} 只 · 存档 ${formatTimestamp(payload.generated_at, true)}。与选股中心使用同一份结果。')
marker='  function renderMacdJScreen(payload) {'
helper='''  function appendMacdPriceEvidence(target, row) {
    if (finiteNumber(row.high_60) === null) return;
    target.append(createElement("div", "", `60日高点 ${formatLevel(row.high_60)}（${text(row.high_60_date)}） · 距高点 ${Number(row.drawdown_60_pct).toFixed(2)}%`),
      createElement("div", "", `MA5 ${Number(row.ma5).toFixed(4)} · 量比 ${Number(row.volume_ratio).toFixed(3)}`));
  }

  function renderMacdPending(target, rows, isClose) {
    target.replaceChildren();
    rows = asArray(rows);
    target.append(createElement("h3", "", `待金叉预警（独立于正式入选） · ${rows.length} 只`),
      createElement("p", "", "尚未金叉；DIF今天上升、两线差距连续两天缩小，J当天或前一交易日拐头且今天仍上升。满足同样的60日高点、MA5和量比过滤；不保证之后金叉。"));
    if (!rows.length) { target.append(createElement("p", "", "当前已核验范围内没有待金叉预警。")); return; }
    const scroll = createElement("div", "stock-selection-table-scroll");
    const table = createElement("table", "stock-selection-table");
    const head = createElement("thead"), header = createElement("tr");
    ["股票", "行业", isClose ? "收盘价" : "参考价", "J拐头", "价格与量比证据", "MACD差距"].forEach(label => header.append(createElement("th", "", label)));
    head.append(header);
    const body = createElement("tbody");
    rows.forEach(row => {
      const tr = createElement("tr"), name = createElement("td"), evidence = createElement("td");
      name.append(stockSelectionNameLink(row), createElement("small", "", row.instrument_id));
      appendMacdPriceEvidence(evidence, row);
      const points = asArray(row.evidence).slice(-3);
      const gap = points.length ? points.map(p => `${p.trade_date}：${(p.dea-p.dif).toFixed(4)}`).join(" → ")
        : asArray(row.macd_gap_evidence).map(p => Number(p).toFixed(4)).join(" → ");
      tr.append(name, createElement("td", "", stockSelectionIndustryName(row)),
        createElement("td", "", formatLevel(row.reference_close ?? row.price)),
        createElement("td", "", text(row.j_turn_date)), evidence, createElement("td", "", gap));
      body.append(tr);
    });
    table.append(head, body); scroll.append(table); target.append(scroll);
  }

'''
assert marker in s
s=s.replace(marker,helper+marker)
p.write_text(s,encoding='utf-8')
p=root/'index.html'; s=p.read_text(encoding='utf-8')
s=s.replace('日线 MACD(12,26,9) 最近一次金叉距今日 0～2 个交易日，且当前 DIF＞DEA；KDJ(9,3,3) J 线今日上升、当天或此前 1～3 日拐头。','主板非ST，日线 MACD(12,26,9) 当日金叉（不限零轴位置）；KDJ(9,3,3) J 今日上升、当天或前一交易日拐头。距近60个交易日的前复权最高价严格低于-20%，价格≥当日MA5，量比≤1.5。另列待金叉预警：DIF上升、两线差距连续两天缩小，不算正式入选。')
s=s.replace('id="stock-macd-j-rule">金叉距今日 0～2 个交易日','id="stock-macd-j-rule">当日金叉 · J当天或提前1日 · 距60日高点&lt;-20% · 价格≥MA5 · 量比≤1.5')
s=s.replace('<option value="prior_3_sessions">J 此前 1～3 个交易日拐头</option>','<option value="prior_3_sessions">J 前一交易日拐头</option>')
s=s.replace('<ul id="stock-macd-j-methodology">','<ul id="stock-macd-j-methodology">')
s=s.replace('<article class="panel"><div class="panel-heading"><h3>判定方法</h3><span class="panel-state">规则 v1</span>', '<article class="panel"><div class="panel-heading"><h3>判定方法</h3><span class="panel-state">以所选存档版本为准</span>')
s=s.replace('<p id="stock-macd-j-source"></p>', '<p id="stock-macd-j-source"></p><section id="stock-macd-j-pending" aria-label="待金叉预警"></section>')
p.write_text(s,encoding='utf-8')
