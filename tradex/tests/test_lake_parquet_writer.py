"""Focused tests for atomic raw-capture Parquet publication."""

from __future__ import annotations

import builtins
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

import pytest

from tradex.data_lake import parquet_writer
from tradex.data_lake.contracts import CaptureBundle, CapturedDataset
from tradex.data_lake.object_store import ObjectStore
from tradex.data_lake.parquet_writer import (
    ParquetConflictError,
    ParquetWriter,
    write_capture,
)
from tradex.data_lake.serde import sha256_hex


SHANGHAI = ZoneInfo("Asia/Shanghai")
OBSERVED = datetime(2026, 8, 19, 10, 1, 20, tzinfo=SHANGHAI)
MINUTE = OBSERVED.replace(second=0)


def _dataset(records=None, *, name="rotation_boards") -> CapturedDataset:
    return CapturedDataset(
        name=name,
        records=tuple(
            records
            if records is not None
            else (
                {
                    "taxonomy": "industry",
                    "board_code": "BK1036",
                    "name": "半导体",
                    "change_pct": "3.25%",
                    "up_count": "70",
                    "down_count": 30,
                    "flow_amount": "500,000,000",
                    "flow_ratio": 5.2,
                    "flow_rank": "4",
                },
                {
                    "板块代码": "BK0800",
                    "板块名称": "机器人",
                    "涨跌幅": -1.5,
                    "上涨家数": 10,
                    "下跌家数": 40,
                    "主力净流入": -20_000_000,
                    "主力净流入-占比": "-2.1%",
                    "主力净流入排名": 8,
                },
            )
        ),
        source="eastmoney",
        provider_as_of="2026-08-19T10:01:00+08:00",
        schema_version="rotation-raw-v1",
    )


def _bundle(dataset: CapturedDataset, *, capture_id="capture-1") -> CaptureBundle:
    return CaptureBundle(
        capture_id=capture_id,
        observed_at=OBSERVED,
        trade_date=OBSERVED.date(),
        minute_bucket=MINUTE,
        market_phase="trading",
        market_data={"provider_as_of": dataset.provider_as_of},
        datasets={dataset.name: dataset},
    )


def _install_fake_duckdb(monkeypatch):
    state = {"frames": [], "connections": []}
    columns = parquet_writer._PUBLISHED_COLUMNS

    class Connection:
        def __init__(self):
            self.frame = None
            self.result = []
            self.closed = False

        def register(self, _name, frame):
            self.frame = frame
            state["frames"].append(frame.copy())

        def unregister(self, _name):
            return None

        def execute(self, sql, params=None):
            if sql.startswith("COPY "):
                match = re.search(r" TO '(.+)' \(FORMAT PARQUET", sql)
                assert match is not None
                target = Path(match.group(1).replace("''", "'"))
                target.write_text(
                    self.frame.to_json(
                        orient="records", date_format="iso", date_unit="us"
                    ),
                    encoding="utf-8",
                )
                self.result = []
                return self
            if "FROM read_parquet(?)" in sql:
                assert params and len(params) == 1
                records = json.loads(Path(params[0]).read_text(encoding="utf-8"))
                if sql.startswith("SELECT preflight_value"):
                    self.result = [(records[0]["preflight_value"],)]
                    return self
                records.sort(key=lambda row: row["record_index"])
                self.result = [tuple(record.get(column) for column in columns) for record in records]
                return self
            raise AssertionError(f"unexpected SQL: {sql}")

        def fetchall(self):
            return self.result

        def fetchone(self):
            return self.result[0] if self.result else None

        def close(self):
            self.closed = True

    module = ModuleType("duckdb")

    def connect(**_kwargs):
        connection = Connection()
        state["connections"].append(connection)
        return connection

    module.connect = connect
    monkeypatch.setitem(sys.modules, "duckdb", module)
    return state


def test_write_capture_publishes_atomic_typed_rows_and_artifact(tmp_path: Path, monkeypatch):
    state = _install_fake_duckdb(monkeypatch)
    dataset = _dataset()
    bundle = _bundle(dataset)
    object_ref = ObjectStore(tmp_path).put_json("dataset", dataset.records)
    replacements = []
    real_replace = parquet_writer.os.replace

    def recording_replace(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        assert source_path.parent == destination_path.parent
        assert source_path.suffix == ".tmp"
        replacements.append((source_path, destination_path))
        return real_replace(source, destination)

    monkeypatch.setattr(parquet_writer.os, "replace", recording_replace)
    artifact = ParquetWriter(tmp_path).write_capture(dataset, bundle, object_ref)

    expected_relative = (
        "parquet/raw/rotation_boards/trade_date=2026-08-19/"
        "capture_id=capture-1/part-0.parquet"
    )
    assert artifact.artifact_id == "raw:capture-1:rotation_boards"
    assert artifact.dataset == "rotation_boards"
    assert artifact.layer == "raw"
    assert artifact.object_sha256 == object_ref.sha256
    assert artifact.object_path == object_ref.relative_path
    assert artifact.parquet_path == expected_relative
    assert artifact.parquet_sha256 == hashlib.sha256(
        (tmp_path / expected_relative).read_bytes()
    ).hexdigest()
    assert artifact.row_count == 2
    assert artifact.schema_version == "rotation-raw-v1"
    assert (tmp_path / expected_relative).is_file()
    assert len(replacements) == 1
    assert list((tmp_path / expected_relative).parent.glob("*.tmp")) == []

    frame = state["frames"][0]
    assert list(frame.columns) == list(parquet_writer._PUBLISHED_COLUMNS)
    assert str(frame.dtypes["change_pct"]) == "Float64"
    assert str(frame.dtypes["up_count"]) == "Int64"
    assert str(frame.dtypes["flow_rank"]) == "Int64"
    assert frame.loc[0, "taxonomy"] == "industry"
    assert frame.loc[0, "change_pct"] == 3.25
    assert frame.loc[0, "flow_amount"] == 500_000_000
    assert frame.loc[1, "board_code"] == "BK0800"
    assert frame.loc[1, "name"] == "机器人"
    assert frame.loc[1, "flow_ratio"] == -2.1
    assert frame.loc[0, "payload_sha256"] == sha256_hex(dataset.records[0])


def test_same_capture_retry_verifies_content_and_does_not_replace(tmp_path: Path, monkeypatch):
    _install_fake_duckdb(monkeypatch)
    dataset = _dataset()
    bundle = _bundle(dataset)
    object_ref = ObjectStore(tmp_path).put_json("dataset", dataset.records)
    calls = []
    real_replace = parquet_writer.os.replace

    def recording_replace(source, destination):
        calls.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr(parquet_writer.os, "replace", recording_replace)
    first = write_capture(dataset, bundle, object_ref)
    target = tmp_path / first.parquet_path
    before = target.read_bytes()
    second = write_capture(dataset, bundle, object_ref)

    assert second == first
    assert target.read_bytes() == before
    assert len(calls) == 1
    assert list(target.parent.glob("*.tmp")) == []


def test_same_capture_with_different_content_is_a_conflict(tmp_path: Path, monkeypatch):
    _install_fake_duckdb(monkeypatch)
    original = _dataset(records=({"taxonomy": "industry", "board_code": "BK1"},))
    original_bundle = _bundle(original)
    original_ref = ObjectStore(tmp_path).put_json("dataset", original.records)
    artifact = write_capture(original, original_bundle, original_ref)
    target = tmp_path / artifact.parquet_path
    before = target.read_bytes()

    changed = _dataset(
        records=({"taxonomy": "industry", "board_code": "BK1", "change_pct": 9.9},)
    )
    changed_bundle = _bundle(changed)
    changed_ref = ObjectStore(tmp_path).put_json("dataset", changed.records)

    with pytest.raises(ParquetConflictError, match="different content"):
        write_capture(changed, changed_bundle, changed_ref)

    assert target.read_bytes() == before
    assert list(target.parent.glob("*.tmp")) == []


def test_same_capture_retry_rejects_byte_tampering_even_when_rows_parse(
    tmp_path: Path,
    monkeypatch,
):
    _install_fake_duckdb(monkeypatch)
    dataset = _dataset()
    bundle = _bundle(dataset)
    object_ref = ObjectStore(tmp_path).put_json("dataset", dataset.records)
    artifact = write_capture(dataset, bundle, object_ref)
    target = tmp_path / artifact.parquet_path
    target.write_bytes(target.read_bytes() + b"\n")

    with pytest.raises(ParquetConflictError, match="different content"):
        write_capture(dataset, bundle, object_ref)


def test_empty_capture_publishes_zero_row_artifact(tmp_path: Path, monkeypatch):
    _install_fake_duckdb(monkeypatch)
    dataset = _dataset(records=(), name="market_breadth")
    bundle = _bundle(dataset, capture_id="empty-capture")
    object_ref = ObjectStore(tmp_path).put_json("dataset", dataset.records)

    artifact = write_capture(dataset, bundle, object_ref)

    assert artifact.row_count == 0
    assert (tmp_path / artifact.parquet_path).is_file()


@pytest.mark.parametrize(
    ("dataset_name", "capture_id", "message"),
    [
        ("../escape", "capture-1", "dataset.name"),
        ("safe_dataset", "../escape", "bundle.capture_id"),
    ],
)
def test_path_components_cannot_escape_lake(
    tmp_path: Path, dataset_name: str, capture_id: str, message: str
):
    dataset = _dataset(records=(), name=dataset_name)
    bundle = _bundle(dataset, capture_id=capture_id)
    object_ref = ObjectStore(tmp_path).put_json("dataset", dataset.records)

    with pytest.raises(ValueError, match=re.escape(message)):
        write_capture(dataset, bundle, object_ref)

    assert not (tmp_path.parent / "escape").exists()


def test_missing_duckdb_has_actionable_lake_extra_error(tmp_path: Path, monkeypatch):
    dataset = _dataset(records=())
    bundle = _bundle(dataset)
    object_ref = ObjectStore(tmp_path).put_json("dataset", dataset.records)
    monkeypatch.delitem(sys.modules, "duckdb", raising=False)
    real_import = builtins.__import__

    def missing_duckdb(name, *args, **kwargs):
        if name == "duckdb":
            raise ModuleNotFoundError("No module named 'duckdb'", name="duckdb")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_duckdb)

    with pytest.raises(RuntimeError, match=r"pip install -e \.\[lake\]"):
        write_capture(dataset, bundle, object_ref)


def test_parquet_preflight_round_trips_once_per_writer(tmp_path: Path, monkeypatch):
    state = _install_fake_duckdb(monkeypatch)
    writer = ParquetWriter(tmp_path)

    first = writer.preflight()
    second = writer.preflight()

    assert first["parquet_roundtrip"] is True
    assert second == first
    assert len(state["connections"]) == 1
    assert state["connections"][0].closed is True


def test_real_duckdb_roundtrip_when_lake_extra_is_installed(tmp_path: Path):
    duckdb = pytest.importorskip("duckdb")
    dataset = _dataset()
    bundle = _bundle(dataset, capture_id="real-engine")
    object_ref = ObjectStore(tmp_path).put_json("dataset", dataset.records)

    artifact = ParquetWriter(tmp_path).write_capture(dataset, bundle, object_ref)

    rows = duckdb.connect().execute(
        "SELECT board_code, change_pct, up_count FROM read_parquet(?) ORDER BY record_index",
        [str(tmp_path / artifact.parquet_path)],
    ).fetchall()
    assert rows == [("BK1036", 3.25, 70), ("BK0800", -1.5, 10)]
