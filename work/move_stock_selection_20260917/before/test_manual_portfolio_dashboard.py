from __future__ import annotations

import json
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradex.analysis_jobs import (
    MANUAL_PORTFOLIO_INTRADAY_ANALYSIS,
    MANUAL_PORTFOLIO_OUTLOOK,
    AnalysisJobReader,
    AnalysisJobStore,
)
from tradex.dashboard import __main__ as dashboard_app
from tradex.dashboard import collector_worker
from tradex.dashboard.__main__ import DashboardWriteRejected
from tradex.manual_portfolio.contracts import (
    ManualPortfolioMarketSnapshotV1,
    ManualPortfolioQuoteV1,
    ManualPortfolioSampleV1,
)
from tradex.manual_portfolio.store import ManualPortfolioStore, portfolio_revision


WATCH_DIR = Path(__file__).parents[1] / "src" / "tradex" / "dashboard" / "watch"


def _request(payload, **headers):
    body = json.dumps(payload).encode("utf-8")
    defaults = {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": str(len(body)),
        "Origin": "http://127.0.0.1:8765",
        "Host": "127.0.0.1:8765",
    }
    defaults.update(headers)
    return SimpleNamespace(headers=defaults, rfile=BytesIO(body))


def test_same_origin_json_guard_accepts_only_bounded_local_json():
    assert dashboard_app._read_same_origin_json_body(
        _request({"action": "add", "instrument_id": "000001"})
    )["action"] == "add"

    with pytest.raises(DashboardWriteRejected) as form:
        dashboard_app._read_same_origin_json_body(
            _request({}, **{"Content-Type": "application/x-www-form-urlencoded"})
        )
    assert form.value.status == 415

    with pytest.raises(DashboardWriteRejected) as cross_origin:
        dashboard_app._read_same_origin_json_body(
            _request({}, Origin="http://evil.example")
        )
    assert cross_origin.value.status == 403

    with pytest.raises(DashboardWriteRejected) as missing_origin:
        dashboard_app._read_same_origin_json_body(_request({}, Origin=""))
    assert missing_origin.value.status == 403

    with pytest.raises(DashboardWriteRejected) as oversized:
        dashboard_app._read_same_origin_json_body(
            _request({}, **{"Content-Length": "20000"})
        )
    assert oversized.value.status == 413


def test_post_route_guard_blocks_cross_origin_before_any_write_handler_runs():
    handler = dashboard_app.DashboardHandler.__new__(dashboard_app.DashboardHandler)
    handler.path = "/api/manual-portfolio"
    request = _request(
        {"action": "add", "instrument_id": "000001"},
        Origin="http://evil.example",
    )
    handler.headers = request.headers
    handler.rfile = request.rfile
    responses = []
    writes = []
    handler._send_json = lambda status, payload: responses.append((status, payload))
    handler._handle_manual_portfolio_command_api = lambda command: writes.append(command)

    handler.do_POST()

    assert responses == [(403, {"error": "写接口要求同源本地页面"})]
    assert writes == []


def test_manual_portfolio_dashboard_payload_crud_is_code_only(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADEX_MANUAL_PORTFOLIO_DB", str(tmp_path / "portfolio.sqlite3"))

    added = dashboard_app.mutate_manual_portfolio(
        {"action": "add", "instrument_id": "000001", "note": "观察"}
    )
    read = dashboard_app.get_manual_portfolio()

    assert added["contract"] == "manual_portfolio_command_result.v1"
    assert read["contract"] == "manual_portfolio.v1"
    assert read["manual_fact_notice"] == "手动维护，并非券商账户事实"
    assert read["enabled_limit"] == 40
    assert read["items"][0]["instrument_id"] == "000001.SZ"
    assert not any(
        key in read["items"][0]
        for key in ("quantity", "cost", "purchase_date", "profit", "cash")
    )

    updated = dashboard_app.mutate_manual_portfolio(
        {"action": "update", "instrument_id": "000001", "enabled": False}
    )
    assert updated["entry"]["enabled"] is False

    deleted = dashboard_app.mutate_manual_portfolio(
        {"action": "delete", "instrument_id": "000001"}
    )
    assert deleted["action"] == "deleted"
    assert deleted["portfolio"]["items"] == []


def test_manual_portfolio_dashboard_rejects_non_boolean_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADEX_MANUAL_PORTFOLIO_DB", str(tmp_path / "portfolio.sqlite3"))

    with pytest.raises(ValueError, match="布尔"):
        dashboard_app.mutate_manual_portfolio(
            {"action": "add", "instrument_id": "000001", "enabled": "false"}
        )


def test_manual_portfolio_analysis_views_read_dated_and_append_only_artifacts(
    tmp_path,
    monkeypatch,
):
    portfolio_path = tmp_path / "portfolio.sqlite3"
    analysis_path = tmp_path / "analysis.sqlite3"
    monkeypatch.setenv("TRADEX_MANUAL_PORTFOLIO_DB", str(portfolio_path))
    dashboard_app.mutate_manual_portfolio(
        {"action": "add", "instrument_id": "000001", "display_name": "平安银行"}
    )
    portfolio = dashboard_app.get_manual_portfolio()
    revision = portfolio["revision"]
    with AnalysisJobStore(analysis_path) as store:
        store.put_artifact(
            MANUAL_PORTFOLIO_OUTLOOK,
            scope_key="portfolio:legacy-revision",
            source_revision="9" * 64,
            payload={
                "contract": "manual_portfolio_outlook.v1",
                "source_trading_date": "2026-08-31",
                "generated_at": "2026-08-31T18:30:00+08:00",
                "items": [{"instrument_id": "000001.SZ", "status": "conditional"}],
            },
        )
        store.put_artifact(
            MANUAL_PORTFOLIO_OUTLOOK,
            scope_key="date:2026-09-01",
            source_revision="a" * 64,
            payload={
                "contract": "manual_portfolio_outlook.v1",
                "source_trading_date": "2026-09-01",
                "generated_at": "2026-09-01T18:30:00+08:00",
                "items": [{"instrument_id": "000001.SZ", "status": "conditional"}],
                "daily_review": {
                    "reviewed_outlook_date": "2026-08-31",
                    "realized_trading_date": "2026-09-01",
                    "generated_at": "2026-09-01T18:30:00+08:00",
                    "self_summary": "只复核可核验条件。",
                    "items": [{"instrument_id": "000001.SZ", "outcome": "mixed"}],
                },
            },
        )
        for minute in (35, 36):
            store.put_artifact(
                MANUAL_PORTFOLIO_INTRADAY_ANALYSIS,
                scope_key=f"snapshot:{minute}",
                source_revision=str(minute) * 32,
                payload={
                    "contract": "manual_portfolio_intraday_analysis.v1",
                    "portfolio_revision": revision,
                    "source_snapshot_revision": str(minute) * 32,
                    "source_trading_date": "2026-09-02",
                    "generated_at": f"2026-09-02T09:{minute}:00+08:00",
                    "items": [{"instrument_id": "000001.SZ", "status": "conditional"}],
                },
            )
        with ManualPortfolioStore(portfolio_path) as portfolio_store:
            portfolio_store.record_snapshot(
                ManualPortfolioMarketSnapshotV1(
                    portfolio_revision=revision,
                    snapshot_revision="f" * 64,
                    generated_at=datetime.fromisoformat("2026-09-02T09:36:00+08:00"),
                    trading_date=datetime.fromisoformat("2026-09-02T09:36:00+08:00").date(),
                    item_count=0,
                    items=(),
                )
            )
        monkeypatch.setattr(dashboard_app, "_get_analysis_job_reader", lambda: store)

        history = dashboard_app.get_manual_portfolio_analysis_history("000001")
        intraday = dashboard_app.get_manual_portfolio_intraday_analysis_history()

    assert history["display_name"] == "平安银行"
    assert [page["source_trading_date"] for page in history["outlook_pages"]] == [
        "2026-09-01",
        "2026-08-31",
    ]
    assert len(history["review_pages"]) == 1
    assert [item["generated_at"] for item in intraday["analyses"]] == [
        "2026-09-02T09:35:00+08:00",
        "2026-09-02T09:36:00+08:00",
    ]
    assert intraday["analyses"][0]["items"][0]["display_name"] == "平安银行"


def test_outlook_can_be_queued_before_matching_market_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADEX_MANUAL_PORTFOLIO_DB", str(tmp_path / "portfolio.sqlite3"))
    dashboard_app.mutate_manual_portfolio(
        {"action": "add", "instrument_id": "000001", "note": "观察"}
    )

    queued = dashboard_app.generate_manual_portfolio_outlook()
    status = dashboard_app.get_manual_portfolio_outlook_generation()

    assert queued["state"] == "queued"
    assert queued["phase"] == "waiting_for_market"
    assert queued["readiness"]["state"] == "waiting_for_market"
    assert queued["readiness"]["next_collection_at"] is not None
    assert status["phase"] == "waiting_for_market"
    assert status["readiness"]["automatic_generation_requested"] is True


def test_collector_dispatches_waiting_outlook_after_matching_snapshot(
    tmp_path,
    monkeypatch,
):
    portfolio_path = tmp_path / "portfolio.sqlite3"
    analysis_path = tmp_path / "analysis.sqlite3"
    monkeypatch.setenv("TRADEX_MANUAL_PORTFOLIO_DB", str(portfolio_path))
    monkeypatch.setenv("TRADEX_ANALYSIS_DB", str(analysis_path))
    observed = datetime.fromisoformat("2026-09-02T15:10:30+08:00")
    with AnalysisJobStore(analysis_path):
        pass
    dashboard_app.mutate_manual_portfolio(
        {"action": "add", "instrument_id": "000001"}
    )
    with ManualPortfolioStore(portfolio_path) as store:
        revision = portfolio_revision(store.list_entries())
        store.request_outlook(revision, requested_at=observed)
        snapshot = ManualPortfolioMarketSnapshotV1(
            portfolio_revision=revision,
            snapshot_revision="b" * 64,
            generated_at=observed,
            trading_date=observed.date(),
            item_count=1,
            items=(
                ManualPortfolioQuoteV1(
                    instrument_id="000001.SZ",
                    trading_date=observed.date(),
                    status="accepted",
                    last_price=10.2,
                    session_high=10.3,
                    session_low=10.0,
                    provider="fixture",
                    provider_as_of=observed.replace(hour=15, minute=0),
                    fetched_at=observed,
                    samples=(
                        ManualPortfolioSampleV1(
                            observed_at=observed.replace(hour=14, minute=59),
                            price=10.2,
                        ),
                        ManualPortfolioSampleV1(
                            observed_at=observed.replace(hour=15, minute=0),
                            price=10.2,
                        ),
                    ),
                ),
            ),
        )

    monkeypatch.setattr(collector_worker, "_now", lambda: observed)
    monkeypatch.setattr(
        "tradex.manual_portfolio.market.refresh_manual_portfolio_market",
        lambda store, now: store.record_snapshot(snapshot),
    )

    result = collector_worker._refresh_manual_portfolio_market()
    with AnalysisJobReader(analysis_path) as reader:
        job = reader.latest_job(
            MANUAL_PORTFOLIO_OUTLOOK,
            scope_key=f"portfolio:{revision}",
        )

    assert result["outlook_generation"]["state"] == "queued"
    assert job["trigger"] == "manual-after-market-refresh"
    assert result["intraday_analysis_generation"]["state"] == "queued"


def test_collector_queues_one_automatic_post_close_outlook_for_daily_review(
    tmp_path,
    monkeypatch,
):
    portfolio_path = tmp_path / "portfolio.sqlite3"
    analysis_path = tmp_path / "analysis.sqlite3"
    monkeypatch.setenv("TRADEX_MANUAL_PORTFOLIO_DB", str(portfolio_path))
    monkeypatch.setenv("TRADEX_ANALYSIS_DB", str(analysis_path))
    observed = datetime.fromisoformat("2026-09-02T15:10:00+08:00")
    with AnalysisJobStore(analysis_path):
        pass
    dashboard_app.mutate_manual_portfolio({"action": "add", "instrument_id": "000001"})
    with ManualPortfolioStore(portfolio_path) as store:
        revision = portfolio_revision(store.list_entries())
    snapshot = ManualPortfolioMarketSnapshotV1(
        portfolio_revision=revision,
        snapshot_revision="e" * 64,
        generated_at=observed,
        trading_date=observed.date(),
        item_count=1,
        items=(
            ManualPortfolioQuoteV1(
                instrument_id="000001.SZ",
                trading_date=observed.date(),
                status="accepted",
                last_price=10.2,
                session_change_pct=1.0,
                session_high=10.3,
                session_low=10.0,
                provider="fixture",
                provider_as_of=observed,
                fetched_at=observed,
                samples=(
                    ManualPortfolioSampleV1(
                        observed_at=observed.replace(hour=14, minute=59),
                        price=10.2,
                    ),
                    ManualPortfolioSampleV1(
                        observed_at=observed.replace(hour=15, minute=0),
                        price=10.2,
                    ),
                ),
            ),
        ),
    )
    monkeypatch.setattr(collector_worker, "_now", lambda: observed)
    monkeypatch.setattr(
        "tradex.manual_portfolio.market.refresh_manual_portfolio_market",
        lambda store, now: store.record_snapshot(snapshot),
    )

    result = collector_worker._refresh_manual_portfolio_market()
    with AnalysisJobReader(analysis_path) as reader:
        job = reader.latest_job(
            MANUAL_PORTFOLIO_OUTLOOK,
            scope_key=f"date:{observed.date()}:portfolio:{revision}",
        )

    assert result["outlook_generation"]["state"] == "queued"
    assert job["trigger"] == "automatic-after-close"


def test_manual_portfolio_desktop_ui_exposes_only_observation_scope():
    html = (WATCH_DIR / "index.html").read_text(encoding="utf-8")
    js = (WATCH_DIR / "app.js").read_text(encoding="utf-8")
    css = (WATCH_DIR / "styles.css").read_text(encoding="utf-8")

    assert 'id="manual-portfolio-section"' in html
    assert 'class="intraday-focus-grid"' in html
    assert html.index('id="intraday-macd-j-section"') < html.index('id="manual-portfolio-section"')
    assert 'id="manual-portfolio-dialog"' in html
    assert 'id="manual-portfolio-open-button"' in html
    assert 'id="manual-portfolio-preview"' in html
    assert 'id="manual-portfolio-analysis-dialog"' in html
    assert 'data-manual-portfolio-analysis-tab="outlook"' in html
    assert 'data-manual-portfolio-analysis-tab="review"' in html
    assert 'data-manual-portfolio-live-tab="analysis"' in html
    assert 'id="manual-portfolio-analysis-past"' in html
    assert "← 往前日期" in html
    assert 'id="manual-portfolio-analysis-date-select"' in html
    assert "跳选日期" in html
    assert 'id="manual-portfolio-analysis-future"' in html
    assert "往后日期 →" in html
    assert "股票代码或名称" in html
    assert "手动维护，并非券商账户事实" in html
    assert "最多启用 40 个代码" in html
    assert "页面关闭不保证送达" in html
    assert "function renderManualPortfolioSummary(" in js
    assert "function openManualPortfolioDialog()" in js
    assert 'byId("manual-portfolio-open-button").addEventListener("click", openManualPortfolioDialog)' in js
    assert ".intraday-focus-grid" in css
    assert "grid-template-columns: minmax(0, 2.15fr) minmax(330px, 0.85fr)" in css
    assert "manual_portfolio_outlook.v1" in js
    assert "本地关系库没有可用归属；不使用供应商板块名称补位。" in js
    assert "明早按这个顺序复核" in js
    assert "昨天有用的部分" in js
    assert "manual_portfolio_market_snapshot.v1" in js
    assert "manual_portfolio_intraday_analysis.v1" in js
    assert "不会覆盖昨晚生成的次日前瞻" in html
    assert "回撤观察区" in js
    assert "压力观察区" in js
    assert "盘面联动" in js
    assert "排队生成前瞻" in js
    assert "最早" in js
    assert "采集后自动生成" in js
    assert 'analysis.dataset.manualPortfolioAction = "analysis"' in js
    assert "function renderManualPortfolioAnalysisArchive()" in js
    assert "function selectManualPortfolioAnalysisDate(" in js
    assert "function renderManualPortfolioIntradayAnalysis(payload)" in js
    assert "display_name" in js
    assert ".manual-portfolio-intraday-log" in css
    assert ".manual-portfolio-analysis-page[hidden]" in css
    for forbidden in ("成本线", "真实盈亏", "自动下单", "交割单", "成交历史"):
        assert forbidden not in html
