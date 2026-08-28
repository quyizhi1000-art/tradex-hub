from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from tradex.dashboard import __main__ as dashboard_app


WATCH_DIR = Path(__file__).parents[1] / "src" / "tradex" / "dashboard" / "watch"
HTML = (WATCH_DIR / "index.html").read_text(encoding="utf-8")
CSS = (WATCH_DIR / "styles.css").read_text(encoding="utf-8")
JS = (WATCH_DIR / "app.js").read_text(encoding="utf-8")


def _bare_handler() -> dashboard_app.DashboardHandler:
    handler = dashboard_app.DashboardHandler.__new__(dashboard_app.DashboardHandler)
    handler.wfile = BytesIO()
    return handler


def test_daily_stock_selection_routes_preserve_bounded_history_query():
    history_calls = []
    history_handler = _bare_handler()
    history_handler.path = (
        "/api/daily-stock-selection/history?trade_date=2026-08-26&limit=30"
    )
    history_handler._handle_daily_stock_selection_history_api = (
        lambda **kwargs: history_calls.append(kwargs)
    )
    post_calls = []
    post_handler = _bare_handler()
    post_handler.path = "/api/daily-stock-selection"
    post_handler._handle_daily_stock_selection_api = lambda: post_calls.append(True)

    history_handler.do_GET()
    post_handler.do_POST()

    assert history_calls == [{"trade_date": "2026-08-26", "limit": "30"}]
    assert post_calls == [True]


def test_daily_stock_selection_generation_status_route():
    calls = []
    handler = _bare_handler()
    handler.path = "/api/daily-stock-selection/generation"
    handler._handle_daily_stock_selection_generation_api = lambda: calls.append(True)

    handler.do_GET()

    assert calls == [True]


def test_stock_selection_strategy_catalog_and_results_routes():
    catalog_calls = []
    catalog_handler = _bare_handler()
    catalog_handler.path = "/api/stock-selection/strategies?trade_date=2026-08-27"
    catalog_handler._handle_stock_selection_strategies_api = (
        lambda **kwargs: catalog_calls.append(kwargs)
    )
    result_calls = []
    result_handler = _bare_handler()
    result_handler.path = (
        "/api/stock-selection/results?trade_date=2026-08-27"
        "&strategy_id=next-session-limit-up-tendency-main-board"
    )
    result_handler._handle_stock_selection_strategy_results_api = (
        lambda **kwargs: result_calls.append(kwargs)
    )

    catalog_handler.do_GET()
    result_handler.do_GET()

    assert catalog_calls == [{"trade_date": "2026-08-27"}]
    assert result_calls == [
        {
            "trade_date": "2026-08-27",
            "strategy_id": "next-session-limit-up-tendency-main-board",
            "limit": None,
        }
    ]


@pytest.mark.parametrize("value", ["0", "366", "abc", "1.5"])
def test_daily_stock_selection_history_limit_is_bounded(value):
    with pytest.raises(ValueError):
        dashboard_app._selection_history_limit(value)


def test_daily_stock_selection_post_queues_worker_job(monkeypatch):
    payload = {
        "contract": "daily_stock_selection_generation.v1",
        "schema_version": 1,
        "job_id": "selection:2026-08-26:1",
        "state": "queued",
        "phase": "queued",
    }
    monkeypatch.setattr(
        dashboard_app,
        "generate_daily_stock_selection",
        lambda: payload,
    )
    responses = []
    handler = _bare_handler()
    handler._send_json = lambda status, body: responses.append((status, body))

    handler._handle_daily_stock_selection_api()

    assert responses == [(202, payload)]


def test_dashboard_has_no_daily_selection_scheduler_or_provider_owner():
    source = Path(dashboard_app.__file__).read_text(encoding="utf-8")

    assert "daily-stock-selection-scheduler" not in source
    assert "DailyStockSelectionService" not in source
    assert "fetch_daily_stock_factor_snapshot" not in source


def test_desktop_page_exposes_versioned_daily_stock_selection_archive():
    assert 'const STOCK_SELECTION_HISTORY_ENDPOINT = "/api/daily-stock-selection/history"' in JS
    assert 'const STOCK_SELECTION_GENERATE_ENDPOINT = "/api/daily-stock-selection"' in JS
    assert 'const STOCK_SELECTION_GENERATION_ENDPOINT = "/api/daily-stock-selection/generation"' in JS
    assert 'payload.contract !== "daily_stock_selection_archive.v1"' in JS
    assert 'payload.contract !== "daily_stock_selection_result.v1"' in JS
    assert 'payload.contract !== "daily_stock_selection_generation.v1"' in JS
    assert '["idle", "queued", "running", "succeeded", "failed"]' in JS
    assert 'new Set(["queued", "running"]).has(generation.state)' in JS
    assert 'id="stock-selection-section"' in HTML
    assert 'id="stock-selection-dialog"' in HTML
    assert 'role="tablist"' in HTML
    assert 'data-stock-selection-tab="daily"' in HTML
    assert 'data-stock-selection-tab="limit-up-tendency"' in HTML
    assert 'data-stock-selection-tab="long-upper-shadow"' in HTML
    assert 'id="stock-limit-up-tendency-table-body"' in HTML
    assert 'id="stock-pattern-table-body"' in HTML
    assert "同一 10 日窗口无收盘涨停" in HTML
    assert "完整 10 日" in HTML
    assert "完整 15 日证据" in HTML
    assert "长上影疑似试盘形态" in HTML
    assert 'const CURRENT_STOCK_SELECTION_CONFIG = "daily-stock-selection-balanced.v6"' in JS
    assert 'item.screen_version === "next-session-limit-up-tendency-main-board.v2"' in JS
    assert "区分未涨停启动与已涨停延续" in HTML
    assert 'item.screen_version === "long-upper-shadow-main-board.v3"' in JS
    assert 'id="stock-selection-generate-button"' in HTML
    assert 'id="stock-selection-date-select"' in HTML
    assert 'id="stock-selection-table-body"' in HTML
    assert "候选池不是买入建议" in HTML
    assert "收益从下一交易日开盘起验证" in HTML
    assert "renderStockSelectionCandidates(canonical.candidates)" in JS
    assert "renderStockSelectionOutcome(history)" in JS
    assert "renderStockPatternScreen(canonical.pattern_screens)" in JS
    assert "renderLimitUpTendencyScreen(canonical.limit_up_tendency_screens)" in JS
    assert 'dialog.showModal()' in JS
    assert 'dialog.addEventListener("click", (event) =>' in JS
    assert "if (event.target !== dialog) return;" in JS
    assert "closeStockSelectionDialog();" in JS
    assert 'method: "POST"' in JS
    assert "pollStockSelectionGeneration" in JS
    assert "await fetchStockSelectionHistory" in JS
    assert "innerHTML" not in JS
    assert ".stock-selection-table-scroll" in CSS
    assert "min-width: 1180px" in CSS
    assert "@media" not in CSS[CSS.index("/* Daily stock selection"):]


def test_server_shutdown_has_no_daily_selection_owner():
    source = Path(dashboard_app.__file__).read_text(encoding="utf-8")

    assert 'name="daily-stock-selection-scheduler"' not in source
    assert "_DAILY_STOCK_SELECTION_STORE" not in source
    assert "_ANALYSIS_JOB_STORE.close()" in source
