"""Static acceptance checks for the desktop-only market-watch page."""

from pathlib import Path
import re


WATCH_DIR = Path(__file__).parents[1] / "src" / "tradex" / "dashboard" / "watch"
HTML = (WATCH_DIR / "index.html").read_text(encoding="utf-8")
CSS = (WATCH_DIR / "styles.css").read_text(encoding="utf-8")
JS = (WATCH_DIR / "app.js").read_text(encoding="utf-8")


def test_watch_page_consumes_versioned_status_summary_and_exact_detail_endpoints():
    assert 'const COLLECTION_STATUS_ENDPOINT = "/api/market-watch/collection-status"' in JS
    assert 'const SUMMARY_ENDPOINT = "/api/market-watch/summary"' in JS
    assert 'const TRAJECTORY_ENDPOINT = "/api/market-watch/trajectory"' in JS
    assert 'payload.contract !== "market_watch_collector_envelope.v1"' in JS
    assert 'payload.contract !== "market_watch_summary.v1"' in JS
    assert 'payload.contract !== "sector_flow_trajectory_detail.v1"' in JS
    assert 'text(snapshot.contract, "market_watch.v1")' in JS
    assert "POLL_INTERVAL_MS = 15_000" in JS
    assert 'cache: "no-cache"' in JS
    assert '"/api/market-watch"' not in JS


def test_watch_page_exposes_server_side_history_and_honest_replay_evaluation():
    assert 'const HISTORY_ENDPOINT = "/api/market-watch/history"' in JS
    assert 'const EVALUATION_ENDPOINT = "/api/market-watch/evaluation"' in JS
    assert 'history.contract !== "market_watch_history.v1"' in JS
    assert 'payload.contract === "market_watch_replay_sample.v1"' in JS
    assert 'report.contract !== "market_watch_evaluation.v1"' in JS
    assert 'id="replay-date-select"' in HTML
    assert 'id="evaluation-scope-select"' in HTML
    assert 'id="replay-observed-minutes"' in HTML
    assert 'id="replay-fresh-ratio"' in HTML
    assert 'id="replay-max-gap"' in HTML
    assert 'id="replay-transition-count"' in HTML
    assert 'id="replay-reversal-count"' in HTML
    assert 'id="evaluation-verdict"' in HTML
    assert 'id="calibration-notes"' in HTML
    assert 'id="replay-timeline"' in HTML
    assert 'id="replay-alert-list"' in HTML
    assert "不会伪装成通过" in JS
    assert "REPLAY_REFRESH_INTERVAL_MS = 60_000" in JS
    assert "?days=${encodeURIComponent(requestedDays)}" in JS
    assert "renderReplayHistory(history);" in JS
    assert "renderReplayEvaluationPending" in JS
    assert "评估后台准备中" in JS
    assert "await Promise.all([" not in JS[
        JS.index("async function fetchReplayData"):JS.index("function renderSnapshot")
    ]
    assert "replayLastAttemptAt" in JS
    assert "if (silent) return" in JS
    assert "state.replayPendingRequest" in JS
    assert "innerHTML" not in JS


def test_collection_copy_describes_closing_auction_as_one_final_result():
    expected = "1 个 09:25 集合竞价结果、237 个连续交易分钟和 1 个 15:00 收盘结果"

    assert expected in HTML
    assert expected in JS


def test_replay_keeps_the_auction_result_visible_with_recent_samples():
    assert "samples.slice(-29).reverse()" in JS
    assert "if (samples.length > 29) visible.push(samples[0])" in JS
    assert "集合竞价结果" in JS
    assert "09:25竞价盘面已验收；板块净流入从09:30起算" in JS


def test_secondary_archives_load_only_when_their_sections_enter_view():
    start = re.search(r"function start\(\) \{(?P<body>.*?)\n  \}", JS, re.DOTALL)
    assert start is not None
    assert "fetchSnapshot();" in start.group("body")
    assert "setupDeferredPanelLoading();" in start.group("body")
    assert "fetchReplayData();" not in start.group("body")
    assert "fetchPostMarketReviewHistory();" not in start.group("body")
    assert 'id="daily-review-section"' in HTML
    assert 'id="replay-section"' in HTML
    assert "IntersectionObserver" in JS
    assert "state.replayHasLoaded" in JS
    assert "state.reviewHasLoaded" in JS


def test_collection_status_is_visible_but_never_used_as_numeric_snapshot():
    assert "state.collectionStatus = status" in JS
    assert "const accepted = status.latest_accepted_real" in JS
    assert "const sourceRevision = accepted.source_snapshot_revision" in JS
    assert "当前分钟采集/追补中" in JS
    assert "当前分钟缺口未解决" in JS
    assert 'response.headers.get("X-Tradex-Refresh-State")' not in JS


def test_post_close_recovery_has_audited_status_and_one_manual_command():
    assert 'const DAILY_RECOVERY_ENDPOINT = "/api/market-watch/daily-recovery"' in JS
    assert 'id="collection-recovery-heading"' in HTML
    assert 'id="collection-recovery-accepted"' in HTML
    assert 'id="collection-recovery-gaps"' in HTML
    assert 'id="collection-recovery-progress"' in HTML
    assert 'id="collection-recovery-failures"' in HTML
    assert 'id="collection-recovery-checked-at"' in HTML
    assert 'id="collection-recovery-status"' in HTML
    assert 'id="collection-recovery-error"' in HTML
    assert 'id="collection-recovery-button"' in HTML
    assert 'recovery.contract !== "market_watch_daily_recovery.v1"' in JS
    assert 'method: "POST"' in JS
    assert "state.recoveryRequestInFlight" in JS
    assert "state.recoveryRequestError = text(" in JS
    assert "recovery.latest_failure_error_code" in JS
    assert "recovery.latest_failure_error_message" in JS
    assert "recovery.latest_attempt_minute_bucket" in JS
    assert "recovery.failed_attempts" in JS
    assert "recovery.last_error_message" in JS
    assert "下次自动重试" in JS
    assert "不会用当前值伪造历史" in JS
    assert ".collection-recovery-error" in CSS
    assert ".collection-recovery-strip.is-attention" in CSS


def test_intraday_trajectory_repair_is_separate_from_post_close_recovery():
    assert (
        'const INTRADAY_TRAJECTORY_REPAIR_ENDPOINT = '
        '"/api/market-watch/intraday-trajectory-repair"' in JS
    )
    assert 'id="trajectory-repair-button"' in HTML
    assert 'id="trajectory-repair-status"' in HTML
    assert 'id="trajectory-repair-remaining"' in HTML
    assert 'id="trajectory-repair-improved"' in HTML
    assert "requestIntradayTrajectoryRepair" in JS
    assert "fetchIntradayTrajectoryRepair" in JS
    assert "低优先级追补中" in JS
    assert "上游仍有真实缺口" in JS


def test_collector_health_comes_from_envelope_heartbeat_not_process_presence():
    assert "COLLECTOR_HEARTBEAT_STALE_MS = 90_000" in JS
    assert "state.collectionStatus?.collector_heartbeat_at" in JS
    assert "state.collectionStatus?.collector_state" in JS
    assert "Date.now() - heartbeatAt <= COLLECTOR_HEARTBEAT_STALE_MS" in JS
    assert "采集心跳超时" in JS
    assert "process" not in re.search(
        r"function updatePollStatus\(\) \{(?P<body>.*?)\n  \}",
        JS,
        re.DOTALL,
    ).group("body")


def test_watch_polling_recovers_when_the_page_returns_to_the_foreground():
    scheduled = re.search(
        r"function runScheduledPoll\(\) \{(?P<body>.*?)\n  \}", JS, re.DOTALL
    )
    resumed = re.search(
        r"function resumeSnapshotPolling\(\) \{(?P<body>.*?)\n  \}", JS, re.DOTALL
    )
    start = re.search(r"function start\(\) \{(?P<body>.*?)\n  \}", JS, re.DOTALL)

    assert "POLL_WATCHDOG_INTERVAL_MS = 1_000" in JS
    assert scheduled is not None
    assert 'document.visibilityState !== "visible"' in scheduled.group("body")
    assert "state.fetchInFlight" in scheduled.group("body")
    assert "Date.now() < state.nextPollAt" in scheduled.group("body")
    assert "fetchSnapshot();" in scheduled.group("body")
    assert resumed is not None
    assert "state.nextPollAt = Date.now();" in resumed.group("body")
    assert "runScheduledPoll();" in resumed.group("body")
    assert 'document.addEventListener("visibilitychange", resumeSnapshotPolling)' in JS
    assert 'window.addEventListener("focus", resumeSnapshotPolling)' in JS
    assert 'window.addEventListener("pageshow", resumeSnapshotPolling)' in JS
    assert start is not None
    assert "bindPollingRecovery();" in start.group("body")
    assert (
        "window.setInterval(runScheduledPoll, POLL_WATCHDOG_INTERVAL_MS);"
        in start.group("body")
    )
    assert "window.setInterval(fetchSnapshot, POLL_INTERVAL_MS);" not in JS


def test_same_snapshot_periodically_rechecks_independent_resonance_revision():
    loader = re.search(
        r"async function loadMarketWatchBatch\(\{ force = false \} = \{\}\) \{(?P<body>.*?)\n  \}",
        JS,
        re.DOTALL,
    )

    assert "RESONANCE_REFRESH_INTERVAL_MS = 30_000" in JS
    assert "lastSummaryCheckedAt: 0" in JS
    assert loader is not None
    assert "const resonanceRefreshDue" in loader.group("body")
    assert "Date.now() - state.lastSummaryCheckedAt" in loader.group("body")
    assert "const previousResonanceRevision" in loader.group("body")
    assert (
        "summary.resonance_revision !== previousResonanceRevision"
        in loader.group("body")
    )
    assert re.search(
        r"hydratedSnapshot\(\s*summary,\s*state\.trajectoryDetails,\s*\)",
        loader.group("body"),
    )


def test_indices_and_turnover_use_the_always_visible_non_blocking_sticky_strip():
    assert 'id="index-dock"' in HTML
    dock_roles = re.findall(r'data-index-dock-role="([^"]+)"', HTML)
    assert dock_roles == ["broad_market", "large_cap", "small_cap", "growth"]
    assert "data-turnover-dock" in HTML
    assert "全 A 成交额 · 昨日同期" in HTML
    assert 'id="turnover-today"' in HTML
    assert 'id="turnover-previous"' in HTML
    assert 'id="turnover-ratio"' in HTML
    assert 'id="turnover-difference"' in HTML
    assert '<article class="panel turnover-panel">' not in HTML
    assert 'id="turnover-direction"' not in HTML
    assert 'id="turnover-note"' not in HTML
    assert "data-index-role" not in HTML
    assert 'id="indices-grid"' not in HTML
    assert "index-card" not in HTML + CSS + JS
    assert 'document.querySelector(`[data-index-dock-role="${role}"]`)' in JS
    assert "function renderIndexDock(snapshot)" in JS
    assert "setupStickyIndexStrip" not in JS
    assert re.search(
        r"\.index-dock\s*\{[^}]*position:\s*sticky;[^}]*top:\s*8px;",
        CSS,
        re.DOTALL,
    )
    assert re.search(
        r"\.index-dock\s*\{[^}]*pointer-events:\s*none;",
        CSS,
        re.DOTALL,
    )
    assert ".index-dock__level" in CSS
    assert ".index-dock__change" in CSS


def test_a_share_market_values_use_red_for_rises_and_green_for_falls():
    assert "--market-up: #ff7c79;" in CSS
    assert "--market-down: #4dd19b;" in CSS
    assert re.search(
        r"\.tone-positive\s*\{[^}]*var\(--market-up\)", CSS, re.DOTALL
    )
    assert re.search(
        r"\.tone-negative\s*\{[^}]*var\(--market-down\)", CSS, re.DOTALL
    )
    assert re.search(
        r"\.breadth-bar__up\s*\{[^}]*var\(--market-up\)", CSS, re.DOTALL
    )
    assert re.search(
        r"\.breadth-bar__down\s*\{[^}]*var\(--market-down\)", CSS, re.DOTALL
    )
    assert re.search(
        r"\.direction-expand\s*\{[^}]*var\(--market-up\)", CSS, re.DOTALL
    )
    assert re.search(
        r"\.direction-shrink\s*\{[^}]*var\(--market-down\)", CSS, re.DOTALL
    )
    # Health/success semantics stay green instead of inheriting quote colors.
    assert re.search(
        r"\.state-open\s*\{[^}]*var\(--positive\)", CSS, re.DOTALL
    )


def test_watch_page_keeps_the_decision_path_and_evidence_boundaries_visible():
    anchors = [
        'id="decision-bar"',
        'id="index-dock"',
        'id="turnover-today"',
        'id="sector-flow-section"',
        'id="sector-flow-defense-chart"',
        'id="sector-flow-defense-observation-list"',
        'id="sector-flow-offense-chart"',
        'id="sector-flow-offense-observation-list"',
        'id="breadth-up-ratio"',
        'id="rotation-summary"',
    ]

    positions = [HTML.index(anchor) for anchor in anchors]
    assert positions == sorted(positions)
    assert "资金仍是估计 / 辅助" in JS
    assert "主力资金" not in HTML + JS
    assert "change.confirmed" not in JS
    assert "return parsed * 100" in JS
    assert 'id="breadth-bar-unclassified"' in HTML
    assert 'id="breadth-unclassified"' in HTML
    assert 'id="breadth-total"' in HTML
    assert "median_change_pct" not in JS
    assert re.search(r"(?<![A-Za-z0-9_])limit_up_count(?![A-Za-z0-9_])", JS) is None
    assert "limit_down_count" not in JS


def test_market_feel_leads_with_canonical_facts_and_abstains_on_stale_data():
    assert "盘面体感 · 硬指标" in HTML
    assert "function marketFeelFacts(snapshot)" in JS
    for field in (
        "breadth.up_count",
        "breadth.down_count",
        "breadth.advance_ratio",
        "snapshot.indices",
        "turnover.difference_ratio",
    ):
        assert field in JS
    assert 'byId("guardrail-current-state").textContent = marketFeel.detail' in JS
    assert 'freshness.status === "stale"' in JS
    assert 'freshness.status === "unavailable"' in JS
    assert "历史读数 · 暂停判断" in JS
    assert "当前数据陈旧，仅展示最后一份可核验读数" in JS


def test_sector_flow_chart_is_versioned_bounded_and_honestly_degraded():
    assert 'snapshot.sector_flow_trajectory' in JS
    assert 'snapshot.offense_sector_flow_trajectory' in JS
    assert 'raw.contract !== "sector_flow_trajectory.v1"' in JS
    assert 'raw.schema_version !== 1' in JS
    assert 'MAX_SECTOR_FLOW_SERIES = 64' in JS
    assert 'MAX_SECTOR_FLOW_CHART_SERIES = 64' in JS
    assert 'MAX_SECTOR_FLOW_ENDPOINT_LABELS = 64' in JS
    assert 'data-flow-mode="cumulative"' in HTML
    assert HTML.count('data-flow-mode="five_day"') == 2
    assert 'data-flow-mode="delta_5m"' not in HTML
    assert "近五日" in HTML
    assert 'data-flow-scope="defense"' in HTML
    assert 'data-flow-scope="offense"' in HTML
    for scope in ("defense", "offense"):
        assert f'id="sector-flow-{scope}-heading"' in HTML
        assert f'id="sector-flow-{scope}-empty"' in HTML
        assert f'id="sector-flow-{scope}-tooltip"' in HTML
        assert f'id="sector-flow-{scope}-legend"' in HTML
        assert f'id="sector-flow-{scope}-mini-chart"' in HTML
        chart_card = re.search(
            rf'<details[^>]+id="sector-flow-{scope}-chart-card"[^>]*>', HTML
        )
        assert chart_card is not None
        assert " open" not in chart_card.group(0)
    assert HTML.count('class="sector-flow-chart-toggle"') == 2
    assert 'chartCard.addEventListener("toggle", syncChartLayout)' in JS
    assert 'classList.toggle("is-chart-open", chartCard.open)' in JS
    assert "function renderSectorFlowMiniChart(payload, scope)" in JS
    assert '"data-mini-sector-flow"' in JS
    assert "renderSectorFlowMiniChart(chartPayload, scope, preparedSeries)" in JS
    assert "if (chartCard.open)" in JS
    assert "clearSectorFlowExpandedChart(scope)" in JS
    assert ".sector-flow-visual[open] .sector-flow-mini-chart" in CSS
    assert "document.createElementNS" in JS
    assert "point.session_segment" in JS
    assert "segment !== previousSegment" in JS
    assert "function sectorFlowTradingMinute" in JS
    assert 'segment === "am" && minute >= 9 * 60 + 25 && minute <= 11 * 60 + 30' in JS
    assert 'segment === "pm" && minute >= 13 * 60 && minute <= 15 * 60' in JS
    assert "const SECTOR_FLOW_LUNCH_GAP_MINUTES = 18" in JS
    assert "const MAX_SECTOR_FLOW_SAMPLE_GAP_MINUTES = 5" in JS
    assert "return 125 + SECTOR_FLOW_LUNCH_GAP_MINUTES + minute - 13 * 60" in JS
    assert "point.tradingMinute" in JS
    assert "function sectorFlowPathData(segments, x, y)" in JS
    assert "(time - previousTime) / 60_000 > MAX_SECTOR_FLOW_SAMPLE_GAP_MINUTES" in JS
    assert 'previousEndpoint.segment === "am"' in JS
    assert 'first.segment === "pm"' in JS
    assert "const bridgeSpan = Math.max(0, endX - startX)" in JS
    assert 'commands.push(`C${controlX1.toFixed(2)},${startY.toFixed(2)}' in JS
    assert JS.count("const pathData = sectorFlowPathData(entry.segments, x, y)") == 2
    assert 'breakLabel.textContent = "午间断点"' in JS
    assert 'return "11:30 / 13:00"' in JS
    assert "point.delta_5m_cny" in JS
    assert "sectorFlowSymlog" not in JS
    assert "const SECTOR_FLOW_ENDPOINT_GAP_PX = 15" in JS
    assert "const SECTOR_FLOW_COLOR_SLOT_COUNT = 72" in JS
    assert "const SECTOR_FLOW_HUE_ORDER = [" in JS
    assert "0, 8, 4, 12, 2, 10, 6, 14" in JS
    assert "1, 9, 5, 13, 3, 11, 7, 15" in JS
    assert "const SECTOR_FLOW_COLOR_BANDS = [" in JS
    assert "function sectorFlowPaletteColor(slot)" in JS
    assert "function sectorFlowColorSlots(payload, entries)" in JS
    assert "function sectorFlowSeries(payload, mode)" in JS
    assert 'sectorFlowColorAssignments: "tradex.marketWatch.sectorFlowColorAssignments.v1"' in JS
    assert "stored.tradeDate !== tradeDate" in JS
    assert "const scope = payload?.direction === \"offense\" ? \"offense\" : \"defense\"" in JS
    assert "Number.isInteger(assignments[sectorKey])" in JS
    assert "!usedSlots.has(candidate)" in JS
    assert "storeJson(STORAGE_KEYS.sectorFlowColorAssignments, stored)" in JS
    assert "color: sectorFlowPaletteColor(colorSlots[index])" in JS
    assert JS.count(
        "const series = preparedSeries || sectorFlowSeries(payload, mode)"
    ) == 2
    assert "sectorFlowStableHash" not in JS
    assert "[index % 8]" not in JS
    assert "const height = Math.max(" in JS
    assert 'svg.setAttribute("viewBox", `0 0 ${width} ${height}`)' in JS
    assert 'shell.style.height = `${height}px`' in JS
    assert "const endpointScaleKnots" not in JS
    assert "const endpointScaleRows" not in JS
    assert "const transformedFromY" not in JS
    assert "const SECTOR_FLOW_ENDPOINT_DENSITY_WEIGHT = 0.65" in JS
    assert "function sectorFlowEndpointRank(value, endpointValues)" in JS
    assert "while (upperIndex - lowerIndex > 1)" in JS
    assert "const endpointScaleValues = [...new Set" in JS
    assert "const amountPosition = (value - yScaleMin) / (yScaleMax - yScaleMin)" in JS
    assert "const densityPosition = sectorFlowEndpointRank(value, endpointScaleValues)" in JS
    assert "const blendedPosition = (1 - SECTOR_FLOW_ENDPOINT_DENSITY_WEIGHT)" in JS
    assert 'scaleLabel.textContent = mode === "five_day"' in JS
    assert "五日连续累计净额" in JS
    assert "endpointLabelTitle" not in JS
    assert "layoutSectorFlowEndpointLabel" not in JS
    assert '"data-sector-flow-endpoint": text(entry.item.sector_key' in JS
    assert '"data-sector-flow-endpoint-label": sectorKey' in JS
    assert "x: x(endpoint.tradingMinute) + 9" in JS
    assert "y: y(endpoint.value) + 3" in JS
    assert "lineGroup.append(textNode)" in JS
    assert "const textX" not in JS
    assert '"data-sector-flow-endpoint-connector"' not in JS
    assert "labelColumn" not in JS
    assert "function focusSectorFlowSeries(svg, sectorKey = null)" in JS
    assert "function setSectorFlowSeriesLock(svg, sectorKey = null)" in JS
    assert "defaultFocusSector" not in JS
    assert '? (active ? "1" : "0")' in JS
    assert 'label.setAttribute("fill-opacity", sectorKey && !active ? "0" : "1")' in JS
    assert 'lineGroup.dataset.defaultStrokeOpacity = "0.96"' in JS
    assert 'lineGroup.dataset.defaultStrokeWidth = "2"' in JS
    assert "const crowdedChart" not in JS
    assert 'entry.addEventListener("pointerenter", () => focusSectorFlowSeries(svg, sectorKey))' not in JS
    assert 'entry.addEventListener("focus", () => focusSectorFlowSeries(svg, sectorKey))' not in JS
    assert 'entry.addEventListener("click", (event) =>' in JS
    assert '"data-sector-flow-click-target": sectorKey' in JS
    assert 'hitPath.addEventListener("click", (event) =>' in JS
    assert 'setSectorFlowSeriesLock(svg, sectorKey)' in JS
    assert 'svg[data-locked-sector-flow]' in JS
    assert ".sector-flow-chart-shell > svg" in CSS
    assert "labelColumns" not in JS
    assert "不会依据单点涨幅补画" in HTML + JS
    assert "近 5 分钟同源基线仍在积累" in JS
    assert "renderSectorFlowTrajectories(snapshot)" in JS
    assert "fetch(`${TRAJECTORY_ENDPOINT}?${query}`" in JS
    assert 'FIVE_DAY_TRAJECTORY_ENDPOINT = "/api/market-watch/five-day-trajectory"' in JS
    assert 'payload.contract !== "sector_flow_five_day_trajectory.v1"' in JS
    assert 'day?.contract !== "sector_flow_five_day_slice.v1"' in JS
    assert "function sectorFlowFiveDayPayload(history, displayPayload)" in JS
    assert "_historyDayIndex: dayIndex" in JS
    assert "_historyTradeDate: day.trade_date" in JS
    assert "_historyContinuousCny: cumulativeOffset + dailyCumulative" in JS
    assert "cumulativeOffset += finalDailyCumulative" in JS
    assert "dayIndex !== previousDayIndex" in JS
    assert "previousEndpoint.dayIndex === first.dayIndex" in JS
    assert "const canBridgeDay = previousEndpoint" in JS
    assert "first.dayIndex === previousEndpoint.dayIndex + 1" in JS
    assert "previousEndpoint.sessionMinute >= SECTOR_FLOW_SESSION_SPAN_MINUTES" in JS
    assert "SECTOR_FLOW_DAY_SPAN_MINUTES" in JS
    assert "historyTradeDates.forEach((tradeDate, dayIndex)" in JS
    assert "近五个交易日连续分钟资金轨迹" in JS
    assert "连续累计" in JS
    assert "每日独立" not in JS
    assert "相邻交易日首尾接续" in JS
    assert "缺失分钟仍保持断点" in JS
    assert "sectorFlowCache" not in JS


def test_sector_flow_chart_renders_one_real_point_without_fabricating_a_path():
    assert ".filter((entry) => entry.segments.some((segment) => segment.length >= 1))" in JS
    assert '"data-sector-flow-first-point": !hasConfirmedPath ? sectorKey : null' in JS
    assert "等待首个真实板块资金点；不会复制集合竞价数据补线。" in JS


def test_sector_flow_chart_expand_reuses_the_pre_rendered_svg():
    toggle_handler = re.search(
        r"const syncChartLayout = \(\) => \{(?P<body>.*?)\n\s*\};",
        JS,
        re.DOTALL,
    )

    assert toggle_handler is not None
    assert 'classList.toggle("is-chart-open", chartCard.open)' in toggle_handler.group("body")
    assert "renderSectorFlowTrajectory" not in toggle_handler.group("body")


def test_sector_flow_line_hover_shows_the_nearest_observed_amount():
    tooltip_handler = re.search(
        r"const showLineTooltip = \(event\) => \{(?P<body>.*?)\n\s*\};",
        JS,
        re.DOTALL,
    )
    pointer_leave_handler = re.search(
        r'hitPath\.addEventListener\("pointerleave", \(\) => \{(?P<body>.*?)\n\s*\}\);',
        JS,
        re.DOTALL,
    )

    assert tooltip_handler is not None
    assert pointer_leave_handler is not None
    assert "const SECTOR_FLOW_HIT_STROKE_PX = 10" in JS
    assert "function nearestSectorFlowObservedPoint(event, svg, entry, x)" in JS
    assert "const matrix = svg.getScreenCTM()" in JS
    assert "pointer.matrixTransform(matrix.inverse()).x" in JS
    assert "entry.segments.flat().reduce" in JS
    assert '"data-sector-flow-hit-target": text(entry.item.sector_key' in JS
    assert '"pointer-events": "stroke"' in JS
    assert '"stroke-width": SECTOR_FLOW_HIT_STROKE_PX' in JS
    assert 'hitPath.addEventListener("pointerenter", showLineTooltip)' in JS
    assert 'hitPath.addEventListener("pointermove", showLineTooltip)' in JS
    assert 'hitPath.addEventListener("pointerleave"' in JS
    assert "showSectorFlowTooltip(event, entry.item, observedPoint.point, mode, scope)" in JS
    assert "focusSectorFlowSeries" not in tooltip_handler.group("body")
    assert "setSectorFlowSeriesLock" not in tooltip_handler.group("body")
    assert "focusSectorFlowSeries" not in pointer_leave_handler.group("body")
    assert "setSectorFlowSeriesLock" not in pointer_leave_handler.group("body")


def test_sector_flow_defense_and_offense_are_stacked_and_rendered_together():
    sector_flow = HTML[
        HTML.index('<section class="section-block sector-flow-block"'):
        HTML.index('<section class="facts-grid"')
    ]
    assert 'role="tablist"' not in sector_flow
    assert 'role="tabpanel"' not in sector_flow
    defense = HTML.index('id="sector-flow-defense-heading"')
    offense = HTML.index('id="sector-flow-offense-heading"')
    assert defense < offense
    assert HTML.count('class="sector-flow-panel') == 2
    assert HTML.count('data-sector-flow-chart-card') == 2
    assert HTML.count('data-sector-flow-picker-options') == 2
    assert 'sectorFlowScope' not in JS
    assert 'offenseSectorFlowSelection: "tradex.marketWatch.offenseSectorFlowSelection.v2"' in JS
    assert 'function sectorFlowSelection(scope)' in JS
    assert 'renderSectorFlowTrajectory(snapshot, "defense")' in JS
    assert 'renderSectorFlowTrajectory(snapshot, "offense")' in JS
    assert HTML.count('id="sector-flow-defense-chart"') == 1
    assert HTML.count('id="sector-flow-offense-chart"') == 1


def test_sector_flow_selection_and_sudden_move_override_are_local_and_explicit():
    assert 'id="sector-flow-defense-picker"' in HTML
    assert 'id="sector-flow-offense-picker"' in HTML
    assert 'id="sector-flow-defense-picker-options"' in HTML
    assert 'id="sector-flow-offense-picker-options"' in HTML
    assert 'id="sector-flow-defense-surge-threshold"' in HTML
    assert 'id="sector-flow-offense-surge-threshold"' in HTML
    assert 'id="sector-flow-defense-selection-count"' in HTML
    assert 'id="sector-flow-offense-selection-count"' in HTML
    assert 'sectorFlowSelection: "tradex.marketWatch.sectorFlowSelection.v1"' in JS
    assert 'sectorFlowSurgeThreshold: "tradex.marketWatch.sectorFlowSurgeThreshold.v1"' in JS
    assert "function sectorFlowDisplayPayload(payload)" in JS
    assert "Math.abs(changeDelta) >= state.sectorFlowSurgeThreshold" in JS
    assert "function hasFullSectorResonance(item)" in JS
    assert "const resonance = hasFullSectorResonance(item)" in JS
    assert "const automatic = (triggered || resonance) && !isSelected" in JS
    assert "if (hasFullSectorResonance(item))" in JS
    assert "自动出现" in JS
    assert "5分涨速" in JS
    assert "storeJson(sectorFlowSelectionStorageKey(scope)" in JS
    assert "storeText(STORAGE_KEYS.sectorFlowSurgeThreshold" in JS
    assert "fetch(`${TRAJECTORY_ENDPOINT}?${query}`" in JS
    assert "hydrateCurrentSectorFlow(scope)" in JS
    assert "sectorFlowSelectionCache" not in JS


def test_sector_flow_cards_rank_by_current_funds_and_make_sudden_moves_visible():
    sorter = JS.split("function compareSectorFlowByCurrentAmount", 1)[1].split(
        "function sectorFlowCurrentAmountLabel", 1
    )[0]

    assert "left?.latest?.cumulative_cny" in sorter
    assert "right?.latest?.cumulative_cny" in sorter
    assert "return rightAmount - leftAmount" in sorter
    assert "change_pct" not in sorter
    assert ".sort(compareSectorFlowByCurrentAmount)" in JS
    assert "sectorFlowCurrentAmountLabel(currentAmount)" in JS
    assert "当前流入" in JS
    assert "当前流出" in JS
    assert "sector-flow-surge-badge" in JS + CSS
    assert "is-surge-up" in JS + CSS
    assert "is-surge-down" in JS + CSS
    assert "⚡涨速突变" in JS
    assert HTML.count("当前净流入额从高到低，突变板块高亮") == 2


def test_sector_flow_large_selection_is_deferred_toggleable_and_dismissible():
    assert "function scheduleSectorFlowRender(scope" in JS
    assert "window.requestAnimationFrame" in JS
    assert "renderPicker: false" in JS
    assert "const allSelected = keys.length > 0 && keys.every" in JS
    assert 'button.textContent = allSelected ? "取消全选" : "全选"' in JS
    assert "allSelected ? new Set() : new Set(keys)" in JS
    assert 'document.addEventListener("pointerdown"' in JS
    assert 'document.addEventListener("keydown"' in JS
    assert 'event.key !== "Escape"' in JS
    assert "!picker.contains(event.target)" in JS
    assert "segment.forEach((point)" not in JS
    assert "const SHANGHAI_TIME_PARTS_FORMATTER = new Intl.DateTimeFormat" in JS
    time_parser = JS.split("function shanghaiMinuteOfDay", 1)[1].split(
        "function sectorFlowTradingMinute", 1
    )[0]
    assert "new Intl.DateTimeFormat" not in time_parser
    assert "SHANGHAI_TIME_PARTS_FORMATTER.formatToParts(parsed)" in time_parser
    assert "const fiveDayTrajectoryRequests = new Map()" in JS
    assert "fiveDayTrajectoryRequests.get(cacheKey)" in JS
    assert "fiveDayTrajectoryRequests.set(cacheKey, request)" in JS
    assert "const preparedSeries = sectorFlowSeries(chartPayload" in JS
    assert "renderSectorFlowMiniChart(chartPayload, scope, preparedSeries)" in JS
    assert "renderSectorFlowChart(chartPayload, scope, preparedSeries)" in JS


def test_sector_flow_observations_keep_sector_change_and_leaders_visible():
    observations = re.search(
        r"function renderSectorFlowObservations\(payload, scope\) \{(?P<body>.*?)\n  \}",
        JS,
        re.DOTALL,
    )
    leaders = re.search(
        r"function renderSectorFlowLeaders\(item, latest\) \{(?P<body>.*?)\n  \}",
        JS,
        re.DOTALL,
    )

    assert observations is not None
    assert leaders is not None
    assert "板块涨幅" in observations.group("body")
    assert ".sort(compareSectorFlowByCurrentAmount)" in observations.group("body")
    assert "板块上涨" in observations.group("body")
    assert "item.tier_label" not in observations.group("body")
    assert "净流占比" not in observations.group("body")
    assert "涨幅/广度" not in observations.group("body")
    assert "renderSectorFlowLeaders(item, latest)" in observations.group("body")
    assert 'text(item.parent_name, "")' in observations.group("body")
    assert "snapshot.leaders" in leaders.group("body")
    assert 'snapshot.selection_method === "sector_fund_flow_path_resonance.v2"' in leaders.group("body")
    assert 'isUpResonance ? asArray(snapshot.leaders).slice(0, 1) : []' in leaders.group("body")
    assert '"共振回溯暂缺"' in leaders.group("body")
    assert "change <= 0" not in leaders.group("body")
    assert 'text(snapshot.status_label, "暂无高共振股")' in leaders.group("body")
    assert '"共振领涨股"' in leaders.group("body")
    assert 'snapshot.resonance_direction === "up"' in leaders.group("body")
    assert '"共振领跌股"' not in leaders.group("body")
    assert 'leader.speed_pct' in leaders.group("body")
    assert '5分 ${formatChangePct(leaderSpeed)}' in leaders.group("body")
    assert 'leader.resonance_correlation' in leaders.group("body")
    assert '相关 ${correlation.toFixed(2)}' in leaders.group("body")
    assert 'leader.directional_agreement_ratio' in leaders.group("body")
    assert '同向 ${(agreement * 100).toFixed(0)}%' in leaders.group("body")
    assert "只识别板块5分钟资金边际流入" in leaders.group("body")
    assert "2分钟内最近的完整共同窗口" in leaders.group("body")
    assert ".slice(0, 1)" in leaders.group("body")
    assert "leader.instrument_id" in leaders.group("body")


def test_intraday_sector_move_radar_has_live_trajectory_leaders_and_alert_delivery():
    assert 'id="sector-move-radar"' not in HTML
    assert 'id="intraday-macd-j-section"' in HTML
    assert 'id="intraday-macd-j-history-dialog"' in HTML
    assert "function sectorMoveCandidates(snapshot)" in JS
    assert "function renderSectorMoveMiniChart(item)" in JS
    assert "function renderSectorMoveRadar(snapshot)" in JS
    assert '["defense", "offense"].flatMap((scope)' in JS
    assert "sectorFlowPayload(snapshot, scope)" in JS
    assert "Math.abs(changeDelta) >= state.sectorFlowSurgeThreshold" in JS
    assert 'sectorFlowSegments(item, "delta_5m")' in JS
    assert "renderSectorFlowLeaders(item, latest)" in JS
    assert "renderSectorMoveRadar(snapshot);" in JS
    assert 'alert.kind === "sector_move"' in JS
    assert "showSectorMoveNotification(sectorAlerts)" in JS
    assert ".sector-move-radar-list" in CSS
    assert ".sector-move-card__chart" in CSS


def test_sector_move_radar_treats_null_latest_as_missing_delta():
    candidates = re.search(
        r"function sectorMoveCandidates\(snapshot\) \{(?P<body>.*?)\n  \}",
        JS,
        re.DOTALL,
    )

    assert candidates is not None
    assert 'item?.latest && typeof item.latest === "object"' in candidates.group("body")


def test_sector_move_radar_uses_trajectory_freshness_not_global_snapshot_freshness():
    assert "const SECTOR_MOVE_MAX_AGE_MS = 120_000" in JS
    assert "function sectorMoveRadarAvailability(snapshot" in JS
    start = JS.index("function renderSectorMoveRadar(snapshot)")
    end = JS.index("\n  function renderSectorFlowObservations", start)
    radar = JS[start:end]
    assert "sectorMoveRadarAvailability(snapshot)" in radar
    assert 'freshness.status !== "fresh"' not in radar
    assert 'payload.status === "ready" || payload.status === "partial"' in JS
    assert "Date.parse(payload.asOf)" in JS
    assert "SECTOR_MOVE_MAX_AGE_MS" in JS
    assert "isSectorMoveAlert(alert)" in JS
    assert "sectorMoveRadarAvailability(snapshot).available" in JS


def test_only_open_session_stale_or_unavailable_data_is_prominent():
    assert 'id="data-risk-overlay"' in HTML
    assert 'aria-live="assertive"' in HTML
    assert "盘面数据已经陈旧" in JS
    assert "盘面数据不可用" in JS
    risk = re.search(
        r"function updateDataRisk\(snapshot\) \{(?P<body>.*?)\n  \}", JS, re.DOTALL
    )
    assert risk is not None
    assert "marketState.isOpen" in risk.group("body")
    assert '"stale", "unavailable", "unknown"' in risk.group("body")
    assert "if (!blocksCurrentJudgment)" in risk.group("body")
    assert "最后画面已保留" in JS
    assert 'freshness.status !== "fresh"' in JS
    assert "盘面变化提醒已锁定" in JS
    assert "processBackendAlerts(snapshot)" in JS
    assert "state.fetchFailed" in JS
    assert "state.lastFetchError" in JS


def test_turnover_uses_component_freshness_and_validates_the_complete_payload():
    freshness = re.search(
        r"function turnoverFreshness\(snapshot\) \{(?P<body>.*?)\n  \}",
        JS,
        re.DOTALL,
    )
    validation = re.search(
        r"function validateAvailableTurnover\(turnover\) \{(?P<body>.*?)\n  \}",
        JS,
        re.DOTALL,
    )
    rendering = re.search(
        r"function renderTurnover\(snapshot\) \{(?P<body>.*?)\n  \}\n\n  function createSvgElement",
        JS,
        re.DOTALL,
    )

    assert freshness is not None
    assert 'item.component === "turnover"' in freshness.group("body")
    assert validation is not None
    assert "strictFiniteNumber" in validation.group("body")
    for field in (
        "today_date",
        "previous_date",
        "as_of",
        "today_amount_cny",
        "previous_same_time_amount_cny",
        "difference_cny",
        "difference_ratio",
        "neutral_band_ratio",
        "direction",
    ):
        assert f"turnover.{field}" in validation.group("body")
    assert "expectedDifference" in validation.group("body")
    assert "expectedRatio" in validation.group("body")
    assert "expectedDirection" in validation.group("body")

    assert rendering is not None
    assert "turnoverFreshness(snapshot)" in rendering.group("body")
    assert "validateAvailableTurnover(turnover)" in rendering.group("body")
    assert "数据延迟；截至 ${comparison.as_of}" in rendering.group("body")
    assert "turnover.reason" in rendering.group("body")
    assert 'document.querySelector("[data-turnover-dock]")' in rendering.group("body")
    assert 'dock.classList.toggle("is-unavailable", !available)' in rendering.group("body")
    assert 'dock.setAttribute("aria-label", accessibleSummary)' in rendering.group("body")
    assert 'byId("turnover-difference")' in rendering.group("body")
    assert 'formatCny(comparison.difference_cny, true)' in rendering.group("body")
    assert 'byId("turnover-direction")' not in rendering.group("body")
    assert 'byId("turnover-note")' not in rendering.group("body")
    assert "lastGoodTurnover" not in JS
    assert "turnoverCache" not in JS


def test_notification_permission_requires_a_user_action_and_alerts_are_backend_owned():
    assert JS.count("Notification.requestPermission()") == 1
    opt_in = re.search(
        r"async function handleNotificationOptIn\(\) \{(?P<body>.*?)\n  \}",
        JS,
        re.DOTALL,
    )
    assert opt_in is not None
    assert "Notification.requestPermission()" in opt_in.group("body")
    assert (
        'byId("notification-button").addEventListener("click", '
        "handleNotificationOptIn)"
    ) in JS
    assert "alertListFromSnapshot(snapshot)" in JS
    assert "ALERT_COOLDOWN_MS = 5 * 60 * 1000" in JS
    assert "deliveredAlertKeys" in JS
    assert "dedupe_key" in JS
    assert "state.muted" in JS
    assert "wasDeliveredRecently(alert, now)" in JS
    assert "!state.notificationEnabled && !state.soundEnabled" in JS
    assert '["data_degraded", "data_stale", "data_unavailable"]' in JS
    assert "alertCanDeliver(snapshot, alert)" in JS


def test_rotation_map_is_removed_while_the_compact_pulse_remains():
    assert "板块轮动地图" not in HTML
    assert "ROTATION MAP" not in HTML
    assert 'id="rotation-strengthening-list"' not in HTML
    assert 'id="rotation-weakening-list"' not in HTML
    assert ".rotation-columns" not in CSS
    assert "makeSectorCard" not in JS
    assert 'id="rotation-summary"' in HTML
    assert "renderRotationPulse(snapshot)" in JS


def test_sound_defaults_off_and_can_only_be_enabled_by_a_button_action():
    assert 'id="sound-button"' in HTML
    assert "声音：关" in HTML
    assert "soundEnabled: false" in JS
    assert 'byId("sound-button").addEventListener("click", handleSoundToggle)' in JS
    assert "if (!state.soundEnabled || !state.audioContext) return" in JS
    assert "if (state.soundEnabled) ensureAudioContext()" in JS


def test_manual_refresh_revalidates_read_only_views_without_provider_refresh():
    assert 'if (!force && etag) headers["If-None-Match"] = etag' in JS
    assert 'fetchSnapshot({ force: true })' in JS
    assert 'refresh=1' not in JS


def test_split_payloads_commit_only_after_revision_and_point_manifest_proofs():
    assert "function validateSummary(payload, expectedRevision, response)" in JS
    assert "function validateTrajectoryDetail(payload, summary, scope, requestedKeys, response)" in JS
    assert 'response.headers.get("X-Source-Snapshot-Revision")' in JS
    assert 'response.headers.get("X-Trajectory-Revision")' in JS
    assert "proof.points_revision !== manifest.points_revision" in JS
    assert "points.length !== manifest.point_count" in JS
    assert "const results = await Promise.all" in JS
    results_index = JS.index("const results = await Promise.all")
    assert results_index < JS.index("state.lastSummary = summary", results_index)
    assert "state.trajectoryDetails = details" in JS
    assert "state.lastSnapshot = snapshot" in JS


def test_revision_conflict_discards_only_the_in_flight_attempt_and_retries_once():
    assert 'payload.action === "discard_batch_and_retry"' in JS
    attempt_reset = re.search(
        r"function discardMarketWatchAttempt\(\) \{(?P<body>.*?)\n  \}", JS, re.DOTALL
    )
    assert attempt_reset is not None
    assert "state.summaryEtag = null" in attempt_reset.group("body")
    assert "state.detailEtags.clear()" in attempt_reset.group("body")
    assert "state.detailPayloads.clear()" in attempt_reset.group("body")
    assert "state.lastSummary = null" not in attempt_reset.group("body")
    assert "state.lastSnapshot = null" not in attempt_reset.group("body")
    assert "for (let attempt = 0; attempt < 2; attempt += 1)" in JS
    assert "(error.discardBatch || error.noAcceptedReal)" in JS
    assert "discardMarketWatchAttempt();" in JS


def test_no_accepted_real_keeps_the_shell_and_last_verified_batch_visible():
    keep_visible = re.search(
        r"function keepMarketWatchSurfacesVisible\(\) \{(?P<body>.*?)\n  \}",
        JS,
        re.DOTALL,
    )
    assert keep_visible is not None
    assert "element.hidden = false" in keep_visible.group("body")
    assert "setNumericSurfacesVisible(false)" not in JS
    no_data = re.search(
        r"function renderNoAcceptedReal\(status\) \{(?P<body>.*?)\n  \}",
        JS,
        re.DOTALL,
    )
    assert no_data is not None
    assert "discardMarketWatch" not in no_data.group("body")
    assert "keepMarketWatchSurfacesVisible();" in no_data.group("body")
    assert "renderMarketWatchUnavailableShell" in no_data.group("body")
    assert "state.lastSnapshot" in no_data.group("body")
    assert "暂无已接收的真实盘中快照" in JS
    assert "保留最后一份已核验画面" in JS
    assert "不会把缺口心跳当成盘面数值" in JS
    assert "latest_accepted_real" in JS


def test_watch_page_reads_only_canonical_v1_fields():
    legacy_aliases = (
        "quality_status",
        "is_fresh",
        "advance_count",
        "decline_count",
        "up_ratio",
        "role_tags",
        "rotation.strengthening",
        "rotation.weakening",
        "change.confirmed",
        "alert.detail",
        "alert.as_of",
    )
    assert all(alias not in JS for alias in legacy_aliases)
    assert ".sector_name" not in JS
    assert 'normalized === "strengthening"' in JS
    assert 'normalized === "weakening"' in JS
    assert 'abstain: "暂不判断"' in JS
    assert "REGIME_LABELS" in JS
    assert "snapshot.snapshot_id" in JS


def test_watch_page_is_self_contained_and_desktop_only():
    combined = HTML + CSS + JS
    assert not re.search(r'(?:href|src)=["\']https?://', HTML, re.IGNORECASE)
    assert "@import" not in CSS
    assert "@media" not in CSS
    assert "min-width: 1180px" in CSS
    assert "react" not in combined.lower()
    assert "cdn" not in combined.lower()


def test_limit_up_pool_is_revision_bound_and_separates_current_display_from_relationships():
    assert 'const LIMIT_UP_POOL_ENDPOINT = "/api/limit-up-pool"' in JS
    assert 'const LIMIT_UP_POOL_LATEST_ENDPOINT = "/api/limit-up-pool/latest"' in JS
    assert 'id="limit-up-pool-open-button"' in HTML
    assert 'id="limit-up-pool-dialog"' in HTML
    assert 'id="limit-up-pool-categories"' in HTML
    assert 'id="limit-up-pool-board"' in HTML
    assert '{ business_key: "all", label: "全部", count: pool.pool_total }' in JS
    assert '{ sector_key: "all", label: "全部", count: pool.pool_total }' not in JS
    assert 'payload.contract !== "limit_up_pool.v2"' in JS
    assert "new URLSearchParams({ source_snapshot_revision: revision })" in JS
    assert "payload.source_snapshot_revision !== expectedRevision" in JS
    assert 'item?.relationship_match_status' in JS
    assert "item?.directory_category_name" in JS
    assert "item?.business_domain_name" in JS
    assert 'text(item?.display_category_key, "") || "unresolved_business"' in JS
    assert "item?.display_category_name" in JS
    assert "item?.display_category_basis" in JS
    assert '"evidence_candidate_ranking"' in JS
    assert 'boardCountBasis === "daily_closed_limit_up_history"' in JS
    assert 'boardCountBasis === "unavailable"' in JS
    assert "payload.market_attributed_count" in JS
    assert "item?.business_tags" in JS
    assert "item?.statistical_industry_name" in JS
    assert "item.relationship_verification_status" in JS
    assert "证据不足显示待核验，不用主营行业或供应商概念补位" in HTML
    assert 'height > 0 ? `${height}板` : "历史日榜\\n不可用"' in JS
    assert "formatLimitUpSealTime(item.first_sealed_at)" in JS
    assert (
        'if (!state.limitUpPool) byId("limit-up-pool-button-count").textContent = "…";'
        in JS
    )
    fetch_source = JS.split("async function fetchLimitUpPool", 1)[1].split(
        "function openLimitUpPoolDialog",
        1,
    )[0]
    assert "const hasPreviousPool = Boolean(state.limitUpPool);" in fetch_source
    assert "等待新的真实盘面快照…当前继续显示上一版涨停池" in fetch_source
    assert "正在刷新…当前继续显示上一版涨停池" in fetch_source
    assert "继续显示上一版涨停池" in fetch_source
    assert "fetchLatestAvailableLimitUpPool(summary)" in fetch_source
    assert "精确版本准备中 · 当前显示同交易日上一版涨停池" in fetch_source
    assert "state.limitUpPool = null;" not in fetch_source
    assert "state.limitUpPoolRevision = null;" not in fetch_source
    card_source = JS.split("function limitUpPoolCard", 1)[1].split(
        "function renderLimitUpPool",
        1,
    )[0]
    assert "区间记录 ·" in card_source
    assert "标签 ·" not in card_source
    assert "业务标签待核验" not in card_source
    assert "统计 ·" not in card_source
    assert "统计行业：" not in card_source
    assert 'fetchLimitUpPool({ showPending: byId("limit-up-pool-dialog").open })' in JS
    assert "analysis_pending_midday_or_post_close" not in JS
    assert "followed_sector" not in JS
    assert "cohort_confirmed_peer_count" not in JS
    assert 'id="limit-up-pool-matched"' in HTML
    assert 'id="limit-up-pool-classified"' in HTML
    assert 'id="limit-up-pool-unmatched"' in HTML
    assert "每分钟刷新涨停名单、板数与首次封板时间" in HTML
    assert "市场主归属统一读取聪明板块库" in HTML
    assert "证据不足显示待核验" in HTML
    assert "申万三级行业单独展示为统计行业" not in HTML
    assert 'displayLabel ? `市场主归属 · ${displayLabel}`' in JS
    assert "长期目录 ·" not in JS
    assert "@media" not in CSS


def test_removed_analysis_cards_leave_no_dom_accesses_and_keep_neighbor_panels():
    removed_ids = (
        "analysis-strength", "what-is-happening", "supporting-evidence",
        "counter-evidence", "scenario-list", "change-as-of", "confirmed-change",
        "mark-read-button", "alert-delivery-state", "unread-count", "alert-list",
    )
    for element_id in removed_ids:
        assert f'id="{element_id}"' not in HTML
        assert f'byId("{element_id}")' not in JS
    for title in ("此刻盘面在做什么", "接下来最值得盯的条件", "最近确认变化", "<h2>盘面变化提醒</h2>"):
        assert title not in HTML
    assert 'class="analysis-grid"' not in HTML
    assert 'class="bottom-grid"' not in HTML
    anchors = ('id="stock-selection-section"', 'id="breadth-up-ratio"', 'id="rotation-summary"')
    positions = [HTML.index(anchor) for anchor in anchors]
    assert positions == sorted(positions)
    assert "state.alerts = alertListFromSnapshot(snapshot);" in JS
    assert "function processBackendAlerts(snapshot)" in JS
