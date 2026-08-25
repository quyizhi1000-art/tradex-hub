from __future__ import annotations

import pytest
import requests

from astock_signals.smart_router import SourceBusyError
from tradex.data_sources import tushare_client


class _Response:
    def __init__(self, payload, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Session:
    def __init__(self, response: _Response | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture(autouse=True)
def _isolated_tushare_environment(monkeypatch, tmp_path):
    for name in (
        "TUSHARE_TOKEN",
        "TUSHARE_TOKEN_FILE",
        "TUSHARE_BASE_URL",
        "TUSHARE_TIMEOUT",
        "TUSHARE_MAX_INFLIGHT",
        "TUSHARE_POINTS_RATE_LIMIT_PER_MINUTE",
        "TUSHARE_REALTIME_DAILY_RATE_LIMIT_PER_MINUTE",
        "TUSHARE_REALTIME_MINUTE_RATE_LIMIT_PER_MINUTE",
        "TUSHARE_AUCTION_RATE_LIMIT_PER_MINUTE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "TRADEX_RATE_LIMIT_STATE_FILE",
        str(tmp_path / "provider-rate-limits.sqlite3"),
    )


def test_token_is_read_from_environment_not_config(monkeypatch):
    monkeypatch.setattr(tushare_client.config, "TUSHARE_TOKEN", "config-secret", raising=False)

    with pytest.raises(tushare_client.TushareConfigurationError):
        tushare_client.get_token()

    monkeypatch.setenv("TUSHARE_TOKEN", "environment-token")
    assert tushare_client.get_token() == "environment-token"


def test_token_file_supports_assignment_and_environment_takes_precedence(
    monkeypatch, tmp_path
):
    token_file = tmp_path / "tushare.token"
    token_file.write_text("TUSHARE_TOKEN='file-token'\n", encoding="utf-8")
    monkeypatch.setenv("TUSHARE_TOKEN_FILE", str(token_file))

    assert tushare_client.get_token() == "file-token"

    monkeypatch.setenv("TUSHARE_TOKEN", "environment-token")
    assert tushare_client.get_token() == "environment-token"


def test_request_posts_standard_envelope_to_https_root_without_redirects(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    session = _Session(
        _Response(
            {
                "request_id": "request-1",
                "code": 0,
                "msg": None,
                "data": {
                    "fields": ["ts_code", "trade_date"],
                    "items": [["000001.SZ", "20260821"]],
                },
            }
        )
    )
    monkeypatch.setattr(tushare_client, "_get_session", lambda: session)

    result = tushare_client.request(
        "daily",
        {"ts_code": "000001.SZ", "limit": 1},
        fields=("ts_code", "trade_date"),
        base_url="https://example.test/",
    )

    assert result.request_id == "request-1"
    assert result.records == (
        {"ts_code": "000001.SZ", "trade_date": "20260821"},
    )
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == "https://example.test/"
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == 15
    assert kwargs["json"] == {
        "api_name": "daily",
        "token": "test-token",
        "params": {"ts_code": "000001.SZ", "limit": 1},
        "fields": "ts_code,trade_date",
    }


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.test/",
        "https://user:password@example.test/",
        "https://example.test/?token=secret",
        "https://example.test/#fragment",
        "https://example.test/api",
    ],
)
def test_request_rejects_non_https_credentialed_or_non_root_urls(
    monkeypatch, base_url
):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")

    with pytest.raises(tushare_client.TushareConfigurationError):
        tushare_client.request("daily", base_url=base_url)


@pytest.mark.parametrize(
    "response",
    [
        requests.RequestException("transport leaked test-token"),
        _Response({"code": 403, "msg": "bad token test-token", "data": None}),
        _Response({"code": 0, "msg": None, "data": None}),
        _Response(ValueError("invalid JSON test-token")),
        _Response({"code": 0, "data": {"fields": ["a"], "items": [[1, 2]]}}),
    ],
)
def test_failures_do_not_disclose_token(monkeypatch, response):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.setattr(tushare_client, "_get_session", lambda: _Session(response))

    with pytest.raises(Exception) as caught:
        tushare_client.request("daily", base_url="https://example.test/")

    assert "test-token" not in str(caught.value)


def test_provider_request_id_cannot_echo_token_into_result(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    session = _Session(
        _Response(
            {
                "request_id": "prefix-test-token-suffix",
                "code": 0,
                "msg": None,
                "data": {"fields": ["ts_code"], "items": [["000001.SZ"]]},
            }
        )
    )
    monkeypatch.setattr(tushare_client, "_get_session", lambda: session)

    result = tushare_client.request("daily", base_url="https://example.test/")

    assert result.request_id is None


def test_busy_concurrency_gate_raises_source_busy_without_http(monkeypatch):
    class _BusyGate:
        def acquire(self, *, blocking):
            assert blocking is False
            return False

    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.setattr(tushare_client, "_REQUEST_GATE", _BusyGate())
    monkeypatch.setattr(
        tushare_client,
        "_get_session",
        lambda: pytest.fail("HTTP must not run while the gate is busy"),
    )

    with pytest.raises(SourceBusyError, match="capacity is busy"):
        tushare_client.request("daily", base_url="https://example.test/")


def test_full_rate_budget_raises_source_busy(monkeypatch):
    monkeypatch.setenv("TUSHARE_POINTS_RATE_LIMIT_PER_MINUTE", "1")

    tushare_client._consume_rate_budget("daily")
    with pytest.raises(SourceBusyError, match="rate budget is full"):
        tushare_client._consume_rate_budget("moneyflow")

    # Separately purchased products never consume the 15000-point bucket.
    tushare_client._consume_rate_budget("rt_k")
    tushare_client._consume_rate_budget("rt_etf_k")


@pytest.mark.parametrize(
    ("api_name", "expected_bucket", "expected_limit"),
    [
        ("daily", "paid:tushare:points", 500),
        ("rt_k", "paid:tushare:realtime_daily:a_share", 50),
        ("rt_etf_k", "paid:tushare:realtime_daily:etf", 50),
        ("rt_sw_k", "paid:tushare:realtime_daily:shenwan", 50),
        ("rt_min_daily", "paid:tushare:realtime_minute:a_share", 500),
        ("stk_auction", "paid:tushare:auction:a_share", 500),
    ],
)
def test_api_permissions_use_distinct_shared_buckets(
    api_name, expected_bucket, expected_limit
):
    assert tushare_client._rate_policy(api_name) == (
        expected_bucket,
        expected_limit,
    )
