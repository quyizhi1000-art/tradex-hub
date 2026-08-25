"""Desktop HTTP integration tests for the provider-neutral market watch."""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest

from tradex.dashboard import __main__ as dashboard_app
from tradex.market_watch.contracts import AlertV1
from tradex.market_watch.service import MarketWatchUnavailableError


def _bare_handler() -> dashboard_app.DashboardHandler:
    handler = dashboard_app.DashboardHandler.__new__(dashboard_app.DashboardHandler)
    handler.wfile = BytesIO()
    return handler


def test_watch_asset_loader_is_allow_listed_and_uncached(tmp_path, monkeypatch):
    watch_path = tmp_path / "watch"
    watch_path.mkdir()
    (watch_path / "styles.css").write_text("body{}", encoding="utf-8")
    monkeypatch.setattr(dashboard_app, "__file__", str(tmp_path / "__main__.py"))

    body, content_type = dashboard_app._get_watch_asset("styles.css")

    assert body == b"body{}"
    assert content_type == "text/css; charset=utf-8"
    with pytest.raises(FileNotFoundError):
        dashboard_app._get_watch_asset("../config.py")


def test_market_watch_builder_receives_one_bundle_and_service_metadata(monkeypatch):
    captured = {}
    expected = object()

    def fake_builder(market_data, risk_data, **metadata):
        captured.update(
            market_data=market_data,
            risk_data=risk_data,
            metadata=metadata,
        )
        return expected

    import tradex.market_watch.analysis as analysis

    monkeypatch.setattr(analysis, "build_market_watch_snapshot", fake_builder)
    result = dashboard_app._build_market_watch_snapshot(
        {
            "market_data": {"indices": []},
            "risk_data": {"breadth": {}},
            "as_of": "2026-08-24T10:30:00+08:00",
            "sequence": 7,
            "snapshot_id": "mw-7",
        }
    )

    assert result is expected
    assert captured == {
        "market_data": {"indices": []},
        "risk_data": {"breadth": {}},
        "metadata": {
            "as_of": "2026-08-24T10:30:00+08:00",
            "sequence": 7,
            "snapshot_id": "mw-7",
        },
    }


def test_market_watch_input_rebuild_reads_risk_cache_without_forcing_upstream(
    monkeypatch,
):
    market = {"contract": "market_overview.v1"}
    risk = {"version": "risk-appetite.v2"}
    market_calls = []
    risk_calls = []

    monkeypatch.setattr(
        dashboard_app,
        "get_market_data",
        lambda force=False: market_calls.append(force) or market,
    )
    from tradex.dashboard import risk_service

    monkeypatch.setattr(
        risk_service,
        "get_risk_appetite_data",
        lambda market_data, force=False: risk_calls.append(
            (market_data, force)
        )
        or risk,
    )

    result = dashboard_app._fetch_market_watch_inputs()

    assert result == {"market_data": market, "risk_data": risk}
    assert market_calls == [False]
    assert risk_calls == [(market, False)]


def test_market_watch_input_rebuild_preserves_offense_when_overview_fails(
    monkeypatch,
):
    previous = {
        "contract": "market_watch.v1",
        "offense_sector_flow_trajectory": {
            "sectors": [{"sector_key": "advanced_packaging", "layer": "concept"}],
        },
        "freshness": {
            "status": "fresh",
            "flags": [],
            "components": [{
                "component": "indices",
                "status": "fresh",
                "flags": [],
            }],
        },
    }
    monkeypatch.setattr(
        dashboard_app,
        "_MARKET_WATCH_SERVICE",
        SimpleNamespace(
            last_snapshot=SimpleNamespace(
                model_dump=lambda mode: previous,
            )
        ),
    )
    monkeypatch.setattr(
        dashboard_app,
        "get_market_data",
        lambda force=False: (_ for _ in ()).throw(RuntimeError("overview down")),
    )
    risk_inputs = []
    from tradex.dashboard import risk_service

    monkeypatch.setattr(
        risk_service,
        "get_risk_appetite_data",
        lambda market_data, force=False: risk_inputs.append((market_data, force))
        or {"freshness": {"status": "fresh"}},
    )

    result = dashboard_app._fetch_market_watch_inputs()

    assert result["market_data"]["offense_sector_flow_trajectory"] == (
        previous["offense_sector_flow_trajectory"]
    )
    assert result["market_data"]["freshness"]["status"] == "stale"
    assert result["risk_data"]["freshness"]["status"] == "stale"
    assert "market_overview_fallback:RuntimeError" in (
        result["risk_data"]["freshness"]["flags"]
    )
    assert risk_inputs == [(result["market_data"], False)]


def test_minute_sampler_remains_the_forced_upstream_refresh_owner(monkeypatch):
    market = {"contract": "market_overview.v1"}
    market_calls = []
    risk_calls = []
    watch_calls = []

    class OnePassStop:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _delay):
            self.stopped = True

    monkeypatch.setattr(dashboard_app, "_market_state", lambda _now: {"is_open": True})
    monkeypatch.setattr(
        dashboard_app,
        "get_market_data",
        lambda force=False: market_calls.append(force) or market,
    )
    monkeypatch.setattr(
        dashboard_app,
        "_market_payload_is_current",
        lambda payload, _now: payload is market,
    )
    from tradex.dashboard import risk_service

    monkeypatch.setattr(
        risk_service,
        "get_risk_appetite_data",
        lambda market_data, **kwargs: risk_calls.append((market_data, kwargs)) or {},
    )
    monkeypatch.setattr(
        dashboard_app,
        "get_market_watch_data",
        lambda force=False: watch_calls.append(force) or {},
    )

    dashboard_app._risk_sampler_loop(OnePassStop())

    assert market_calls == [True]
    assert risk_calls == [
        (market, {"force": True, "record_trajectory": True})
    ]
    assert watch_calls == [True]


def test_market_watch_api_returns_safe_structured_unavailable_response(monkeypatch):
    alert = AlertV1(
        code="data_unavailable",
        severity="stop",
        title="盘面数据暂不可用",
        message="请暂停追单并重新核对盘面。",
        dedupe_key="market_watch:data_unavailable",
    )
    unavailable = MarketWatchUnavailableError(
        "internal detail",
        cause=RuntimeError("paid-provider-secret"),
        alerts=(alert,),
    )
    monkeypatch.setattr(
        dashboard_app,
        "get_market_watch_data",
        lambda force=False: (_ for _ in ()).throw(unavailable),
    )
    responses = []
    handler = _bare_handler()
    handler._send_json = lambda status, payload: responses.append((status, payload))

    handler._handle_market_watch_api(force=True)

    assert responses[0][0] == 503
    payload = responses[0][1]
    assert payload["contract"] == "market_watch.v1"
    assert payload["schema_version"] == 1
    assert payload["alerts"] == [alert.model_dump(mode="json")]
    assert "paid-provider-secret" not in str(payload)


def test_market_watch_route_passes_manual_refresh_flag(monkeypatch):
    calls = []
    handler = _bare_handler()
    handler.path = "/api/market-watch?refresh=1"
    handler._handle_market_watch_api = lambda force=False: calls.append(force)

    handler.do_GET()

    assert calls == [True]


def test_market_watch_data_records_the_strict_snapshot(monkeypatch):
    snapshot = SimpleNamespace(model_dump=lambda mode: {"contract": "market_watch.v1"})
    recorded = []
    service_calls = []
    monkeypatch.setattr(
        dashboard_app,
        "_get_market_watch_service",
        lambda: SimpleNamespace(
            get=lambda **kwargs: service_calls.append(kwargs) or snapshot
        ),
    )
    monkeypatch.setattr(dashboard_app, "_record_market_watch_snapshot", recorded.append)

    payload = dashboard_app.get_market_watch_data(force=True)

    assert payload == {"contract": "market_watch.v1"}
    assert recorded == [snapshot]
    assert service_calls == [
        {"force": True, "stale_while_revalidate": False}
    ]


def test_normal_market_watch_read_uses_stale_while_revalidate(monkeypatch):
    snapshot = SimpleNamespace(model_dump=lambda mode: {"contract": "market_watch.v1"})
    service_calls = []
    monkeypatch.setattr(
        dashboard_app,
        "_get_market_watch_service",
        lambda: SimpleNamespace(
            get=lambda **kwargs: service_calls.append(kwargs) or snapshot
        ),
    )
    monkeypatch.setattr(dashboard_app, "_record_market_watch_snapshot", lambda _snapshot: None)

    dashboard_app.get_market_watch_data(force=False)

    assert service_calls == [
        {"force": False, "stale_while_revalidate": True}
    ]


def test_market_watch_api_marks_a_background_refresh(monkeypatch):
    monkeypatch.setattr(
        dashboard_app,
        "get_market_watch_data",
        lambda force=False: {"contract": "market_watch.v1"},
    )
    monkeypatch.setattr(
        dashboard_app,
        "_get_market_watch_service",
        lambda: SimpleNamespace(refreshing=True),
    )
    responses = []
    handler = _bare_handler()
    handler._send_json = lambda status, payload, **kwargs: responses.append(
        (status, payload, kwargs)
    )

    handler._handle_market_watch_api(force=False)

    assert responses == [
        (
            200,
            {"contract": "market_watch.v1"},
            {"headers": {"X-Tradex-Refresh-State": "background"}},
        )
    ]


def test_history_adapter_returns_dates_timeline_and_alerts(monkeypatch):
    store = SimpleNamespace(
        config_version="market-watch-policy.v1",
        list_dates=lambda: [
            {"trade_date": "2026-08-24", "sample_count": 2, "alert_count": 1}
        ],
        get_timeline=lambda trade_date, limit=None: [
            {"trade_date": trade_date, "payload": {"snapshot_id": "mw-1"}}
        ],
        get_alerts=lambda trade_date, limit=None: [
            {"trade_date": trade_date, "alert": {"code": "market_caution"}}
        ],
    )
    monkeypatch.setattr(dashboard_app, "_get_market_watch_history_store", lambda: store)
    monkeypatch.setattr(dashboard_app, "_MARKET_WATCH_HISTORY_ERROR", None)

    payload = dashboard_app.get_market_watch_history(limit=20)

    assert payload["contract"] == "market_watch_history.v1"
    assert payload["schema_version"] == 1
    assert payload["trade_date"] == "2026-08-24"
    assert payload["samples"][0]["payload"]["snapshot_id"] == "mw-1"
    assert payload["alerts"][0]["alert"]["code"] == "market_caution"
    assert payload["recording"] == {"status": "ready", "error": None}


@pytest.mark.parametrize("value", ["0", "1001", "abc", "1.5"])
def test_history_limit_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        dashboard_app._history_limit(value)


@pytest.mark.parametrize("value", ["0", "31", "250", "abc", "1.5"])
def test_history_days_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        dashboard_app._history_days(value)


def test_empty_session_evaluation_is_honestly_insufficient(monkeypatch):
    monkeypatch.setattr(
        dashboard_app,
        "get_market_watch_history",
        lambda **kwargs: {
            "trade_date": "2026-08-24",
            "samples": [],
            "alerts": [],
        },
    )

    payload = dashboard_app.get_market_watch_evaluation(trade_date="2026-08-24")

    assert payload["contract"] == "market_watch_evaluation.v1"
    assert payload["acceptance"]["verdict"] == "insufficient"
    assert payload["metrics"]["coverage"]["sample_count"] == 0


def test_persisted_alert_envelope_is_narrowed_for_the_evaluator():
    event = {
        "history_contract": "market_watch_history.v1",
        "history_schema_version": 1,
        "config_version": "market-watch-policy.v1",
        "event_key": "event-1",
        "trade_date": "2026-08-24",
        "minute_bucket": "2026-08-24T10:00:00+08:00",
        "observed_at": "2026-08-24T10:00:05+08:00",
        "recorded_at": "2026-08-24T10:00:06+08:00",
        "snapshot_id": "mw-1",
        "sequence": 1,
        "dedupe_key": "market_watch:data_stale",
        "code": "data_stale",
        "severity": "stop",
        "alert": {
            "code": "data_stale",
            "severity": "stop",
            "title": "数据陈旧",
            "message": "暂停判断。",
            "dedupe_key": "market_watch:data_stale",
        },
    }

    narrowed = dashboard_app._evaluation_alerts([event])

    from tradex.market_watch.evaluation import evaluate_market_watch_session

    report = evaluate_market_watch_session(
        [],
        alerts=narrowed,
        trade_date="2026-08-24",
    )
    assert report.metrics.alerts.source == "explicit"
    assert report.metrics.alerts.emitted_alert_count == 1


def test_multi_day_evaluation_reads_dates_in_chronological_order(monkeypatch):
    store = SimpleNamespace(
        list_dates=lambda limit: [
            {"trade_date": "2026-08-25"},
            {"trade_date": "2026-08-24"},
        ][:limit],
        get_timeline=lambda trade_date, limit=None: [
            {"payload": {"trade_date_marker": trade_date}}
        ],
        get_alerts=lambda trade_date, limit=None: [
            {
                "trade_date": trade_date,
                "observed_at": f"{trade_date}T10:00:00+08:00",
                "snapshot_id": f"mw-{trade_date}",
                "alert": {"code": "marker"},
            }
        ],
    )
    captured = {}

    class Report:
        def model_dump(self, mode):
            return {"contract": "market_watch_evaluation.v1", "scope": "multi_day"}

    import tradex.market_watch.evaluation as evaluation

    def fake_evaluate(samples, alerts, config):
        captured["samples"] = samples
        captured["alerts"] = alerts
        captured["config"] = config
        return Report()

    monkeypatch.setattr(dashboard_app, "_get_market_watch_history_store", lambda: store)
    monkeypatch.setattr(evaluation, "evaluate_market_watch_history", fake_evaluate)

    payload = dashboard_app.get_market_watch_evaluation(days=2)

    assert payload["scope"] == "multi_day"
    assert [item["trade_date_marker"] for item in captured["samples"]] == [
        "2026-08-24",
        "2026-08-25",
    ]
    assert [item["trade_date"] for item in captured["alerts"]] == [
        "2026-08-24",
        "2026-08-25",
    ]
    assert captured["config"].minimum_sessions_for_multi_day == 2


def test_history_and_evaluation_routes_preserve_bounded_query(monkeypatch):
    history_calls = []
    evaluation_calls = []
    history_handler = _bare_handler()
    history_handler.path = "/api/market-watch/history?trade_date=2026-08-24&limit=20"
    history_handler._handle_market_watch_history_api = lambda **kwargs: history_calls.append(kwargs)
    history_handler.do_GET()

    evaluation_handler = _bare_handler()
    evaluation_handler.path = "/api/market-watch/evaluation?trade_date=2026-08-24"
    evaluation_handler._handle_market_watch_evaluation_api = (
        lambda **kwargs: evaluation_calls.append(kwargs)
    )
    evaluation_handler.do_GET()

    assert history_calls == [{"trade_date": "2026-08-24", "limit": "20"}]
    assert evaluation_calls == [{"trade_date": "2026-08-24", "days": None}]
