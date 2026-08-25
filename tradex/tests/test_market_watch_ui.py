"""Static acceptance checks for the desktop-only market-watch page."""

from pathlib import Path
import re


WATCH_DIR = Path(__file__).parents[1] / "src" / "tradex" / "dashboard" / "watch"
HTML = (WATCH_DIR / "index.html").read_text(encoding="utf-8")
CSS = (WATCH_DIR / "styles.css").read_text(encoding="utf-8")
JS = (WATCH_DIR / "app.js").read_text(encoding="utf-8")


def test_watch_page_consumes_one_versioned_market_watch_endpoint():
    assert JS.count('"/api/market-watch"') == 1
    assert "const API_ENDPOINT" in JS
    assert 'payload.contract !== "market_watch.v1"' in JS
    assert "payload.schema_version !== 1" in JS
    assert 'text(snapshot.contract, "market_watch.v1")' in JS
    assert "POLL_INTERVAL_MS = 15_000" in JS
    assert 'cache: "no-store"' in JS


def test_watch_page_exposes_server_side_history_and_honest_replay_evaluation():
    assert 'const HISTORY_ENDPOINT = "/api/market-watch/history"' in JS
    assert 'const EVALUATION_ENDPOINT = "/api/market-watch/evaluation"' in JS
    assert 'history.contract !== "market_watch_history.v1"' in JS
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
    assert "replayLastAttemptAt" in JS
    assert "if (silent) return" in JS
    assert "state.replayPendingRequest" in JS
    assert "innerHTML" not in JS


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


def test_background_refresh_state_is_visible_without_hiding_the_cached_snapshot():
    assert 'response.headers.get("X-Tradex-Refresh-State")' in JS
    assert "后台更新中" in JS


def test_index_data_only_uses_the_always_visible_non_blocking_sticky_strip():
    assert 'id="index-dock"' in HTML
    dock_roles = re.findall(r'data-index-dock-role="([^"]+)"', HTML)
    assert dock_roles == ["broad_market", "large_cap", "small_cap", "growth"]
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
        'id="sector-flow-section"',
        'id="sector-flow-defense-chart"',
        'id="sector-flow-defense-observation-list"',
        'id="sector-flow-offense-chart"',
        'id="sector-flow-offense-observation-list"',
        'id="breadth-up-ratio"',
        'id="turnover-today"',
        'id="rotation-summary"',
        'id="what-is-happening"',
        'id="supporting-evidence"',
        'id="counter-evidence"',
        'id="scenario-list"',
        'id="confirmed-change"',
        'id="alert-list"',
    ]

    positions = [HTML.index(anchor) for anchor in anchors]
    assert positions == sorted(positions)
    assert "资金仍是估计 / 辅助" in JS
    assert "主力资金" not in HTML + JS
    assert "slice(0, 2)" in JS
    assert "scenario.invalidation" in JS
    assert "change.available === true" in JS
    assert "change.confirmed" not in JS
    assert "return parsed * 100" in JS
    assert 'id="breadth-bar-unclassified"' in HTML
    assert 'id="breadth-unclassified"' in HTML
    assert 'id="breadth-total"' in HTML
    assert "median_change_pct" not in JS
    assert "limit_up_count" not in JS
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
    assert 'MAX_SECTOR_FLOW_SERIES = 48' in JS
    assert 'MAX_SECTOR_FLOW_CHART_SERIES = 16' in JS
    assert 'SECTOR_FLOW_SYMLOG_CONSTANT_CNY = 100_000_000' in JS
    assert 'data-flow-mode="cumulative"' in HTML
    assert 'data-flow-mode="delta_5m"' in HTML
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
    assert "renderSectorFlowMiniChart(displayPayload, scope)" in JS
    assert ".sector-flow-visual[open] .sector-flow-mini-chart" in CSS
    assert "document.createElementNS" in JS
    assert "point.session_segment" in JS
    assert "segment !== previousSegment" in JS
    assert "function sectorFlowTradingMinute" in JS
    assert 'segment === "am" && minute >= 9 * 60 + 30 && minute <= 11 * 60 + 30' in JS
    assert 'segment === "pm" && minute >= 13 * 60 && minute <= 15 * 60' in JS
    assert "point.tradingMinute" in JS
    assert 'breakLabel.textContent = "午间断点"' in JS
    assert 'return "11:30 / 13:00"' in JS
    assert "point.delta_5m_cny" in JS
    assert "function sectorFlowSymlog(value)" in JS
    assert "function sectorFlowSymlogInverse(value)" in JS
    assert "Math.log1p(Math.abs(value) / SECTOR_FLOW_SYMLOG_CONSTANT_CNY)" in JS
    assert "Math.expm1(Math.abs(value)) * SECTOR_FLOW_SYMLOG_CONSTANT_CNY" in JS
    assert "const SECTOR_FLOW_ENDPOINT_GAP_PX = 15" in JS
    assert "const endpointScaleKnots" in JS
    assert "const transformedFromY" in JS
    assert 'scaleLabel.textContent = "Y 轴密度自适应 · 刻度为真实金额"' in JS
    assert '"data-sector-flow-endpoint": text(entry.item.sector_key' in JS
    assert '"data-sector-flow-endpoint-label": text(label.item.sector_key' in JS
    assert "y: label.actualY + 3" in JS
    assert "label.labelY" not in JS
    assert "data-sector-flow-endpoint-connector" not in JS
    assert "labelColumns" not in JS
    assert "不会依据单点涨幅补画" in HTML + JS
    assert "近 5 分钟同源基线仍在积累" in JS
    assert "renderSectorFlowTrajectories(snapshot)" in JS
    assert "fetch(`${API_ENDPOINT}" in JS
    assert "sectorFlowCache" not in JS


def test_sector_flow_chart_expand_reuses_the_pre_rendered_svg():
    toggle_handler = re.search(
        r"const syncChartLayout = \(\) => \{(?P<body>.*?)\n\s*\};",
        JS,
        re.DOTALL,
    )

    assert toggle_handler is not None
    assert 'classList.toggle("is-chart-open", chartCard.open)' in toggle_handler.group("body")
    assert "renderSectorFlowTrajectory" not in toggle_handler.group("body")


def test_sector_flow_defense_and_offense_are_stacked_and_rendered_together():
    assert 'role="tablist"' not in HTML
    assert 'role="tabpanel"' not in HTML
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
    assert "自动出现" in JS
    assert "5分涨速" in JS
    assert "storeJson(sectorFlowSelectionStorageKey(scope)" in JS
    assert "storeText(STORAGE_KEYS.sectorFlowSurgeThreshold" in JS
    assert "fetch(`${API_ENDPOINT}" in JS
    assert "sectorFlowSelectionCache" not in JS


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
    assert "rightChange - leftChange" in observations.group("body")
    assert "板块上涨" in observations.group("body")
    assert "item.tier_label" not in observations.group("body")
    assert "净流占比" not in observations.group("body")
    assert "涨幅/广度" not in observations.group("body")
    assert "renderSectorFlowLeaders(item, latest)" in observations.group("body")
    assert 'text(item.parent_name, "")' in observations.group("body")
    assert "snapshot.leaders" in leaders.group("body")
    assert "change <= 0" not in leaders.group("body")
    assert 'text(snapshot.status_label, "领涨股数据暂缺")' in leaders.group("body")
    assert ".slice(0, 3)" in leaders.group("body")
    assert "leader.instrument_id" in leaders.group("body")


def test_stale_or_failed_data_is_prominent_and_locks_change_alerts():
    assert 'id="data-risk-overlay"' in HTML
    assert 'aria-live="assertive"' in HTML
    assert "盘面数据已经陈旧" in JS
    assert "盘面数据部分降级" in JS
    assert "盘面数据不可用" in JS
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
        r"function renderTurnover\(snapshot\) \{(?P<body>.*?)\n  \}\n\n  function sectorDirection",
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


def test_manual_refresh_requests_the_bounded_force_endpoint():
    assert 'force ? "?refresh=1" : ""' in JS
    assert 'fetchSnapshot({ force: true })' in JS


def test_watch_page_reads_only_canonical_v1_fields_and_event_reads_are_scoped():
    legacy_aliases = (
        "quality_status",
        "is_fresh",
        "advance_count",
        "decline_count",
        "up_ratio",
        "sector_name",
        "role_tags",
        "rotation.strengthening",
        "rotation.weakening",
        "change.confirmed",
        "alert.detail",
        "alert.as_of",
    )
    assert all(alias not in JS for alias in legacy_aliases)
    assert 'normalized === "strengthening"' in JS
    assert 'normalized === "weakening"' in JS
    assert 'abstain: "暂不判断"' in JS
    assert "REGIME_LABELS" in JS
    assert "alertReadKey(alert, snapshot)" in JS
    assert "snapshot.snapshot_id" in JS


def test_watch_page_is_self_contained_and_desktop_only():
    combined = HTML + CSS + JS
    assert not re.search(r'(?:href|src)=["\']https?://', HTML, re.IGNORECASE)
    assert "@import" not in CSS
    assert "@media" not in CSS
    assert "min-width: 1180px" in CSS
    assert "react" not in combined.lower()
    assert "cdn" not in combined.lower()
