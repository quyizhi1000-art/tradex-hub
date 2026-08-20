"""Focused tests for the SQLite lake publication catalog."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tradex.data_lake.catalog import (
    CATALOG_SCHEMA_VERSION,
    Catalog,
    CatalogConflictError,
    CatalogReadOnlyError,
    CatalogSchemaError,
)
from tradex.data_lake.contracts import (
    ArtifactRef,
    CaptureBundle,
    CapturedDataset,
    DecisionDraft,
    FeatureSnapshot,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
OBSERVED = datetime(2026, 8, 19, 10, 1, 20, tzinfo=SHANGHAI)
MINUTE = OBSERVED.replace(second=0)


def _bundle(capture_id: str, *, market_value: int = 1) -> CaptureBundle:
    dataset = CapturedDataset(
        name="market_breadth",
        records=({"上涨": 3200, "下跌": 1800},),
        source="eastmoney",
        provider_as_of="2026-08-19T10:01:00+08:00",
        status={"stale": False},
    )
    return CaptureBundle(
        capture_id=capture_id,
        observed_at=OBSERVED,
        trade_date=OBSERVED.date(),
        minute_bucket=MINUTE,
        market_phase="trading",
        market_data={"value": market_value},
        datasets={dataset.name: dataset},
    )


def _publication(capture_id: str, *, artifact_id: str = "artifact-1"):
    artifact = ArtifactRef(
        artifact_id=artifact_id,
        dataset="market_breadth",
        layer="raw",
        object_sha256="a" * 64,
        object_path=f"objects/sha256/aa/{'a' * 64}.json.gz",
        parquet_path=(
            f"parquet/raw/market_breadth/trade_date=2026-08-19/"
            f"capture_id={capture_id}/part-0.parquet"
        ),
        parquet_sha256="b" * 64,
        row_count=1,
        schema_version="market-breadth-v1",
    )
    feature = FeatureSnapshot(
        feature_id=f"feature-{capture_id}",
        capture_id=capture_id,
        name="risk_appetite",
        schema_version="risk-feature-v1",
        config_version="risk-appetite-v1.3",
        payload={"quality": "high"},
        input_artifact_ids=(artifact.artifact_id,),
        code_sha="deadbeef",
        created_at=OBSERVED,
    )
    decision = DecisionDraft(
        decision_id=f"decision-{capture_id}",
        feature_id=feature.feature_id,
        policy_version="shadow-v1",
        effective_at=OBSERVED,
        offense_weight=0.3,
        defense_weight=0.4,
        cash_weight=0.3,
        contributions={"breadth": 0.2},
        confidence=0.7,
    )
    return (artifact,), (feature,), decision


def test_begin_is_wal_backed_and_incomplete_capture_is_hidden(tmp_path: Path):
    db_path = tmp_path / "meta" / "catalog.sqlite3"
    with Catalog(db_path) as catalog:
        started = catalog.begin_capture(_bundle("capture-1"))

        assert started["status"] == "started"
        assert catalog.get_capture("capture-1") is None
        assert catalog.get_capture("capture-1", include_incomplete=True)["status"] == "started"
        assert catalog.latest_completed() is None
        assert catalog._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"capture_runs", "artifacts", "features", "decisions", "outcome_labels"} <= tables


def test_commit_atomically_publishes_full_audit_record_and_pending_label(tmp_path: Path):
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.begin_capture(_bundle("capture-1"))
        artifacts, features, decision = _publication("capture-1")
        completed = catalog.commit_capture(
            "capture-1",
            artifacts,
            features,
            decision,
        )

        assert completed["status"] == "completed"
        assert completed["publication_sha256"]
        assert completed["artifacts"] == [{
            "artifact_id": "artifact-1",
            "dataset": "market_breadth",
            "layer": "raw",
            "object_sha256": "a" * 64,
            "object_path": f"objects/sha256/aa/{'a' * 64}.json.gz",
            "parquet_path": (
                "parquet/raw/market_breadth/trade_date=2026-08-19/"
                "capture_id=capture-1/part-0.parquet"
            ),
            "parquet_sha256": "b" * 64,
            "row_count": 1,
            "schema_version": "market-breadth-v1",
        }]
        assert completed["features"][0]["payload"] == {"quality": "high"}
        assert completed["decisions"][0]["mode"] == "shadow"
        assert catalog.latest_completed()["capture_id"] == "capture-1"
        assert [item["decision_id"] for item in catalog.pending_labels()] == [
            "decision-capture-1"
        ]


def test_capture_retry_is_idempotent_and_completed_cannot_be_failed(tmp_path: Path):
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        bundle = _bundle("capture-1")
        first_start = catalog.begin_capture(bundle)
        second_start = catalog.begin_capture(bundle)
        assert first_start["started_at"] == second_start["started_at"]

        publication = _publication("capture-1")
        first_commit = catalog.commit_capture("capture-1", *publication)
        second_commit = catalog.commit_capture("capture-1", *publication)
        assert first_commit["publication_sha256"] == second_commit["publication_sha256"]
        assert len(second_commit["artifacts"]) == 1

        unchanged = catalog.fail_capture("capture-1", "late worker error")
        assert unchanged["status"] == "completed"
        assert unchanged["error"] is None
        assert catalog.begin_capture(bundle)["status"] == "completed"

        with pytest.raises(CatalogConflictError, match="different bundle"):
            catalog.begin_capture(_bundle("capture-1", market_value=2))


def test_failed_capture_can_resume_but_must_be_rebegun_before_commit(tmp_path: Path):
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        bundle = _bundle("capture-failed")
        catalog.begin_capture(bundle)
        failed = catalog.fail_capture("capture-failed", "upstream timeout")
        assert failed["status"] == "failed"
        assert catalog.get_capture("capture-failed") is None

        with pytest.raises(CatalogConflictError, match="begun again"):
            catalog.commit_capture("capture-failed", *_publication("capture-failed"))

        resumed = catalog.begin_capture(bundle)
        assert resumed["status"] == "started"
        assert resumed["error"] is None
        completed = catalog.commit_capture(
            "capture-failed", *_publication("capture-failed")
        )
        assert completed["status"] == "completed"


def test_failed_commit_rolls_back_every_publication_row(tmp_path: Path):
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.begin_capture(_bundle("capture-1"))
        catalog.commit_capture("capture-1", *_publication("capture-1"))

        catalog.begin_capture(_bundle("capture-2"))
        # artifact-1 is globally immutable and already belongs to capture-1.
        with pytest.raises(CatalogConflictError, match="publication conflicts"):
            catalog.commit_capture(
                "capture-2",
                *_publication("capture-2", artifact_id="artifact-1"),
            )

        incomplete = catalog.get_capture("capture-2", include_incomplete=True)
        assert incomplete["status"] == "started"
        assert incomplete["artifacts"] == []
        assert incomplete["features"] == []
        assert incomplete["decisions"] == []


def test_pending_labels_filters_existing_name_and_version(tmp_path: Path):
    db_path = tmp_path / "catalog.sqlite3"
    with Catalog(db_path) as catalog:
        catalog.begin_capture(_bundle("capture-1"))
        catalog.commit_capture("capture-1", *_publication("capture-1"))
        catalog._connection.execute(
            """
            INSERT INTO outcome_labels (
                label_id, decision_id, label_name, horizon_sessions,
                label_version, source_generation, payload_json, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "label-1",
                "decision-capture-1",
                "benchmark_1d",
                1,
                "label-v1",
                "generation-1",
                "{}",
                OBSERVED.isoformat(),
            ),
        )

        assert catalog.pending_labels(label_name="benchmark_1d") == []
        assert catalog.pending_labels(
            label_name="benchmark_1d", label_version="label-v1"
        ) == []
        assert [item["decision_id"] for item in catalog.pending_labels(
            label_name="benchmark_1d", label_version="label-v2"
        )] == ["decision-capture-1"]

        catalog._connection.execute(
            "UPDATE decisions SET mode = 'executed' WHERE decision_id = ?",
            ("decision-capture-1",),
        )
        assert catalog.pending_labels(
            label_name="benchmark_1d", label_version="label-v2"
        ) == []


@pytest.mark.parametrize("damage", ["missing", "extra", "duplicate"])
def test_commit_requires_exactly_one_artifact_for_each_begun_dataset(
    tmp_path: Path,
    damage: str,
):
    with Catalog(tmp_path / f"{damage}.sqlite3") as catalog:
        catalog.begin_capture(_bundle("capture-1"))
        artifacts, _, _ = _publication("capture-1")
        if damage == "missing":
            damaged = ()
        elif damage == "extra":
            damaged = (
                replace(artifacts[0], artifact_id="extra", dataset="unexpected"),
            )
        else:
            damaged = (
                artifacts[0],
                replace(artifacts[0], artifact_id="artifact-2"),
            )

        with pytest.raises(ValueError, match="must match begin_capture exactly once"):
            catalog.commit_capture("capture-1", damaged)

        incomplete = catalog.get_capture("capture-1", include_incomplete=True)
        assert incomplete["status"] == "started"
        assert incomplete["artifacts"] == []


def test_commit_rejects_non_shadow_decision(tmp_path: Path):
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.begin_capture(_bundle("capture-1"))
        artifacts, features, decision = _publication("capture-1")

        with pytest.raises(ValueError, match="only accepts shadow"):
            catalog.commit_capture(
                "capture-1",
                artifacts,
                features,
                replace(decision, mode="live"),
            )


def test_read_only_catalog_uses_existing_schema_without_sidecar_writes(tmp_path: Path):
    db_path = tmp_path / "meta" / "catalog.sqlite3"
    with Catalog(db_path) as writer:
        writer.begin_capture(_bundle("capture-1"))
        writer.commit_capture("capture-1", *_publication("capture-1"))

    wal_path = Path(f"{db_path}-wal")
    shm_path = Path(f"{db_path}-shm")
    assert not wal_path.exists()
    assert not shm_path.exists()

    with Catalog(db_path, read_only=True) as reader:
        assert reader.latest_completed()["capture_id"] == "capture-1"
        with pytest.raises(CatalogReadOnlyError, match="read_only=True"):
            reader.begin_capture(_bundle("capture-2"))

    assert not wal_path.exists()
    assert not shm_path.exists()


def test_catalog_reports_incompatible_user_version(tmp_path: Path):
    db_path = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"PRAGMA user_version = {CATALOG_SCHEMA_VERSION + 1}")

    with pytest.raises(CatalogSchemaError, match="incompatible Tradex catalog schema"):
        Catalog(db_path, read_only=True)


def test_read_only_catalog_never_creates_a_missing_database(tmp_path: Path):
    db_path = tmp_path / "absent" / "catalog.sqlite3"

    with pytest.raises(FileNotFoundError, match="does not exist"):
        Catalog(db_path, read_only=True)

    assert not db_path.parent.exists()
