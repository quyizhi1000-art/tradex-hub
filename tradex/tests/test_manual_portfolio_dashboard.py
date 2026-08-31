from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradex.dashboard import __main__ as dashboard_app
from tradex.dashboard.__main__ import DashboardWriteRejected


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


def test_manual_portfolio_desktop_ui_exposes_only_observation_scope():
    html = (WATCH_DIR / "index.html").read_text(encoding="utf-8")
    js = (WATCH_DIR / "app.js").read_text(encoding="utf-8")

    assert 'id="manual-portfolio-section"' in html
    assert "手动维护，并非券商账户事实" in html
    assert "最多启用 40 个代码" in html
    assert "页面关闭不保证送达" in html
    assert "manual_portfolio_outlook.v1" in js
    assert "manual_portfolio_market_snapshot.v1" in js
    for forbidden in ("成本线", "真实盈亏", "自动下单", "交割单", "成交历史"):
        assert forbidden not in html
