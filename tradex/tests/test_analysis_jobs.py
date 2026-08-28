from __future__ import annotations

import sqlite3
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tradex.analysis_jobs import (
    MARKET_WATCH_EVALUATION,
    POST_MARKET_REVIEW,
    AnalysisJobCommandWriter,
    AnalysisJobReader,
    AnalysisJobStore,
    AnalysisStateUnavailable,
)
from tradex.analysis_worker import AnalysisRuntime, _compact_review_history


SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_analysis_reader_never_creates_a_missing_ledger(tmp_path):
    db_path = tmp_path / "missing" / "analysis.sqlite3"

    with pytest.raises(AnalysisStateUnavailable):
        AnalysisJobReader(db_path)

    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_analysis_reader_reads_artifacts_and_rejects_writes(tmp_path):
    db_path = tmp_path / "analysis.sqlite3"
    with AnalysisJobStore(db_path) as store:
        store.put_artifact(
            MARKET_WATCH_EVALUATION,
            scope_key="date:2026-08-26",
            source_revision="revision-1",
            payload={"contract": "market_watch_evaluation.v1"},
        )

    with AnalysisJobReader(db_path) as reader:
        artifact = reader.get_artifact(
            MARKET_WATCH_EVALUATION,
            scope_key="date:2026-08-26",
        )
        assert artifact is not None
        assert artifact["source_revision"] == "revision-1"
        with pytest.raises(sqlite3.OperationalError):
            reader._connection.execute("CREATE TABLE forbidden (id INTEGER)")


def test_analysis_command_writer_requires_worker_schema_and_only_enqueues(tmp_path):
    missing_path = tmp_path / "missing" / "analysis.sqlite3"
    with pytest.raises(AnalysisStateUnavailable):
        AnalysisJobCommandWriter(missing_path)
    assert not missing_path.exists()

    db_path = tmp_path / "analysis.sqlite3"
    with AnalysisJobStore(db_path):
        pass
    with AnalysisJobCommandWriter(db_path) as writer:
        job = writer.enqueue(
            POST_MARKET_REVIEW,
            trade_date="2026-08-26",
            trigger="manual",
        )
    assert job["state"] == "queued"


def test_analysis_queue_is_durable_idempotent_and_retryable(tmp_path):
    requested = datetime(2026, 8, 26, 18, 0, tzinfo=SHANGHAI)
    with AnalysisJobStore(tmp_path / "analysis.sqlite3") as store:
        first = store.enqueue(
            POST_MARKET_REVIEW,
            trade_date=requested.date(),
            trigger="manual",
            requested_at=requested,
        )
        duplicate = store.enqueue(
            POST_MARKET_REVIEW,
            trade_date=requested.date(),
            trigger="manual",
            requested_at=requested,
        )

        assert duplicate["job_id"] == first["job_id"]
        assert duplicate["state"] == "queued"

        claimed = store.claim_next()
        assert claimed is not None
        assert claimed["job_id"] == first["job_id"]
        assert claimed["state"] == "running"
        store.set_phase(first["job_id"], "publishing")
        store.succeed(first["job_id"], result={"trade_date": "2026-08-26"})

        completed = store.latest_job(POST_MARKET_REVIEW, trade_date=requested.date())
        assert completed is not None
        assert completed["state"] == "succeeded"
        assert completed["phase"] == "completed"
        assert completed["result"] == {"trade_date": "2026-08-26"}

        retry = store.enqueue(
            POST_MARKET_REVIEW,
            trade_date=requested.date(),
            trigger="manual",
            requested_at=requested,
        )
        assert retry["job_id"] != first["job_id"]
        assert retry["state"] == "queued"


def test_analysis_artifact_round_trip_keeps_source_revision(tmp_path):
    with AnalysisJobStore(tmp_path / "analysis.sqlite3") as store:
        stored = store.put_artifact(
            MARKET_WATCH_EVALUATION,
            scope_key="date:2026-08-26",
            source_revision="source-revision-1",
            payload={"contract": "market_watch_evaluation.v1", "session_count": 1},
        )

        assert stored["source_revision"] == "source-revision-1"
        assert stored["payload"] == {
            "contract": "market_watch_evaluation.v1",
            "session_count": 1,
        }
        assert len(stored["payload_digest"]) == 64


def test_interrupted_worker_job_returns_to_queue(tmp_path):
    with AnalysisJobStore(tmp_path / "analysis.sqlite3") as store:
        queued = store.enqueue(
            POST_MARKET_REVIEW,
            trade_date="2026-08-26",
            trigger="manual",
        )
        assert store.claim_next()["state"] == "running"

        assert store.recover_interrupted() == 1
        recovered = store.latest_job(POST_MARKET_REVIEW, trade_date="2026-08-26")
        assert recovered is not None
        assert recovered["job_id"] == queued["job_id"]
        assert recovered["state"] == "queued"
        assert recovered["started_at"] is None


def test_review_display_projection_excludes_raw_evidence_but_keeps_coverage():
    payload = {
        "contract": "post_market_review_archive.v1",
        "schema_version": 1,
        "review": {
            "contract": "post_market_review.v1",
            "schema_version": 1,
            "review_id": "review-1",
            "trade_date": "2026-08-26",
            "quality": "ready",
            "evidence": {
                "components": [{"component": "market_universe", "status": "accepted"}],
                "very_large_raw_rows": [{"instrument_id": str(index)} for index in range(50)],
            },
            "limitations": [],
        },
        "presentation": {"review_id": "review-1", "sections": []},
    }

    compact = _compact_review_history(payload)

    assert compact["presentation"] == payload["presentation"]
    assert compact["review"]["evidence"] == {
        "components": [{"component": "market_universe", "status": "accepted"}]
    }
    assert "very_large_raw_rows" not in compact["review"]["evidence"]


def test_worker_executes_review_job_and_publishes_only_a_small_result(tmp_path):
    requested = datetime(2026, 8, 26, 18, 0, tzinfo=SHANGHAI)
    with AnalysisJobStore(tmp_path / "analysis.sqlite3") as store:
        store.enqueue(
            POST_MARKET_REVIEW,
            trade_date=requested.date(),
            trigger="manual",
            requested_at=requested,
        )
        runtime = AnalysisRuntime.__new__(AnalysisRuntime)
        runtime.jobs = store
        runtime.review_service = SimpleNamespace(
            generate=lambda **_kwargs: {
                "action": "inserted",
                "review": {
                    "trade_date": "2026-08-26",
                    "review_id": "review-1",
                    "evidence": {"large": list(range(100))},
                },
            }
        )
        runtime.materialize_review_views = lambda force=False: 1

        completed = runtime.execute_next_job()

        assert completed is not None
        assert completed["state"] == "succeeded"
        assert completed["result"] == {
            "action": "inserted",
            "trade_date": "2026-08-26",
            "review_id": "review-1",
        }
        assert "evidence" not in completed["result"]
