"""Strict provider-neutral contracts for intraday collection completeness."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SHANGHAI = ZoneInfo("Asia/Shanghai")


class TerminalCollectionError(RuntimeError):
    """A historical collection gap cannot change without new external evidence."""


class RetryableCollectionError(RuntimeError):
    """A collection gap needs new evidence and an explicit short retry cadence."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float,
        retry_deadline: datetime | None = None,
    ) -> None:
        if retry_after_seconds <= 0:
            raise ValueError("retry_after_seconds must be positive")
        if retry_deadline is not None and (
            retry_deadline.tzinfo is None or retry_deadline.utcoffset() is None
        ):
            raise ValueError("retry_deadline must include a timezone")
        super().__init__(message)
        self.retry_after_seconds = float(retry_after_seconds)
        self.retry_deadline = (
            None
            if retry_deadline is None
            else retry_deadline.astimezone(SHANGHAI)
        )


class CollectionContractModel(BaseModel):
    """Immutable collection model that rejects accidental transport fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CollectionSlotStatus(str, Enum):
    EXPECTED = "expected"
    CAPTURING = "capturing"
    RETRYING = "retrying"
    ACCEPTED_REAL = "accepted_real"
    REPAIRED = "repaired"
    UNRESOLVED = "unresolved"
    EXCLUDED = "excluded"


class CollectorRuntimeState(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


class DailyRecoveryTrigger(str, Enum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class DailyRecoveryStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    RETRYING = "retrying"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"


def _aware_shanghai(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(SHANGHAI)


class CollectionSlotV1(CollectionContractModel):
    contract: Literal["market_watch_collection_slot.v1"] = (
        "market_watch_collection_slot.v1"
    )
    schema_version: Literal[1] = 1
    trade_date: date
    minute_bucket: datetime
    status: CollectionSlotStatus
    attempt_count: int = Field(ge=0)
    first_attempt_at: datetime | None = None
    last_attempt_at: datetime | None = None
    next_retry_at: datetime | None = None
    accepted_at: datetime | None = None
    source_snapshot_id: str | None = None
    source_snapshot_revision: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    gap_heartbeat: bool = False
    last_error_code: str | None = None
    last_error_message: str | None = None
    ledger_revision: int = Field(ge=0)
    updated_at: datetime

    @field_validator(
        "minute_bucket",
        "first_attempt_at",
        "last_attempt_at",
        "next_retry_at",
        "accepted_at",
        "updated_at",
    )
    @classmethod
    def require_aware_times(cls, value: datetime | None, info):
        return _aware_shanghai(value, info.field_name)

    @model_validator(mode="after")
    def validate_state(self) -> "CollectionSlotV1":
        if self.minute_bucket.second or self.minute_bucket.microsecond:
            raise ValueError("minute_bucket must be minute aligned")
        if self.minute_bucket.date() != self.trade_date:
            raise ValueError("minute_bucket must belong to trade_date")
        accepted = self.status in {
            CollectionSlotStatus.ACCEPTED_REAL,
            CollectionSlotStatus.REPAIRED,
        }
        pointer_values = (
            self.accepted_at,
            self.source_snapshot_id,
            self.source_snapshot_revision,
        )
        if accepted and any(value is None for value in pointer_values):
            raise ValueError("accepted collection slot requires a complete snapshot pointer")
        excluded = self.status is CollectionSlotStatus.EXCLUDED
        if excluded and any(value is not None for value in pointer_values) and any(
            value is None for value in pointer_values
        ):
            raise ValueError("excluded collection slot must retain a complete snapshot pointer")
        if not accepted and not excluded and any(value is not None for value in pointer_values):
            raise ValueError("unaccepted collection slot cannot publish a snapshot pointer")
        if self.attempt_count == 0 and any(
            value is not None
            for value in (self.first_attempt_at, self.last_attempt_at)
        ):
            raise ValueError("unattempted collection slot cannot have attempt timestamps")
        if self.attempt_count > 0 and any(
            value is None
            for value in (self.first_attempt_at, self.last_attempt_at)
        ):
            raise ValueError("attempted collection slot requires attempt timestamps")
        if accepted and self.next_retry_at is not None:
            raise ValueError("accepted collection slot cannot schedule another retry")
        if accepted and self.gap_heartbeat:
            raise ValueError("accepted collection slot cannot remain a gap heartbeat")
        if self.gap_heartbeat and self.status not in {
            CollectionSlotStatus.EXPECTED,
            CollectionSlotStatus.CAPTURING,
            CollectionSlotStatus.RETRYING,
            CollectionSlotStatus.UNRESOLVED,
            CollectionSlotStatus.EXCLUDED,
        }:
            raise ValueError("gap heartbeat has an invalid collection status")
        return self


class AcceptedSnapshotPointerV1(CollectionContractModel):
    trade_date: date
    minute_bucket: datetime
    snapshot_id: str = Field(min_length=1)
    source_snapshot_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_at: datetime
    repaired: bool

    @field_validator("minute_bucket", "accepted_at")
    @classmethod
    def require_aware_times(cls, value: datetime, info):
        return _aware_shanghai(value, info.field_name)

    @model_validator(mode="after")
    def validate_minute(self) -> "AcceptedSnapshotPointerV1":
        if self.minute_bucket.second or self.minute_bucket.microsecond:
            raise ValueError("accepted pointer minute_bucket must be minute aligned")
        if self.minute_bucket.date() != self.trade_date:
            raise ValueError("accepted pointer minute_bucket must belong to trade_date")
        return self


class CollectionCompletenessV1(CollectionContractModel):
    contract: Literal["market_watch_collection_completeness.v1"] = (
        "market_watch_collection_completeness.v1"
    )
    schema_version: Literal[1] = 1
    trade_date: date
    as_of: datetime
    expected_minute_buckets: int = Field(ge=0)
    accepted_real: int = Field(ge=0)
    repaired: int = Field(ge=0)
    pending: int = Field(ge=0)
    retrying: int = Field(ge=0)
    unresolved: int = Field(ge=0)
    gap_heartbeat: int = Field(ge=0)
    ledger_revision: int = Field(ge=0)
    ledger_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("as_of")
    @classmethod
    def require_aware_as_of(cls, value: datetime):
        return _aware_shanghai(value, "as_of")

    @model_validator(mode="after")
    def validate_counts(self) -> "CollectionCompletenessV1":
        classified = self.accepted_real + self.pending + self.retrying + self.unresolved
        if classified != self.expected_minute_buckets:
            raise ValueError("collection completeness counts must classify every expected minute")
        if self.repaired > self.accepted_real:
            raise ValueError("repaired count cannot exceed accepted_real")
        if self.gap_heartbeat > self.expected_minute_buckets:
            raise ValueError("gap_heartbeat count cannot exceed expected minutes")
        return self


class DailyCollectionRecoveryV1(CollectionContractModel):
    """Durable state for one post-close completeness and repair sweep."""

    contract: Literal["market_watch_daily_recovery.v1"] = (
        "market_watch_daily_recovery.v1"
    )
    schema_version: Literal[1] = 1
    run_id: int = Field(gt=0)
    trade_date: date
    trigger: DailyRecoveryTrigger
    status: DailyRecoveryStatus
    requested_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    expected_minute_buckets: int = Field(ge=0)
    accepted_before: int = Field(ge=0)
    accepted_after: int = Field(ge=0)
    reconciled_slots: int = Field(ge=0)
    attempted_slots: int = Field(ge=0)
    failed_attempts: int = Field(ge=0)
    remaining_gaps: int = Field(ge=0)
    manual_action_required: bool
    latest_attempt_minute_bucket: datetime | None = None
    latest_attempt_at: datetime | None = None
    latest_attempt_outcome: str | None = None
    latest_attempt_progress_completed: int = Field(default=0, ge=0)
    latest_attempt_progress_total: int = Field(default=0, ge=0)
    latest_attempt_progress_stage: str | None = None
    latest_attempt_progress_message: str | None = None
    latest_failure_minute_bucket: datetime | None = None
    latest_failure_at: datetime | None = None
    latest_failure_next_retry_at: datetime | None = None
    latest_failure_error_code: str | None = None
    latest_failure_error_message: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None

    @field_validator(
        "requested_at",
        "started_at",
        "completed_at",
        "latest_attempt_minute_bucket",
        "latest_attempt_at",
        "latest_failure_minute_bucket",
        "latest_failure_at",
        "latest_failure_next_retry_at",
    )
    @classmethod
    def require_aware_times(cls, value: datetime | None, info):
        return _aware_shanghai(value, info.field_name)

    @model_validator(mode="after")
    def validate_recovery(self) -> "DailyCollectionRecoveryV1":
        if self.requested_at.date() < self.trade_date:
            raise ValueError("daily recovery request cannot precede trade_date")
        if self.accepted_before > self.expected_minute_buckets:
            raise ValueError("accepted_before cannot exceed expected minutes")
        if self.accepted_after > self.expected_minute_buckets:
            raise ValueError("accepted_after cannot exceed expected minutes")
        if self.remaining_gaps != self.expected_minute_buckets - self.accepted_after:
            raise ValueError("remaining_gaps does not match accepted_after")
        if self.failed_attempts > self.attempted_slots:
            raise ValueError("failed_attempts cannot exceed attempted_slots")
        if self.latest_attempt_minute_bucket is not None:
            if (
                self.latest_attempt_minute_bucket.second
                or self.latest_attempt_minute_bucket.microsecond
            ):
                raise ValueError("latest attempt minute must be minute aligned")
            if self.latest_attempt_minute_bucket.date() != self.trade_date:
                raise ValueError("latest attempt minute must belong to trade_date")
            if self.latest_attempt_at is None or not self.latest_attempt_outcome:
                raise ValueError("latest attempt requires timestamp and outcome")
        elif any(
            value is not None
            for value in (
                self.latest_attempt_at,
                self.latest_attempt_outcome,
            )
        ):
            raise ValueError("latest attempt detail requires a minute bucket")
        if self.latest_attempt_progress_completed > self.latest_attempt_progress_total:
            raise ValueError("latest attempt progress cannot exceed its total")
        if self.latest_attempt_progress_total and not self.latest_attempt_progress_stage:
            raise ValueError("latest attempt progress requires a stage")
        if self.latest_attempt_minute_bucket is None and any(
            (
                self.latest_attempt_progress_completed,
                self.latest_attempt_progress_total,
                self.latest_attempt_progress_stage,
                self.latest_attempt_progress_message,
            )
        ):
            raise ValueError("latest attempt progress requires a minute bucket")
        if self.latest_failure_minute_bucket is not None:
            if (
                self.latest_failure_minute_bucket.second
                or self.latest_failure_minute_bucket.microsecond
            ):
                raise ValueError("latest failure minute must be minute aligned")
            if self.latest_failure_minute_bucket.date() != self.trade_date:
                raise ValueError("latest failure minute must belong to trade_date")
            if self.latest_failure_at is None or not self.latest_failure_error_code:
                raise ValueError("latest failure requires timestamp and error code")
        elif any(
            value is not None
            for value in (
                self.latest_failure_at,
                self.latest_failure_next_retry_at,
                self.latest_failure_error_code,
                self.latest_failure_error_message,
            )
        ):
            raise ValueError("latest failure detail requires a minute bucket")
        if self.status is DailyRecoveryStatus.PENDING:
            if self.started_at is not None or self.completed_at is not None:
                raise ValueError("pending recovery cannot have execution timestamps")
        elif self.status is DailyRecoveryStatus.RUNNING:
            if self.started_at is None or self.completed_at is not None:
                raise ValueError("running recovery requires only started_at")
        elif self.started_at is None or self.completed_at is None:
            raise ValueError("finished recovery requires execution timestamps")
        expected_manual = self.status in {
            DailyRecoveryStatus.NEEDS_ATTENTION,
            DailyRecoveryStatus.FAILED,
        }
        if self.manual_action_required != expected_manual:
            raise ValueError("manual_action_required does not match recovery status")
        if self.status is DailyRecoveryStatus.COMPLETE and self.remaining_gaps:
            raise ValueError("complete recovery cannot retain gaps")
        if self.status is DailyRecoveryStatus.FAILED and not self.last_error_code:
            raise ValueError("failed recovery requires last_error_code")
        if self.status is not DailyRecoveryStatus.FAILED and (
            self.last_error_code or self.last_error_message
        ):
            raise ValueError("only failed recovery may expose run error detail")
        return self


class MarketWatchCollectorEnvelopeV1(CollectionContractModel):
    contract: Literal["market_watch_collector_envelope.v1"] = (
        "market_watch_collector_envelope.v1"
    )
    schema_version: Literal[1] = 1
    as_of: datetime
    collector_state: CollectorRuntimeState
    collector_heartbeat_at: datetime | None = None
    latest_accepted_real: AcceptedSnapshotPointerV1 | None = None
    collection_cursor: CollectionSlotV1 | None = None
    latest_published_status: CollectionSlotV1 | None = None
    collection_completeness: CollectionCompletenessV1
    daily_recovery: DailyCollectionRecoveryV1 | None = None

    @field_validator("as_of", "collector_heartbeat_at")
    @classmethod
    def require_aware_times(cls, value: datetime | None, info):
        return _aware_shanghai(value, info.field_name)


__all__ = [
    "AcceptedSnapshotPointerV1",
    "CollectionCompletenessV1",
    "CollectionContractModel",
    "CollectionSlotStatus",
    "CollectionSlotV1",
    "CollectorRuntimeState",
    "DailyCollectionRecoveryV1",
    "DailyRecoveryStatus",
    "DailyRecoveryTrigger",
    "MarketWatchCollectorEnvelopeV1",
    "RetryableCollectionError",
    "TerminalCollectionError",
]
