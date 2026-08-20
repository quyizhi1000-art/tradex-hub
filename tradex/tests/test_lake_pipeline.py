from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from tradex.data_lake import pipeline as pipeline_module
from tradex.data_lake.catalog import Catalog
from tradex.data_lake.contracts import ArtifactRef, CaptureBundle, CapturedDataset
from tradex.data_lake.live_source import LiveCaptureResult
from tradex.data_lake.object_store import ObjectStore
from tradex.data_lake.pipeline import CapturePipeline, CaptureResult, capture_id_for_minute


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _feature_payload():
    components = {
        name: {"stale": False, "partial": False, "expired": False}
        for name in ("market_breadth", "industry_quotes", "concept_quotes", "leadership_pool")
    }
    return {
        "version": "risk-v1",
        "opening_observation": False,
        "stale": False,
        "emotion": {"key": "strong"},
        "structure": {"tone": "positive"},
        "data_quality": {
            "key": "high",
            "evidence_available": 8,
            "evidence_total": 10,
        },
        "components": components,
    }


class FakeSource:
    def __init__(self, at: datetime):
        self.at = at
        self.calls = 0

    def capture(self, capture_id: str):
        self.calls += 1
        dataset = CapturedDataset(
            name="market_breadth",
            records=({"up_count": 3200, "source": "fixture"},),
            source="fixture",
            status={"status": "ready"},
        )
        bundle = CaptureBundle(
            capture_id=capture_id,
            observed_at=self.at,
            trade_date=date(2026, 8, 19),
            minute_bucket=self.at.replace(second=0, microsecond=0),
            market_phase="trading",
            market_data={"source": "fixture"},
            datasets={"market_breadth": dataset},
        )
        return LiveCaptureResult(bundle, _feature_payload())


@dataclass
class FakeParquetWriter:
    fail: bool = False
    mutate_bundle: bool = False

    def write_capture(self, dataset, bundle, object_ref):
        if self.fail:
            raise RuntimeError("parquet unavailable")
        if self.mutate_bundle:
            dataset.records[0]["up_count"] = -1
        return ArtifactRef(
            artifact_id=f"raw-{bundle.capture_id}-{dataset.name}",
            dataset=dataset.name,
            layer="raw",
            object_sha256=object_ref.sha256,
            object_path=object_ref.relative_path,
            parquet_path=None,
            parquet_sha256=None,
            row_count=len(dataset.records),
            schema_version=dataset.schema_version,
        )


def test_pipeline_atomically_publishes_and_deduplicates_same_minute(tmp_path):
    at = datetime(2026, 8, 19, 10, 8, 30, tzinfo=SHANGHAI)
    source = FakeSource(at)
    catalog = Catalog(tmp_path / "meta" / "catalog.sqlite3")
    pipeline = CapturePipeline(
        tmp_path,
        source=source,
        object_store=ObjectStore(tmp_path),
        catalog=catalog,
        parquet_writer=FakeParquetWriter(),
        code_sha="fixture-sha",
    )

    first = pipeline.capture_once(at)
    second = pipeline.capture_once(at.replace(second=59))

    assert first.status == "completed"
    assert second.status == "already_completed"
    assert source.calls == 1
    published = catalog.get_capture(first.capture_id)
    assert published["status"] == "completed"
    assert published["artifacts"][0]["row_count"] == 1
    assert published["decisions"][0]["mode"] == "shadow"


def test_pipeline_failure_is_not_visible_as_completed(tmp_path):
    at = datetime(2026, 8, 19, 10, 9, tzinfo=SHANGHAI)
    source = FakeSource(at)
    catalog = Catalog(tmp_path / "meta" / "catalog.sqlite3")
    pipeline = CapturePipeline(
        tmp_path,
        source=source,
        catalog=catalog,
        parquet_writer=FakeParquetWriter(fail=True),
    )

    result = pipeline.capture_once(at)

    assert result.status == "failed"
    assert catalog.get_capture(capture_id_for_minute(at)) is None
    failed = catalog.get_capture(capture_id_for_minute(at), include_incomplete=True)
    assert failed["status"] == "failed"
    assert "parquet unavailable" in failed["error"]


def test_failed_minute_is_not_refetched_or_rebegun(tmp_path):
    at = datetime(2026, 8, 19, 10, 9, 10, tzinfo=SHANGHAI)
    source = FakeSource(at)
    catalog = Catalog(tmp_path / "meta" / "catalog.sqlite3")
    pipeline = CapturePipeline(
        tmp_path,
        source=source,
        catalog=catalog,
        parquet_writer=FakeParquetWriter(fail=True),
    )

    first = pipeline.capture_once(at)
    second = pipeline.capture_once(at.replace(second=55))

    assert first.status == "failed"
    assert second.status == "already_failed"
    assert "next minute" in second.error
    assert "new capture identity" in second.error
    assert source.calls == 1
    failed = catalog.get_capture(
        capture_id_for_minute(at),
        include_incomplete=True,
    )
    assert failed["status"] == "failed"
    assert "parquet unavailable" in failed["error"]


def test_default_writer_preflight_fails_before_live_source(
    tmp_path,
    monkeypatch,
):
    at = datetime(2026, 8, 19, 10, 9, tzinfo=SHANGHAI)
    source = FakeSource(at)
    calls = []

    def failed_preflight(self):
        calls.append(self)
        raise RuntimeError("DuckDB Parquet preflight failed")

    monkeypatch.setattr(
        "tradex.data_lake.parquet_writer.ParquetWriter.preflight",
        failed_preflight,
    )
    catalog = Catalog(tmp_path / "meta" / "catalog.sqlite3")
    pipeline = CapturePipeline(tmp_path, source=source, catalog=catalog)

    result = pipeline.capture_once(at)

    assert result.status == "failed"
    assert "DuckDB Parquet preflight failed" in result.error
    assert len(calls) == 1
    assert source.calls == 0
    assert catalog.get_capture(
        capture_id_for_minute(at),
        include_incomplete=True,
    ) is None


def test_pipeline_refuses_bundle_content_that_drifts_after_begin(tmp_path):
    at = datetime(2026, 8, 19, 10, 10, tzinfo=SHANGHAI)
    catalog = Catalog(tmp_path / "meta" / "catalog.sqlite3")
    pipeline = CapturePipeline(
        tmp_path,
        source=FakeSource(at),
        catalog=catalog,
        parquet_writer=FakeParquetWriter(mutate_bundle=True),
        code_sha="fixture-sha",
    )

    result = pipeline.capture_once(at)

    assert result.status == "failed"
    assert "CaptureBundle changed after begin_capture" in result.error
    capture_id = capture_id_for_minute(at)
    assert catalog.get_capture(capture_id) is None
    assert catalog.get_capture(capture_id, include_incomplete=True)["artifacts"] == []


def test_default_code_sha_prefers_environment_then_git_state(monkeypatch):
    monkeypatch.setenv("TRADEX_CODE_SHA", "release-2026-08-19")
    monkeypatch.setattr(pipeline_module, "_git_code_sha", lambda: "abc123+dirty")
    assert pipeline_module._default_code_sha() == "release-2026-08-19"

    monkeypatch.delenv("TRADEX_CODE_SHA")
    assert pipeline_module._default_code_sha() == "abc123+dirty"


def test_collect_run_logs_every_result_and_counts_consecutive_failures(
    tmp_path,
    monkeypatch,
    caplog,
):
    at = datetime(2026, 8, 19, 10, 12, tzinfo=SHANGHAI)
    pipeline = CapturePipeline(
        tmp_path,
        source=FakeSource(at),
        parquet_writer=FakeParquetWriter(),
        code_sha="fixture-sha",
    )
    results = iter(
        [
            CaptureResult("capture-1", "failed", 0, error="network down"),
            CaptureResult("capture-1", "already_failed", 0, error="same minute"),
            CaptureResult("capture-2", "completed", 1),
        ]
    )
    monkeypatch.setattr(pipeline, "capture_once", lambda now=None: next(results))

    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            return at

    class StopAfterThree:
        waits = 0

        def is_set(self):
            return self.waits >= 3

        def wait(self, interval):
            self.waits += 1
            return self.is_set()

    monkeypatch.setattr(pipeline_module, "datetime", FixedDateTime)
    caplog.set_level(logging.INFO, logger=pipeline_module.__name__)

    pipeline.run(StopAfterThree(), interval_seconds=0.01)

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == pipeline_module.__name__
    ]
    assert len(messages) == 3
    assert "status=failed" in messages[0]
    assert "consecutive_failures=1" in messages[0]
    assert "status=already_failed" in messages[1]
    assert "consecutive_failures=2" in messages[1]
    assert "status=completed" in messages[2]
    assert "consecutive_failures=0" in messages[2]
