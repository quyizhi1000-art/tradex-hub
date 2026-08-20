"""Focused tests for Tradex MCP transport selection."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tradex import __main__ as cli


@pytest.mark.parametrize(
    ("arguments", "expected_transport"),
    [
        ([], "stdio"),
        (["--http"], "sse"),
        (["--streamable-http"], "streamable-http"),
    ],
)
def test_main_selects_requested_transport(
    monkeypatch, arguments, expected_transport
):
    mcp = MagicMock()
    mcp.settings = SimpleNamespace(host=None, port=None)
    config = SimpleNamespace(WS_SERVER_ENABLED=False)

    monkeypatch.setitem(sys.modules, "tradex.server", SimpleNamespace(mcp=mcp))
    monkeypatch.setitem(sys.modules, "tradex.config", SimpleNamespace(config=config))
    monkeypatch.setattr(sys, "argv", ["tradex", *arguments])

    cli.main()

    mcp.run.assert_called_once_with(transport=expected_transport)


def test_streamable_http_applies_custom_bind_address(monkeypatch):
    mcp = MagicMock()
    mcp.settings = SimpleNamespace(host=None, port=None)
    config = SimpleNamespace(WS_SERVER_ENABLED=False)

    monkeypatch.setitem(sys.modules, "tradex.server", SimpleNamespace(mcp=mcp))
    monkeypatch.setitem(sys.modules, "tradex.config", SimpleNamespace(config=config))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tradex",
            "--streamable-http",
            "--host",
            "127.0.0.2",
            "--port",
            "9000",
        ],
    )

    cli.main()

    assert mcp.settings.host == "127.0.0.2"
    assert mcp.settings.port == 9000
    mcp.run.assert_called_once_with(transport="streamable-http")


def test_http_flags_are_mutually_exclusive(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["tradex", "--http", "--streamable-http"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
