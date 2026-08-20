"""Focused tests for the read-only CNEquity lake bridge."""

from __future__ import annotations

import builtins
import os
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

from tradex.data_lake.cnequity_bridge import CNEquityBridge
from tradex.data_lake.serde import sha256_hex


class _Frame:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def to_dicts(self) -> list[dict]:
        return self._rows


def _install_fake_reader(monkeypatch, load):
    package = ModuleType("cnequity")
    package.__path__ = []
    query = ModuleType("cnequity.query")
    query.__path__ = []
    reader = ModuleType("cnequity.query.reader")
    reader.load = load
    monkeypatch.setitem(sys.modules, "cnequity", package)
    monkeypatch.setitem(sys.modules, "cnequity.query", query)
    monkeypatch.setitem(sys.modules, "cnequity.query.reader", reader)


def test_load_daily_bars_lazily_calls_public_reader(tmp_path: Path, monkeypatch):
    observed = {}
    rows = [{"symbol": "600519.SH", "trade_date": date(2026, 8, 19), "close": 1400.0}]

    def fake_load(dataset, **kwargs):
        observed["dataset"] = dataset
        observed["kwargs"] = kwargs
        return _Frame(rows)

    _install_fake_reader(monkeypatch, fake_load)
    bridge = CNEquityBridge(tmp_path)

    result = bridge.load_daily_bars(
        ["600519.SH"],
        "2026-05-22",
        "2026-08-19",
        adjust="hfq",
    )

    assert result == rows
    assert observed == {
        "dataset": "daily_bars",
        "kwargs": {
            "symbols": ["600519.SH"],
            "start": "2026-05-22",
            "end": "2026-08-19",
            "adjust": "hfq",
            "strict_adj": True,
            "data_root": tmp_path.resolve(),
        },
    }


def test_load_trading_sessions_uses_public_reader_and_filters_closed_days(
    tmp_path: Path,
    monkeypatch,
):
    observed = {}

    def fake_load(dataset, **kwargs):
        observed["dataset"] = dataset
        observed["kwargs"] = kwargs
        return _Frame(
            [
                {"trade_date": date(2026, 8, 15), "is_trading": False},
                {"trade_date": date(2026, 8, 16), "is_trading": False},
                {"trade_date": "2026-08-17", "is_trading": True},
                # An identical duplicate is harmless; conflicting state is not.
                {"trade_date": date(2026, 8, 17), "is_trading": True},
                {"trade_date": date(2026, 8, 18), "is_trading": True},
            ]
        )

    _install_fake_reader(monkeypatch, fake_load)
    sessions = CNEquityBridge(tmp_path).load_trading_sessions(
        date(2026, 8, 15),
        date(2026, 8, 18),
    )

    assert sessions == [date(2026, 8, 17), date(2026, 8, 18)]
    assert observed == {
        "dataset": "trading_calendar",
        "kwargs": {
            "start": date(2026, 8, 15),
            "end": date(2026, 8, 18),
            "data_root": tmp_path.resolve(),
        },
    }


def test_load_trading_sessions_rejects_conflicting_calendar_rows(
    tmp_path: Path,
    monkeypatch,
):
    def fake_load(dataset, **kwargs):
        return _Frame(
            [
                {"trade_date": date(2026, 8, 17), "is_trading": True},
                {"trade_date": date(2026, 8, 17), "is_trading": False},
            ]
        )

    _install_fake_reader(monkeypatch, fake_load)

    with pytest.raises(ValueError, match=r"conflicting.*2026-08-17"):
        CNEquityBridge(tmp_path).load_trading_sessions(
            date(2026, 8, 17),
            date(2026, 8, 17),
        )


def test_load_trading_sessions_rejects_calendar_coverage_holes(
    tmp_path: Path,
    monkeypatch,
):
    def fake_load(dataset, **kwargs):
        return _Frame(
            [
                {"trade_date": date(2026, 8, 15), "is_trading": False},
                {"trade_date": date(2026, 8, 17), "is_trading": True},
            ]
        )

    _install_fake_reader(monkeypatch, fake_load)

    with pytest.raises(ValueError, match=r"densely cover.*2026-08-16"):
        CNEquityBridge(tmp_path).load_trading_sessions(
            date(2026, 8, 15),
            date(2026, 8, 17),
        )


def test_load_index_bars_names_index_dataset_explicitly(tmp_path: Path, monkeypatch):
    observed = {}

    def fake_load(dataset, **kwargs):
        observed["dataset"] = dataset
        observed["kwargs"] = kwargs
        return _Frame([])

    _install_fake_reader(monkeypatch, fake_load)
    assert CNEquityBridge(tmp_path).load_index_bars(
        ["000300.SH"], "2026-08-18", "2026-08-19"
    ) == []
    assert observed == {
        "dataset": "index_bars",
        "kwargs": {
            "symbols": ["000300.SH"],
            "start": "2026-08-18",
            "end": "2026-08-19",
            "data_root": tmp_path.resolve(),
        },
    }


def test_load_daily_bars_reports_missing_cnequity_runtime(tmp_path: Path, monkeypatch):
    for name in tuple(sys.modules):
        if name == "cnequity" or name.startswith("cnequity."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    real_import = builtins.__import__

    def missing_cnequity(name, *args, **kwargs):
        if name == "cnequity.query.reader":
            raise ModuleNotFoundError("No module named 'cnequity'", name="cnequity")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_cnequity)

    with pytest.raises(RuntimeError, match=r"pip install cnequity"):
        CNEquityBridge(tmp_path).load_daily_bars(
            ["600519.SH"], "2026-05-22", "2026-08-19"
        )


def test_missing_data_root_has_actionable_error(tmp_path: Path):
    missing = tmp_path / "not-created"

    with pytest.raises(FileNotFoundError, match=r"cne init"):
        CNEquityBridge(missing)


def test_describe_generation_hashes_sorted_relative_metadata_and_coverage(tmp_path: Path):
    early = (
        tmp_path
        / "curated"
        / "daily_bars"
        / "trade_date=2026-05-22"
        / "part-z.parquet"
    )
    late = (
        tmp_path
        / "curated"
        / "daily_bars"
        / "trade_date=2026-08-19"
        / "part-a.parquet"
    )
    early.parent.mkdir(parents=True)
    late.parent.mkdir(parents=True)
    early.write_bytes(b"early")
    late.write_bytes(b"latest-data")
    os.utime(early, ns=(1_700_000_000_000_000_001, 1_700_000_000_000_000_001))
    os.utime(late, ns=(1_700_000_000_000_000_002, 1_700_000_000_000_000_002))

    description = CNEquityBridge(tmp_path).describe_generation()
    files = [
        {
            "relative_path": "curated/daily_bars/trade_date=2026-05-22/part-z.parquet",
            "size": 5,
            "mtime_ns": early.stat().st_mtime_ns,
        },
        {
            "relative_path": "curated/daily_bars/trade_date=2026-08-19/part-a.parquet",
            "size": 11,
            "mtime_ns": late.stat().st_mtime_ns,
        },
    ]

    assert description == {
        "dataset": "daily_bars",
        "generation_sha256": sha256_hex(files),
        "coverage": {"min": "2026-05-22", "max": "2026-08-19"},
        "file_count": 2,
        "total_size": 16,
        "files": files,
    }
    assert CNEquityBridge(tmp_path).describe_generation() == description


def test_describe_generation_rejects_missing_dataset_and_path_traversal(tmp_path: Path):
    bridge = CNEquityBridge(tmp_path)

    with pytest.raises(FileNotFoundError, match=r"cne init.*cne backfill"):
        bridge.describe_generation("daily_bars")
    with pytest.raises(ValueError, match="letters, numbers, and underscores"):
        bridge.describe_generation("../daily_bars")
