"""Single scheduler owner for market-watch capture, retry and publication."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from tradex.market_calendar import a_share_session

from .collection_contracts import (
    CollectionSlotStatus,
    CollectionSlotV1,
    CollectorRuntimeState,
    DailyCollectionRecoveryV1,
    DailyRecoveryTrigger,
    RetryableCollectionError,
    TerminalCollectionError,
)
from .collection_store import MarketWatchCollectionStore
from .session_schedule import EXPECTED_MARKET_WATCH_MINUTES, FINAL_CLOSE_TIME
from .contracts import MarketWatchSnapshotV1


SHANGHAI = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)
AUTOMATIC_RECOVERY_START = time(15, 5)


CaptureCallable = Callable[[CollectionSlotV1], MarketWatchSnapshotV1]
RepairProgressCallback = Callable[[int, int, str, str | None], None]
ProgressCaptureCallable = Callable[
    [CollectionSlotV1, RepairProgressCallback], MarketWatchSnapshotV1
]
PersistCallable = Callable[[MarketWatchSnapshotV1], Mapping[str, Any]]
HistoryRecordsCallable = Callable[[], Iterable[Mapping[str, Any]]]
RecoveryPreparationCallable = Callable[
    [date, datetime, Callable[[], None]], Mapping[str, Any] | None
]


@dataclass(frozen=True, slots=True)
class CollectorRetryPolicy:
    """Bounded retry schedule; exhausted attempts remain explicit unresolved."""

    retry_delays_seconds: tuple[float, ...] = (
        10.0,
        20.0,
        60.0,
        180.0,
        600.0,
        1800.0,
    )
    repair_retention_seconds: float = 7 * 24 * 60 * 60
    repair_interval_seconds: float = 5.0
    idle_poll_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not self.retry_delays_seconds:
            raise ValueError("retry_delays_seconds must not be empty")
        if any(value <= 0 for value in self.retry_delays_seconds):
            raise ValueError("retry delays must be positive")
        if self.repair_retention_seconds <= 0:
            raise ValueError("repair_retention_seconds must be positive")
        if self.repair_interval_seconds <= 0 or self.idle_poll_seconds <= 0:
            raise ValueError("collector intervals must be positive")

    def next_retry_at(
        self,
        *,
        attempt_count: int,
        completed_at: datetime,
        minute_bucket: datetime,
    ) -> datetime | None:
        index = attempt_count - 1
        if index < 0:
            raise ValueError("attempt_count must be positive")
        repair_deadline = minute_bucket + timedelta(
            seconds=self.repair_retention_seconds
        )
        if completed_at >= repair_deadline:
            return None
        delay = self.retry_delays_seconds[
            min(index, len(self.retry_delays_seconds) - 1)
        ]
        candidate = completed_at + timedelta(seconds=delay)
        return candidate if candidate <= repair_deadline else None


class MarketWatchCollector:
    """Own minute scheduling while delegating provider-neutral capture seams.

    ``capture_current`` and ``repair_historical`` must both return the strict
    canonical snapshot for the exact claimed minute.  Only this owner calls
    them.  Web readers consume persisted snapshots and never call either seam.
    """

    def __init__(
        self,
        *,
        store: MarketWatchCollectionStore,
        capture_current: CaptureCallable,
        repair_historical: CaptureCallable,
        repair_historical_with_progress: ProgressCaptureCallable | None = None,
        persist_snapshot: PersistCallable,
        history_records: HistoryRecordsCallable | None = None,
        prepare_daily_recovery: RecoveryPreparationCallable | None = None,
        clock: Callable[[], datetime] | None = None,
        retry_policy: CollectorRetryPolicy | None = None,
        recovery_batch_limit: int = EXPECTED_MARKET_WATCH_MINUTES,
    ) -> None:
        self._store = store
        self._capture_current = capture_current
        self._repair_historical = repair_historical
        self._repair_historical_with_progress = repair_historical_with_progress
        self._persist_snapshot = persist_snapshot
        self._history_records = history_records
        self._prepare_daily_recovery = prepare_daily_recovery
        self._clock = clock or (lambda: datetime.now(SHANGHAI))
        self.retry_policy = retry_policy or CollectorRetryPolicy()
        if not 1 <= int(recovery_batch_limit) <= EXPECTED_MARKET_WATCH_MINUTES:
            raise ValueError(
                "recovery_batch_limit must be between 1 and "
                f"{EXPECTED_MARKET_WATCH_MINUTES}"
            )
        self._recovery_batch_limit = int(recovery_batch_limit)
        self._started = False
        self._closed = False

    def start(self) -> int:
        """Recover an interrupted attempt once and publish a running heartbeat."""

        if self._closed:
            raise RuntimeError("collector is closed")
        now = self._now()
        self._store.update_runtime(CollectorRuntimeState.STARTING, heartbeat_at=now)
        recovered = self._store.recover_inflight(now)
        self._store.ensure_expected_slots(now.date())
        reconciled = 0
        if self._history_records is not None:
            for item in self._history_records():
                result = self._store.reconcile_history_record(item)
                if result.get("action") in {"imported", "heartbeat"}:
                    reconciled += 1
        self._store.update_runtime(CollectorRuntimeState.RUNNING, heartbeat_at=now)
        self._started = True
        return recovered + reconciled

    def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("collector is closed")
        if not self._started:
            self.start()
        observed = self._aware(now or self._clock(), name="now")
        session = a_share_session(observed)
        recovery = self._store.claim_daily_recovery(started_at=observed)
        if recovery is not None:
            return self._run_daily_recovery(recovery, observed)
        self._store.update_runtime(CollectorRuntimeState.RUNNING, heartbeat_at=observed)
        if not session.is_trading_day:
            return {"action": "idle", "reason": session.phase.value}
        return self._run_due_slot(observed)

    def _run_due_slot(
        self,
        observed: datetime,
        *,
        trade_date=None,
        recovery_run_id: int | None = None,
    ) -> dict[str, Any]:
        self._store.update_runtime(CollectorRuntimeState.RUNNING, heartbeat_at=observed)
        session = a_share_session(observed)
        claimed = self._store.claim_due(
            observed,
            trade_date=trade_date,
            current_only=(
                recovery_run_id is None and session.phase.value != "closed"
            ),
        )
        if claimed is None:
            return {"action": "idle", "reason": "no_due_slot"}
        attempt_id, slot = claimed
        if recovery_run_id is not None:
            self._store.record_daily_recovery_attempt_started(
                recovery_run_id,
                slot,
                started_at=observed,
            )
        current_minute = observed.replace(second=0, microsecond=0)
        is_current = slot.minute_bucket == current_minute
        is_final_close = slot.minute_bucket.time().replace(tzinfo=None) == FINAL_CLOSE_TIME
        capture = (
            self._repair_historical
            if is_final_close or not is_current
            else self._capture_current
        )
        snapshot: MarketWatchSnapshotV1 | None = None
        persistence: dict[str, Any] | None = None
        digest = ""
        try:
            if capture is self._repair_historical and self._repair_historical_with_progress:
                def publish_progress(
                    completed: int,
                    total: int,
                    stage: str,
                    message: str | None = None,
                ) -> None:
                    if recovery_run_id is not None:
                        progress_at = self._now()
                        self._store.record_daily_recovery_attempt_progress(
                            recovery_run_id,
                            slot,
                            completed=completed,
                            total=total,
                            stage=stage,
                            message=message,
                            observed_at=progress_at,
                        )
                        self._store.update_runtime(
                            CollectorRuntimeState.RUNNING,
                            heartbeat_at=progress_at,
                        )

                captured = self._repair_historical_with_progress(
                    slot,
                    publish_progress,
                )
            else:
                captured = capture(slot)
            snapshot = MarketWatchSnapshotV1.model_validate(captured)
            if snapshot.as_of.astimezone(SHANGHAI).replace(
                second=0,
                microsecond=0,
            ) != slot.minute_bucket:
                raise ValueError("capture returned a different minute than the claimed slot")
            persistence = dict(self._persist_snapshot(snapshot))
            action = str(persistence.get("action") or "")
            digest = str(persistence.get("payload_digest") or "")
            if action not in {"inserted", "updated", "unchanged"}:
                raise RuntimeError(
                    f"canonical history did not accept capture: {action or 'missing_action'}"
                )
            accepted = self._store.mark_accepted(
                attempt_id,
                snapshot,
                source_snapshot_revision=digest,
                completed_at=self._now(),
            )
        except Exception as error:
            completed_at = self._now()
            if isinstance(error, TerminalCollectionError):
                next_retry_at = None
            elif isinstance(error, RetryableCollectionError):
                retry_deadline = slot.minute_bucket + timedelta(
                    seconds=self.retry_policy.repair_retention_seconds
                )
                if error.retry_deadline is not None:
                    retry_deadline = min(retry_deadline, error.retry_deadline)
                candidate = completed_at + timedelta(
                    seconds=error.retry_after_seconds
                )
                next_retry_at = candidate if candidate <= retry_deadline else None
            else:
                next_retry_at = self.retry_policy.next_retry_at(
                    attempt_count=slot.attempt_count,
                    completed_at=completed_at,
                    minute_bucket=slot.minute_bucket,
                )
            failed = self._store.mark_failed(
                attempt_id,
                error=error,
                completed_at=completed_at,
                next_retry_at=next_retry_at,
            )
            if (
                snapshot is not None
                and persistence is not None
                and str(persistence.get("action") or "")
                in {"inserted", "updated", "unchanged"}
                and digest
            ):
                # History is authoritative once its canonical transaction has
                # committed.  If the following ledger publication failed, do
                # not leave a durable real row mislabeled as a data gap until
                # process restart; reconcile the exact pointer immediately.
                try:
                    recovery = self._store.reconcile_history_record(
                        {
                            "trade_date": slot.trade_date.isoformat(),
                            "minute_bucket": slot.minute_bucket.isoformat(),
                            "snapshot_id": snapshot.snapshot_id,
                            "payload_digest": digest,
                            "record_kind": "accepted_real",
                            "updated_at": completed_at.isoformat(),
                        }
                    )
                    recovered_slot = recovery.get("slot")
                except Exception:
                    logger.exception(
                        "persisted market-watch row could not be reconciled to ledger"
                    )
                else:
                    if (
                        isinstance(recovered_slot, CollectionSlotV1)
                        and recovered_slot.status
                        in {
                            CollectionSlotStatus.ACCEPTED_REAL,
                            CollectionSlotStatus.REPAIRED,
                        }
                    ):
                        if recovery_run_id is not None:
                            self._store.record_daily_recovery_attempt_finished(
                                recovery_run_id,
                                recovered_slot,
                                completed_at=completed_at,
                                outcome="reconciled_persisted",
                            )
                        self._store.update_runtime(
                            CollectorRuntimeState.RUNNING,
                            heartbeat_at=completed_at,
                        )
                        return {
                            "action": "accepted",
                            "minute_bucket": recovered_slot.minute_bucket.isoformat(),
                            "current": is_current,
                            "status": recovered_slot.status.value,
                            "attempt_count": recovered_slot.attempt_count,
                            "snapshot_id": recovered_slot.source_snapshot_id,
                            "source_snapshot_revision": (
                                recovered_slot.source_snapshot_revision
                            ),
                            "ledger_reconciled": True,
                        }
            if recovery_run_id is not None:
                self._store.record_daily_recovery_attempt_finished(
                    recovery_run_id,
                    failed,
                    completed_at=completed_at,
                )
            self._store.update_runtime(
                CollectorRuntimeState.DEGRADED,
                heartbeat_at=completed_at,
            )
            return {
                "action": "failed",
                "minute_bucket": slot.minute_bucket.isoformat(),
                "current": is_current,
                "status": failed.status.value,
                "attempt_count": failed.attempt_count,
                "next_retry_at": (
                    None
                    if failed.next_retry_at is None
                    else failed.next_retry_at.isoformat()
                ),
                "error_code": failed.last_error_code,
                "error_message": failed.last_error_message,
            }
        if recovery_run_id is not None:
            self._store.record_daily_recovery_attempt_finished(
                recovery_run_id,
                accepted,
                completed_at=accepted.accepted_at or self._now(),
            )
        self._store.update_runtime(
            CollectorRuntimeState.RUNNING,
            heartbeat_at=self._now(),
        )
        return {
            "action": "accepted",
            "minute_bucket": accepted.minute_bucket.isoformat(),
            "current": is_current,
            "status": accepted.status.value,
            "attempt_count": accepted.attempt_count,
            "snapshot_id": accepted.source_snapshot_id,
            "source_snapshot_revision": accepted.source_snapshot_revision,
        }

    def _run_daily_recovery(
        self,
        recovery: DailyCollectionRecoveryV1,
        observed: datetime,
    ) -> dict[str, Any]:
        """Reconcile persisted truth, then attempt each remaining minute once."""

        reconciled = 0
        attempted = 0
        try:
            if self._history_records is not None:
                for item in self._history_records():
                    if str(item.get("trade_date") or "") != recovery.trade_date.isoformat():
                        continue
                    result = self._store.reconcile_history_record(item)
                    if result.get("action") == "imported":
                        reconciled += 1
            if self._prepare_daily_recovery is not None:
                self._prepare_daily_recovery(
                    recovery.trade_date,
                    observed,
                    lambda: self._store.update_runtime(
                        CollectorRuntimeState.RUNNING,
                        heartbeat_at=self._now(),
                    ),
                )
            self._store.requeue_daily_recovery_gaps(
                recovery.trade_date,
                requested_at=observed,
            )
            while attempted < self._recovery_batch_limit:
                attempt_observed = self._now()
                result = self._run_due_slot(
                    attempt_observed,
                    trade_date=recovery.trade_date,
                    recovery_run_id=recovery.run_id,
                )
                if result.get("action") == "idle":
                    break
                attempted += 1
        except Exception as error:
            finished = self._store.finish_daily_recovery(
                recovery.run_id,
                completed_at=self._now(),
                reconciled_slots=reconciled,
                attempted_slots=attempted,
                error=error,
            )
            self._store.update_runtime(
                CollectorRuntimeState.DEGRADED,
                heartbeat_at=self._now(),
            )
            logger.exception("post-close market-watch recovery failed")
        else:
            finished = self._store.finish_daily_recovery(
                recovery.run_id,
                completed_at=self._now(),
                reconciled_slots=reconciled,
                attempted_slots=attempted,
            )
            self._store.update_runtime(
                (
                    CollectorRuntimeState.RUNNING
                    if finished.remaining_gaps == 0
                    else CollectorRuntimeState.DEGRADED
                ),
                heartbeat_at=self._now(),
            )
        return {
            "action": "daily_recovery",
            "trade_date": recovery.trade_date.isoformat(),
            "trigger": recovery.trigger.value,
            "status": finished.status.value,
            "reconciled_slots": finished.reconciled_slots,
            "attempted_slots": finished.attempted_slots,
            "remaining_gaps": finished.remaining_gaps,
            "manual_action_required": finished.manual_action_required,
        }

    def _queue_automatic_recovery_if_due(self, observed: datetime) -> None:
        session = a_share_session(observed)
        if (
            session.is_trading_day
            and session.phase.value == "closed"
            and observed.time().replace(tzinfo=None) >= AUTOMATIC_RECOVERY_START
        ):
            self._store.request_daily_recovery(
                observed.date(),
                trigger=DailyRecoveryTrigger.AUTOMATIC,
                requested_at=observed,
            )

    def run_forever(self, stop_event: threading.Event) -> None:
        self.start()
        try:
            while not stop_event.is_set():
                observed = self._now()
                self._queue_automatic_recovery_if_due(observed)
                result = self.run_once(now=observed)
                delay = self._next_delay(result)
                stop_event.wait(delay)
        finally:
            now = self._now()
            self._store.update_runtime(CollectorRuntimeState.STOPPING, heartbeat_at=now)
            self._store.update_runtime(CollectorRuntimeState.STOPPED, heartbeat_at=now)

    def _next_delay(self, result: Mapping[str, Any]) -> float:
        if result.get("action") == "accepted" and result.get("current") is False:
            return self.retry_policy.repair_interval_seconds
        if result.get("action") == "failed" and result.get("current") is False:
            return self.retry_policy.repair_interval_seconds
        return self.retry_policy.idle_poll_seconds

    def _now(self) -> datetime:
        return self._aware(self._clock(), name="clock")

    @staticmethod
    def _aware(value: datetime, *, name: str) -> datetime:
        if not isinstance(value, datetime):
            raise TypeError(f"{name} must be a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must include a timezone")
        return value.astimezone(SHANGHAI)

    def close(self) -> None:
        if self._closed:
            return
        now = self._now()
        self._store.update_runtime(CollectorRuntimeState.STOPPED, heartbeat_at=now)
        self._closed = True


__all__ = [
    "CollectorRetryPolicy",
    "MarketWatchCollector",
]
