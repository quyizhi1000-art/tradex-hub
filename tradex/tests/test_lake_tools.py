"""Focused, network-free tests for the read-only lake MCP tools."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tradex.data_lake.replay import ReplayResult
from tradex.tools import lake_data


class FakeMCP:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self):
        def decorate(function):
            self.tools[function.__name__] = function
            return function

        return decorate


@pytest.fixture
def tools():
    mcp = FakeMCP()
    lake_data.register(mcp)
    assert set(mcp.tools) == {
        "get_data_lake_status",
        "get_lake_daily_bars",
        "replay_lake_capture",
    }
    return mcp.tools


def test_status_returns_latest_completed_and_cnequity_generation(
    tools, tmp_path: Path, monkeypatch
):
    lake_root = tmp_path / "tradex-lake"
    catalog_path = lake_root / "meta" / "catalog.sqlite3"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.touch()
    cn_root = tmp_path / "cnequity"
    cn_root.mkdir()
    monkeypatch.setenv("TRADEX_DATA_LAKE_ROOT", str(lake_root))
    monkeypatch.setenv("CNEQUITY_DATA_ROOT", str(cn_root))

    class FakeCatalog:
        closed = False

        def latest_completed(self):
            return {"capture_id": "capture-1", "status": "completed"}

        def close(self):
            self.closed = True

    catalog = FakeCatalog()
    monkeypatch.setattr(lake_data, "_catalog_for_root", lambda root: catalog)

    class FakeBridge:
        def describe_generation(self, dataset):
            assert dataset == "daily_bars"
            return {
                "dataset": dataset,
                "generation_sha256": "a" * 64,
                "coverage": {"min": "2026-05-22", "max": "2026-08-19"},
                "file_count": 90,
                "total_size": 12345,
                "files": [{"relative_path": "must-not-leak-in-status"}],
            }

    monkeypatch.setattr(lake_data, "_bridge_for_root", lambda root: FakeBridge())

    payload = json.loads(asyncio.run(tools["get_data_lake_status"]()))

    assert payload["read_only"] is True
    assert payload["tradex"]["latest_completed"] == {
        "capture_id": "capture-1",
        "status": "completed",
    }
    assert payload["cnequity"]["generation"]["generation_sha256"] == "a" * 64
    assert "files" not in payload["cnequity"]["generation"]
    assert catalog.closed is True


def test_status_isolates_cnequity_failure(tools, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TRADEX_DATA_LAKE_ROOT", str(tmp_path / "empty-lake"))
    cn_root = tmp_path / "bad-cnequity"
    cn_root.mkdir()
    monkeypatch.setenv("CNEQUITY_DATA_ROOT", str(cn_root))

    def failed_bridge(root):
        raise RuntimeError("CNEquity optional runtime missing")

    monkeypatch.setattr(lake_data, "_bridge_for_root", failed_bridge)
    payload = json.loads(asyncio.run(tools["get_data_lake_status"]()))

    assert payload["tradex"]["latest_completed"] is None
    assert payload["tradex"]["catalog_exists"] is False
    assert "CNEquity optional runtime missing" in payload["cnequity"]["error"]
    assert "error" not in payload or payload.get("error") is not True


def test_daily_bars_reports_total_returned_and_truncation(
    tools, tmp_path: Path, monkeypatch
):
    cn_root = tmp_path / "cnequity"
    cn_root.mkdir()
    monkeypatch.setenv("CNEQUITY_DATA_ROOT", str(cn_root))
    observed = {}
    rows = [{"symbol": "600519.SH", "trade_date": f"day-{index}"} for index in range(7)]

    class FakeBridge:
        def load_daily_bars(self, symbols, start, end, adjust=None, strict_adj=True):
            observed.update({
                "symbols": symbols,
                "start": start,
                "end": end,
                "adjust": adjust,
                "strict_adj": strict_adj,
            })
            return rows

    monkeypatch.setattr(lake_data, "_bridge_for_root", lambda root: FakeBridge())
    payload = json.loads(asyncio.run(tools["get_lake_daily_bars"](
        "600519, SH600519, 000001.SZ",
        "2026-05-22",
        "2026-08-19",
        adjust="HFQ",
        strict_adj=True,
        limit=3,
    )))

    assert observed == {
        "symbols": ["600519.SH", "000001.SZ"],
        "start": "2026-05-22",
        "end": "2026-08-19",
        "adjust": "hfq",
        "strict_adj": True,
    }
    assert payload["total"] == 7
    assert payload["returned"] == 3
    assert payload["truncated"] is True
    assert payload["rows"] == rows[:3]


@pytest.mark.parametrize("limit", [0, 5001, True])
def test_daily_bars_rejects_invalid_limit_before_bridge(
    tools, limit, monkeypatch
):
    monkeypatch.setenv("CNEQUITY_DATA_ROOT", "G:/not-called")

    def should_not_run(root):
        raise AssertionError("bridge must not be created")

    monkeypatch.setattr(lake_data, "_bridge_for_root", should_not_run)
    payload = json.loads(asyncio.run(tools["get_lake_daily_bars"](
        "600519.SH", "2026-05-22", "2026-08-19", limit=limit
    )))

    assert payload["error"] is True
    assert payload["tool"] == "get_lake_daily_bars"
    assert "1 到 5000" in payload["message"]


def test_daily_bars_without_cnequity_root_returns_actionable_error(
    tools, monkeypatch
):
    monkeypatch.delenv("CNEQUITY_DATA_ROOT", raising=False)
    payload = json.loads(asyncio.run(tools["get_lake_daily_bars"](
        "600519.SH", "2026-05-22", "2026-08-19"
    )))

    assert payload["error"] is True
    assert "CNEQUITY_DATA_ROOT" in payload["message"]
    assert "[data].root" in payload["message"]


@pytest.mark.parametrize(
    ("symbols", "start", "end", "expected"),
    [
        (
            ",".join(f"{index:06d}.SZ" for index in range(1, 52)),
            "2026-05-22",
            "2026-08-19",
            "最多允许 50",
        ),
        ("600519.SH", "2025-01-01", "2026-08-19", "最多 366"),
    ],
)
def test_daily_bars_bounds_work_before_bridge(
    tools, symbols, start, end, expected, monkeypatch
):
    monkeypatch.setenv("CNEQUITY_DATA_ROOT", "G:/not-called")

    def should_not_run(root):
        raise AssertionError("bridge must not be created")

    monkeypatch.setattr(lake_data, "_bridge_for_root", should_not_run)
    payload = json.loads(asyncio.run(
        tools["get_lake_daily_bars"](symbols, start, end)
    ))

    assert payload["error"] is True
    assert expected in payload["message"]


def test_replay_tool_uses_explicit_offline_engine_and_serializes_result(
    tools, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("TRADEX_DATA_LAKE_ROOT", str(tmp_path))
    observed = {}

    class FakeCatalog:
        def close(self):
            observed["closed"] = True

    class FakeReplay:
        catalog = FakeCatalog()

        def rebuild(self, capture_id):
            observed["capture_id"] = capture_id
            return ReplayResult(
                capture_id=capture_id,
                stored_hash="a" * 64,
                recomputed_hash="a" * 64,
                match=True,
                versions={"replay_schema_version": "lake-base-replay-v1"},
            )

    monkeypatch.setattr(lake_data, "_replay_for_root", lambda root: FakeReplay())
    payload = json.loads(asyncio.run(
        tools["replay_lake_capture"](" capture-1 ")
    ))

    assert observed == {"capture_id": "capture-1", "closed": True}
    assert payload["match"] is True
    assert payload["stored_hash"] == payload["recomputed_hash"]


def test_replay_failure_isolated_as_standard_error(tools, monkeypatch):
    class FailedReplay:
        def rebuild(self, capture_id):
            raise RuntimeError("object is corrupt")

    monkeypatch.setattr(lake_data, "_replay_for_root", lambda root: FailedReplay())
    payload = json.loads(asyncio.run(
        tools["replay_lake_capture"]("capture-bad")
    ))

    assert payload == {
        "error": True,
        "message": "数据湖回放失败: object is corrupt",
        "tool": "replay_lake_capture",
    }
