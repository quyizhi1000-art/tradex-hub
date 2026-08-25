(() => {
  "use strict";

  const API_ENDPOINT = "/api/market-watch";
  const HISTORY_ENDPOINT = "/api/market-watch/history";
  const EVALUATION_ENDPOINT = "/api/market-watch/evaluation";
  const REVIEW_HISTORY_ENDPOINT = "/api/post-market-review/history";
  const REVIEW_GENERATE_ENDPOINT = "/api/post-market-review";
  const POLL_INTERVAL_MS = 15_000;
  const REPLAY_REFRESH_INTERVAL_MS = 60_000;
  const REVIEW_REFRESH_INTERVAL_MS = 60_000;
  const ALERT_COOLDOWN_MS = 5 * 60 * 1000;
  const MAX_STORED_ALERT_KEYS = 200;
  const MAX_SECTOR_FLOW_SERIES = 48;
  const MAX_SECTOR_FLOW_CHART_SERIES = 16;
  const SECTOR_FLOW_SYMLOG_CONSTANT_CNY = 100_000_000;
  const SECTOR_FLOW_ENDPOINT_GAP_PX = 15;
  const DEFAULT_SECTOR_FLOW_SELECTIONS = {
    defense: [
      "electric_power", "agriculture", "precious_metals", "oil_gas",
      "ports", "baijiu", "retail", "bank",
    ],
    offense: [
      "semiconductor", "software_development", "communication_equipment",
      "automation_equipment", "consumer_electronics", "battery",
      "photovoltaic_equipment", "defense",
    ],
  };
  const DEFAULT_SECTOR_FLOW_SURGE_THRESHOLD = 0.5;
  const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
  const SECTOR_FLOW_COLORS = {
    electric_power: "#ff7c79",
    agriculture: "#f4bb5f",
    precious_metals: "#c58cff",
    oil_gas: "#ff9b69",
    ports: "#55c8d2",
    baijiu: "#f0cf65",
    retail: "#78a9ff",
    bank: "#8799a4",
    coal: "#d6a764",
    food_beverage: "#ff9fb2",
    insurance: "#98a8ff",
    gas: "#6bc8a4",
    traditional_chinese_medicine: "#9dd27d",
    white_goods: "#78b7de",
    water_utilities: "#65ced8",
    highway: "#bd9e7b",
    semiconductor: "#ff7c79",
    software_development: "#c58cff",
    communication_equipment: "#55c8d2",
    automation_equipment: "#f4bb5f",
    consumer_electronics: "#78a9ff",
    battery: "#ff9b69",
    photovoltaic_equipment: "#f0cf65",
    defense: "#d98b72",
  };
  const STORAGE_KEYS = {
    deliveredAlerts: "tradex.marketWatch.deliveredAlerts.v1",
    readAlerts: "tradex.marketWatch.readAlerts.v1",
    lastAlertAt: "tradex.marketWatch.lastAlertAt.v1",
    muted: "tradex.marketWatch.muted.v1",
    notificationEnabled: "tradex.marketWatch.notificationEnabled.v1",
    soundEnabled: "tradex.marketWatch.soundEnabled.v1",
    sectorFlowSelection: "tradex.marketWatch.sectorFlowSelection.v1",
    offenseSectorFlowSelection: "tradex.marketWatch.offenseSectorFlowSelection.v2",
    sectorFlowSurgeThreshold: "tradex.marketWatch.sectorFlowSurgeThreshold.v1",
  };

  const ROLE_CONFIG = {
    broad_market: { fallbackName: "上证指数" },
    large_cap: { fallbackName: "沪深300" },
    small_cap: { fallbackName: "中证1000" },
    growth: { fallbackName: "创业板指" },
  };
  const REGIME_LABELS = {
    attack: "进攻占优",
    defense: "防御占优",
    mixed: "风格拉扯",
    uncertain: "方向未确认",
  };
  const CHANGE_REASON_LABELS = {
    no_confirmed_change_evidence: "需要连续快照支持，单次抖动不会作为变化提醒。",
  };

  const state = {
    alerts: [],
    audioContext: null,
    backgroundRefresh: false,
    deliveredAlertKeys: loadStoredObject(STORAGE_KEYS.deliveredAlerts),
    evaluationDays: null,
    fetchFailed: false,
    fetchInFlight: false,
    lastFetchError: null,
    lastAlertAt: Number(readStoredText(STORAGE_KEYS.lastAlertAt, "0")) || 0,
    lastSnapshot: null,
    lastSuccessAt: 0,
    muted: readStoredText(STORAGE_KEYS.muted) === "true",
    nextPollAt: 0,
    notificationEnabled: false,
    readAlertKeys: new Set(loadStoredArray(STORAGE_KEYS.readAlerts)),
    replayFetchInFlight: false,
    replayHasLoaded: false,
    replayLastAttemptAt: 0,
    replayLastFetchedAt: 0,
    replayPendingRequest: null,
    replayTradeDate: null,
    reviewFetchInFlight: false,
    reviewGenerateInFlight: false,
    reviewHasLoaded: false,
    reviewHistory: null,
    reviewSchedule: {
      manual_after: "20:30",
      automatic_if_missing_after: "21:00",
      timezone: "Asia/Shanghai",
    },
    reviewTradeDate: null,
    sectorFlowMode: {
      defense: "cumulative",
      offense: "cumulative",
    },
    sectorFlowSelection: readStoredText(STORAGE_KEYS.sectorFlowSelection) === null
      ? null
      : new Set(loadStoredArray(STORAGE_KEYS.sectorFlowSelection)),
    offenseSectorFlowSelection: readStoredText(STORAGE_KEYS.offenseSectorFlowSelection) === null
      ? null
      : new Set(loadStoredArray(STORAGE_KEYS.offenseSectorFlowSelection)),
    sectorFlowSurgeThreshold: Number(readStoredText(
      STORAGE_KEYS.sectorFlowSurgeThreshold,
      String(DEFAULT_SECTOR_FLOW_SURGE_THRESHOLD),
    )) || DEFAULT_SECTOR_FLOW_SURGE_THRESHOLD,
    soundEnabled: false,
  };

  if (
    "Notification" in window &&
    Notification.permission === "granted" &&
    readStoredText(STORAGE_KEYS.notificationEnabled) === "true"
  ) {
    state.notificationEnabled = true;
  }
  if (readStoredText(STORAGE_KEYS.soundEnabled) === "true") {
    state.soundEnabled = true;
  }

  const byId = (id) => document.getElementById(id);

  function readStoredText(key, fallback = null) {
    try {
      return localStorage.getItem(key) ?? fallback;
    } catch (_error) {
      return fallback;
    }
  }

  function storeText(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (_error) {
      // Storage is optional; in-memory state remains fully functional.
    }
  }

  function loadStoredArray(key) {
    try {
      const parsed = JSON.parse(readStoredText(key, "[]"));
      return Array.isArray(parsed) ? parsed.filter((item) => typeof item === "string") : [];
    } catch (_error) {
      return [];
    }
  }

  function loadStoredObject(key) {
    try {
      const parsed = JSON.parse(readStoredText(key, "{}"));
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch (_error) {
      return {};
    }
  }

  function storeJson(key, value) {
    storeText(key, JSON.stringify(value));
  }

  function text(value, fallback = "--") {
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  }

  function finiteNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function stringList(value) {
    if (!Array.isArray(value)) return [];
    return value
      .map((item) => typeof item === "string" ? item.trim() : "")
      .filter(Boolean);
  }

  function formatLevel(value) {
    const parsed = finiteNumber(value);
    if (parsed === null) return "--";
    return new Intl.NumberFormat("zh-CN", {
      maximumFractionDigits: parsed >= 1000 ? 2 : 3,
      minimumFractionDigits: parsed >= 1000 ? 2 : 0,
    }).format(parsed);
  }

  function formatCount(value) {
    const parsed = finiteNumber(value);
    return parsed === null ? "--" : Math.round(parsed).toLocaleString("zh-CN");
  }

  function formatChangePct(value, withSign = true) {
    const parsed = finiteNumber(value);
    if (parsed === null) return "--";
    const sign = withSign && parsed > 0 ? "+" : "";
    return `${sign}${parsed.toFixed(2)}%`;
  }

  function ratioAsPercent(value) {
    const parsed = finiteNumber(value);
    if (parsed === null) return null;
    // market_watch.v1 ratios are canonical decimal ratios, including values
    // whose absolute value may exceed 1 (for example 1.5 means +150%).
    return parsed * 100;
  }

  function formatRatio(value, withSign = true) {
    const parsed = ratioAsPercent(value);
    if (parsed === null) return "--";
    const sign = withSign && parsed > 0 ? "+" : "";
    return `${sign}${parsed.toFixed(1)}%`;
  }

  function formatCny(value, withSign = false) {
    const parsed = finiteNumber(value);
    if (parsed === null) return "--";
    const absolute = Math.abs(parsed);
    const sign = parsed < 0 ? "−" : withSign && parsed > 0 ? "+" : "";
    if (absolute >= 1e12) return `${sign}${(absolute / 1e12).toFixed(2)} 万亿`;
    if (absolute >= 1e8) return `${sign}${(absolute / 1e8).toFixed(1)} 亿`;
    if (absolute >= 1e4) return `${sign}${(absolute / 1e4).toFixed(1)} 万`;
    return `${sign}${absolute.toFixed(0)} 元`;
  }

  function formatTimestamp(value, includeDate = false) {
    if (!value) return "--";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return text(value);
    return new Intl.DateTimeFormat("zh-CN", {
      month: includeDate ? "2-digit" : undefined,
      day: includeDate ? "2-digit" : undefined,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
      timeZone: "Asia/Shanghai",
    }).format(parsed);
  }

  function toneClass(value) {
    const parsed = finiteNumber(value);
    if (parsed === null || parsed === 0) return "tone-flat";
    return parsed > 0 ? "tone-positive" : "tone-negative";
  }

  function setTone(element, value) {
    element.classList.remove("tone-positive", "tone-negative", "tone-flat");
    element.classList.add(toneClass(value));
  }

  function createElement(tag, className, content) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (content !== undefined) element.textContent = text(content, "");
    return element;
  }

  function normalizeFreshness(snapshot) {
    const raw = snapshot && snapshot.freshness && typeof snapshot.freshness === "object"
      ? snapshot.freshness
      : {};
    const rawStatus = text(raw.status, "unknown").toLowerCase();
    const status = new Set(["fresh", "degraded", "stale", "unavailable"]).has(rawStatus)
      ? rawStatus
      : "unknown";

    return {
      detail: stringList(raw.flags).length
        ? "部分关键组件未达到当前判断所需的数据质量。"
        : null,
      status,
    };
  }

  function normalizeMarketState(snapshot) {
    const raw = snapshot && snapshot.market_state && typeof snapshot.market_state === "object"
      ? snapshot.market_state
      : {};
    const phase = text(raw.phase, "unknown").toLowerCase();
    const isOpen = raw.is_open === true;
    const kind = isOpen ? "open" : phase === "midday_break" ? "break" : phase === "unknown" ? "unknown" : "closed";
    const labels = {
      pre_open: "开盘前",
      opening_observation: "开盘观察",
      trading: "盘中交易",
      midday_break: "午间休市",
      closed: "已收盘",
      non_trading: "非交易日",
      unknown: "状态未知",
    };
    return { isOpen, kind, label: labels[phase] || "状态未知" };
  }

  function guardrailLabels(guardrail) {
    const regime = text(guardrail.regime, "uncertain").toLowerCase();
    const severity = text(guardrail.severity, "unknown").toLowerCase();
    const severityLabels = {
      calm: "可观察",
      caution: "谨慎",
      stop: "先刹车",
      unknown: "未评级",
    };
    return {
      regime,
      regimeLabel: REGIME_LABELS[regime] || "方向未确认",
      severity,
      severityLabel: severityLabels[severity] || text(guardrail.severity, "未评级"),
    };
  }

  function strengthLabel(value) {
    if (value === null || value === undefined || value === "") return "未确认";
    const normalized = String(value).toLowerCase();
    const labels = {
      strong: "强",
      moderate: "中",
      weak: "弱",
      abstain: "暂不判断",
      unknown: "未知",
    };
    return labels[normalized] || "未知";
  }

  function marketFeelFacts(snapshot) {
    const freshness = normalizeFreshness(snapshot);
    const guardrail = snapshot.guardrail && typeof snapshot.guardrail === "object"
      ? snapshot.guardrail
      : {};
    const labels = guardrailLabels(guardrail);
    const facts = [];
    const breadth = snapshot.breadth && typeof snapshot.breadth === "object"
      ? snapshot.breadth
      : {};
    const up = finiteNumber(breadth.up_count);
    const down = finiteNumber(breadth.down_count);
    const advanceRatio = finiteNumber(breadth.advance_ratio);
    if (breadth.available !== false && up !== null && down !== null) {
      const ratioCopy = advanceRatio === null
        ? ""
        : `（上涨占比 ${formatRatio(advanceRatio, false)}）`;
      facts.push(`上涨 ${formatCount(up)} / 下跌 ${formatCount(down)}${ratioCopy}`);
    } else {
      facts.push("涨跌家数不可用");
    }

    const indexFacts = asArray(snapshot.indices)
      .filter((item) => item && item.available !== false && finiteNumber(item.change_pct) !== null)
      .map((item) => `${text(item.name, ROLE_CONFIG[item.role]?.fallbackName || item.role)} ${formatChangePct(item.change_pct)}`);
    facts.push(indexFacts.length ? indexFacts.join("、") : "关键指数涨跌不可用");

    const turnover = snapshot.turnover && typeof snapshot.turnover === "object"
      ? snapshot.turnover
      : {};
    const turnoverRatio = finiteNumber(turnover.difference_ratio);
    if (turnover.available !== false && turnoverRatio !== null) {
      const comparison = turnover.today_date && turnover.previous_date
        ? `${turnover.today_date} 较 ${turnover.previous_date} 同时点`
        : "成交较前一交易日同时点";
      facts.push(`${comparison} ${formatRatio(turnoverRatio)}`);
    } else {
      facts.push("成交额缺少同时点基线");
    }

    const abstain = freshness.status === "stale" || freshness.status === "unavailable";
    return {
      detail: facts.join(" ｜ "),
      heading: abstain ? "历史读数 · 暂停判断" : labels.regimeLabel,
    };
  }

  function renderDecision(snapshot) {
    const marketState = normalizeMarketState(snapshot);
    const freshness = normalizeFreshness(snapshot);
    const guardrail = snapshot.guardrail && typeof snapshot.guardrail === "object" ? snapshot.guardrail : {};
    const labels = guardrailLabels(guardrail);
    const marketFeel = marketFeelFacts(snapshot);

    const marketPill = byId("market-state-pill");
    marketPill.textContent = marketState.label;
    marketPill.className = `state-pill state-${marketState.kind}`;
    byId("market-as-of").textContent = `统一时点 ${formatTimestamp(snapshot.as_of)}`;

    const freshnessLabels = { fresh: "新鲜", degraded: "降级", stale: "陈旧", unavailable: "不可用", unknown: "未知" };
    byId("freshness-label").textContent = `数据新鲜度：${freshnessLabels[freshness.status]}`;

    byId("decision-heading").textContent = marketFeel.heading;
    const severityBadge = byId("severity-badge");
    severityBadge.textContent = labels.severityLabel;
    severityBadge.className = `severity-badge severity-${["calm", "caution", "stop"].includes(labels.severity) ? labels.severity : "unknown"}`;
    byId("guardrail-current-state").textContent = marketFeel.detail;
    byId("behavioral-constraint").textContent = text(
      guardrail.behavioral_constraint,
      "证据不足时，不因单一指数波动改变原计划。",
    );
    const strength = strengthLabel(guardrail.conclusion_strength);
    byId("conclusion-strength").textContent = `证据强度：${strength}`;
    byId("analysis-strength").textContent = `证据强度：${strength}`;
  }

  function renderIndexDock(snapshot) {
    const indices = asArray(snapshot.indices);
    Object.keys(ROLE_CONFIG).forEach((role) => {
      const dockItem = document.querySelector(`[data-index-dock-role="${role}"]`);
      if (!dockItem) return;
      const record = indices.find((item) => item && item.role === role) || {};
      const level = finiteNumber(record.level);
      const change = finiteNumber(record.change_pct);
      const available = record.available !== false && (level !== null || change !== null);
      const name = text(record.name, ROLE_CONFIG[role].fallbackName);

      dockItem.classList.toggle("is-unavailable", !available);
      dockItem.querySelector('[data-index-dock-field="name"]').textContent = name;
      dockItem.querySelector('[data-index-dock-field="level"]').textContent = level !== null ? formatLevel(level) : "--";
      const dockChange = dockItem.querySelector('[data-index-dock-field="change"]');
      dockChange.textContent = change !== null ? formatChangePct(change) : "--";
      setTone(dockChange, change);
    });
  }

  function renderBreadth(snapshot) {
    const breadth = snapshot.breadth && typeof snapshot.breadth === "object" ? snapshot.breadth : {};
    const up = finiteNumber(breadth.up_count);
    const down = finiteNumber(breadth.down_count);
    const flat = finiteNumber(breadth.flat_count) || 0;
    const unclassified = finiteNumber(breadth.unclassified_count) || 0;
    const totalFromField = finiteNumber(breadth.total_count);
    const total = totalFromField !== null
      ? totalFromField
      : up !== null && down !== null
        ? up + down + flat + unclassified
        : null;
    const ratioFromField = ratioAsPercent(breadth.advance_ratio);
    const directional = up !== null && down !== null ? up + down : 0;
    const upRatio = ratioFromField !== null ? ratioFromField : directional ? (up / directional) * 100 : null;
    const upShare = total ? (up / total) * 100 : 0;
    const flatShare = total ? (flat / total) * 100 : 0;
    const downShare = total ? (down / total) * 100 : 0;
    const unclassifiedShare = total ? (unclassified / total) * 100 : 0;
    const available = (
      breadth.available !== false &&
      up !== null &&
      down !== null &&
      total !== null &&
      total > 0 &&
      upRatio !== null
    );

    byId("breadth-state").textContent = available ? "同一快照" : "不可用";
    byId("breadth-up-ratio").textContent = available ? `${upRatio.toFixed(1)}%` : "--";
    byId("breadth-up").textContent = available ? formatCount(up) : "--";
    byId("breadth-flat").textContent = available ? formatCount(flat) : "--";
    byId("breadth-down").textContent = available ? formatCount(down) : "--";
    byId("breadth-unclassified").textContent = available ? formatCount(unclassified) : "--";
    byId("breadth-total").textContent = available ? formatCount(total) : "--";
    byId("breadth-bar-up").style.width = available ? `${Math.max(0, Math.min(100, upShare))}%` : "0";
    byId("breadth-bar-flat").style.width = available ? `${Math.max(0, Math.min(100, flatShare))}%` : "0";
    byId("breadth-bar-down").style.width = available ? `${Math.max(0, Math.min(100, downShare))}%` : "0";
    byId("breadth-bar-unclassified").style.width = available ? `${Math.max(0, Math.min(100, unclassifiedShare))}%` : "0";
  }

  function turnoverFreshness(snapshot) {
    const freshness = snapshot && snapshot.freshness && typeof snapshot.freshness === "object"
      ? snapshot.freshness
      : {};
    const component = asArray(freshness.components).find((item) => (
      item && typeof item === "object" && item.component === "turnover"
    ));
    const rawStatus = text(component && component.status, "unavailable").toLowerCase();
    const status = new Set(["fresh", "degraded", "stale", "unavailable"]).has(rawStatus)
      ? rawStatus
      : "unavailable";
    return { status };
  }

  function validDateOnly(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const [year, month, day] = value.split("-").map(Number);
    const parsed = new Date(Date.UTC(year, month - 1, day));
    return (
      parsed.getUTCFullYear() === year &&
      parsed.getUTCMonth() === month - 1 &&
      parsed.getUTCDate() === day
    );
  }

  function validTimeOnly(value) {
    if (typeof value !== "string" || !/^\d{2}:\d{2}$/.test(value)) return false;
    const [hour, minute] = value.split(":").map(Number);
    return hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59;
  }

  function numbersClose(left, right, absoluteTolerance = 1e-9) {
    const scale = Math.max(Math.abs(left), Math.abs(right), 1);
    return Math.abs(left - right) <= Math.max(absoluteTolerance, scale * 1e-9);
  }

  function strictFiniteNumber(value) {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

  function validateAvailableTurnover(turnover) {
    if (!turnover || typeof turnover !== "object" || turnover.available !== true) return null;
    const todayAmount = strictFiniteNumber(turnover.today_amount_cny);
    const previousAmount = strictFiniteNumber(turnover.previous_same_time_amount_cny);
    const difference = strictFiniteNumber(turnover.difference_cny);
    const differenceRatio = strictFiniteNumber(turnover.difference_ratio);
    const neutralBand = strictFiniteNumber(turnover.neutral_band_ratio);
    const direction = typeof turnover.direction === "string"
      ? turnover.direction.trim().toLowerCase()
      : "";

    if (
      !validDateOnly(turnover.today_date) ||
      !validDateOnly(turnover.previous_date) ||
      !validTimeOnly(turnover.as_of) ||
      todayAmount === null ||
      todayAmount < 0 ||
      previousAmount === null ||
      previousAmount <= 0 ||
      difference === null ||
      differenceRatio === null ||
      neutralBand === null ||
      neutralBand < 0 ||
      neutralBand > 1 ||
      !["expand", "shrink", "flat"].includes(direction) ||
      (turnover.reason !== null && turnover.reason !== undefined)
    ) {
      return null;
    }

    const expectedDifference = todayAmount - previousAmount;
    const expectedRatio = expectedDifference / previousAmount;
    const expectedDirection = expectedRatio > neutralBand
      ? "expand"
      : expectedRatio < -neutralBand
        ? "shrink"
        : "flat";
    if (
      !numbersClose(difference, expectedDifference, 1e-6) ||
      !numbersClose(differenceRatio, expectedRatio) ||
      direction !== expectedDirection
    ) {
      return null;
    }

    return {
      as_of: turnover.as_of,
      difference_cny: difference,
      difference_ratio: differenceRatio,
      direction,
      neutral_band_ratio: neutralBand,
      previous_same_time_amount_cny: previousAmount,
      today_amount_cny: todayAmount,
    };
  }

  function renderTurnover(snapshot) {
    const turnover = snapshot.turnover && typeof snapshot.turnover === "object" ? snapshot.turnover : {};
    const freshness = turnoverFreshness(snapshot);
    const comparison = validateAvailableTurnover(turnover);
    const available = freshness.status !== "unavailable" && comparison !== null;
    const stale = available && freshness.status === "stale";
    const directionLabels = { expand: "放量", shrink: "缩量", flat: "持平", unknown: "待判断" };
    const ratio = available ? ratioAsPercent(comparison.difference_ratio) : null;
    const neutralBand = available ? ratioAsPercent(comparison.neutral_band_ratio) : null;

    byId("turnover-today").textContent = available ? formatCny(comparison.today_amount_cny) : "--";
    byId("turnover-previous").textContent = available ? formatCny(comparison.previous_same_time_amount_cny) : "--";
    const difference = byId("turnover-difference");
    difference.textContent = available ? formatCny(comparison.difference_cny, true) : "--";
    setTone(difference, available ? comparison.difference_cny : null);
    const ratioNode = byId("turnover-ratio");
    ratioNode.textContent = available ? formatRatio(comparison.difference_ratio) : "--";
    setTone(ratioNode, available ? ratio : null);

    const badgeDirection = available ? comparison.direction : "unknown";
    const badge = byId("turnover-direction");
    badge.textContent = stale
      ? `延迟 · ${directionLabels[badgeDirection]}`
      : available
        ? directionLabels[badgeDirection]
        : "不可用";
    badge.className = `direction-badge direction-${badgeDirection}`;
    const definition = neutralBand === null
      ? "仅比较昨日同一交易分钟，不与昨日全天成交额混比。"
      : `仅比较昨日同一交易分钟；±${neutralBand.toFixed(1)}% 以内按持平处理。`;
    const unavailableReason = typeof turnover.reason === "string" && turnover.reason.trim()
      ? turnover.reason.trim()
      : comparison === null && turnover.available === true
        ? "成交额数据字段不完整，暂时无法显示。"
        : "成交额数据暂不可用。";
    byId("turnover-note").textContent = !available
      ? unavailableReason
      : stale
        ? `数据延迟；截至 ${comparison.as_of}。${definition}`
        : definition;
  }

  function createSvgElement(tag, attributes = {}) {
    const element = document.createElementNS(SVG_NAMESPACE, tag);
    Object.entries(attributes).forEach(([name, value]) => {
      if (value !== null && value !== undefined) element.setAttribute(name, String(value));
    });
    return element;
  }

  function formatShortTime(value) {
    if (!value) return "--";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "--";
    return new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone: "Asia/Shanghai",
    }).format(parsed);
  }

  function formatChartCny(value) {
    const parsed = finiteNumber(value);
    if (parsed === null) return "--";
    if (Math.abs(parsed) < 1) return "0";
    const sign = parsed > 0 ? "+" : parsed < 0 ? "−" : "";
    const absolute = Math.abs(parsed);
    if (absolute >= 1e8) return `${sign}${(absolute / 1e8).toFixed(1)}亿`;
    if (absolute >= 1e4) return `${sign}${(absolute / 1e4).toFixed(0)}万`;
    return `${sign}${absolute.toFixed(0)}`;
  }

  function sectorFlowSymlog(value) {
    if (!Number.isFinite(value) || value === 0) return 0;
    return Math.sign(value)
      * Math.log1p(Math.abs(value) / SECTOR_FLOW_SYMLOG_CONSTANT_CNY);
  }

  function sectorFlowSymlogInverse(value) {
    if (!Number.isFinite(value) || value === 0) return 0;
    return Math.sign(value)
      * Math.expm1(Math.abs(value)) * SECTOR_FLOW_SYMLOG_CONSTANT_CNY;
  }

  function sectorFlowPayload(snapshot, scope) {
    const raw = snapshot && (scope === "offense"
      ? snapshot.offense_sector_flow_trajectory
      : snapshot.sector_flow_trajectory);
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    if (raw.contract !== "sector_flow_trajectory.v1" || raw.schema_version !== 1) return null;
    if (text(raw.direction, scope).toLowerCase() !== scope) return null;
    const rawStatus = text(raw.status, "unavailable").toLowerCase();
    const status = new Set(["ready", "collecting", "partial", "unavailable"]).has(rawStatus)
      ? rawStatus
      : "unavailable";
    return {
      asOf: raw.as_of,
      direction: scope,
      flags: stringList(raw.flags),
      marketPhase: text(raw.market_phase, "unknown").toLowerCase(),
      reason: text(raw.reason, ""),
      sectors: asArray(raw.sectors)
        .filter((item) => item && typeof item === "object")
        .slice(0, MAX_SECTOR_FLOW_SERIES),
      status,
      tradeDate: text(raw.trade_date, ""),
    };
  }

  function sectorFlowSelection(scope) {
    return scope === "offense"
      ? state.offenseSectorFlowSelection
      : state.sectorFlowSelection;
  }

  function sectorFlowElement(scope, name) {
    const normalized = scope === "offense" ? "offense" : "defense";
    return byId(`sector-flow-${normalized}-${name}`);
  }

  function setSectorFlowSelection(scope, selection) {
    if (scope === "offense") state.offenseSectorFlowSelection = selection;
    else state.sectorFlowSelection = selection;
  }

  function sectorFlowSelectionStorageKey(scope) {
    return scope === "offense"
      ? STORAGE_KEYS.offenseSectorFlowSelection
      : STORAGE_KEYS.sectorFlowSelection;
  }

  function defaultSectorFlowSelection(payload) {
    const scope = payload.direction === "offense" ? "offense" : "defense";
    const available = new Set(payload.sectors.map((item) => text(item.sector_key, "")));
    const offenseConcepts = scope === "offense"
      ? payload.sectors
        .filter((item) => item.layer === "concept" && item.latest)
        .slice(0, 12)
        .map((item) => text(item.sector_key, ""))
        .filter(Boolean)
      : [];
    return (offenseConcepts.length ? offenseConcepts : DEFAULT_SECTOR_FLOW_SELECTIONS[scope])
      .filter((key) => available.has(key));
  }

  function ensureSectorFlowSelection(payload) {
    const scope = payload.direction === "offense" ? "offense" : "defense";
    let selected = sectorFlowSelection(scope);
    if (selected === null) {
      selected = new Set(defaultSectorFlowSelection(payload));
      setSectorFlowSelection(scope, selected);
    }
    return selected;
  }

  function sectorFlowDisplayPayload(payload) {
    const selected = ensureSectorFlowSelection(payload);
    const sectors = payload.sectors.flatMap((item) => {
      const key = text(item.sector_key, "");
      const isSelected = selected.has(key);
      const changeDelta = finiteNumber(item?.latest?.change_delta_5m_pct);
      const triggered = changeDelta !== null
        && Math.abs(changeDelta) >= state.sectorFlowSurgeThreshold;
      const automatic = triggered && !isSelected;
      if (!isSelected && !automatic) return [];
      return [{
        ...item,
        _display: {
          automatic,
          changeDelta,
          direction: changeDelta > 0 ? "strengthening" : "weakening",
          triggered,
        },
      }];
    });
    return {
      ...payload,
      overrideCount: sectors.filter((item) => item._display.automatic).length,
      sectors,
    };
  }

  function renderSectorFlowPicker(payload, displayPayload, scope) {
    const selected = ensureSectorFlowSelection(payload);
    const options = sectorFlowElement(scope, "picker-options");
    options.replaceChildren();
    payload.sectors.forEach((item) => {
      const key = text(item.sector_key, "");
      if (!key) return;
      const label = createElement("label", "sector-flow-picker__option");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = selected.has(key);
      checkbox.dataset.sectorFlowKey = key;
      label.append(
        checkbox,
        createElement("span", "", text(item.name, key)),
        createElement(
          "small",
          "",
          item.layer === "concept"
            ? `细分 · ${text(item.parent_name, "进攻行业")}`
            : text(item.category_name, "待分类"),
        ),
      );
      options.append(label);
    });
    const selectedAvailable = payload.sectors.filter((item) => (
      selected.has(text(item.sector_key, ""))
    )).length;
    sectorFlowElement(scope, "selection-count").textContent = `${selectedAvailable} / ${payload.sectors.length}`;
    sectorFlowElement(scope, "auto-count").textContent = `突变越权 ${displayPayload.overrideCount}`;
    sectorFlowElement(scope, "surge-threshold").value = String(state.sectorFlowSurgeThreshold);
  }

  function sectorFlowPointValue(point, mode) {
    return finiteNumber(mode === "delta_5m" ? point.delta_5m_cny : point.cumulative_cny);
  }

  function shanghaiMinuteOfDay(value) {
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return null;
    const parts = new Intl.DateTimeFormat("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
      timeZone: "Asia/Shanghai",
    }).formatToParts(parsed);
    const hour = Number(parts.find((part) => part.type === "hour")?.value);
    const minute = Number(parts.find((part) => part.type === "minute")?.value);
    return Number.isFinite(hour) && Number.isFinite(minute) ? hour * 60 + minute : null;
  }

  function sectorFlowTradingMinute(value, segment) {
    const minute = shanghaiMinuteOfDay(value);
    if (minute === null) return null;
    if (segment === "am" && minute >= 9 * 60 + 30 && minute <= 11 * 60 + 30) {
      return minute - (9 * 60 + 30);
    }
    if (segment === "pm" && minute >= 13 * 60 && minute <= 15 * 60) {
      return 120 + minute - 13 * 60;
    }
    return null;
  }

  function formatMinuteOfDay(value) {
    const minute = Math.round(value);
    return `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;
  }

  function formatSectorFlowTradingMinute(value) {
    if (!Number.isFinite(value)) return "--";
    if (Math.abs(value - 120) < 0.01) return "11:30 / 13:00";
    return value < 120
      ? formatMinuteOfDay(9 * 60 + 30 + value)
      : formatMinuteOfDay(13 * 60 + value - 120);
  }

  function sectorFlowSegments(item, mode) {
    const segments = [];
    let current = [];
    let previousSegment = null;
    let previousTime = null;
    const flush = () => {
      if (current.length) segments.push(current);
      current = [];
    };
    asArray(item.points).forEach((point) => {
      if (!point || typeof point !== "object") {
        flush();
        previousSegment = null;
        previousTime = null;
        return;
      }
      const value = sectorFlowPointValue(point, mode);
      const time = Date.parse(point.provider_as_of);
      const segment = text(point.session_segment, "");
      const tradingMinute = sectorFlowTradingMinute(point.provider_as_of, segment);
      if (value === null || !Number.isFinite(time) || tradingMinute === null) {
        flush();
        previousSegment = null;
        previousTime = null;
        return;
      }
      if (
        current.length
        && (segment !== previousSegment || (previousTime !== null && time <= previousTime))
      ) {
        flush();
      }
      current.push({ point, segment, time, tradingMinute, value });
      previousSegment = segment;
      previousTime = time;
    });
    flush();
    return segments;
  }

  function hasRenderableSectorFlow(item, mode) {
    return sectorFlowSegments(item, mode).some((segment) => segment.length >= 2);
  }

  function sectorFlowColor(item, index) {
    return SECTOR_FLOW_COLORS[text(item.sector_key, "")] || [
      "#ff7c79", "#f4bb5f", "#55c8d2", "#c58cff",
      "#78a9ff", "#ff9b69", "#f0cf65", "#8799a4",
    ][index % 8];
  }

  function hideSectorFlowTooltip(scope) {
    const tooltip = sectorFlowElement(scope, "tooltip");
    tooltip.hidden = true;
    tooltip.replaceChildren();
  }

  function showSectorFlowTooltip(event, item, point, mode, scope) {
    const tooltip = sectorFlowElement(scope, "tooltip");
    const shell = sectorFlowElement(scope, "chart-shell");
    const value = sectorFlowPointValue(point, mode);
    const valueLabel = mode === "delta_5m" ? "近 5 分钟变化" : "当日累计估计";
    tooltip.replaceChildren(
      createElement("strong", "", text(item.name, item.sector_key)),
      createElement("span", "", `${formatShortTime(point.provider_as_of)} · ${valueLabel}`),
      createElement("span", toneClass(value), formatCny(value, true)),
    );
    tooltip.hidden = false;
    const bounds = shell.getBoundingClientRect();
    const left = Math.max(8, Math.min(shell.clientWidth - 238, event.clientX - bounds.left + 12));
    const top = Math.max(8, Math.min(shell.clientHeight - 78, event.clientY - bounds.top + 12));
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  }

  function renderSectorFlowLegend(series, scope) {
    const legend = sectorFlowElement(scope, "legend");
    legend.replaceChildren();
    series.forEach(({ item, color }) => {
      const entry = createElement("span", "sector-flow-legend-item");
      const swatch = createElement("i");
      swatch.style.background = color;
      entry.append(swatch, document.createTextNode(text(item.name, item.sector_key)));
      legend.append(entry);
    });
  }

  function renderSectorFlowMiniChart(payload, scope) {
    const svg = sectorFlowElement(scope, "mini-chart");
    svg.replaceChildren();

    const mode = state.sectorFlowMode[scope];
    const series = asArray(payload?.sectors)
      .map((item, index) => ({
        color: sectorFlowColor(item, index),
        item,
        segments: sectorFlowSegments(item, mode),
      }))
      .filter((entry) => entry.segments.some((segment) => segment.length >= 2))
      .slice(0, MAX_SECTOR_FLOW_CHART_SERIES);
    if (!series.length) {
      const message = createSvgElement("text", {
        x: 210,
        y: 36,
        fill: "#708692",
        "font-size": 10,
        "text-anchor": "middle",
      });
      message.textContent = mode === "delta_5m" ? "近 5 分钟轨迹积累中" : "资金轨迹积累中";
      svg.append(message);
      return;
    }

    const width = 420;
    const height = 64;
    const margin = { top: 6, right: 5, bottom: 6, left: 5 };
    const allPoints = series.flatMap((entry) => entry.segments.flat());
    let xMin = Math.min(...allPoints.map((point) => point.tradingMinute));
    let xMax = Math.max(...allPoints.map((point) => point.tradingMinute));
    if (xMin === xMax) {
      xMin -= 1;
      xMax += 1;
    }
    const values = [0, ...allPoints.map((point) => point.value)];
    let yMin = Math.min(...values);
    let yMax = Math.max(...values);
    if (yMin === yMax) {
      const pad = Math.max(Math.abs(yMin) * 0.1, 100_000_000);
      yMin -= pad;
      yMax += pad;
    } else {
      const pad = (yMax - yMin) * 0.08;
      yMin -= pad;
      yMax += pad;
    }
    const x = (value) => margin.left
      + ((value - xMin) / (xMax - xMin)) * (width - margin.left - margin.right);
    const y = (value) => margin.top
      + ((yMax - value) / (yMax - yMin)) * (height - margin.top - margin.bottom);

    svg.append(createSvgElement("line", {
      x1: margin.left,
      x2: width - margin.right,
      y1: y(0),
      y2: y(0),
      stroke: "#8799a4",
      "stroke-opacity": 0.38,
      "stroke-width": 1,
      "vector-effect": "non-scaling-stroke",
    }));
    series.forEach((entry) => {
      const group = createSvgElement("g", {
        "data-mini-sector-flow": text(entry.item.sector_key, "unknown"),
      });
      entry.segments.forEach((segment) => {
        if (segment.length < 2) return;
        const pathData = segment
          .map((point, index) => `${index ? "L" : "M"}${x(point.tradingMinute).toFixed(2)},${y(point.value).toFixed(2)}`)
          .join(" ");
        group.append(createSvgElement("path", {
          d: pathData,
          fill: "none",
          stroke: entry.color,
          "stroke-linecap": "round",
          "stroke-linejoin": "round",
          "stroke-opacity": 0.88,
          "stroke-width": 1.55,
          "vector-effect": "non-scaling-stroke",
        }));
      });
      svg.append(group);
    });
  }

  function renderSectorFlowChart(payload, scope) {
    const svg = sectorFlowElement(scope, "chart");
    const empty = sectorFlowElement(scope, "empty");
    svg.replaceChildren();
    hideSectorFlowTooltip(scope);

    const mode = state.sectorFlowMode[scope];
    const series = payload.sectors
      .map((item, index) => ({
        color: sectorFlowColor(item, index),
        item,
        segments: sectorFlowSegments(item, mode),
      }))
      .filter((entry) => entry.segments.some((segment) => segment.length >= 2))
      .slice(0, MAX_SECTOR_FLOW_CHART_SERIES);
    if (!series.length) {
      empty.hidden = false;
      empty.textContent = mode === "delta_5m"
        ? "近 5 分钟同源基线仍在积累，不会用相邻分钟或跨午休数据替代。"
        : "至少需要两个连续同源水位才能绘制资金轨迹；当前只保留观察分层。";
      renderSectorFlowLegend([], scope);
      return;
    }
    empty.hidden = true;

    const width = 980;
    const height = 360;
    const margin = { top: 24, right: 185, bottom: 38, left: 72 };
    const plotRight = width - margin.right;
    const plotBottom = height - margin.bottom;
    const allPoints = series.flatMap((entry) => entry.segments.flat());
    let xMin = Math.min(...allPoints.map((point) => point.tradingMinute));
    let xMax = Math.max(...allPoints.map((point) => point.tradingMinute));
    if (xMin === xMax) {
      xMin -= 1;
      xMax += 1;
    }
    const values = [0, ...allPoints.map((point) => point.value)];
    const transformedValues = values.map(sectorFlowSymlog);
    let yScaleMin = Math.min(...transformedValues);
    let yScaleMax = Math.max(...transformedValues);
    if (yScaleMin === yScaleMax) {
      const pad = Math.max(Math.abs(yScaleMin) * 0.1, Math.log(2));
      yScaleMin -= pad;
      yScaleMax += pad;
    } else {
      const pad = (yScaleMax - yScaleMin) * 0.08;
      yScaleMin -= pad;
      yScaleMax += pad;
    }
    const x = (value) => margin.left + ((value - xMin) / (xMax - xMin)) * (plotRight - margin.left);
    const baseY = (transformedValue) => margin.top
      + ((yScaleMax - transformedValue) / (yScaleMax - yScaleMin))
        * (plotBottom - margin.top);
    const endpointScaleRows = series
      .map((entry) => {
        const finalSegment = [...entry.segments].reverse().find((segment) => segment.length >= 2);
        const endpoint = finalSegment ? finalSegment[finalSegment.length - 1] : null;
        if (!endpoint) return null;
        const transformedValue = sectorFlowSymlog(endpoint.value);
        return { transformedValue, baseY: baseY(transformedValue) };
      })
      .filter(Boolean)
      .sort((left, right) => left.baseY - right.baseY);
    const effectiveEndpointGap = endpointScaleRows.length > 1
      ? Math.min(
        SECTOR_FLOW_ENDPOINT_GAP_PX,
        (plotBottom - margin.top - 14) / (endpointScaleRows.length - 1),
      )
      : SECTOR_FLOW_ENDPOINT_GAP_PX;
    endpointScaleRows.forEach((row, index) => {
      const minimum = index === 0
        ? margin.top + 7
        : endpointScaleRows[index - 1].allocatedY + effectiveEndpointGap;
      row.allocatedY = Math.max(row.baseY, minimum);
    });
    if (endpointScaleRows.length) {
      const overflow = endpointScaleRows[endpointScaleRows.length - 1].allocatedY - (plotBottom - 7);
      if (overflow > 0) {
        endpointScaleRows[endpointScaleRows.length - 1].allocatedY = plotBottom - 7;
        for (let index = endpointScaleRows.length - 2; index >= 0; index -= 1) {
          endpointScaleRows[index].allocatedY = Math.min(
            endpointScaleRows[index].allocatedY,
            endpointScaleRows[index + 1].allocatedY - effectiveEndpointGap,
          );
        }
      }
    }
    const endpointScaleKnots = [
      { transformedValue: yScaleMax, allocatedY: margin.top },
      ...endpointScaleRows,
      { transformedValue: yScaleMin, allocatedY: plotBottom },
    ].sort((left, right) => right.transformedValue - left.transformedValue);
    const distinctScaleKnots = endpointScaleKnots.filter((knot, index) => (
      index === 0
      || Math.abs(knot.transformedValue - endpointScaleKnots[index - 1].transformedValue) > 1e-9
    ));
    const yFromTransformed = (transformedValue) => {
      if (transformedValue >= distinctScaleKnots[0].transformedValue) return distinctScaleKnots[0].allocatedY;
      for (let index = 1; index < distinctScaleKnots.length; index += 1) {
        const upper = distinctScaleKnots[index - 1];
        const lower = distinctScaleKnots[index];
        if (transformedValue >= lower.transformedValue) {
          const ratio = (upper.transformedValue - transformedValue)
            / (upper.transformedValue - lower.transformedValue);
          return upper.allocatedY + ratio * (lower.allocatedY - upper.allocatedY);
        }
      }
      return distinctScaleKnots[distinctScaleKnots.length - 1].allocatedY;
    };
    const transformedFromY = (yPosition) => {
      if (yPosition <= distinctScaleKnots[0].allocatedY) return distinctScaleKnots[0].transformedValue;
      for (let index = 1; index < distinctScaleKnots.length; index += 1) {
        const upper = distinctScaleKnots[index - 1];
        const lower = distinctScaleKnots[index];
        if (yPosition <= lower.allocatedY) {
          const ratio = (yPosition - upper.allocatedY) / (lower.allocatedY - upper.allocatedY);
          return upper.transformedValue
            + ratio * (lower.transformedValue - upper.transformedValue);
        }
      }
      return distinctScaleKnots[distinctScaleKnots.length - 1].transformedValue;
    };
    const y = (value) => yFromTransformed(sectorFlowSymlog(value));

    const grid = createSvgElement("g", { "aria-hidden": "true" });
    const zeroY = y(0);
    const plotHeight = plotBottom - margin.top;
    const tickPositions = [zeroY];
    [0, 0.25, 0.5, 0.75, 1].forEach((ratio) => {
      const candidate = margin.top + ratio * plotHeight;
      if (tickPositions.every((position) => Math.abs(position - candidate) >= 16)) {
        tickPositions.push(candidate);
      }
    });
    tickPositions.sort((left, right) => left - right).forEach((yPosition) => {
      const transformedValue = transformedFromY(yPosition);
      const value = sectorFlowSymlogInverse(transformedValue);
      if (Math.abs(yPosition - zeroY) >= 1) {
        grid.append(createSvgElement("line", {
          x1: margin.left,
          x2: plotRight,
          y1: yPosition,
          y2: yPosition,
          stroke: "#20323d",
          "stroke-width": 1,
        }));
      }
      const label = createSvgElement("text", {
        x: margin.left - 10,
        y: yPosition + 3,
        fill: "#708692",
        "font-size": 9,
        "text-anchor": "end",
      });
      label.textContent = formatChartCny(value);
      grid.append(label);
    });
    const scaleLabel = createSvgElement("text", {
      x: margin.left,
      y: 13,
      fill: "#708692",
      "font-size": 8,
    });
    scaleLabel.textContent = "Y 轴密度自适应 · 刻度为真实金额";
    grid.append(scaleLabel);
    grid.append(createSvgElement("line", {
      x1: margin.left,
      x2: plotRight,
      y1: zeroY,
      y2: zeroY,
      stroke: "#8799a4",
      "stroke-opacity": 0.72,
      "stroke-width": 1.2,
    }));
    [xMin, (xMin + xMax) / 2, xMax].forEach((value, index) => {
      const label = createSvgElement("text", {
        x: x(value),
        y: height - 13,
        fill: "#708692",
        "font-size": 9,
        "text-anchor": index === 0 ? "start" : index === 2 ? "end" : "middle",
      });
      label.textContent = formatSectorFlowTradingMinute(value);
      grid.append(label);
    });
    const includesMorning = allPoints.some((point) => point.segment === "am");
    const includesAfternoon = allPoints.some((point) => point.segment === "pm");
    if (includesMorning && includesAfternoon && xMin <= 120 && xMax >= 120) {
      const breakX = x(120);
      grid.append(createSvgElement("line", {
        x1: breakX,
        x2: breakX,
        y1: margin.top,
        y2: plotBottom,
        stroke: "#4b626f",
        "stroke-dasharray": "3 5",
        "stroke-opacity": 0.62,
        "stroke-width": 1,
      }));
      const breakLabel = createSvgElement("text", {
        x: breakX + 5,
        y: margin.top + 10,
        fill: "#708692",
        "font-size": 8,
      });
      breakLabel.textContent = "午间断点";
      grid.append(breakLabel);
    }
    svg.append(grid);

    const endpointLabels = [];
    series.forEach((entry) => {
      const lineGroup = createSvgElement("g", { "data-sector-flow": text(entry.item.sector_key, "unknown") });
      entry.segments.forEach((segment) => {
        if (segment.length < 2) return;
        const pathData = segment
          .map((point, index) => `${index ? "L" : "M"}${x(point.tradingMinute).toFixed(2)},${y(point.value).toFixed(2)}`)
          .join(" ");
        lineGroup.append(createSvgElement("path", {
          d: pathData,
          fill: "none",
          stroke: entry.color,
          "stroke-linecap": "round",
          "stroke-linejoin": "round",
          "stroke-width": 2.1,
          "vector-effect": "non-scaling-stroke",
        }));
        segment.forEach((point) => {
          const target = createSvgElement("circle", {
            cx: x(point.tradingMinute),
            cy: y(point.value),
            r: 6,
            fill: "transparent",
            "data-provider-as-of": point.point.provider_as_of,
          });
          target.addEventListener("pointerenter", (event) => (
            showSectorFlowTooltip(event, entry.item, point.point, mode, scope)
          ));
          target.addEventListener("pointermove", (event) => (
            showSectorFlowTooltip(event, entry.item, point.point, mode, scope)
          ));
          target.addEventListener("pointerleave", () => hideSectorFlowTooltip(scope));
          lineGroup.append(target);
        });
      });
      const finalSegment = [...entry.segments].reverse().find((segment) => segment.length >= 2);
      if (finalSegment) {
        const endpoint = finalSegment[finalSegment.length - 1];
        lineGroup.append(createSvgElement("circle", {
          cx: x(endpoint.tradingMinute),
          cy: y(endpoint.value),
          r: 3.3,
          fill: entry.color,
          stroke: "#071016",
          "stroke-width": 1.2,
          "data-sector-flow-endpoint": text(entry.item.sector_key, "unknown"),
        }));
        endpointLabels.push({
          actualY: y(endpoint.value),
          color: entry.color,
          item: entry.item,
          value: endpoint.value,
          x: x(endpoint.tradingMinute),
        });
      }
      svg.append(lineGroup);
    });

    endpointLabels.sort((left, right) => left.actualY - right.actualY);
    endpointLabels.forEach((label) => {
      const textX = plotRight + 12;
      const textNode = createSvgElement("text", {
        x: textX,
        y: label.actualY + 3,
        fill: label.color,
        "font-size": 10,
        "font-weight": 700,
        "data-sector-flow-endpoint-label": text(label.item.sector_key, "unknown"),
      });
      textNode.textContent = `${text(label.item.name, label.item.sector_key)} ${formatChartCny(label.value)}`;
      svg.append(textNode);
    });
    renderSectorFlowLegend(series, scope);
  }

  function renderSectorFlowLeaders(item, latest) {
    const snapshot = item.leader_snapshot && typeof item.leader_snapshot === "object"
      ? item.leader_snapshot
      : {};
    const leaders = asArray(snapshot.leaders).slice(0, 3);
    const block = createElement("div", "sector-flow-observation-item__leaders");
    block.append(createElement("span", "sector-flow-observation-item__leaders-label", "领涨股"));
    const list = createElement("div", "sector-flow-observation-item__leaders-list");
    if (!leaders.length) {
      list.append(createElement(
        "span",
        "sector-flow-leaders-empty",
        text(snapshot.status_label, "领涨股数据暂缺"),
      ));
      block.append(list);
      return block;
    }
    leaders.forEach((leader) => {
      const instrumentId = text(leader.instrument_id, "");
      const code = instrumentId.includes(".") ? instrumentId.split(".")[0] : instrumentId;
      const leaderChange = finiteNumber(leader.change_pct);
      const chip = createElement("span", "sector-flow-leader");
      chip.append(
        createElement("strong", "", text(leader.name, code || "未命名股票")),
        createElement("small", "", code),
        createElement(
          "span",
          leaderChange === null ? "" : toneClass(leaderChange),
          leaderChange === null ? "--" : formatChangePct(leaderChange),
        ),
      );
      list.append(chip);
    });
    block.append(list);
    return block;
  }

  function renderSectorFlowObservations(payload, scope) {
    const target = sectorFlowElement(scope, "observation-list");
    target.replaceChildren();
    if (!payload.sectors.length) {
      const scopeLabel = payload.direction === "offense" ? "进攻" : "防守";
      target.append(createElement("p", "empty-state", `等待可比较的${scopeLabel}方向证据`));
      return;
    }
    const sectors = [...payload.sectors].sort((left, right) => {
      const leftAutomatic = Boolean(left?._display?.automatic);
      const rightAutomatic = Boolean(right?._display?.automatic);
      if (leftAutomatic !== rightAutomatic) return rightAutomatic ? 1 : -1;
      if (leftAutomatic && rightAutomatic) {
        const leftDelta = Math.abs(finiteNumber(left?._display?.changeDelta) || 0);
        const rightDelta = Math.abs(finiteNumber(right?._display?.changeDelta) || 0);
        if (leftDelta !== rightDelta) return rightDelta - leftDelta;
      }
      const leftChange = finiteNumber(left?.latest?.change_pct);
      const rightChange = finiteNumber(right?.latest?.change_pct);
      if (leftChange === null && rightChange === null) return 0;
      if (leftChange === null) return 1;
      if (rightChange === null) return -1;
      return rightChange - leftChange;
    });
    sectors.forEach((item, index) => {
      const latest = item.latest && typeof item.latest === "object" ? item.latest : {};
      const change = finiteNumber(latest.change_pct);
      const rising = change !== null && change > 0;
      const display = item._display && typeof item._display === "object" ? item._display : {};
      const changeDelta = finiteNumber(display.changeDelta);
      const automatic = Boolean(display.automatic);
      const triggered = Boolean(display.triggered) && changeDelta !== null;
      const card = createElement(
        "article",
        `sector-flow-observation-item ${rising ? "is-rising" : "is-non-rising"}${automatic ? " is-automatic" : ""}`,
      );
      const rank = createElement("span", "sector-flow-rank", `#${index + 1}`);
      const body = createElement("div", "sector-flow-observation-item__body");
      const top = createElement("div", "sector-flow-observation-item__top");
      const title = createElement("div", "sector-flow-observation-item__title");
      const parentName = text(item.parent_name, "");
      title.append(
        createElement("strong", "", text(item.name, item.sector_key)),
        createElement(
          "span",
          "",
          item.layer === "concept" && parentName
            ? `${parentName} · 细分概念`
            : text(item.category_name, "分类待确认"),
        ),
      );
      if (automatic) title.append(createElement("span", "sector-flow-auto-badge", "自动出现"));
      const moveKey = triggered
        ? changeDelta > 0 ? "confirmed_strengthening" : "retreat"
        : change === null ? "unavailable" : rising ? "confirmed_strengthening" : change < 0 ? "retreat" : "observing";
      const moveLabel = triggered
        ? `${automatic ? "自动出现 · " : ""}${changeDelta > 0 ? "突然增强" : "突然衰弱"} ${changeDelta > 0 ? "+" : ""}${changeDelta.toFixed(2)}个百分点/5分`
        : change === null ? "涨幅待更新" : rising ? "板块上涨" : change < 0 ? "板块下跌" : "板块平盘";
      const tier = createElement(
        "span",
        `sector-flow-tier sector-flow-tier--${moveKey}`,
        moveLabel,
      );
      top.append(title, tier);

      const metrics = createElement("div", "sector-flow-observation-item__metrics");
      const metricValues = [
        ["板块涨幅", formatChangePct(latest.change_pct), toneClass(latest.change_pct)],
        ["5分涨速", changeDelta === null ? "--" : `${changeDelta > 0 ? "+" : ""}${changeDelta.toFixed(2)}个百分点`, toneClass(changeDelta)],
        ["近5分资金", formatCny(latest.delta_5m_cny, true), ""],
      ];
      metricValues.forEach(([label, value, valueClass]) => {
        const node = createElement("span", "", label);
        node.append(createElement("strong", valueClass, value));
        metrics.append(node);
      });
      body.append(top, metrics);
      const leaders = renderSectorFlowLeaders(item, latest);
      if (leaders) body.append(leaders);
      card.append(rank, body);
      target.append(card);
    });
  }

  function renderSectorFlowUnavailable(message, scope) {
    const svg = sectorFlowElement(scope, "chart");
    svg.replaceChildren();
    renderSectorFlowMiniChart(null, scope);
    sectorFlowElement(scope, "legend").replaceChildren();
    hideSectorFlowTooltip(scope);
    const empty = sectorFlowElement(scope, "empty");
    empty.hidden = false;
    empty.textContent = message;
    const list = sectorFlowElement(scope, "observation-list");
    const scopeLabel = scope === "offense" ? "进攻" : "防御";
    list.replaceChildren(createElement("p", "empty-state", `等待可比较的${scopeLabel}方向证据`));
    sectorFlowElement(scope, "status").textContent = "资金轨迹不可用";
    sectorFlowElement(scope, "chart-caption").textContent = "不会依据单点涨幅补画";
    sectorFlowElement(scope, "chart-as-of").textContent = "统一时点 --";
  }

  function renderSectorFlowTrajectory(snapshot, scope) {
    const payload = sectorFlowPayload(snapshot, scope);
    const scopeLabel = scope === "offense" ? "进攻" : "防御";
    if (!payload) {
      renderSectorFlowUnavailable("资金轨迹子契约暂不可用，不会影响上方宏观盘面结论。", scope);
      return;
    }
    const displayPayload = sectorFlowDisplayPayload(payload);
    renderSectorFlowPicker(payload, displayPayload, scope);
    renderSectorFlowObservations(displayPayload, scope);
    const deltaAvailable = displayPayload.sectors.some((item) => hasRenderableSectorFlow(item, "delta_5m"));
    if (state.sectorFlowMode[scope] === "delta_5m" && !deltaAvailable) {
      state.sectorFlowMode[scope] = "cumulative";
    }
    const panel = document.querySelector(`.sector-flow-panel[data-flow-scope="${scope}"]`);
    panel.querySelectorAll("[data-flow-mode]").forEach((button) => {
      const mode = button.dataset.flowMode;
      const active = mode === state.sectorFlowMode[scope];
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
      button.disabled = mode === "delta_5m" && !deltaAvailable;
      button.title = button.disabled ? "至少需要两个带同源 5 分钟基线的真实采样点" : "";
    });
    const statusLabels = {
      ready: "同源分钟轨迹已更新",
      collecting: "近 5 分钟基线积累中",
      partial: `部分${scopeLabel}方向可比较`,
      unavailable: "资金轨迹不可用",
    };
    const phaseSuffix = payload.marketPhase === "closed"
      ? " · 今日定格"
      : payload.marketPhase === "midday_break"
        ? " · 午休定格"
        : "";
    sectorFlowElement(scope, "status").textContent = `${statusLabels[payload.status]}${phaseSuffix}`;
    sectorFlowElement(scope, "chart-caption").textContent = state.sectorFlowMode[scope] === "delta_5m"
      ? "近 5 分钟边际净流入 / 出"
      : "当日累计净流入 / 出";
    sectorFlowElement(scope, "chart-as-of").textContent = `轨迹时点 ${formatTimestamp(payload.asOf)}`;
    sectorFlowElement(scope, "note").textContent = payload.status === "unavailable"
      ? `资金轨迹暂不可用（${payload.reason || "缺少可比较同源水位"}）；现有宏观盘面结论保持独立。`
      : scope === "offense"
        ? "进攻方向按行业锚与其细分概念分层；概念轨迹复用同一全市场分钟快照，领涨股来自板块快照并由成分行情补全。资金仍是估计 / 辅助，不构成买卖建议。"
        : "资金为供应商口径下的日内累计估计；行业与概念仅按各自同类分位比较，不代表资金从一个板块确定转移到另一个板块，也不构成买卖建议。";
    renderSectorFlowMiniChart(displayPayload, scope);
    renderSectorFlowChart(displayPayload, scope);
  }

  function renderSectorFlowTrajectories(snapshot) {
    renderSectorFlowTrajectory(snapshot, "defense");
    renderSectorFlowTrajectory(snapshot, "offense");
  }

  function sectorDirection(value) {
    const normalized = text(value, "unknown").toLowerCase();
    if (normalized === "strengthening") return "strengthening";
    if (normalized === "weakening") return "weakening";
    return "flat";
  }

  function renderRotationPulse(snapshot) {
    const rotation = snapshot.rotation && typeof snapshot.rotation === "object" ? snapshot.rotation : {};
    const sectors = asArray(rotation.sectors).filter((item, index, all) => {
      if (!item || typeof item !== "object") return false;
      const key = item.sector_key;
      return all.findIndex((candidate) => candidate && candidate.sector_key === key) === index;
    });
    const strengthening = sectors.filter((sector) => sectorDirection(sector.direction) === "strengthening");
    const weakening = sectors.filter((sector) => sectorDirection(sector.direction) === "weakening");
    const neutralCount = sectors.length - strengthening.length - weakening.length;
    const regime = REGIME_LABELS[text(rotation.regime, "uncertain").toLowerCase()] || "方向未确认";

    byId("rotation-regime").textContent = regime;
    byId("rotation-strengthening-count").textContent = `${strengthening.length}`;
    byId("rotation-weakening-count").textContent = `${weakening.length}`;
    byId("rotation-summary").textContent = text(
      rotation.summary,
      sectors.length
        ? `增强 ${strengthening.length} 个、减弱 ${weakening.length} 个、观察 ${neutralCount} 个；先看内部广度与持续性是否同步。`
        : "轮动证据不可用，暂不判断资金正在涌向哪里。",
    );

  }

  function renderEvidenceList(targetId, items, fallback) {
    const target = byId(targetId);
    target.replaceChildren();
    if (!items.length) {
      target.append(createElement("li", "muted-item", fallback));
      return;
    }
    items.slice(0, 5).forEach((item) => target.append(createElement("li", "", item)));
  }

  function renderInterpretation(snapshot) {
    const guardrail = snapshot.guardrail && typeof snapshot.guardrail === "object" ? snapshot.guardrail : {};
    const freshness = normalizeFreshness(snapshot);
    const interpretation = freshness.status === "stale"
      ? "当前数据陈旧，仅展示最后一份可核验读数；不形成进攻、防御或轮动判断。"
      : freshness.status === "unavailable"
        ? "关键数据不可用，当前不形成盘面判断。"
        : text(guardrail.current_state, "尚未取得足够同向证据，当前结论保持不确定。");
    byId("what-is-happening").textContent = text(
      interpretation,
      "尚未取得足够同向证据，当前结论保持不确定。",
    );
    renderEvidenceList(
      "supporting-evidence",
      stringList(guardrail.supporting_evidence),
      "暂无已确认支持证据",
    );
    renderEvidenceList(
      "counter-evidence",
      stringList(guardrail.counter_evidence),
      "暂无已确认反证",
    );
  }

  function renderScenarios(snapshot) {
    const scenarios = asArray(snapshot.scenarios).slice(0, 2);
    const target = byId("scenario-list");
    target.replaceChildren();
    if (!scenarios.length) {
      target.append(createElement(
        "p",
        "empty-state",
        "暂无有效条件情景。这里不展示精确概率，只展示 if / then 与失效条件。",
      ));
      return;
    }

    scenarios.forEach((scenario, index) => {
      const card = createElement("article", "scenario-card");
      const header = createElement("div", "scenario-card__header");
      header.append(
        createElement("strong", "", `条件情景 ${index + 1}`),
        createElement("span", "", `${text(scenario.horizon_minutes, "5–15")} 分钟`),
      );

      const ifRow = createElement("div", "scenario-rule");
      ifRow.append(
        createElement("span", "", "IF 如果"),
        createElement("p", "", text(scenario.if_condition, "触发条件尚未完整")),
      );
      const thenRow = createElement("div", "scenario-rule");
      thenRow.append(
        createElement("span", "", "THEN"),
        createElement("p", "", text(scenario.then_expectation, "等待更多盘面证据")),
      );
      const invalidation = createElement("p", "scenario-invalidation");
      invalidation.append(
        createElement("strong", "", "失效："),
        document.createTextNode(text(scenario.invalidation, "任一关键证据转弱时不再成立")),
      );
      card.append(header, ifRow, thenRow, invalidation);
      target.append(card);
    });
  }

  function renderChange(snapshot) {
    const change = snapshot.change && typeof snapshot.change === "object" && !Array.isArray(snapshot.change)
      ? snapshot.change
      : {};
    const changedFields = stringList(change.changed_fields);
    const confirmed = change.available === true;
    byId("change-as-of").textContent = confirmed ? formatTimestamp(snapshot.as_of) : "尚无";
    const target = byId("confirmed-change");
    target.replaceChildren();
    const icon = createElement("span", "change-icon", confirmed ? "✓" : "↔");
    icon.setAttribute("aria-hidden", "true");
    const copy = createElement("div");
    copy.append(
      createElement("strong", "", confirmed ? "盘面状态出现确认变化" : "尚未确认状态切换"),
      createElement("p", "", confirmed
        ? text(change.summary, changedFields.length ? `变化字段：${changedFields.join("、")}` : "变化已确认。")
        : CHANGE_REASON_LABELS[change.reason]
          || "需要连续快照支持，单次抖动不会作为变化提醒。"),
    );
    target.append(icon, copy);
  }

  function alertKey(alert) {
    return text(alert && alert.dedupe_key, text(alert && alert.code, "alert"));
  }

  function alertReadKey(alert, snapshot) {
    return `${alertKey(alert)}@${text(snapshot && snapshot.snapshot_id, "unknown")}`;
  }

  function alertListFromSnapshot(snapshot) {
    return asArray(snapshot.alerts)
      .filter((item) => item && typeof item === "object")
      .slice(0, 12);
  }

  function renderAlerts(snapshot) {
    state.alerts = alertListFromSnapshot(snapshot);
    const target = byId("alert-list");
    target.replaceChildren();
    if (!state.alerts.length) {
      target.append(createElement("p", "empty-state", "当前没有后端确认的变化提醒。"));
    } else {
      state.alerts.forEach((alert) => {
        const key = alertKey(alert);
        const read = state.readAlertKeys.has(alertReadKey(alert, snapshot));
        const item = createElement("article", `alert-item${read ? " is-read" : ""}`);
        item.dataset.alertKey = key;
        const severity = text(alert.severity, "info").toLowerCase();
        item.append(
          createElement("span", `alert-severity alert-severity--${severity}`),
          (() => {
            const copy = createElement("div");
            copy.append(
              createElement("strong", "", text(alert.title, text(alert.code, "盘面变化"))),
              createElement("p", "", text(alert.message, "变化已确认")),
            );
            return copy;
          })(),
          createElement("time", "", formatTimestamp(snapshot.as_of)),
        );
        target.append(item);
      });
    }
    updateUnreadCount(snapshot);
  }

  function replayPayload(item) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return null;
    const payload = item.payload && typeof item.payload === "object" ? item.payload : item;
    return payload && payload.contract === "market_watch.v1" ? payload : null;
  }

  function renderReplayDates(history) {
    const select = byId("replay-date-select");
    const dates = asArray(history.dates)
      .map((item) => item && typeof item === "object" ? text(item.trade_date, "") : "")
      .filter(Boolean);
    const target = text(history.trade_date, state.replayTradeDate || dates[0] || "");
    const options = target && !dates.includes(target) ? [target, ...dates] : dates;
    select.replaceChildren();
    if (!options.length) {
      const option = createElement("option", "", target || "尚无记录");
      option.value = target;
      select.append(option);
      select.disabled = true;
    } else {
      options.forEach((tradeDate) => {
        const option = createElement("option", "", tradeDate);
        option.value = tradeDate;
        option.selected = tradeDate === target;
        select.append(option);
      });
      select.disabled = false;
    }
    state.replayTradeDate = target || null;
  }

  function renderReplayTimeline(history) {
    const samples = asArray(history.samples);
    const target = byId("replay-timeline");
    target.replaceChildren();
    byId("replay-sample-count").textContent = `${samples.length} 个分钟样本`;
    const visible = samples.slice(-30).reverse();
    if (!visible.length) {
      target.append(createElement(
        "p",
        "empty-state",
        "该日尚无分钟快照；交易时段由服务器自动记录。",
      ));
      return;
    }
    visible.forEach((item) => {
      const snapshot = replayPayload(item);
      if (!snapshot) return;
      const guardrail = snapshot.guardrail && typeof snapshot.guardrail === "object"
        ? snapshot.guardrail
        : {};
      const freshness = snapshot.freshness && typeof snapshot.freshness === "object"
        ? text(snapshot.freshness.status, "unknown")
        : "unknown";
      const row = createElement("article", "replay-timeline-item");
      row.append(
        createElement("time", "", formatTimestamp(snapshot.as_of)),
        createElement(
          "strong",
          "",
          REGIME_LABELS[text(guardrail.regime, "uncertain").toLowerCase()] || "方向未确认",
        ),
        createElement("span", "", text(guardrail.current_state, "暂无盘面摘要")),
        createElement("small", "", freshness),
      );
      target.append(row);
    });
  }

  function renderReplayAlerts(history) {
    const alerts = asArray(history.alerts);
    const target = byId("replay-alert-list");
    target.replaceChildren();
    byId("replay-alert-count").textContent = `${alerts.length} 条`;
    if (!alerts.length) {
      target.append(createElement("p", "empty-state", "该交易日尚无持久化提醒。"));
      return;
    }
    alerts.slice(-30).reverse().forEach((event) => {
      const alert = event && event.alert && typeof event.alert === "object" ? event.alert : event;
      if (!alert || typeof alert !== "object") return;
      const severity = text(alert.severity, "caution").toLowerCase();
      const item = createElement("article", "alert-item");
      const copy = createElement("div");
      copy.append(
        createElement("strong", "", text(alert.title, text(alert.code, "盘面变化"))),
        createElement("p", "", text(alert.message, "变化已确认")),
      );
      item.append(
        createElement("span", `alert-severity alert-severity--${severity}`),
        copy,
        createElement("time", "", formatTimestamp(event.observed_at || event.as_of)),
      );
      target.append(item);
    });
  }

  function renderReplayHistory(history) {
    if (!history || history.contract !== "market_watch_history.v1" || history.schema_version !== 1) {
      throw new Error("历史接口契约不匹配");
    }
    renderReplayDates(history);
    renderReplayTimeline(history);
    renderReplayAlerts(history);
  }

  function evaluationText(verdict) {
    if (verdict === "passed") return "验收通过";
    if (verdict === "failed") return "需要修复";
    return "数据不足";
  }

  function renderReplayEvaluation(report) {
    if (!report || report.contract !== "market_watch_evaluation.v1" || report.schema_version !== 1) {
      throw new Error("评估接口契约不匹配");
    }
    const metrics = report.metrics && typeof report.metrics === "object" ? report.metrics : {};
    const coverage = metrics.coverage && typeof metrics.coverage === "object" ? metrics.coverage : {};
    const freshness = metrics.freshness && typeof metrics.freshness === "object" ? metrics.freshness : {};
    const regimeTransitions = metrics.regime_transitions && typeof metrics.regime_transitions === "object"
      ? metrics.regime_transitions
      : {};
    const noise = metrics.noise && typeof metrics.noise === "object" ? metrics.noise : {};
    const acceptance = report.acceptance && typeof report.acceptance === "object" ? report.acceptance : {};
    const verdict = text(acceptance.verdict, "insufficient").toLowerCase();

    byId("replay-observed-minutes").textContent = formatCount(coverage.covered_trading_minutes);
    byId("replay-coverage-detail").textContent = `${formatRatio(coverage.trading_minute_coverage_ratio, false)} / ${formatCount(coverage.expected_trading_minutes)} 分钟`;
    byId("replay-fresh-ratio").textContent = formatRatio(freshness.fresh_ratio, false);
    const gap = finiteNumber(coverage.longest_data_gap_seconds);
    byId("replay-max-gap").textContent = gap === null ? "--" : gap >= 60 ? `${(gap / 60).toFixed(1)} 分` : `${gap.toFixed(0)} 秒`;
    byId("replay-transition-count").textContent = formatCount(regimeTransitions.transition_count);
    byId("replay-reversal-count").textContent = formatCount(noise.total_rapid_reversal_count);

    const badge = byId("evaluation-verdict");
    badge.className = `evaluation-badge evaluation-${verdict}`;
    badge.textContent = evaluationText(verdict);
    const windowLabel = report.scope === "multi_day"
      ? `${text(report.date_from, "--")} 至 ${text(report.date_to, "--")} 的 ${formatCount(report.session_count)} 个交易日窗口`
      : "当前交易日";
    byId("evaluation-detail").textContent = verdict === "passed"
      ? `${windowLabel}的采样覆盖、数据新鲜度与快速反转代理均通过本版策略门槛。`
      : verdict === "failed"
        ? "样本已达到验收条件，但数据质量或信号稳定性存在未通过项。"
        : `${windowLabel}的记录尚未达到验收门槛，不会伪装成通过。`;

    const hints = asArray(report.calibration_hints);
    const list = byId("calibration-notes");
    list.replaceChildren();
    if (!hints.length) {
      list.append(createElement("li", "", "尚无可用的校准建议。"));
    } else {
      hints.slice(0, 4).forEach((hint) => list.append(createElement(
        "li",
        "",
        text(hint.action, "保持当前策略并继续累积样本。"),
      )));
    }
  }

  async function fetchReplayData({
    tradeDate = null,
    silent = false,
    evaluationDays = state.evaluationDays,
  } = {}) {
    state.replayHasLoaded = true;
    const requestedTradeDate = tradeDate || state.replayTradeDate;
    const requestedDays = evaluationDays;
    if (state.replayFetchInFlight) {
      if (silent) return;
      state.replayPendingRequest = {
        tradeDate: requestedTradeDate,
        silent,
        evaluationDays: requestedDays,
      };
      return;
    }
    state.replayFetchInFlight = true;
    state.replayLastAttemptAt = Date.now();
    if (!silent) byId("replay-status").textContent = "正在读取回放…";
    const historyQuery = requestedTradeDate
      ? `?trade_date=${encodeURIComponent(requestedTradeDate)}`
      : "";
    const evaluationQuery = requestedDays
      ? `?days=${encodeURIComponent(requestedDays)}`
      : historyQuery;
    try {
      const [historyResponse, evaluationResponse] = await Promise.all([
        fetch(`${HISTORY_ENDPOINT}${historyQuery}`, { cache: "no-store", headers: { Accept: "application/json" } }),
        fetch(`${EVALUATION_ENDPOINT}${evaluationQuery}`, { cache: "no-store", headers: { Accept: "application/json" } }),
      ]);
      if (!historyResponse.ok || !evaluationResponse.ok) {
        throw new Error(`回放服务返回 ${historyResponse.status}/${evaluationResponse.status}`);
      }
      const history = await historyResponse.json();
      const evaluation = await evaluationResponse.json();
      if (state.replayPendingRequest) return;
      renderReplayHistory(history);
      renderReplayEvaluation(evaluation);
      state.replayLastFetchedAt = Date.now();
      const recording = history.recording && typeof history.recording === "object" ? history.recording : {};
      const actualDays = finiteNumber(evaluation.session_count);
      const evaluationLabel = requestedDays
        ? ` · 实得 ${actualDays === null ? 0 : Math.round(actualDays)}/${requestedDays} 日验收`
        : "";
      byId("replay-status").textContent = recording.status === "degraded"
        ? `${text(history.trade_date, "--")} · 记录降级${evaluationLabel}`
        : `${text(history.trade_date, "--")} · 已更新${evaluationLabel}`;
    } catch (error) {
      if (!state.replayPendingRequest) {
        byId("replay-status").textContent = text(error && error.message, "回放暂不可用");
      }
    } finally {
      state.replayFetchInFlight = false;
      const pending = state.replayPendingRequest;
      state.replayPendingRequest = null;
      if (pending) fetchReplayData(pending);
    }
  }

  function renderSnapshot(snapshot) {
    byId("contract-chip").textContent = text(snapshot.contract, "market_watch.v1");
    byId("snapshot-meta").textContent = `snapshot ${text(snapshot.snapshot_id)} · sequence ${text(snapshot.sequence)}`;
    renderDecision(snapshot);
    renderSectorFlowTrajectories(snapshot);
    renderIndexDock(snapshot);
    renderBreadth(snapshot);
    renderTurnover(snapshot);
    renderRotationPulse(snapshot);
    renderInterpretation(snapshot);
    renderScenarios(snapshot);
    renderChange(snapshot);
    renderAlerts(snapshot);
    updateDataRisk(snapshot);
  }

  function alertEligibility(snapshot) {
    const freshness = normalizeFreshness(snapshot);
    const marketState = normalizeMarketState(snapshot);
    if (freshness.status !== "fresh") return { allowed: false, reason: "数据非新鲜，变化提醒已锁定" };
    if (!marketState.isOpen) return { allowed: false, reason: "当前非交易时段，不发送盘中变化提醒" };
    if (state.muted) return { allowed: false, reason: "提醒已手动静音" };
    return { allowed: true, reason: "仅发送后端确认的变化" };
  }

  function isDataRiskAlert(alert) {
    return new Set(["data_degraded", "data_stale", "data_unavailable"]).has(
      text(alert && alert.code, ""),
    );
  }

  function alertCanDeliver(snapshot, alert) {
    const freshness = normalizeFreshness(snapshot);
    const marketState = normalizeMarketState(snapshot);
    if (!marketState.isOpen || state.muted) return false;
    if (isDataRiskAlert(alert)) return freshness.status !== "fresh";
    return freshness.status === "fresh";
  }

  function updateDataRisk(snapshot) {
    if (state.fetchFailed) {
      renderFetchRisk(state.lastFetchError);
      return;
    }
    const freshness = normalizeFreshness(snapshot);
    const overlay = byId("data-risk-overlay");
    const eligibility = alertEligibility(snapshot);
    if (freshness.status === "fresh") {
      overlay.classList.remove("is-visible", "is-warning");
    } else {
      overlay.classList.add("is-visible");
      overlay.classList.toggle("is-warning", ["degraded", "stale"].includes(freshness.status));
      const title = freshness.status === "degraded"
        ? "盘面数据部分降级"
        : freshness.status === "stale"
          ? "盘面数据已经陈旧"
        : freshness.status === "unavailable"
          ? "盘面数据不可用"
          : "无法确认数据新鲜度";
      byId("data-risk-title").textContent = title;
      byId("data-risk-detail").textContent = text(
        freshness.detail,
        "保留最后画面仅供回看，不得用于判断当前盘面。",
      );
    }
    byId("alert-lock-label").textContent = eligibility.allowed ? "盘面变化提醒可用" : "盘面变化提醒已锁定";
    updateAlertDeliveryState(snapshot);
  }

  function renderFetchRisk(error) {
    const overlay = byId("data-risk-overlay");
    overlay.classList.add("is-visible");
    overlay.classList.remove("is-warning");
    byId("data-risk-title").textContent = "实时请求失败";
    byId("data-risk-detail").textContent = state.lastSnapshot
      ? `最后画面已保留；${text(error && error.message, "等待自动重试")}。`
      : `尚无可展示快照；${text(error && error.message, "等待自动重试")}。`;
    byId("alert-lock-label").textContent = "盘面变化提醒已锁定";
    byId("alert-delivery-state").textContent = "请求恢复前不发送提醒";
    byId("alert-delivery-state").parentElement.classList.add("is-locked");
  }

  function updateAlertDeliveryState(snapshot) {
    const eligibility = alertEligibility(snapshot);
    const delivery = byId("alert-delivery-state");
    delivery.textContent = eligibility.reason;
    delivery.parentElement.classList.toggle("is-locked", !eligibility.allowed);
  }

  function wasDeliveredRecently(alert, now = Date.now()) {
    const timestamp = Number(state.deliveredAlertKeys[alertKey(alert)]);
    return Number.isFinite(timestamp) && now - timestamp < ALERT_COOLDOWN_MS;
  }

  function pruneDeliveredKeys(now = Date.now()) {
    const entries = Object.entries(state.deliveredAlertKeys)
      .filter(([, timestamp]) => (
        Number.isFinite(Number(timestamp)) && now - Number(timestamp) < ALERT_COOLDOWN_MS
      ))
      .sort((a, b) => Number(b[1]) - Number(a[1]))
      .slice(0, MAX_STORED_ALERT_KEYS);
    state.deliveredAlertKeys = Object.fromEntries(entries);
    storeJson(STORAGE_KEYS.deliveredAlerts, state.deliveredAlertKeys);
  }

  function showSystemNotification(alert) {
    if (!state.notificationEnabled || !("Notification" in window) || Notification.permission !== "granted") return;
    const notification = new Notification(`盘面刹车器 · ${text(alert.title, "盘面变化")}`, {
      body: text(alert.message, "变化已由后端连续快照确认。"),
      tag: alertKey(alert),
      renotify: false,
      silent: true,
    });
    window.setTimeout(() => notification.close(), 8000);
  }

  function playAlertTone() {
    if (!state.soundEnabled || !state.audioContext) return;
    const context = state.audioContext;
    if (context.state === "suspended") context.resume().catch(() => undefined);
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(620, context.currentTime);
    gain.gain.setValueAtTime(0.0001, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.07, context.currentTime + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.16);
    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.start(context.currentTime);
    oscillator.stop(context.currentTime + 0.18);
  }

  function processBackendAlerts(snapshot) {
    // The on-page alert list remains available without opt-in. Do not consume
    // a delivery key until at least one external delivery channel is enabled.
    if (!state.notificationEnabled && !state.soundEnabled) return;
    const now = Date.now();
    if (now - state.lastAlertAt < ALERT_COOLDOWN_MS) return;

    const pending = state.alerts.find((alert) => (
      alertCanDeliver(snapshot, alert) && !wasDeliveredRecently(alert, now)
    ));
    if (!pending) return;
    const key = alertKey(pending);
    state.deliveredAlertKeys[key] = now;
    state.lastAlertAt = now;
    storeText(STORAGE_KEYS.lastAlertAt, String(now));
    pruneDeliveredKeys(now);
    showSystemNotification(pending);
    playAlertTone();
  }

  function updateUnreadCount(snapshot) {
    const unread = state.alerts.filter((alert) => (
      !state.readAlertKeys.has(alertReadKey(alert, snapshot))
    )).length;
    byId("unread-count").textContent = `${unread} 条未读`;
  }

  function markAllAlertsRead() {
    if (!state.lastSnapshot) return;
    state.alerts.forEach((alert) => (
      state.readAlertKeys.add(alertReadKey(alert, state.lastSnapshot))
    ));
    const keys = [...state.readAlertKeys].slice(-MAX_STORED_ALERT_KEYS);
    state.readAlertKeys = new Set(keys);
    storeJson(STORAGE_KEYS.readAlerts, keys);
    if (state.lastSnapshot) renderAlerts(state.lastSnapshot);
  }

  async function handleNotificationOptIn() {
    if (!("Notification" in window)) return;
    if (state.notificationEnabled) {
      state.notificationEnabled = false;
      storeText(STORAGE_KEYS.notificationEnabled, "false");
      renderControls();
      return;
    }

    let permission = Notification.permission;
    if (permission !== "granted") {
      permission = await Notification.requestPermission();
    }
    state.notificationEnabled = permission === "granted";
    storeText(STORAGE_KEYS.notificationEnabled, String(state.notificationEnabled));
    renderControls();
  }

  function ensureAudioContext() {
    if (state.audioContext) return;
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (AudioContext) state.audioContext = new AudioContext();
  }

  function handleSoundToggle() {
    state.soundEnabled = !state.soundEnabled;
    if (state.soundEnabled) ensureAudioContext();
    storeText(STORAGE_KEYS.soundEnabled, String(state.soundEnabled));
    renderControls();
  }

  function handleMuteToggle() {
    state.muted = !state.muted;
    storeText(STORAGE_KEYS.muted, String(state.muted));
    renderControls();
    if (state.lastSnapshot) updateDataRisk(state.lastSnapshot);
  }

  function renderControls() {
    const notificationButton = byId("notification-button");
    if (!("Notification" in window)) {
      notificationButton.textContent = "系统通知不支持";
      notificationButton.disabled = true;
    } else if (Notification.permission === "denied") {
      notificationButton.textContent = "系统通知被浏览器阻止";
      notificationButton.disabled = true;
    } else {
      notificationButton.disabled = false;
      notificationButton.textContent = state.notificationEnabled ? "系统通知：开" : "开启系统通知";
      notificationButton.setAttribute("aria-pressed", String(state.notificationEnabled));
    }

    const soundButton = byId("sound-button");
    soundButton.textContent = `声音：${state.soundEnabled ? "开" : "关"}`;
    soundButton.setAttribute("aria-pressed", String(state.soundEnabled));

    const muteButton = byId("mute-button");
    muteButton.textContent = state.muted ? "提醒：静音" : "提醒：正常";
    muteButton.setAttribute("aria-pressed", String(state.muted));
  }

  function objectValue(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function shanghaiClock(now = new Date()) {
    const parts = Object.fromEntries(
      new Intl.DateTimeFormat("en-CA", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
        hourCycle: "h23",
        timeZone: "Asia/Shanghai",
      }).formatToParts(now).filter((part) => part.type !== "literal")
        .map((part) => [part.type, part.value]),
    );
    return {
      date: `${parts.year}-${parts.month}-${parts.day}`,
      minutes: Number(parts.hour) * 60 + Number(parts.minute),
    };
  }

  function reviewScheduleMinutes(value, fallback) {
    const match = /^(\d{1,2}):(\d{2})$/.exec(text(value, fallback));
    if (!match) return reviewScheduleMinutes(fallback, "20:30");
    const hours = Number(match[1]);
    const minutes = Number(match[2]);
    return hours >= 0 && hours <= 23 && minutes >= 0 && minutes <= 59
      ? hours * 60 + minutes
      : reviewScheduleMinutes(fallback, "20:30");
  }

  function updateReviewSchedule() {
    const schedule = objectValue(state.reviewSchedule);
    const manualAfter = text(schedule.manual_after, "20:30");
    const automaticAfter = text(schedule.automatic_if_missing_after, "21:00");
    const clock = shanghaiClock();
    const currentReview = objectValue(objectValue(state.reviewHistory).review);
    const todayExists = text(currentReview.trade_date, "") === clock.date
      || asArray(objectValue(state.reviewHistory).dates).some((item) => (
        text(objectValue(item).trade_date, text(item, "")) === clock.date
      ));
    const due = clock.minutes >= reviewScheduleMinutes(manualAfter, "20:30");
    const button = byId("daily-review-generate-button");
    button.disabled = state.reviewGenerateInFlight || !due || todayExists;
    if (state.reviewGenerateInFlight) {
      button.textContent = "正在生成…";
    } else if (todayExists) {
      button.textContent = "今日已存档";
    } else if (!due) {
      button.textContent = `${manualAfter} 后生成`;
    } else {
      button.textContent = "生成今日复盘";
    }
    byId("daily-review-schedule").textContent = (
      `上海时间 ${manualAfter} 后可手动生成；`
      + `当日缺失时 ${automaticAfter} 由后台自动生成。`
    );
  }

  function validateReviewHistory(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("返回内容不是日复盘档案");
    }
    if (payload.contract !== "post_market_review_archive.v1" || payload.schema_version !== 1) {
      throw new Error(
        `复盘档案契约不匹配：${text(payload.contract, "缺少契约")}`,
      );
    }
    return payload;
  }

  function validateReviewResult(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("返回内容不是生成结果");
    }
    if (payload.contract !== "post_market_review_result.v1" || payload.schema_version !== 1) {
      throw new Error(
        `复盘生成契约不匹配：${text(payload.contract, "缺少契约")}`,
      );
    }
    return payload;
  }

  function renderReviewDateOptions(history) {
    const select = byId("daily-review-date-select");
    const dates = asArray(history.dates)
      .map((item) => text(objectValue(item).trade_date, text(item, "")))
      .filter(Boolean);
    const selected = text(history.trade_date, state.reviewTradeDate || dates[0] || "");
    select.replaceChildren();
    if (!dates.length) {
      select.append(createElement("option", "", "尚无存档"));
      select.disabled = true;
      state.reviewTradeDate = null;
      return;
    }
    dates.forEach((tradeDate) => {
      const option = createElement("option", "", tradeDate);
      option.value = tradeDate;
      option.selected = tradeDate === selected;
      select.append(option);
    });
    select.disabled = false;
    state.reviewTradeDate = dates.includes(selected) ? selected : dates[0];
  }

  function replaceTextList(targetId, values, fallback) {
    const target = byId(targetId);
    const items = stringList(values);
    target.replaceChildren();
    if (!items.length) {
      target.append(createElement("li", "muted-item", fallback));
      return;
    }
    items.slice(0, 8).forEach((item) => target.append(createElement("li", "", item)));
  }

  function reviewEvidenceObject(review) {
    return objectValue(
      review.evidence
      || review.review_evidence
      || review.daily_evidence
      || review.data_coverage,
    );
  }

  function coverageCandidates(review, aliases) {
    const evidence = reviewEvidenceObject(review);
    const components = asArray(evidence.components || review.evidence_components);
    const matches = components.filter((item) => aliases.includes(text(objectValue(item).component, "")));
    if (matches.length) return matches;
    return aliases
      .map((alias) => objectValue(evidence[alias]))
      .filter((item) => Object.keys(item).length);
  }

  function renderReviewCoverageItem(targetId, review, aliases) {
    const target = byId(targetId);
    const items = coverageCandidates(review, aliases);
    target.classList.remove("coverage-accepted", "coverage-degraded", "coverage-unavailable");
    if (!items.length) {
      target.textContent = "未随档案返回";
      target.classList.add("coverage-unavailable");
      return;
    }
    const statuses = items.map((item) => text(item.status || item.quality, "accepted").toLowerCase());
    const counts = items.map((item) => finiteNumber(
      item.record_count ?? item.scanned_count ?? item.sample_count,
    ));
    const knownCount = counts.filter((item) => item !== null).reduce((sum, item) => sum + item, 0);
    const hasCount = counts.some((item) => item !== null);
    const providers = [...new Set(
      items.map((item) => text(item.provider, "")).filter(Boolean),
    )];
    const accepted = statuses.every((item) => ["accepted", "ready", "fresh"].includes(item));
    const unavailable = statuses.every((item) => ["unavailable", "rejected", "abstained"].includes(item));
    const statusLabel = accepted ? "已覆盖" : unavailable ? "不可用" : "部分覆盖";
    const providerLabel = providers.length ? ` · ${providers.join(" / ")}` : "";
    target.textContent = hasCount
      ? `${statusLabel} · ${formatCount(knownCount)} 条${providerLabel}`
      : `${statusLabel}${providerLabel}`;
    target.classList.add(
      accepted ? "coverage-accepted" : unavailable ? "coverage-unavailable" : "coverage-degraded",
    );
  }

  function renderReviewCoverage(review) {
    renderReviewCoverageItem(
      "daily-review-coverage-universe",
      review,
      ["market_universe", "universe", "a_share_universe"],
    );
    renderReviewCoverageItem("daily-review-coverage-etfs", review, ["etfs", "etf"]);
    renderReviewCoverageItem(
      "daily-review-coverage-sectors",
      review,
      ["industry_sectors", "concept_sectors", "sectors"],
    );
    renderReviewCoverageItem(
      "daily-review-coverage-money-flow",
      review,
      [
        "stock_fund_flow",
        "money_flow",
        "stock_money_flow",
        "sector_money_flow",
        "fund_flow",
      ],
    );
    renderReviewCoverageItem(
      "daily-review-coverage-dragon-tiger",
      review,
      ["dragon_tiger", "dragon_tiger_list", "top_list"],
    );
    renderReviewCoverageItem(
      "daily-review-coverage-limit-events",
      review,
      ["limit_events", "limit_pool", "limit_ladder"],
    );
    renderReviewCoverageItem(
      "daily-review-coverage-intraday",
      review,
      ["intraday_history", "intraday", "session_trajectory"],
    );
  }

  function renderReviewLearning(learning, outcome) {
    const data = objectValue(learning);
    const count = finiteNumber(data.evaluated_count);
    const ratio = finiteNumber(data.support_ratio);
    byId("daily-review-learning").textContent = count === null
      ? text(data.calibration_note, "尚无可评估历史。")
      : `${formatCount(count)} 份已回看`
        + (ratio === null ? " · 暂无可验证支持率" : ` · 加权支持率 ${(ratio * 100).toFixed(1)}%`)
        + ` · ${text(data.calibration_note, "暂无额外校准提示")}`;
    const actual = objectValue(outcome);
    const verdictLabels = {
      supported: "已支持",
      partial: "部分支持",
      not_supported: "未支持",
      unverifiable: "不可验证",
    };
    byId("daily-review-outcome").textContent = actual.verdict
      ? `${verdictLabels[actual.verdict] || text(actual.verdict)} · ${text(actual.summary, "无摘要")}`
      : "尚未回看";
  }

  function reviewReportText(value, fallback = "") {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return text(value.text ?? value.value, fallback);
    }
    return text(value, fallback);
  }

  function reviewReportToneClass(value) {
    const tone = text(value, "neutral").trim().toLowerCase();
    if (["positive", "up", "rise", "rising", "gain", "red"].includes(tone)) {
      return "tone-positive";
    }
    if (["negative", "down", "fall", "falling", "loss", "green"].includes(tone)) {
      return "tone-negative";
    }
    if (["warning", "caution", "risk"].includes(tone)) return "daily-review-tone-warning";
    if (["muted", "missing", "unavailable"].includes(tone)) return "daily-review-tone-muted";
    if (["accent", "highlight", "focus"].includes(tone)) return "daily-review-tone-accent";
    return "tone-flat";
  }

  function reviewSectionAnchor(section, index) {
    const raw = text(section.section_id || section.id || section.key, "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "");
    return `daily-review-report-section-${raw || index + 1}`;
  }

  function reviewTableColumns(table) {
    return asArray(table.columns || table.headers).map((item, index) => {
      const column = objectValue(item);
      return {
        key: text(column.key || column.id || column.field, String(index)),
        label: reviewReportText(
          Object.keys(column).length ? column.label || column.text || column.title : item,
          `列 ${index + 1}`,
        ),
      };
    });
  }

  function reviewTableCells(row, columns) {
    if (Array.isArray(row)) return row;
    const record = objectValue(row);
    const cells = asArray(record.cells || record.values);
    if (cells.length) return cells;
    return columns.map((column) => record[column.key]);
  }

  function renderReviewReportTable(value, sectionTitle, tableIndex) {
    const tableRecord = objectValue(value);
    const columns = reviewTableColumns(tableRecord);
    const rows = asArray(tableRecord.rows);
    const wrapper = createElement("div", "daily-review-table-scroll");
    wrapper.tabIndex = 0;
    wrapper.setAttribute(
      "aria-label",
      text(tableRecord.title || tableRecord.caption, `${sectionTitle}表格 ${tableIndex + 1}`),
    );
    const table = createElement("table", "daily-review-report-table");
    const captionText = text(tableRecord.title || tableRecord.caption, "");
    if (captionText) table.append(createElement("caption", "", captionText));
    if (columns.length) {
      const head = createElement("thead");
      const row = createElement("tr");
      columns.forEach((column) => {
        const cell = createElement("th", "", column.label);
        cell.scope = "col";
        row.append(cell);
      });
      head.append(row);
      table.append(head);
    }
    const body = createElement("tbody");
    if (!rows.length) {
      const row = createElement("tr");
      const cell = createElement(
        "td",
        "daily-review-table-empty",
        text(tableRecord.empty_state, "表格数据未随档案返回。"),
      );
      cell.colSpan = Math.max(columns.length, 1);
      row.append(cell);
      body.append(row);
    } else {
      rows.forEach((rowValue) => {
        const row = createElement("tr");
        const cells = reviewTableCells(rowValue, columns);
        const visibleCells = cells.length ? cells : [rowValue];
        visibleCells.forEach((cellValue) => {
          const cellRecord = objectValue(cellValue);
          row.append(createElement(
            "td",
            reviewReportToneClass(cellRecord.tone),
            reviewReportText(cellValue, "未披露"),
          ));
        });
        body.append(row);
      });
    }
    table.append(body);
    wrapper.append(table);
    return wrapper;
  }

  function renderReviewArticleSections(sections) {
    const target = byId("daily-review-report-sections");
    target.replaceChildren();
    const records = asArray(sections).map(objectValue).filter((item) => Object.keys(item).length);
    if (!records.length) {
      target.append(createElement("p", "empty-state", "文章正文未随档案返回。"));
      return;
    }
    records.forEach((section, index) => {
      const titleValue = text(section.title || section.heading, `第 ${index + 1} 节`);
      const article = createElement("section", "daily-review-article-section");
      article.id = reviewSectionAnchor(section, index);
      article.append(createElement("h4", "", titleValue));
      const paragraphs = asArray(section.paragraphs)
        .map((paragraph) => reviewReportText(paragraph, ""))
        .filter(Boolean);
      const legacyLead = reviewReportText(section.lead ?? section.summary, "");
      if (!paragraphs.length && legacyLead) paragraphs.push(legacyLead);
      paragraphs.forEach((paragraph) => {
        article.append(createElement("p", "", paragraph));
      });
      target.append(article);
    });
  }

  function renderReviewWatchItems(items) {
    const target = byId("daily-review-watch-list");
    target.replaceChildren();
    const records = asArray(items).map(objectValue).filter((item) => Object.keys(item).length);
    if (!records.length) {
      target.append(createElement("li", "", "观察项未随档案返回。"));
      return;
    }
    records.forEach((item, index) => {
      const entry = createElement("li", "daily-review-watch-item");
      const heading = createElement("h5", "", text(item.title, `观察项 ${index + 1}`));
      entry.append(heading);
      const why = reviewReportText(item.why_it_matters, "");
      if (why) entry.append(createElement("p", "", why));
      const conditions = createElement("dl");
      conditions.append(
        createElement("dt", "", "看到什么算确认"),
        createElement("dd", "", reviewReportText(item.confirmation, "未披露")),
        createElement("dt", "", "什么情况算失效"),
        createElement("dd", "", reviewReportText(item.invalidation, "未披露")),
      );
      entry.append(conditions);
      target.append(entry);
    });
  }

  function renderReviewAppendixSections(sections) {
    const target = byId("daily-review-appendix-sections");
    target.replaceChildren();
    const records = asArray(sections).map(objectValue).filter((item) => Object.keys(item).length);
    if (!records.length) {
      target.append(createElement("p", "empty-state", "详细数据底稿未随档案返回。"));
      return;
    }
    records.forEach((section, index) => {
      const titleValue = text(section.title || section.heading, `底稿 ${index + 1}`);
      const article = createElement("section", "daily-review-report-section");
      const heading = createElement("header", "daily-review-report-section__header");
      heading.append(
        createElement("span", "daily-review-report-section__number", String(index + 1).padStart(2, "0")),
        createElement("h4", "", titleValue),
      );
      article.append(heading);
      const summary = reviewReportText(section.summary, "");
      if (summary) article.append(createElement("p", "daily-review-report-lead", summary));
      asArray(section.tables).forEach((table, tableIndex) => {
        article.append(renderReviewReportTable(table, titleValue, tableIndex));
      });
      const notes = asArray(section.notes)
        .map((note) => reviewReportText(note, ""))
        .filter(Boolean);
      if (notes.length) {
        const noteBlock = createElement("aside", "daily-review-report-notes");
        noteBlock.append(createElement("h5", "", "说明与边界"));
        const list = createElement("ul");
        notes.forEach((note) => list.append(createElement("li", "", note)));
        noteBlock.append(list);
        article.append(noteBlock);
      }
      target.append(article);
    });
  }

  function renderPostMarketReview(review, outcome = null, archiveLearning = null) {
    const canonical = objectValue(review);
    const recap = objectValue(canonical.recap);
    const qualityLabels = { ready: "完备", degraded: "降级", abstained: "主动弃权" };
    const triggerLabels = { manual: "手动", automatic: "21:00 自动" };
    byId("daily-review-empty").hidden = true;
    byId("daily-review-content").hidden = false;
    byId("daily-review-trade-date").textContent = text(canonical.trade_date);
    byId("daily-review-quality").textContent = qualityLabels[canonical.quality] || text(canonical.quality);
    byId("daily-review-trigger").textContent = triggerLabels[canonical.trigger] || text(canonical.trigger);
    byId("daily-review-as-of").textContent = formatTimestamp(
      canonical.source_snapshot_as_of || canonical.generated_at,
      true,
    );
    byId("daily-review-report-title").textContent = text(
      canonical.title,
      text(recap.headline, "盘后结构化复盘"),
    );
    byId("daily-review-report-deck").textContent = text(
      canonical.standfirst || canonical.deck,
      text(canonical.core_conclusion || recap.summary, "报告正文未随档案返回。"),
    );
    byId("daily-review-report-contract").textContent = text(
      canonical.presentation_contract || canonical.contract,
      "post_market_review_presentation.v4",
    );
    renderReviewArticleSections(canonical.sections);
    renderReviewWatchItems(canonical.watch_items);
    renderReviewAppendixSections(canonical.appendix_sections);
    renderReviewCoverage(canonical);
    replaceTextList("daily-review-limitations", canonical.limitations, "未随档案返回。");
    renderReviewLearning(canonical.learning || archiveLearning, outcome);
  }

  function reviewWithPresentation(review, presentation) {
    const canonical = objectValue(review);
    const current = objectValue(presentation);
    if (
      !Object.keys(current).length
      || text(current.review_id, "") !== text(canonical.review_id, "")
    ) return canonical;
    return {
      ...canonical,
      review_contract: canonical.contract,
      contract: current.contract || canonical.contract,
      schema_version: current.schema_version ?? canonical.schema_version,
      presentation_contract: current.contract || canonical.presentation_contract,
      title: current.title || canonical.title,
      standfirst: current.standfirst || current.deck || canonical.standfirst || canonical.deck,
      sections: current.sections || canonical.sections,
      watch_items: current.watch_items || canonical.watch_items,
      appendix_sections: current.appendix_sections || current.sections || canonical.appendix_sections,
      day_character: current.day_character || canonical.day_character,
      core_conclusion: current.core_conclusion || canonical.core_conclusion,
      session_story: current.session_story || canonical.session_story,
      comparison_statement: current.comparison_statement || canonical.comparison_statement,
      themes: current.themes || canonical.themes,
      money_making_effect: current.money_making_effect || canonical.money_making_effect,
      loss_making_effect: current.loss_making_effect || canonical.loss_making_effect,
      next_day_scenarios: current.next_day_scenarios || canonical.next_day_scenarios,
      recap: current.recap || canonical.recap,
      next_day_outlook: current.next_day_outlook || canonical.next_day_outlook,
      opportunity_sectors: current.opportunity_sectors || canonical.opportunity_sectors,
      limitations: current.limitations || canonical.limitations,
      learning: current.learning || canonical.learning,
    };
  }

  function renderPostMarketReviewHistory(history) {
    state.reviewHistory = history;
    state.reviewSchedule = objectValue(history.schedule);
    renderReviewDateOptions(history);
    const review = objectValue(history.review);
    if (!Object.keys(review).length) {
      byId("daily-review-content").hidden = true;
      byId("daily-review-empty").hidden = false;
      byId("daily-review-status").textContent = "尚无存档";
    } else {
      renderPostMarketReview(
        reviewWithPresentation(review, history.presentation),
        history.outcome,
        history.learning,
      );
      byId("daily-review-status").textContent = `${text(review.trade_date)} · 已存档`;
    }
    updateReviewSchedule();
  }

  async function fetchPostMarketReviewHistory({ tradeDate = null, silent = false } = {}) {
    state.reviewHasLoaded = true;
    if (state.reviewFetchInFlight) return;
    state.reviewFetchInFlight = true;
    if (!silent) byId("daily-review-status").textContent = "正在读取存档…";
    try {
      const query = tradeDate ? `?trade_date=${encodeURIComponent(tradeDate)}` : "";
      const response = await fetch(`${REVIEW_HISTORY_ENDPOINT}${query}`, {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(text(payload.error, `服务返回 ${response.status}`));
      renderPostMarketReviewHistory(validateReviewHistory(payload));
    } catch (error) {
      byId("daily-review-status").textContent = `档案暂不可用：${text(error.message, "等待重试")}`;
    } finally {
      state.reviewFetchInFlight = false;
      updateReviewSchedule();
    }
  }

  async function generatePostMarketReview() {
    if (state.reviewGenerateInFlight) return;
    state.reviewGenerateInFlight = true;
    updateReviewSchedule();
    byId("daily-review-status").textContent = "正在生成并存档…";
    try {
      const response = await fetch(REVIEW_GENERATE_ENDPOINT, {
        method: "POST",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(text(payload.error, `服务返回 ${response.status}`));
      const result = validateReviewResult(payload);
      renderPostMarketReview(reviewWithPresentation(result.review, result.presentation));
      byId("daily-review-status").textContent = result.action === "existing"
        ? "今日复盘已存在，已读取存档"
        : "今日复盘已生成并存档";
      await fetchPostMarketReviewHistory({ tradeDate: text(result.review && result.review.trade_date, null), silent: true });
    } catch (error) {
      byId("daily-review-status").textContent = text(error.message, "日复盘生成失败");
    } finally {
      state.reviewGenerateInFlight = false;
      updateReviewSchedule();
    }
  }

  function validateSnapshot(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("返回内容不是盘面快照");
    }
    if (payload.contract !== "market_watch.v1" || payload.schema_version !== 1) {
      throw new Error(
        `接口契约不匹配：${text(payload.contract, "缺少契约")} / ${text(payload.schema_version, "缺少版本")}`,
      );
    }
    return payload;
  }

  async function fetchSnapshot({ force = false } = {}) {
    if (state.fetchInFlight) return;
    state.fetchInFlight = true;
    document.body.classList.add("is-fetching");
    byId("poll-status").textContent = "正在更新…";
    try {
      const response = await fetch(`${API_ENDPOINT}${force ? "?refresh=1" : ""}`, {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`服务返回 ${response.status}`);
      state.backgroundRefresh = (
        response.headers.get("X-Tradex-Refresh-State") === "background"
      );
      const snapshot = validateSnapshot(await response.json());
      state.fetchFailed = false;
      state.lastFetchError = null;
      state.lastSnapshot = snapshot;
      state.lastSuccessAt = Date.now();
      renderSnapshot(snapshot);
      processBackendAlerts(snapshot);
      const replayReference = Math.max(
        state.replayLastAttemptAt,
        state.replayLastFetchedAt,
      );
      if (
        state.replayHasLoaded
        && !state.replayFetchInFlight
        && Date.now() - replayReference >= REPLAY_REFRESH_INTERVAL_MS
      ) {
        fetchReplayData({ silent: true });
      }
    } catch (error) {
      state.backgroundRefresh = false;
      state.fetchFailed = true;
      state.lastFetchError = error;
      renderFetchRisk(error);
    } finally {
      state.fetchInFlight = false;
      state.nextPollAt = Date.now() + POLL_INTERVAL_MS;
      document.body.classList.remove("is-fetching");
      updatePollStatus();
    }
  }

  function updatePollStatus() {
    const target = byId("poll-status");
    if (state.fetchInFlight) {
      target.textContent = "正在更新…";
      return;
    }
    if (state.backgroundRefresh) {
      target.textContent = "已显示上次快照 · 后台更新中…";
      return;
    }
    if (!state.nextPollAt) {
      target.textContent = "等待首份盘面";
      return;
    }
    const seconds = Math.max(0, Math.ceil((state.nextPollAt - Date.now()) / 1000));
    target.textContent = `${seconds} 秒后自动刷新`;
  }

  function bindUserActions() {
    byId("refresh-button").addEventListener("click", () => fetchSnapshot({ force: true }));
    byId("notification-button").addEventListener("click", handleNotificationOptIn);
    byId("sound-button").addEventListener("click", handleSoundToggle);
    byId("mute-button").addEventListener("click", handleMuteToggle);
    byId("mark-read-button").addEventListener("click", markAllAlertsRead);
    document.querySelectorAll("[data-flow-mode]").forEach((button) => {
      button.addEventListener("click", () => {
        if (button.disabled) return;
        const scope = button.closest("[data-sector-flow-chart-card]")?.dataset.flowScope;
        if (!scope) return;
        state.sectorFlowMode[scope] = button.dataset.flowMode === "delta_5m"
          ? "delta_5m"
          : "cumulative";
        if (state.lastSnapshot) renderSectorFlowTrajectory(state.lastSnapshot, scope);
      });
    });
    document.querySelectorAll("[data-sector-flow-picker-options]").forEach((options) => {
      options.addEventListener("change", (event) => {
        const key = event.target?.dataset?.sectorFlowKey;
        if (!key || !(event.target instanceof HTMLInputElement)) return;
        const scope = options.dataset.flowScope;
        let selected = sectorFlowSelection(scope);
        if (selected === null) {
          selected = new Set();
          setSectorFlowSelection(scope, selected);
        }
        if (event.target.checked) selected.add(key);
        else selected.delete(key);
        storeJson(sectorFlowSelectionStorageKey(scope), [...selected]);
        if (state.lastSnapshot) renderSectorFlowTrajectory(state.lastSnapshot, scope);
      });
    });
    document.querySelectorAll("[data-sector-flow-action]").forEach((button) => {
      button.addEventListener("click", () => {
        const scope = button.dataset.flowScope;
        const payload = sectorFlowPayload(state.lastSnapshot, scope);
        if (!payload) return;
        const selected = button.dataset.sectorFlowAction === "select-all"
          ? new Set(payload.sectors.map((item) => text(item.sector_key, "")).filter(Boolean))
          : new Set(defaultSectorFlowSelection(payload));
        setSectorFlowSelection(scope, selected);
        storeJson(sectorFlowSelectionStorageKey(scope), [...selected]);
        renderSectorFlowTrajectory(state.lastSnapshot, scope);
      });
    });
    document.querySelectorAll("[data-sector-flow-surge-threshold]").forEach((select) => {
      select.addEventListener("change", (event) => {
        const threshold = finiteNumber(event.target.value);
        if (threshold === null || threshold <= 0) return;
        state.sectorFlowSurgeThreshold = threshold;
        storeText(STORAGE_KEYS.sectorFlowSurgeThreshold, String(threshold));
        if (state.lastSnapshot) renderSectorFlowTrajectories(state.lastSnapshot);
      });
    });
    document.querySelectorAll("[data-sector-flow-chart-card]").forEach((chartCard) => {
      const chartLayout = chartCard.closest(".sector-flow-layout");
      const syncChartLayout = () => {
        chartLayout.classList.toggle("is-chart-open", chartCard.open);
      };
      chartCard.addEventListener("toggle", syncChartLayout);
      syncChartLayout();
    });
    byId("replay-refresh-button").addEventListener("click", () => fetchReplayData({
      tradeDate: byId("replay-date-select").value || null,
    }));
    byId("replay-date-select").addEventListener("change", (event) => {
      state.replayTradeDate = event.target.value || null;
      fetchReplayData({ tradeDate: state.replayTradeDate });
    });
    byId("daily-review-generate-button").addEventListener("click", generatePostMarketReview);
    byId("daily-review-date-select").addEventListener("change", (event) => {
      state.reviewTradeDate = event.target.value || null;
      fetchPostMarketReviewHistory({ tradeDate: state.reviewTradeDate });
    });
    byId("evaluation-scope-select").addEventListener("change", (event) => {
      const parsed = Number(event.target.value);
      state.evaluationDays = Number.isInteger(parsed) && parsed > 0 ? parsed : null;
      fetchReplayData({ tradeDate: state.replayTradeDate });
    });
  }

  function loadPanelWhenVisible(elementId, loader) {
    const target = byId(elementId);
    if (!("IntersectionObserver" in window)) {
      window.setTimeout(loader, 0);
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      observer.disconnect();
      loader();
    }, { rootMargin: "240px 0px" });
    observer.observe(target);
  }

  function setupDeferredPanelLoading() {
    loadPanelWhenVisible("daily-review-section", () => {
      fetchPostMarketReviewHistory();
    });
    loadPanelWhenVisible("replay-section", () => {
      fetchReplayData();
    });
  }

  function start() {
    bindUserActions();
    renderControls();
    setupDeferredPanelLoading();
    fetchSnapshot();
    window.setInterval(fetchSnapshot, POLL_INTERVAL_MS);
    window.setInterval(updatePollStatus, 1000);
    window.setInterval(updateReviewSchedule, 30_000);
    window.setInterval(
      () => {
        if (state.reviewHasLoaded) {
          fetchPostMarketReviewHistory({ tradeDate: state.reviewTradeDate, silent: true });
        }
      },
      REVIEW_REFRESH_INTERVAL_MS,
    );
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
