from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


CLIENT_PATH = (
    Path(__file__).parents[2]
    / ".agents"
    / "skills"
    / "tradex-daily-stock-research"
    / "scripts"
    / "daily_selection_client.py"
)
SPEC = importlib.util.spec_from_file_location("daily_selection_client", CLIENT_PATH)
assert SPEC is not None and SPEC.loader is not None
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)


def _generation(state: str, *, result=None, error=None, failure_code=None):
    return {
        "contract": "daily_stock_selection_generation.v1",
        "schema_version": 1,
        "state": state,
        "result": result,
        "error": error,
        "failure_code": failure_code,
    }


def test_generate_client_polls_background_job_until_result(monkeypatch):
    expected = {
        "contract": "daily_stock_selection_result.v1",
        "schema_version": 1,
        "action": "inserted",
    }
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda _request, _timeout: _generation("running"),
    )
    monkeypatch.setattr(
        client,
        "read_generation",
        lambda _base_url, timeout: _generation("succeeded", result=expected),
    )
    monkeypatch.setattr(client.time, "sleep", lambda _seconds: None)

    result = client.generate_result(
        "http://127.0.0.1:8765",
        timeout=1.0,
        wait_timeout=2.0,
    )

    assert result == expected


def test_generate_client_reports_safe_background_failure(monkeypatch):
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda _request, _timeout: _generation(
            "failed",
            error="每日选股数据暂不可用。",
            failure_code="RuntimeError",
        ),
    )

    with pytest.raises(RuntimeError, match="RuntimeError.*每日选股数据暂不可用"):
        client.generate_result("http://127.0.0.1:8765", timeout=1.0)


@pytest.mark.parametrize(
    "value",
    ["https://127.0.0.1:8765", "http://example.com", "http://user@localhost:8765"],
)
def test_client_rejects_non_local_or_credentialed_origins(value):
    with pytest.raises(client.argparse.ArgumentTypeError):
        client._local_base_url(value)
