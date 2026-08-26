from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradex.dashboard import __main__ as dashboard_app
from tradex.stock_selection.service import (
    SelectionDataUnavailableError,
    SelectionTooEarlyError,
)


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


@pytest.mark.parametrize("value", ["0", "366", "abc", "1.5"])
def test_daily_stock_selection_history_limit_is_bounded(value):
    with pytest.raises(ValueError):
        dashboard_app._selection_history_limit(value)


def test_daily_stock_selection_post_uses_safe_domain_error(monkeypatch):
    monkeypatch.setattr(
        dashboard_app,
        "generate_daily_stock_selection",
        lambda: (_ for _ in ()).throw(
            SelectionTooEarlyError("当日 18:00 后才允许生成每日选股。")
        ),
    )
    responses = []
    handler = _bare_handler()
    handler._send_json = lambda status, payload: responses.append((status, payload))

    handler._handle_daily_stock_selection_api()

    assert responses == [(409, {"error": "当日 18:00 后才允许生成每日选股。"})]


def test_daily_stock_selection_post_starts_background_job(monkeypatch):
    payload = {
        "contract": "daily_stock_selection_generation.v1",
        "schema_version": 1,
        "job_id": "selection:2026-08-26:1",
        "state": "running",
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


def test_daily_stock_selection_post_reports_provider_unavailable(monkeypatch):
    monkeypatch.setattr(
        dashboard_app,
        "generate_daily_stock_selection",
        lambda: (_ for _ in ()).throw(
            SelectionDataUnavailableError("Tushare 每日选股数据暂不可用，未生成候选池。")
        ),
    )
    responses = []
    handler = _bare_handler()
    handler._send_json = lambda status, payload: responses.append((status, payload))

    handler._handle_daily_stock_selection_api()

    assert responses == [
        (503, {"error": "Tushare 每日选股数据暂不可用，未生成候选池。"})
    ]


def test_daily_stock_selection_scheduler_delegates_retry_policy(monkeypatch):
    calls = []

    class OnePassStop:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            calls.append(("wait", seconds))
            self.stopped = True

    service = SimpleNamespace(
        maybe_generate_automatic=lambda: calls.append(("generate", None))
        or {"action": "not_due"}
    )
    monkeypatch.setattr(
        dashboard_app,
        "_get_daily_stock_selection_service",
        lambda: service,
    )

    dashboard_app._daily_stock_selection_loop(OnePassStop())

    assert calls == [("generate", None), ("wait", 30.0)]


def test_desktop_page_exposes_versioned_daily_stock_selection_archive():
    assert 'const STOCK_SELECTION_HISTORY_ENDPOINT = "/api/daily-stock-selection/history"' in JS
    assert 'const STOCK_SELECTION_GENERATE_ENDPOINT = "/api/daily-stock-selection"' in JS
    assert 'const STOCK_SELECTION_GENERATION_ENDPOINT = "/api/daily-stock-selection/generation"' in JS
    assert 'payload.contract !== "daily_stock_selection_archive.v1"' in JS
    assert 'payload.contract !== "daily_stock_selection_result.v1"' in JS
    assert 'payload.contract !== "daily_stock_selection_generation.v1"' in JS
    assert 'id="stock-selection-section"' in HTML
    assert 'id="stock-selection-dialog"' in HTML
    assert 'role="tablist"' in HTML
    assert 'data-stock-selection-tab="daily"' in HTML
    assert 'data-stock-selection-tab="long-upper-shadow"' in HTML
    assert 'id="stock-pattern-table-body"' in HTML
    assert 'id="stock-selection-generate-button"' in HTML
    assert 'id="stock-selection-date-select"' in HTML
    assert 'id="stock-selection-table-body"' in HTML
    assert "候选池不是买入建议" in HTML
    assert "收益从下一交易日开盘起验证" in HTML
    assert "renderStockSelectionCandidates(canonical.candidates)" in JS
    assert "renderStockSelectionOutcome(history)" in JS
    assert "renderStockPatternScreen(canonical.pattern_screens)" in JS
    assert 'dialog.showModal()' in JS
    assert 'method: "POST"' in JS
    assert "pollStockSelectionGeneration" in JS
    assert "innerHTML" not in JS
    assert ".stock-selection-table-scroll" in CSS
    assert "min-width: 1180px" in CSS
    assert "@media" not in CSS[CSS.index("/* Daily stock selection"):]


def test_server_shutdown_stops_and_closes_daily_selection_owner():
    source = Path(dashboard_app.__file__).read_text(encoding="utf-8")

    assert 'name="daily-stock-selection-scheduler"' in source
    assert "selection_stop.set()" in source
    assert "selection_scheduler.join()" in source
    assert "_DAILY_STOCK_SELECTION_STORE.close()" in source
