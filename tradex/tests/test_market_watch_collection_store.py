from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tradex.market_watch import build_market_watch_snapshot
from tradex.market_watch.collection import build_collection_gap_snapshots
from tradex.market_watch.collection_contracts import (
    CollectionSlotV1,
    CollectionSlotStatus,
    CollectorRuntimeState,
    DailyRecoveryStatus,
    DailyRecoveryTrigger,
)
from tradex.market_watch.collection_store import (
    MarketWatchCollectionStore,
    expected_session_minutes,
)
from tradex.market_watch.collector import CollectorRetryPolicy, MarketWatchCollector
from tradex.market_watch.history import MarketWatchHistoryStore


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _ledger_fingerprint(db_path: Path) -> tuple[str, int, str | None]:
    with sqlite3.connect(db_path) as connection:
        revision = connection.execute(
            "SELECT value FROM market_watch_collection_meta "
            "WHERE key = 'ledger_revision'"
        ).fetchone()[0]
        row_count, latest_updated_at = connection.execute(
            "SELECT COUNT(*), MAX(updated_at) "
            "FROM market_watch_collection_slots"
        ).fetchone()
    return str(revision), int(row_count), latest_updated_at


def _snapshot(observed_at: datetime, *, snapshot_id: str, sequence: int = 1):
    specs = (
        ("broad_market", "000001.SH", "上证指数", 3400.0, 0.4),
        ("large_cap", "000300.SH", "沪深300", 4100.0, 0.5),
        ("small_cap", "000852.SH", "中证1000", 6800.0, 0.8),
        ("growth", "399006.SZ", "创业板指", 2250.0, 0.9),
    )
    market = {
        "timestamp": observed_at.isoformat(),
        "provider_as_of": observed_at.isoformat(),
        "market_state": {"phase": "trading", "is_open": True},
        "quality": "accepted",
        "indices": [
            {
                "role": role,
                "instrument_id": instrument_id,
                "name": name,
                "available": True,
                "level": level,
                "change_pct": change,
                "provider_as_of": observed_at.isoformat(),
                "quality": "accepted",
            }
            for role, instrument_id, name, level, change in specs
        ],
        "market_turnover": {
            "available": True,
            "today_date": observed_at.date().isoformat(),
            "previous_date": "2026-08-21",
            "as_of": observed_at.strftime("%H:%M"),
            "today_amount": 110_000_000_000.0,
            "previous_same_time_amount": 100_000_000_000.0,
        },
    }
    risk = {
        "timestamp": observed_at.isoformat(),
        "breadth": {
            "up_count": 3000,
            "down_count": 1800,
            "flat_count": 100,
            "unclassified_count": 100,
            "total_count": 5000,
            "provider_as_of": observed_at.isoformat(),
            "quality": "accepted",
        },
        "rotation": {
            "sectors": [
                {
                    "sector_key": "industry:securities",
                    "name": "证券",
                    "tags": ["attack"],
                    "change_pct": 1.5,
                    "breadth_ratio": 0.66,
                    "main_net_inflow_cny": 5_000_000_000.0,
                    "provider_as_of": observed_at.isoformat(),
                }
            ]
        },
    }
    return build_market_watch_snapshot(
        market,
        risk,
        as_of=observed_at,
        sequence=sequence,
        snapshot_id=snapshot_id,
    )


@pytest.mark.parametrize("status", ["stale", "unavailable"])
def test_collector_rejects_bad_quality_before_history_and_after_restart(tmp_path, status):
    from tradex.market_watch.contracts import MarketWatchSnapshotV1

    now = datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI)
    payload = _snapshot(now, snapshot_id="mw-bad-quality").model_dump(mode="json")
    payload["freshness"]["status"] = status
    for component in payload["freshness"]["components"]:
        component["status"] = status
        if status == "unavailable":
            component["quality"] = "unavailable"
    payload["guardrail"].update(regime="uncertain", severity="stop", conclusion_strength="abstain")
    snapshot = MarketWatchSnapshotV1.model_validate(payload)
    path = tmp_path / "quality.sqlite3"
    with MarketWatchHistoryStore(path, clock=lambda: now) as history, MarketWatchCollectionStore(path, clock=lambda: now) as ledger:
        collector = MarketWatchCollector(
            store=ledger, capture_current=lambda slot: snapshot,
            repair_historical=lambda slot: snapshot, persist_snapshot=history.record,
            clock=lambda: now,
        )
        result = collector.run_once()
        assert result["action"] == "failed"
        assert ledger.get_slot(now).source_snapshot_revision is None
        assert history.get_collection_records(now.date()) == []

        # A misleading record_kind must not override actual canonical quality.
        stored = history.record(snapshot)
        record = {**history.get_collection_records(now.date())[0], "record_kind": "accepted_real", "payload": payload}
        assert ledger.reconcile_history_record(record)["action"] == "skipped"
        unproven = {k: v for k, v in record.items() if k not in {"freshness_status", "payload"}}
        assert ledger.reconcile_history_record(unproven)["action"] == "skipped"
        # Reproduce the old bug in the ledger, then check restart correction is
        # exact-revision scoped and preserves original attempts and payload.
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE market_watch_collection_slots SET status='repaired', accepted_at=?, source_snapshot_id=?, source_snapshot_revision=?, gap_heartbeat=0, next_retry_at=NULL WHERE minute_bucket=?",
                               (now.isoformat(), snapshot.snapshot_id, stored["payload_digest"], now.isoformat()))
        attempts = ledger.list_attempts(now)
        stale_record = history.get_collection_records(now.date())[0]
        mismatched = {**stale_record, "payload_digest": "f" * 64}
        assert ledger.reconcile_history_record(mismatched)["action"] == "skipped"
        assert ledger.get_slot(now).source_snapshot_revision == stored["payload_digest"]
        assert ledger.reconcile_history_record(stale_record)["action"] == "invalidated"
        assert ledger.get_slot(now).status is CollectionSlotStatus.UNRESOLVED
        assert ledger.get_slot(now).source_snapshot_revision is None
        assert ledger.list_attempts(now) == attempts
        assert history.get_collection_records(now.date())[0] == stale_record
        assert ledger.reconcile_history_record(stale_record)["action"] == "skipped"


def test_expected_session_minutes_are_phase_aware_with_one_final_close() -> None:
    minutes = expected_session_minutes(date(2026, 8, 24))

    assert len(minutes) == 239
    assert minutes[0].isoformat() == "2026-08-24T09:25:00+08:00"
    assert minutes[1].isoformat() == "2026-08-24T09:30:00+08:00"
    assert minutes[120].isoformat() == "2026-08-24T11:29:00+08:00"
    assert minutes[121].isoformat() == "2026-08-24T13:00:00+08:00"
    assert minutes[-2].isoformat() == "2026-08-24T14:56:00+08:00"
    assert minutes[-1].isoformat() == "2026-08-24T15:00:00+08:00"
    assert not any(
        item.strftime("%H:%M") in {"14:57", "14:58", "14:59"}
        for item in minutes
    )
    assert expected_session_minutes(date(2026, 8, 23)) == ()


def test_legacy_closing_auction_rows_are_excluded_without_deletion(
    tmp_path: Path,
) -> None:
    trade_date = date(2026, 8, 24)
    observed = datetime(2026, 8, 24, 15, 6, tzinfo=SHANGHAI)
    db_path = tmp_path / "legacy-closing-auction.sqlite3"
    with MarketWatchCollectionStore(db_path, clock=lambda: observed) as store:
        assert store.ensure_expected_slots(trade_date) == 239
        revision = int(store._meta_locked("ledger_revision"))
        legacy = (
            (
                "2026-08-24T14:57:00+08:00",
                CollectionSlotStatus.ACCEPTED_REAL.value,
                1,
                observed.isoformat(),
                observed.isoformat(),
                observed.isoformat(),
                "mw-legacy-1457",
                "a" * 64,
                0,
            ),
            (
                "2026-08-24T14:58:00+08:00",
                CollectionSlotStatus.RETRYING.value,
                1,
                observed.isoformat(),
                observed.isoformat(),
                None,
                None,
                None,
                1,
            ),
            (
                "2026-08-24T14:59:00+08:00",
                CollectionSlotStatus.EXPECTED.value,
                0,
                None,
                None,
                None,
                None,
                None,
                0,
            ),
        )
        with store._connection:
            for row in legacy:
                store._connection.execute(
                    """
                    INSERT INTO market_watch_collection_slots (
                        trade_date, minute_bucket, config_version, status,
                        attempt_count, first_attempt_at, last_attempt_at,
                        accepted_at, source_snapshot_id, source_snapshot_revision,
                        gap_heartbeat, ledger_revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade_date.isoformat(),
                        row[0],
                        store.config_version,
                        row[1],
                        *row[2:],
                        revision,
                        observed.isoformat(),
                        observed.isoformat(),
                    ),
                )

        assert store.ensure_expected_slots(trade_date) == 0
        completeness = store.read_completeness(trade_date, as_of=observed)
        excluded = tuple(
            store.get_slot(datetime.fromisoformat(row[0])) for row in legacy
        )
        row_count = store._connection.execute(
            "SELECT COUNT(*) FROM market_watch_collection_slots"
        ).fetchone()[0]

    assert row_count == 242
    assert all(item is not None for item in excluded)
    assert all(item.status is CollectionSlotStatus.EXCLUDED for item in excluded)
    assert excluded[0].source_snapshot_id == "mw-legacy-1457"
    assert completeness.expected_minute_buckets == 239
    assert completeness.accepted_real == 0
    assert completeness.pending == 239
    assert completeness.gap_heartbeat == 0


def test_post_close_recovery_is_idempotent_audited_and_manual_retryable(
    tmp_path: Path,
) -> None:
    trade_date = date(2026, 8, 24)
    closed_at = datetime(2026, 8, 24, 15, 6, tzinfo=SHANGHAI)
    db_path = tmp_path / "daily-recovery.sqlite3"

    with MarketWatchCollectionStore(db_path, clock=lambda: closed_at) as store:
        first = store.request_daily_recovery(
            trade_date,
            trigger=DailyRecoveryTrigger.AUTOMATIC,
            requested_at=closed_at,
        )
        duplicate = store.request_daily_recovery(
            trade_date,
            trigger=DailyRecoveryTrigger.AUTOMATIC,
            requested_at=closed_at + timedelta(seconds=1),
        )
        claimed = store.claim_daily_recovery(started_at=closed_at)

        assert first["action"] == "queued"
        assert duplicate["action"] == "existing"
        assert claimed is not None
        assert claimed.status is DailyRecoveryStatus.RUNNING
        assert store.requeue_daily_recovery_gaps(
            trade_date,
            requested_at=closed_at,
        ) == 239

        attempt_id, slot = store.claim_due(closed_at, trade_date=trade_date)
        store.mark_failed(
            attempt_id,
            error=TimeoutError("historical source unavailable"),
            completed_at=closed_at + timedelta(seconds=2),
            next_retry_at=None,
        )
        finished = store.finish_daily_recovery(
            claimed.run_id,
            completed_at=closed_at + timedelta(seconds=3),
            reconciled_slots=0,
            attempted_slots=1,
        )
        manual = store.request_daily_recovery(
            trade_date,
            trigger=DailyRecoveryTrigger.MANUAL,
            requested_at=closed_at + timedelta(seconds=4),
        )

    assert slot.minute_bucket == expected_session_minutes(trade_date)[0]
    assert finished.status is DailyRecoveryStatus.RETRYING
    assert finished.remaining_gaps == 239
    assert finished.manual_action_required is False
    assert finished.next_retry_at == closed_at + timedelta(seconds=3)
    assert manual["action"] == "queued"
    assert manual["recovery"].run_id != finished.run_id


@pytest.mark.parametrize("mixed_gap", [False, True])
def test_unpublished_gaps_remain_visible_without_requeueing(tmp_path, monkeypatch, mixed_gap):
    from tradex.market_watch import collection_store
    from tradex.market_watch.reconstruction import HistoricalTrajectoryNotPublished

    closed_at = datetime(2026, 8, 24, 16, 0, tzinfo=SHANGHAI)
    minutes = [closed_at.replace(hour=9, minute=30)]
    if mixed_gap:
        minutes.append(minutes[0] + timedelta(minutes=1))
    monkeypatch.setattr(collection_store, "expected_session_minutes", lambda _day: tuple(minutes))
    path = tmp_path / "unpublished.sqlite3"
    with MarketWatchCollectionStore(path, clock=lambda: closed_at) as store:
        store.request_daily_recovery(closed_at.date(), trigger=DailyRecoveryTrigger.MANUAL, requested_at=closed_at)
        run = store.claim_daily_recovery(started_at=closed_at)
        for index, minute in enumerate(minutes):
            attempt_id, slot = store.claim_due(closed_at, trade_date=closed_at.date())
            assert slot.minute_bucket == minute
            store.record_daily_recovery_attempt_started(run.run_id, slot, started_at=closed_at)
            failed = store.mark_failed(
                attempt_id,
                error=TimeoutError("retryable source failure") if index else HistoricalTrajectoryNotPublished("09:30"),
                completed_at=closed_at,
                next_retry_at=None,
            )
            store.record_daily_recovery_attempt_finished(run.run_id, failed, completed_at=closed_at)
        finished = store.finish_daily_recovery(run.run_id, completed_at=closed_at, reconciled_slots=0, attempted_slots=len(minutes))
        assert finished.remaining_gaps == len(minutes)
        assert finished.unavailable_gaps == 1
        assert finished.accepted_after == 0
        assert finished.manual_action_required is mixed_gap
        with MarketWatchCollectionStore(path, read_only=True) as reader:
            assert reader.read_latest_daily_recovery(closed_at.date(), as_of=closed_at) == finished
        request = store.request_daily_recovery(closed_at.date(), trigger=DailyRecoveryTrigger.MANUAL, requested_at=closed_at)
        assert request["action"] == ("queued" if mixed_gap else "existing")
        if not mixed_gap:
            assert request["recovery"].run_id == run.run_id
        assert store.requeue_daily_recovery_gaps(closed_at.date(), requested_at=closed_at) == int(mixed_gap)
        claimed = store.claim_due(closed_at, trade_date=closed_at.date())
        if mixed_gap:
            assert claimed[1].minute_bucket == minutes[1]
        else:
            assert claimed is None


def test_running_post_close_recovery_projects_live_attempt_progress_and_error(
    tmp_path: Path,
) -> None:
    trade_date = date(2026, 8, 24)
    closed_at = datetime(2026, 8, 24, 15, 6, tzinfo=SHANGHAI)

    with MarketWatchCollectionStore(tmp_path / "recovery-progress.sqlite3") as store:
        store.request_daily_recovery(
            trade_date,
            trigger=DailyRecoveryTrigger.MANUAL,
            requested_at=closed_at,
        )
        claimed = store.claim_daily_recovery(started_at=closed_at)
        assert claimed is not None
        store.requeue_daily_recovery_gaps(trade_date, requested_at=closed_at)

        attempt_id, slot = store.claim_due(closed_at, trade_date=trade_date)
        store.record_daily_recovery_attempt_started(
            claimed.run_id,
            slot,
            started_at=closed_at,
        )
        store.record_daily_recovery_attempt_progress(
            claimed.run_id,
            slot,
            completed=7,
            total=139,
            stage="stock_minutes",
            message="已加载 280/5564 只",
            observed_at=closed_at + timedelta(milliseconds=500),
        )
        capturing = store.read_latest_daily_recovery(
            trade_date,
            as_of=closed_at + timedelta(seconds=1),
        )
        failed_slot = store.mark_failed(
            attempt_id,
            error=TimeoutError("exact historical snapshot timed out"),
            completed_at=closed_at + timedelta(seconds=2),
            next_retry_at=closed_at + timedelta(minutes=30),
        )
        store.record_daily_recovery_attempt_finished(
            claimed.run_id,
            failed_slot,
            completed_at=closed_at + timedelta(seconds=2),
        )
        failed_attempt = store.read_latest_daily_recovery(
            trade_date,
            as_of=closed_at + timedelta(seconds=3),
        )

    assert capturing is not None
    assert capturing.status is DailyRecoveryStatus.RUNNING
    assert capturing.attempted_slots == 1
    assert capturing.failed_attempts == 0
    assert capturing.latest_attempt_minute_bucket == slot.minute_bucket
    assert capturing.latest_attempt_outcome == "started"
    assert capturing.latest_attempt_progress_completed == 7
    assert capturing.latest_attempt_progress_total == 139
    assert capturing.latest_attempt_progress_stage == "stock_minutes"
    assert capturing.latest_attempt_progress_message == "已加载 280/5564 只"
    assert capturing.latest_failure_error_code is None
    assert failed_attempt is not None
    assert failed_attempt.status is DailyRecoveryStatus.RUNNING
    assert failed_attempt.attempted_slots == 1
    assert failed_attempt.failed_attempts == 1
    assert failed_attempt.latest_attempt_minute_bucket == slot.minute_bucket
    assert failed_attempt.latest_attempt_outcome == "retrying"
    assert failed_attempt.latest_failure_error_code == "TimeoutError"
    assert (
        failed_attempt.latest_failure_error_message
        == "exact historical snapshot timed out"
    )


@pytest.mark.parametrize("ledger_complete", [False, True])
def test_failed_post_close_recovery_preserves_public_error_detail(tmp_path: Path, monkeypatch, ledger_complete) -> None:
    trade_date = date(2026, 8, 24)
    closed_at = datetime(2026, 8, 24, 15, 6, tzinfo=SHANGHAI)

    with MarketWatchCollectionStore(tmp_path / "recovery-failure.sqlite3") as store:
        store.request_daily_recovery(
            trade_date,
            trigger=DailyRecoveryTrigger.MANUAL,
            requested_at=closed_at,
        )
        claimed = store.claim_daily_recovery(started_at=closed_at)
        assert claimed is not None
        if ledger_complete:
            completeness = store._read_completeness_locked(trade_date, closed_at)
            complete = completeness.model_copy(update={
                "accepted_real": completeness.expected_minute_buckets,
                "pending": 0, "retrying": 0, "unresolved": 0,
            })
            monkeypatch.setattr(store, "_read_completeness_locked", lambda *args: complete)
        failed = store.finish_daily_recovery(
            claimed.run_id,
            completed_at=closed_at + timedelta(seconds=1),
            reconciled_slots=0,
            attempted_slots=0,
            error=RuntimeError("history index could not be read"),
        )

    assert failed.status is DailyRecoveryStatus.FAILED
    assert failed.last_error_code == "RuntimeError"
    assert failed.last_error_message == "history index could not be read"


def test_post_close_recovery_cannot_be_queued_before_close(tmp_path: Path) -> None:
    before_close = datetime(2026, 8, 24, 14, 59, tzinfo=SHANGHAI)
    with MarketWatchCollectionStore(
        tmp_path / "early-recovery.sqlite3",
        clock=lambda: before_close,
    ) as store:
        with pytest.raises(ValueError, match="after the market closes"):
            store.request_daily_recovery(
                before_close.date(),
                trigger=DailyRecoveryTrigger.MANUAL,
                requested_at=before_close,
            )


def test_automatic_recovery_survives_prepare_failure_restart_and_mixed_gaps(tmp_path, monkeypatch):
    from tradex.market_watch import collection_store
    from tradex.market_watch.reconstruction import HistoricalTrajectoryNotPublished

    start = datetime(2026, 9, 18, 15, 6, tzinfo=SHANGHAI)
    minutes = tuple(start.replace(hour=10, minute=value) for value in (10, 11, 12))
    monkeypatch.setattr(collection_store, "expected_session_minutes", lambda day: minutes)
    current = [start]
    repairs, preparations = [], []
    path = tmp_path / "automatic-lifecycle.sqlite3"

    def prepare(*args):
        preparations.append(current[0])
        if len(preparations) == 1:
            raise TimeoutError("fixture preparation deadline")

    def repair(slot):
        repairs.append(slot.minute_bucket)
        if slot.minute_bucket == minutes[0]:
            raise HistoricalTrajectoryNotPublished("fixture terminal minute")
        if slot.minute_bucket == minutes[1] and repairs.count(minutes[1]) == 1:
            raise TimeoutError("fixture provider deadline")
        return _snapshot(slot.minute_bucket, snapshot_id=f"recovered-{slot.minute_bucket:%H%M}")

    def cycle(ledger, history):
        collector = MarketWatchCollector(store=ledger, capture_current=lambda slot: pytest.fail("closed session"), repair_historical=repair, prepare_daily_recovery=prepare, persist_snapshot=history.record, clock=lambda: current[0])
        collector.start()
        collector._queue_automatic_recovery_if_due(current[0])
        return collector.run_once()

    with MarketWatchCollectionStore(path) as ledger, MarketWatchHistoryStore(path) as history:
        assert cycle(ledger, history)["status"] == "failed"
        failed = ledger.read_latest_daily_recovery(start.date(), as_of=current[0])
        assert failed.next_retry_at == start + timedelta(seconds=60)
        assert failed.manual_action_required is False
        current[0] += timedelta(seconds=59)
        assert ledger.request_daily_recovery(start.date(), trigger=DailyRecoveryTrigger.AUTOMATIC, requested_at=current[0])["action"] == "existing"

    # A new persistence owner simulates process restart; no manual queue request.
    current[0] += timedelta(seconds=1)
    with MarketWatchCollectionStore(path) as ledger, MarketWatchHistoryStore(path) as history:
        assert cycle(ledger, history)["status"] == "retrying"
        mixed = ledger.read_latest_daily_recovery(start.date(), as_of=current[0])
        assert mixed.unavailable_gaps == 1 and mixed.remaining_gaps == 2
        assert mixed.manual_action_required is False
        current[0] += timedelta(seconds=10)
        assert cycle(ledger, history)["status"] == "needs_attention"
        final = ledger.read_latest_daily_recovery(start.date(), as_of=current[0])
        assert final.accepted_after == 2 and final.remaining_gaps == final.unavailable_gaps == 1
        assert final.next_retry_at is None and final.manual_action_required is False
        assert final.failed_attempts == 0
        assert repairs.count(minutes[0]) == 1
        assert repairs.count(minutes[1]) == 2
        assert repairs.count(minutes[2]) == 1
        current[0] += timedelta(minutes=10)
        assert ledger.request_daily_recovery(start.date(), trigger=DailyRecoveryTrigger.AUTOMATIC, requested_at=current[0])["action"] == "existing"


@pytest.mark.parametrize("preserve", [False, True])
def test_daily_recovery_can_preserve_later_slot_backoff(tmp_path, preserve):
    now = datetime(2026, 9, 18, 16, tzinfo=SHANGHAI)
    with MarketWatchCollectionStore(tmp_path / "backoff.sqlite3") as store:
        attempt, slot = store.claim_due(now)
        retry_at = now + timedelta(minutes=10)
        store.mark_failed(attempt, error=TimeoutError("fixture"), completed_at=now, next_retry_at=retry_at)
        store.requeue_daily_recovery_gaps(now.date(), requested_at=now, preserve_retry_schedule=preserve)
        assert store.get_slot(slot.minute_bucket).next_retry_at == (retry_at if preserve else now)


def test_legacy_needs_attention_does_not_block_retryable_slots(tmp_path):
    now = datetime(2026, 9, 18, 16, tzinfo=SHANGHAI)
    with MarketWatchCollectionStore(tmp_path / "legacy.sqlite3") as store:
        queued = store.request_daily_recovery(now.date(), trigger=DailyRecoveryTrigger.AUTOMATIC, requested_at=now)
        store.claim_daily_recovery(started_at=now)
        store.finish_daily_recovery(queued["recovery"].run_id, completed_at=now, reconciled_slots=0, attempted_slots=0)
        store._connection.execute("UPDATE market_watch_daily_recovery_runs SET status='needs_attention'")
        store._connection.commit()
        result = store.request_daily_recovery(now.date(), trigger=DailyRecoveryTrigger.AUTOMATIC, requested_at=now + timedelta(seconds=1))
        assert result["action"] == "queued"


def test_repaired_failure_is_removed_from_active_error_projection(tmp_path):
    now = datetime(2026, 9, 18, 16, tzinfo=SHANGHAI)
    with MarketWatchCollectionStore(tmp_path / "resolved.sqlite3") as store:
        queued = store.request_daily_recovery(now.date(), trigger=DailyRecoveryTrigger.AUTOMATIC, requested_at=now)
        run = store.claim_daily_recovery(started_at=now)
        attempt, slot = store.claim_due(now)
        store.record_daily_recovery_attempt_started(run.run_id, slot, started_at=now)
        failed = store.mark_failed(attempt, error=TimeoutError("fixture"), completed_at=now, next_retry_at=now)
        store.record_daily_recovery_attempt_finished(run.run_id, failed, completed_at=now)
        assert store.read_latest_daily_recovery(now.date(), as_of=now).latest_failure_error_code == "TimeoutError"
        attempt, slot = store.claim_due(now)
        store.mark_accepted(attempt, _snapshot(slot.minute_bucket, snapshot_id="repaired"), source_snapshot_revision="a" * 64, completed_at=now)
        projected = store.read_latest_daily_recovery(now.date(), as_of=now)
        assert projected.latest_failure_error_code is None
        assert projected.latest_failure_next_retry_at is None
        assert store._connection.execute("SELECT COUNT(*) FROM market_watch_collection_attempts WHERE error_code='TimeoutError'").fetchone()[0] == 1


@pytest.mark.parametrize("prestarted", [False, True])
def test_collector_runs_one_automatic_post_close_recovery_batch(
    tmp_path: Path, prestarted: bool,
) -> None:
    closed_at = datetime(2026, 8, 24, 15, 6, tzinfo=SHANGHAI)
    db_path = tmp_path / "automatic-recovery.sqlite3"
    repair_calls: list[datetime] = []
    preparation_calls: list[date] = []
    reconciliation_passes = []
    stop_event = threading.Event()

    def repair(slot):
        repair_calls.append(slot.minute_bucket)
        stop_event.set()
        return _snapshot(slot.minute_bucket, snapshot_id="mw-post-close-repair")

    def repair_with_progress(slot, progress):
        progress(7, 139, "stock_minutes", "已加载 280/5564 只")
        return repair(slot)

    with (
        MarketWatchCollectionStore(db_path, clock=lambda: closed_at) as ledger,
        MarketWatchHistoryStore(db_path, clock=lambda: closed_at) as history,
    ):
        collector = MarketWatchCollector(
            store=ledger,
            capture_current=lambda _slot: pytest.fail(
                "post-close recovery must use the historical seam"
            ),
            repair_historical=repair,
            repair_historical_with_progress=repair_with_progress,
            persist_snapshot=history.record,
            history_records=lambda: reconciliation_passes.append(1) or (),
            prepare_daily_recovery=lambda trade_date, _observed, heartbeat: (
                heartbeat(),
                preparation_calls.append(trade_date),
            ),
            clock=lambda: closed_at,
            recovery_batch_limit=1,
        )

        if prestarted:
            collector.start()
        collector.run_forever(stop_event)
        envelope = ledger.read_envelope(as_of=closed_at)

    assert repair_calls == [expected_session_minutes(closed_at.date())[0]]
    assert preparation_calls == [closed_at.date()]
    assert len(reconciliation_passes) == 2  # Startup once, then daily recovery once.
    assert envelope.daily_recovery is not None
    assert envelope.daily_recovery.trigger is DailyRecoveryTrigger.AUTOMATIC
    assert envelope.daily_recovery.status is DailyRecoveryStatus.RETRYING
    assert envelope.daily_recovery.attempted_slots == 1
    assert envelope.daily_recovery.latest_attempt_progress_completed == 7
    assert envelope.daily_recovery.latest_attempt_progress_total == 139
    assert envelope.daily_recovery.latest_attempt_progress_stage == "stock_minutes"
    assert envelope.daily_recovery.remaining_gaps == 238
    assert envelope.daily_recovery.manual_action_required is False
    assert envelope.collection_completeness.repaired == 1


def test_daily_recovery_attempts_each_gap_at_most_once_per_run(tmp_path):
    db_path = tmp_path / "single-attempt-per-run.sqlite3"
    observed = datetime(2026, 8, 24, 16, 0, tzinfo=SHANGHAI)
    clock_ticks = 0
    repaired_minutes = []

    def clock():
        nonlocal clock_ticks
        clock_ticks += 1
        return observed + timedelta(seconds=clock_ticks * 20)

    def repair(slot):
        repaired_minutes.append(slot.minute_bucket)
        if len(repaired_minutes) == 1:
            raise TimeoutError("first gap remains retryable")
        return _snapshot(slot.minute_bucket, snapshot_id="mw-second-gap")

    with (
        MarketWatchCollectionStore(db_path, clock=clock) as ledger,
        MarketWatchHistoryStore(db_path, clock=clock) as history,
    ):
        collector = MarketWatchCollector(
            store=ledger,
            capture_current=lambda _slot: pytest.fail("closed recovery is historical"),
            repair_historical=repair,
            persist_snapshot=history.record,
            history_records=lambda: (),
            clock=clock,
            recovery_batch_limit=2,
        )
        ledger.request_daily_recovery(
            observed.date(),
            trigger=DailyRecoveryTrigger.MANUAL,
            requested_at=observed,
        )
        recovery = ledger.claim_daily_recovery(started_at=observed)
        assert recovery is not None

        result = collector._run_daily_recovery(recovery, observed)

    assert result["attempted_slots"] == 2
    assert len(set(repaired_minutes)) == 2


def test_read_envelope_is_zero_write_and_never_initializes_collection_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "read-only-ledger.sqlite3"
    observed = datetime(2026, 8, 24, 10, 30, 5, tzinfo=SHANGHAI)
    with MarketWatchCollectionStore(db_path, clock=lambda: observed) as owner:
        assert owner.ensure_expected_slots(observed.date()) == 239

    before = _ledger_fingerprint(db_path)
    with MarketWatchCollectionStore(db_path, read_only=True) as reader:
        monkeypatch.setattr(
            reader,
            "ensure_expected_slots",
            lambda *_args, **_kwargs: pytest.fail(
                "read_envelope must not initialize collection slots"
            ),
        )
        envelope = reader.read_envelope(as_of=observed)
    after = _ledger_fingerprint(db_path)

    assert before == after
    assert before[1] == 239
    assert envelope.collection_completeness.expected_minute_buckets == 239
    assert envelope.latest_accepted_real is None

    missing_path = tmp_path / "missing-ledger.sqlite3"
    with MarketWatchCollectionStore(missing_path, read_only=True) as reader:
        missing = reader.read_envelope(as_of=observed)

    assert missing.collection_completeness.expected_minute_buckets == 0
    assert missing.latest_accepted_real is None
    assert missing.collection_cursor is None
    assert not missing_path.exists()
    with pytest.raises(ValueError, match="outside the verified"):
        expected_session_minutes(date(2027, 1, 4))


def test_read_only_ledger_lazily_recovers_after_collector_creates_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "late-ledger.sqlite3"
    observed = datetime(2026, 8, 24, 10, 30, 5, tzinfo=SHANGHAI)
    reader = MarketWatchCollectionStore(db_path, read_only=True)
    try:
        assert reader.read_envelope(
            as_of=observed
        ).collection_completeness.expected_minute_buckets == 0
        assert not db_path.exists()

        with sqlite3.connect(db_path) as partial:
            partial.execute("CREATE TABLE collector_bootstrap_in_progress (id INTEGER)")
        assert reader.read_envelope(
            as_of=observed
        ).collection_completeness.expected_minute_buckets == 0
        assert reader._connection is None

        with MarketWatchCollectionStore(db_path, clock=lambda: observed) as owner:
            assert owner.ensure_expected_slots(observed.date()) == 239
            owner.update_runtime(
                CollectorRuntimeState.RUNNING,
                heartbeat_at=observed,
            )
        before = _ledger_fingerprint(db_path)

        real_connect = sqlite3.connect
        connect_count = 0
        count_lock = threading.Lock()

        def counting_connect(*args, **kwargs):
            nonlocal connect_count
            if kwargs.get("uri") is True:
                with count_lock:
                    connect_count += 1
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", counting_connect)
        with ThreadPoolExecutor(max_workers=8) as executor:
            envelopes = list(
                executor.map(
                    lambda _index: reader.read_envelope(as_of=observed),
                    range(16),
                )
            )

        assert connect_count == 1
        assert all(
            item.collector_state is CollectorRuntimeState.RUNNING
            and item.collection_completeness.expected_minute_buckets == 239
            for item in envelopes
        )
        assert _ledger_fingerprint(db_path) == before
    finally:
        reader.close()


def test_completeness_reader_uses_one_sqlite_snapshot_during_concurrent_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "consistent-read.sqlite3"
    observed = datetime(2026, 8, 24, 10, 30, 5, tzinfo=SHANGHAI)
    writer = MarketWatchCollectionStore(db_path, clock=lambda: observed)
    reader = MarketWatchCollectionStore(db_path, read_only=True)
    try:
        attempt_id, _slot = writer.claim_due(observed)
        before_revision = writer.read_completeness(
            observed.date(), as_of=observed
        ).ledger_revision
        original_meta = reader._meta_locked
        committed = False

        def commit_between_reader_selects(key: str) -> str:
            nonlocal committed
            if key == "ledger_revision" and not committed:
                committed = True
                writer.mark_failed(
                    attempt_id,
                    error=TimeoutError("interleaved failure"),
                    completed_at=observed + timedelta(seconds=1),
                    next_retry_at=observed + timedelta(seconds=30),
                )
            return original_meta(key)

        monkeypatch.setattr(reader, "_meta_locked", commit_between_reader_selects)
        consistent = reader.read_completeness(observed.date(), as_of=observed)
        current = writer.read_completeness(
            observed.date(), as_of=observed + timedelta(seconds=2)
        )
    finally:
        reader.close()
        writer.close()

    assert committed is True
    assert consistent.ledger_revision == before_revision
    assert consistent.pending == 239
    assert consistent.retrying == 0
    assert consistent.gap_heartbeat == 0
    assert current.ledger_revision > consistent.ledger_revision
    assert current.pending == 238
    assert current.retrying == 1
    assert current.gap_heartbeat == 1


def test_current_minute_is_claimed_before_older_repair_work(tmp_path: Path) -> None:
    now = datetime(2026, 8, 24, 10, 30, 5, tzinfo=SHANGHAI)
    with MarketWatchCollectionStore(tmp_path / "priority.sqlite3") as store:
        assert store.ensure_expected_slots(now.date()) == 239
        claimed = store.claim_due(now)

    assert claimed is not None
    _attempt_id, slot = claimed
    assert slot.minute_bucket.isoformat() == "2026-08-24T10:30:00+08:00"
    assert slot.status is CollectionSlotStatus.CAPTURING
    assert slot.attempt_count == 1


def test_prior_day_gap_requires_an_explicit_daily_recovery_scope(
    tmp_path: Path,
) -> None:
    prior = date(2026, 8, 26)
    preopen = datetime(2026, 8, 27, 0, 30, tzinfo=SHANGHAI)
    with MarketWatchCollectionStore(tmp_path / "prior-day-scope.sqlite3") as store:
        assert store.ensure_expected_slots(prior) == 239

        assert store.claim_due(preopen) is None
        claimed = store.claim_due(preopen, trade_date=prior)

    assert claimed is not None
    _attempt_id, slot = claimed
    assert slot.trade_date == prior
    assert slot.minute_bucket == expected_session_minutes(prior)[0]


def test_retry_state_and_attempt_audit_survive_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "restart.sqlite3"
    started = datetime(2026, 8, 24, 10, 30, 5, tzinfo=SHANGHAI)
    retry_at = started + timedelta(minutes=1)

    with MarketWatchCollectionStore(db_path) as store:
        attempt_id, _slot = store.claim_due(started)
        failed = store.mark_failed(
            attempt_id,
            error=TimeoutError("provider deadline"),
            completed_at=started + timedelta(seconds=10),
            next_retry_at=retry_at,
        )
        assert failed.status is CollectionSlotStatus.RETRYING
        assert failed.gap_heartbeat is True

    with MarketWatchCollectionStore(db_path) as reopened:
        restored = reopened.get_slot(started)
        attempts = reopened.list_attempts(started)

    assert restored is not None
    assert restored.status is CollectionSlotStatus.RETRYING
    assert restored.gap_heartbeat is True
    assert restored.next_retry_at == retry_at
    assert restored.last_error_code == "TimeoutError"
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "retrying"


def test_claim_due_can_skip_a_minute_already_attempted_in_this_recovery(tmp_path):
    now = datetime(2026, 8, 24, 16, 0, tzinfo=SHANGHAI)
    trade_date = now.date()
    with MarketWatchCollectionStore(tmp_path / "claim-exclusion.sqlite3") as store:
        first_attempt, first = store.claim_due(now, trade_date=trade_date)
        store.mark_failed(
            first_attempt,
            error=TimeoutError("retry later"),
            completed_at=now,
            next_retry_at=now,
        )

        _second_attempt, second = store.claim_due(
            now,
            trade_date=trade_date,
            exclude_minutes=(first.minute_bucket,),
        )

    assert second.minute_bucket != first.minute_bucket


def test_inflight_attempt_is_recovered_as_retryable_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "inflight.sqlite3"
    started = datetime(2026, 8, 24, 10, 30, 5, tzinfo=SHANGHAI)
    recovered_at = started + timedelta(seconds=20)

    with MarketWatchCollectionStore(db_path) as first:
        first.claim_due(started)

    with MarketWatchCollectionStore(db_path) as second:
        assert second.recover_inflight(recovered_at) == 1
        slot = second.get_slot(started)
        attempts = second.list_attempts(started)

    assert slot is not None
    assert slot.status is CollectionSlotStatus.RETRYING
    assert slot.gap_heartbeat is True
    assert slot.next_retry_at == recovered_at
    assert slot.last_error_code == "CollectorRestarted"
    assert attempts[0]["outcome"] == "interrupted"


@pytest.mark.parametrize("trigger", list(DailyRecoveryTrigger))
def test_restart_requeues_the_interrupted_daily_recovery_run(tmp_path: Path, trigger) -> None:
    started_at = datetime(2026, 8, 24, 15, 6, tzinfo=SHANGHAI)
    restarted_at = started_at + timedelta(minutes=4)
    db_path = tmp_path / "interrupted-daily-recovery.sqlite3"
    with MarketWatchCollectionStore(db_path, clock=lambda: started_at) as first:
        first.request_daily_recovery(
            started_at.date(),
            trigger=trigger,
            requested_at=started_at,
        )
        claimed = first.claim_daily_recovery(started_at=started_at)
        assert claimed is not None
        assert claimed.status is DailyRecoveryStatus.RUNNING

    with MarketWatchCollectionStore(db_path, clock=lambda: restarted_at) as second:
        assert second.recover_inflight(restarted_at) == 1
        recovered = second.read_latest_daily_recovery(
            started_at.date(),
            as_of=restarted_at,
        )
        continued = second.request_daily_recovery(
            started_at.date(), trigger=DailyRecoveryTrigger.AUTOMATIC,
            requested_at=restarted_at,
        )
        assert continued["action"] == "queued"
        assert continued["recovery"].run_id != recovered.run_id

    assert recovered is not None
    assert recovered.status is DailyRecoveryStatus.RETRYING
    assert recovered.completed_at == restarted_at
    assert recovered.last_error_code is None


def test_real_acceptance_and_repair_are_exact_and_heartbeat_never_wins(
    tmp_path: Path,
) -> None:
    first_minute = datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI)
    second_minute = first_minute + timedelta(minutes=1)
    with MarketWatchCollectionStore(tmp_path / "repair.sqlite3") as store:
        first_attempt, _ = store.claim_due(first_minute)
        first = store.mark_accepted(
            first_attempt,
            _snapshot(first_minute, snapshot_id="mw-first"),
            source_snapshot_revision="a" * 64,
            completed_at=first_minute + timedelta(seconds=20),
        )
        preserved = store.record_gap_heartbeat(
            first_minute,
            recorded_at=first_minute + timedelta(seconds=30),
        )
        missing = store.record_gap_heartbeat(
            second_minute,
            recorded_at=second_minute + timedelta(seconds=10),
        )
        second_attempt, _ = store.claim_due(second_minute + timedelta(seconds=15))
        repaired = store.mark_accepted(
            second_attempt,
            _snapshot(second_minute, snapshot_id="mw-repaired", sequence=2),
            source_snapshot_revision="b" * 64,
            completed_at=second_minute + timedelta(seconds=25),
        )
        store.update_runtime(
            CollectorRuntimeState.RUNNING,
            heartbeat_at=second_minute + timedelta(seconds=30),
        )
        envelope = store.get_envelope(as_of=second_minute + timedelta(seconds=30))

    assert first.status is CollectionSlotStatus.ACCEPTED_REAL
    assert preserved.status is CollectionSlotStatus.ACCEPTED_REAL
    assert preserved.gap_heartbeat is False
    assert missing.status is CollectionSlotStatus.RETRYING
    assert missing.gap_heartbeat is True
    assert repaired.status is CollectionSlotStatus.REPAIRED
    assert repaired.gap_heartbeat is False
    assert envelope.collector_state is CollectorRuntimeState.RUNNING
    assert envelope.latest_accepted_real is not None
    assert envelope.latest_accepted_real.snapshot_id == "mw-repaired"
    assert envelope.latest_accepted_real.repaired is True
    assert envelope.collection_completeness.expected_minute_buckets == 239
    assert envelope.collection_completeness.accepted_real == 2
    assert envelope.collection_completeness.repaired == 1
    assert envelope.collection_completeness.gap_heartbeat == 0
    assert len(envelope.collection_completeness.ledger_digest) == 64
    assert envelope.collection_completeness.pending == 237
    assert envelope.collection_completeness.retrying == 0
    assert envelope.collection_completeness.unresolved == 0


def test_stale_or_wrong_minute_snapshot_cannot_be_published_as_real(tmp_path: Path) -> None:
    target = datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI)
    with MarketWatchCollectionStore(tmp_path / "reject.sqlite3") as store:
        attempt_id, _ = store.claim_due(target)
        wrong_minute = _snapshot(
            target + timedelta(minutes=1),
            snapshot_id="mw-wrong-minute",
        )
        with pytest.raises(ValueError, match="exactly match"):
            store.mark_accepted(
                attempt_id,
                wrong_minute,
                source_snapshot_revision="c" * 64,
                completed_at=target + timedelta(seconds=10),
            )

        anchor = _snapshot(
            target - timedelta(minutes=1),
            snapshot_id="mw-anchor",
        )
        stale = build_collection_gap_snapshots(
            anchor,
            started_at=target - timedelta(minutes=1),
            completed_at=target,
        )[0]
        with pytest.raises(ValueError, match="cannot be accepted"):
            store.mark_accepted(
                attempt_id,
                stale,
                source_snapshot_revision="d" * 64,
                completed_at=target + timedelta(seconds=20),
            )

        slot = store.get_slot(target)

    assert slot is not None
    assert slot.status is CollectionSlotStatus.CAPTURING
    assert slot.source_snapshot_revision is None


def test_collector_retries_current_but_defers_older_gap_until_close(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "collector.sqlite3"
    clock = [datetime(2026, 8, 24, 10, 30, 5, tzinfo=SHANGHAI)]
    current_calls: list[datetime] = []
    repair_calls: list[datetime] = []

    def capture_current(slot):
        current_calls.append(slot.minute_bucket)
        if len(current_calls) == 1:
            raise TimeoutError("first current attempt failed")
        return _snapshot(slot.minute_bucket, snapshot_id=f"mw-current-{len(current_calls)}")

    def repair_historical(slot):
        repair_calls.append(slot.minute_bucket)
        return _snapshot(slot.minute_bucket, snapshot_id="mw-historical")

    with (
        MarketWatchCollectionStore(db_path, clock=lambda: clock[0]) as ledger,
        MarketWatchHistoryStore(db_path, clock=lambda: clock[0]) as history,
    ):
        collector = MarketWatchCollector(
            store=ledger,
            capture_current=capture_current,
            repair_historical=repair_historical,
            persist_snapshot=history.record,
            clock=lambda: clock[0],
            retry_policy=CollectorRetryPolicy(
                retry_delays_seconds=(10.0, 20.0),
                repair_interval_seconds=1.0,
                idle_poll_seconds=0.1,
            ),
        )

        first = collector.run_once()
        clock[0] += timedelta(seconds=5)
        guarded = collector.run_once()
        clock[0] += timedelta(seconds=5)
        retried = collector.run_once()
        live_idle = collector.run_once()
        clock[0] = datetime(2026, 8, 24, 15, 10, tzinfo=SHANGHAI)
        repaired_old = collector.run_once()

    assert first["action"] == "failed"
    assert first["current"] is True
    assert guarded == {"action": "idle", "reason": "no_due_slot"}
    assert retried["action"] == "accepted"
    assert retried["current"] is True
    assert retried["status"] == "repaired"
    assert live_idle == {"action": "idle", "reason": "no_due_slot"}
    assert repaired_old["action"] == "accepted"
    assert repaired_old["current"] is False
    assert repair_calls == [datetime(2026, 8, 24, 9, 25, tzinfo=SHANGHAI)]
    assert current_calls == [
        datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI),
        datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI),
    ]


def test_collector_continues_persisted_gap_reconciliation_after_close(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "closing-reconcile.sqlite3"
    closed_at = datetime(2026, 8, 24, 15, 10, tzinfo=SHANGHAI)
    repaired_minutes: list[datetime] = []

    def repair(slot):
        repaired_minutes.append(slot.minute_bucket)
        return _snapshot(slot.minute_bucket, snapshot_id="mw-closing-repair")

    with (
        MarketWatchCollectionStore(db_path, clock=lambda: closed_at) as ledger,
        MarketWatchHistoryStore(db_path, clock=lambda: closed_at) as history,
    ):
        collector = MarketWatchCollector(
            store=ledger,
            capture_current=lambda _slot: pytest.fail(
                "post-close reconciliation must not use current capture"
            ),
            repair_historical=repair,
            persist_snapshot=history.record,
            clock=lambda: closed_at,
        )

        result = collector.run_once()
        first_slot = ledger.get_slot(
            datetime(2026, 8, 24, 9, 25, tzinfo=SHANGHAI)
        )

    assert result["action"] == "accepted"
    assert result["current"] is False
    assert result["status"] == "repaired"
    assert repaired_minutes == [
        datetime(2026, 8, 24, 9, 25, tzinfo=SHANGHAI)
    ]
    assert first_slot is not None
    assert first_slot.source_snapshot_revision == result["source_snapshot_revision"]


def test_collector_never_accepts_history_rejection_or_wrong_minute(tmp_path: Path) -> None:
    target = datetime(2026, 8, 24, 10, 30, 5, tzinfo=SHANGHAI)
    modes = ["wrong_minute", "history_rejected"]

    def capture_current(slot):
        if modes[0] == "wrong_minute":
            return _snapshot(
                slot.minute_bucket + timedelta(minutes=1),
                snapshot_id="mw-wrong",
            )
        return _snapshot(slot.minute_bucket, snapshot_id="mw-valid")

    with MarketWatchCollectionStore(tmp_path / "collector-reject.sqlite3") as ledger:
        collector = MarketWatchCollector(
            store=ledger,
            capture_current=capture_current,
            repair_historical=capture_current,
            persist_snapshot=lambda _snapshot: {"action": "skipped"},
            clock=lambda: target,
            retry_policy=CollectorRetryPolicy(retry_delays_seconds=(1.0,)),
        )
        wrong = collector.run_once()
        modes[0] = "history_rejected"
        target = target + timedelta(seconds=1)
        rejected = collector.run_once(now=target)
        slot = ledger.get_slot(target)

    assert wrong["action"] == "failed"
    assert wrong["error_code"] == "ValueError"
    assert rejected["action"] == "failed"
    assert rejected["status"] == "retrying"
    assert rejected["error_code"] == "RuntimeError"
    assert slot is not None
    assert slot.status is CollectionSlotStatus.RETRYING
    assert slot.source_snapshot_id is None


def test_committed_history_is_immediately_reconciled_if_ledger_publish_fails_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "commit-reconcile.sqlite3"
    observed = datetime(2026, 8, 24, 10, 30, 5, tzinfo=SHANGHAI)
    with (
        MarketWatchCollectionStore(db_path, clock=lambda: observed) as ledger,
        MarketWatchHistoryStore(db_path, clock=lambda: observed) as history,
    ):
        original_mark_accepted = ledger.mark_accepted
        calls = 0

        def fail_first_publish(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient ledger publish failure")
            return original_mark_accepted(*args, **kwargs)

        monkeypatch.setattr(ledger, "mark_accepted", fail_first_publish)
        collector = MarketWatchCollector(
            store=ledger,
            capture_current=lambda slot: _snapshot(
                slot.minute_bucket,
                snapshot_id="mw-durable-before-ledger",
            ),
            repair_historical=lambda _slot: pytest.fail("not historical"),
            persist_snapshot=history.record,
            clock=lambda: observed,
        )

        result = collector.run_once()
        slot = ledger.get_slot(observed)
        attempts = ledger.list_attempts(observed)
        timeline = history.get_timeline(observed.date())

    assert result["action"] == "accepted"
    assert result["status"] == "repaired"
    assert result["ledger_reconciled"] is True
    assert slot is not None
    assert slot.status is CollectionSlotStatus.REPAIRED
    assert slot.gap_heartbeat is False
    assert slot.source_snapshot_revision == result["source_snapshot_revision"]
    assert attempts[0]["outcome"] == "reconciled_persisted"
    assert timeline[0]["payload"]["snapshot_id"] == "mw-durable-before-ledger"


def test_collector_retries_through_retention_then_marks_explicit_unresolved(
    tmp_path: Path,
) -> None:
    minute = datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI)
    clock = [minute + timedelta(seconds=2)]
    with MarketWatchCollectionStore(tmp_path / "retention.sqlite3") as ledger:
        collector = MarketWatchCollector(
            store=ledger,
            capture_current=lambda _slot: (_ for _ in ()).throw(
                TimeoutError("still unavailable")
            ),
            repair_historical=lambda _slot: (_ for _ in ()).throw(
                TimeoutError("still unavailable")
            ),
            persist_snapshot=lambda _snapshot: {},
            clock=lambda: clock[0],
            retry_policy=CollectorRetryPolicy(
                retry_delays_seconds=(1.0,),
                repair_retention_seconds=1.0,
            ),
        )

        result = collector.run_once()
        slot = ledger.get_slot(minute)

    assert result["action"] == "failed"
    assert result["status"] == "unresolved"
    assert result["next_retry_at"] is None
    assert slot is not None
    assert slot.status is CollectionSlotStatus.UNRESOLVED


def test_accepted_slot_contract_rejects_gap_heartbeat_overlap() -> None:
    minute = datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI)
    with pytest.raises(ValueError, match="cannot remain a gap heartbeat"):
        CollectionSlotV1(
            trade_date=minute.date(),
            minute_bucket=minute,
            status=CollectionSlotStatus.ACCEPTED_REAL,
            attempt_count=1,
            first_attempt_at=minute,
            last_attempt_at=minute,
            accepted_at=minute,
            source_snapshot_id="mw-real",
            source_snapshot_revision="a" * 64,
            gap_heartbeat=True,
            ledger_revision=1,
            updated_at=minute,
        )


def test_collector_reconciles_existing_real_history_without_refetching(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "history-reconcile.sqlite3"
    now = datetime(2026, 8, 24, 10, 31, 5, tzinfo=SHANGHAI)
    existing_minute = now.replace(minute=30, second=0)
    provider_calls: list[str] = []

    with MarketWatchHistoryStore(db_path, clock=lambda: now) as history:
        history.record(_snapshot(existing_minute, snapshot_id="mw-existing"))
        records = history.get_collection_records(now.date())

    with MarketWatchCollectionStore(db_path, clock=lambda: now) as ledger:
        collector = MarketWatchCollector(
            store=ledger,
            capture_current=lambda _slot: provider_calls.append("current"),
            repair_historical=lambda _slot: provider_calls.append("repair"),
            persist_snapshot=lambda _snapshot: {},
            history_records=lambda: records,
            clock=lambda: now,
        )
        assert collector.start() == 1
        restored = ledger.get_slot(existing_minute)
        envelope = ledger.get_envelope(as_of=now)

    assert provider_calls == []
    assert restored is not None
    assert restored.status is CollectionSlotStatus.ACCEPTED_REAL
    assert restored.attempt_count == 0
    assert restored.source_snapshot_id == "mw-existing"
    assert envelope.latest_accepted_real is not None
    assert envelope.latest_accepted_real.snapshot_id == "mw-existing"


def test_restart_recovers_history_commit_before_ledger_pointer_without_refetch(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "crash-window.sqlite3"
    now = datetime(2026, 8, 24, 10, 30, 5, tzinfo=SHANGHAI)
    minute = now.replace(second=0)
    provider_calls: list[str] = []

    with (
        MarketWatchCollectionStore(db_path, clock=lambda: now) as ledger,
        MarketWatchHistoryStore(db_path, clock=lambda: now) as history,
    ):
        ledger.claim_due(now)
        history.record(_snapshot(minute, snapshot_id="mw-crash-window"))

    with MarketWatchHistoryStore(db_path, read_only=True) as reader:
        records = reader.get_collection_records(now.date())

    with MarketWatchCollectionStore(db_path, clock=lambda: now) as reopened:
        collector = MarketWatchCollector(
            store=reopened,
            capture_current=lambda _slot: provider_calls.append("current"),
            repair_historical=lambda _slot: provider_calls.append("repair"),
            persist_snapshot=lambda _snapshot: {},
            history_records=lambda: records,
            clock=lambda: now,
        )
        assert collector.start() == 2
        restored = reopened.get_slot(minute)
        attempts = reopened.list_attempts(minute)

    assert provider_calls == []
    assert restored is not None
    assert restored.status is CollectionSlotStatus.REPAIRED
    assert restored.source_snapshot_id == "mw-crash-window"
    assert restored.gap_heartbeat is False
    assert attempts[0]["outcome"] == "reconciled_persisted"
