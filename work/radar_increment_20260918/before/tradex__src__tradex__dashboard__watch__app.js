// Appearance is local presentation state; it never refreshes market data.
(() => {
  "use strict";
  const root = document.documentElement;
  const panel = document.getElementById("appearance-panel");
  if (!panel) return;
  const themeNames = { porcelain: "瓷白钴蓝", sand: "月砂赤陶", ink: "墨夜鸢尾", paper: "暖纸米白", mist: "雾蓝浅灰", sage: "鼠尾草绿", graphite: "柔和石墨", classic: "经典深青" };
  const layouts = ["comfortable", "compact", "wide"];
  const fonts = ["noto", "deng", "yahei"];
  const storageKey = "tradex.marketWatch.appearance.v1";
  function renderAppearance() {
    document.getElementById("appearance-current").textContent = themeNames[root.dataset.theme];
    panel.querySelectorAll("[data-appearance-theme]").forEach(button => {
      button.setAttribute("aria-pressed", String(button.dataset.appearanceTheme === root.dataset.theme));
    });
    panel.querySelectorAll("[data-appearance-layout]").forEach(button => {
      button.setAttribute("aria-pressed", String(button.dataset.appearanceLayout === root.dataset.layout));
    });
    panel.querySelectorAll("[data-appearance-font]").forEach(button => {
      button.setAttribute("aria-pressed", String(button.dataset.appearanceFont === root.dataset.font));
    });
  }
  panel.addEventListener("click", event => {
    const button = event.target.closest("[data-appearance-theme], [data-appearance-layout], [data-appearance-font]");
    if (!button || !panel.contains(button)) return;
    const { appearanceTheme: theme, appearanceLayout: layout, appearanceFont: font } = button.dataset;
    if (Object.hasOwn(themeNames, theme)) root.dataset.theme = theme;
    else if (layouts.includes(layout)) root.dataset.layout = layout;
    else if (fonts.includes(font)) root.dataset.font = font;
    else return;
    renderAppearance();
    const status = document.getElementById("appearance-status");
    try {
      localStorage.setItem(storageKey, JSON.stringify({ theme: root.dataset.theme, layout: root.dataset.layout, font: root.dataset.font }));
      status.textContent = "已应用并记住，下次打开仍使用这套外观。";
    } catch {
      status.textContent = "已应用；浏览器暂不允许保存，下次打开需重新选择。";
    }
  });
  panel.addEventListener("toggle", event => {
    document.getElementById("appearance-button").setAttribute("aria-expanded", String(event.newState === "open"));
  });
  renderAppearance();
})();

(() => {
  "use strict";

  const COLLECTION_STATUS_ENDPOINT = "/api/market-watch/collection-status";
  const DAILY_RECOVERY_ENDPOINT = "/api/market-watch/daily-recovery";
  const INTRADAY_TRAJECTORY_REPAIR_ENDPOINT = "/api/market-watch/intraday-trajectory-repair";
  const SUMMARY_ENDPOINT = "/api/market-watch/summary";
  const TRAJECTORY_ENDPOINT = "/api/market-watch/trajectory";
  const FIVE_DAY_TRAJECTORY_ENDPOINT = "/api/market-watch/five-day-trajectory";
  const LIMIT_UP_POOL_ENDPOINT = "/api/limit-up-pool";
  const LIMIT_UP_POOL_LATEST_ENDPOINT = "/api/limit-up-pool/latest";
  const HISTORY_ENDPOINT = "/api/market-watch/history";
  const EVALUATION_ENDPOINT = "/api/market-watch/evaluation";
  const REVIEW_HISTORY_ENDPOINT = "/api/post-market-review/history";
  const REVIEW_GENERATE_ENDPOINT = "/api/post-market-review";
  const REVIEW_GENERATION_ENDPOINT = "/api/post-market-review/generation";
  const STOCK_SELECTION_RESULTS_ENDPOINT = "/api/stock-selection/results";
  const STOCK_SELECTION_GENERATE_ENDPOINT = "/api/daily-stock-selection";
  const STOCK_SELECTION_GENERATION_ENDPOINT = "/api/daily-stock-selection/generation";
  const MANUAL_PORTFOLIO_ENDPOINT = "/api/manual-portfolio";
  const MANUAL_PORTFOLIO_MARKET_ENDPOINT = "/api/manual-portfolio/market";
  const MANUAL_PORTFOLIO_OUTLOOK_ENDPOINT = "/api/manual-portfolio/outlook";
  const MANUAL_PORTFOLIO_OUTLOOK_GENERATION_ENDPOINT = "/api/manual-portfolio/outlook/generation";
  const MANUAL_PORTFOLIO_INTRADAY_ANALYSIS_ENDPOINT = "/api/manual-portfolio/intraday-analysis";
  const MANUAL_PORTFOLIO_ANALYSIS_HISTORY_ENDPOINT = "/api/manual-portfolio/analysis-history";
  const MANUAL_PORTFOLIO_INTRADAY_HISTORY_ENDPOINT = "/api/manual-portfolio/intraday-analysis/history";
  const POLL_INTERVAL_MS = 15_000;
  const POLL_WATCHDOG_INTERVAL_MS = 1_000;
  const RESONANCE_REFRESH_INTERVAL_MS = 30_000;
  const REPLAY_REFRESH_INTERVAL_MS = 60_000;
  const REVIEW_REFRESH_INTERVAL_MS = 60_000;
  const STOCK_SELECTION_REFRESH_INTERVAL_MS = 60_000;
  const COLLECTOR_HEARTBEAT_STALE_MS = 90_000;
  const ALERT_COOLDOWN_MS = 5 * 60 * 1000;
  const SECTOR_MOVE_MAX_AGE_MS = 120_000;
  const MAX_STORED_ALERT_KEYS = 200;
  const MAX_SECTOR_FLOW_SERIES = 64;
  const MAX_SECTOR_FLOW_CHART_SERIES = 64;
  const MAX_SECTOR_FLOW_ENDPOINT_LABELS = 64;
  const SECTOR_FLOW_ENDPOINT_DENSITY_WEIGHT = 0.65;
  const SECTOR_FLOW_ENDPOINT_GAP_PX = 15;
  const SECTOR_FLOW_HIT_STROKE_PX = 10;
  const SECTOR_FLOW_LUNCH_GAP_MINUTES = 18;
  const SECTOR_FLOW_SESSION_SPAN_MINUTES = 263;
  const SECTOR_FLOW_DAY_GAP_MINUTES = 16;
  const SECTOR_FLOW_DAY_SPAN_MINUTES = SECTOR_FLOW_SESSION_SPAN_MINUTES
    + SECTOR_FLOW_DAY_GAP_MINUTES;
  const MAX_SECTOR_FLOW_SAMPLE_GAP_MINUTES = 5;
  const SECTOR_FLOW_COLOR_SLOT_COUNT = 72;
  const SECTOR_FLOW_HUE_ORDER = [
    0, 8, 4, 12, 2, 10, 6, 14,
    1, 9, 5, 13, 3, 11, 7, 15,
  ];
  const SECTOR_FLOW_COLOR_BANDS = [
    [94, 62],
    [72, 76],
    [100, 50],
    [62, 66],
    [88, 84],
  ];
  const DEFAULT_SECTOR_FLOW_SELECTIONS = {
    defense: [
      "electric_power", "agriculture", "precious_metals", "oil_gas",
      "ports", "baijiu", "retail", "bank", "diversified_finance",
    ],
    offense: [
      "semiconductor", "software_development", "communication_equipment",
      "automation_equipment", "consumer_electronics", "battery",
      "photovoltaic_equipment", "defense",
    ],
  };
  const DEFAULT_SECTOR_FLOW_SURGE_THRESHOLD = 0.5;
  const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
  const SHANGHAI_TIME_PARTS_FORMATTER = new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone: "Asia/Shanghai",
  });
  const STORAGE_KEYS = {
    deliveredAlerts: "tradex.marketWatch.deliveredAlerts.v1",
    lastAlertAt: "tradex.marketWatch.lastAlertAt.v1",
    muted: "tradex.marketWatch.muted.v1",
    notificationEnabled: "tradex.marketWatch.notificationEnabled.v1",
    soundEnabled: "tradex.marketWatch.soundEnabled.v1",
    sectorFlowSelection: "tradex.marketWatch.sectorFlowSelection.v1",
    offenseSectorFlowSelection: "tradex.marketWatch.offenseSectorFlowSelection.v2",
    sectorFlowColorAssignments: "tradex.marketWatch.sectorFlowColorAssignments.v1",
    sectorFlowSurgeThreshold: "tradex.marketWatch.sectorFlowSurgeThreshold.v1",
    manualPortfolioDeliveredAlerts: "tradex.manualPortfolio.deliveredAlerts.v1",
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

  const state = {
    alerts: [],
    audioContext: null,
    collectionStatus: null,
    collectionStatusEtag: null,
    recoveryRequestError: null,
    recoveryRequestInFlight: false,
    trajectoryRepair: null,
    trajectoryRepairError: null,
    trajectoryRepairRequestInFlight: false,
    detailEtags: new Map(),
    detailPayloads: new Map(),
    fiveDayTrajectory: {
      defense: { cacheKey: null, etag: null, error: null, payload: null },
      offense: { cacheKey: null, etag: null, error: null, payload: null },
    },
    deliveredAlertKeys: loadStoredObject(STORAGE_KEYS.deliveredAlerts),
    evaluationDays: null,
    fetchFailed: false,
    fetchInFlight: false,
    lastFetchError: null,
    lastAlertAt: Number(readStoredText(STORAGE_KEYS.lastAlertAt, "0")) || 0,
    lastSnapshot: null,
    lastSummary: null,
    lastSuccessAt: 0,
    limitUpPool: null,
    limitUpPoolCategory: "all",
    limitUpPoolFetchInFlight: false,
    limitUpPoolRetryTimer: null,
    limitUpPoolRevision: null,
    muted: readStoredText(STORAGE_KEYS.muted) === "true",
    manualPortfolio: null,
    manualPortfolioMarket: null,
    manualPortfolioFetchInFlight: false,
    manualPortfolioIntradayAnalysis: null,
    manualPortfolioAnalysisHistory: null,
    manualPortfolioAnalysisInstrumentId: null,
    manualPortfolioAnalysisTab: "outlook",
    manualPortfolioAnalysisPage: { outlook: 0, review: 0 },
    manualPortfolioOutlookGeneration: null,
    manualPortfolioMutationInFlight: false,
    manualPortfolioOutlook: null,
    manualPortfolioOutlookPollTimer: null,
    manualPortfolioDeliveredAlerts: new Set(
      loadStoredArray(STORAGE_KEYS.manualPortfolioDeliveredAlerts),
    ),
    nextPollAt: 0,
    notificationEnabled: false,
    replayFetchInFlight: false,
    replayHasLoaded: false,
    replayLastAttemptAt: 0,
    replayLastFetchedAt: 0,
    replayPendingRequest: null,
    replayTradeDate: null,
    reviewFetchInFlight: false,
    reviewGenerateInFlight: false,
    reviewGenerationPhase: "idle",
    reviewGenerationPollInFlight: false,
    reviewHasLoaded: false,
    reviewHistory: null,
    reviewHighlightTerms: new Map(),
    reviewSchedule: {
      manual_after: "17:30",
      automatic_if_missing_after: "21:00",
      timezone: "Asia/Shanghai",
    },
    reviewTradeDate: null,
    stockSelectionFetchInFlight: false,
    stockSelectionRequestId: 0,
    stockSelectionGenerateInFlight: false,
    stockSelectionGenerationPhase: "idle",
    stockSelectionGenerationPollInFlight: false,
    stockSelectionHasLoaded: false,
    stockSelectionHistory: null,
    stockSelectionStrategyId: null,
    stockSelectionIndustry: null,
    stockSelectionIndustryScope: null,
    stockSelectionChangeFilter: "all",
    stockSelectionInsightScope: null,
    stockSelectionComparison: null,
    stockSelectionSchedule: {
      manual_after: "18:00",
      automatic_if_missing_after: "18:30",
      timezone: "Asia/Shanghai",
    },
    stockSelectionTradeDate: null,
    sectorFlowMode: {
      defense: "cumulative",
      offense: "cumulative",
    },
    summaryEtag: null,
    lastSummaryCheckedAt: 0,
    trajectoryDetails: {
      defense: new Map(),
      offense: new Map(),
    },
    sectorFlowSelection: readStoredText(STORAGE_KEYS.sectorFlowSelection) === null
      ? null
      : new Set(loadStoredArray(STORAGE_KEYS.sectorFlowSelection)),
    offenseSectorFlowSelection: readStoredText(STORAGE_KEYS.offenseSectorFlowSelection) === null
      ? null
      : new Set(loadStoredArray(STORAGE_KEYS.offenseSectorFlowSelection)),
    sectorFlowColorAssignments: loadStoredObject(STORAGE_KEYS.sectorFlowColorAssignments),
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
    const dock = document.querySelector("[data-turnover-dock]");
    if (!dock) return;

    byId("turnover-today").textContent = available ? formatCny(comparison.today_amount_cny) : "--";
    byId("turnover-previous").textContent = available ? formatCny(comparison.previous_same_time_amount_cny) : "--";
    const ratioNode = byId("turnover-ratio");
    const differenceNode = byId("turnover-difference");
    const badgeDirection = available ? comparison.direction : "unknown";
    ratioNode.textContent = available
      ? `${stale ? "延迟 · " : ""}${directionLabels[badgeDirection]} ${formatRatio(comparison.difference_ratio)}`
      : "不可用";
    differenceNode.textContent = available ? `差 ${formatCny(comparison.difference_cny, true)}` : "差 --";
    setTone(ratioNode, available ? ratio : null);
    setTone(differenceNode, available ? comparison.difference_cny : null);
    dock.classList.toggle("is-unavailable", !available);
    const definition = neutralBand === null
      ? "仅比较昨日同一交易分钟，不与昨日全天成交额混比。"
      : `仅比较昨日同一交易分钟；±${neutralBand.toFixed(1)}% 以内按持平处理。`;
    const unavailableReason = typeof turnover.reason === "string" && turnover.reason.trim()
      ? turnover.reason.trim()
      : comparison === null && turnover.available === true
        ? "成交额数据字段不完整，暂时无法显示。"
        : "成交额数据暂不可用。";
    const detail = !available
      ? unavailableReason
      : stale
        ? `数据延迟；截至 ${comparison.as_of}。${definition}`
        : definition;
    const accessibleSummary = available
      ? `全 A 成交额今日累计 ${formatCny(comparison.today_amount_cny)}，昨日同期 ${formatCny(comparison.previous_same_time_amount_cny)}，${directionLabels[badgeDirection]} ${formatRatio(comparison.difference_ratio)}，金额差 ${formatCny(comparison.difference_cny, true)}。${detail}`
      : `全 A 成交额与昨日同期：${detail}`;
    dock.setAttribute("aria-label", accessibleSummary);
    dock.setAttribute("title", accessibleSummary);
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

  function sectorFlowEndpointRank(value, endpointValues) {
    if (endpointValues.length < 2) return 0.5;
    if (value <= endpointValues[0]) return 0;
    const finalIndex = endpointValues.length - 1;
    if (value >= endpointValues[finalIndex]) return 1;
    let lowerIndex = 0;
    let upperIndex = finalIndex;
    while (upperIndex - lowerIndex > 1) {
      const middleIndex = Math.floor((lowerIndex + upperIndex) / 2);
      if (endpointValues[middleIndex] <= value) lowerIndex = middleIndex;
      else upperIndex = middleIndex;
    }
    const lowerValue = endpointValues[lowerIndex];
    const upperValue = endpointValues[upperIndex];
    const ratio = upperValue === lowerValue ? 0 : (value - lowerValue) / (upperValue - lowerValue);
    return (lowerIndex + ratio) / finalIndex;
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
    if (scope === "catalog") return byId(`sector-catalog-${name}`);
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

  function hasFullSectorResonance(item) {
    const snapshot = item?.leader_snapshot;
    return snapshot?.selection_method === "sector_fund_flow_path_resonance.v2"
      && snapshot.status === "full"
      && snapshot.resonance_direction === "up"
      && asArray(snapshot.leaders).length > 0;
  }

  function sectorFlowDisplayPayload(payload) {
    const selected = ensureSectorFlowSelection(payload);
    const sectors = payload.sectors.flatMap((item) => {
      const key = text(item.sector_key, "");
      const isSelected = selected.has(key);
      const changeDelta = finiteNumber(item?.latest?.change_delta_5m_pct);
      const triggered = changeDelta !== null
        && Math.abs(changeDelta) >= state.sectorFlowSurgeThreshold;
      const resonance = hasFullSectorResonance(item);
      const automatic = (triggered || resonance) && !isSelected;
      if (!isSelected && !automatic) return [];
      return [{
        ...item,
        _display: {
          automatic,
          changeDelta,
          direction: changeDelta > 0 ? "strengthening" : "weakening",
          resonance,
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

  function sectorFlowFiveDayPayload(history, displayPayload) {
    if (!history) return null;
    const days = asArray(history.days);
    const sectors = displayPayload.sectors.map((current) => {
      const points = [];
      let historicalMetadata = null;
      let cumulativeOffset = 0;
      days.forEach((day, dayIndex) => {
        const series = asArray(day.sectors).find((item) => (
          item?.sector_key === current.sector_key
        ));
        if (!series) return;
        historicalMetadata = series;
        let finalDailyCumulative = null;
        asArray(series.points).forEach((point) => {
          const dailyCumulative = finiteNumber(point.cumulative_cny);
          if (dailyCumulative === null) return;
          points.push({
            ...point,
            _historyDayIndex: dayIndex,
            _historyTradeDate: day.trade_date,
            _historyContinuousCny: cumulativeOffset + dailyCumulative,
          });
          finalDailyCumulative = dailyCumulative;
        });
        if (finalDailyCumulative !== null) cumulativeOffset += finalDailyCumulative;
      });
      return {
        ...(historicalMetadata || {}),
        ...current,
        points,
      };
    });
    return {
      ...displayPayload,
      asOf: history.as_of,
      flags: stringList(history.flags),
      historyAvailableDays: Number(history.available_trade_days) || 0,
      historyRequestedDays: Number(history.requested_trade_days) || 5,
      historyTradeDates: asArray(history.trade_dates),
      sectors,
      status: text(history.status, "unavailable"),
      tradeDate: text(asArray(history.trade_dates).at(-1), displayPayload.tradeDate),
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
    updateSectorFlowPickerSummary(payload, displayPayload, scope);
  }

  function updateSectorFlowPickerSummary(payload, displayPayload, scope) {
    const selected = ensureSectorFlowSelection(payload);
    const selectedAvailable = payload.sectors.filter((item) => (
      selected.has(text(item.sector_key, ""))
    )).length;
    sectorFlowElement(scope, "selection-count").textContent = `${selectedAvailable} / ${payload.sectors.length}`;
    sectorFlowElement(scope, "auto-count").textContent = `突变越权 ${displayPayload.overrideCount}`;
    sectorFlowElement(scope, "surge-threshold").value = String(state.sectorFlowSurgeThreshold);
    const keys = payload.sectors.map((item) => text(item.sector_key, "")).filter(Boolean);
    const allSelected = keys.length > 0 && keys.every((key) => selected.has(key));
    const button = document.querySelector(
      `[data-sector-flow-action="select-all"][data-flow-scope="${scope}"]`,
    );
    button.textContent = allSelected ? "取消全选" : "全选";
    button.setAttribute("aria-pressed", String(allSelected));
  }

  function sectorFlowPointValue(point, mode) {
    if (mode === "five_day") return finiteNumber(point._historyContinuousCny);
    return finiteNumber(mode === "delta_5m" ? point.delta_5m_cny : point.cumulative_cny);
  }

  function shanghaiMinuteOfDay(value) {
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return null;
    const parts = SHANGHAI_TIME_PARTS_FORMATTER.formatToParts(parsed);
    const hour = Number(parts.find((part) => part.type === "hour")?.value);
    const minute = Number(parts.find((part) => part.type === "minute")?.value);
    return Number.isFinite(hour) && Number.isFinite(minute) ? hour * 60 + minute : null;
  }

  function sectorFlowTradingMinute(value, segment) {
    const minute = shanghaiMinuteOfDay(value);
    if (minute === null) return null;
    if (segment === "am" && minute >= 9 * 60 + 25 && minute <= 11 * 60 + 30) {
      return minute - (9 * 60 + 25);
    }
    if (segment === "pm" && minute >= 13 * 60 && minute <= 15 * 60) {
      return 125 + SECTOR_FLOW_LUNCH_GAP_MINUTES + minute - 13 * 60;
    }
    return null;
  }

  function formatMinuteOfDay(value) {
    const minute = Math.round(value);
    return `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;
  }

  function formatSectorFlowTradingMinute(value) {
    if (!Number.isFinite(value)) return "--";
    if (value >= 125 && value <= 125 + SECTOR_FLOW_LUNCH_GAP_MINUTES) {
      return "11:30 / 13:00";
    }
    return value < 125
      ? formatMinuteOfDay(9 * 60 + 25 + value)
      : formatMinuteOfDay(13 * 60 + value - 125 - SECTOR_FLOW_LUNCH_GAP_MINUTES);
  }

  function sectorFlowSegments(item, mode) {
    const segments = [];
    let current = [];
    let previousSegment = null;
    let previousDayIndex = null;
    let previousTime = null;
    const flush = () => {
      if (current.length) segments.push(current);
      current = [];
    };
    asArray(item.points).forEach((point) => {
      if (!point || typeof point !== "object") {
        flush();
        previousSegment = null;
        previousDayIndex = null;
        previousTime = null;
        return;
      }
      const value = sectorFlowPointValue(point, mode);
      const time = Date.parse(point.provider_as_of);
      const segment = text(point.session_segment, "");
      const sessionMinute = sectorFlowTradingMinute(point.provider_as_of, segment);
      const dayIndex = mode === "five_day" ? Number(point._historyDayIndex) : 0;
      const tradingMinute = sessionMinute === null || !Number.isInteger(dayIndex)
        ? null
        : sessionMinute + dayIndex * SECTOR_FLOW_DAY_SPAN_MINUTES;
      if (value === null || !Number.isFinite(time) || tradingMinute === null) {
        flush();
        previousSegment = null;
        previousDayIndex = null;
        previousTime = null;
        return;
      }
      if (
        current.length
        && (
          segment !== previousSegment
          || dayIndex !== previousDayIndex
          || (
            previousTime !== null
            && (
              time <= previousTime
              || (time - previousTime) / 60_000 > MAX_SECTOR_FLOW_SAMPLE_GAP_MINUTES
            )
          )
        )
      ) {
        flush();
      }
      current.push({ dayIndex, point, segment, sessionMinute, time, tradingMinute, value });
      previousSegment = segment;
      previousDayIndex = dayIndex;
      previousTime = time;
    });
    flush();
    return segments;
  }

  function sectorFlowPathData(segments, x, y) {
    const commands = [];
    let previousEndpoint = null;
    asArray(segments).forEach((segment) => {
      if (!Array.isArray(segment) || segment.length < 2) return;
      const first = segment[0];
      const canBridgeLunch = previousEndpoint
        && previousEndpoint.dayIndex === first.dayIndex
        && previousEndpoint.segment === "am"
        && first.segment === "pm"
        && previousEndpoint.sessionMinute >= 125 - MAX_SECTOR_FLOW_SAMPLE_GAP_MINUTES
        && first.sessionMinute <= 125 + SECTOR_FLOW_LUNCH_GAP_MINUTES
          + MAX_SECTOR_FLOW_SAMPLE_GAP_MINUTES;
      const canBridgeDay = previousEndpoint
        && first.dayIndex === previousEndpoint.dayIndex + 1
        && previousEndpoint.segment === "pm"
        && first.segment === "am"
        && previousEndpoint.sessionMinute >= SECTOR_FLOW_SESSION_SPAN_MINUTES
          - MAX_SECTOR_FLOW_SAMPLE_GAP_MINUTES
        && first.sessionMinute <= 10;
      if (canBridgeLunch) {
        const startX = x(previousEndpoint.tradingMinute);
        const startY = y(previousEndpoint.value);
        const endX = x(first.tradingMinute);
        const endY = y(first.value);
        const bridgeSpan = Math.max(0, endX - startX);
        const controlX1 = startX + bridgeSpan * 0.42;
        const controlX2 = endX - bridgeSpan * 0.42;
        commands.push(`C${controlX1.toFixed(2)},${startY.toFixed(2)} ${controlX2.toFixed(2)},${endY.toFixed(2)} ${endX.toFixed(2)},${endY.toFixed(2)}`);
        segment.slice(1).forEach((point) => {
          commands.push(`L${x(point.tradingMinute).toFixed(2)},${y(point.value).toFixed(2)}`);
        });
      } else if (canBridgeDay) {
        for (const point of segment) {
          commands.push(`L${x(point.tradingMinute).toFixed(2)},${y(point.value).toFixed(2)}`);
        }
      } else {
        for (const [index, point] of segment.entries()) {
          commands.push(`${index ? "L" : "M"}${x(point.tradingMinute).toFixed(2)},${y(point.value).toFixed(2)}`);
        }
      }
      previousEndpoint = segment[segment.length - 1];
    });
    return commands.join(" ");
  }

  function hasRenderableSectorFlow(item, mode) {
    return sectorFlowSegments(item, mode).some((segment) => segment.length >= 2);
  }

  function sectorFlowPaletteColor(slot) {
    const hueSlot = SECTOR_FLOW_HUE_ORDER[slot % SECTOR_FLOW_HUE_ORDER.length];
    const band = Math.floor(slot / SECTOR_FLOW_HUE_ORDER.length);
    const hue = hueSlot * (360 / SECTOR_FLOW_HUE_ORDER.length);
    const [saturation, lightness] = SECTOR_FLOW_COLOR_BANDS[band];
    return `hsl(${hue.toFixed(1)} ${saturation}% ${lightness}%)`;
  }

  function sectorFlowColorSlots(payload, entries) {
    const tradeDate = text(payload?.tradeDate, "");
    const scope = payload?.direction === "offense" ? "offense" : "defense";
    let stored = state.sectorFlowColorAssignments;
    let changed = false;
    if (
      !stored
      || typeof stored !== "object"
      || Array.isArray(stored)
      || stored.tradeDate !== tradeDate
    ) {
      stored = { tradeDate, defense: {}, offense: {} };
      state.sectorFlowColorAssignments = stored;
      changed = true;
    }

    const rawAssignments = stored[scope];
    const assignments = {};
    const usedSlots = new Set();
    if (rawAssignments && typeof rawAssignments === "object" && !Array.isArray(rawAssignments)) {
      Object.entries(rawAssignments).forEach(([sectorKey, slot]) => {
        if (
          sectorKey
          && Number.isInteger(slot)
          && slot >= 0
          && slot < SECTOR_FLOW_COLOR_SLOT_COUNT
          && !usedSlots.has(slot)
        ) {
          assignments[sectorKey] = slot;
          usedSlots.add(slot);
        } else {
          changed = true;
        }
      });
    } else {
      changed = true;
    }
    stored[scope] = assignments;

    const slots = entries.map(({ item }) => {
      const sectorKey = text(item?.sector_key, "unknown");
      if (Number.isInteger(assignments[sectorKey])) return assignments[sectorKey];
      const slot = Array.from(
        { length: SECTOR_FLOW_COLOR_SLOT_COUNT },
        (_unused, candidate) => candidate,
      ).find((candidate) => !usedSlots.has(candidate));
      const assignedSlot = slot ?? 0;
      assignments[sectorKey] = assignedSlot;
      usedSlots.add(assignedSlot);
      changed = true;
      return assignedSlot;
    });
    if (changed) storeJson(STORAGE_KEYS.sectorFlowColorAssignments, stored);
    return slots;
  }

  function sectorFlowSeries(payload, mode) {
    const entries = asArray(payload?.sectors)
      .map((item) => ({
        item,
        segments: sectorFlowSegments(item, mode),
      }))
      .filter((entry) => entry.segments.some((segment) => segment.length >= 1))
      .slice(0, MAX_SECTOR_FLOW_CHART_SERIES);
    const colorSlots = sectorFlowColorSlots(payload, entries);
    return entries.map((entry, index) => ({
      ...entry,
      color: sectorFlowPaletteColor(colorSlots[index]),
    }));
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
    const valueLabel = mode === "five_day"
      ? "五日连续累计估计"
      : mode === "delta_5m"
        ? "近 5 分钟变化"
        : "当日累计估计";
    const historyDate = text(point._historyTradeDate, "");
    tooltip.replaceChildren(
      createElement("strong", "", text(item.name, item.sector_key)),
      createElement(
        "span",
        "",
        `${historyDate ? `${historyDate} ` : ""}${formatShortTime(point.provider_as_of)} · ${valueLabel}`,
      ),
      createElement("span", toneClass(value), formatCny(value, true)),
    );
    tooltip.hidden = false;
    const bounds = shell.getBoundingClientRect();
    const left = Math.max(8, Math.min(shell.clientWidth - 238, event.clientX - bounds.left + 12));
    const top = Math.max(8, Math.min(shell.clientHeight - 78, event.clientY - bounds.top + 12));
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  }

  function nearestSectorFlowObservedPoint(event, svg, entry, x) {
    const matrix = svg.getScreenCTM();
    if (!matrix) return null;
    const pointer = svg.createSVGPoint();
    pointer.x = event.clientX;
    pointer.y = event.clientY;
    const pointerX = pointer.matrixTransform(matrix.inverse()).x;
    return entry.segments.flat().reduce((nearest, point) => {
      const distance = Math.abs(x(point.tradingMinute) - pointerX);
      return !nearest || distance < nearest.distance ? { point, distance } : nearest;
    }, null)?.point || null;
  }

  function focusSectorFlowSeries(svg, sectorKey = null) {
    const groups = [...svg.querySelectorAll("[data-sector-flow]")];
    groups.forEach((group) => {
      const active = !sectorKey || group.dataset.sectorFlow === sectorKey;
      // Opacity alone leaves transparent hit paths and endpoint strokes active.
      group.setAttribute("display", active ? "inline" : "none");
      const line = group.querySelector("[data-sector-flow-line]");
      const endpoint = group.querySelector("[data-sector-flow-endpoint]");
      const label = group.querySelector("[data-sector-flow-endpoint-label]");
      if (line) {
        line.setAttribute(
          "stroke-opacity",
          sectorKey
            ? (active ? "1" : "0")
            : text(group.dataset.defaultStrokeOpacity, "0.88"),
        );
        line.setAttribute(
          "stroke-width",
          sectorKey
            ? (active ? "2.9" : "1.15")
            : text(group.dataset.defaultStrokeWidth, "2.1"),
        );
      }
      if (endpoint) endpoint.setAttribute("fill-opacity", sectorKey && !active ? "0" : "1");
      if (label) label.setAttribute("fill-opacity", sectorKey && !active ? "0" : "1");
    });
  }

  function setSectorFlowSeriesLock(svg, sectorKey = null) {
    const lockedSectorKey = sectorKey && [...svg.querySelectorAll("[data-sector-flow]")]
      .some((group) => group.dataset.sectorFlow === sectorKey)
      ? sectorKey
      : null;
    if (lockedSectorKey) svg.dataset.lockedSectorFlow = lockedSectorKey;
    else delete svg.dataset.lockedSectorFlow;
    focusSectorFlowSeries(svg, lockedSectorKey);
  }

  function renderSectorFlowLegend(series, scope) {
    const legend = sectorFlowElement(scope, "legend");
    const svg = sectorFlowElement(scope, "chart");
    legend.replaceChildren();
    series.forEach(({ item, color }) => {
      const sectorKey = text(item.sector_key, "unknown");
      const entry = createElement("span", "sector-flow-legend-item");
      const swatch = createElement("i");
      swatch.style.background = color;
      entry.append(swatch, document.createTextNode(text(item.name, item.sector_key)));
      entry.tabIndex = 0;
      entry.addEventListener("click", (event) => {
        event.stopPropagation();
        setSectorFlowSeriesLock(svg, sectorKey);
      });
      legend.append(entry);
    });
  }

  function renderSectorFlowMiniChart(payload, scope) {
    const svg = sectorFlowElement(scope, "mini-chart");
    svg.replaceChildren();

    const mode = state.sectorFlowMode[scope];
    const preparedSeries = arguments.length > 2 ? arguments[2] : null;
    const series = preparedSeries || sectorFlowSeries(payload, mode);
    if (!series.length) {
      const message = createSvgElement("text", {
        x: 210,
        y: 36,
        fill: "#708692",
        "font-size": 10,
        "text-anchor": "middle",
      });
      message.textContent = mode === "five_day"
        ? "近五日分钟轨迹读取中"
        : mode === "delta_5m"
          ? "近 5 分钟轨迹积累中"
          : "资金轨迹积累中";
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
      const pathData = sectorFlowPathData(entry.segments, x, y);
      if (pathData) {
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
      }
      const endpointSegment = [...entry.segments].reverse().find((segment) => segment.length >= 1);
      if (endpointSegment) {
        const endpoint = endpointSegment[endpointSegment.length - 1];
        group.append(createSvgElement("circle", {
          cx: x(endpoint.tradingMinute),
          cy: y(endpoint.value),
          r: 2.2,
          fill: entry.color,
          stroke: "#071016",
          "stroke-width": 0.8,
          "vector-effect": "non-scaling-stroke",
        }));
      }
      svg.append(group);
    });
  }

  function renderSectorFlowChart(payload, scope) {
    const svg = sectorFlowElement(scope, "chart");
    const shell = sectorFlowElement(scope, "chart-shell");
    const empty = sectorFlowElement(scope, "empty");
    svg.replaceChildren();
    hideSectorFlowTooltip(scope);

    const mode = scope === "catalog" ? "cumulative" : state.sectorFlowMode[scope];
    const preparedSeries = arguments.length > 2 ? arguments[2] : null;
    const series = preparedSeries || sectorFlowSeries(payload, mode);
    if (!series.length) {
      empty.hidden = false;
      empty.textContent = mode === "five_day"
        ? "近五个交易日的真实分钟轨迹暂不可用；不会复制旧值或用相邻日补线。"
        : mode === "delta_5m"
          ? "近 5 分钟同源基线仍在积累，不会用相邻分钟或跨午休数据替代。"
          : "等待首个真实板块资金点；不会复制集合竞价数据补线。";
      renderSectorFlowLegend([], scope);
      return;
    }
    empty.hidden = true;

    const width = 980;
    const margin = { top: 24, right: 185, bottom: mode === "five_day" ? 48 : 38, left: 72 };
    const endpointLabelCount = series.filter((entry) => (
      [...entry.segments].reverse().some((segment) => segment.length >= 1)
    )).length;
    const height = Math.max(
      360,
      margin.top + margin.bottom + 14
        + Math.max(0, endpointLabelCount - 1) * SECTOR_FLOW_ENDPOINT_GAP_PX,
    );
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    shell.style.height = `${height}px`;
    svg.style.height = `${height}px`;
    const plotBottom = height - margin.bottom;
    const allPoints = series.flatMap((entry) => entry.segments.flat());
    const includesMorning = allPoints.some((point) => point.segment === "am");
    const includesAfternoon = allPoints.some((point) => point.segment === "pm");
    let xMin = Math.min(...allPoints.map((point) => point.tradingMinute));
    let xMax = Math.max(...allPoints.map((point) => point.tradingMinute));
    const historyTradeDates = mode === "five_day" ? asArray(payload.historyTradeDates) : [];
    if (historyTradeDates.length) {
      xMin = 0;
      xMax = (historyTradeDates.length - 1) * SECTOR_FLOW_DAY_SPAN_MINUTES
        + SECTOR_FLOW_SESSION_SPAN_MINUTES;
    }
    if (xMin === xMax) {
      xMin -= 1;
      xMax += 1;
    }
    const values = [0, ...allPoints.map((point) => point.value)];
    let yScaleMin = Math.min(...values);
    let yScaleMax = Math.max(...values);
    if (yScaleMin === yScaleMax) {
      const pad = Math.max(Math.abs(yScaleMin) * 0.1, 100_000_000);
      yScaleMin -= pad;
      yScaleMax += pad;
    } else {
      const pad = (yScaleMax - yScaleMin) * 0.08;
      yScaleMin -= pad;
      yScaleMax += pad;
    }
    const endpointScaleValues = [...new Set(series.flatMap((entry) => {
      const finalSegment = [...entry.segments].reverse().find((segment) => segment.length >= 1);
      return finalSegment ? [finalSegment[finalSegment.length - 1].value] : [];
    }))].sort((left, right) => left - right);
    const y = (value) => {
      const amountPosition = (value - yScaleMin) / (yScaleMax - yScaleMin);
      const densityPosition = sectorFlowEndpointRank(value, endpointScaleValues);
      const blendedPosition = (1 - SECTOR_FLOW_ENDPOINT_DENSITY_WEIGHT) * amountPosition
        + SECTOR_FLOW_ENDPOINT_DENSITY_WEIGHT * densityPosition;
      return plotBottom - blendedPosition * (plotBottom - margin.top);
    };
    const plotRight = width - margin.right;
    const x = (value) => margin.left
      + ((value - xMin) / (xMax - xMin)) * (plotRight - margin.left);

    const grid = createSvgElement("g", { "aria-hidden": "true" });
    const zeroY = y(0);
    const tickValues = [0];
    [0, 0.25, 0.5, 0.75, 1].forEach((ratio) => {
      const candidate = yScaleMax - ratio * (yScaleMax - yScaleMin);
      if (tickValues.every((value) => Math.abs(y(value) - y(candidate)) >= 16)) {
        tickValues.push(candidate);
      }
    });
    tickValues.sort((left, right) => y(left) - y(right)).forEach((value) => {
      const yPosition = y(value);
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
    scaleLabel.textContent = mode === "five_day"
      ? "五个交易日连续展开 · 五日连续累计净额 · 刻度为真实金额"
      : "金额比例 + 终点密度混合 Y 轴 · 刻度为真实金额";
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
    if (historyTradeDates.length) {
      historyTradeDates.forEach((tradeDate, dayIndex) => {
        const dayStart = dayIndex * SECTOR_FLOW_DAY_SPAN_MINUTES;
        const dayEnd = dayStart + SECTOR_FLOW_SESSION_SPAN_MINUTES;
        if (dayIndex > 0) {
          const separatorX = x(dayStart - SECTOR_FLOW_DAY_GAP_MINUTES / 2);
          grid.append(createSvgElement("line", {
            x1: separatorX,
            x2: separatorX,
            y1: margin.top,
            y2: plotBottom,
            stroke: "#4b626f",
            "stroke-dasharray": "2 5",
            "stroke-opacity": 0.72,
            "stroke-width": 1,
          }));
        }
        const lunchX = x(dayStart + 125 + SECTOR_FLOW_LUNCH_GAP_MINUTES / 2);
        grid.append(createSvgElement("line", {
          x1: lunchX,
          x2: lunchX,
          y1: margin.top,
          y2: plotBottom,
          stroke: "#314854",
          "stroke-dasharray": "2 6",
          "stroke-opacity": 0.42,
          "stroke-width": 1,
        }));
        const label = createSvgElement("text", {
          x: x((dayStart + dayEnd) / 2),
          y: height - 15,
          fill: "#8da1ad",
          "font-size": 9,
          "font-weight": 700,
          "text-anchor": "middle",
        });
        label.textContent = text(tradeDate, "").slice(5);
        grid.append(label);
      });
    } else {
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
      if (
        includesMorning
        && includesAfternoon
        && xMin <= 125
        && xMax >= 125 + SECTOR_FLOW_LUNCH_GAP_MINUTES
      ) {
        const breakX = x(125 + SECTOR_FLOW_LUNCH_GAP_MINUTES / 2);
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
    }
    svg.append(grid);

    series.forEach((entry, seriesIndex) => {
      const sectorKey = text(entry.item.sector_key, "unknown");
      const lineGroup = createSvgElement("g", { "data-sector-flow": sectorKey });
      lineGroup.dataset.defaultStrokeOpacity = "0.96";
      lineGroup.dataset.defaultStrokeWidth = "2";
      const pathData = sectorFlowPathData(entry.segments, x, y);
      const hasConfirmedPath = entry.segments.some((segment) => segment.length >= 2);
      if (pathData) {
        lineGroup.append(createSvgElement("path", {
          d: pathData,
          fill: "none",
          stroke: entry.color,
          "stroke-linecap": "round",
          "stroke-linejoin": "round",
          "stroke-opacity": lineGroup.dataset.defaultStrokeOpacity,
          "stroke-width": lineGroup.dataset.defaultStrokeWidth,
          "vector-effect": "non-scaling-stroke",
          "data-sector-flow-line": sectorKey,
        }));
        const hitPath = createSvgElement("path", {
          d: pathData,
          fill: "none",
          stroke: entry.color,
          "stroke-linecap": "round",
          "stroke-linejoin": "round",
          "stroke-opacity": 0,
          "stroke-width": SECTOR_FLOW_HIT_STROKE_PX,
          "pointer-events": "stroke",
          cursor: "crosshair",
          "data-sector-flow-hit-target": text(entry.item.sector_key, "unknown"),
          "data-sector-flow-click-target": sectorKey,
        });
        const showLineTooltip = (event) => {
          const observedPoint = nearestSectorFlowObservedPoint(event, svg, entry, x);
          if (observedPoint) {
            showSectorFlowTooltip(event, entry.item, observedPoint.point, mode, scope);
          }
        };
        hitPath.addEventListener("pointerenter", showLineTooltip);
        hitPath.addEventListener("pointermove", showLineTooltip);
        hitPath.addEventListener("pointerleave", () => {
          hideSectorFlowTooltip(scope);
        });
        hitPath.addEventListener("click", (event) => {
          event.stopPropagation();
          setSectorFlowSeriesLock(svg, sectorKey);
        });
        lineGroup.append(hitPath);
      }
      const finalSegment = [...entry.segments].reverse().find((segment) => segment.length >= 1);
      if (finalSegment) {
        const endpoint = finalSegment[finalSegment.length - 1];
        const endpointNode = createSvgElement("circle", {
          cx: x(endpoint.tradingMinute),
          cy: y(endpoint.value),
          r: 4.5,
          fill: entry.color,
          stroke: "#071016",
          "stroke-width": 1.2,
          "data-sector-flow-endpoint": text(entry.item.sector_key, "unknown"),
          "data-sector-flow-first-point": !hasConfirmedPath ? sectorKey : null,
          "data-sector-flow-click-target": sectorKey,
        });
        const showEndpointTooltip = (event) => {
          showSectorFlowTooltip(event, entry.item, endpoint.point, mode, scope);
        };
        endpointNode.addEventListener("pointerenter", showEndpointTooltip);
        endpointNode.addEventListener("pointermove", showEndpointTooltip);
        endpointNode.addEventListener("pointerleave", () => {
          hideSectorFlowTooltip(scope);
        });
        endpointNode.addEventListener("click", (event) => {
          event.stopPropagation();
          setSectorFlowSeriesLock(svg, sectorKey);
        });
        lineGroup.append(endpointNode);
        if (seriesIndex < MAX_SECTOR_FLOW_ENDPOINT_LABELS) {
          const textNode = createSvgElement("text", {
            x: x(endpoint.tradingMinute) + 9,
            y: y(endpoint.value) + 3,
            fill: entry.color,
            "font-size": 10,
            "font-weight": 700,
            "data-sector-flow-endpoint-label": sectorKey,
            "data-sector-flow-click-target": sectorKey,
          });
          textNode.textContent = `${text(entry.item.name, entry.item.sector_key)} ${formatChartCny(endpoint.value)}${hasConfirmedPath ? "" : " · 1次观测"}`;
          textNode.addEventListener("pointerenter", showEndpointTooltip);
          textNode.addEventListener("pointermove", showEndpointTooltip);
          textNode.addEventListener("pointerleave", () => {
            hideSectorFlowTooltip(scope);
          });
          textNode.addEventListener("click", (event) => {
            event.stopPropagation();
            setSectorFlowSeriesLock(svg, sectorKey);
          });
          lineGroup.append(textNode);
        }
      }
      svg.append(lineGroup);
    });
    setSectorFlowSeriesLock(svg, text(svg.dataset.lockedSectorFlow, "") || null);
    renderSectorFlowLegend(series, scope);
  }

  // The directory supplies revision-validated data and selection. Path, axis,
  // endpoint, tooltip and focus behavior stay in the existing chart renderer.
  const catalogFlowColors = new Map();
  window.TradexSectorFlowChart = Object.freeze({
    renderCatalog(payload) {
      const entries = asArray(payload.sectors).map(item => ({
        item, segments: sectorFlowSegments(item, "cumulative"),
      })).filter(entry => entry.segments.length);
      const keys = new Set(entries.map(entry => entry.item.sector_key));
      for (const key of catalogFlowColors.keys()) if (!keys.has(key)) catalogFlowColors.delete(key);
      const used = new Set(catalogFlowColors.values());
      const series = entries.map(entry => {
        const key = entry.item.sector_key;
        if (!catalogFlowColors.has(key)) {
          let slot = 0;
          while (used.has(slot)) slot++;
          catalogFlowColors.set(key, slot); used.add(slot);
        }
        return { ...entry, color: sectorFlowPaletteColor(catalogFlowColors.get(key)) };
      });
      renderSectorFlowChart(payload, "catalog", series);
    },
  });

  function renderSectorFlowLeaders(item, latest) {
    const snapshot = item.leader_snapshot && typeof item.leader_snapshot === "object"
      ? item.leader_snapshot
      : {};
    const isResonanceSnapshot = snapshot.selection_method === "sector_fund_flow_path_resonance.v2";
    const isUpResonance = isResonanceSnapshot && snapshot.resonance_direction === "up";
    const leaders = isUpResonance ? asArray(snapshot.leaders).slice(0, 1) : [];
    const leaderLabel = isResonanceSnapshot ? "共振领涨股" : "共振代表股";
    const block = createElement("div", "sector-flow-observation-item__leaders");
    block.title = "共振领涨股：只识别板块5分钟资金边际流入；资金累计轨迹与个股价格累计轨迹相关系数不低于0.60，同向区间占比不低于60%，且个股5分钟涨幅不低于0.10%。股票分钟线轻微滞后时使用2分钟内最近的完整共同窗口。";
    block.append(createElement("span", "sector-flow-observation-item__leaders-label", leaderLabel));
    const list = createElement("div", "sector-flow-observation-item__leaders-list");
    if (!leaders.length) {
      list.append(createElement(
        "span",
        "sector-flow-leaders-empty",
        isResonanceSnapshot
          ? (isUpResonance
            ? text(snapshot.status_label, "暂无高共振股")
            : "板块近5分钟未边际流入")
          : "共振回溯暂缺",
      ));
      block.append(list);
      return block;
    }
    leaders.forEach((leader) => {
      const instrumentId = text(leader.instrument_id, "");
      const code = instrumentId.includes(".") ? instrumentId.split(".")[0] : instrumentId;
      const leaderChange = finiteNumber(leader.change_pct);
      const leaderSpeed = finiteNumber(leader.speed_pct);
      const correlation = finiteNumber(leader.resonance_correlation);
      const agreement = finiteNumber(leader.directional_agreement_ratio);
      const chip = createElement("span", "sector-flow-leader");
      chip.append(
        createElement("strong", "", text(leader.name, code || "未命名股票")),
        createElement("small", "", code),
        createElement(
          "span",
          leaderChange === null ? "" : toneClass(leaderChange),
          leaderChange === null ? "--" : formatChangePct(leaderChange),
        ),
        createElement(
          "span",
          leaderSpeed === null ? "sector-flow-leader__speed" : `sector-flow-leader__speed ${toneClass(leaderSpeed)}`,
          leaderSpeed === null ? "5分 --" : `5分 ${formatChangePct(leaderSpeed)}`,
        ),
        createElement(
          "span",
          "sector-flow-leader__correlation",
          correlation === null ? "相关 --" : `相关 ${correlation.toFixed(2)}`,
        ),
        createElement(
          "span",
          "sector-flow-leader__agreement",
          agreement === null ? "同向 --" : `同向 ${(agreement * 100).toFixed(0)}%`,
        ),
      );
      list.append(chip);
    });
    block.append(list);
    return block;
  }

  function sectorMoveRadarAvailability(snapshot, now = Date.now()) {
    const marketState = normalizeMarketState(snapshot);
    if (!marketState.isOpen) {
      return {
        available: false,
        message: "盘中异动雷达仅在 A 股连续竞价时段运行。",
        status: "非交易时段 · 已暂停",
        usableScopes: new Set(),
      };
    }
    const usableScopes = new Set(["defense", "offense"].filter((scope) => {
      const payload = sectorFlowPayload(snapshot, scope);
      if (!payload || !(payload.status === "ready" || payload.status === "partial")) {
        return false;
      }
      const asOf = Date.parse(payload.asOf);
      const age = now - asOf;
      return Number.isFinite(asOf) && age >= -30_000 && age <= SECTOR_MOVE_MAX_AGE_MS;
    }));
    if (!usableScopes.size) {
      return {
        available: false,
        message: "板块轨迹不可用或已超过 2 分钟；等待轨迹自身刷新，不受其他盘面组件影响。",
        status: "板块轨迹陈旧 · 雷达锁定",
        usableScopes,
      };
    }
    return {
      available: true,
      message: "",
      status: normalizeFreshness(snapshot).status === "fresh"
        ? "板块轨迹新鲜"
        : "板块轨迹可用 · 全局降级不锁雷达",
      usableScopes,
    };
  }

  function sectorMoveCandidates(snapshot) {
    const availability = sectorMoveRadarAvailability(snapshot);
    if (!availability.available) return [];
    const candidates = ["defense", "offense"].flatMap((scope) => {
      if (!availability.usableScopes.has(scope)) return [];
      const payload = sectorFlowPayload(snapshot, scope);
      if (!payload || payload.status === "unavailable") return [];
      return payload.sectors.flatMap((item) => {
        const latest = item?.latest && typeof item.latest === "object"
          ? item.latest
          : {};
        const changeDelta = finiteNumber(latest.change_delta_5m_pct);
        if (
          changeDelta === null
          || Math.abs(changeDelta) < state.sectorFlowSurgeThreshold
        ) return [];
        return [{
          ...item,
          _move: {
            changeDelta,
            direction: changeDelta > 0 ? "strengthening" : "weakening",
            scope,
          },
        }];
      });
    });
    return candidates
      .sort((left, right) => (
        Math.abs(right._move.changeDelta) - Math.abs(left._move.changeDelta)
        || text(left.name, left.sector_key).localeCompare(text(right.name, right.sector_key), "zh-CN")
      ))
      .slice(0, 6);
  }

  function renderSectorMoveMiniChart(item) {
    const svg = createSvgElement("svg", {
      class: "sector-move-card__chart",
      viewBox: "0 0 360 66",
      preserveAspectRatio: "none",
      role: "img",
      "aria-label": `${text(item.name, item.sector_key)}实时资金轨迹`,
    });
    const deltaSegments = sectorFlowSegments(item, "delta_5m");
    const mode = deltaSegments.some((segment) => segment.length >= 2)
      ? "delta_5m"
      : "cumulative";
    const segments = mode === "delta_5m" ? deltaSegments : sectorFlowSegments(item, mode);
    const points = segments.flat();
    if (points.length < 2) {
      const message = createSvgElement("text", {
        x: 180,
        y: 37,
        fill: "#708692",
        "font-size": 9,
        "text-anchor": "middle",
      });
      message.textContent = "真实资金轨迹积累中";
      svg.append(message);
      return svg;
    }

    const width = 360;
    const height = 66;
    const margin = { top: 8, right: 5, bottom: 8, left: 5 };
    let xMin = Math.min(...points.map((point) => point.tradingMinute));
    let xMax = Math.max(...points.map((point) => point.tradingMinute));
    if (xMin === xMax) {
      xMin -= 1;
      xMax += 1;
    }
    const values = [0, ...points.map((point) => point.value)];
    let yMin = Math.min(...values);
    let yMax = Math.max(...values);
    if (yMin === yMax) {
      const pad = Math.max(Math.abs(yMin) * 0.1, 100_000_000);
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
      "stroke-opacity": 0.32,
      "stroke-width": 1,
      "vector-effect": "non-scaling-stroke",
    }));
    const pathData = sectorFlowPathData(segments, x, y);
    if (pathData) {
      svg.append(createSvgElement("path", {
        d: pathData,
        fill: "none",
        stroke: item._move.direction === "strengthening" ? "#ff7c79" : "#4dd19b",
        "stroke-linecap": "round",
        "stroke-linejoin": "round",
        "stroke-width": 2,
        "vector-effect": "non-scaling-stroke",
      }));
    }
    return svg;
  }

  function renderSectorMoveRadar(snapshot) {
    const target = byId("sector-move-radar-list");
    const status = byId("sector-move-radar-status");
    if (!target || !status) return;
    const availability = sectorMoveRadarAvailability(snapshot);
    document.querySelectorAll("[data-sector-flow-surge-threshold]").forEach((select) => {
      select.value = String(state.sectorFlowSurgeThreshold);
    });
    target.replaceChildren();
    if (!availability.available) {
      status.textContent = availability.status;
      target.append(createElement("p", "empty-state", availability.message));
      return;
    }

    const candidates = sectorMoveCandidates(snapshot);
    status.textContent = candidates.length
      ? `${candidates.length} 个板块达到阈值 · ${availability.status}`
      : `扫描中 · ±${state.sectorFlowSurgeThreshold.toFixed(2)}pp · ${availability.status}`;
    if (!candidates.length) {
      target.append(createElement("p", "empty-state", "尚未发现达到阈值的板块异动。"));
      return;
    }

    const confirmedAlerts = asArray(snapshot.alerts).filter((alert) => (
      alert && alert.kind === "sector_move"
    ));
    candidates.forEach((item) => {
      const latest = item.latest && typeof item.latest === "object" ? item.latest : {};
      const move = item._move;
      const confirmed = confirmedAlerts.some((alert) => (
        text(alert.sector_key, "") === text(item.sector_key, "")
        && text(alert.sector_direction, "") === move.scope
        && text(alert.move_direction, "") === move.direction
      ));
      const card = createElement("article", `sector-move-card is-${move.direction}`);
      card.dataset.sectorMove = text(item.sector_key, "unknown");
      card.dataset.sectorMoveScope = move.scope;
      const head = createElement("div", "sector-move-card__head");
      const title = createElement("div");
      title.append(
        createElement("span", "eyebrow", text(item.category_name, "板块异动")),
        createElement("h3", "", text(item.name, item.sector_key)),
      );
      const badges = createElement("div", "sector-move-card__badges");
      badges.append(
        createElement("span", "sector-move-card__scope", move.scope === "offense" ? "进攻" : "防御"),
        createElement(
          "span",
          `sector-move-card__state${confirmed ? " is-confirmed" : ""}`,
          confirmed ? "提醒已确认" : move.direction === "strengthening" ? "突然增强" : "突然走弱",
        ),
      );
      head.append(title, badges);
      const metrics = createElement("div", "sector-move-card__metrics");
      const values = [
        ["5分突变", formatChangePct(move.changeDelta), toneClass(move.changeDelta)],
        ["板块涨幅", formatChangePct(latest.change_pct), toneClass(finiteNumber(latest.change_pct))],
        ["5分资金", formatCny(latest.delta_5m_cny, true), toneClass(finiteNumber(latest.delta_5m_cny))],
      ];
      values.forEach(([label, value, tone]) => {
        const metric = createElement("div");
        metric.append(
          createElement("span", "", label),
          createElement("strong", tone, value),
        );
        metrics.append(metric);
      });
      card.append(
        head,
        metrics,
        renderSectorMoveMiniChart(item),
        renderSectorFlowLeaders(item, latest),
      );
      target.append(card);
    });
  }

  function compareSectorFlowByCurrentAmount(left, right) {
    const leftAmount = finiteNumber(left?.latest?.cumulative_cny);
    const rightAmount = finiteNumber(right?.latest?.cumulative_cny);
    if (leftAmount === null && rightAmount === null) {
      return text(left?.name, left?.sector_key).localeCompare(
        text(right?.name, right?.sector_key),
        "zh-CN",
      );
    }
    if (leftAmount === null) return 1;
    if (rightAmount === null) return -1;
    if (leftAmount !== rightAmount) return rightAmount - leftAmount;
    return text(left?.name, left?.sector_key).localeCompare(
      text(right?.name, right?.sector_key),
      "zh-CN",
    );
  }

  function sectorFlowCurrentAmountLabel(value) {
    const amount = finiteNumber(value);
    if (amount === null) return "当前资金 --";
    const direction = amount > 0 ? "当前流入" : amount < 0 ? "当前流出" : "当前净流";
    return `${direction} ${formatCny(Math.abs(amount))}`;
  }

  function renderSectorFlowObservations(payload, scope) {
    const target = sectorFlowElement(scope, "observation-list");
    target.replaceChildren();
    if (!payload.sectors.length) {
      const scopeLabel = payload.direction === "offense" ? "进攻" : "防守";
      target.append(createElement("p", "empty-state", `等待可比较的${scopeLabel}方向证据`));
      return;
    }
    const sectors = [...payload.sectors].sort(compareSectorFlowByCurrentAmount);
    sectors.forEach((item, index) => {
      const latest = item.latest && typeof item.latest === "object" ? item.latest : {};
      const change = finiteNumber(latest.change_pct);
      const currentAmount = finiteNumber(latest.cumulative_cny);
      const rising = change !== null && change > 0;
      const display = item._display && typeof item._display === "object" ? item._display : {};
      const changeDelta = finiteNumber(display.changeDelta);
      const automatic = Boolean(display.automatic);
      const triggered = Boolean(display.triggered) && changeDelta !== null;
      const card = createElement(
        "article",
        `sector-flow-observation-item ${rising ? "is-rising" : "is-non-rising"}${automatic ? " is-automatic" : ""}${triggered ? ` is-surge is-surge-${changeDelta > 0 ? "up" : "down"}` : ""}`,
      );
      card.dataset.currentFlowCny = currentAmount === null ? "" : String(currentAmount);
      if (triggered) card.dataset.sectorFlowSurge = changeDelta > 0 ? "up" : "down";
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
        createElement(
          "span",
          `sector-flow-current-flow ${toneClass(currentAmount)}`,
          sectorFlowCurrentAmountLabel(currentAmount),
        ),
      );
      if (automatic) title.append(createElement("span", "sector-flow-auto-badge", "自动出现"));
      if (triggered) {
        title.append(createElement(
          "span",
          `sector-flow-surge-badge is-${changeDelta > 0 ? "up" : "down"}`,
          `⚡涨速突变 ${changeDelta > 0 ? "+" : ""}${changeDelta.toFixed(2)}pp/5分`,
        ));
      }
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

  function clearSectorFlowExpandedChart(scope) {
    sectorFlowElement(scope, "chart").replaceChildren();
    sectorFlowElement(scope, "legend").replaceChildren();
    sectorFlowElement(scope, "empty").hidden = true;
    hideSectorFlowTooltip(scope);
  }

  function renderSectorFlowTrajectory(snapshot, scope, { renderPicker = true } = {}) {
    const payload = sectorFlowPayload(snapshot, scope);
    const scopeLabel = scope === "offense" ? "进攻" : "防御";
    if (!payload) {
      renderSectorFlowUnavailable("资金轨迹子契约暂不可用，不会影响上方宏观盘面结论。", scope);
      return;
    }
    const displayPayload = sectorFlowDisplayPayload(payload);
    if (renderPicker) renderSectorFlowPicker(payload, displayPayload, scope);
    else updateSectorFlowPickerSummary(payload, displayPayload, scope);
    renderSectorFlowObservations(displayPayload, scope);
    const fiveDayState = state.fiveDayTrajectory[scope];
    const fiveDayKeys = state.lastSummary
      ? requiredSectorKeys(state.lastSummary, scope)
      : displayPayload.sectors.map((item) => text(item.sector_key, "")).filter(Boolean);
    const fiveDayCacheKey = `${scope}:${[...new Set(fiveDayKeys)].sort().join(",")}`;
    const currentFiveDayPayload = fiveDayState.cacheKey === fiveDayCacheKey
      ? fiveDayState.payload
      : null;
    const fiveDayPayload = state.sectorFlowMode[scope] === "five_day"
      ? sectorFlowFiveDayPayload(currentFiveDayPayload, displayPayload)
      : null;
    const chartPayload = state.sectorFlowMode[scope] === "five_day"
      ? (fiveDayPayload || { ...displayPayload, sectors: [] })
      : displayPayload;
    const panel = document.querySelector(`.sector-flow-panel[data-flow-scope="${scope}"]`);
    panel.querySelectorAll("[data-flow-mode]").forEach((button) => {
      const mode = button.dataset.flowMode;
      const active = mode === state.sectorFlowMode[scope];
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
      button.disabled = false;
      button.title = mode === "five_day" ? "读取每日缓存中的最近五个交易日分钟轨迹" : "";
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
    if (state.sectorFlowMode[scope] === "five_day") {
      const dates = asArray(fiveDayPayload?.historyTradeDates);
      const available = Number(fiveDayPayload?.historyAvailableDays) || 0;
      sectorFlowElement(scope, "status").textContent = fiveDayState.error
        ? "近五日读取失败"
        : fiveDayPayload
          ? `${fiveDayPayload.status === "ready" ? "近五日完整" : "近五日部分可用"} · ${available}/5 日`
          : "近五日读取中";
      sectorFlowElement(scope, "chart-caption").textContent = "近五个交易日连续分钟资金轨迹";
      sectorFlowElement(scope, "chart-as-of").textContent = dates.length
        ? `${dates[0]} — ${dates.at(-1)} · 连续累计`
        : "交易日范围 --";
      sectorFlowElement(scope, "note").textContent = fiveDayState.error
        ? `每日缓存读取失败（${fiveDayState.error}）；不会用旧值、相邻日或当前值补线。`
        : "近五日图按交易日顺序累加同一板块的每日累计净额；相邻交易日首尾接续，日界线保留，缺失分钟仍保持断点。资金仍是估计 / 辅助，不构成买卖建议。";
    } else {
      sectorFlowElement(scope, "status").textContent = `${statusLabels[payload.status]}${phaseSuffix}`;
      sectorFlowElement(scope, "chart-caption").textContent = "当日累计净流入 / 出";
      sectorFlowElement(scope, "chart-as-of").textContent = `轨迹时点 ${formatTimestamp(payload.asOf)}`;
      sectorFlowElement(scope, "note").textContent = payload.status === "unavailable"
        ? `资金轨迹暂不可用（${payload.reason || "缺少可比较同源水位"}）；现有宏观盘面结论保持独立。`
        : scope === "offense"
          ? "进攻方向按行业锚与其细分概念分层；概念轨迹复用同一全市场分钟快照，领涨股来自板块快照并由成分行情补全。资金仍是估计 / 辅助，不构成买卖建议。"
          : "资金为供应商口径下的日内累计估计；行业与概念仅按各自同类分位比较，不代表资金从一个板块确定转移到另一个板块，也不构成买卖建议。";
    }
    const preparedSeries = sectorFlowSeries(chartPayload, state.sectorFlowMode[scope]);
    renderSectorFlowMiniChart(chartPayload, scope, preparedSeries);
    const chartCard = sectorFlowElement(scope, "chart-card");
    if (chartCard.open) renderSectorFlowChart(chartPayload, scope, preparedSeries);
    else clearSectorFlowExpandedChart(scope);
  }

  function renderSectorFlowTrajectories(snapshot) {
    renderSectorFlowTrajectory(snapshot, "defense");
    renderSectorFlowTrajectory(snapshot, "offense");
  }

  const pendingSectorFlowRenders = new Map();

  function scheduleSectorFlowRender(scope, { renderPicker = false } = {}) {
    const pending = pendingSectorFlowRenders.get(scope);
    if (pending) {
      pending.renderPicker = pending.renderPicker || renderPicker;
      return;
    }
    const request = { renderPicker };
    pendingSectorFlowRenders.set(scope, request);
    window.requestAnimationFrame(async () => {
      pendingSectorFlowRenders.delete(scope);
      if (!state.lastSnapshot) return;
      try {
        await hydrateCurrentSectorFlow(scope);
        if (state.sectorFlowMode[scope] === "five_day") {
          await hydrateFiveDaySectorFlow(scope);
        }
      } catch (error) {
        if (error.discardBatch || error.noAcceptedReal) {
          discardMarketWatchBatch();
          fetchSnapshot({ force: true });
          return;
        }
        if (state.sectorFlowMode[scope] !== "five_day") {
          renderSectorFlowUnavailable(
            `精确轨迹读取失败：${text(error.message, "等待自动重试")}`,
            scope,
          );
          return;
        }
      }
      renderSectorFlowTrajectory(state.lastSnapshot, scope, {
        renderPicker: request.renderPicker,
      });
      renderSectorMoveRadar(state.lastSnapshot);
    });
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

  function alertKey(alert) {
    return text(alert && alert.dedupe_key, text(alert && alert.code, "alert"));
  }

  function alertListFromSnapshot(snapshot) {
    return asArray(snapshot.alerts)
      .filter((item) => item && typeof item === "object")
      .filter((item) => (
        item.kind !== "sector_move"
        || Math.abs(finiteNumber(item.change_delta_5m_pct) || 0)
          >= state.sectorFlowSurgeThreshold
      ))
      .slice(0, 12);
  }

  function replayPayload(item) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return null;
    const payload = item.payload && typeof item.payload === "object" ? item.payload : item;
    return payload && (
      payload.contract === "market_watch.v1"
      || payload.contract === "market_watch_replay_sample.v1"
    ) ? payload : null;
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
    const visible = samples.slice(-29).reverse();
    if (samples.length > 29) visible.push(samples[0]);
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
      const marketState = snapshot.market_state && typeof snapshot.market_state === "object"
        ? snapshot.market_state
        : {};
      const isAuctionResult = text(marketState.phase, "") === "pre_open";
      const row = createElement("article", "replay-timeline-item");
      row.append(
        createElement("time", "", formatTimestamp(snapshot.as_of)),
        createElement(
          "strong",
          "",
          isAuctionResult
            ? "集合竞价结果"
            : REGIME_LABELS[text(guardrail.regime, "uncertain").toLowerCase()] || "方向未确认",
        ),
        createElement(
          "span",
          "",
          isAuctionResult
            ? "09:25竞价盘面已验收；板块净流入从09:30起算"
            : text(guardrail.current_state, "该分钟仅保留回放状态元数据"),
        ),
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

  function renderReplayEvaluationPending(message = "评估后台准备中") {
    byId("replay-observed-minutes").textContent = "--";
    byId("replay-coverage-detail").textContent = "-- / -- 分钟";
    byId("replay-fresh-ratio").textContent = "--";
    byId("replay-max-gap").textContent = "--";
    byId("replay-transition-count").textContent = "--";
    byId("replay-reversal-count").textContent = "--";
    const badge = byId("evaluation-verdict");
    badge.className = "evaluation-badge evaluation-insufficient";
    badge.textContent = "后台准备中";
    byId("evaluation-detail").textContent = message;
    const list = byId("calibration-notes");
    list.replaceChildren(createElement("li", "", "评估结果生成后会自动刷新，不影响分钟回放浏览。"));
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
      const historyResponse = await fetch(`${HISTORY_ENDPOINT}${historyQuery}`, {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!historyResponse.ok) throw new Error(`回放服务返回 ${historyResponse.status}`);
      const history = await historyResponse.json();
      if (state.replayPendingRequest) return;
      renderReplayHistory(history);
      state.replayLastFetchedAt = Date.now();
      const recording = history.recording && typeof history.recording === "object" ? history.recording : {};
      const historyLabel = recording.status === "degraded"
        ? `${text(history.trade_date, "--")} · 记录降级`
        : `${text(history.trade_date, "--")} · 回放已更新`;
      byId("replay-status").textContent = `${historyLabel} · 正在读取评估…`;
      renderReplayEvaluationPending("评估后台准备中；分钟回放已可浏览。");

      try {
        const evaluationResponse = await fetch(`${EVALUATION_ENDPOINT}${evaluationQuery}`, {
          cache: "no-store",
          headers: { Accept: "application/json" },
        });
        let evaluation = {};
        try {
          evaluation = await evaluationResponse.json();
        } catch (_error) {
          evaluation = {};
        }
        if (state.replayPendingRequest) return;
        if (evaluationResponse.status === 503) {
          renderReplayEvaluationPending(text(
            evaluation.error,
            "评估后台准备中；分钟回放已可浏览。",
          ));
          byId("replay-status").textContent = `${historyLabel} · 评估后台准备中`;
          return;
        }
        if (!evaluationResponse.ok) {
          throw new Error(text(evaluation.error, `服务返回 ${evaluationResponse.status}`));
        }
        renderReplayEvaluation(evaluation);
        const actualDays = finiteNumber(evaluation.session_count);
        const evaluationLabel = requestedDays
          ? ` · 实得 ${actualDays === null ? 0 : Math.round(actualDays)}/${requestedDays} 日验收`
          : "";
        byId("replay-status").textContent = `${historyLabel}${evaluationLabel}`;
      } catch (evaluationError) {
        if (state.replayPendingRequest) return;
        renderReplayEvaluationPending(
          `评估暂不可用：${text(evaluationError.message, "等待后台重试")}；分钟回放不受影响。`,
        );
        byId("replay-status").textContent = `${historyLabel} · 评估暂不可用`;
      }
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
    renderSectorMoveRadar(snapshot);
    renderSectorFlowTrajectories(snapshot);
    renderIndexDock(snapshot);
    renderBreadth(snapshot);
    renderTurnover(snapshot);
    renderRotationPulse(snapshot);
    state.alerts = alertListFromSnapshot(snapshot);
    updateDataRisk(snapshot);
    if (state.lastSummary?.source_snapshot_revision !== state.limitUpPoolRevision) {
      if (!state.limitUpPool) byId("limit-up-pool-button-count").textContent = "…";
      fetchLimitUpPool({ showPending: byId("limit-up-pool-dialog").open });
    }
  }

  function alertEligibility(snapshot) {
    const freshness = normalizeFreshness(snapshot);
    const marketState = normalizeMarketState(snapshot);
    if (!marketState.isOpen) return { allowed: false, reason: "当前非交易时段，不发送盘中变化提醒" };
    if (state.muted) return { allowed: false, reason: "提醒已手动静音" };
    if (freshness.status === "fresh") {
      return { allowed: true, reason: "仅发送后端确认的变化" };
    }
    if (sectorMoveRadarAvailability(snapshot).available) {
      return { allowed: true, reason: "板块轨迹可用，异动提醒独立运行" };
    }
    return { allowed: false, reason: "板块轨迹非新鲜，异动提醒已锁定" };
  }

  function isSectorMoveAlert(alert) {
    return alert && alert.kind === "sector_move";
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
    if (isSectorMoveAlert(alert)) {
      return sectorMoveRadarAvailability(snapshot).available;
    }
    return freshness.status === "fresh";
  }

  function updateDataRisk(snapshot) {
    if (state.fetchFailed) {
      renderFetchRisk(state.lastFetchError);
      return;
    }
    const freshness = normalizeFreshness(snapshot);
    const marketState = normalizeMarketState(snapshot);
    const overlay = byId("data-risk-overlay");
    const eligibility = alertEligibility(snapshot);
    const blocksCurrentJudgment = marketState.isOpen && new Set([
      "stale", "unavailable", "unknown",
    ]).has(freshness.status);
    if (!blocksCurrentJudgment) {
      overlay.classList.remove("is-visible", "is-warning");
    } else {
      overlay.classList.add("is-visible");
      overlay.classList.toggle("is-warning", freshness.status === "stale");
      const title = freshness.status === "stale"
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
  }

  function renderFetchRisk(error) {
    keepMarketWatchSurfacesVisible();
    if (!state.lastSnapshot) {
      renderMarketWatchUnavailableShell("实时盘面暂不可用；图表区域保留，等待下一次完整快照。");
    }
    const overlay = byId("data-risk-overlay");
    overlay.classList.add("is-visible");
    overlay.classList.remove("is-warning");
    byId("data-risk-title").textContent = "实时请求失败";
    byId("data-risk-detail").textContent = state.lastSnapshot
      ? `最后画面已保留；${text(error && error.message, "等待自动重试")}。`
      : `尚无可展示快照；${text(error && error.message, "等待自动重试")}。`;
    byId("alert-lock-label").textContent = "盘面变化提醒已锁定";
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

  function showSectorMoveNotification(sectorAlerts) {
    if (!sectorAlerts.length) return;
    const top = sectorAlerts.slice(0, 3);
    const title = sectorAlerts.length === 1
      ? text(top[0].title, "板块出现异动")
      : `${sectorAlerts.length} 个板块出现异动`;
    const message = top.map((alert) => {
      const delta = finiteNumber(alert.change_delta_5m_pct);
      return `${text(alert.sector_label, "板块")} ${delta === null ? "" : formatChangePct(delta)}`.trim();
    }).join(" · ");
    showSystemNotification({
      title,
      message: sectorAlerts.length > 3 ? `${message} 等` : message,
      dedupe_key: `market_watch:sector_move_batch:${top.map(alertKey).join("|")}`,
    });
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
    const pendingAlerts = state.alerts.filter((alert) => (
      alertCanDeliver(snapshot, alert) && !wasDeliveredRecently(alert, now)
    ));
    const sectorAlerts = pendingAlerts.filter((alert) => alert.kind === "sector_move");
    if (sectorAlerts.length) {
      sectorAlerts.forEach((alert) => {
        state.deliveredAlertKeys[alertKey(alert)] = now;
      });
      state.lastAlertAt = now;
      storeText(STORAGE_KEYS.lastAlertAt, String(now));
      pruneDeliveredKeys(now);
      showSectorMoveNotification(sectorAlerts);
      playAlertTone();
      return;
    }
    if (now - state.lastAlertAt < ALERT_COOLDOWN_MS) return;

    const pending = pendingAlerts[0];
    if (!pending) return;
    const key = alertKey(pending);
    state.deliveredAlertKeys[key] = now;
    state.lastAlertAt = now;
    storeText(STORAGE_KEYS.lastAlertAt, String(now));
    pruneDeliveredKeys(now);
    showSystemNotification(pending);
    playAlertTone();
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

  function validateStockSelectionHistory(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("返回内容不是每日选股档案");
    }
    if (payload.contract !== "stock_selection_strategy_archive.v1" || payload.schema_version !== 1) {
      throw new Error(`选股档案契约不匹配：${text(payload.contract, "缺少契约")}`);
    }
    return payload;
  }

  function validateStockSelectionResult(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("返回内容不是每日选股生成结果");
    }
    if (payload.contract !== "daily_stock_selection_result.v1" || payload.schema_version !== 1) {
      throw new Error(`选股生成契约不匹配：${text(payload.contract, "缺少契约")}`);
    }
    return payload;
  }

  function validateStockSelectionGeneration(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("返回内容不是每日选股任务状态");
    }
    if (
      payload.contract !== "daily_stock_selection_generation.v1"
      || payload.schema_version !== 1
    ) {
      throw new Error(`选股任务契约不匹配：${text(payload.contract, "缺少契约")}`);
    }
    if (!["idle", "queued", "running", "succeeded", "failed"].includes(payload.state)) {
      throw new Error("选股任务返回了未知状态");
    }
    return payload;
  }

  function renderStockSelectionDateOptions(history) {
    const select = byId("stock-selection-date-select");
    const dates = asArray(history.dates)
      .map((item) => text(objectValue(item).trade_date, text(item, "")))
      .filter(Boolean);
    const selected = state.stockSelectionTradeDate;
    select.replaceChildren();
    if (!dates.length) {
      select.append(createElement("option", "", "尚无存档"));
      select.disabled = true;
      state.stockSelectionTradeDate = null;
      return;
    }
    const latest = createElement("option", "", `最新存档（${dates[0]}）`);
    latest.value = "";
    latest.selected = !selected;
    select.append(latest);
    dates.forEach((tradeDate) => {
      const option = createElement("option", "", tradeDate);
      option.value = tradeDate;
      option.selected = tradeDate === selected;
      select.append(option);
    });
    select.disabled = false;
  }

  function renderStockSelectionExclusions(
    excludedCounts,
    targetId = "stock-selection-exclusions",
  ) {
    const labels = {
      special_treatment: "ST / 退市风险",
      missing_listing_date: "缺少上市日期",
      recent_listing: "上市不足 120 日",
      delisted: "已退市",
      low_price: "低价过滤",
      low_liquidity: "成交额不足",
      missing_market_cap: "缺少市值",
      small_market_cap: "市值不足",
      insufficient_factor_coverage: "因子覆盖不足",
      not_main_board: "非主板",
      incomplete_candlestick_window: "规则窗口 K 线证据不完整",
      recent_limit_up: "近 10 日出现收盘涨停",
      insufficient_occurrences: "长上影少于 2 次",
      missing_float_market_cap: "缺少流通市值",
      small_float_market_cap: "流通市值不足 20 亿元",
      missing_activity_metrics: "缺少换手率或量比",
      non_positive_session: "信号日未上涨",
      missing_volume_window: "12 日成交量证据不完整",
      limit_up_in_7_sessions: "近 7 日出现收盘涨停",
      no_upward_volume_surge: "近 7 日未出现上涨且放量 ≥ 2 倍",
      next_day_volume_above_66pct: "放量次日成交量超过放量日的 66%",
      pending_next_day_confirmation: "当日放量尚待下一交易日缩量确认",
      technical_window_unavailable: "MACD / KDJ 历史指标未取得",
      incomplete_indicator_window: "6 个交易日指标证据不完整",
      inactive_session: "窗口内有停牌或无成交日",
      indicator_price_mismatch: "指标源与日线收盘价不一致",
      no_fresh_macd_cross: "当天未新出现金叉",
      j_not_rising: "金叉当天 J 未上行",
      no_recent_j_turn: "当天及此前 3 个交易日未见 J 拐头",
      close_below_anchor_low: "收盘跌破本轮起点最低价，尚未重新有效命中",
      weak_close: "收盘位置低于日内振幅 55%",
    };
    const target = byId(targetId);
    target.replaceChildren();
    const entries = Object.entries(objectValue(excludedCounts))
      .filter(([, value]) => finiteNumber(value) !== null)
      .sort((left, right) => Number(right[1]) - Number(left[1]));
    entries.forEach(([key, value]) => {
      const wrapper = createElement("div");
      wrapper.append(
        createElement("dt", "", labels[key] || key),
        createElement("dd", "", formatCount(value)),
      );
      target.append(wrapper);
    });
  }

  function stockSelectionNameLink(candidate) {
    const label = createElement("strong", "", text(candidate.name));
    const match = /^(\d{6})\.(SH|SZ|BJ)$/.exec(String(candidate.instrument_id || ""));
    if (!match) return label;
    const link = createElement("a", "stock-selection-quote-link");
    link.href = `https://stockpage.10jqka.com.cn/${match[1]}/`;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.title = "查看同花顺行情（新标签页）";
    link.setAttribute("aria-label", `${text(candidate.name)} ${match[1]}，查看同花顺行情（新标签页）`);
    link.append(label);
    return link;
  }

  function stockSelectionIndustryName(candidate) {
    return typeof candidate.industry_block_name === "string" && candidate.industry_block_name.trim()
      ? candidate.industry_block_name.trim() : "未分类";
  }

  function stockSelectionIndustryPayload(payload, display) {
    const mapping = display.contract === "selection_industry_display.v1"
      && display.schema_version === 1 && display.basis === "current_ths_industry"
      ? objectValue(display.names_by_instrument) : {};
    return {
      ...payload,
      candidates: asArray(payload.candidates).map((candidate) => ({
        ...candidate, industry_block_name: mapping[candidate.instrument_id] || null,
      })),
    };
  }

  function stockSelectionDisplayCandidates(candidates, hitCount = null) {
    const industryOrder = new Intl.Collator("zh-CN", { numeric: true });
    return asArray(candidates).map(objectValue).filter((item) => Object.keys(item).length)
      .sort((left, right) => {
        const leftIndustry = stockSelectionIndustryName(left);
        const rightIndustry = stockSelectionIndustryName(right);
        const industry = Number(leftIndustry === "未分类") - Number(rightIndustry === "未分类")
          || industryOrder.compare(leftIndustry, rightIndustry);
        if (industry || !hitCount) return industry;
        return (finiteNumber(hitCount(right)) ?? -1) - (finiteNumber(hitCount(left)) ?? -1);
      });
  }

  function stockSelectionVisibleCandidates(candidates, hitCount = null) {
    return stockSelectionDisplayCandidates(candidates, hitCount).filter((candidate) => (
      state.stockSelectionIndustry === null
      || stockSelectionIndustryName(candidate) === state.stockSelectionIndustry
    ) && (!state.stockSelectionChangeFilter || state.stockSelectionChangeFilter === "all"
      || objectValue(objectValue(objectValue(state.stockSelectionComparison).rows)[candidate.instrument_id]).status
        === state.stockSelectionChangeFilter));
  }

  function renderStockSelectionIndustryFilters(candidates, scope) {
    const records = stockSelectionDisplayCandidates(candidates);
    const counts = new Map();
    records.forEach((candidate) => {
      const industry = stockSelectionIndustryName(candidate);
      counts.set(industry, (counts.get(industry) || 0) + 1);
    });
    if (state.stockSelectionIndustryScope !== scope || !counts.has(state.stockSelectionIndustry)) {
      state.stockSelectionIndustry = null;
    }
    state.stockSelectionIndustryScope = scope;
    byId("stock-selection-industry-filter").hidden = records.length === 0;
    const tags = byId("stock-selection-industry-tags");
    tags.replaceChildren();
    [[null, records.length], ...counts].forEach(([industry, count]) => {
      const button = createElement("button", "stock-selection-industry-tag",
        `${industry === null ? "全部" : industry} ${formatCount(count)}`);
      button.type = "button";
      button.setAttribute("aria-pressed", String(state.stockSelectionIndustry === industry));
      button.addEventListener("click", () => {
        state.stockSelectionIndustry = industry;
        renderStockSelectionStrategy(state.stockSelectionHistory, state.stockSelectionStrategyId);
        tags.querySelector('button[aria-pressed="true"]')?.focus();
      });
      tags.append(button);
    });
    const selected = state.stockSelectionIndustry;
    const visibleCount = selected === null ? records.length : counts.get(selected);
    byId("stock-selection-industry-summary").textContent =
      `${selected === null ? "全部行业" : selected} · 显示 ${formatCount(visibleCount)} / ${formatCount(records.length)} 只`;
  }

  function renderStockPatternCandidates(candidates) {
    const target = byId("stock-pattern-table-body");
    target.replaceChildren();
    const records = stockSelectionVisibleCandidates(candidates, (item) => item.occurrence_count);
    if (!records.length) {
      const row = createElement("tr");
      const cell = createElement("td", "stock-selection-table-empty", "该交易日没有股票命中完整规则。");
      cell.colSpan = 6;
      row.append(cell);
      target.append(row);
      return;
    }
    records.forEach((candidate) => {
      const row = createElement("tr");
      const nameCell = createElement("td");
      const name = createElement("div", "stock-selection-name");
      name.append(
        stockSelectionNameLink(candidate),
        createElement("small", "", text(candidate.instrument_id)),
      );
      nameCell.append(name);
      row.append(nameCell);
      row.append(createElement("td", "", stockSelectionIndustryName(candidate)));
      row.append(createElement("td", "stock-selection-score", formatCount(candidate.occurrence_count)));
      row.append(createElement("td", "", text(candidate.latest_occurrence_date, "--")));
      row.append(createElement("td", "", formatLevel(candidate.reference_close)));
      const evidenceCell = createElement("td");
      const evidenceList = createElement("div", "stock-pattern-evidence");
      asArray(candidate.evidence).map(objectValue).forEach((evidence) => {
        const pct = finiteNumber(evidence.upper_shadow_pct_of_close);
        const body = finiteNumber(evidence.upper_shadow_body_multiple);
        const range = finiteNumber(evidence.upper_shadow_range_ratio);
        evidenceList.append(createElement(
          "span",
          "",
          `${text(evidence.trade_date, "--")} · 上影/收盘 ${pct === null ? "--" : `${pct.toFixed(1)}%`}`
            + ` · 实体 ${body === null ? "十字" : `${body.toFixed(1)}×`}`
            + ` · 振幅占比 ${range === null ? "--" : `${(range * 100).toFixed(0)}%`}`,
        ));
      });
      evidenceCell.append(evidenceList);
      row.append(evidenceCell);
      target.append(row);
    });
  }

  function renderStockPatternScreen(patternScreens) {
    const screen = asArray(patternScreens)
      .map(objectValue)
      .find((item) => item.contract === "stock_pattern_screen.v1");
    const empty = byId("stock-pattern-empty");
    const content = byId("stock-pattern-content");
    if (!screen || screen.contract !== "stock_pattern_screen.v1" || screen.schema_version !== 1) {
      empty.hidden = false;
      content.hidden = true;
      return;
    }
    empty.hidden = true;
    content.hidden = false;
    const eligible = finiteNumber(screen.board_eligible_count);
    const evaluated = finiteNumber(screen.evaluated_count);
    const coverage = eligible && evaluated !== null ? evaluated / eligible : null;
    const qualityLabels = { accepted: "完备", degraded: "降级", unavailable: "不可用" };
    byId("stock-pattern-eligible-count").textContent = formatCount(eligible);
    byId("stock-pattern-evaluated-count").textContent = formatCount(evaluated);
    byId("stock-pattern-matched-count").textContent = formatCount(screen.matched_count);
    byId("stock-pattern-quality").textContent = qualityLabels[screen.quality] || "未知";
    byId("stock-pattern-coverage").textContent = coverage === null
      ? "覆盖 --"
      : `覆盖 ${(coverage * 100).toFixed(1)}%`;
    renderStockPatternCandidates(screen.candidates);
    replaceTextList("stock-pattern-methodology", screen.methodology, "方法尚未随档案返回。");
    replaceTextList("stock-pattern-limitations", screen.limitations, "长上影线只是一种形态代理。");
    renderStockSelectionExclusions(screen.excluded_counts, "stock-pattern-exclusions");
  }

  function renderLimitUpTendencyCandidates(candidates) {
    const target = byId("stock-limit-up-tendency-table-body");
    target.replaceChildren();
    const records = stockSelectionVisibleCandidates(candidates);
    if (!records.length) {
      const row = createElement("tr");
      const cell = createElement("td", "stock-selection-table-empty", "该交易日没有股票进入倾向前 20。 ");
      cell.colSpan = 9;
      row.append(cell);
      target.append(row);
      return;
    }
    records.forEach((candidate) => {
      const row = createElement("tr");
      row.append(createElement("td", "stock-selection-rank", text(candidate.rank)));
      const nameCell = createElement("td");
      const name = createElement("div", "stock-selection-name");
      name.append(
        stockSelectionNameLink(candidate),
        createElement(
          "small",
          "",
          `${text(candidate.instrument_id)} · ${candidate.opportunity_stage === "limit_up_continuation" ? "已涨停延续观察" : "未涨停启动机会"}`,
        ),
      );
      nameCell.append(name);
      row.append(nameCell);
      row.append(createElement("td", "", stockSelectionIndustryName(candidate)));
      const score = finiteNumber(candidate.score);
      row.append(createElement("td", "stock-selection-score", score === null ? "--" : score.toFixed(1)));
      row.append(createElement(
        "td",
        "",
        `${formatChangePct(candidate.daily_return_pct)} / ${formatChangePct(candidate.five_day_return_pct)}`,
      ));
      const turnover = finiteNumber(candidate.turnover_rate_pct);
      const volumeRatio = finiteNumber(candidate.volume_ratio);
      row.append(createElement(
        "td",
        "",
        `${turnover === null ? "--" : `${turnover.toFixed(1)}%`} / ${volumeRatio === null ? "--" : volumeRatio.toFixed(2)}`,
      ));
      const amountExpansion = finiteNumber(candidate.amount_expansion_ratio);
      row.append(createElement("td", "", amountExpansion === null ? "--" : `${amountExpansion.toFixed(2)}×`));
      row.append(createElement("td", "", formatCny(candidate.float_market_cap_cny)));
      const evidenceCell = createElement("td");
      const evidence = createElement("div", "stock-selection-evidence");
      const reasons = stringList(candidate.reasons).slice(0, 4);
      const risks = stringList(candidate.risks).slice(0, 3);
      evidence.append(
        createElement("span", "", reasons.join("；") || "综合技术强度进入前 20"),
        createElement("small", "", `风险：${risks.join("；") || "需继续核查个股风险"}`),
      );
      evidenceCell.append(evidence);
      row.append(evidenceCell);
      target.append(row);
    });
  }

  function renderLimitUpTendencyScreen(tendencyScreens) {
    const screen = asArray(tendencyScreens)
      .map(objectValue)
      .find((item) => item.contract === "stock_limit_up_tendency_screen.v1");
    const empty = byId("stock-limit-up-tendency-empty");
    const content = byId("stock-limit-up-tendency-content");
    if (!screen || screen.contract !== "stock_limit_up_tendency_screen.v1" || screen.schema_version !== 1) {
      empty.hidden = false;
      content.hidden = true;
      return;
    }
    empty.hidden = true;
    content.hidden = false;
    const eligible = finiteNumber(screen.board_eligible_count);
    const evaluated = finiteNumber(screen.evaluated_count);
    const coverage = eligible && evaluated !== null ? evaluated / eligible : null;
    const qualityLabels = { accepted: "完备", degraded: "降级", unavailable: "不可用" };
    byId("stock-limit-up-tendency-eligible-count").textContent = formatCount(eligible);
    byId("stock-limit-up-tendency-evaluated-count").textContent = formatCount(evaluated);
    byId("stock-limit-up-tendency-selected-count").textContent = formatCount(screen.selected_count);
    byId("stock-limit-up-tendency-quality").textContent = qualityLabels[screen.quality] || "未知";
    byId("stock-limit-up-tendency-coverage").textContent = coverage === null
      ? "覆盖 --"
      : `覆盖 ${(coverage * 100).toFixed(1)}%`;
    renderLimitUpTendencyCandidates(screen.candidates);
    replaceTextList(
      "stock-limit-up-tendency-methodology",
      screen.methodology,
      "方法尚未随档案返回。",
    );
    replaceTextList(
      "stock-limit-up-tendency-limitations",
      screen.limitations,
      "分数只用于相对排序。",
    );
    renderStockSelectionExclusions(
      screen.excluded_counts,
      "stock-limit-up-tendency-exclusions",
    );
  }

  function renderLimitUpTendencyOutcome(outcome) {
    const canonical = objectValue(outcome);
    const status = byId("stock-limit-up-tendency-outcome-status");
    const detail = byId("stock-limit-up-tendency-outcome-detail");
    status.classList.remove("tone-positive", "tone-negative", "tone-flat");
    if (!Object.keys(canonical).length) {
      status.textContent = "尚未验证";
      status.classList.add("tone-flat");
      detail.textContent = "结果会分别统计下一有效交易日的盘中触板率与收盘封板率。";
      return;
    }
    const coverage = finiteNumber(canonical.coverage);
    if (canonical.evaluation_status !== "evaluated") {
      status.textContent = `${text(canonical.evaluation_trade_date, "--")} · 覆盖不足`;
      status.classList.add("tone-flat");
      detail.textContent = `可验证覆盖 ${coverage === null ? "--" : `${(coverage * 100).toFixed(0)}%`}，不输出触板率。`;
      return;
    }
    const touchedRate = finiteNumber(canonical.touched_limit_up_rate);
    const closedRate = finiteNumber(canonical.closed_limit_up_rate);
    status.textContent = `${text(canonical.evaluation_trade_date, "--")} · 已验证`;
    status.classList.add("tone-flat");
    detail.textContent = (
      `盘中触板 ${formatCount(canonical.touched_limit_up_count)} / ${formatCount(canonical.evaluated_count)}`
      + `（${touchedRate === null ? "--" : `${(touchedRate * 100).toFixed(1)}%`}）；`
      + `收盘封板 ${formatCount(canonical.closed_limit_up_count)} / ${formatCount(canonical.evaluated_count)}`
      + `（${closedRate === null ? "--" : `${(closedRate * 100).toFixed(1)}%`}）。`
    );
  }

  function renderStockSelectionCandidates(candidates) {
    const target = byId("stock-selection-table-body");
    target.replaceChildren();
    const records = stockSelectionVisibleCandidates(candidates);
    if (!records.length) {
      const row = createElement("tr");
      const cell = createElement("td", "stock-selection-table-empty", "该交易日没有可展示候选。");
      cell.colSpan = 9;
      row.append(cell);
      target.append(row);
      return;
    }
    records.forEach((candidate) => {
      const row = createElement("tr");
      row.append(createElement("td", "stock-selection-rank", text(candidate.rank)));
      const nameCell = createElement("td");
      const name = createElement("div", "stock-selection-name");
      name.append(
        stockSelectionNameLink(candidate),
        createElement("small", "", text(candidate.instrument_id)),
      );
      nameCell.append(name);
      row.append(nameCell);
      row.append(createElement("td", "", stockSelectionIndustryName(candidate)));
      const score = finiteNumber(candidate.score);
      row.append(createElement("td", "stock-selection-score", score === null ? "--" : score.toFixed(1)));
      const coverage = finiteNumber(candidate.factor_coverage);
      row.append(createElement("td", "", coverage === null ? "--" : `${(coverage * 100).toFixed(0)}%`));
      row.append(createElement("td", "", formatLevel(candidate.reference_close)));
      row.append(createElement("td", "", formatCny(candidate.amount_cny)));
      row.append(createElement("td", "", formatCny(candidate.total_market_cap_cny)));
      const evidenceCell = createElement("td");
      const evidence = createElement("div", "stock-selection-evidence");
      const reason = stringList(candidate.reasons)[0] || "综合因子进入候选池";
      const risk = stringList(candidate.risks)[0] || "需继续核查个股风险";
      evidence.append(createElement("span", "", reason), createElement("small", "", `风险：${risk}`));
      evidenceCell.append(evidence);
      row.append(evidenceCell);
      target.append(row);
    });
  }

  function renderStockSelectionOutcome(history) {
    const direct = objectValue(history.outcome);
    const latest = Object.keys(direct).length
      ? direct
      : objectValue(asArray(history.recent_outcomes)[0]);
    const status = byId("stock-selection-outcome-status");
    const detail = byId("stock-selection-outcome-detail");
    status.classList.remove("tone-positive", "tone-negative", "tone-flat");
    if (!Object.keys(latest).length) {
      status.textContent = "尚未验证";
      status.classList.add("tone-flat");
      detail.textContent = "新候选池会从下一交易日开盘成交假设开始验证，并计入双边成本。";
      return;
    }
    const labels = {
      supported: "跑赢基准",
      not_supported: "未跑赢基准",
      mixed: "接近基准",
      unverifiable: "覆盖不足",
    };
    const excess = finiteNumber(latest.excess_return_pct);
    status.textContent = `${text(latest.evaluation_trade_date)} · ${labels[latest.verdict] || text(latest.verdict)}`;
    status.classList.add(excess === null || excess === 0 ? "tone-flat" : excess > 0 ? "tone-positive" : "tone-negative");
    detail.textContent = excess === null
      ? `可验证覆盖 ${(Number(latest.coverage || 0) * 100).toFixed(0)}%，未达到收益判断门槛。`
      : `候选池 ${formatChangePct(latest.portfolio_return_pct)}，沪深300 ${formatChangePct(latest.benchmark_return_pct)}，超额 ${formatChangePct(excess)}。`;
  }

  function renderStockSelection(selection, history = {}) {
    const canonical = objectValue(selection);
    byId("stock-selection-empty").hidden = true;
    byId("stock-selection-content").hidden = false;
    byId("stock-selection-universe-count").textContent = formatCount(canonical.universe_count);
    byId("stock-selection-eligible-count").textContent = formatCount(canonical.eligible_count);
    byId("stock-selection-selected-count").textContent = formatCount(canonical.selected_count);
    byId("stock-selection-quality").textContent = canonical.source_quality === "accepted" ? "完备" : "降级";
    byId("stock-selection-as-of").textContent = `证据时间 ${formatTimestamp(canonical.source_provider_as_of || canonical.generated_at, true)}`;
    byId("stock-selection-trade-date").textContent = text(canonical.trade_date);
    renderStockSelectionCandidates(canonical.candidates);
    replaceTextList(
      "stock-selection-methodology",
      canonical.methodology,
      "方法尚未随档案返回。",
    );
    replaceTextList(
      "stock-selection-limitations",
      canonical.limitations,
      "候选池不是投资建议。",
    );
    renderStockSelectionExclusions(canonical.excluded_counts);
    renderStockSelectionOutcome(history);
  }

  function stockSelectionDefinitions(archive) {
    return asArray(objectValue(archive.catalog).strategies)
      .map(objectValue)
      .filter((item) => item.strategy_id && item.result_contract);
  }

  function stockSelectionPanelForContract(resultContract) {
    return [...document.querySelectorAll("[data-stock-selection-result-contract]")]
      .find((panel) => panel.dataset.stockSelectionResultContract === resultContract) || null;
  }

  function handleStockSelectionTabKeydown(event, button) {
    if (!new Set(["ArrowLeft", "ArrowRight", "Home", "End"]).has(event.key)) return;
    event.preventDefault();
    const tabs = [...document.querySelectorAll("[data-stock-selection-tab]")];
    const current = tabs.indexOf(button);
    const target = event.key === "Home"
      ? 0
      : event.key === "End"
        ? tabs.length - 1
        : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    if (tabs[target]) selectStockSelectionTab(tabs[target].dataset.stockSelectionTab, { focus: true });
  }

  function renderStockSelectionStrategyTabs(archive) {
    const target = byId("stock-selection-tabs");
    const definitions = stockSelectionDefinitions(archive)
      .filter((definition) => stockSelectionPanelForContract(definition.result_contract));
    target.replaceChildren();
    definitions.forEach((definition, index) => {
      const panel = stockSelectionPanelForContract(definition.result_contract);
      const button = createElement("button", "", text(definition.title, definition.strategy_id));
      button.id = `stock-selection-tab-${definition.strategy_id}`;
      button.type = "button";
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", "false");
      button.setAttribute("aria-controls", panel.id);
      button.dataset.stockSelectionTab = definition.strategy_id;
      button.tabIndex = index === 0 ? 0 : -1;
      button.addEventListener("click", () => selectStockSelectionTab(definition.strategy_id));
      button.addEventListener("keydown", (event) => handleStockSelectionTabKeydown(event, button));
      target.append(button);
    });
    const availableIds = definitions.map((item) => item.strategy_id);
    if (!availableIds.includes(state.stockSelectionStrategyId)) {
      state.stockSelectionStrategyId = availableIds[0] || null;
    }
  }

  function legacyStockSelectionPayload(archive, definition) {
    const legacy = objectValue(archive.legacy_selection);
    if (!Object.keys(legacy).length) return {};
    if (definition.result_contract === "balanced_stock_selection_result.v1") return legacy;
    if (definition.result_contract === "stock_pattern_screen.v1") {
      return objectValue(
        asArray(legacy.pattern_screens)
          .map(objectValue)
          .find((item) => item.contract === definition.result_contract),
      );
    }
    if (definition.result_contract === "stock_limit_up_tendency_screen.v1") {
      return objectValue(
        asArray(legacy.limit_up_tendency_screens)
          .map(objectValue)
          .find((item) => item.contract === definition.result_contract),
      );
    }
    return {};
  }

  function renderVolumeSurgeScreen(payload) {
    const valid = payload.contract === "stock_volume_surge_screen.v1" && payload.schema_version === 1;
    byId("stock-volume-surge-empty").hidden = valid;
    byId("stock-volume-surge-content").hidden = !valid;
    const body = byId("stock-volume-surge-table-body");
    body.replaceChildren();
    if (!valid) return;
    const eligible = Number(payload.board_eligible_count);
    const evaluated = Number(payload.evaluated_count);
    const quality = { accepted: "完备", degraded: "降级", unavailable: "不可用" };
    byId("stock-volume-surge-summary").textContent =
      `主板非 ST ${formatCount(eligible)} 只 · 完整证据 ${formatCount(evaluated)} 只`
      + ` · 覆盖 ${eligible ? (evaluated / eligible * 100).toFixed(1) : "0.0"}%`
      + ` · 命中 ${formatCount(payload.matched_count)} 只 · ${quality[payload.quality] || "未知"}`
      + (payload.screen_version === "upward-volume-surge-main-board.v3"
        ? " · 已确认次日缩量 ≤ 66%及收盘底线"
        : payload.screen_version === "upward-volume-surge-main-board.v2"
          ? " · 旧版规则，仅检查收盘底线" : " · 旧版规则，未检查收盘底线");
    const candidates = stockSelectionVisibleCandidates(payload.candidates, (item) => asArray(item.evidence).length);
    if (!candidates.length) {
      const row = createElement("tr");
      const cell = createElement("td", "stock-selection-table-empty",
        payload.quality === "unavailable" ? "证据不足，无法完成筛选。" : "完整证据范围内，没有股票命中规则。");
      cell.colSpan = 5;
      row.append(cell);
      body.append(row);
    }
    candidates.map(objectValue).forEach((candidate) => {
      const row = createElement("tr");
      const evidence = asArray(candidate.evidence).map(objectValue);
      const nameCell = createElement("td");
      nameCell.append(
        stockSelectionNameLink(candidate),
        createElement("span", "", ` · ${text(candidate.instrument_id)}`),
      );
      row.append(
        nameCell,
        createElement("td", "", stockSelectionIndustryName(candidate)),
        createElement("td", "", formatLevel(candidate.reference_close)),
        createElement("td", "", formatCount(evidence.length)),
      );
      const cell = createElement("td");
      const list = createElement("div", "stock-pattern-evidence");
      if (candidate.anchor_trade_date && finiteNumber(candidate.anchor_low) !== null) {
        list.append(createElement("strong", "",
          `本轮起点 ${candidate.anchor_trade_date} · 收盘底线 ${formatLevel(candidate.anchor_low)}`
          + (finiteNumber(candidate.minimum_subsequent_close) === null
            ? " · 当日新命中，暂无后续交易日"
            : ` · 后续最低收盘 ${formatLevel(candidate.minimum_subsequent_close)}`)
          + (Number(candidate.reset_count) > 0 ? ` · 已重启 ${candidate.reset_count} 次` : ""),
        ));
      }
      evidence.forEach((event) => list.append(createElement("span", "",
        `${text(event.trade_date)} · 涨幅 ${Number(event.change_pct).toFixed(2)}%`
        + ` · 放量 ${Number(event.volume_multiple).toFixed(2)} 倍`
        + ` · 成交量 ${formatCount(event.volume_shares)} 股`
        + ` / 前 5 日均量 ${formatCount(event.prior_5d_average_volume_shares)} 股`
        + (event.next_trade_date && finiteNumber(event.next_volume_ratio) !== null
          ? ` · 次日 ${event.next_trade_date} 成交量 ${formatCount(event.next_volume_shares)} 股`
            + `（放量日的 ${(Number(event.next_volume_ratio) * 100).toFixed(2)}%）` : ""),
      )));
      cell.append(list);
      row.append(cell);
      body.append(row);
    });
    const methodology = asArray(payload.methodology).map((line) => (
      typeof line === "string" && line.startsWith("展示全部命中股票，按")
        ? "展示全部命中股票；行业优先，同一行业按命中次数降序，同值保留档案原顺序。" : line
    ));
    replaceTextList("stock-volume-surge-methodology", methodology, "暂无方法说明。");
    replaceTextList("stock-volume-surge-limitations", payload.limitations, "暂无证据说明。");
    renderStockSelectionExclusions(payload.excluded_counts, "stock-volume-surge-exclusions");
  }

  function renderMacdJScreen(payload) {
    const valid = payload.contract === "stock_macd_j_screen.v1" && payload.schema_version === 1;
    byId("stock-macd-j-empty").hidden = valid;
    byId("stock-macd-j-content").hidden = !valid;
    const body = byId("stock-macd-j-table-body");
    body.replaceChildren();
    if (!valid) return;
    const eligible = Number(payload.board_eligible_count);
    const evaluated = Number(payload.evaluated_count);
    byId("stock-macd-j-summary").textContent =
      `主板非 ST ${formatCount(eligible)} 只 · 完整指标 ${formatCount(evaluated)} 只`
      + ` · 覆盖 ${eligible ? (evaluated / eligible * 100).toFixed(1) : "0.0"}%`
      + ` · 命中 ${formatCount(payload.matched_count)} 只`
      + `（同日 ${formatCount(payload.same_day_count)} · 提前 1～3 日 ${formatCount(payload.prior_3_sessions_count)}）`
      + ` · ${{ accepted: "完备", degraded: "降级", unavailable: "不可用" }[payload.quality] || "未知"}`;
    const metadata = asArray(payload.source_metadata);
    const latest = objectValue(metadata[metadata.length - 1]);
    byId("stock-macd-j-source").textContent =
      `指标来源：${text(latest.provider, "未知")} · 前复权日线 · 获取时间 ${text(latest.fetched_at, "未知")}`
      + " · 精确发布时间未提供；按信号日 ST 名单排除。";
    const group = byId("stock-macd-j-group").value || "all";
    const candidates = stockSelectionVisibleCandidates(payload.candidates)
      .filter((item) => group === "all" || item.signal_group === group);
    const number = (value) => finiteNumber(value) === null ? "--" : Number(value).toFixed(4);
    if (!candidates.length) {
      const cell = createElement("td", "stock-selection-table-empty",
        payload.quality === "unavailable" ? "指标证据不可用，不能判定是否命中。" : "当前分组与行业下没有命中股票。");
      cell.colSpan = 7;
      const row = createElement("tr"); row.append(cell); body.append(row);
    }
    candidates.forEach((candidate) => {
      const row = createElement("tr");
      const nameCell = createElement("td");
      nameCell.append(stockSelectionNameLink(candidate), createElement("small", "", candidate.instrument_id));
      const turn = createElement("td", "", `${candidate.j_turn_date} · 相隔 ${candidate.gap_sessions} 个交易日`);
      turn.append(createElement("div", "", `J 谷值 ${number(candidate.j_trough)} → 拐头日 ${number(candidate.j_turn_value)}`),
        createElement("small", "", asArray(candidate.low_j_tags).join(" · ") || "无低位标签"));
      const evidence = createElement("td");
      const details = createElement("details");
      const points = asArray(candidate.evidence);
      const today = objectValue(points[points.length - 1]);
      details.append(createElement("summary", "", `${candidate.signal_trade_date} 金叉 · DIF ${number(today.dif)} / DEA ${number(today.dea)} · J ${number(today.j)}`));
      const list = createElement("ul");
      points.forEach((point) => list.append(createElement("li", "",
        `${point.trade_date}：DIF ${number(point.dif)} / DEA ${number(point.dea)} · K ${number(point.k)} / D ${number(point.d)} / J ${number(point.j)}`)));
      details.append(list); evidence.append(details);
      row.append(nameCell, createElement("td", "", stockSelectionIndustryName(candidate)),
        createElement("td", "", formatLevel(candidate.reference_close)),
        createElement("td", "", candidate.signal_group === "same_day" ? "同日金叉与拐头" : "J 提前 1～3 日拐头"),
        createElement("td", "", { above_zero: "零轴上方", below_zero: "零轴下方", crossing_zero: "零轴附近" }[candidate.zero_axis_zone]),
        turn, evidence);
      body.append(row);
    });
    replaceTextList("stock-macd-j-methodology", payload.methodology, "暂无方法说明。");
    replaceTextList("stock-macd-j-limitations", payload.limitations, "暂无数据说明。");
    renderStockSelectionExclusions(payload.excluded_counts, "stock-macd-j-exclusions");
  }

  function stockSelectionStrategyResult(archive, definition) {
    const results = asArray(archive.results).map(objectValue)
      .filter((item) => item.strategy_id === definition.strategy_id);
    return objectValue(results.find((item) => item.strategy_version === definition.strategy_version)
      || results.sort((a, b) => Number(String(b.strategy_version).slice(1)) - Number(String(a.strategy_version).slice(1)))[0]);
  }

  function stockSelectionEvidenceValue(value) {
    const labels = {
      rank: "策略排名", score: "综合分", factor_coverage: "因子覆盖", contributions: "因子贡献",
      reasons: "入选依据", risks: "风险", occurrence_count: "命中次数", latest_occurrence_date: "最近命中",
      evidence: "逐次证据", anchor_trade_date: "本轮首次命中", anchor_low: "本轮底线", reset_count: "重置次数",
      signal_trade_date: "金叉日期", signal_group: "信号分组", zero_axis_zone: "零轴位置",
      j_turn_date: "J 拐头日期", gap_sessions: "间隔交易日", j_trough: "J 谷值", j_turn_value: "J 拐头值",
      low_j_tags: "低位标签", opportunity_stage: "机会阶段", recent_limit_up_count: "近期涨停次数",
      consecutive_limit_up_count: "连续涨停次数", entry_feasibility_factor: "入场可行性系数",
      reference_close: "入档收盘价", trade_date: "日期", open: "开盘", high: "最高", low: "最低", close: "收盘",
      minimum_subsequent_close: "后续最低收盘", amount_cny: "成交额(元)", total_market_cap_cny: "总市值(元)",
      daily_return_pct: "当日涨幅%", five_day_return_pct: "5日涨幅%", close_position_ratio: "收盘位置",
      turnover_rate_pct: "换手率%", volume_ratio: "量比", amount_expansion_ratio: "成交额扩张倍数",
      float_market_cap_cny: "流通市值(元)", closed_at_limit_up: "收盘涨停", opened_at_limit_up: "开盘涨停",
      breakout_distance_pct: "突破距离%", industry_positive_ratio: "行业上涨占比", industry_peer_count: "行业样本数",
      upper_shadow_pct_of_close: "上影/收盘%", upper_shadow_body_multiple: "上影/实体倍数",
      upper_shadow_range_ratio: "上影振幅占比", previous_close: "前收盘", change_pct: "涨幅%",
      volume_shares: "成交量(股)", prior_5d_average_volume_shares: "前5日均量(股)", volume_multiple: "放量倍数",
      factor: "因子", label: "指标", raw_value: "原值", z_score: "标准分", weighted_contribution: "加权贡献",
      normalized_score: "标准分", weighted_points: "加权分", dif: "DIF", dea: "DEA", k: "K", d: "D", j: "J",
    };
    if (typeof value === "number") return Number.isInteger(value) ? String(value) : String(Number(value.toFixed(4)));
    if (value === null || value === undefined) return "无";
    if (Array.isArray(value)) return value.length ? value.map(stockSelectionEvidenceValue).join("；") : "无";
    if (typeof value === "object") return Object.entries(value).map(([key, item]) =>
      `${labels[key] || key} ${stockSelectionEvidenceValue(item)}`).join(" · ");
    return ({same_day: "当日共振", prior_3_sessions: "此前3日拐头", above_zero: "零轴上方",
      below_zero: "零轴下方", crossing_zero: "跨零轴", pre_limit_up: "涨停前机会",
      limit_up_continuation: "涨停延续"})[value] || String(value);
  }

  function stockSelectionInsight(archive, strategyId) {
    const insights = objectValue(archive.insights);
    if (insights.contract !== "stock_selection_insights.v1" || insights.schema_version !== 1
      || insights.trade_date !== archive.trade_date) return {};
    return objectValue(objectValue(insights.strategies)[strategyId]);
  }

  function renderStockSelectionStrategy(archive, strategyId) {
    const scope = `${archive.trade_date}:${strategyId}`;
    if (state.stockSelectionInsightScope !== scope) state.stockSelectionChangeFilter = "all";
    state.stockSelectionInsightScope = scope;
    const insight = stockSelectionInsight(archive, strategyId);
    const comparison = objectValue(insight.comparison);
    if (comparison.status !== "available") state.stockSelectionChangeFilter = "all";
    state.stockSelectionComparison = comparison;
    renderStockSelectionStrategyContent(archive, strategyId);
    const toolbar = byId("stock-selection-insights-toolbar");
    toolbar.hidden = false;
    const tags = byId("stock-selection-change-tags");
    tags.replaceChildren();
    const counts = objectValue(comparison.counts);
    const total = Object.values(counts).reduce((sum, value) => sum + Number(value || 0), 0);
    [["all", "全部", total], ["changed", "有变化", counts.changed || 0], ["new", "新增", counts.new || 0]]
      .forEach(([filter, label, count]) => {
        const button = createElement("button", `stock-selection-industry-tag selection-change-tag--${filter}`, `${label} ${count}`);
        button.type = "button";
        button.setAttribute("aria-pressed", String(state.stockSelectionChangeFilter === filter));
        button.disabled = filter !== "all" && comparison.status !== "available";
        button.addEventListener("click", () => {
          state.stockSelectionChangeFilter = filter;
          renderStockSelectionStrategy(archive, strategyId);
          tags.querySelector('button[aria-pressed="true"]')?.focus();
        });
        tags.append(button);
      });
    const visible = stockSelectionVisibleCandidates(
      Object.entries(objectValue(comparison.rows)).map(([instrument_id]) => ({
        instrument_id, industry_block_name: objectValue(objectValue(archive.industry_display).names_by_instrument)[instrument_id],
      })),
    ).length;
    byId("stock-selection-comparison-summary").textContent = comparison.status === "available"
      ? `对比 ${comparison.previous_trade_date} · 橙色：选股证据变化 · 蓝色：较前一交易日新增 · 当前变化/行业筛选 ${visible} / ${total} 只`
        + (comparison.quality === "degraded" ? " · 存档数据降级，对比可能不完整" : "")
      : `无法对比：${comparison.previous_trade_date || "前一交易日"} 或当日同版本有效存档缺失，未将全部股票视为新增。`;
    byId("stock-selection-followup-open").disabled = !insight.followup;
    const definition = stockSelectionDefinitions(archive).find((item) => item.strategy_id === strategyId);
    const panel = definition && stockSelectionPanelForContract(definition.result_contract);
    panel?.querySelectorAll(".stock-selection-quote-link").forEach((link) => {
      const code = /\/(\d{6})\/$/.exec(link.href)?.[1];
      const item = Object.entries(objectValue(comparison.rows)).find(([id]) => id.slice(0, 6) === code)?.[1];
      if (!item || !["new", "changed"].includes(item.status)) return;
      const row = link.closest("tr");
      row?.classList.add(`selection-row--${item.status}`);
      const cell = item.status === "changed" ? row?.lastElementChild : link.closest("td");
      if (!cell) return;
      cell.append(createElement("small", `selection-change-badge selection-change-tag--${item.status}`,
        item.status === "new" ? "新增" : "证据有变化"));
      if (item.status === "changed") {
        const details = createElement("details", "selection-change-details");
        details.append(createElement("summary", "", `查看 ${item.changes.length} 项证据变化`));
        item.changes.forEach((change) => {
          details.append(createElement("div", "",
            `${stockSelectionEvidenceValue({[change.field]: change.before})} → ${stockSelectionEvidenceValue(change.after)}`));
        });
        cell.append(details);
      }
    });
    if (byId("stock-selection-followup-dialog").open) renderStockSelectionFollowup();
  }

  function renderStockSelectionFollowup() {
    const archive = objectValue(state.stockSelectionHistory);
    const strategyId = state.stockSelectionStrategyId;
    const definition = stockSelectionDefinitions(archive).find((item) => item.strategy_id === strategyId);
    const insight = stockSelectionInsight(archive, strategyId);
    const followup = objectValue(insight.followup);
    const window = asArray(objectValue(archive.insights).window_dates);
    byId("stock-selection-followup-title").textContent = `${definition?.title || "选股策略"} · 入档涨停回溯`;
    byId("stock-selection-followup-summary").textContent =
      `${window[0] || "--"} 至 ${archive.trade_date || "--"} · 含所选日共 5 个交易日 · 入档当天至所选日收盘涨停 · ${asArray(followup.rows).length} 只股票`;
    const warnings = [];
    if (asArray(followup.missing_archive_dates).length) warnings.push(`缺少同版本存档：${followup.missing_archive_dates.join("、")}`);
    if (asArray(followup.degraded_archive_dates).length) warnings.push(`存档数据降级：${followup.degraded_archive_dates.join("、")}`);
    if (asArray(followup.missing_limit_up_dates).length) warnings.push(`涨停名单缺失：${followup.missing_limit_up_dates.join("、")}`);
    byId("stock-selection-followup-quality").textContent = (followup.quality === "complete"
      ? "窗口数据齐全。" : `统计不完整，已展示有证据的记录。${warnings.join("；")}。`)
      + ` 策略版本 ${insight.strategy_version || "--"}；每个入档日单独保留，同一涨停日只计一次。入档通常在收盘后，入档日涨停仅作同日事实展示。行业归属沿用当前同花顺目录。`;
    const body = byId("stock-selection-followup-body");
    body.replaceChildren();
    const mapping = objectValue(objectValue(archive.industry_display).names_by_instrument);
    asArray(followup.rows).forEach((item) => {
      const row = createElement("tr");
      const nameCell = createElement("td");
      nameCell.append(stockSelectionNameLink(item), createElement("small", "", item.instrument_id));
      row.append(nameCell, createElement("td", "", mapping[item.instrument_id] || "未分类"),
        createElement("td", "stock-selection-score", String(item.archive_records.length)));
      const entries = createElement("td");
      const entryList = createElement("div", "stock-pattern-evidence");
      item.archive_records.forEach((record) => {
        const data = objectValue(record.candidate);
        const details = createElement("details", "selection-entry-evidence");
        const score = data.score === undefined ? "" : ` · 综合分 ${formatLevel(data.score)}`;
        const count = data.occurrence_count ?? asArray(data.evidence).length;
        details.append(createElement("summary", "", `${record.trade_date} · 入档收盘 ${formatLevel(data.reference_close)}${score}`
          + (count ? ` · 证据 ${count} 条` : "") + " · 展开策略数据"));
        Object.entries(data).filter(([key]) => !["instrument_id", "name", "industry", "market", "primary_business_name", "reference_close"].includes(key))
          .forEach(([key, value]) => details.append(createElement("div", "", stockSelectionEvidenceValue({[key]: value}))));
        entryList.append(details);
      });
      entries.append(entryList);
      row.append(entries, createElement("td", "stock-selection-score", String(item.limit_up_records.length)));
      const limits = createElement("td");
      const limitList = createElement("div", "stock-pattern-evidence");
      item.limit_up_records.forEach((record) => {
        const line = createElement("span", "", `${record.trade_date} · 收盘涨停 · 对应入档：${record.archive_dates.join("、")}`);
        line.title = `来源 ${text(objectValue(record.source).provider)} · 日级收盘涨停名单`;
        limitList.append(line);
      });
      limits.append(limitList);
      row.append(limits);
      body.append(row);
    });
    if (!asArray(followup.rows).length) {
      const row = createElement("tr");
      const empty = createElement("td", "stock-selection-table-empty", followup.quality === "complete"
        ? "该窗口内没有入档后（含当日）收盘涨停的股票。" : "暂无可核实的涨停记录，数据缺失不等于没有涨停。");
      empty.colSpan = 6;
      row.append(empty);
      body.append(row);
    }
  }

  function renderStockSelectionStrategyContent(archive, strategyId) {
    const definition = stockSelectionDefinitions(archive)
      .find((item) => item.strategy_id === strategyId);
    if (!definition) return;
    const result = stockSelectionStrategyResult(archive, definition);
    const archivedPayload = Object.keys(objectValue(result.payload)).length
      ? objectValue(result.payload)
      : legacyStockSelectionPayload(archive, definition);
    const industryDisplay = objectValue(archive.industry_display);
    const payload = Object.keys(archivedPayload).length
      ? stockSelectionIndustryPayload(archivedPayload, industryDisplay) : archivedPayload;
    byId("stock-selection-industry-basis").textContent = industryDisplay.as_of
      ? `行业板块：同花顺 · 目录日期 ${industryDisplay.as_of}。历史候选按该目录归组；缺少板块归属时列为未分类。`
      : "行业板块目录暂不可用，缺少归属的股票列为未分类。";
    renderStockSelectionIndustryFilters(payload.candidates, `${archive.trade_date}:${strategyId}`);
    const outcome = objectValue(
      asArray(archive.outcomes)
        .map(objectValue)
        .find((item) => item.result_id === result.result_id),
    );
    if (definition.result_contract === "stock_macd_j_screen.v1") {
      renderMacdJScreen(payload);
      return;
    }
    if (definition.result_contract === "stock_volume_surge_screen.v1") {
      renderVolumeSurgeScreen(payload);
      return;
    }
    if (definition.result_contract === "balanced_stock_selection_result.v1") {
      if (!Object.keys(payload).length) {
        byId("stock-selection-content").hidden = true;
        byId("stock-selection-empty").hidden = false;
        return;
      }
      renderStockSelection(
        {
          ...payload,
          trade_date: result.trade_date || archive.trade_date,
          generated_at: result.generated_at,
          source_quality: result.source_quality || objectValue(archive.legacy_selection).source_quality,
          source_provider_as_of: result.source_provider_as_of
            || objectValue(archive.legacy_selection).source_provider_as_of,
        },
        {
          outcome: Object.keys(outcome).length ? outcome : archive.legacy_outcome,
          recent_outcomes: archive.legacy_recent_outcomes,
        },
      );
      return;
    }
    if (definition.result_contract === "stock_limit_up_tendency_screen.v1") {
      byId("stock-limit-up-tendency-version").textContent = (
        `固定规则 ${text(payload.screen_version, definition.strategy_version)}`
      );
      renderLimitUpTendencyScreen(Object.keys(payload).length ? [payload] : []);
      renderLimitUpTendencyOutcome(outcome);
      return;
    }
    if (definition.result_contract === "stock_pattern_screen.v1") {
      byId("stock-pattern-version").textContent = (
        `固定规则 ${text(payload.screen_version, definition.strategy_version)}`
      );
      renderStockPatternScreen(Object.keys(payload).length ? [payload] : []);
    }
  }

  function updateStockSelectionSchedule() {
    const schedule = objectValue(state.stockSelectionSchedule);
    const manualAfter = text(schedule.manual_after, "18:00");
    const automaticAfter = text(schedule.automatic_if_missing_after, "18:30");
    const clock = shanghaiClock();
    const archive = objectValue(state.stockSelectionHistory);
    const expectedCount = stockSelectionDefinitions(archive).length;
    const todayEntry = asArray(archive.dates).find((item) => {
      const entry = objectValue(item);
      return text(entry.trade_date, text(item, "")) === clock.date;
    });
    const todayCount = Number(objectValue(todayEntry).strategy_count || 0);
    const todayExists = expectedCount > 0 && todayCount >= expectedCount;
    const due = clock.minutes >= reviewScheduleMinutes(manualAfter, "18:00");
    const button = byId("stock-selection-generate-button");
    button.disabled = state.stockSelectionGenerateInFlight || !due || todayExists;
    const phaseLabels = {
      queued: "等待执行…",
      acquiring: "正在获取数据…",
      selecting: "正在筛选…",
      archiving: "正在存档…",
    };
    if (state.stockSelectionGenerateInFlight) {
      button.textContent = phaseLabels[state.stockSelectionGenerationPhase] || "正在生成…";
    }
    else if (todayExists) button.textContent = "今日结果已齐全";
    else if (!due) button.textContent = `${manualAfter} 后可手动生成`;
    else button.textContent = todayCount > 0 ? "手动补生成今日结果" : "手动生成今日结果";
    byId("stock-selection-schedule").textContent = (
      `上海时间 ${manualAfter} 后可手动生成；`
      + `${automaticAfter} 自动补齐，仍有缺失时可手动重试。只补今日缺失策略，已存档结果保留。`
    );
  }

  function renderStockSelectionHistory(history) {
    state.stockSelectionHistory = history;
    state.stockSelectionSchedule = objectValue(history.schedule);
    renderStockSelectionDateOptions(history);
    renderStockSelectionStrategyTabs(history);
    if (!stockSelectionDefinitions(history).length) {
      byId("stock-selection-content").hidden = true;
      byId("stock-selection-empty").hidden = false;
      if (!state.stockSelectionGenerateInFlight) {
        byId("stock-selection-status").textContent = "尚无策略目录";
        byId("stock-selection-launch-status").textContent = "尚无策略目录";
      }
    } else {
      selectStockSelectionTab(state.stockSelectionStrategyId);
      const balanced = objectValue(
        asArray(history.results)
          .map(objectValue)
          .find((item) => item.result_contract === "balanced_stock_selection_result.v1"),
      );
      const balancedPayload = Object.keys(objectValue(balanced.payload)).length
        ? objectValue(balanced.payload)
        : objectValue(history.legacy_selection);
      if (!state.stockSelectionGenerateInFlight) {
        const missingCount = stockSelectionDefinitions(history).filter((definition) => (
          !asArray(history.results).some((result) => result.strategy_id === definition.strategy_id
            && result.strategy_version === definition.strategy_version)
        )).length;
        byId("stock-selection-status").textContent = missingCount
          ? `${text(history.trade_date, "--")} · 仍有 ${missingCount} 项策略未生成，数据就绪后可重试`
          : `${text(history.trade_date, "--")} · 策略结果已存档`;
        byId("stock-selection-launch-status").textContent = (
          `${text(history.trade_date, "--")} · ${formatCount(balancedPayload.selected_count)} 只量化候选`
        );
      }
    }
    updateStockSelectionSchedule();
  }

  async function fetchStockSelectionHistory({ tradeDate = null, silent = false } = {}) {
    state.stockSelectionHasLoaded = true;
    if (state.stockSelectionFetchInFlight && silent) return;
    const requestId = ++state.stockSelectionRequestId;
    state.stockSelectionFetchInFlight = true;
    if (!silent) {
      byId("stock-selection-status").textContent = "正在读取策略结果…";
      byId("stock-selection-insights-toolbar").hidden = true;
      if (byId("stock-selection-followup-dialog").open) byId("stock-selection-followup-dialog").close();
      byId("stock-selection-industry-filter").hidden = true;
      byId("stock-selection-launch-status").textContent = "正在读取策略结果…";
      document.querySelectorAll("[data-stock-selection-result-contract]").forEach((panel) => {
        panel.hidden = true;
      });
      document.querySelectorAll("[data-stock-selection-tab]").forEach((button) => {
        button.disabled = true;
      });
    }
    try {
      const query = tradeDate ? `?trade_date=${encodeURIComponent(tradeDate)}` : "";
      const response = await fetch(`${STOCK_SELECTION_RESULTS_ENDPOINT}${query}`, {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      const payload = await response.json();
      if (requestId !== state.stockSelectionRequestId) return;
      if (!response.ok) throw new Error(text(payload.error, `服务返回 ${response.status}`));
      const history = validateStockSelectionHistory(payload);
      if ((tradeDate && history.trade_date !== tradeDate)
        || asArray(history.results).some((item) => item.trade_date !== history.trade_date)) {
        throw new Error("选股结果日期不匹配，请重新读取");
      }
      renderStockSelectionHistory(history);
    } catch (error) {
      if (requestId !== state.stockSelectionRequestId) return;
      byId("stock-selection-industry-filter").hidden = true;
      document.querySelectorAll("[data-stock-selection-result-contract]").forEach((panel) => {
        panel.hidden = true;
      });
      document.querySelectorAll("[data-stock-selection-tab]").forEach((button) => {
        button.disabled = true;
      });
      byId("stock-selection-status").textContent = `策略结果暂不可用：${text(error.message, "等待重试")}`;
      byId("stock-selection-insights-toolbar").hidden = true;
      if (byId("stock-selection-followup-dialog").open) byId("stock-selection-followup-dialog").close();
      byId("stock-selection-launch-status").textContent = "策略结果暂不可用";
    } finally {
      if (requestId === state.stockSelectionRequestId) {
        state.stockSelectionFetchInFlight = false;
        updateStockSelectionSchedule();
      }
    }
  }

  function renderStockSelectionGenerationStatus(generation) {
    const phaseLabels = {
      queued: "任务已进入后台队列",
      acquiring: "后台正在获取 15 个交易日全市场数据",
      selecting: "数据已取得，正在执行选股与形态筛选",
      archiving: "筛选完成，正在写入不可变档案",
    };
    state.stockSelectionGenerateInFlight = new Set(["queued", "running"]).has(generation.state);
    state.stockSelectionGenerationPhase = text(generation.phase, "idle");
    if (new Set(["queued", "running"]).has(generation.state)) {
      const label = phaseLabels[generation.phase] || "后台正在生成选股档案";
      byId("stock-selection-status").textContent = `${label}；页面可继续使用`;
      byId("stock-selection-launch-status").textContent = label;
    } else if (generation.state === "failed") {
      byId("stock-selection-status").textContent = text(
        generation.error,
        "每日选股后台任务失败",
      );
      byId("stock-selection-launch-status").textContent = "生成失败";
    }
    updateStockSelectionSchedule();
  }

  async function readStockSelectionGeneration() {
    const response = await fetch(STOCK_SELECTION_GENERATION_ENDPOINT, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(text(payload.error, `服务返回 ${response.status}`));
    return validateStockSelectionGeneration(payload);
  }

  async function pollStockSelectionGeneration(initial = null) {
    if (state.stockSelectionGenerationPollInFlight) return;
    state.stockSelectionGenerationPollInFlight = true;
    try {
      let generation = initial || await readStockSelectionGeneration();
      renderStockSelectionGenerationStatus(generation);
      while (new Set(["queued", "running"]).has(generation.state)) {
        await new Promise((resolve) => window.setTimeout(resolve, 1500));
        generation = await readStockSelectionGeneration();
        renderStockSelectionGenerationStatus(generation);
      }
      if (generation.state === "succeeded") {
        await fetchStockSelectionHistory({
          tradeDate: state.stockSelectionTradeDate,
          silent: true,
        });
      }
    } catch (error) {
      state.stockSelectionGenerateInFlight = false;
      byId("stock-selection-status").textContent = `任务状态暂不可用：${text(error.message, "等待重试")}`;
      byId("stock-selection-launch-status").textContent = "任务状态暂不可用";
    } finally {
      state.stockSelectionGenerationPollInFlight = false;
      updateStockSelectionSchedule();
    }
  }

  async function generateStockSelection() {
    if (state.stockSelectionGenerateInFlight) return;
    state.stockSelectionGenerateInFlight = true;
    updateStockSelectionSchedule();
    state.stockSelectionGenerationPhase = "queued";
    byId("stock-selection-status").textContent = "正在提交后台选股任务…";
    byId("stock-selection-launch-status").textContent = "正在提交选股任务…";
    try {
      const response = await fetch(STOCK_SELECTION_GENERATE_ENDPOINT, {
        method: "POST",
        cache: "no-store",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: "{}",
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(text(payload.error, `服务返回 ${response.status}`));
      const generation = validateStockSelectionGeneration(payload);
      state.stockSelectionTradeDate = shanghaiClock().date;
      state.stockSelectionGenerateInFlight = false;
      await pollStockSelectionGeneration(generation);
    } catch (error) {
      state.stockSelectionGenerateInFlight = false;
      byId("stock-selection-status").textContent = text(error.message, "每日选股生成失败");
      byId("stock-selection-launch-status").textContent = "生成失败";
      updateStockSelectionSchedule();
    }
  }

  function reviewScheduleMinutes(value, fallback) {
    const match = /^(\d{1,2}):(\d{2})$/.exec(text(value, fallback));
    if (!match) return reviewScheduleMinutes(fallback, "17:30");
    const hours = Number(match[1]);
    const minutes = Number(match[2]);
    return hours >= 0 && hours <= 23 && minutes >= 0 && minutes <= 59
      ? hours * 60 + minutes
      : reviewScheduleMinutes(fallback, "17:30");
  }

  function updateReviewSchedule() {
    const schedule = objectValue(state.reviewSchedule);
    const manualAfter = text(schedule.manual_after, "17:30");
    const automaticAfter = text(schedule.automatic_if_missing_after, "21:00");
    const clock = shanghaiClock();
    const currentReview = objectValue(objectValue(state.reviewHistory).review);
    const todayExists = text(currentReview.trade_date, "") === clock.date
      || asArray(objectValue(state.reviewHistory).dates).some((item) => (
        text(objectValue(item).trade_date, text(item, "")) === clock.date
      ));
    const due = clock.minutes >= reviewScheduleMinutes(manualAfter, "17:30");
    const button = byId("daily-review-generate-button");
    button.disabled = state.reviewGenerateInFlight || !due || todayExists;
    const phaseLabels = {
      queued: "等待执行…",
      loading: "正在读取盘面…",
      evaluating: "正在评估复盘…",
      generating: "正在生成复盘…",
      archiving: "正在存档…",
    };
    if (state.reviewGenerateInFlight) {
      button.textContent = phaseLabels[state.reviewGenerationPhase] || "正在生成…";
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

  function validateReviewGeneration(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("返回内容不是日复盘任务状态");
    }
    if (payload.contract !== "post_market_review_generation.v1" || payload.schema_version !== 1) {
      throw new Error(
        `复盘任务契约不匹配：${text(payload.contract, "缺少契约")}`,
      );
    }
    if (!["idle", "queued", "running", "succeeded", "failed"].includes(payload.state)) {
      throw new Error("复盘任务返回了未知状态");
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

  const REVIEW_HIGHLIGHT_PATTERN = /([+\-−]?\d+(?:,\d{3})*(?:\.\d+)?(?:%|％|万亿|亿元|亿|万元|万|元|只|家|个|倍|点|分钟|日|板|连板)|\d{1,2}:\d{2}|赚钱效应|亏钱效应|净流入|净流出|涨停|跌停|领涨|领跌|上涨|下跌|回血|修复|回撤|偏强|偏弱|走强|走弱|主线|机会|风险|分歧|放量|缩量|扩散|确认|失效|证伪)/g;
  const REVIEW_UP_CONTEXT = /上涨|涨停|领涨|净流入|流入|回血|修复|偏强|走强|扩散|赚钱|涨|升/;
  const REVIEW_DOWN_CONTEXT = /下跌|跌停|领跌|净流出|流出|回撤|偏弱|走弱|亏钱|跌|降/;

  function reviewHighlightTone(token, source, offset) {
    const value = text(token, "");
    const before = source.slice(Math.max(0, offset - 7), offset);
    const after = source.slice(offset + value.length, offset + value.length + 7);
    const configuredTone = state.reviewHighlightTerms.get(value);
    if (configuredTone) return `daily-review-highlight--${configuredTone}`;
    if (/^\+/.test(value)) return "daily-review-highlight--up";
    if (/^[\-−]/.test(value)) return "daily-review-highlight--down";
    if (
      REVIEW_DOWN_CONTEXT.test(value)
      || /(?:下跌|跌停|领跌|流出|回撤|偏弱|走弱|亏钱|跌|降)[^，。；：]*$/.test(before)
      || /^[^，。；：]*(?:下跌|跌停|领跌|流出|回撤|偏弱|走弱|亏钱|跌|降)/.test(after)
    ) {
      return "daily-review-highlight--down";
    }
    if (
      REVIEW_UP_CONTEXT.test(value)
      || /(?:上涨|涨停|领涨|流入|回血|修复|偏强|走强|扩散|赚钱|涨|升)[^，。；：]*$/.test(before)
      || /^[^，。；：]*(?:上涨|涨停|领涨|流入|回血|修复|偏强|走强|扩散|赚钱|涨|升)/.test(after)
    ) return "daily-review-highlight--up";
    if (/风险|分歧|缩量|失效|证伪/.test(value)) return "daily-review-highlight--warning";
    if (/主线|机会|确认|放量/.test(value)) return "daily-review-highlight--focus";
    return "daily-review-highlight--metric";
  }

  function reviewHighlightPattern() {
    const terms = [...state.reviewHighlightTerms.keys()]
      .filter((term) => term.length >= 2)
      .sort((left, right) => right.length - left.length)
      .map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    return new RegExp(
      terms.length ? `${terms.join("|")}|${REVIEW_HIGHLIGHT_PATTERN.source}` : REVIEW_HIGHLIGHT_PATTERN.source,
      "g",
    );
  }

  function configureReviewHighlightTerms(review) {
    const terms = new Map();
    const add = (value, tone = "focus") => {
      text(value, "").split(/[、，,\/]/).map((item) => item.trim()).filter(Boolean).forEach((item) => {
        if (item.length >= 2) terms.set(item, tone);
      });
    };
    asArray(review.themes).map(objectValue).forEach((theme) => {
      const tone = /亏钱|风险|回避|抛压/.test(text(theme.role, "")) ? "down" : "focus";
      add(theme.name, tone);
      asArray(theme.representatives).forEach((name) => add(name, tone));
    });
    asArray(review.opportunity_sectors).map(objectValue).forEach((sector) => {
      add(sector.name, "focus");
      add(sector.leader_name, "focus");
    });
    state.reviewHighlightTerms = terms;
  }

  function appendReviewHighlights(element, value, fallback = "") {
    const content = reviewReportText(value, fallback);
    element.replaceChildren();
    let cursor = 0;
    for (const match of content.matchAll(reviewHighlightPattern())) {
      const offset = match.index ?? 0;
      if (offset > cursor) element.append(document.createTextNode(content.slice(cursor, offset)));
      element.append(createElement(
        "strong",
        `daily-review-highlight ${reviewHighlightTone(match[0], content, offset)}`,
        match[0],
      ));
      cursor = offset + match[0].length;
    }
    if (cursor < content.length) element.append(document.createTextNode(content.slice(cursor)));
    return element;
  }

  function createReviewHighlightedElement(tag, className, content, fallback = "") {
    return appendReviewHighlights(createElement(tag, className), content, fallback);
  }

  function reviewReadingBlocks(value) {
    const content = reviewReportText(value, "").trim();
    if (!content) return [];
    const semicolonCount = (content.match(/；/g) || []).length;
    const hasIntroducedList = content.includes("：") && semicolonCount >= 2;
    const blocks = [];
    let start = 0;
    let relation = "start";
    let listActive = false;
    const pushBlock = (end, nextRelation) => {
      const blockText = content.slice(start, end).trim();
      if (blockText) blocks.push({ relation, text: blockText });
      start = end;
      relation = nextRelation;
    };
    for (let index = 0; index < content.length; index += 1) {
      const character = content[index];
      const end = index + 1;
      const length = content.slice(start, end).trim().length;
      const hasRemainder = content.slice(end).trim().length > 0;
      if (!hasRemainder) continue;
      if (/[。！？]/.test(character)) {
        pushBlock(end, "sentence");
        listActive = false;
      } else if (character === "；" && length >= 18) {
        pushBlock(end, listActive ? "list-item" : "semicolon");
      } else if (character === "：" && hasIntroducedList && length >= 12) {
        pushBlock(end, "list-item");
        listActive = true;
      }
    }
    pushBlock(content.length, relation);
    return blocks;
  }

  function createReviewReadingGroup(paragraph, lead = false) {
    const group = createElement(
      "div",
      `daily-review-reading-group${lead ? " daily-review-article-section__lead" : ""}`,
    );
    reviewReadingBlocks(paragraph).forEach((block) => {
      const item = createReviewHighlightedElement(
        "p",
        `daily-review-reading-block daily-review-reading-block--${block.relation}`,
        block.text,
      );
      group.append(item);
    });
    return group;
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
      article.dataset.sectionNumber = String(index + 1).padStart(2, "0");
      article.append(createReviewHighlightedElement("h4", "", titleValue));
      const paragraphs = asArray(section.paragraphs)
        .map((paragraph) => reviewReportText(paragraph, ""))
        .filter(Boolean);
      const legacyLead = reviewReportText(section.lead ?? section.summary, "");
      if (!paragraphs.length && legacyLead) paragraphs.push(legacyLead);
      paragraphs.forEach((paragraph, paragraphIndex) => {
        article.append(createReviewReadingGroup(paragraph, paragraphIndex === 0));
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
      const stance = text(item.stance, "conditional");
      entry.dataset.stance = stance;
      const heading = createReviewHighlightedElement("h5", "", item.title, `观察项 ${index + 1}`);
      entry.append(heading);
      const meta = createElement("div", "daily-review-watch-item__meta");
      const stanceLabels = {
        conditional: "满足条件才看",
        wait_divergence: "只等分歧，不追",
        avoid: "回避",
      };
      meta.append(
        createElement("span", "daily-review-watch-item__stance", stanceLabels[stance] || "满足条件才看"),
        createElement("span", "daily-review-watch-item__checkpoint", `检查点 · ${text(item.checkpoint, "盘中")}`),
      );
      entry.append(meta);
      const why = reviewReportText(item.why_it_matters, "");
      if (why) entry.append(createReviewHighlightedElement("p", "", why));
      const metrics = asArray(item.metrics).map((value) => reviewReportText(value, "")).filter(Boolean);
      if (metrics.length) {
        const metricList = createElement("ul", "daily-review-watch-item__metrics");
        metrics.forEach((value) => metricList.append(createReviewHighlightedElement("li", "", value)));
        entry.append(metricList);
      }
      const conditions = createElement("dl");
      const confirmationLabel = createElement("dt", "daily-review-watch-item__confirmation-label", "看到什么算确认");
      const confirmation = createReviewHighlightedElement(
        "dd",
        "daily-review-watch-item__confirmation",
        item.confirmation,
        "未披露",
      );
      const invalidationLabel = createElement("dt", "daily-review-watch-item__invalidation-label", "什么情况算失效");
      const invalidation = createReviewHighlightedElement(
        "dd",
        "daily-review-watch-item__invalidation",
        item.invalidation,
        "未披露",
      );
      conditions.append(
        confirmationLabel,
        confirmation,
        invalidationLabel,
        invalidation,
      );
      entry.append(conditions);
      const action = reviewReportText(item.action, "");
      if (action) {
        entry.append(createReviewHighlightedElement("p", "daily-review-watch-item__action", `执行规则：${action}`));
      }
      const stocks = asArray(item.stocks).map(objectValue).filter((stock) => Object.keys(stock).length);
      if (stocks.length) {
        const stockList = createElement("ul", "daily-review-watch-item__stocks");
        stocks.forEach((stock) => {
          const boardLabels = {
            main_board: "主板",
            chi_next: "创业板",
            star: "科创板",
            beijing: "北交所",
          };
          const row = createElement("li", "daily-review-watch-stock");
          row.append(createReviewHighlightedElement(
            "strong",
            "",
            `${text(stock.name)} ${text(stock.instrument_id).slice(0, 6)} · ${boardLabels[text(stock.board)] || text(stock.board)}`,
          ));
          row.append(createReviewHighlightedElement("p", "", stock.reason, "未披露入选理由"));
          row.append(createReviewHighlightedElement("small", "", `确认：${text(stock.confirmation, "未披露")}`));
          row.append(createReviewHighlightedElement("small", "", `失效：${text(stock.invalidation, "未披露")}`));
          stockList.append(row);
        });
        entry.append(stockList);
      }
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
    configureReviewHighlightTerms(canonical);
    byId("daily-review-trade-date").textContent = text(canonical.trade_date);
    const quality = byId("daily-review-quality");
    quality.textContent = qualityLabels[canonical.quality] || text(canonical.quality);
    quality.dataset.quality = text(canonical.quality, "unknown");
    byId("daily-review-trigger").textContent = triggerLabels[canonical.trigger] || text(canonical.trigger);
    byId("daily-review-as-of").textContent = formatTimestamp(
      canonical.source_snapshot_as_of || canonical.generated_at,
      true,
    );
    appendReviewHighlights(
      byId("daily-review-report-title"),
      canonical.title,
      text(recap.headline, "盘后结构化复盘"),
    );
    appendReviewHighlights(
      byId("daily-review-report-deck"),
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
      if (!state.reviewGenerateInFlight) byId("daily-review-status").textContent = "尚无存档";
    } else {
      renderPostMarketReview(
        reviewWithPresentation(review, history.presentation),
        history.outcome,
        history.learning,
      );
      if (!state.reviewGenerateInFlight) {
        byId("daily-review-status").textContent = `${text(review.trade_date)} · 已存档`;
      }
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

  function renderPostMarketReviewGenerationStatus(generation) {
    const active = new Set(["queued", "running"]).has(generation.state);
    const phaseLabels = {
      queued: "任务已进入后台队列",
      loading: "后台正在读取已接收的盘面档案",
      evaluating: "后台正在评估历史记录",
      generating: "后台正在生成日复盘",
      archiving: "复盘已生成，正在写入不可变档案",
    };
    state.reviewGenerateInFlight = active;
    state.reviewGenerationPhase = text(generation.phase, active ? "queued" : "idle");
    if (active) {
      const label = phaseLabels[generation.phase] || "后台正在生成日复盘";
      byId("daily-review-status").textContent = `${label}；页面可继续使用`;
    } else if (generation.state === "failed") {
      byId("daily-review-status").textContent = text(
        generation.error,
        "日复盘后台任务失败",
      );
    }
    updateReviewSchedule();
  }

  async function readPostMarketReviewGeneration() {
    const response = await fetch(REVIEW_GENERATION_ENDPOINT, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(text(payload.error, `服务返回 ${response.status}`));
    return validateReviewGeneration(payload);
  }

  async function pollPostMarketReviewGeneration(initial = null) {
    if (state.reviewGenerationPollInFlight) return;
    state.reviewGenerationPollInFlight = true;
    try {
      let generation = initial || await readPostMarketReviewGeneration();
      renderPostMarketReviewGenerationStatus(generation);
      while (new Set(["queued", "running"]).has(generation.state)) {
        await new Promise((resolve) => window.setTimeout(resolve, 1500));
        generation = await readPostMarketReviewGeneration();
        renderPostMarketReviewGenerationStatus(generation);
      }
      if (generation.state === "succeeded") {
        byId("daily-review-status").textContent = "今日复盘已生成并存档";
        await fetchPostMarketReviewHistory({
          tradeDate: text(generation.trade_date, null),
          silent: true,
        });
      }
    } catch (error) {
      state.reviewGenerateInFlight = false;
      byId("daily-review-status").textContent = `任务状态暂不可用：${text(error.message, "等待重试")}`;
    } finally {
      state.reviewGenerationPollInFlight = false;
      updateReviewSchedule();
    }
  }

  async function generatePostMarketReview() {
    if (state.reviewGenerateInFlight) return;
    state.reviewGenerateInFlight = true;
    updateReviewSchedule();
    state.reviewGenerationPhase = "queued";
    byId("daily-review-status").textContent = "正在提交后台复盘任务…";
    try {
      const response = await fetch(REVIEW_GENERATE_ENDPOINT, {
        method: "POST",
        cache: "no-store",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: "{}",
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(text(payload.error, `服务返回 ${response.status}`));
      const generation = validateReviewGeneration(payload);
      state.reviewGenerateInFlight = false;
      await pollPostMarketReviewGeneration(generation);
    } catch (error) {
      state.reviewGenerateInFlight = false;
      byId("daily-review-status").textContent = text(error.message, "日复盘生成失败");
      updateReviewSchedule();
    }
  }

  const REVISION_PATTERN = /^[0-9a-f]{64}$/;

  function isRevision(value) {
    return typeof value === "string" && REVISION_PATTERN.test(value);
  }

  async function marketWatchResponseError(response) {
    let payload = {};
    try {
      payload = await response.json();
    } catch (_error) {
      payload = {};
    }
    const error = new Error(text(payload.error, `服务返回 ${response.status}`));
    error.status = response.status;
    error.discardBatch = response.status === 409
      && payload.action === "discard_batch_and_retry";
    error.noAcceptedReal = response.status === 503
      && payload.reason === "no_accepted_real";
    return error;
  }

  function marketWatchHeaders(etag, force) {
    const headers = { Accept: "application/json" };
    if (!force && etag) headers["If-None-Match"] = etag;
    return headers;
  }

  function validateCollectionStatus(payload) {
    if (
      !payload
      || typeof payload !== "object"
      || Array.isArray(payload)
      || payload.contract !== "market_watch_collector_envelope.v1"
      || payload.schema_version !== 1
    ) {
      throw new Error("采集状态契约不匹配");
    }
    const completeness = payload.collection_completeness;
    if (!completeness || typeof completeness !== "object") {
      throw new Error("采集完整性状态缺失");
    }
    const counts = [
      completeness.accepted_real,
      completeness.pending,
      completeness.retrying,
      completeness.unresolved,
    ];
    const expected = completeness.expected_minute_buckets;
    if (
      !Number.isInteger(expected)
      || expected < 0
      || counts.some((value) => !Number.isInteger(value) || value < 0)
      || counts.reduce((total, value) => total + value, 0) !== expected
    ) {
      throw new Error("采集完整性恒等式不成立");
    }
    const accepted = payload.latest_accepted_real;
    if (accepted !== null && (
      !accepted
      || typeof accepted !== "object"
      || !isRevision(accepted.source_snapshot_revision)
      || !text(accepted.snapshot_id, "")
      || !text(accepted.minute_bucket, "")
    )) {
      throw new Error("最新真实快照指针不完整");
    }
    const recovery = payload.daily_recovery;
    if (recovery !== null && recovery !== undefined && (
      !recovery
      || typeof recovery !== "object"
      || recovery.contract !== "market_watch_daily_recovery.v1"
      || recovery.schema_version !== 1
      || !Number.isInteger(recovery.remaining_gaps)
      || recovery.remaining_gaps < 0
      || !Number.isInteger(recovery.unavailable_gaps ?? 0)
      || (recovery.unavailable_gaps ?? 0) < 0
      || (recovery.unavailable_gaps ?? 0) > recovery.remaining_gaps
      || recovery.expected_minute_buckets - recovery.accepted_after !== recovery.remaining_gaps
      || !Number.isInteger(recovery.latest_attempt_progress_completed || 0)
      || !Number.isInteger(recovery.latest_attempt_progress_total || 0)
      || (recovery.latest_attempt_progress_completed || 0) < 0
      || (recovery.latest_attempt_progress_completed || 0) > (recovery.latest_attempt_progress_total || 0)
    )) {
      throw new Error("收盘完整性检查状态不匹配");
    }
    return payload;
  }

  function shanghaiClock(now = new Date()) {
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(now).filter((item) => item.type !== "literal").map((item) => [
      item.type,
      item.value,
    ]));
    const minutes = Number(parts.hour) * 60 + Number(parts.minute);
    return {
      date: `${parts.year}-${parts.month}-${parts.day}`,
      minutes,
      minuteOfDay: minutes,
    };
  }

  function collectionRecoveryErrorMessage(recovery) {
    if (!recovery) return "";
    const runErrorCode = text(recovery.last_error_code, "");
    const attemptErrorCode = text(recovery.latest_failure_error_code, "");
    const errorCode = runErrorCode || attemptErrorCode;
    if (!errorCode) return "";
    const rawMessage = text(
      runErrorCode
        ? recovery.last_error_message
        : recovery.latest_failure_error_message,
      "",
    );
    if (errorCode === "HistoricalTrajectoryNotPublished") {
      const parts = ["历史数据缺口"];
      if (!runErrorCode && recovery.latest_failure_minute_bucket) {
        parts.push(formatTimestamp(recovery.latest_failure_minute_bucket));
      }
      parts.push("数据源未发布该分钟的精确板块资金流，重试无法补齐；保留缺口，不影响其他分钟追补");
      return parts.join(" · ");
    }
    const knownMessages = {
      HistoricalMarketWatchUnavailable: "目标分钟没有已保存的真实盘面，当前历史接口也不能回放该分钟",
      CollectorRestarted: "采集进程在本次分钟处理完成前重启",
    };
    const message = knownMessages[errorCode] || rawMessage || errorCode;
    const parts = [runErrorCode ? "批次失败" : "最近失败"];
    if (!runErrorCode && recovery.latest_failure_minute_bucket) {
      parts.push(formatTimestamp(recovery.latest_failure_minute_bucket));
    }
    parts.push(`${errorCode}：${message}`);
    if (!runErrorCode && recovery.latest_failure_next_retry_at) {
      parts.push(`下次重试 ${formatTimestamp(recovery.latest_failure_next_retry_at)}`);
    }
    return parts.join(" · ");
  }

  function renderCollectionRecoveryStatus(payload) {
    const shell = document.querySelector(".collection-recovery-strip");
    const completeness = payload?.collection_completeness || {};
    const expected = Number.isInteger(completeness.expected_minute_buckets)
      ? completeness.expected_minute_buckets
      : 0;
    const accepted = Number.isInteger(completeness.accepted_real)
      ? completeness.accepted_real
      : 0;
    const gaps = Math.max(0, expected - accepted);
    const recovery = payload?.daily_recovery || null;
    const clock = shanghaiClock();
    const isToday = text(completeness.trade_date, "") === clock.date;
    const afterClose = isToday && clock.minuteOfDay > 15 * 60;
    const active = recovery && new Set(["pending", "running"]).has(recovery.status);
    const unavailableOnly = recovery?.status === "needs_attention"
      && gaps > 0 && recovery.unavailable_gaps === gaps;
    const statusLabels = {
      pending: "手动检查已排队",
      running: "正在检查并追补",
      complete: "收盘数据完整",
      retrying: "追补受阻 · 等待重试",
      needs_attention: "仍有缺口 · 请查看原因",
      failed: "检查失败 · 可手动重试",
    };
    let status = recovery ? text(recovery.status, "") : "";
    if (!status) status = expected === 0 ? "not_due" : afterClose ? "pending_auto" : "before_close";
    const label = statusLabels[status]
      || (status === "not_due" ? "今日无交易日检查" : "15:05 后自动检查");
    const initialGaps = recovery
      ? Math.max(0, expected - Number(recovery.accepted_before || 0))
      : 0;
    const attempted = recovery ? Number(recovery.attempted_slots || 0) : 0;
    const failedAttempts = recovery ? Number(recovery.failed_attempts || 0) : 0;
    const workDone = recovery ? Number(recovery.latest_attempt_progress_completed || 0) : 0;
    const workTotal = recovery ? Number(recovery.latest_attempt_progress_total || 0) : 0;
    byId("collection-recovery-accepted").textContent = expected ? `${accepted} / ${expected}` : "--";
    byId("collection-recovery-gaps").textContent = expected ? String(gaps) : "--";
    byId("collection-recovery-progress").textContent = recovery
      ? `${Math.min(attempted, initialGaps)} / ${initialGaps}${active && workTotal ? ` · 批内 ${workDone} / ${workTotal}` : ""}`
      : "--";
    byId("collection-recovery-failures").textContent = recovery
      ? String(failedAttempts)
      : "--";
    byId("collection-recovery-checked-at").textContent = recovery
      ? formatTimestamp(recovery.completed_at || recovery.started_at || recovery.requested_at)
      : "--";
    byId("collection-recovery-status").textContent = state.recoveryRequestError
      ? "排队失败 · 请重试"
      : unavailableOnly ? `追补已结束 · ${gaps} 分钟无历史源` : label;
    const detail = byId("collection-recovery-detail");
    if (recovery) {
      const trigger = recovery.trigger === "manual" ? "手动" : "自动";
      const repairedThisRun = Math.max(
        0,
        Number(recovery.accepted_after || accepted)
          - Number(recovery.accepted_before || 0),
      );
      const currentMinute = recovery.latest_attempt_minute_bucket
        ? formatTimestamp(recovery.latest_attempt_minute_bucket)
        : "";
      const stageLabels = {
        starting: "准备精确数据源",
        stock_minutes: "加载全市场分钟曲线",
        source_cache: "复用本轮精确曲线",
        breadth: "重算涨跌家数",
        indices: "重建角色指数与成交额",
        turnover_baseline: "读取上一交易日同分钟基线",
        rotation: "回放板块轮动",
        contract: "执行严格契约校验",
      };
      const stage = stageLabels[text(recovery.latest_attempt_progress_stage, "")] || "";
      const workMessage = text(recovery.latest_attempt_progress_message, "");
      if (status === "pending") {
        detail.textContent = `${trigger}检查已排队；等待采集进程领取，本轮预计处理 ${initialGaps} 个缺口。`;
      } else if (status === "running") {
        detail.textContent = `${trigger}检查进行中：已启动 ${attempted} / ${initialGaps} 个缺口${currentMinute ? `，当前 ${currentMinute}` : ""}${stage ? `；${stage}${workTotal ? ` ${workDone}/${workTotal}` : ""}` : ""}${workMessage ? `，${workMessage}` : ""}。`;
      } else if (gaps === 0) {
        detail.textContent = `${trigger}检查已完成；${expected} 个交易分钟均有可用真实快照。`;
      } else if (unavailableOnly) {
        detail.textContent = `可恢复分钟已处理完毕；保留 ${gaps} 个无精确历史来源的缺口，不再重复追补。真实完整度 ${accepted}/${expected}，未标记为完整。`;
      } else {
        detail.textContent = `${trigger}检查已处理 ${attempted} / ${initialGaps} 个缺口，补齐 ${repairedThisRun} 个、失败 ${failedAttempts} 个；仍有 ${gaps} 个分钟等待精确历史数据，不会用当前值伪造历史。`;
      }
    } else if (expected) {
      detail.textContent = "交易日 15:05 后自动扫描 1 个 09:25 集合竞价结果、237 个连续交易分钟和 1 个 15:00 收盘结果；缺口只接受可验证快照。";
    } else {
      detail.textContent = "非交易日不创建空检查；下一交易日收盘后自动执行。";
    }
    const errorDetail = byId("collection-recovery-error");
    const recoveryError = state.recoveryRequestError
      || collectionRecoveryErrorMessage(recovery);
    errorDetail.textContent = recoveryError;
    errorDetail.hidden = !recoveryError;
    shell.classList.remove("is-complete", "is-retrying", "is-attention", "is-failed", "is-unavailable");
    if (status === "complete") shell.classList.add("is-complete");
    if (status === "pending" || status === "running" || status === "retrying") shell.classList.add("is-retrying");
    if (status === "needs_attention") shell.classList.add(unavailableOnly ? "is-unavailable" : "is-attention");
    if (status === "failed") shell.classList.add("is-failed");
    const button = byId("collection-recovery-button");
    button.disabled = state.recoveryRequestInFlight || active || unavailableOnly || !expected || !afterClose;
    button.textContent = state.recoveryRequestInFlight
      ? "正在排队…"
      : active
        ? "已排队"
        : unavailableOnly
          ? "无可追补分钟"
          : recovery
            ? "重新检查并追补"
            : "检查并追补";
  }

  function renderIntradayTrajectoryRepair() {
    const repair = state.trajectoryRepair;
    const status = text(repair?.status, "idle");
    const active = status === "pending" || status === "running";
    const awaitingPublication = status === "partial"
      && Number(repair?.target_count || 0) > 0
      && Number(repair?.remaining_targets || 0) === 0;
    const labels = {
      pending: "等待采集空档",
      running: "低优先级追补中",
      complete: "已补齐到请求分钟",
      partial: awaitingPublication ? "缓存已补齐，等待发布验收" : "追补尚未完整交付",
      idle: "可在盘中手动追补",
    };
    byId("trajectory-repair-status").textContent = state.trajectoryRepairError
      ? "追补排队失败"
      : `${labels[status] || "等待轨迹状态"}${status === "partial" && repair?.last_error ? ` · ${repair.last_error}` : ""}`;
    byId("trajectory-repair-remaining").textContent = repair
      ? String(Number(repair.remaining_targets || 0))
      : "--";
    byId("trajectory-repair-improved").textContent = repair
      ? `${Number(repair.improved_targets || 0)} / ${Number(repair.attempted_targets || 0)}`
      : "--";
    const clock = shanghaiClock();
    const canRequest = (
      state.collectionStatus?.latest_accepted_real
      && clock.minuteOfDay >= 9 * 60 + 31
      && clock.minuteOfDay < 15 * 60
    );
    const button = byId("trajectory-repair-button");
    button.disabled = state.trajectoryRepairRequestInFlight || active || awaitingPublication || !canRequest;
    button.textContent = state.trajectoryRepairRequestInFlight
      ? "正在排队…"
      : active
        ? "追补已排队"
        : awaitingPublication
          ? "等待自动发布"
        : status === "partial"
          ? "重试真实缺口"
          : "盘中追补轨迹";
  }

  async function fetchIntradayTrajectoryRepair() {
    const response = await fetch(INTRADAY_TRAJECTORY_REPAIR_ENDPOINT, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw await marketWatchResponseError(response);
    const payload = await response.json();
    if (
      payload?.contract !== "market_watch_intraday_trajectory_repair_response.v1"
      || payload.schema_version !== 1
    ) {
      throw new Error("盘中轨迹追补状态契约无效");
    }
    state.trajectoryRepair = payload.repair || null;
    state.trajectoryRepairError = null;
    renderIntradayTrajectoryRepair();
  }

  async function requestIntradayTrajectoryRepair() {
    if (state.trajectoryRepairRequestInFlight) return;
    state.trajectoryRepairRequestInFlight = true;
    state.trajectoryRepairError = null;
    renderIntradayTrajectoryRepair();
    try {
      const response = await fetch(INTRADAY_TRAJECTORY_REPAIR_ENDPOINT, {
        method: "POST",
        cache: "no-store",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: "{}",
      });
      if (!response.ok) throw await marketWatchResponseError(response);
      const payload = await response.json();
      state.trajectoryRepair = payload.repair || null;
      await fetchIntradayTrajectoryRepair();
    } catch (error) {
      state.trajectoryRepairError = text(error?.message, "盘中轨迹追补暂时无法排队");
    } finally {
      state.trajectoryRepairRequestInFlight = false;
      renderIntradayTrajectoryRepair();
    }
  }

  async function fetchCollectionStatus({ force = false } = {}) {
    const response = await fetch(COLLECTION_STATUS_ENDPOINT, {
      cache: "no-cache",
      headers: marketWatchHeaders(state.collectionStatusEtag, force),
    });
    if (response.status === 304) {
      if (!state.collectionStatus) throw new Error("采集状态 304 缺少本地基线");
      try {
        await fetchIntradayTrajectoryRepair();
      } catch (error) {
        state.trajectoryRepairError = text(error?.message, "盘中轨迹追补状态暂不可用");
        renderIntradayTrajectoryRepair();
      }
      return state.collectionStatus;
    }
    if (!response.ok) throw await marketWatchResponseError(response);
    const status = validateCollectionStatus(await response.json());
    state.collectionStatus = status;
    state.collectionStatusEtag = response.headers.get("ETag");
    renderCollectionRecoveryStatus(status);
    try {
      await fetchIntradayTrajectoryRepair();
    } catch (error) {
      state.trajectoryRepairError = text(error?.message, "盘中轨迹追补状态暂不可用");
      renderIntradayTrajectoryRepair();
    }
    return status;
  }

  async function requestDailyRecovery() {
    if (state.recoveryRequestInFlight) return;
    state.recoveryRequestError = null;
    state.recoveryRequestInFlight = true;
    renderCollectionRecoveryStatus(state.collectionStatus);
    try {
      const response = await fetch(DAILY_RECOVERY_ENDPOINT, {
        method: "POST",
        cache: "no-store",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: "{}",
      });
      if (!response.ok) throw await marketWatchResponseError(response);
      const result = await response.json();
      if (result?.recovery && state.collectionStatus) {
        state.collectionStatus = {
          ...state.collectionStatus,
          daily_recovery: result.recovery,
        };
      }
      renderCollectionRecoveryStatus(state.collectionStatus);
      const refreshed = await fetchCollectionStatus({ force: true });
      renderCollectionRecoveryStatus(refreshed);
    } catch (error) {
      state.recoveryRequestError = text(
        error?.message,
        "收盘完整性检查暂时无法排队",
      );
    } finally {
      state.recoveryRequestInFlight = false;
      renderCollectionRecoveryStatus(state.collectionStatus);
    }
  }

  function trajectorySummary(summary, scope) {
    return scope === "offense"
      ? summary.offense_sector_flow_trajectory
      : summary.sector_flow_trajectory;
  }

  function validateTrajectorySummary(summary, scope) {
    const trajectory = trajectorySummary(summary, scope);
    const integrity = summary.payload_integrity?.[scope];
    if (trajectory === null && integrity === null) return;
    if (!trajectory || !integrity) throw new Error(`${scope} 轨迹 manifest 缺失`);
    if (
      trajectory.contract !== "sector_flow_trajectory_summary.v1"
      || trajectory.schema_version !== 1
      || trajectory.direction !== scope
      || trajectory.source_snapshot_revision !== summary.source_snapshot_revision
      || trajectory.trajectory_revision !== integrity.trajectory_revision
      || !isRevision(trajectory.trajectory_revision)
    ) {
      throw new Error(`${scope} 轨迹 revision 不一致`);
    }
    const sectors = asArray(trajectory.sectors);
    const keys = sectors.map((item) => text(item?.sector_key, ""));
    if (
      sectors.length !== trajectory.sector_count
      || keys.some((key) => !key)
      || new Set(keys).size !== keys.length
      || sectors.some((item) => Object.hasOwn(item, "points"))
      || sectors.reduce((total, item) => total + Number(item.point_count || 0), 0)
        !== trajectory.point_count
    ) {
      throw new Error(`${scope} 轨迹摘要 manifest 不完整`);
    }
  }

  function validateSummary(payload, expectedRevision, response) {
    if (
      !payload
      || typeof payload !== "object"
      || Array.isArray(payload)
      || payload.contract !== "market_watch_summary.v1"
      || payload.schema_version !== 1
      || payload.snapshot_contract !== "market_watch.v1"
      || payload.snapshot_schema_version !== 1
      || payload.source_snapshot_revision !== expectedRevision
      || payload.payload_integrity?.source_snapshot_revision !== expectedRevision
      || (payload.resonance_revision !== null && !isRevision(payload.resonance_revision))
      || (payload.resonance_revision !== null && !isRevision(payload.resonance_source_snapshot_revision))
      || (payload.resonance_revision !== null && !text(payload.resonance_as_of, ""))
      || (payload.resonance_revision === null && (
        payload.resonance_source_snapshot_revision !== null
        || payload.resonance_as_of !== null
      ))
      || response.headers.get("X-Source-Snapshot-Revision") !== expectedRevision
    ) {
      throw new Error("盘面摘要与 accepted-real revision 不一致");
    }
    validateTrajectorySummary(payload, "defense");
    validateTrajectorySummary(payload, "offense");
    return payload;
  }

  async function fetchSummary(expectedRevision, { force = false } = {}) {
    const query = new URLSearchParams({
      source_snapshot_revision: expectedRevision,
    });
    const response = await fetch(`${SUMMARY_ENDPOINT}?${query}`, {
      cache: "no-cache",
      headers: marketWatchHeaders(state.summaryEtag, force),
    });
    if (response.status === 304) {
      if (state.lastSummary?.source_snapshot_revision !== expectedRevision) {
        const error = new Error("盘面摘要 304 与本地 revision 不一致");
        error.discardBatch = true;
        throw error;
      }
      state.lastSummaryCheckedAt = Date.now();
      return state.lastSummary;
    }
    if (!response.ok) throw await marketWatchResponseError(response);
    const summary = validateSummary(await response.json(), expectedRevision, response);
    state.summaryEtag = response.headers.get("ETag");
    state.lastSummaryCheckedAt = Date.now();
    return summary;
  }

  function requiredSectorKeys(summary, scope) {
    const trajectory = trajectorySummary(summary, scope);
    if (!trajectory) return [];
    const payload = {
      ...trajectory,
      direction: scope,
      sectors: asArray(trajectory.sectors),
    };
    const selected = ensureSectorFlowSelection(payload);
    const required = new Set(selected);
    payload.sectors.forEach((item) => {
      const changeDelta = finiteNumber(item?.latest?.change_delta_5m_pct);
      if (hasFullSectorResonance(item)) {
        required.add(text(item.sector_key, ""));
      }
      if (
        changeDelta !== null
        && Math.abs(changeDelta) >= state.sectorFlowSurgeThreshold
      ) {
        required.add(text(item.sector_key, ""));
      }
    });
    return payload.sectors
      .map((item) => text(item.sector_key, ""))
      .filter((key) => key && required.has(key));
  }

  function validateTrajectoryDetail(payload, summary, scope, requestedKeys, response) {
    const trajectory = trajectorySummary(summary, scope);
    if (!trajectory) throw new Error(`${scope} 轨迹摘要缺失`);
    const expectedKeys = new Set(requestedKeys);
    const sectorKeys = asArray(payload?.sector_keys);
    const sectors = asArray(payload?.sectors);
    const integrity = asArray(payload?.sector_integrity);
    if (
      !payload
      || payload.contract !== "sector_flow_trajectory_detail.v1"
      || payload.schema_version !== 1
      || payload.direction !== scope
      || payload.source_snapshot_revision !== summary.source_snapshot_revision
      || payload.trajectory_revision !== trajectory.trajectory_revision
      || response.headers.get("X-Source-Snapshot-Revision")
        !== summary.source_snapshot_revision
      || response.headers.get("X-Trajectory-Revision")
        !== trajectory.trajectory_revision
      || sectorKeys.length !== expectedKeys.size
      || sectorKeys.some((key) => !expectedKeys.has(key))
      || sectors.length !== sectorKeys.length
      || integrity.length !== sectorKeys.length
      || sectors.some((item, index) => item?.sector_key !== sectorKeys[index])
      || integrity.some((item, index) => item?.sector_key !== sectorKeys[index])
    ) {
      throw new Error(`${scope} 精确轨迹与摘要 revision 不一致`);
    }
    const summaryByKey = new Map(
      asArray(trajectory.sectors).map((item) => [item.sector_key, item]),
    );
    let pointCount = 0;
    sectors.forEach((item, index) => {
      const manifest = summaryByKey.get(item.sector_key);
      const proof = integrity[index];
      const points = asArray(item.points);
      pointCount += points.length;
      if (
        !manifest
        || points.length !== manifest.point_count
        || points.length !== proof.point_count
        || proof.points_revision !== manifest.points_revision
        || text(proof.first_provider_as_of, "")
          !== text(manifest.first_provider_as_of, "")
        || text(proof.last_provider_as_of, "")
          !== text(manifest.last_provider_as_of, "")
        || (points.length > 0 && (
          text(points[0].provider_as_of, "") !== text(proof.first_provider_as_of, "")
          || text(points.at(-1).provider_as_of, "") !== text(proof.last_provider_as_of, "")
        ))
      ) {
        throw new Error(`${scope}/${text(item.sector_key, "unknown")} 盘中点 manifest 不一致`);
      }
    });
    if (pointCount !== payload.point_count) {
      throw new Error(`${scope} 盘中点数不一致`);
    }
    return payload;
  }

  function detailCacheKey(summary, scope, sectorKeys) {
    const trajectory = trajectorySummary(summary, scope);
    return [
      summary.source_snapshot_revision,
      trajectory.trajectory_revision,
      scope,
      [...sectorKeys].sort().join(","),
    ].join(":");
  }

  async function fetchTrajectoryDetail(summary, scope, sectorKeys, { force = false } = {}) {
    if (!sectorKeys.length) return { cacheKey: null, etag: null, map: new Map() };
    const trajectory = trajectorySummary(summary, scope);
    const cacheKey = detailCacheKey(summary, scope, sectorKeys);
    const query = new URLSearchParams({
      direction: scope,
      sector_keys: sectorKeys.join(","),
      source_snapshot_revision: summary.source_snapshot_revision,
      trajectory_revision: trajectory.trajectory_revision,
    });
    const response = await fetch(`${TRAJECTORY_ENDPOINT}?${query}`, {
      cache: "no-cache",
      headers: marketWatchHeaders(state.detailEtags.get(cacheKey), force),
    });
    let payload;
    if (response.status === 304) {
      payload = state.detailPayloads.get(cacheKey);
      if (!payload) {
        const error = new Error("轨迹 304 缺少本地精确批次");
        error.discardBatch = true;
        throw error;
      }
    } else {
      if (!response.ok) throw await marketWatchResponseError(response);
      payload = await response.json();
    }
    validateTrajectoryDetail(payload, summary, scope, sectorKeys, response);
    return {
      cacheKey,
      etag: response.status === 304
        ? state.detailEtags.get(cacheKey)
        : response.headers.get("ETag"),
      map: new Map(payload.sectors.map((item) => [item.sector_key, item])),
      payload,
    };
  }

  function validateFiveDayTrajectory(payload, scope, requestedKeys, response) {
    const requested = new Set(requestedKeys);
    const days = asArray(payload?.days);
    const tradeDates = asArray(payload?.trade_dates);
    const sectorKeys = asArray(payload?.sector_keys);
    if (
      !payload
      || payload.contract !== "sector_flow_five_day_trajectory.v1"
      || payload.schema_version !== 1
      || payload.direction !== scope
      || payload.requested_trade_days !== 5
      || payload.available_trade_days !== days.length
      || payload.sector_count !== sectorKeys.length
      || sectorKeys.length !== requested.size
      || sectorKeys.some((key) => !requested.has(key))
      || tradeDates.length !== days.length
      || !isRevision(payload.history_revision)
      || response.headers.get("X-Trajectory-Revision") !== payload.history_revision
    ) {
      throw new Error(`${scope} 近五日轨迹总契约不一致`);
    }
    let pointCount = 0;
    let previousDate = "";
    days.forEach((day, dayIndex) => {
      const sectors = asArray(day?.sectors);
      const keys = sectors.map((item) => text(item?.sector_key, ""));
      const tradeDate = text(day?.trade_date, "");
      const dailyPointCount = sectors.reduce((total, item) => (
        total + asArray(item?.points).length
      ), 0);
      if (
        day?.contract !== "sector_flow_five_day_slice.v1"
        || day?.schema_version !== 1
        || day?.direction !== scope
        || !tradeDate
        || tradeDates[dayIndex] !== tradeDate
        || (previousDate && tradeDate <= previousDate)
        || !isRevision(day?.source_snapshot_revision)
        || !isRevision(day?.trajectory_revision)
        || keys.some((key) => !key || !requested.has(key))
        || new Set(keys).size !== keys.length
        || dailyPointCount !== day?.point_count
        || sectors.some((item) => asArray(item?.points).some((point) => (
          text(point?.provider_as_of, "").slice(0, 10) !== tradeDate
        )))
      ) {
        throw new Error(`${scope}/${tradeDate || "unknown"} 近五日轨迹日切片不一致`);
      }
      previousDate = tradeDate;
      pointCount += dailyPointCount;
    });
    if (pointCount !== payload.point_count) {
      throw new Error(`${scope} 近五日轨迹点数不一致`);
    }
    return payload;
  }

  const fiveDayTrajectoryRequests = new Map();

  async function fetchFiveDayTrajectory(scope, sectorKeys, { force = false } = {}) {
    const sortedKeys = [...new Set(sectorKeys)].sort();
    if (!sortedKeys.length) throw new Error(`${scope} 没有可读取的板块`);
    const cacheKey = `${scope}:${sortedKeys.join(",")}`;
    const pending = fiveDayTrajectoryRequests.get(cacheKey);
    if (pending && !force) return pending;
    const request = (async () => {
      const cached = state.fiveDayTrajectory[scope];
      const query = new URLSearchParams({
        direction: scope,
        sector_keys: sortedKeys.join(","),
      });
      const response = await fetch(`${FIVE_DAY_TRAJECTORY_ENDPOINT}?${query}`, {
        cache: "no-cache",
        headers: marketWatchHeaders(
          cached.cacheKey === cacheKey ? cached.etag : null,
          force,
        ),
      });
      let payload;
      if (response.status === 304) {
        if (cached.cacheKey !== cacheKey || !cached.payload) {
          throw new Error("近五日轨迹 304 缺少本地精确批次");
        }
        payload = cached.payload;
      } else {
        if (!response.ok) throw await marketWatchResponseError(response);
        payload = await response.json();
      }
      validateFiveDayTrajectory(payload, scope, sortedKeys, response);
      state.fiveDayTrajectory[scope] = {
        cacheKey,
        error: null,
        etag: response.status === 304 ? cached.etag : response.headers.get("ETag"),
        payload,
      };
      return payload;
    })();
    fiveDayTrajectoryRequests.set(cacheKey, request);
    try {
      return await request;
    } finally {
      if (fiveDayTrajectoryRequests.get(cacheKey) === request) {
        fiveDayTrajectoryRequests.delete(cacheKey);
      }
    }
  }

  async function hydrateFiveDaySectorFlow(scope) {
    if (!state.lastSummary) return null;
    const sectorKeys = requiredSectorKeys(state.lastSummary, scope);
    try {
      return await fetchFiveDayTrajectory(scope, sectorKeys);
    } catch (error) {
      state.fiveDayTrajectory[scope] = {
        ...state.fiveDayTrajectory[scope],
        error: text(error?.message, "近五日轨迹读取失败"),
      };
      throw error;
    }
  }

  function hydratedTrajectory(summary, scope, exactByKey) {
    const trajectory = trajectorySummary(summary, scope);
    if (!trajectory) return null;
    return {
      ...trajectory,
      contract: "sector_flow_trajectory.v1",
      schema_version: 1,
      sectors: asArray(trajectory.sectors).map((manifest) => {
        const exact = exactByKey.get(manifest.sector_key) || {};
        return {
          ...manifest,
          ...exact,
          leader_snapshot: manifest.leader_snapshot || exact.leader_snapshot || null,
          points: asArray(exact.points),
        };
      }),
    };
  }

  function hydratedSnapshot(summary, details) {
    return {
      ...summary,
      contract: summary.snapshot_contract,
      schema_version: summary.snapshot_schema_version,
      sector_flow_trajectory: hydratedTrajectory(
        summary,
        "defense",
        details.defense,
      ),
      offense_sector_flow_trajectory: hydratedTrajectory(
        summary,
        "offense",
        details.offense,
      ),
    };
  }

  function keepMarketWatchSurfacesVisible() {
    const selectors = [
      "#decision-bar",
      "#index-dock",
      "#intraday-macd-j-section",
      "#sector-flow-section",
      "main > .facts-grid",
      "main > .analysis-grid",
      "main > .bottom-grid",
    ];
    selectors.forEach((selector) => {
      document.querySelectorAll(selector).forEach((element) => {
        element.hidden = false;
      });
    });
  }

  function renderMarketWatchUnavailableShell(message) {
    keepMarketWatchSurfacesVisible();
    if (state.lastSnapshot) return;
    ["defense", "offense"].forEach((scope) => {
      renderSectorFlowUnavailable(message, scope);
    });
  }

  function discardMarketWatchAttempt() {
    state.summaryEtag = null;
    state.lastSummaryCheckedAt = 0;
    state.detailEtags.clear();
    state.detailPayloads.clear();
  }

  function renderNoAcceptedReal(status) {
    keepMarketWatchSurfacesVisible();
    const hasLastSnapshot = Boolean(state.lastSnapshot);
    const cursor = status.collection_cursor || status.latest_published_status || {};
    if (!hasLastSnapshot) {
      byId("contract-chip").textContent = "collector_waiting.v1";
      byId("snapshot-meta").textContent = `collection ${text(cursor.status, "unknown")}`;
      renderMarketWatchUnavailableShell("暂无已接收的真实盘中快照；图表区域保留并等待采集恢复。");
    }
    const overlay = byId("data-risk-overlay");
    overlay.classList.add("is-visible");
    overlay.classList.remove("is-warning");
    byId("data-risk-title").textContent = "暂无已接收的真实盘中快照";
    byId("data-risk-detail").textContent = hasLastSnapshot
      ? `本轮未取得新快照；保留最后一份已核验画面（${formatTimestamp(state.lastSnapshot.as_of)}），仅供回看，不代表当前盘面。`
      : "页面结构和图表占位继续显示；不会把缺口心跳当成盘面数值。";
    byId("alert-lock-label").textContent = "盘面变化提醒已锁定";
  }

  function rememberDetailResult(result) {
    if (!result.cacheKey) return;
    state.detailEtags.set(result.cacheKey, result.etag);
    state.detailPayloads.set(result.cacheKey, result.payload);
  }

  async function loadMarketWatchBatch({ force = false } = {}) {
    const status = await fetchCollectionStatus({ force });
    const accepted = status.latest_accepted_real;
    if (!accepted) {
      renderNoAcceptedReal(status);
      return { changed: true, snapshot: null };
    }
    const sourceRevision = accepted.source_snapshot_revision;
    const sameSource = state.lastSummary?.source_snapshot_revision === sourceRevision;
    const resonanceRefreshDue = (
      state.lastSummaryCheckedAt === 0
      || Date.now() - state.lastSummaryCheckedAt >= RESONANCE_REFRESH_INTERVAL_MS
    );
    if (
      !force
      && sameSource
      && state.lastSnapshot
      && !resonanceRefreshDue
    ) {
      keepMarketWatchSurfacesVisible();
      return { changed: false, snapshot: state.lastSnapshot };
    }
    const previousResonanceRevision = state.lastSummary?.resonance_revision ?? null;
    const summary = await fetchSummary(sourceRevision, { force });
    const resonanceChanged = (
      summary.resonance_revision !== previousResonanceRevision
    );
    if (!force && sameSource && state.lastSnapshot) {
      if (!resonanceChanged) {
        keepMarketWatchSurfacesVisible();
        return { changed: false, snapshot: state.lastSnapshot };
      }
      const snapshot = validateSnapshot(hydratedSnapshot(
        summary,
        state.trajectoryDetails,
      ));
      state.lastSummary = summary;
      state.lastSnapshot = snapshot;
      keepMarketWatchSurfacesVisible();
      return { changed: true, snapshot };
    }
    const scopes = ["defense", "offense"];
    const results = await Promise.all(scopes.map((scope) => (
      fetchTrajectoryDetail(
        summary,
        scope,
        requiredSectorKeys(summary, scope),
        { force },
      )
    )));
    const details = {
      defense: results[0].map,
      offense: results[1].map,
    };
    const snapshot = validateSnapshot(hydratedSnapshot(summary, details));
    results.forEach(rememberDetailResult);
    state.lastSummary = summary;
    state.trajectoryDetails = details;
    state.lastSnapshot = snapshot;
    keepMarketWatchSurfacesVisible();
    return { changed: true, snapshot };
  }

  async function hydrateCurrentSectorFlow(scope) {
    const summary = state.lastSummary;
    if (!summary) return;
    const sectorKeys = requiredSectorKeys(summary, scope);
    const current = state.trajectoryDetails[scope];
    if (sectorKeys.every((key) => current.has(key))) return;
    const result = await fetchTrajectoryDetail(summary, scope, sectorKeys);
    if (state.lastSummary?.source_snapshot_revision !== summary.source_snapshot_revision) {
      return;
    }
    rememberDetailResult(result);
    state.trajectoryDetails = {
      ...state.trajectoryDetails,
      [scope]: new Map([...current, ...result.map]),
    };
    state.lastSnapshot = validateSnapshot(hydratedSnapshot(
      summary,
      state.trajectoryDetails,
    ));
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
      let loaded = null;
      for (let attempt = 0; attempt < 2; attempt += 1) {
        try {
          loaded = await loadMarketWatchBatch({ force: force || attempt > 0 });
          break;
        } catch (error) {
          if (
            attempt === 0
            && (error.discardBatch || error.noAcceptedReal)
          ) {
            if (error.discardBatch) discardMarketWatchAttempt();
            continue;
          }
          throw error;
        }
      }
      if (!loaded) throw new Error("盘面批次未完成");
      state.fetchFailed = false;
      state.lastFetchError = null;
      state.lastSuccessAt = Date.now();
      if (loaded.snapshot && loaded.changed) {
        renderSnapshot(loaded.snapshot);
        processBackendAlerts(loaded.snapshot);
      }
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

  function runScheduledPoll() {
    if (document.visibilityState !== "visible" || state.fetchInFlight) return;
    if (!state.nextPollAt || Date.now() < state.nextPollAt) return;
    fetchSnapshot();
  }

  function resumeSnapshotPolling() {
    if (document.visibilityState !== "visible") return;
    state.nextPollAt = Date.now();
    runScheduledPoll();
  }

  function bindPollingRecovery() {
    document.addEventListener("visibilitychange", resumeSnapshotPolling);
    window.addEventListener("focus", resumeSnapshotPolling);
    window.addEventListener("pageshow", resumeSnapshotPolling);
  }

  function validateLimitUpPool(payload, expectedRevision) {
    const items = asArray(payload?.items);
    const categories = asArray(payload?.categories);
    const counts = [
      payload?.catalog_matched_count,
      payload?.unmatched_count,
    ];
    if (
      !payload
      || typeof payload !== "object"
      || Array.isArray(payload)
      || payload.contract !== "limit_up_pool.v2"
      || payload.schema_version !== 2
      || payload.source_snapshot_revision !== expectedRevision
      || !isRevision(payload.pool_revision)
      || !Number.isInteger(payload.pool_total)
      || payload.pool_total < 0
      || payload.pool_total !== items.length
      || !Number.isInteger(payload.business_classified_count)
      || payload.business_classified_count < 0
      || payload.business_classified_count > payload.catalog_matched_count
      || !Number.isInteger(payload.market_attributed_count)
      || payload.market_attributed_count < 0
      || payload.market_attributed_count > payload.pool_total
      || counts.some((value) => !Number.isInteger(value) || value < 0)
      || counts.reduce((total, value) => total + value, 0) !== payload.pool_total
      || categories.reduce((total, item) => total + Number(item?.count || 0), 0)
        !== payload.pool_total
      || (payload.catalog_matched_count > 0 && !isRevision(payload.relationship_catalog_revision))
    ) {
      throw new Error("涨停池归属契约与当前真实快照不一致");
    }
    const instrumentIds = items.map((item) => text(item?.instrument_id, ""));
    if (
      instrumentIds.some((value) => !/^\d{6}\.(SH|SZ|BJ)$/.test(value))
      || new Set(instrumentIds).size !== instrumentIds.length
    ) {
      throw new Error("涨停池股票身份不完整或重复");
    }
    items.forEach((item) => {
      const status = text(item?.relationship_match_status, "");
      const boardCountBasis = text(item?.board_count_basis, "");
      const validBoardCount = (
        boardCountBasis === "daily_closed_limit_up_history"
        && Number.isInteger(item?.board_count)
        && item.board_count >= 1
      ) || (
        boardCountBasis === "unavailable"
        && item?.board_count === null
      );
      const displayKey = text(item?.display_category_key, "");
      const displayName = text(item?.display_category_name, "");
      const displayBasis = text(item?.display_category_basis, "");
      const hasCatalogFields = Boolean(text(item?.directory_category_name, ""))
        || Boolean(text(item?.business_domain_name, ""))
        || Boolean(text(item?.primary_business_name, ""))
        || asArray(item?.business_tags).length > 0
        || Boolean(text(item?.statistical_industry_name, ""))
        || Boolean(text(item?.relationship_verification_status, ""));
      const matched = status === "matched"
        && Boolean(text(item?.relationship_verification_status, ""));
      const unmatched = status === "unmatched" && !hasCatalogFields;
      const validDisplayBasis = new Set([
        "manual_market_review",
        "event_business_crosscheck",
        "evidence_candidate_ranking",
        "relationship_directory",
        "primary_business",
        "unresolved",
      ]).has(displayBasis);
      const validDisplay = validDisplayBasis
        && text(item?.display_category_effective_on, "") === text(payload.trade_date, "")
        && (displayBasis === "unresolved"
          ? !displayKey && !displayName
          : Boolean(displayKey && displayName));
      if ((!matched && !unmatched) || !validDisplay || !validBoardCount) {
        throw new Error(`涨停池 ${text(item?.name, item?.instrument_id)} 归属匹配状态不完整`);
      }
    });
    return payload;
  }

  function formatLimitUpSealTime(value) {
    if (!value) return "时间待核验";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "时间待核验";
    return new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone: "Asia/Shanghai",
    }).format(parsed);
  }

  function limitUpPoolCategoryKey(item) {
    return text(item?.display_category_key, "") || "unresolved_business";
  }

  function renderLimitUpPoolCategories(pool) {
    const target = byId("limit-up-pool-categories");
    const categories = asArray(pool.categories);
    const validKeys = new Set(["all", ...categories.map((item) => text(item.business_key, ""))]);
    if (!validKeys.has(state.limitUpPoolCategory)) state.limitUpPoolCategory = "all";
    const specs = [
      { business_key: "all", label: "全部", count: pool.pool_total },
      ...categories,
    ];
    const buttons = specs.map((category) => {
      const key = text(category.business_key, "unresolved_business");
      const button = createElement(
        "button",
        "",
        `${text(category.label, "待确认")} (${Number(category.count || 0)})`,
      );
      button.type = "button";
      button.setAttribute("role", "tab");
      button.dataset.limitUpCategory = key;
      const selected = key === state.limitUpPoolCategory;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      button.addEventListener("click", () => {
        state.limitUpPoolCategory = key;
        renderLimitUpPool(pool);
      });
      button.addEventListener("keydown", (event) => {
        if (!new Set(["ArrowLeft", "ArrowRight", "Home", "End"]).has(event.key)) return;
        event.preventDefault();
        const current = buttons.indexOf(button);
        const index = event.key === "Home"
          ? 0
          : event.key === "End"
            ? buttons.length - 1
            : (current + (event.key === "ArrowRight" ? 1 : -1) + buttons.length)
              % buttons.length;
        state.limitUpPoolCategory = buttons[index].dataset.limitUpCategory;
        renderLimitUpPool(pool);
        byId("limit-up-pool-categories").querySelector(
          `[data-limit-up-category="${state.limitUpPoolCategory}"]`,
        )?.focus();
      });
      return button;
    });
    target.replaceChildren(...buttons);
  }

  function limitUpPoolCard(item) {
    const status = text(item.relationship_match_status, "unmatched");
    const card = createElement("article", `limit-up-stock-card is-${status}`);
    card.title = [
      `真实归属库状态：${text(item.relationship_verification_status, "未匹配")}`,
      `主显示依据：${text(item.display_category_basis, "待核验")}`,
      `目录标志：${asArray(item.relationship_flags).join(" / ") || "无"}`,
    ].join("\n");
    card.appendChild(createElement(
      "span",
      "limit-up-stock-card__time",
      formatLimitUpSealTime(item.first_sealed_at),
    ));
    card.appendChild(createElement(
      "strong",
      "limit-up-stock-card__name",
      text(item.name, item.instrument_id),
    ));
    const boardLabel = text(item.board_label, "");
    if (boardLabel) {
      card.appendChild(createElement(
        "span",
        "limit-up-stock-card__meta",
        `区间记录 · ${boardLabel}`,
      ));
    }
    const sector = createElement("div", "limit-up-stock-card__sector");
    if (item.is_one_word_board) {
      sector.appendChild(createElement("i", "one-word-badge", "一字"));
    }
    const displayLabel = text(item.display_category_name, "");
    const sectorLabel = displayLabel ? `主显示 · ${displayLabel}` : "主显示待核验";
    sector.appendChild(createElement("span", "", sectorLabel));
    card.appendChild(sector);
    const businessPath = [
      text(item.business_domain_name, ""),
      text(item.primary_business_name, ""),
    ].filter((value, index, values) => value && values.indexOf(value) === index).join(" → ");
    card.appendChild(createElement(
      "span",
      "limit-up-stock-card__meta",
      businessPath ? `主营 · ${businessPath}` : "主营明细待核验",
    ));
    card.appendChild(createElement(
      "span",
      "limit-up-stock-card__meta",
      `长期目录 · ${text(item.directory_category_name, "待核验")}`,
    ));
    return card;
  }

  function renderLimitUpPool(pool) {
    byId("limit-up-pool-total").textContent = String(pool.pool_total);
    byId("limit-up-pool-matched").textContent = String(pool.catalog_matched_count);
    byId("limit-up-pool-classified").textContent = String(pool.business_classified_count);
    byId("limit-up-pool-unmatched").textContent = String(pool.unmatched_count);
    byId("limit-up-pool-trade-date").textContent = `交易日 ${text(pool.trade_date, "--")}`;
    const catalogRevision = text(pool.relationship_catalog_revision, "");
    byId("limit-up-pool-status").textContent = `${
      pool.quality === "accepted" ? "真实归属已完整匹配" : "部分股票归属或主营待核验"
    } · 当期题材归因 ${Number(pool.market_attributed_count || 0)}只 · 归属库 ${
      catalogRevision ? catalogRevision.slice(0, 8) : "不可用"
    } · 快照 ${
      formatTimestamp(pool.source_as_of)
    }`;
    byId("limit-up-pool-button-count").textContent = String(pool.pool_total);
    renderLimitUpPoolCategories(pool);

    const filtered = asArray(pool.items)
      .filter((item) => (
        state.limitUpPoolCategory === "all"
        || limitUpPoolCategoryKey(item) === state.limitUpPoolCategory
      ))
      .sort((left, right) => {
        const leftBoard = Number.isInteger(left.board_count) ? left.board_count : 0;
        const rightBoard = Number.isInteger(right.board_count) ? right.board_count : 0;
        if (leftBoard !== rightBoard) return rightBoard - leftBoard;
        const leftTime = Date.parse(text(left.first_sealed_at, ""));
        const rightTime = Date.parse(text(right.first_sealed_at, ""));
        if (Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime !== rightTime) {
          return leftTime - rightTime;
        }
        return text(left.instrument_id, "").localeCompare(text(right.instrument_id, ""));
      });
    const board = byId("limit-up-pool-board");
    if (!filtered.length) {
      board.replaceChildren(createElement("p", "limit-up-pool-empty", "当前分类没有股票。"));
      return;
    }
    const groups = new Map();
    filtered.forEach((item) => {
      const key = Number.isInteger(item.board_count) ? item.board_count : 0;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(item);
    });
    const sections = [...groups.entries()].map(([height, items]) => {
      const section = createElement("section", "limit-up-board-group");
      section.appendChild(createElement(
        "div",
        "limit-up-board-height",
        height > 0 ? `${height}板` : "历史日榜\n不可用",
      ));
      const grid = createElement("div", "limit-up-stock-grid");
      items.forEach((item) => grid.appendChild(limitUpPoolCard(item)));
      section.appendChild(grid);
      return section;
    });
    board.replaceChildren(...sections);
  }

  function renderLimitUpPoolPending(message) {
    byId("limit-up-pool-status").textContent = message;
    byId("limit-up-pool-board").replaceChildren(
      createElement("p", "limit-up-pool-empty", message),
    );
  }

  async function fetchLatestAvailableLimitUpPool(summary) {
    const notAfter = text(summary?.as_of, "");
    const expectedTradeDate = notAfter.slice(0, 10);
    if (!notAfter || !/^\d{4}-\d{2}-\d{2}$/.test(expectedTradeDate)) {
      throw new Error("当前快照时间不可用于读取上一版涨停池");
    }
    const query = new URLSearchParams({ not_after: notAfter });
    const response = await fetch(`${LIMIT_UP_POOL_LATEST_ENDPOINT}?${query}`, {
      cache: "no-cache",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw await marketWatchResponseError(response);
    const payload = await response.json();
    const actualRevision = text(payload?.source_snapshot_revision, "");
    if (!isRevision(actualRevision)) {
      throw new Error("上一版涨停池缺少真实快照版本");
    }
    const pool = validateLimitUpPool(payload, actualRevision);
    const poolAsOf = Date.parse(text(pool.source_as_of, ""));
    const cutoff = Date.parse(notAfter);
    if (
      pool.trade_date !== expectedTradeDate
      || !Number.isFinite(poolAsOf)
      || !Number.isFinite(cutoff)
      || poolAsOf > cutoff
    ) {
      throw new Error("上一版涨停池越过当前快照时点");
    }
    return pool;
  }

  async function fetchLimitUpPool({ showPending = true } = {}) {
    const summary = state.lastSummary;
    const revision = summary?.source_snapshot_revision;
    const hasPreviousPool = Boolean(state.limitUpPool);
    if (!isRevision(revision)) {
      if (showPending) {
        if (hasPreviousPool) {
          byId("limit-up-pool-status").textContent = "等待新的真实盘面快照…当前继续显示上一版涨停池";
        } else {
          renderLimitUpPoolPending("等待首份真实盘面快照");
        }
      }
      return;
    }
    if (state.limitUpPoolFetchInFlight) return;
    if (state.limitUpPoolRevision === revision && state.limitUpPool) {
      renderLimitUpPool(state.limitUpPool);
      return;
    }
    state.limitUpPoolFetchInFlight = true;
    if (showPending) {
      if (hasPreviousPool) {
        byId("limit-up-pool-status").textContent = "正在刷新…当前继续显示上一版涨停池";
      } else {
        renderLimitUpPoolPending("正在读取与当前快照绑定的实时涨停状态…");
      }
    }
    try {
      const query = new URLSearchParams({ source_snapshot_revision: revision });
      const response = await fetch(`${LIMIT_UP_POOL_ENDPOINT}?${query}`, {
        cache: "no-cache",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw await marketWatchResponseError(response);
      const pool = validateLimitUpPool(await response.json(), revision);
      if (state.lastSummary?.source_snapshot_revision !== revision) return;
      state.limitUpPool = pool;
      state.limitUpPoolRevision = revision;
      renderLimitUpPool(pool);
    } catch (error) {
      const message = text(error?.message, "实时涨停状态暂不可用");
      let recoveredPreviousPool = null;
      if (error?.status === 503 && !hasPreviousPool) {
        try {
          recoveredPreviousPool = await fetchLatestAvailableLimitUpPool(summary);
        } catch (_fallbackError) {
          recoveredPreviousPool = null;
        }
      }
      if (state.lastSummary?.source_snapshot_revision !== revision) return;
      if (recoveredPreviousPool) {
        state.limitUpPool = recoveredPreviousPool;
        state.limitUpPoolRevision = recoveredPreviousPool.source_snapshot_revision;
        renderLimitUpPool(recoveredPreviousPool);
        if (recoveredPreviousPool.source_snapshot_revision !== revision) {
          byId("limit-up-pool-status").textContent = (
            "精确版本准备中 · 当前显示同交易日上一版涨停池"
          );
        }
      } else if (hasPreviousPool) {
        if (showPending || byId("limit-up-pool-dialog").open) {
          byId("limit-up-pool-status").textContent = `${message} · 继续显示上一版涨停池`;
        }
      } else if (showPending || byId("limit-up-pool-dialog").open) {
        renderLimitUpPoolPending(message);
      } else {
        byId("limit-up-pool-button-count").textContent = "--";
      }
      if (error?.status === 503 && byId("limit-up-pool-dialog").open) {
        window.clearTimeout(state.limitUpPoolRetryTimer);
        state.limitUpPoolRetryTimer = window.setTimeout(fetchLimitUpPool, 5_000);
      }
    } finally {
      state.limitUpPoolFetchInFlight = false;
    }
  }

  function openLimitUpPoolDialog() {
    const dialog = byId("limit-up-pool-dialog");
    if (!dialog.open) dialog.showModal();
    fetchLimitUpPool();
  }

  function closeLimitUpPoolDialog() {
    window.clearTimeout(state.limitUpPoolRetryTimer);
    state.limitUpPoolRetryTimer = null;
    const dialog = byId("limit-up-pool-dialog");
    if (dialog.open) dialog.close();
    byId("limit-up-pool-open-button").focus();
  }

  function validateManualPortfolio(payload) {
    if (
      !payload
      || payload.contract !== "manual_portfolio.v1"
      || payload.schema_version !== 1
      || payload.enabled_limit !== 40
      || !isRevision(payload.revision)
      || !Array.isArray(payload.items)
    ) {
      throw new Error("手动持仓契约不匹配");
    }
    return payload;
  }

  function validateManualPortfolioMarket(payload) {
    if (
      !payload
      || payload.contract !== "manual_portfolio_market_snapshot.v1"
      || payload.schema_version !== 1
      || !isRevision(payload.portfolio_revision)
      || !isRevision(payload.snapshot_revision)
      || !Array.isArray(payload.items)
      || !Array.isArray(payload.alerts)
    ) {
      throw new Error("持仓行情契约不匹配");
    }
    return payload;
  }

  async function readManualJson(endpoint) {
    const response = await fetch(endpoint, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw await marketWatchResponseError(response);
    return response.json();
  }

  async function postManualJson(endpoint, payload) {
    const response = await fetch(endpoint, {
      method: "POST",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw await marketWatchResponseError(response);
    return response.json();
  }

  function manualPortfolioQualityLabel(status) {
    return {
      accepted: "新鲜 / 可观察",
      degraded: "降级 / 抑制提醒",
      stale: "过期 / 抑制提醒",
      unavailable: "不可用",
      disabled: "已停用",
    }[status] || "尚无行情";
  }

  function setManualPortfolioStatus(message) {
    byId("manual-portfolio-status").textContent = message;
    byId("manual-portfolio-launch-status").textContent = message;
  }

  function manualPortfolioDisplayStatus(quote) {
    const priceStatus = quote?.status || "unavailable";
    if (priceStatus !== "accepted") return priceStatus;
    const changeStatus = text(quote?.session_change_status, "");
    if (!changeStatus) return finiteNumber(quote?.session_change_pct) === null
      ? "degraded"
      : priceStatus;
    if (changeStatus === "stale") return "stale";
    if (changeStatus === "unavailable" || changeStatus === "degraded") return "degraded";
    return priceStatus;
  }

  function manualPortfolioQualityDetail(quote, entry) {
    if (!quote) return entry.enabled ? "等待采集进程物化" : "已停用，不请求行情";
    if (quote.reason) return quote.reason;
    const changeStatus = text(quote.session_change_status, "");
    if (changeStatus === "stale") return "价格轨迹新鲜，但标准实时涨幅已过期";
    if (changeStatus === "unavailable") return "价格轨迹可用，但标准实时涨幅不可用";
    if (changeStatus === "degraded") return "标准实时涨幅证据不完整";
    return "价格与标准实时涨幅均可核验";
  }

  function renderManualPortfolioSummary(portfolio, market, marketMatches, quoteMap) {
    const entries = asArray(portfolio.items);
    const enabledEntries = entries.filter((entry) => entry.enabled);
    const enabledIds = new Set(enabledEntries.map((entry) => entry.instrument_id));
    const freshCount = marketMatches
      ? asArray(market.items).filter((item) => (
        enabledIds.has(item.instrument_id) && manualPortfolioDisplayStatus(item) === "accepted"
      )).length
      : 0;
    const alertCount = marketMatches ? asArray(market.alerts).length : 0;
    byId("manual-portfolio-summary-enabled").textContent = `${portfolio.enabled_count} / ${portfolio.enabled_limit}`;
    byId("manual-portfolio-summary-fresh").textContent = marketMatches
      ? `${freshCount} / ${portfolio.enabled_count}`
      : `0 / ${portfolio.enabled_count}`;
    byId("manual-portfolio-summary-alerts").textContent = String(alertCount);

    const previewEntries = (enabledEntries.length ? enabledEntries : entries).slice(0, 3);
    const preview = byId("manual-portfolio-preview");
    if (!previewEntries.length) {
      preview.replaceChildren(createElement("p", "empty-state", "尚未手工添加证券代码。"));
      return;
    }
    const cards = previewEntries.map((entry) => {
      const quote = marketMatches ? quoteMap.get(entry.instrument_id) : null;
      const card = createElement("article", "manual-portfolio-preview__item");
      const identity = createElement("div", "manual-portfolio-preview__identity");
      identity.append(
        createElement("strong", "", entry.instrument_id),
        createElement("small", "", entry.display_name || (entry.enabled ? "名称未提供" : "已停用")),
      );
      const quoteStatus = quote ? manualPortfolioDisplayStatus(quote) : (entry.enabled ? "unavailable" : "disabled");
      const marketBox = createElement("div", "manual-portfolio-preview__market");
      const marketValue = createElement(
        "strong",
        "",
        quote ? `${formatLevel(quote.last_price)} · ${formatChangePct(quote.session_change_pct)}` : "--",
      );
      if (quote) setTone(marketValue, quote.session_change_pct);
      marketBox.append(
        marketValue,
        createElement("small", `quality-${quoteStatus}`, manualPortfolioQualityLabel(quoteStatus)),
      );
      card.append(identity, marketBox);
      return card;
    });
    if ((enabledEntries.length ? enabledEntries : entries).length > previewEntries.length) {
      cards.push(createElement(
        "p",
        "manual-portfolio-preview__more",
        `另有 ${(enabledEntries.length ? enabledEntries : entries).length - previewEntries.length} 个代码，打开后查看`,
      ));
    }
    preview.replaceChildren(...cards);
  }

  function manualPortfolioRow(entry, quote) {
    const row = document.createElement("tr");
    const identity = document.createElement("td");
    const name = createElement("div", "manual-portfolio-identity");
    name.append(
      createElement("strong", "", entry.instrument_id),
      createElement("span", "", entry.display_name || "名称未提供"),
      createElement(
        "small",
        "",
        entry.code_validation_status === "catalog_verified"
          ? `代码已入目录 · 归因 ${entry.attribution_status}`
          : "代码格式有效 · 业务归因未核验",
      ),
    );
    identity.appendChild(name);
    const market = document.createElement("td");
    const marketBox = createElement("div", "manual-portfolio-market");
    const price = createElement("strong", "", quote ? formatLevel(quote.last_price) : "--");
    const change = createElement("span", "", quote ? formatChangePct(quote.session_change_pct) : "等待采集");
    if (quote) setTone(change, quote.session_change_pct);
    marketBox.append(price, change);
    market.appendChild(marketBox);
    const quality = document.createElement("td");
    const status = quote ? manualPortfolioDisplayStatus(quote) : (entry.enabled ? "unavailable" : "disabled");
    quality.append(
      createElement("strong", `manual-portfolio-quality quality-${status}`, manualPortfolioQualityLabel(status)),
      createElement("small", "manual-portfolio-quality-detail", manualPortfolioQualityDetail(quote, entry)),
    );
    const note = createElement("td", "manual-portfolio-note", entry.note || "--");
    const actions = document.createElement("td");
    const controls = createElement("div", "manual-portfolio-actions");
    const toggle = createElement("button", "button button-secondary", entry.enabled ? "停用" : "启用");
    toggle.type = "button";
    toggle.dataset.manualPortfolioAction = "toggle";
    toggle.dataset.instrumentId = entry.instrument_id;
    toggle.dataset.enabled = String(!entry.enabled);
    const remove = createElement("button", "text-button manual-portfolio-delete", "删除");
    remove.type = "button";
    remove.dataset.manualPortfolioAction = "delete";
    remove.dataset.instrumentId = entry.instrument_id;
    const analysis = createElement("button", "button button-secondary", "次日分析");
    analysis.type = "button";
    analysis.dataset.manualPortfolioAction = "analysis";
    analysis.dataset.instrumentId = entry.instrument_id;
    controls.append(
      createElement("span", entry.enabled ? "portfolio-enabled" : "portfolio-disabled", entry.enabled ? "已启用" : "已停用"),
      analysis,
      toggle,
      remove,
    );
    actions.appendChild(controls);
    row.append(identity, market, quality, note, actions);
    return row;
  }

  function renderManualPortfolio() {
    const portfolio = state.manualPortfolio;
    if (!portfolio) return;
    const market = state.manualPortfolioMarket;
    const marketMatches = market?.portfolio_revision === portfolio.revision;
    const quoteMap = new Map(
      asArray(marketMatches ? market.items : []).map((item) => [item.instrument_id, item]),
    );
    const rows = asArray(portfolio.items).map((entry) => (
      manualPortfolioRow(entry, quoteMap.get(entry.instrument_id))
    ));
    renderManualPortfolioSummary(portfolio, market, marketMatches, quoteMap);
    byId("manual-portfolio-table-body").replaceChildren(...(
      rows.length
        ? rows
        : [(() => {
          const row = document.createElement("tr");
          const cell = createElement("td", "manual-portfolio-empty", "尚未手工添加证券代码。");
          cell.colSpan = 5;
          row.appendChild(cell);
          return row;
        })()]
    ));
    const generation = state.manualPortfolioOutlookGeneration;
    const waitingForMarket = generation?.phase === "waiting_for_market";
    const marketStatus = marketMatches
      ? `行情 ${formatTimestamp(market.generated_at)}`
      : waitingForMarket
        ? "前瞻已排队，等待对应版本行情"
        : "等待对应版本行情";
    byId("manual-portfolio-status").textContent = `${portfolio.enabled_count} / ${portfolio.enabled_limit} 已启用 · ${marketStatus}`;
    byId("manual-portfolio-launch-status").textContent = marketStatus;
    byId("manual-portfolio-outlook-button").disabled = (
      state.manualPortfolioMutationInFlight
      || portfolio.enabled_count === 0
      || new Set(["queued", "running"]).has(generation?.state)
    );
    byId("manual-portfolio-outlook-button").textContent = waitingForMarket
      ? "已排队等待行情"
      : marketMatches
        ? "生成条件式前瞻"
        : "排队生成前瞻";
    renderManualPortfolioAlerts(asArray(market?.alerts));
  }

  function renderManualPortfolioAlerts(alerts) {
    const target = byId("manual-portfolio-alerts");
    if (!alerts.length) {
      target.replaceChildren(createElement("p", "empty-state", "暂无新鲜样本触发的观察提醒。"));
      return;
    }
    target.replaceChildren(...alerts.map((alert) => {
      const card = createElement("article", "manual-portfolio-alert");
      card.append(
        createElement("strong", "", alert.title),
        createElement("p", "", alert.message),
        createElement("small", "", `证据 ${asArray(alert.evidence).map((item) => `${formatTimestamp(item.observed_at)} ${formatLevel(item.price)}`).join(" → ")} · ${alert.invalidation_condition}`),
      );
      return card;
    }));
  }

  function deliverManualPortfolioAlerts(alerts) {
    if (!state.notificationEnabled || !("Notification" in window) || Notification.permission !== "granted") return;
    let changed = false;
    alerts.forEach((alert) => {
      if (!isRevision(alert.alert_id) || state.manualPortfolioDeliveredAlerts.has(alert.alert_id)) return;
      state.manualPortfolioDeliveredAlerts.add(alert.alert_id);
      changed = true;
      showSystemNotification({
        title: alert.title,
        message: `${alert.message} ${alert.invalidation_condition}`,
        dedupe_key: `manual_portfolio:${alert.alert_id}`,
      });
    });
    if (changed) {
      const keys = [...state.manualPortfolioDeliveredAlerts].slice(-200);
      state.manualPortfolioDeliveredAlerts = new Set(keys);
      storeJson(STORAGE_KEYS.manualPortfolioDeliveredAlerts, keys);
    }
  }

  async function fetchManualPortfolio({ silent = false } = {}) {
    if (state.manualPortfolioFetchInFlight) return;
    state.manualPortfolioFetchInFlight = true;
    try {
      const portfolio = validateManualPortfolio(await readManualJson(MANUAL_PORTFOLIO_ENDPOINT));
      let market = null;
      try {
        market = validateManualPortfolioMarket(await readManualJson(MANUAL_PORTFOLIO_MARKET_ENDPOINT));
      } catch (error) {
        if (error?.status !== 503) throw error;
      }
      state.manualPortfolio = portfolio;
      state.manualPortfolioMarket = market;
      renderManualPortfolio();
      if (market?.portfolio_revision === portfolio.revision) {
        deliverManualPortfolioAlerts(asArray(market.alerts));
        fetchManualPortfolioIntradayAnalysis({ silent: true });
      }
      if (!state.manualPortfolioOutlook) fetchManualPortfolioOutlook({ silent: true });
      if (!state.manualPortfolioOutlookGeneration) pollManualPortfolioOutlook();
    } catch (error) {
      if (!silent) setManualPortfolioStatus(text(error?.message, "手动持仓暂不可用"));
    } finally {
      state.manualPortfolioFetchInFlight = false;
    }
  }

  async function mutateManualPortfolio(command) {
    if (state.manualPortfolioMutationInFlight) return;
    state.manualPortfolioMutationInFlight = true;
    setManualPortfolioStatus("正在保存手动列表…");
    try {
      await postManualJson(MANUAL_PORTFOLIO_ENDPOINT, command);
      if (command.action === "add") byId("manual-portfolio-form").reset();
      state.manualPortfolioMarket = null;
      state.manualPortfolioIntradayAnalysis = null;
      state.manualPortfolioOutlook = null;
      state.manualPortfolioOutlookGeneration = null;
      await fetchManualPortfolio();
    } catch (error) {
      setManualPortfolioStatus(text(error?.message, "保存失败"));
    } finally {
      state.manualPortfolioMutationInFlight = false;
      renderManualPortfolio();
    }
  }

  function manualPortfolioOutlookCard(item, marketContext) {
    const card = createElement("article", `manual-portfolio-outlook-card outlook-${item.status}`);
    if (marketContext) {
      card.append(
        createElement("strong", "", `次日盘面基准 · ${text(marketContext.bias, "uncertain")} / ${text(marketContext.confidence, "abstain")}`),
        createElement("p", "", marketContext.thesis),
        createElement("p", "", `预计形态：${marketContext.expected_shape}`),
      );
    }
    card.append(
      createElement(
        "strong",
        "manual-portfolio-outlook-headline",
        text(item.headline, item.status === "conditional" ? "条件式次日分析" : "证据不足"),
      ),
      createElement("small", "", `${text(item.display_name, "名称未提供")} · ${item.instrument_id}`),
      ...asArray(item.evidence_digest).map((value) => (
        createElement("p", "manual-portfolio-evidence", `已知事实：${value}`)
      )),
      createElement("p", "manual-portfolio-sector", text(
        item.sector_interpretation,
        "本地关系库没有可用归属；不使用供应商板块名称补位。",
      )),
      createElement("p", "", `明日主问题：${item.next_session}`),
      createElement("p", "", `未来 2–5 日：${item.next_2_to_5_sessions}`),
    );
    const plan = item.price_plan;
    if (plan) {
      const pullback = asArray(plan.pullback_observation_zone);
      const pressure = asArray(plan.pressure_observation_zone);
      card.append(
        createElement(
          "p",
          "manual-portfolio-price-plan",
          `回撤观察区 ${formatLevel(pullback[0])}–${formatLevel(pullback[1])} · 压力观察区 ${formatLevel(pressure[0])}–${formatLevel(pressure[1])} · 中位 ${formatLevel(plan.previous_midpoint)} · 风险参考 ${formatLevel(plan.risk_reference)}`,
        ),
        createElement("small", "", plan.note),
      );
    }
    asArray(item.opening_scenarios).forEach((scenario) => {
      card.append(createElement("p", "manual-portfolio-scenario", `开盘情景：${scenario}`));
    });
    asArray(item.market_scenarios).forEach((scenario) => {
      card.append(createElement("p", "manual-portfolio-scenario", `盘面联动：${scenario}`));
    });
    if (asArray(item.tomorrow_checkpoints).length) {
      card.append(createElement("strong", "manual-portfolio-checkpoint-title", "明早按这个顺序复核"));
      asArray(item.tomorrow_checkpoints).forEach((checkpoint) => {
        card.append(createElement("p", "manual-portfolio-scenario", checkpoint));
      });
    }
    card.append(createElement(
      "small",
      "",
      `确认：${stringList(item.confirmation_conditions).join("；") || "无"} · 失效：${stringList(item.invalidation_conditions).join("；") || "无"}`,
    ));
    return card;
  }

  function renderManualPortfolioAnalysisArchive() {
    const history = state.manualPortfolioAnalysisHistory;
    const tab = state.manualPortfolioAnalysisTab;
    const pages = asArray(tab === "outlook" ? history?.outlook_pages : history?.review_pages);
    const index = Math.min(state.manualPortfolioAnalysisPage[tab] || 0, Math.max(0, pages.length - 1));
    state.manualPortfolioAnalysisPage[tab] = index;
    const page = pages[index];
    const outlookTarget = byId("manual-portfolio-analysis-outlook");
    const reviewTarget = byId("manual-portfolio-analysis-review");
    outlookTarget.hidden = tab !== "outlook";
    reviewTarget.hidden = tab !== "review";
    document.querySelectorAll("[data-manual-portfolio-analysis-tab]").forEach((button) => {
      const active = button.dataset.manualPortfolioAnalysisTab === tab;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", String(active));
    });
    const dateSelect = byId("manual-portfolio-analysis-date-select");
    const dateOptions = pages.map((candidate) => {
      const option = document.createElement("option");
      const sourceDate = tab === "outlook"
        ? candidate.source_trading_date
        : candidate.reviewed_outlook_date;
      option.value = sourceDate;
      option.textContent = tab === "outlook"
        ? sourceDate
        : `${sourceDate} → ${candidate.realized_trading_date}`;
      return option;
    });
    if (!dateOptions.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "暂无档案";
      dateOptions.push(option);
    }
    dateSelect.replaceChildren(...dateOptions);
    dateSelect.disabled = !page;
    dateSelect.value = page
      ? (tab === "outlook" ? page.source_trading_date : page.reviewed_outlook_date)
      : "";
    byId("manual-portfolio-analysis-past").disabled = !page || index >= pages.length - 1;
    byId("manual-portfolio-analysis-future").disabled = !page || index === 0;
    if (!page) {
      byId("manual-portfolio-analysis-date").textContent = "暂无档案";
      const target = tab === "outlook" ? outlookTarget : reviewTarget;
      target.replaceChildren(createElement(
        "p",
        "empty-state",
        tab === "outlook" ? "尚无按交易日保存的次日分析。" : "尚无可核验的前一日分析回顾。",
      ));
      return;
    }
    if (tab === "outlook") {
      byId("manual-portfolio-analysis-date").textContent = `基于 ${page.source_trading_date} 收盘 · ${index + 1} / ${pages.length}`;
      outlookTarget.replaceChildren(
        createElement("p", "manual-portfolio-archive-time", `生成时间 ${formatTimestamp(page.generated_at)}`),
        manualPortfolioOutlookCard(page.analysis, page.market_context),
      );
    } else {
      const item = page.review;
      const outcomeLabel = {
        conditions_met: "收盘确认",
        mixed: "只到达、未确认",
        invalidated: "原方案失效",
        not_evaluable: "证据不足",
      }[item.outcome] || item.outcome;
      byId("manual-portfolio-analysis-date").textContent = `复核 ${page.reviewed_outlook_date} → ${page.realized_trading_date} · ${index + 1} / ${pages.length}`;
      const card = createElement("article", `manual-portfolio-outlook-card review-${item.outcome}`);
      card.append(
        createElement("strong", "", text(item.headline, `复核结果 · ${outcomeLabel}`)),
        createElement("p", "", item.observation),
        ...asArray(item.evidence).map((evidence) => createElement("p", "manual-portfolio-scenario", evidence)),
        item.worked ? createElement("p", "manual-portfolio-review-worked", `昨天有用的部分：${item.worked}`) : document.createTextNode(""),
        item.missed ? createElement("p", "manual-portfolio-review-missed", `昨天漏掉的部分：${item.missed}`) : document.createTextNode(""),
        item.next_adjustment ? createElement("p", "manual-portfolio-review-adjustment", `下一次怎么改：${item.next_adjustment}`) : document.createTextNode(""),
        createElement("small", "", `自我总结：${page.self_summary}`),
        createElement("small", "", `限制：${stringList(item.limitations).join("；") || "无"}`),
      );
      reviewTarget.replaceChildren(card);
    }
  }

  function renderManualPortfolioIntradayAnalysis(payload) {
    const target = byId("manual-portfolio-intraday-analysis");
    const analyses = asArray(payload?.analyses);
    if (!analyses.length) {
      target.replaceChildren(createElement("p", "empty-state", "当前没有可分析的已启用代码。"));
      return;
    }
    const table = createElement("table", "manual-portfolio-intraday-log");
    const head = document.createElement("thead");
    const headingRow = document.createElement("tr");
    ["分析时间", "股票数", "分析内容"].forEach((label) => {
      const cell = createElement("th", "", label);
      cell.scope = "col";
      headingRow.append(cell);
    });
    head.append(headingRow);
    const body = document.createElement("tbody");
    analyses.forEach((analysis) => {
      const items = asArray(analysis.items);
      const row = document.createElement("tr");
      row.append(
        createElement("td", "manual-portfolio-intraday-time", formatTimestamp(analysis.generated_at)),
        createElement("td", "", String(items.length)),
      );
      const detailCell = document.createElement("td");
      const details = createElement("details", "manual-portfolio-intraday-entry");
      details.append(createElement("summary", "", `展开 ${items.length} 只股票的本次分析`));
      items.forEach((item) => {
        const card = createElement("article", `manual-portfolio-intraday-item outlook-${item.status}`);
        card.append(
          createElement("strong", "", `${item.display_name || "名称未提供"} · ${item.instrument_id}`),
          createElement("p", "", item.current_observation),
          createElement("small", "", `确认：${stringList(item.confirmation_conditions).join("；") || "无"} · 失效：${stringList(item.invalidation_conditions).join("；") || "无"}`),
        );
        details.append(card);
      });
      detailCell.append(details);
      row.append(detailCell);
      body.append(row);
    });
    table.append(head, body);
    target.replaceChildren(table);
  }

  async function fetchManualPortfolioIntradayAnalysis({ silent = false } = {}) {
    try {
      const payload = await readManualJson(MANUAL_PORTFOLIO_INTRADAY_HISTORY_ENDPOINT);
      if (
        payload?.contract !== "manual_portfolio_intraday_analysis_history.v1"
        || asArray(payload.analyses).some((item) => item?.contract !== "manual_portfolio_intraday_analysis.v1")
      ) {
        throw new Error("盘中分析契约不匹配");
      }
      state.manualPortfolioIntradayAnalysis = payload;
      renderManualPortfolioIntradayAnalysis(payload);
      const latest = asArray(payload.analyses).at(-1);
      byId("manual-portfolio-intraday-analysis-status").textContent = (
        `显示最近 ${asArray(payload.analyses).length} 次 · 最新 ${formatTimestamp(latest?.generated_at)}`
      );
    } catch (error) {
      if (!silent || error?.status === 503) {
        byId("manual-portfolio-intraday-analysis-status").textContent = text(
          error?.message,
          "盘中分析暂不可用",
        );
      }
    }
  }

  async function fetchManualPortfolioOutlook({ silent = false } = {}) {
    try {
      const payload = await readManualJson(MANUAL_PORTFOLIO_OUTLOOK_ENDPOINT);
      if (payload?.contract !== "manual_portfolio_outlook.v1") throw new Error("条件式前瞻契约不匹配");
      state.manualPortfolioOutlook = payload;
      byId("manual-portfolio-outlook-status").textContent = `已生成 ${formatTimestamp(payload.generated_at)}`;
      if (state.manualPortfolioAnalysisInstrumentId) {
        fetchManualPortfolioAnalysisHistory(state.manualPortfolioAnalysisInstrumentId, { silent: true });
      }
    } catch (error) {
      if (!silent) byId("manual-portfolio-outlook-status").textContent = text(error?.message, "前瞻暂不可用");
    }
  }

  async function pollManualPortfolioOutlook() {
    window.clearTimeout(state.manualPortfolioOutlookPollTimer);
    state.manualPortfolioOutlookPollTimer = null;
    try {
      const generation = await readManualJson(MANUAL_PORTFOLIO_OUTLOOK_GENERATION_ENDPOINT);
      state.manualPortfolioOutlookGeneration = generation;
      const readiness = generation?.readiness || {};
      if (generation.phase === "waiting_for_market") {
        const nextCollectionAt = readiness.next_collection_at
          ? formatTimestamp(readiness.next_collection_at, true)
          : "交易日历核验后";
        byId("manual-portfolio-outlook-status").textContent = (
          `已排队 · 最早 ${nextCollectionAt} 采集后自动生成；上游可用时通常约 1 分钟`
        );
      } else if (generation.state === "idle" && readiness.state === "waiting_for_market") {
        const nextCollectionAt = readiness.next_collection_at
          ? formatTimestamp(readiness.next_collection_at, true)
          : "交易日历核验后";
        byId("manual-portfolio-outlook-status").textContent = (
          `尚未排队 · 最早 ${nextCollectionAt} 采集后可生成`
        );
      } else if (generation.state === "idle" && readiness.state === "ready") {
        byId("manual-portfolio-outlook-status").textContent = "行情已就绪，可立即生成";
      } else {
        byId("manual-portfolio-outlook-status").textContent = `后台：${text(generation.phase, generation.state)}`;
      }
      renderManualPortfolio();
      if (new Set(["queued", "running"]).has(generation.state)) {
        const pollDelay = generation.phase === "waiting_for_market" ? 30_000 : 2_000;
        state.manualPortfolioOutlookPollTimer = window.setTimeout(pollManualPortfolioOutlook, pollDelay);
      } else if (generation.state === "succeeded") {
        await fetchManualPortfolioOutlook();
        renderManualPortfolio();
      } else if (generation.state === "failed") {
        byId("manual-portfolio-outlook-status").textContent = text(generation.error, "前瞻生成失败");
      }
    } catch (error) {
      byId("manual-portfolio-outlook-status").textContent = text(error?.message, "任务状态暂不可用");
    }
  }

  async function generateManualPortfolioOutlook() {
    const button = byId("manual-portfolio-outlook-button");
    button.disabled = true;
    byId("manual-portfolio-outlook-status").textContent = "正在排队…";
    try {
      const generation = await postManualJson(MANUAL_PORTFOLIO_OUTLOOK_ENDPOINT, { action: "generate" });
      state.manualPortfolioOutlookGeneration = generation;
      renderManualPortfolio();
      pollManualPortfolioOutlook();
    } catch (error) {
      byId("manual-portfolio-outlook-status").textContent = text(error?.message, "前瞻排队失败");
      renderManualPortfolio();
    }
  }

  async function fetchManualPortfolioAnalysisHistory(instrumentId, { silent = false } = {}) {
    try {
      const endpoint = `${MANUAL_PORTFOLIO_ANALYSIS_HISTORY_ENDPOINT}?instrument_id=${encodeURIComponent(instrumentId)}&limit=365`;
      const payload = await readManualJson(endpoint);
      if (payload?.contract !== "manual_portfolio_analysis_history.v1") {
        throw new Error("个股分析档案契约不匹配");
      }
      if (state.manualPortfolioAnalysisInstrumentId !== instrumentId) return;
      state.manualPortfolioAnalysisHistory = payload;
      byId("manual-portfolio-analysis-identity").textContent = `${payload.display_name || "名称未提供"} · ${payload.instrument_id}`;
      renderManualPortfolioAnalysisArchive();
    } catch (error) {
      if (!silent) {
        const target = state.manualPortfolioAnalysisTab === "review"
          ? byId("manual-portfolio-analysis-review")
          : byId("manual-portfolio-analysis-outlook");
        target.replaceChildren(createElement("p", "empty-state", text(error?.message, "个股分析档案暂不可用")));
      }
    }
  }

  function updatePollStatus() {
    const target = byId("poll-status");
    if (state.fetchInFlight) {
      target.textContent = "正在更新…";
      return;
    }
    if (!state.nextPollAt) {
      target.textContent = "等待首份盘面";
      return;
    }
    const seconds = Math.max(0, Math.ceil((state.nextPollAt - Date.now()) / 1000));
    const collectorState = text(state.collectionStatus?.collector_state, "unknown");
    const heartbeatAt = Date.parse(text(
      state.collectionStatus?.collector_heartbeat_at,
      "",
    ));
    const heartbeatFresh = Number.isFinite(heartbeatAt)
      && Date.now() - heartbeatAt <= COLLECTOR_HEARTBEAT_STALE_MS;
    const collectorHealthy = heartbeatFresh && collectorState === "running";
    const collectorLabel = collectorHealthy
      ? "采集心跳正常"
      : heartbeatFresh
        ? `采集状态 ${collectorState}`
        : "采集心跳超时";
    const cursor = state.collectionStatus?.collection_cursor
      || state.collectionStatus?.latest_published_status;
    const cursorStatus = text(cursor?.status, "");
    const collectionLabel = new Set(["retrying", "capturing", "expected"]).has(cursorStatus)
      ? "当前分钟采集/追补中"
      : cursorStatus === "unresolved"
        ? "当前分钟缺口未解决"
        : cursorStatus === "accepted_real" || cursorStatus === "repaired"
          ? "真实快照已接收"
          : "等待采集状态";
    target.textContent = `${collectorLabel} · ${collectionLabel} · ${seconds} 秒后检查`;
  }

  function selectStockSelectionTab(tabName, { focus = false } = {}) {
    const archive = objectValue(state.stockSelectionHistory);
    const definitions = stockSelectionDefinitions(archive);
    const definition = definitions.find((item) => item.strategy_id === tabName)
      || definitions[0];
    if (!definition) return;
    const requested = definition.strategy_id;
    state.stockSelectionStrategyId = requested;
    document.querySelectorAll("[data-stock-selection-tab]").forEach((button) => {
      const selected = button.dataset.stockSelectionTab === requested;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      if (selected && focus) button.focus();
    });
    document.querySelectorAll("[data-stock-selection-result-contract]").forEach((panel) => {
      const selected = panel.dataset.stockSelectionResultContract === definition.result_contract;
      panel.hidden = !selected;
      if (selected) panel.setAttribute("aria-labelledby", `stock-selection-tab-${requested}`);
    });
    renderStockSelectionStrategy(archive, requested);
  }

  function openStockSelectionDialog() {
    const dialog = byId("stock-selection-dialog");
    if (!dialog.open) dialog.showModal();
    if (!state.stockSelectionHasLoaded) fetchStockSelectionHistory();
    pollStockSelectionGeneration();
  }

  function closeStockSelectionDialog() {
    const dialog = byId("stock-selection-dialog");
    if (dialog.open) dialog.close();
    byId("stock-selection-open-button").focus();
  }

  function openManualPortfolioDialog() {
    const dialog = byId("manual-portfolio-dialog");
    if (!dialog.open) dialog.showModal();
    fetchManualPortfolio({ silent: true });
    pollManualPortfolioOutlook();
  }

  function closeManualPortfolioDialog() {
    const dialog = byId("manual-portfolio-dialog");
    if (dialog.open) dialog.close();
    byId("manual-portfolio-open-button").focus();
  }

  function openManualPortfolioAnalysis(instrumentId) {
    state.manualPortfolioAnalysisInstrumentId = instrumentId;
    state.manualPortfolioAnalysisHistory = null;
    state.manualPortfolioAnalysisTab = "outlook";
    state.manualPortfolioAnalysisPage = { outlook: 0, review: 0 };
    const entry = asArray(state.manualPortfolio?.items).find((item) => item.instrument_id === instrumentId);
    byId("manual-portfolio-analysis-identity").textContent = `${entry?.display_name || "名称未提供"} · ${instrumentId}`;
    byId("manual-portfolio-analysis-outlook").replaceChildren(createElement("p", "empty-state", "正在读取次日分析档案…"));
    const dialog = byId("manual-portfolio-analysis-dialog");
    if (!dialog.open) dialog.showModal();
    renderManualPortfolioAnalysisArchive();
    fetchManualPortfolioAnalysisHistory(instrumentId);
  }

  function closeManualPortfolioAnalysis() {
    const instrumentId = state.manualPortfolioAnalysisInstrumentId;
    const dialog = byId("manual-portfolio-analysis-dialog");
    if (dialog.open) dialog.close();
    state.manualPortfolioAnalysisInstrumentId = null;
    const returnButton = document.querySelector(
      `[data-manual-portfolio-action="analysis"][data-instrument-id="${instrumentId}"]`,
    );
    if (returnButton) returnButton.focus();
  }

  function selectManualPortfolioAnalysisTab(tab) {
    if (!new Set(["outlook", "review"]).has(tab)) return;
    state.manualPortfolioAnalysisTab = tab;
    renderManualPortfolioAnalysisArchive();
  }

  function moveManualPortfolioAnalysisPage(delta) {
    const tab = state.manualPortfolioAnalysisTab;
    state.manualPortfolioAnalysisPage[tab] = Math.max(
      0,
      (state.manualPortfolioAnalysisPage[tab] || 0) + delta,
    );
    renderManualPortfolioAnalysisArchive();
  }

  function selectManualPortfolioAnalysisDate(dateValue) {
    const tab = state.manualPortfolioAnalysisTab;
    const history = state.manualPortfolioAnalysisHistory;
    const pages = asArray(tab === "outlook" ? history?.outlook_pages : history?.review_pages);
    const index = pages.findIndex((page) => (
      tab === "outlook"
        ? page.source_trading_date === dateValue
        : page.reviewed_outlook_date === dateValue
    ));
    if (index < 0) return;
    state.manualPortfolioAnalysisPage[tab] = index;
    renderManualPortfolioAnalysisArchive();
  }

  function selectManualPortfolioLiveTab(tab) {
    const analysisActive = tab === "analysis";
    byId("manual-portfolio-live-panel-analysis").hidden = !analysisActive;
    byId("manual-portfolio-live-panel-alerts").hidden = analysisActive;
    document.querySelectorAll("[data-manual-portfolio-live-tab]").forEach((button) => {
      const active = button.dataset.manualPortfolioLiveTab === tab;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", String(active));
    });
  }

  function bindUserActions() {
    byId("refresh-button").addEventListener("click", () => fetchSnapshot({ force: true }));
      byId("collection-recovery-button").addEventListener("click", requestDailyRecovery);
      byId("trajectory-repair-button").addEventListener("click", requestIntradayTrajectoryRepair);
    byId("notification-button").addEventListener("click", handleNotificationOptIn);
    byId("sound-button").addEventListener("click", handleSoundToggle);
    byId("mute-button").addEventListener("click", handleMuteToggle);
    byId("manual-portfolio-form").addEventListener("submit", (event) => {
      event.preventDefault();
      mutateManualPortfolio({
        action: "add",
        instrument_id: byId("manual-portfolio-code").value,
        display_name: byId("manual-portfolio-name").value,
        note: byId("manual-portfolio-note").value,
      });
    });
    byId("manual-portfolio-table-body").addEventListener("click", (event) => {
      const button = event.target.closest("[data-manual-portfolio-action]");
      if (!button) return;
      const instrumentId = button.dataset.instrumentId;
      if (button.dataset.manualPortfolioAction === "analysis") {
        openManualPortfolioAnalysis(instrumentId);
      } else if (button.dataset.manualPortfolioAction === "delete") {
        if (!window.confirm(`确认从手动持仓观察中删除 ${instrumentId}？`)) return;
        mutateManualPortfolio({ action: "delete", instrument_id: instrumentId });
      } else {
        mutateManualPortfolio({
          action: "update",
          instrument_id: instrumentId,
          enabled: button.dataset.enabled === "true",
        });
      }
    });
    byId("manual-portfolio-outlook-button").addEventListener("click", generateManualPortfolioOutlook);
    byId("manual-portfolio-open-button").addEventListener("click", openManualPortfolioDialog);
    byId("manual-portfolio-close-button").addEventListener("click", closeManualPortfolioDialog);
    const manualPortfolioDialog = byId("manual-portfolio-dialog");
    manualPortfolioDialog.addEventListener("click", (event) => {
      if (event.target !== manualPortfolioDialog) return;
      closeManualPortfolioDialog();
    });
    document.querySelectorAll("[data-manual-portfolio-live-tab]").forEach((button) => {
      button.addEventListener("click", () => selectManualPortfolioLiveTab(button.dataset.manualPortfolioLiveTab));
    });
    document.querySelectorAll("[data-manual-portfolio-analysis-tab]").forEach((button) => {
      button.addEventListener("click", () => selectManualPortfolioAnalysisTab(button.dataset.manualPortfolioAnalysisTab));
    });
    byId("manual-portfolio-analysis-past").addEventListener("click", () => moveManualPortfolioAnalysisPage(1));
    byId("manual-portfolio-analysis-future").addEventListener("click", () => moveManualPortfolioAnalysisPage(-1));
    byId("manual-portfolio-analysis-date-select").addEventListener("change", (event) => {
      selectManualPortfolioAnalysisDate(event.target.value);
    });
    byId("manual-portfolio-analysis-close-button").addEventListener("click", closeManualPortfolioAnalysis);
    const manualPortfolioAnalysisDialog = byId("manual-portfolio-analysis-dialog");
    manualPortfolioAnalysisDialog.addEventListener("click", (event) => {
      if (event.target !== manualPortfolioAnalysisDialog) return;
      closeManualPortfolioAnalysis();
    });
    byId("limit-up-pool-open-button").addEventListener("click", openLimitUpPoolDialog);
    byId("limit-up-pool-close-button").addEventListener("click", closeLimitUpPoolDialog);
    const limitUpDialog = byId("limit-up-pool-dialog");
    limitUpDialog.addEventListener("click", (event) => {
      if (event.target !== limitUpDialog) return;
      closeLimitUpPoolDialog();
    });
    document.querySelectorAll("[data-flow-mode]").forEach((button) => {
      button.addEventListener("click", () => {
        if (button.disabled) return;
        const scope = button.closest("[data-sector-flow-chart-card]")?.dataset.flowScope;
        if (!scope) return;
        state.sectorFlowMode[scope] = button.dataset.flowMode === "five_day"
          ? "five_day"
          : "cumulative";
        if (state.lastSnapshot) scheduleSectorFlowRender(scope);
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
        scheduleSectorFlowRender(scope, { renderPicker: false });
      });
    });
    document.querySelectorAll("[data-sector-flow-action]").forEach((button) => {
      button.addEventListener("click", () => {
        const scope = button.dataset.flowScope;
        const payload = sectorFlowPayload(state.lastSnapshot, scope);
        if (!payload) return;
        const keys = payload.sectors.map((item) => text(item.sector_key, "")).filter(Boolean);
        const current = ensureSectorFlowSelection(payload);
        const allSelected = keys.length > 0 && keys.every((key) => current.has(key));
        const selected = button.dataset.sectorFlowAction === "select-all"
          ? allSelected ? new Set() : new Set(keys)
          : new Set(defaultSectorFlowSelection(payload));
        setSectorFlowSelection(scope, selected);
        storeJson(sectorFlowSelectionStorageKey(scope), [...selected]);
        scheduleSectorFlowRender(scope, { renderPicker: true });
      });
    });
    document.querySelectorAll("[data-sector-flow-surge-threshold]").forEach((select) => {
      select.addEventListener("change", (event) => {
        const threshold = finiteNumber(event.target.value);
        if (threshold === null || threshold <= 0) return;
        state.sectorFlowSurgeThreshold = threshold;
        storeText(STORAGE_KEYS.sectorFlowSurgeThreshold, String(threshold));
        if (state.lastSnapshot) {
          scheduleSectorFlowRender("defense", { renderPicker: false });
          scheduleSectorFlowRender("offense", { renderPicker: false });
        }
      });
    });
    document.querySelectorAll("[data-sector-flow-chart-card]").forEach((chartCard) => {
      const chartLayout = chartCard.closest(".sector-flow-layout");
      const scope = chartCard.dataset.flowScope;
      const syncChartLayout = () => {
        chartLayout.classList.toggle("is-chart-open", chartCard.open);
        if (chartCard.open) scheduleSectorFlowRender(scope, { renderPicker: false });
        else clearSectorFlowExpandedChart(scope);
      };
      chartCard.addEventListener("toggle", syncChartLayout);
      syncChartLayout();
    });
    document.addEventListener("pointerdown", (event) => {
      document.querySelectorAll(".sector-flow-picker[open]").forEach((picker) => {
        if (!picker.contains(event.target)) picker.open = false;
      });
    });
    document.addEventListener("click", () => {
      document.querySelectorAll(".sector-flow-chart-shell > svg[data-locked-sector-flow]")
        .forEach((svg) => setSectorFlowSeriesLock(svg));
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      document.querySelectorAll(".sector-flow-picker[open]").forEach((picker) => {
        picker.open = false;
      });
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
    byId("stock-selection-generate-button").addEventListener("click", generateStockSelection);
    byId("stock-macd-j-group").addEventListener("change", () => {
      renderStockSelectionStrategy(objectValue(state.stockSelectionHistory), state.stockSelectionStrategyId);
    });
    byId("stock-selection-open-button").addEventListener("click", openStockSelectionDialog);
    byId("stock-selection-close-button").addEventListener("click", closeStockSelectionDialog);
    byId("stock-selection-followup-open").addEventListener("click", () => {
      renderStockSelectionFollowup();
      byId("stock-selection-followup-dialog").showModal();
    });
    byId("stock-selection-followup-close").addEventListener("click", () => {
      byId("stock-selection-followup-dialog").close();
    });
    byId("stock-selection-followup-dialog").addEventListener("pointerdown", (event) => {
      const dialog = byId("stock-selection-followup-dialog");
      if (event.target !== dialog || event.button !== 0 || event.isPrimary === false) return;
      event.preventDefault();
      event.stopPropagation();
      dialog.close();
    });
    byId("stock-selection-followup-dialog").addEventListener("close", () => {
      byId("stock-selection-followup-open").focus();
    });
    const dialog = byId("stock-selection-dialog");
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      closeStockSelectionDialog();
    });
    byId("stock-selection-date-select").addEventListener("change", (event) => {
      state.stockSelectionTradeDate = event.target.value || null;
      fetchStockSelectionHistory({ tradeDate: state.stockSelectionTradeDate });
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
    loadPanelWhenVisible("stock-selection-section", () => {
      fetchStockSelectionHistory();
      pollStockSelectionGeneration();
    });
    loadPanelWhenVisible("daily-review-section", () => {
      fetchPostMarketReviewHistory();
      pollPostMarketReviewGeneration();
    });
    loadPanelWhenVisible("replay-section", () => {
      fetchReplayData();
    });
  }

  function renderIntradayMacdJ(payload) {
    const confirmed = payload.status === "close_confirmed" && payload.scan_kind === "close_confirmation";
    const closeScan = payload.status === "close_scanned" && payload.scan_kind === "manual_close";
    const partial = payload.status === "partial";
    const fresh = (["monitoring", "partial"].includes(payload.status) || closeScan) && payload.trade_date === shanghaiClock().date;
    byId("intraday-macd-j-status").textContent = `${text(payload.trade_date)} · ${text(payload.message)}`;
    byId("intraday-macd-j-coverage").textContent = confirmed
      ? `正式收盘指标 · 主板非 ST ${payload.eligible_count} 只 · 已核验 ${payload.evaluated_count} 只 · 命中 ${payload.matched_count} 只 · 存档 ${formatTimestamp(payload.generated_at, true)}。与选股中心使用同一份结果。`
      : payload.evaluated_count === undefined ? ""
      : `本轮请求 ${payload.requested_count ?? payload.eligible_count} 只 · 已核验 ${payload.evaluated_count} 只 · 过期行情 ${objectValue(payload.excluded_counts).stale_quote || 0} 只 · 未取得报价 ${payload.missing_quote_count || 0} 只 · 扫描 ${formatTimestamp(payload.generated_at, true)} · 下次 ${payload.next_scan_at ? formatTimestamp(payload.next_scan_at) : "下个交易日"}。历史缺失、复权变化或行情过期时不提示。`;
    const records = asArray(payload.records);
    if (!fresh) {
      byId("intraday-macd-j-toast").hidden = true;
      return;
    }
    const storageKey = `tradex.intradayMacdJ.seen.${payload.trade_date}${closeScan ? ".manual-close" : ""}`;
    state.intradayMacdJSeen = state.intradayMacdJSeen || {};
    const seen = new Set(state.intradayMacdJSeen[storageKey] || loadStoredArray(storageKey));
    if (asArray(state.intradayMacdJToastIds).some(id => !records.some(r => r.instrument_id === id && r.active))) {
      byId("intraday-macd-j-toast").hidden = true;
    }
    const newAlerts = records.filter(r => r.active && r.alerted_at && !seen.has(r.instrument_id));
    if (newAlerts.length) {
      newAlerts.forEach(r => seen.add(r.instrument_id));
      state.intradayMacdJSeen[storageKey] = [...seen];
      state.intradayMacdJToastIds = newAlerts.map(r => r.instrument_id);
      storeJson(storageKey, [...seen]);
      byId("intraday-macd-j-toast-text").textContent = `${partial ? "扫描不完整，仅在已核验范围内：" : ""}${closeScan ? "手动收盘扫描（预估）" : "MACD + J 线盘中"}命中 ${newAlerts.length} 只：${newAlerts.slice(0, 5).map(r => `${r.name} ${r.instrument_id}`).join("、")}${newAlerts.length > 5 ? "等，完整名单见扫描记录" : ""}。${closeScan ? "使用收盘价推算，等待正式指标确认。" : "收盘前可能失效。"}`;
      byId("intraday-macd-j-toast").hidden = false;
    }
  }

  async function fetchIntradayMacdJ() {
    if (state.intradayMacdJInFlight) return;
    state.intradayMacdJInFlight = true;
    try {
      const response = await fetch("/api/stock-selection/intraday-macd-j", { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || payload.contract !== "intraday_macd_j_watch.v1") throw new Error("unavailable");
      renderIntradayMacdJ(payload);
      if (byId("intraday-macd-j-history-dialog").open) fetchIntradayMacdJHistory(state.intradayMacdJHistoryDate, { silent: true });
    } catch (_) {
      byId("intraday-macd-j-status").textContent = "盘中扫描状态暂不可用，暂停新提示。";
      byId("intraday-macd-j-toast").hidden = true;
      byId("intraday-macd-j-coverage").textContent = "";
    } finally {
      state.intradayMacdJInFlight = false;
    }
  }

  function renderIntradayMacdJIndustryFilters(candidates, scan, index, target) {
    const records = stockSelectionDisplayCandidates(candidates);
    const counts = new Map();
    records.forEach(record => {
      const name = stockSelectionIndustryName(record);
      counts.set(name, (counts.get(name) || 0) + 1);
    });
    const scope = `${scan.trade_date}:${scan.last_scan_slot}`;
    if (state.intradayMacdJIndustryScope !== scope || !counts.has(state.intradayMacdJIndustry)) {
      state.intradayMacdJIndustry = null;
    }
    state.intradayMacdJIndustryScope = scope;
    const display = objectValue(state.intradayMacdJIndustryDisplay);
    const panel = createElement("div", "stock-selection-industry-filter");
    panel.append(createElement("p", "", display.as_of
      ? `行业板块：同花顺 · 目录日期 ${display.as_of}。历史扫描按该目录归组；缺少归属时列为未分类。`
      : "行业板块目录暂不可用，缺少归属的股票列为未分类。"));
    const tags = createElement("div", "stock-selection-industry-tags");
    tags.setAttribute("role", "group");
    tags.setAttribute("aria-label", "扫描行业筛选");
    [[null, records.length], ...counts].forEach(([industry, count]) => {
      const button = createElement("button", "stock-selection-industry-tag",
        `${industry === null ? "全部" : industry} ${formatCount(count)}`);
      button.type = "button";
      button.setAttribute("aria-pressed", String(state.intradayMacdJIndustry === industry));
      button.addEventListener("click", () => {
        state.intradayMacdJIndustry = industry;
        selectIntradayMacdJScan(index);
        target.querySelector('[aria-pressed="true"]')?.focus();
      });
      tags.append(button);
    });
    const visible = records.filter(record => state.intradayMacdJIndustry === null
      || stockSelectionIndustryName(record) === state.intradayMacdJIndustry);
    panel.append(tags, createElement("p", "", `${state.intradayMacdJIndustry || "全部行业"} · 显示 ${visible.length} / ${records.length} 只`));
    target.append(panel);
    return visible;
  }

  function selectIntradayMacdJScan(index) {
    const scan = asArray(state.intradayMacdJScans)[index];
    if (!scan) return;
    state.intradayMacdJSelectedSlot = scan.last_scan_slot;
    [...byId("intraday-macd-j-history-tabs").children].forEach((button, i) => {
      button.setAttribute("aria-selected", String(i === index));
      button.tabIndex = i === index ? 0 : -1;
    });
    const target = byId("intraday-macd-j-history-result");
    target.setAttribute("aria-labelledby", `intraday-macd-j-scan-tab-${index}`);
    const renderKey = () => {
      const display = objectValue(state.intradayMacdJIndustryDisplay);
      const names = objectValue(display.names_by_instrument);
      return JSON.stringify([scan, display.as_of,
        asArray(scan.scan_candidates).map(row => names[row.instrument_id]), state.intradayMacdJIndustry]);
    };
    if (state.intradayMacdJRenderedKey === renderKey() && target.children.length) return;
    target.replaceChildren();
    target.append(createElement("p", "", `${formatTimestamp(scan.last_scan_slot, true)} · ${text(scan.message)}`));
    if (scan.scan_kind === "manual_close") {
      target.append(createElement("p", "", "此记录使用收盘价推算指标，属于预估；正式名单见收盘确认。"));
    }
    target.append(createElement("p", "", `已核验 ${scan.evaluated_count ?? 0} / 请求 ${scan.requested_count ?? 0} 只 · 未取得报价 ${scan.missing_quote_count ?? 0} 只 · 数据源 ${text(objectValue(scan.source_metadata).provider)}`));
    const candidates = stockSelectionIndustryPayload({ candidates: scan.scan_candidates },
      objectValue(state.intradayMacdJIndustryDisplay)).candidates;
    target.append(createElement("p", "", scan.status === "partial"
      ? `扫描不完整：已核验范围内命中 ${candidates.length} 只，不代表全量结果（含此前已提示的股票）。`
      : ["monitoring", "close_scanned", "close_confirmed"].includes(scan.status)
        ? `本次命中 ${candidates.length} 只（含此前已提示的股票）。` : "本次未完成可靠扫描，不代表没有股票符合条件。"));
    const visibleCandidates = renderIntradayMacdJIndustryFilters(candidates, scan, index, target);
    const scroll = createElement("div", "stock-selection-table-scroll");
    scroll.tabIndex = 0;
    scroll.setAttribute("aria-label", "MACD J 扫描命中股票表");
    const table = createElement("table", "stock-selection-table");
    const head = createElement("thead");
    const header = createElement("tr");
    const isClose = ["manual_close", "close_confirmation"].includes(scan.scan_kind);
    ["股票", "行业板块", isClose ? "收盘价" : "参考价", "信号分组", "金叉位置", "J 拐头与低位标签", "指标证据", "数据时间"].forEach(label => {
      const cell = createElement("th", "", label);
      cell.scope = "col";
      header.append(cell);
    });
    head.append(header);
    const body = createElement("tbody");
    const number = value => finiteNumber(value) === null ? "--" : Number(value).toFixed(4);
    if (!visibleCandidates.length) {
      const row = createElement("tr");
      const cell = createElement("td", "stock-selection-table-empty",
        scan.status === "partial" ? "已核验范围内未命中；扫描不完整，不能判定全量结果。"
          : ["monitoring", "close_scanned", "close_confirmed"].includes(scan.status)
            ? "本次扫描没有命中股票。" : "本次未完成可靠扫描，不能判定是否命中。");
      cell.colSpan = 8;
      row.append(cell);
      body.append(row);
    }
    visibleCandidates.forEach(record => {
      const row = createElement("tr");
      const name = createElement("td");
      name.append(stockSelectionNameLink(record), createElement("small", "", record.instrument_id));
      const turn = createElement("td", "", text(record.j_turn_date));
      turn.append(createElement("div", "", `J 谷值 ${number(record.j_trough)}`),
        createElement("small", "", asArray(record.low_j_tags).join(" · ") || "无低位标签"));
      const evidence = createElement("td");
      const details = createElement("details");
      details.append(createElement("summary", "", `DIF ${number(record.dif)} / DEA ${number(record.dea)} · J ${number(record.j)}`),
        createElement("p", "", `K ${number(record.k)} / D ${number(record.d)} / J ${number(record.j)}`));
      evidence.append(details);
      row.append(name, createElement("td", "", stockSelectionIndustryName(record)),
        createElement("td", "", formatLevel(record.price)),
        createElement("td", "", text(record.signal_group)),
        createElement("td", "", text(record.zero_axis_zone)), turn, evidence,
        createElement("td", "", isClose ? `${record.price_trade_date} 收盘日线` : `${formatTimestamp(record.observed_at, true)}${record.quote_provider ? " · " + record.quote_provider : ""}`));
      body.append(row);
    });
    table.append(head, body);
    scroll.append(table);
    target.append(scroll);
    state.intradayMacdJRenderedKey = renderKey();
  }

  async function fetchIntradayMacdJHistory(day = null, { silent = false } = {}) {
    const requestId = (state.intradayMacdJHistoryRequestId || 0) + 1;
    state.intradayMacdJHistoryRequestId = requestId;
    state.intradayMacdJHistoryDate = day;
    if (!silent && (!state.intradayMacdJHistoryKey || day !== state.intradayMacdJLoadedDate)) {
      byId("intraday-macd-j-history-status").textContent = "正在读取扫描记录…";
    }
    try {
      const response = await fetch(`/api/stock-selection/intraday-macd-j/history${day ? `?trade_date=${encodeURIComponent(day)}` : ""}`, {cache: "no-store"});
      const payload = await response.json();
      if (requestId !== state.intradayMacdJHistoryRequestId) return;
      if (!response.ok || payload.contract !== "intraday_macd_j_history.v1"
          || (day && day !== payload.trade_date)) throw new Error("扫描记录暂不可用");
      const historyKey = JSON.stringify([payload.trade_date, payload.dates, payload.scans, payload.industry_display]);
      if (historyKey === state.intradayMacdJHistoryKey) {
        byId("intraday-macd-j-history-status").textContent = state.intradayMacdJScans.length
          ? `${payload.trade_date} · ${state.intradayMacdJScans.length} 次扫描` : "该日期暂无扫描记录";
        return;
      }
      state.intradayMacdJHistoryKey = historyKey;
      state.intradayMacdJLoadedDate = payload.trade_date;
      state.intradayMacdJHistoryDate = payload.trade_date;
      state.intradayMacdJScans = asArray(payload.scans);
      state.intradayMacdJIndustryDisplay = objectValue(payload.industry_display);
      const select = byId("intraday-macd-j-history-date");
      select.replaceChildren();
      asArray(payload.dates).forEach(date => {
        const option = createElement("option", "", date); option.value = date; select.append(option);
      });
      select.value = payload.trade_date;
      const tabs = byId("intraday-macd-j-history-tabs");
      const tabScrollLeft = tabs.scrollLeft;
      const hadTabFocus = tabs.contains?.(document.activeElement);
      tabs.replaceChildren();
      state.intradayMacdJScans.forEach((scan, index) => {
        const button = createElement("button", "", `${scan.last_scan_slot.slice(11, 16)}${scan.scan_kind === "close_confirmation" ? " 收盘确认" : scan.scan_kind === "manual_close" ? " 收盘预估" : scan.scan_kind === "manual_intraday" ? " 手动重扫" : ""} · ${scan.status === "partial" ? asArray(scan.scan_candidates).length + " 只（扫描不完整）" : ["monitoring", "close_scanned", "close_confirmed"].includes(scan.status) ? asArray(scan.scan_candidates).length + " 只" : "未完成"}`);
        button.type = "button";
        button.id = `intraday-macd-j-scan-tab-${index}`;
        button.setAttribute("role", "tab");
        button.setAttribute("aria-controls", "intraday-macd-j-history-result");
        button.addEventListener("click", () => selectIntradayMacdJScan(index));
        button.addEventListener("keydown", event => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const count = state.intradayMacdJScans.length;
          const next = event.key === "Home" ? 0 : event.key === "End" ? count-1 : (index + (event.key === "ArrowRight" ? 1 : count-1)) % count;
          selectIntradayMacdJScan(next); tabs.children[next].focus();
        });
        tabs.append(button);
      });
      const selected = state.intradayMacdJScans.findIndex(scan => scan.last_scan_slot === state.intradayMacdJSelectedSlot);
      if (state.intradayMacdJScans.length) selectIntradayMacdJScan(Math.max(0, selected));
      else byId("intraday-macd-j-history-result").replaceChildren();
      tabs.scrollLeft = tabScrollLeft;
      if (hadTabFocus) tabs.children[Math.max(0, selected)]?.focus({ preventScroll: true });
      byId("intraday-macd-j-history-status").textContent = state.intradayMacdJScans.length
        ? `${payload.trade_date} · ${state.intradayMacdJScans.length} 次扫描` : "该日期暂无扫描记录";
    } catch (error) {
      if (requestId === state.intradayMacdJHistoryRequestId) {
        byId("intraday-macd-j-history-status").textContent = text(error.message, "扫描记录暂不可用");
      }
    }
  }

  function start() {
    keepMarketWatchSurfacesVisible();
    bindUserActions();
    renderControls();
    setupDeferredPanelLoading();
    bindPollingRecovery();
    fetchSnapshot();
    fetchManualPortfolio();
    fetchIntradayMacdJ();
    byId("intraday-macd-j-dismiss").addEventListener("click", () => {
      byId("intraday-macd-j-toast").hidden = true;
    });
    byId("intraday-macd-j-history-open").addEventListener("click", () => {
      byId("intraday-macd-j-history-dialog").showModal();
      fetchIntradayMacdJHistory();
    });
    byId("intraday-macd-j-history-close").addEventListener("click", () => byId("intraday-macd-j-history-dialog").close());
    byId("intraday-macd-j-history-dialog").addEventListener("pointerdown", (event) => {
      const dialog = byId("intraday-macd-j-history-dialog");
      if (event.target !== dialog || event.button !== 0 || event.isPrimary === false) return;
      event.preventDefault();
      event.stopPropagation();
      dialog.close();
    });
    byId("intraday-macd-j-history-date").addEventListener("change", event => fetchIntradayMacdJHistory(event.target.value));
    window.setInterval(fetchIntradayMacdJ, 30_000);
    window.setInterval(runScheduledPoll, POLL_WATCHDOG_INTERVAL_MS);
    window.setInterval(updatePollStatus, 1000);
    window.setInterval(updateReviewSchedule, 30_000);
    window.setInterval(updateStockSelectionSchedule, 30_000);
    window.setInterval(() => fetchManualPortfolio({ silent: true }), 30_000);
    window.setInterval(
      () => {
        if (state.reviewHasLoaded) {
          fetchPostMarketReviewHistory({ tradeDate: state.reviewTradeDate, silent: true });
        }
      },
      REVIEW_REFRESH_INTERVAL_MS,
    );
    window.setInterval(
      () => {
        if (state.stockSelectionHasLoaded) {
          fetchStockSelectionHistory({
            tradeDate: state.stockSelectionTradeDate,
            silent: true,
          });
          pollStockSelectionGeneration();
        }
      },
      STOCK_SELECTION_REFRESH_INTERVAL_MS,
    );
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
