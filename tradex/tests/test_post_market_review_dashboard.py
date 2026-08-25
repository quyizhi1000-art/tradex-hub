"""Desktop/API fixtures for the archived post-market review adapter."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradex.dashboard import __main__ as dashboard_app
from tradex.market_watch.review_service import ReviewTooEarlyError


WATCH_DIR = Path(__file__).parents[1] / "src" / "tradex" / "dashboard" / "watch"
HTML = (WATCH_DIR / "index.html").read_text(encoding="utf-8")
CSS = (WATCH_DIR / "styles.css").read_text(encoding="utf-8")
JS = (WATCH_DIR / "app.js").read_text(encoding="utf-8")


def _bare_handler() -> dashboard_app.DashboardHandler:
    handler = dashboard_app.DashboardHandler.__new__(dashboard_app.DashboardHandler)
    handler.wfile = BytesIO()
    return handler


def test_post_market_review_routes_preserve_bounded_history_query():
    history_calls = []
    history_handler = _bare_handler()
    history_handler.path = (
        "/api/post-market-review/history?trade_date=2026-08-21&limit=30"
    )
    history_handler._handle_post_market_review_history_api = (
        lambda **kwargs: history_calls.append(kwargs)
    )

    post_calls = []
    post_handler = _bare_handler()
    post_handler.path = "/api/post-market-review"
    post_handler._handle_post_market_review_api = lambda: post_calls.append(True)

    history_handler.do_GET()
    post_handler.do_POST()

    assert history_calls == [{"trade_date": "2026-08-21", "limit": "30"}]
    assert post_calls == [True]


@pytest.mark.parametrize("value", ["0", "366", "abc", "1.5"])
def test_post_market_review_history_limit_is_bounded(value):
    with pytest.raises(ValueError):
        dashboard_app._review_history_limit(value)


def test_post_market_review_history_handler_returns_archive_fixture(monkeypatch):
    archive = {
        "contract": "post_market_review_archive.v1",
        "schema_version": 1,
        "trade_date": "2026-08-21",
        "dates": [{"trade_date": "2026-08-21"}],
        "review": None,
        "outcome": None,
        "learning": {"evaluated_count": 0},
        "schedule": {
            "manual_after": "20:30",
            "automatic_if_missing_after": "21:00",
            "timezone": "Asia/Shanghai",
        },
    }
    monkeypatch.setattr(
        dashboard_app,
        "get_post_market_review_history",
        lambda **kwargs: archive | {"request": kwargs},
    )
    responses = []
    handler = _bare_handler()
    handler._send_json = lambda status, payload: responses.append((status, payload))

    handler._handle_post_market_review_history_api(
        trade_date="2026-08-21",
        limit="30",
    )

    assert responses == [
        (
            200,
            archive
            | {"request": {"trade_date": "2026-08-21", "limit": 30}},
        )
    ]


def test_post_market_review_post_rejects_too_early_with_safe_message(monkeypatch):
    monkeypatch.setattr(
        dashboard_app,
        "generate_post_market_review",
        lambda: (_ for _ in ()).throw(
            ReviewTooEarlyError("当日 20:30 后才允许生成这份日复盘。")
        ),
    )
    responses = []
    handler = _bare_handler()
    handler._send_json = lambda status, payload: responses.append((status, payload))

    handler._handle_post_market_review_api()

    assert responses == [
        (409, {"error": "当日 20:30 后才允许生成这份日复盘。"})
    ]


def test_post_market_review_post_returns_versioned_result(monkeypatch):
    result = {
        "contract": "post_market_review_result.v1",
        "schema_version": 1,
        "action": "inserted",
        "review": {"contract": "post_market_review.v1"},
    }
    monkeypatch.setattr(dashboard_app, "generate_post_market_review", lambda: result)
    responses = []
    handler = _bare_handler()
    handler._send_json = lambda status, payload: responses.append((status, payload))

    handler._handle_post_market_review_api()

    assert responses == [(201, result)]


def test_post_market_review_scheduler_delegates_retry_policy(monkeypatch):
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
        "_get_post_market_review_service",
        lambda: service,
    )

    dashboard_app._post_market_review_loop(OnePassStop())

    assert calls == [("generate", None), ("wait", 30.0)]


def test_desktop_review_page_exposes_honest_archive_controls_and_sections():
    assert 'const REVIEW_HISTORY_ENDPOINT = "/api/post-market-review/history"' in JS
    assert 'const REVIEW_GENERATE_ENDPOINT = "/api/post-market-review"' in JS
    assert 'method: "POST"' in JS
    assert 'payload.contract !== "post_market_review_archive.v1"' in JS
    assert 'payload.contract !== "post_market_review_result.v1"' in JS
    assert "reviewWithPresentation(review, history.presentation)" in JS
    assert "reviewWithPresentation(result.review, result.presentation)" in JS
    assert 'id="daily-review-generate-button"' in HTML
    assert 'id="daily-review-date-select"' in HTML
    assert 'id="daily-review-quality"' in HTML
    assert 'id="daily-review-report-title"' in HTML
    assert 'id="daily-review-report-deck"' in HTML
    assert 'id="daily-review-report-contract"' in HTML
    assert 'id="daily-review-report-sections"' in HTML
    assert 'id="daily-review-watch-list"' in HTML
    assert 'id="daily-review-data-appendix"' in HTML
    assert 'id="daily-review-appendix-sections"' in HTML
    assert 'id="daily-review-calibration"' in HTML
    assert 'id="daily-review-limitations"' in HTML
    assert 'id="daily-review-learning"' in HTML
    assert 'id="daily-review-outcome"' in HTML
    assert "renderReviewArticleSections(canonical.sections)" in JS
    assert "renderReviewWatchItems(canonical.watch_items)" in JS
    assert "renderReviewAppendixSections(canonical.appendix_sections)" in JS
    assert "reviewReportToneClass(cellRecord.tone)" in JS
    assert 'createElement("table", "daily-review-report-table")' in JS
    assert 'createElement("caption", "", captionText)' in JS
    assert 'wrapper.tabIndex = 0' in JS
    assert "title: current.title || canonical.title" in JS
    assert "standfirst: current.standfirst || current.deck" in JS
    assert "sections: current.sections || canonical.sections" in JS
    assert "watch_items: current.watch_items || canonical.watch_items" in JS
    assert "appendix_sections: current.appendix_sections || current.sections" in JS
    assert "contract: current.contract || canonical.contract" in JS
    assert "limitations: current.limitations || canonical.limitations" in JS
    assert "20:30" in HTML + JS
    assert "21:00" in HTML + JS
    assert "未随档案返回" in HTML + JS
    assert '"stock_fund_flow"' in JS
    assert "数据底稿与口径" in HTML
    assert "明天只盯这几件事" in HTML
    assert "A股每日复盘" in HTML
    assert "post_market_review_presentation.v4" in HTML + JS
    assert 'id="daily-review-report-nav"' not in HTML
    assert 'id="daily-review-report-nav-list"' not in HTML
    for removed_id in (
        "daily-review-day-character",
        "daily-review-story",
        "daily-review-themes",
        "daily-review-money-making",
        "daily-review-loss-making",
        "daily-review-scenarios",
        "daily-review-indices",
        "daily-review-opportunities",
    ):
        assert f'id="{removed_id}"' not in HTML
    assert "innerHTML" not in JS


def test_desktop_review_page_names_all_required_market_coverage():
    labels = [
        "全 A 个股",
        "场内 ETF",
        "行业 / 概念板块",
        "个股 / 板块资金",
        "龙虎榜",
        "涨停 / 连板",
        "盘中轨迹",
    ]

    assert all(label in HTML for label in labels)
    assert ".daily-review-coverage" in CSS
    assert "grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));" in CSS
    assert ".daily-review-table-scroll" in CSS
    assert "overflow-x: auto" in CSS
    assert ".daily-review-report-section" in CSS
    assert ".daily-review-article-section p" in CSS
    assert ".daily-review-appendix" in CSS


def test_server_shutdown_stops_scheduler_and_closes_review_store():
    source = Path(dashboard_app.__file__).read_text(encoding="utf-8")

    assert 'name="post-market-review-scheduler"' in source
    assert "review_stop.set()" in source
    assert "review_scheduler.join()" in source
    assert "_POST_MARKET_REVIEW_STORE.close()" in source
