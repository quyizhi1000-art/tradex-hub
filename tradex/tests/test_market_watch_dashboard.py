"""Desktop HTTP integration tests for the provider-neutral market watch."""

from __future__ import annotations

import inspect
from io import BytesIO
from types import SimpleNamespace

import pytest

from tradex.dashboard import __main__ as dashboard_app
from tradex.market_watch.web_api import MarketWatchHttpResponse


def _bare_handler() -> dashboard_app.DashboardHandler:
    handler = dashboard_app.DashboardHandler.__new__(dashboard_app.DashboardHandler)
    handler.wfile = BytesIO()
    handler.headers = {}
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


def test_dashboard_process_has_no_market_watch_sampler_or_persistence_owner():
    source = inspect.getsource(dashboard_app.main)

    assert not hasattr(dashboard_app, "_risk_sampler_loop")
    assert not hasattr(dashboard_app, "_record_market_watch_snapshot")
    assert "market-watch-sampler" not in source
    assert "get_market_data" not in source


def test_market_watch_routes_preserve_revision_and_exact_sector_selection():
    calls = []
    revision = "a" * 64
    trajectory_revision = "b" * 64

    status_handler = _bare_handler()
    status_handler.path = "/api/market-watch/collection-status"
    status_handler._handle_market_watch_collection_status_api = (
        lambda: calls.append(("status", {}))
    )
    status_handler.do_GET()

    summary_handler = _bare_handler()
    summary_handler.path = (
        "/api/market-watch/summary?source_snapshot_revision=" + revision
    )
    summary_handler._handle_market_watch_summary_api = (
        lambda **kwargs: calls.append(("summary", kwargs))
    )
    summary_handler.do_GET()

    detail_handler = _bare_handler()
    detail_handler.path = (
        "/api/market-watch/trajectory?direction=defense"
        "&sector_keys=electric_power,bank&sector_keys=coal"
        f"&source_snapshot_revision={revision}"
        f"&trajectory_revision={trajectory_revision}"
    )
    detail_handler._handle_market_watch_trajectory_api = (
        lambda **kwargs: calls.append(("trajectory", kwargs))
    )
    detail_handler.do_GET()

    assert calls == [
        ("status", {}),
        ("summary", {"source_snapshot_revision": revision}),
        (
            "trajectory",
            {
                "direction": "defense",
                "sector_keys": ("electric_power", "bank", "coal"),
                "source_snapshot_revision": revision,
                "trajectory_revision": trajectory_revision,
            },
        ),
    ]


def test_manual_daily_recovery_route_is_a_single_post_command():
    calls = []
    handler = _bare_handler()
    handler.path = "/api/market-watch/daily-recovery"
    handler._handle_market_watch_daily_recovery_api = (
        lambda: calls.append("daily-recovery")
    )

    handler.do_POST()

    assert calls == ["daily-recovery"]


def test_market_watch_http_handlers_forward_etag_without_provider_refresh(
    monkeypatch,
):
    revision = "a" * 64
    response = MarketWatchHttpResponse(
        status_code=304,
        headers=(("ETag", '"cached"'),),
        body=b"",
    )
    calls = []
    api = SimpleNamespace(
        get_collection_status=lambda **kwargs: calls.append(("status", kwargs))
        or response,
        get_summary=lambda **kwargs: calls.append(("summary", kwargs)) or response,
        get_detail=lambda **kwargs: calls.append(("detail", kwargs)) or response,
    )
    monkeypatch.setattr(dashboard_app, "_get_market_watch_web_api", lambda: api)
    monkeypatch.setattr(
        dashboard_app,
        "get_market_watch_data",
        lambda **_kwargs: pytest.fail("read-only Web route refreshed providers"),
    )
    handler = _bare_handler()
    handler.headers = {"If-None-Match": '"cached"'}
    handler._send_market_watch_response = lambda item: calls.append(("send", item))

    handler._handle_market_watch_collection_status_api()
    handler._handle_market_watch_summary_api(source_snapshot_revision=revision)
    handler._handle_market_watch_trajectory_api(
        direction="defense",
        sector_keys=("bank",),
        source_snapshot_revision=revision,
        trajectory_revision="b" * 64,
    )

    assert calls == [
        ("status", {"if_none_match": '"cached"'}),
        ("send", response),
        (
            "summary",
            {
                "source_snapshot_revision": revision,
                "if_none_match": '"cached"',
            },
        ),
        ("send", response),
        (
            "detail",
            {
                "direction": "defense",
                "sector_keys": ("bank",),
                "source_snapshot_revision": revision,
                "trajectory_revision": "b" * 64,
                "if_none_match": '"cached"',
            },
        ),
        ("send", response),
    ]


def test_market_watch_response_writer_preserves_canonical_bytes_and_304_body():
    writes = []
    headers = []
    handler = _bare_handler()
    handler.send_response = lambda status: writes.append(("status", status))
    handler.send_header = lambda name, value: headers.append((name, value))
    handler.end_headers = lambda: writes.append(("end", None))
    body = b'{"contract":"market_watch_summary.v1"}'

    handler._send_market_watch_response(
        MarketWatchHttpResponse(
            status_code=200,
            headers=(("Content-Type", "application/json; charset=utf-8"),),
            body=body,
        )
    )

    assert handler.wfile.getvalue() == body
    assert ("Content-Length", str(len(body))) in headers

    unchanged = _bare_handler()
    unchanged.send_response = lambda status: writes.append(("status", status))
    unchanged.send_header = lambda name, value: headers.append((name, value))
    unchanged.end_headers = lambda: writes.append(("end", None))
    unchanged._send_market_watch_response(
        MarketWatchHttpResponse(status_code=304, headers=(), body=b"")
    )
    assert unchanged.wfile.getvalue() == b""


def test_legacy_market_watch_route_is_gone_and_never_refreshes(monkeypatch):
    responses = []
    handler = _bare_handler()
    handler.path = "/api/market-watch?refresh=1"
    handler._send_json = lambda status, payload: responses.append((status, payload))
    monkeypatch.setattr(
        dashboard_app,
        "get_market_watch_data",
        lambda **_kwargs: pytest.fail("legacy route refreshed providers"),
    )

    handler.do_GET()

    assert responses[0][0] == 410
    assert responses[0][1]["contract"] == "market_watch_api_moved.v1"


def test_market_watch_data_has_no_persistence_side_effect(monkeypatch):
    snapshot = SimpleNamespace(model_dump=lambda mode: {"contract": "market_watch.v1"})
    service_calls = []
    monkeypatch.setattr(
        dashboard_app,
        "_get_market_watch_service",
        lambda: SimpleNamespace(
            get=lambda **kwargs: service_calls.append(kwargs) or snapshot
        ),
    )
    payload = dashboard_app.get_market_watch_data(force=True)

    assert payload == {"contract": "market_watch.v1"}
    assert service_calls == [
        {"force": True, "stale_while_revalidate": False}
    ]


def test_dashboard_history_dependency_is_opened_read_only(tmp_path, monkeypatch):
    db_path = tmp_path / "missing" / "market-watch.sqlite3"
    previous = dashboard_app._MARKET_WATCH_HISTORY_STORE
    monkeypatch.setenv("TRADEX_MARKET_WATCH_DB", str(db_path))
    monkeypatch.setattr(dashboard_app, "_MARKET_WATCH_HISTORY_STORE", None)

    store = dashboard_app._get_market_watch_history_store()
    try:
        assert store.read_only is True
        assert store.list_dates() == []
        assert not db_path.exists()
    finally:
        store.close()
        monkeypatch.setattr(dashboard_app, "_MARKET_WATCH_HISTORY_STORE", previous)


def test_dashboard_collection_dependency_is_opened_read_only(tmp_path, monkeypatch):
    db_path = tmp_path / "missing" / "market-watch.sqlite3"
    previous = dashboard_app._MARKET_WATCH_COLLECTION_STORE
    monkeypatch.setenv("TRADEX_MARKET_WATCH_DB", str(db_path))
    monkeypatch.setattr(dashboard_app, "_MARKET_WATCH_COLLECTION_STORE", None)

    store = dashboard_app._get_market_watch_collection_store()
    try:
        assert store.read_only is True
        assert not db_path.exists()
    finally:
        store.close()
        monkeypatch.setattr(dashboard_app, "_MARKET_WATCH_COLLECTION_STORE", previous)


def test_post_market_review_uses_latest_accepted_raw_payload_only(monkeypatch):
    source_payload = {"contract": "market_watch.v1", "snapshot_id": "mw-accepted"}
    facade = SimpleNamespace(
        read=lambda: SimpleNamespace(
            accepted=SimpleNamespace(source_payload=source_payload)
        )
    )
    monkeypatch.setattr(dashboard_app, "_get_market_watch_read_facade", lambda: facade)
    monkeypatch.setattr(
        dashboard_app,
        "get_market_watch_data",
        lambda **_kwargs: pytest.fail("post-market review refreshed providers"),
    )

    assert dashboard_app._load_post_market_review_snapshot() is source_payload


def test_post_market_review_fails_closed_without_accepted_real(monkeypatch):
    facade = SimpleNamespace(read=lambda: SimpleNamespace(accepted=None))
    monkeypatch.setattr(dashboard_app, "_get_market_watch_read_facade", lambda: facade)

    with pytest.raises(RuntimeError, match="collector-accepted real"):
        dashboard_app._load_post_market_review_snapshot()


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
    dashboard_app.get_market_watch_data(force=False)

    assert service_calls == [
        {"force": False, "stale_while_revalidate": True}
    ]


def test_history_adapter_returns_dates_timeline_and_alerts(monkeypatch):
    def fail_full_timeline(*_args, **_kwargs):
        raise AssertionError("Web replay must not decode complete market-watch snapshots")

    store = SimpleNamespace(
        config_version="market-watch-policy.v1",
        list_dates=lambda: [
            {"trade_date": "2026-08-24", "sample_count": 2, "alert_count": 1}
        ],
        get_replay_timeline=lambda trade_date, limit=None: [
            {
                "trade_date": trade_date,
                "payload": {
                    "contract": "market_watch_replay_sample.v1",
                    "snapshot_id": "mw-1",
                },
            }
        ],
        get_timeline=fail_full_timeline,
        get_alerts=lambda trade_date, limit=None: [
            {"trade_date": trade_date, "alert": {"code": "market_caution"}}
        ],
    )
    monkeypatch.setattr(dashboard_app, "_get_market_watch_history_store", lambda: store)
    payload = dashboard_app.get_market_watch_history(limit=20)

    assert payload["contract"] == "market_watch_history.v1"
    assert payload["schema_version"] == 1
    assert payload["trade_date"] == "2026-08-24"
    assert payload["samples"][0]["payload"]["contract"] == (
        "market_watch_replay_sample.v1"
    )
    assert payload["samples"][0]["payload"]["snapshot_id"] == "mw-1"
    assert payload["alerts"][0]["alert"]["code"] == "market_caution"
    assert payload["recording"] == {"status": "collector_owned", "error": None}


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
        get_replay_timeline=lambda trade_date, limit=None: [
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
