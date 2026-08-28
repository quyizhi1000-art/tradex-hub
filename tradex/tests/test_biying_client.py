from __future__ import annotations

import requests
import pytest

from astock_signals.smart_router import SourceBusyError
from tradex.data_sources import biying_client


class _Response:
    def __init__(self, status_code: int = 200, payload=None) -> None:
        self.status_code = status_code
        self._payload = [] if payload is None else payload
        self.url = "https://api.example.invalid/path/test-secret"

    def json(self):
        return self._payload


class _Session:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response or _Response()
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture(autouse=True)
def _isolated_rate_budget(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "TRADEX_RATE_LIMIT_STATE_FILE",
        str(tmp_path / "provider-rate-limits.sqlite3"),
    )


def test_request_appends_licence_internally_and_returns_direct_payload(monkeypatch):
    session = _Session(_Response(payload=[{"dm": "000001"}]))
    monkeypatch.setattr(biying_client, "get_licence", lambda: "test-secret")
    monkeypatch.setattr(biying_client, "_get_session", lambda: session)

    result = biying_client.request(["hslt", "list"])

    assert result == [{"dm": "000001"}]
    assert session.calls[0][0].endswith("/hslt/list/test-secret")
    assert "test-secret" not in repr(result)


def test_transport_and_http_errors_never_expose_path_licence(monkeypatch):
    monkeypatch.setattr(biying_client, "get_licence", lambda: "test-secret")
    leaking = requests.RequestException(
        "failed https://api.example.invalid/hslt/list/test-secret"
    )
    monkeypatch.setattr(
        biying_client, "_get_session", lambda: _Session(error=leaking)
    )

    with pytest.raises(RuntimeError) as transport:
        biying_client.request(["hslt", "list"])
    assert "test-secret" not in str(transport.value)
    assert "api.example" not in str(transport.value)

    monkeypatch.setattr(
        biying_client,
        "_get_session",
        lambda: _Session(_Response(status_code=403, payload={})),
    )
    with pytest.raises(biying_client.BiyingAPIError) as status:
        biying_client.request(["hslt", "list"])
    assert "test-secret" not in str(status.value)
    assert "api.example" not in str(status.value)


def test_provider_business_codes_distinguish_daily_quota_and_invalid_licence(
    monkeypatch,
):
    monkeypatch.setattr(biying_client, "get_licence", lambda: "test-secret")
    monkeypatch.setattr(
        biying_client,
        "_get_session",
        lambda: _Session(
            _Response(
                status_code=429,
                payload={"code": 101, "message": "test-secret must not leak"},
            )
        ),
    )

    with pytest.raises(biying_client.BiyingDailyQuotaExceeded) as daily:
        biying_client.request(["hslt", "list"])
    assert "test-secret" not in str(daily.value)

    monkeypatch.setattr(
        biying_client,
        "_get_session",
        lambda: _Session(
            _Response(payload={"code": 102, "message": "test-secret invalid"})
        ),
    )
    with pytest.raises(biying_client.BiyingLicenceInvalid) as invalid:
        biying_client.request(["hslt", "list"])
    assert "test-secret" not in str(invalid.value)


def test_http_429_without_safe_business_code_is_classified_as_upstream_throttle(
    monkeypatch,
):
    monkeypatch.setattr(biying_client, "get_licence", lambda: "test-secret")
    monkeypatch.setattr(
        biying_client,
        "_get_session",
        lambda: _Session(_Response(status_code=429, payload={})),
    )

    with pytest.raises(biying_client.BiyingUpstreamThrottled) as throttled:
        biying_client.request(["hslt", "list"])
    assert "test-secret" not in str(throttled.value)


def test_client_rejects_insecure_base_url_before_reading_licence(monkeypatch):
    monkeypatch.setattr(
        biying_client,
        "get_licence",
        lambda: pytest.fail("licence must not be read for an insecure base URL"),
    )

    with pytest.raises(biying_client.BiyingConfigurationError, match="HTTPS"):
        biying_client.request(["hslt", "list"], base_url="http://api.example.invalid")


def test_rate_budget_is_non_blocking_and_uses_router_fallback(monkeypatch):
    monkeypatch.setenv("BIYING_RATE_LIMIT_PER_MINUTE", "1")
    monkeypatch.setattr(biying_client, "get_licence", lambda: "test-secret")
    monkeypatch.setattr(
        biying_client, "_get_session", lambda: _Session(_Response(payload=[]))
    )

    assert biying_client.request(["hslt", "list"]) == []
    with pytest.raises(SourceBusyError):
        biying_client.request(["hslt", "list"])


def test_capability_switch_is_independent(monkeypatch):
    monkeypatch.setattr(biying_client, "is_configured", lambda: True)
    monkeypatch.setenv(
        "BIYING_PRIMARY_CAPABILITIES", "realtime_quote,financial_stmt"
    )

    assert biying_client.provides("realtime_quote")
    assert biying_client.provides("financial_stmt")
    assert not biying_client.provides("market_overview")
