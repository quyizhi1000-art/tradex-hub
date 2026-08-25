from __future__ import annotations

import threading
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests
from astock_signals.smart_router import SourceBusyError

from tradex.config import Config, config
from tradex.data_sources import fuyao_client, fuyao_fetchers


_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_SNAPSHOT_PATH = "/api/a-share/prices/snapshot"
_HISTORICAL_PATH = "/api/a-share/prices/historical"


@pytest.fixture(autouse=True)
def _isolated_rate_budget(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "TRADEX_RATE_LIMIT_STATE_FILE",
        str(tmp_path / "provider-rate-limits.sqlite3"),
    )


class _FakeResponse:
    def __init__(
        self,
        payload: object | None = None,
        *,
        json_error: Exception | None = None,
        http_error: Exception | None = None,
        status_code: int = 200,
    ) -> None:
        self._payload = payload
        self._json_error = json_error
        self._http_error = http_error
        self.status_code = status_code
        self.text = ""

    def raise_for_status(self) -> None:
        if self._http_error is not None:
            raise self._http_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


def _set_fuyao_config(
    monkeypatch,
    *,
    api_key: str = "unit-test-key",
    api_key_file: str = "",
    base_url: str = "https://fuyao.invalid",
    timeout: int = 15,
) -> None:
    """Patch both Config defaults and the process-wide config singleton."""
    values = {
        "FUYAO_API_KEY": api_key,
        "FUYAO_API_KEY_FILE": api_key_file,
        "FUYAO_BASE_URL": base_url,
        "FUYAO_TIMEOUT": timeout,
    }
    for target in (Config, config):
        for name, value in values.items():
            monkeypatch.setattr(target, name, value, raising=False)
    # The client intentionally gives process environment variables precedence.
    # Always override them in tests so a developer's real local credentials can
    # never be read or displayed by an assertion failure.
    for name, value in values.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)


def _patch_session(monkeypatch, fake_get) -> None:
    monkeypatch.setattr(
        fuyao_client,
        "_get_session",
        lambda: SimpleNamespace(get=fake_get),
    )


def _snapshot_item(ticker: str = "600519", suffix: str = "SH") -> dict:
    return {
        "thscode": f"{ticker}.{suffix}",
        "ticker": ticker,
        "volume": 3_098_875,
        "turnover": 3_937_375_200,
        "last_price": 1277.8,
        "price_change": 21.8,
        "price_change_ratio_pct": 1.735669,
        "open_price": 1252.08,
        "high_price": 1282.0,
        "low_price": 1250.21,
        "prev_price": 1256.0,
    }


def _millis(date_text: str) -> int:
    parsed = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=_SHANGHAI_TZ)
    return int(parsed.timestamp() * 1000)


def _bar(
    date_text: str,
    *,
    open_price: float,
    high_price: float,
    low_price: float,
    close_price: float,
    volume: float,
    turnover: float,
) -> dict:
    return {
        "date_ms": _millis(date_text),
        "open_price": open_price,
        "high_price": high_price,
        "low_price": low_price,
        "close_price": close_price,
        "volume": volume,
        "turnover": turnover,
    }


def _success(data: dict) -> dict:
    return {
        "code": 0,
        "message": "success",
        "request_id": "unit-request-id",
        "data": data,
    }


def test_request_uses_api_key_header_and_returns_validated_data(monkeypatch, caplog):
    secret = "header-only-unit-secret"
    _set_fuyao_config(monkeypatch, api_key=secret)
    calls = []
    data = {"timestamp": 1784275991000, "total": 0, "item": []}

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResponse(_success(data))

    _patch_session(monkeypatch, fake_get)

    result = fuyao_client._request(_SNAPSHOT_PATH, {"limit": 100, "offset": 0})

    assert result == data
    assert calls == [
        (
            "https://fuyao.invalid/api/a-share/prices/snapshot",
            {
                "params": {"limit": 100, "offset": 0},
                "headers": {
                    "Accept": "application/json",
                    "X-api-key": secret,
                },
                "timeout": 15,
            },
        )
    ]
    assert secret not in repr(result)
    assert secret not in caplog.text


def test_request_capacity_is_nonblocking_and_falls_back(monkeypatch):
    _set_fuyao_config(monkeypatch)
    gate = threading.BoundedSemaphore(1)
    assert gate.acquire(blocking=False)
    monkeypatch.setattr(fuyao_client, "_REQUEST_GATE", gate)
    calls = []
    _patch_session(monkeypatch, lambda *args, **kwargs: calls.append((args, kwargs)))

    try:
        with pytest.raises(SourceBusyError, match="capacity is busy"):
            fuyao_client._request(_SNAPSHOT_PATH)
    finally:
        gate.release()

    assert calls == []


def test_request_shared_rate_budget_is_nonblocking(monkeypatch):
    _set_fuyao_config(monkeypatch)
    monkeypatch.setenv("FUYAO_RATE_LIMIT_PER_MINUTE", "1")
    data = {"timestamp": 1784275991000, "total": 0, "item": []}
    calls = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        return _FakeResponse(_success(data))

    _patch_session(monkeypatch, fake_get)

    assert fuyao_client._request(_SNAPSHOT_PATH) == data
    with pytest.raises(SourceBusyError, match="rate budget is full"):
        fuyao_client._request(_SNAPSHOT_PATH)
    assert len(calls) == 1


def test_request_reads_key_file_lazily_and_strips_whitespace(monkeypatch, tmp_path):
    key_path = tmp_path / "fuyao-key.txt"
    key_path.write_text("  file-unit-secret\n", encoding="utf-8")
    _set_fuyao_config(monkeypatch, api_key="", api_key_file=str(key_path))
    calls = []

    def fake_get(url, **kwargs):
        calls.append(kwargs)
        return _FakeResponse(_success({"timestamp": None, "item": []}))

    _patch_session(monkeypatch, fake_get)

    fuyao_client._request(_HISTORICAL_PATH)

    assert calls[0]["headers"]["X-api-key"] == "file-unit-secret"


def test_request_http_failure_does_not_leak_api_key(monkeypatch, caplog):
    secret = "never-include-this-secret"
    _set_fuyao_config(monkeypatch, api_key=secret)
    response = _FakeResponse(
        http_error=requests.HTTPError("401 Client Error"), status_code=401
    )
    _patch_session(monkeypatch, lambda *args, **kwargs: response)

    with pytest.raises(RuntimeError) as exc_info:
        fuyao_client._request(_SNAPSHOT_PATH)

    assert secret not in str(exc_info.value)
    assert secret not in caplog.text


def test_request_rejects_nonzero_business_code(monkeypatch):
    _set_fuyao_config(monkeypatch)
    _patch_session(
        monkeypatch,
        lambda *args, **kwargs: _FakeResponse(
            {
                "code": 2001,
                "message": "API Key 缺失或无效",
                "request_id": "failed-request-id",
                "data": None,
            }
        ),
    )

    with pytest.raises(fuyao_client.FuyaoAPIError) as exc_info:
        fuyao_client._request(_SNAPSHOT_PATH)

    message = str(exc_info.value)
    assert "2001" in message
    assert "API Key 缺失或无效" in message
    assert "failed-request-id" in message


@pytest.mark.parametrize(
    "response",
    [
        _FakeResponse(json_error=ValueError("invalid json")),
        _FakeResponse([]),
        _FakeResponse(
            {"code": False, "message": "invalid code type", "data": {}}
        ),
        _FakeResponse({"code": 0, "message": "success", "request_id": "r"}),
        _FakeResponse(
            {"code": 0, "message": "success", "request_id": "r", "data": None}
        ),
    ],
    ids=["invalid-json", "non-object", "boolean-code", "missing-data", "null-data"],
)
def test_request_rejects_invalid_json_or_envelope(monkeypatch, response):
    _set_fuyao_config(monkeypatch)
    _patch_session(monkeypatch, lambda *args, **kwargs: response)

    with pytest.raises(RuntimeError):
        fuyao_client._request(_SNAPSHOT_PATH)


def test_official_api_key_name_takes_precedence_without_leaking(monkeypatch):
    _set_fuyao_config(monkeypatch, api_key="legacy-key")
    monkeypatch.setenv("HITHINK_FINANCE_API_KEY", " official-key ")

    assert fuyao_client.get_api_key() == "official-key"


def test_http_sessions_are_isolated_per_worker_thread(monkeypatch):
    created: list[object] = []
    creation_lock = threading.Lock()

    class FakeSession:
        def __init__(self):
            self.trust_env = True
            with creation_lock:
                created.append(self)

    monkeypatch.setattr(fuyao_client, "_SESSION_LOCAL", threading.local())
    monkeypatch.setattr(fuyao_client.requests, "Session", FakeSession)
    seen: list[tuple[object, object]] = []

    def worker() -> None:
        seen.append((fuyao_client._get_session(), fuyao_client._get_session()))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(created) == 2
    assert all(first is second for first, second in seen)
    assert seen[0][0] is not seen[1][0]
    assert all(session.trust_env is False for session in created)


@pytest.mark.parametrize(
    ("kwargs", "expected_thscode"),
    [
        ({"symbol": "600519"}, "600519.SH"),
        ({"code": "000001"}, "000001.SZ"),
    ],
)
def test_realtime_quote_accepts_symbol_or_code_and_maps_fields(
    monkeypatch, kwargs, expected_thscode
):
    calls = []
    ticker, suffix = expected_thscode.split(".")

    def fake_request(path, params=None):
        calls.append((path, params))
        return {
            "timestamp": 1784275991000,
            "total": 1,
            "item": [_snapshot_item(ticker, suffix)],
        }

    monkeypatch.setattr(fuyao_fetchers, "_request", fake_request)

    result = fuyao_fetchers.fetch_realtime_quote(**kwargs)

    assert calls == [(_SNAPSHOT_PATH, {"thscodes": expected_thscode})]
    assert isinstance(result, pd.DataFrame)
    assert len(result) == 1
    record = result.iloc[0].to_dict()
    assert {
        "代码": record["代码"],
        "最新价": record["最新价"],
        "涨跌额": record["涨跌额"],
        "涨跌幅": record["涨跌幅"],
        "今开": record["今开"],
        "最高": record["最高"],
        "最低": record["最低"],
        "昨收": record["昨收"],
        "成交量": record["成交量"],
        "成交额": record["成交额"],
    } == {
        "代码": ticker,
        "最新价": 1277.8,
        "涨跌额": 21.8,
        "涨跌幅": 1.735669,
        "今开": 1252.08,
        "最高": 1282.0,
        "最低": 1250.21,
        "昨收": 1256.0,
        "成交量": 3_098_875,
        "成交额": 3_937_375_200,
    }


def test_realtime_quote_without_symbol_paginates_full_market(monkeypatch):
    calls = []
    monkeypatch.setattr(fuyao_fetchers, "_SNAPSHOT_PAGE_SIZE", 2)

    def fake_request(path, params=None):
        calls.append((path, dict(params)))
        limit = params["limit"]
        offset = params["offset"]
        if offset == 0:
            items = [
                _snapshot_item(f"{index:06d}", "SZ") for index in range(limit)
            ]
            return {"timestamp": 1, "total": limit + 1, "item": items}
        return {
            "timestamp": 2,
            "total": limit + 1,
            "item": [_snapshot_item(f"{limit:06d}", "SZ")],
        }

    monkeypatch.setattr(fuyao_fetchers, "_request", fake_request)

    result = fuyao_fetchers.fetch_realtime_quote()

    assert len(calls) == 2
    assert calls[0] == (_SNAPSHOT_PATH, {"limit": 2, "offset": 0})
    assert calls[1] == (_SNAPSHOT_PATH, {"limit": 2, "offset": 2})
    assert len(result) == 3
    assert result["代码"].iloc[0] == "000000"
    assert result["代码"].iloc[-1] == "000002"


def test_realtime_quote_short_page_ends_even_when_total_is_an_estimate(monkeypatch):
    calls = []
    monkeypatch.setattr(fuyao_fetchers, "_SNAPSHOT_PAGE_SIZE", 3)

    def fake_request(path, params=None):
        calls.append((path, dict(params)))
        return {
            "timestamp": 1,
            "total": 100,
            "item": [_snapshot_item("600519", "SH"), _snapshot_item("000001", "SZ")],
        }

    monkeypatch.setattr(fuyao_fetchers, "_request", fake_request)

    result = fuyao_fetchers.fetch_realtime_quote()

    assert calls == [(_SNAPSHOT_PATH, {"limit": 3, "offset": 0})]
    assert result["代码"].tolist() == ["600519", "000001"]
    assert result.attrs["total"] == 2
    assert result.attrs["reported_total"] == 100


@pytest.mark.parametrize(
    ("adjust", "expected_adjust"),
    [("qfq", "forward"), ("hfq", "backward"), ("", "none")],
)
def test_historical_kline_maps_request_and_daily_fields(
    monkeypatch, adjust, expected_adjust
):
    calls = []
    items = [
        _bar(
            "2024-05-20",
            open_price=1611.602,
            high_price=1626.602,
            low_price=1601.722,
            close_price=1602.612,
            volume=3_142_572.0,
            turnover=5_401_389_334.87,
        )
    ]

    def fake_request(path, params=None):
        calls.append((path, dict(params)))
        return {"timestamp": items[-1]["date_ms"], "item": items}

    monkeypatch.setattr(fuyao_fetchers, "_request", fake_request)

    result = fuyao_fetchers.fetch_historical_kline(
        code="600519",
        period="daily",
        start_date="20240520",
        end_date="20240521",
        adjust=adjust,
    )

    assert len(calls) == 1
    path, params = calls[0]
    assert path == _HISTORICAL_PATH
    assert params["thscode"] == "600519.SH"
    assert params["interval"] == "1d"
    assert params["start"] == _millis("2024-05-20")
    assert params["end"] == _millis("2024-05-21")
    assert params["adjust"] == expected_adjust
    assert isinstance(result, pd.DataFrame)
    assert len(result) == 1
    record = result.iloc[0].to_dict()
    assert str(record["日期"])[:10] == "2024-05-20"
    assert record["开盘"] == 1611.602
    assert record["最高"] == 1626.602
    assert record["最低"] == 1601.722
    assert record["收盘"] == 1602.612
    assert record["成交量"] == 3_142_572.0
    assert record["成交额"] == 5_401_389_334.87


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        (
            "weekly",
            [
                {
                    "开盘": 10.0,
                    "最高": 15.0,
                    "最低": 8.0,
                    "收盘": 14.0,
                    "成交量": 1000.0,
                    "成交额": 10_000.0,
                },
                {
                    "开盘": 20.0,
                    "最高": 22.0,
                    "最低": 19.0,
                    "收盘": 21.0,
                    "成交量": 500.0,
                    "成交额": 5_000.0,
                },
            ],
        ),
        (
            "monthly",
            [
                {
                    "开盘": 10.0,
                    "最高": 13.0,
                    "最低": 9.0,
                    "收盘": 12.0,
                    "成交量": 300.0,
                    "成交额": 3_000.0,
                },
                {
                    "开盘": 12.0,
                    "最高": 22.0,
                    "最低": 8.0,
                    "收盘": 21.0,
                    "成交量": 1200.0,
                    "成交额": 12_000.0,
                },
            ],
        ),
    ],
)
def test_historical_kline_aggregates_daily_bars_locally(
    monkeypatch, period, expected
):
    items = [
        _bar(
            "2024-01-29",
            open_price=10,
            high_price=12,
            low_price=9,
            close_price=11,
            volume=100,
            turnover=1000,
        ),
        _bar(
            "2024-01-30",
            open_price=11,
            high_price=13,
            low_price=10,
            close_price=12,
            volume=200,
            turnover=2000,
        ),
        _bar(
            "2024-02-01",
            open_price=12,
            high_price=14,
            low_price=8,
            close_price=10,
            volume=300,
            turnover=3000,
        ),
        _bar(
            "2024-02-02",
            open_price=10,
            high_price=15,
            low_price=9,
            close_price=14,
            volume=400,
            turnover=4000,
        ),
        _bar(
            "2024-02-05",
            open_price=20,
            high_price=22,
            low_price=19,
            close_price=21,
            volume=500,
            turnover=5000,
        ),
    ]
    calls = []

    def fake_request(path, params=None):
        calls.append((path, dict(params)))
        return {"timestamp": items[-1]["date_ms"], "item": items}

    monkeypatch.setattr(fuyao_fetchers, "_request", fake_request)

    result = fuyao_fetchers.fetch_historical_kline(
        symbol="600519",
        period=period,
        start_date="20240129",
        end_date="20240205",
    )

    assert len(calls) == 1
    assert calls[0][1]["interval"] == "1d"
    actual = result.drop(columns=["日期"]).to_dict("records")
    assert actual == expected
