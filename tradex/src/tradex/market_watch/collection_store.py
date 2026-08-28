"""Persistent minute ledger for the provider-neutral market-watch collector."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tradex.market_calendar import CalendarDayStatus, calendar_day_status

from .collection_contracts import (
    AcceptedSnapshotPointerV1,
    CollectionCompletenessV1,
    CollectionSlotStatus,
    CollectionSlotV1,
    CollectorRuntimeState,
    DailyCollectionRecoveryV1,
    DailyRecoveryStatus,
    DailyRecoveryTrigger,
    MarketWatchCollectorEnvelopeV1,
)
from .contracts import FreshnessStatus, MarketWatchSnapshotV1
from .history import DEFAULT_CONFIG_VERSION, ENV_DB_PATH
from .session_schedule import expected_market_watch_minutes
from .integrity import stable_sha256


COLLECTION_LEDGER_CONTRACT = "market_watch_collection_ledger.v1"
COLLECTION_LEDGER_SCHEMA_VERSION = 1
SHANGHAI = ZoneInfo("Asia/Shanghai")
_ACCEPTED_STATUSES = (
    CollectionSlotStatus.ACCEPTED_REAL.value,
    CollectionSlotStatus.REPAIRED.value,
)


def _aware_shanghai(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return value.astimezone(SHANGHAI)


def _minute(value: datetime, *, name: str = "minute_bucket") -> datetime:
    return _aware_shanghai(value, name=name).replace(second=0, microsecond=0)


def expected_session_minutes(trade_date: date) -> tuple[datetime, ...]:
    """Compatibility alias for the phase-aware market-watch schedule."""

    return expected_market_watch_minutes(trade_date)


class MarketWatchCollectionStore:
    """SQLite/WAL collection ledger sharing the market-watch history database."""

    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        *,
        config_version: str = DEFAULT_CONFIG_VERSION,
        clock=None,
        read_only: bool = False,
    ) -> None:
        version = str(config_version).strip()
        if not version:
            raise ValueError("config_version must not be empty")
        configured = db_path or os.environ.get(ENV_DB_PATH)
        if configured is None:
            configured = Path.home() / ".tradex" / "market_watch.sqlite3"
        self.read_only = bool(read_only)
        if str(configured) == ":memory:":
            if self.read_only:
                raise ValueError("read-only collection store requires a file path")
            self.db_path = ":memory:"
        else:
            resolved = Path(configured).expanduser().resolve()
            if not self.read_only:
                resolved.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(resolved)
        self.config_version = version
        self._clock = clock or (lambda: datetime.now(SHANGHAI))
        self._lock = threading.RLock()
        self._closed = False
        self._schema_available = False
        self._connection: sqlite3.Connection | None = None
        if self.read_only:
            with self._lock:
                self._open_read_only_locked()
            return
        self._connection = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=5,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA busy_timeout = 5000")
            if self.db_path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
            self._initialize()
            self._schema_available = True
        except Exception:
            self._connection.close()
            self._closed = True
            raise

    def _open_read_only_locked(self) -> bool:
        """Open the database lazily without ever creating files or schema."""

        if self._connection is not None and self._schema_available:
            return True
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._schema_available = False
        target = Path(self.db_path)
        if not target.exists():
            return False

        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{target.as_uri()}?mode=ro",
                check_same_thread=False,
                timeout=5,
                uri=True,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA query_only = ON")
            required = {
                "market_watch_collection_meta",
                "market_watch_collection_slots",
                "market_watch_collection_attempts",
            }
            present = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not required <= present:
                connection.close()
                return False
            expected = {
                "contract": COLLECTION_LEDGER_CONTRACT,
                "schema_version": str(COLLECTION_LEDGER_SCHEMA_VERSION),
            }
            stored = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM market_watch_collection_meta "
                    "WHERE key IN ('contract', 'schema_version')"
                ).fetchall()
            }
            if not expected.keys() <= stored.keys():
                connection.close()
                return False
            for key, value in expected.items():
                if stored[key] != value:
                    raise RuntimeError(
                        f"incompatible collection ledger {key}: {stored[key]}"
                    )
        except sqlite3.OperationalError:
            if connection is not None:
                connection.close()
            return False
        except Exception:
            if connection is not None:
                connection.close()
            raise

        self._connection = connection
        self._schema_available = True
        return True

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_watch_collection_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS market_watch_collection_slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    minute_bucket TEXT NOT NULL,
                    config_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    first_attempt_at TEXT,
                    last_attempt_at TEXT,
                    next_retry_at TEXT,
                    accepted_at TEXT,
                    source_snapshot_id TEXT,
                    source_snapshot_revision TEXT,
                    gap_heartbeat INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT,
                    last_error_message TEXT,
                    ledger_revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (trade_date, minute_bucket, config_version)
                );

                CREATE INDEX IF NOT EXISTS idx_market_watch_collection_due
                    ON market_watch_collection_slots (
                        config_version, status, next_retry_at,
                        trade_date, minute_bucket
                    );

                CREATE TABLE IF NOT EXISTS market_watch_collection_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    minute_bucket TEXT NOT NULL,
                    config_version TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    repair INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    source_snapshot_id TEXT,
                    source_snapshot_revision TEXT,
                    error_code TEXT,
                    error_message TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_market_watch_collection_attempts
                    ON market_watch_collection_attempts (
                        config_version, trade_date, minute_bucket, id
                    );

                CREATE TABLE IF NOT EXISTS market_watch_daily_recovery_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    config_version TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    expected_minute_buckets INTEGER NOT NULL,
                    accepted_before INTEGER NOT NULL,
                    accepted_after INTEGER NOT NULL,
                    reconciled_slots INTEGER NOT NULL DEFAULT 0,
                    attempted_slots INTEGER NOT NULL DEFAULT 0,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    remaining_gaps INTEGER NOT NULL,
                    latest_attempt_minute_bucket TEXT,
                    latest_attempt_at TEXT,
                    latest_attempt_outcome TEXT,
                    latest_attempt_progress_completed INTEGER NOT NULL DEFAULT 0,
                    latest_attempt_progress_total INTEGER NOT NULL DEFAULT 0,
                    latest_attempt_progress_stage TEXT,
                    latest_attempt_progress_message TEXT,
                    latest_failure_minute_bucket TEXT,
                    latest_failure_at TEXT,
                    latest_failure_next_retry_at TEXT,
                    latest_failure_error_code TEXT,
                    latest_failure_error_message TEXT,
                    last_error_code TEXT,
                    last_error_message TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_market_watch_daily_recovery
                    ON market_watch_daily_recovery_runs (
                        config_version, trade_date, id DESC
                    );
                """
            )
            recovery_columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(market_watch_daily_recovery_runs)"
                ).fetchall()
            }
            additive_recovery_columns = {
                "failed_attempts": "INTEGER NOT NULL DEFAULT 0",
                "latest_attempt_minute_bucket": "TEXT",
                "latest_attempt_at": "TEXT",
                "latest_attempt_outcome": "TEXT",
                "latest_attempt_progress_completed": "INTEGER NOT NULL DEFAULT 0",
                "latest_attempt_progress_total": "INTEGER NOT NULL DEFAULT 0",
                "latest_attempt_progress_stage": "TEXT",
                "latest_attempt_progress_message": "TEXT",
                "latest_failure_minute_bucket": "TEXT",
                "latest_failure_at": "TEXT",
                "latest_failure_next_retry_at": "TEXT",
                "latest_failure_error_code": "TEXT",
                "latest_failure_error_message": "TEXT",
                "last_error_message": "TEXT",
            }
            for column, declaration in additive_recovery_columns.items():
                if column not in recovery_columns:
                    self._connection.execute(
                        f"ALTER TABLE market_watch_daily_recovery_runs "
                        f"ADD COLUMN {column} {declaration}"
                    )
            expected = {
                "contract": COLLECTION_LEDGER_CONTRACT,
                "schema_version": str(COLLECTION_LEDGER_SCHEMA_VERSION),
                "ledger_revision": "0",
                "collector_state": CollectorRuntimeState.UNKNOWN.value,
                "collector_heartbeat_at": "",
            }
            for key, value in expected.items():
                self._connection.execute(
                    "INSERT OR IGNORE INTO market_watch_collection_meta (key, value) VALUES (?, ?)",
                    (key, value),
                )
            for key in ("contract", "schema_version"):
                stored = self._meta_locked(key)
                if stored != expected[key]:
                    raise RuntimeError(f"incompatible collection ledger {key}: {stored}")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("market-watch collection store is closed")

    def _ensure_writable(self) -> None:
        self._ensure_open()
        if self.read_only:
            raise RuntimeError("market-watch collection store is read-only")

    def _can_read(self) -> bool:
        self._ensure_open()
        if self._connection is not None and self._schema_available:
            return True
        if not self.read_only:
            return False
        with self._lock:
            return self._open_read_only_locked()

    def _meta_locked(self, key: str) -> str:
        row = self._connection.execute(
            "SELECT value FROM market_watch_collection_meta WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"collection ledger meta is missing {key}")
        return str(row["value"])

    def _bump_revision_locked(self) -> int:
        revision = int(self._meta_locked("ledger_revision")) + 1
        self._connection.execute(
            "UPDATE market_watch_collection_meta SET value = ? WHERE key = 'ledger_revision'",
            (str(revision),),
        )
        return revision

    def ensure_expected_slots(self, trade_date: date) -> int:
        self._ensure_writable()
        minutes = expected_session_minutes(trade_date)
        if not minutes:
            return 0
        now = _aware_shanghai(self._clock(), name="clock").isoformat(timespec="seconds")
        with self._lock:
            self._ensure_open()
            with self._connection:
                existing_rows = self._connection.execute(
                    """
                    SELECT id, minute_bucket, status, attempt_count
                    FROM market_watch_collection_slots
                    WHERE trade_date = ? AND config_version = ?
                    """,
                    (trade_date.isoformat(), self.config_version),
                ).fetchall()
                expected_isos = {
                    minute.isoformat(timespec="seconds") for minute in minutes
                }
                existing_isos = {str(row["minute_bucket"]) for row in existing_rows}
                to_exclude = [
                    row
                    for row in existing_rows
                    if row["minute_bucket"] not in expected_isos
                    and row["status"] != CollectionSlotStatus.EXCLUDED.value
                ]
                to_restore = [
                    row
                    for row in existing_rows
                    if row["minute_bucket"] in expected_isos
                    and row["status"] == CollectionSlotStatus.EXCLUDED.value
                ]
                missing = [
                    minute
                    for minute in minutes
                    if minute.isoformat(timespec="seconds") not in existing_isos
                ]
                if not to_exclude and not to_restore and not missing:
                    return 0
                revision = self._bump_revision_locked()
                for row in to_exclude:
                    self._connection.execute(
                        """
                        UPDATE market_watch_collection_slots
                        SET status = ?, next_retry_at = NULL,
                            ledger_revision = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            CollectionSlotStatus.EXCLUDED.value,
                            revision,
                            now,
                            row["id"],
                        ),
                    )
                for row in to_restore:
                    restored = (
                        CollectionSlotStatus.RETRYING
                        if int(row["attempt_count"]) > 0
                        else CollectionSlotStatus.EXPECTED
                    )
                    self._connection.execute(
                        """
                        UPDATE market_watch_collection_slots
                        SET status = ?, next_retry_at = ?,
                            ledger_revision = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (restored.value, now, revision, now, row["id"]),
                    )
                inserted = 0
                for minute_bucket in missing:
                    cursor = self._connection.execute(
                        """
                        INSERT OR IGNORE INTO market_watch_collection_slots (
                            trade_date, minute_bucket, config_version, status,
                            ledger_revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            trade_date.isoformat(),
                            minute_bucket.isoformat(timespec="seconds"),
                            self.config_version,
                            CollectionSlotStatus.EXPECTED.value,
                            revision,
                            now,
                            now,
                        ),
                    )
                    inserted += max(cursor.rowcount, 0)
                return inserted

    def request_daily_recovery(
        self,
        trade_date: date,
        *,
        trigger: DailyRecoveryTrigger,
        requested_at: datetime,
    ) -> dict[str, Any]:
        """Queue one post-close sweep without running provider work here."""

        self._ensure_writable()
        observed = _aware_shanghai(requested_at, name="requested_at")
        if calendar_day_status(trade_date) is not CalendarDayStatus.VERIFIED_TRADING_DAY:
            raise ValueError("daily recovery requires a verified trading day")
        if trade_date > observed.date():
            raise ValueError("daily recovery cannot target a future trading day")
        if trade_date == observed.date() and observed.time().replace(tzinfo=None) <= time(15, 0):
            raise ValueError("daily recovery is available after the market closes")
        self.ensure_expected_slots(trade_date)
        requested_iso = observed.isoformat(timespec="seconds")
        with self._lock:
            self._ensure_open()
            with self._connection:
                active = self._connection.execute(
                    """
                    SELECT * FROM market_watch_daily_recovery_runs
                    WHERE config_version = ? AND trade_date = ?
                        AND status IN (?, ?)
                    ORDER BY id DESC LIMIT 1
                    """,
                    (
                        self.config_version,
                        trade_date.isoformat(),
                        DailyRecoveryStatus.PENDING.value,
                        DailyRecoveryStatus.RUNNING.value,
                    ),
                ).fetchone()
                existing = active
                if existing is None and trigger is DailyRecoveryTrigger.AUTOMATIC:
                    existing = self._connection.execute(
                        """
                        SELECT * FROM market_watch_daily_recovery_runs
                        WHERE config_version = ? AND trade_date = ? AND trigger = ?
                        ORDER BY id DESC LIMIT 1
                        """,
                        (
                            self.config_version,
                            trade_date.isoformat(),
                            DailyRecoveryTrigger.AUTOMATIC.value,
                        ),
                    ).fetchone()
                if existing is not None:
                    completeness = self._read_completeness_locked(trade_date, observed)
                    return {
                        "action": "existing",
                        "recovery": self._recovery(existing, completeness),
                    }
                completeness = self._read_completeness_locked(trade_date, observed)
                remaining = (
                    completeness.expected_minute_buckets - completeness.accepted_real
                )
                cursor = self._connection.execute(
                    """
                    INSERT INTO market_watch_daily_recovery_runs (
                        trade_date, config_version, trigger, status, requested_at,
                        expected_minute_buckets, accepted_before, accepted_after,
                        remaining_gaps
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade_date.isoformat(),
                        self.config_version,
                        DailyRecoveryTrigger(trigger).value,
                        DailyRecoveryStatus.PENDING.value,
                        requested_iso,
                        completeness.expected_minute_buckets,
                        completeness.accepted_real,
                        completeness.accepted_real,
                        remaining,
                    ),
                )
                row = self._connection.execute(
                    "SELECT * FROM market_watch_daily_recovery_runs WHERE id = ?",
                    (int(cursor.lastrowid),),
                ).fetchone()
                return {
                    "action": "queued",
                    "recovery": self._recovery(row, completeness),
                }

    def claim_daily_recovery(
        self,
        *,
        started_at: datetime,
    ) -> DailyCollectionRecoveryV1 | None:
        """Claim the oldest durable request for the single collector owner."""

        self._ensure_writable()
        observed = _aware_shanghai(started_at, name="started_at")
        started_iso = observed.isoformat(timespec="seconds")
        with self._lock:
            self._ensure_open()
            with self._connection:
                row = self._connection.execute(
                    """
                    SELECT * FROM market_watch_daily_recovery_runs
                    WHERE config_version = ? AND status = ?
                    ORDER BY requested_at ASC, id ASC LIMIT 1
                    """,
                    (self.config_version, DailyRecoveryStatus.PENDING.value),
                ).fetchone()
                if row is None:
                    return None
                trade_date = date.fromisoformat(row["trade_date"])
                completeness = self._read_completeness_locked(trade_date, observed)
                remaining = (
                    completeness.expected_minute_buckets - completeness.accepted_real
                )
                self._connection.execute(
                    """
                    UPDATE market_watch_daily_recovery_runs
                    SET status = ?, started_at = ?, expected_minute_buckets = ?,
                        accepted_before = ?, accepted_after = ?, remaining_gaps = ?,
                        reconciled_slots = 0, attempted_slots = 0, failed_attempts = 0,
                        latest_attempt_minute_bucket = NULL,
                        latest_attempt_at = NULL, latest_attempt_outcome = NULL,
                        latest_attempt_progress_completed = 0,
                        latest_attempt_progress_total = 0,
                        latest_attempt_progress_stage = NULL,
                        latest_attempt_progress_message = NULL,
                        latest_failure_minute_bucket = NULL,
                        latest_failure_at = NULL,
                        latest_failure_next_retry_at = NULL,
                        latest_failure_error_code = NULL,
                        latest_failure_error_message = NULL,
                        completed_at = NULL, last_error_code = NULL,
                        last_error_message = NULL
                    WHERE id = ? AND status = ?
                    """,
                    (
                        DailyRecoveryStatus.RUNNING.value,
                        started_iso,
                        completeness.expected_minute_buckets,
                        completeness.accepted_real,
                        completeness.accepted_real,
                        remaining,
                        row["id"],
                        DailyRecoveryStatus.PENDING.value,
                    ),
                )
                claimed = self._connection.execute(
                    "SELECT * FROM market_watch_daily_recovery_runs WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                return self._recovery(claimed, completeness)

    def requeue_daily_recovery_gaps(
        self,
        trade_date: date,
        *,
        requested_at: datetime,
    ) -> int:
        """Make every non-accepted post-close slot due for one bounded sweep."""

        self._ensure_writable()
        observed = _aware_shanghai(requested_at, name="requested_at")
        self.ensure_expected_slots(trade_date)
        requested_iso = observed.isoformat(timespec="seconds")
        with self._lock:
            self._ensure_open()
            with self._connection:
                rows = self._connection.execute(
                    """
                    SELECT id FROM market_watch_collection_slots
                    WHERE config_version = ? AND trade_date = ?
                        AND status NOT IN (?, ?, ?, ?)
                    """,
                    (
                        self.config_version,
                        trade_date.isoformat(),
                        *_ACCEPTED_STATUSES,
                        CollectionSlotStatus.CAPTURING.value,
                        CollectionSlotStatus.EXCLUDED.value,
                    ),
                ).fetchall()
                if not rows:
                    return 0
                revision = self._bump_revision_locked()
                self._connection.execute(
                    """
                    UPDATE market_watch_collection_slots
                    SET status = CASE
                            WHEN attempt_count = 0 THEN ? ELSE ? END,
                        next_retry_at = ?, ledger_revision = ?, updated_at = ?
                    WHERE config_version = ? AND trade_date = ?
                        AND status NOT IN (?, ?, ?, ?)
                    """,
                    (
                        CollectionSlotStatus.EXPECTED.value,
                        CollectionSlotStatus.RETRYING.value,
                        requested_iso,
                        revision,
                        requested_iso,
                        self.config_version,
                        trade_date.isoformat(),
                        *_ACCEPTED_STATUSES,
                        CollectionSlotStatus.CAPTURING.value,
                        CollectionSlotStatus.EXCLUDED.value,
                    ),
                )
                return len(rows)

    def record_daily_recovery_attempt_started(
        self,
        run_id: int,
        slot: CollectionSlotV1,
        *,
        started_at: datetime,
    ) -> DailyCollectionRecoveryV1:
        """Publish one claimed minute immediately so the UI can show live progress."""

        self._ensure_writable()
        observed = _aware_shanghai(started_at, name="started_at")
        with self._lock:
            self._ensure_open()
            with self._connection:
                row = self._running_recovery_row_locked(run_id)
                if row["trade_date"] != slot.trade_date.isoformat():
                    raise ValueError("daily recovery attempt belongs to a different trade date")
                completeness = self._read_completeness_locked(slot.trade_date, observed)
                remaining = (
                    completeness.expected_minute_buckets - completeness.accepted_real
                )
                self._connection.execute(
                    """
                    UPDATE market_watch_daily_recovery_runs
                    SET attempted_slots = attempted_slots + 1,
                        accepted_after = ?, remaining_gaps = ?,
                        latest_attempt_minute_bucket = ?, latest_attempt_at = ?,
                        latest_attempt_outcome = 'started',
                        latest_attempt_progress_completed = 0,
                        latest_attempt_progress_total = 0,
                        latest_attempt_progress_stage = 'starting',
                        latest_attempt_progress_message = NULL
                    WHERE id = ?
                    """,
                    (
                        completeness.accepted_real,
                        remaining,
                        slot.minute_bucket.isoformat(timespec="seconds"),
                        observed.isoformat(timespec="seconds"),
                        int(run_id),
                    ),
                )
                updated = self._connection.execute(
                    "SELECT * FROM market_watch_daily_recovery_runs WHERE id = ?",
                    (int(run_id),),
                ).fetchone()
                return self._recovery(updated, completeness)

    def record_daily_recovery_attempt_progress(
        self,
        run_id: int,
        slot: CollectionSlotV1,
        *,
        completed: int,
        total: int,
        stage: str,
        message: str | None = None,
        observed_at: datetime,
    ) -> DailyCollectionRecoveryV1:
        """Publish bounded intra-minute reconstruction progress."""

        self._ensure_writable()
        done, count = int(completed), int(total)
        stage_text = str(stage).strip()
        if count <= 0 or done < 0 or done > count:
            raise ValueError("daily recovery progress must satisfy 0 <= completed <= total")
        if not stage_text:
            raise ValueError("daily recovery progress stage must not be empty")
        observed = _aware_shanghai(observed_at, name="observed_at")
        with self._lock:
            self._ensure_open()
            with self._connection:
                row = self._running_recovery_row_locked(run_id)
                minute_iso = slot.minute_bucket.isoformat(timespec="seconds")
                if row["latest_attempt_minute_bucket"] != minute_iso:
                    raise ValueError("daily recovery progress does not match its started minute")
                self._connection.execute(
                    """
                    UPDATE market_watch_daily_recovery_runs
                    SET latest_attempt_at = ?,
                        latest_attempt_progress_completed = ?,
                        latest_attempt_progress_total = ?,
                        latest_attempt_progress_stage = ?,
                        latest_attempt_progress_message = ?
                    WHERE id = ?
                    """,
                    (
                        observed.isoformat(timespec="seconds"),
                        done,
                        count,
                        stage_text[:120],
                        str(message)[:500] if message else None,
                        int(run_id),
                    ),
                )
                completeness = self._read_completeness_locked(slot.trade_date, observed)
                updated = self._connection.execute(
                    "SELECT * FROM market_watch_daily_recovery_runs WHERE id = ?",
                    (int(run_id),),
                ).fetchone()
                return self._recovery(updated, completeness)

    def record_daily_recovery_attempt_finished(
        self,
        run_id: int,
        slot: CollectionSlotV1,
        *,
        completed_at: datetime,
        outcome: str | None = None,
    ) -> DailyCollectionRecoveryV1:
        """Publish the exact result and safe failure detail for the latest minute."""

        self._ensure_writable()
        observed = _aware_shanghai(completed_at, name="completed_at")
        failed = slot.status in {
            CollectionSlotStatus.RETRYING,
            CollectionSlotStatus.UNRESOLVED,
        }
        with self._lock:
            self._ensure_open()
            with self._connection:
                row = self._running_recovery_row_locked(run_id)
                if row["trade_date"] != slot.trade_date.isoformat():
                    raise ValueError("daily recovery result belongs to a different trade date")
                if row["latest_attempt_minute_bucket"] != slot.minute_bucket.isoformat(
                    timespec="seconds"
                ):
                    raise ValueError("daily recovery result does not match its started minute")
                completeness = self._read_completeness_locked(slot.trade_date, observed)
                remaining = (
                    completeness.expected_minute_buckets - completeness.accepted_real
                )
                self._connection.execute(
                    """
                    UPDATE market_watch_daily_recovery_runs
                    SET failed_attempts = failed_attempts + ?,
                        accepted_after = ?, remaining_gaps = ?,
                        latest_attempt_at = ?, latest_attempt_outcome = ?
                    WHERE id = ?
                    """,
                    (
                        1 if failed else 0,
                        completeness.accepted_real,
                        remaining,
                        observed.isoformat(timespec="seconds"),
                        str(outcome or slot.status.value),
                        int(run_id),
                    ),
                )
                if failed:
                    self._connection.execute(
                        """
                        UPDATE market_watch_daily_recovery_runs
                        SET latest_failure_minute_bucket = ?, latest_failure_at = ?,
                            latest_failure_next_retry_at = ?,
                            latest_failure_error_code = ?,
                            latest_failure_error_message = ?
                        WHERE id = ?
                        """,
                        (
                            slot.minute_bucket.isoformat(timespec="seconds"),
                            observed.isoformat(timespec="seconds"),
                            (
                                slot.next_retry_at.isoformat(timespec="seconds")
                                if slot.next_retry_at is not None
                                else None
                            ),
                            slot.last_error_code,
                            slot.last_error_message,
                            int(run_id),
                        ),
                    )
                updated = self._connection.execute(
                    "SELECT * FROM market_watch_daily_recovery_runs WHERE id = ?",
                    (int(run_id),),
                ).fetchone()
                return self._recovery(updated, completeness)

    def finish_daily_recovery(
        self,
        run_id: int,
        *,
        completed_at: datetime,
        reconciled_slots: int,
        attempted_slots: int,
        error: BaseException | None = None,
    ) -> DailyCollectionRecoveryV1:
        """Close one sweep while projecting any remaining owned retries honestly."""

        self._ensure_writable()
        observed = _aware_shanghai(completed_at, name="completed_at")
        with self._lock:
            self._ensure_open()
            with self._connection:
                row = self._connection.execute(
                    "SELECT * FROM market_watch_daily_recovery_runs WHERE id = ?",
                    (int(run_id),),
                ).fetchone()
                if row is None or row["config_version"] != self.config_version:
                    raise KeyError(f"unknown daily recovery run: {run_id}")
                if row["status"] != DailyRecoveryStatus.RUNNING.value:
                    raise ValueError("daily recovery run is not running")
                trade_date = date.fromisoformat(row["trade_date"])
                completeness = self._read_completeness_locked(trade_date, observed)
                remaining = (
                    completeness.expected_minute_buckets - completeness.accepted_real
                )
                if error is not None:
                    status = DailyRecoveryStatus.FAILED
                    error_code = type(error).__name__
                    error_message = str(error)[:1000] or error_code
                elif not remaining:
                    status = DailyRecoveryStatus.COMPLETE
                    error_code = None
                    error_message = None
                elif completeness.unresolved:
                    status = DailyRecoveryStatus.NEEDS_ATTENTION
                    error_code = None
                    error_message = None
                else:
                    status = DailyRecoveryStatus.RETRYING
                    error_code = None
                    error_message = None
                self._connection.execute(
                    """
                    UPDATE market_watch_daily_recovery_runs
                    SET status = ?, completed_at = ?, accepted_after = ?,
                        reconciled_slots = ?, attempted_slots = ?,
                        remaining_gaps = ?, last_error_code = ?,
                        last_error_message = ?
                    WHERE id = ?
                    """,
                    (
                        status.value,
                        observed.isoformat(timespec="seconds"),
                        completeness.accepted_real,
                        max(0, int(reconciled_slots)),
                        max(int(row["attempted_slots"]), int(attempted_slots), 0),
                        remaining,
                        error_code,
                        error_message,
                        int(run_id),
                    ),
                )
                finished = self._connection.execute(
                    "SELECT * FROM market_watch_daily_recovery_runs WHERE id = ?",
                    (int(run_id),),
                ).fetchone()
                return self._recovery(finished, completeness)

    def _running_recovery_row_locked(self, run_id: int) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM market_watch_daily_recovery_runs WHERE id = ?",
            (int(run_id),),
        ).fetchone()
        if row is None or row["config_version"] != self.config_version:
            raise KeyError(f"unknown daily recovery run: {run_id}")
        if row["status"] != DailyRecoveryStatus.RUNNING.value:
            raise ValueError("daily recovery run is not running")
        return row

    def claim_due(
        self,
        now: datetime,
        *,
        trade_date: date | None = None,
    ) -> tuple[int, CollectionSlotV1] | None:
        self._ensure_writable()
        current = _minute(now, name="now")
        target_date = trade_date or current.date()
        # Ordinary collector cycles own only the current trade date.  A prior
        # date may be reopened solely by an explicit audited daily recovery.
        target_filter = target_date.isoformat()
        self.ensure_expected_slots(target_date)
        now_iso = _aware_shanghai(now, name="now").isoformat(timespec="seconds")
        current_iso = current.isoformat(timespec="seconds")
        with self._lock:
            self._ensure_open()
            with self._connection:
                current_row = self._connection.execute(
                    """
                    SELECT * FROM market_watch_collection_slots
                    WHERE config_version = ? AND trade_date = ? AND minute_bucket = ?
                    """,
                    (
                        self.config_version,
                        current.date().isoformat(),
                        current_iso,
                    ),
                ).fetchone()
                if (
                    target_date == current.date()
                    and current_row is not None
                    and current_row["status"] in {
                    CollectionSlotStatus.CAPTURING.value,
                    CollectionSlotStatus.EXPECTED.value,
                    CollectionSlotStatus.RETRYING.value,
                    }
                ):
                    retry_at = current_row["next_retry_at"]
                    if (
                        current_row["status"] == CollectionSlotStatus.CAPTURING.value
                        or (retry_at is not None and retry_at > now_iso)
                    ):
                        # Never spend the shared source budget on historical
                        # repair while the current minute is in flight or
                        # waiting for its owned retry window.
                        return None
                row = self._connection.execute(
                    """
                    SELECT * FROM market_watch_collection_slots
                    WHERE config_version = ?
                        AND (? IS NULL OR trade_date = ?)
                        AND status IN (?, ?)
                        AND minute_bucket <= ?
                        AND (next_retry_at IS NULL OR next_retry_at <= ?)
                    ORDER BY
                        CASE WHEN minute_bucket = ? THEN 0 ELSE 1 END,
                        trade_date ASC, minute_bucket ASC
                    LIMIT 1
                    """,
                    (
                        self.config_version,
                        target_filter,
                        target_filter,
                        CollectionSlotStatus.EXPECTED.value,
                        CollectionSlotStatus.RETRYING.value,
                        current_iso,
                        now_iso,
                        current_iso,
                    ),
                ).fetchone()
                if row is None:
                    return None
                revision = self._bump_revision_locked()
                attempt_count = int(row["attempt_count"]) + 1
                first_attempt_at = row["first_attempt_at"] or now_iso
                self._connection.execute(
                    """
                    UPDATE market_watch_collection_slots
                    SET status = ?, attempt_count = ?, first_attempt_at = ?,
                        last_attempt_at = ?, next_retry_at = NULL,
                        last_error_code = NULL, last_error_message = NULL,
                        ledger_revision = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        CollectionSlotStatus.CAPTURING.value,
                        attempt_count,
                        first_attempt_at,
                        now_iso,
                        revision,
                        now_iso,
                        row["id"],
                    ),
                )
                attempt = self._connection.execute(
                    """
                    INSERT INTO market_watch_collection_attempts (
                        trade_date, minute_bucket, config_version,
                        attempt_number, repair, outcome, started_at
                    ) VALUES (?, ?, ?, ?, ?, 'started', ?)
                    """,
                    (
                        row["trade_date"],
                        row["minute_bucket"],
                        self.config_version,
                        attempt_count,
                        int(
                            row["minute_bucket"] < current_iso
                            or attempt_count > 1
                            or bool(row["gap_heartbeat"])
                        ),
                        now_iso,
                    ),
                )
                claimed = self._connection.execute(
                    "SELECT * FROM market_watch_collection_slots WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                return int(attempt.lastrowid), self._slot(claimed)

    def mark_failed(
        self,
        attempt_id: int,
        *,
        error: BaseException,
        completed_at: datetime,
        next_retry_at: datetime | None,
    ) -> CollectionSlotV1:
        self._ensure_writable()
        completed = _aware_shanghai(completed_at, name="completed_at")
        retry = (
            _aware_shanghai(next_retry_at, name="next_retry_at")
            if next_retry_at is not None
            else None
        )
        if retry is not None and retry < completed:
            raise ValueError("next_retry_at cannot precede completed_at")
        code = type(error).__name__
        message = str(error)[:1000] or code
        with self._lock:
            self._ensure_open()
            with self._connection:
                attempt, slot = self._attempt_slot_locked(attempt_id)
                self._require_started_attempt(attempt)
                revision = self._bump_revision_locked()
                status = (
                    CollectionSlotStatus.RETRYING
                    if retry is not None
                    else CollectionSlotStatus.UNRESOLVED
                )
                completed_iso = completed.isoformat(timespec="seconds")
                self._connection.execute(
                    """
                    UPDATE market_watch_collection_attempts
                    SET outcome = ?, completed_at = ?, error_code = ?, error_message = ?
                    WHERE id = ?
                    """,
                    (status.value, completed_iso, code, message, attempt_id),
                )
                self._connection.execute(
                    """
                    UPDATE market_watch_collection_slots
                    SET status = ?, next_retry_at = ?, last_error_code = ?,
                        last_error_message = ?, gap_heartbeat = 1,
                        ledger_revision = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status.value,
                        None if retry is None else retry.isoformat(timespec="seconds"),
                        code,
                        message,
                        revision,
                        completed_iso,
                        slot["id"],
                    ),
                )
                return self._slot_by_id_locked(slot["id"])

    def mark_accepted(
        self,
        attempt_id: int,
        snapshot: MarketWatchSnapshotV1,
        *,
        source_snapshot_revision: str,
        completed_at: datetime,
    ) -> CollectionSlotV1:
        self._ensure_writable()
        canonical = MarketWatchSnapshotV1.model_validate(snapshot)
        if canonical.freshness.status in {
            FreshnessStatus.STALE,
            FreshnessStatus.UNAVAILABLE,
        }:
            raise ValueError("stale or unavailable snapshot cannot be accepted as real")
        revision_digest = str(source_snapshot_revision).strip().lower()
        if len(revision_digest) != 64 or any(
            character not in "0123456789abcdef" for character in revision_digest
        ):
            raise ValueError("source_snapshot_revision must be a SHA-256 digest")
        completed = _aware_shanghai(completed_at, name="completed_at")
        with self._lock:
            self._ensure_open()
            with self._connection:
                attempt, slot = self._attempt_slot_locked(attempt_id)
                self._require_started_attempt(attempt)
                target = datetime.fromisoformat(slot["minute_bucket"])
                if _minute(canonical.as_of) != target:
                    raise ValueError("accepted snapshot must exactly match claimed minute")
                repaired = bool(attempt["repair"] or slot["gap_heartbeat"])
                status = (
                    CollectionSlotStatus.REPAIRED
                    if repaired
                    else CollectionSlotStatus.ACCEPTED_REAL
                )
                revision = self._bump_revision_locked()
                completed_iso = completed.isoformat(timespec="seconds")
                self._connection.execute(
                    """
                    UPDATE market_watch_collection_attempts
                    SET outcome = ?, completed_at = ?, source_snapshot_id = ?,
                        source_snapshot_revision = ?
                    WHERE id = ?
                    """,
                    (
                        status.value,
                        completed_iso,
                        canonical.snapshot_id,
                        revision_digest,
                        attempt_id,
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE market_watch_collection_slots
                    SET status = ?, next_retry_at = NULL, accepted_at = ?,
                        source_snapshot_id = ?, source_snapshot_revision = ?,
                        gap_heartbeat = 0,
                        last_error_code = NULL, last_error_message = NULL,
                        ledger_revision = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status.value,
                        completed_iso,
                        canonical.snapshot_id,
                        revision_digest,
                        revision,
                        completed_iso,
                        slot["id"],
                    ),
                )
                return self._slot_by_id_locked(slot["id"])

    def record_gap_heartbeat(
        self,
        minute_bucket: datetime,
        *,
        recorded_at: datetime,
    ) -> CollectionSlotV1:
        self._ensure_writable()
        minute = _minute(minute_bucket)
        if minute not in expected_session_minutes(minute.date()):
            raise ValueError("gap heartbeat must target an expected market-watch minute")
        self.ensure_expected_slots(minute.date())
        updated = _aware_shanghai(recorded_at, name="recorded_at")
        with self._lock:
            self._ensure_open()
            with self._connection:
                row = self._slot_row_locked(minute)
                if row["status"] in _ACCEPTED_STATUSES:
                    return self._slot(row)
                revision = self._bump_revision_locked()
                self._connection.execute(
                    """
                    UPDATE market_watch_collection_slots
                    SET status = ?, gap_heartbeat = 1, ledger_revision = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        CollectionSlotStatus.RETRYING.value,
                        revision,
                        updated.isoformat(timespec="seconds"),
                        row["id"],
                    ),
                )
                return self._slot_by_id_locked(row["id"])

    def recover_inflight(self, now: datetime) -> int:
        self._ensure_writable()
        recovered_at = _aware_shanghai(now, name="now").isoformat(timespec="seconds")
        with self._lock:
            self._ensure_open()
            with self._connection:
                rows = self._connection.execute(
                    """
                    SELECT id FROM market_watch_collection_slots
                    WHERE config_version = ? AND status = ?
                    """,
                    (self.config_version, CollectionSlotStatus.CAPTURING.value),
                ).fetchall()
                if not rows:
                    return 0
                revision = self._bump_revision_locked()
                self._connection.execute(
                    """
                    UPDATE market_watch_collection_slots
                    SET status = ?, next_retry_at = ?, last_error_code = ?,
                        last_error_message = ?, gap_heartbeat = 1,
                        ledger_revision = ?, updated_at = ?
                    WHERE config_version = ? AND status = ?
                    """,
                    (
                        CollectionSlotStatus.RETRYING.value,
                        recovered_at,
                        "CollectorRestarted",
                        "collector stopped before the attempt completed",
                        revision,
                        recovered_at,
                        self.config_version,
                        CollectionSlotStatus.CAPTURING.value,
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE market_watch_collection_attempts
                    SET outcome = 'interrupted', completed_at = ?,
                        error_code = 'CollectorRestarted',
                        error_message = 'collector stopped before the attempt completed'
                    WHERE config_version = ? AND outcome = 'started'
                    """,
                    (recovered_at, self.config_version),
                )
                return len(rows)

    def reconcile_history_record(self, item: Mapping[str, Any]) -> dict[str, Any]:
        """Import one authoritative history row without inventing an attempt.

        Existing strict history predates the ledger.  Fresh/degraded rows become
        accepted real pointers; derived collection-gap rows become heartbeat
        status only.  Other stale rows remain unresolved for the collector.
        """

        self._ensure_writable()
        raw_payload = item.get("payload")
        payload = (
            MarketWatchSnapshotV1.model_validate(raw_payload)
            if raw_payload is not None
            else None
        )
        minute = _minute(
            payload.as_of
            if payload is not None
            else datetime.fromisoformat(str(item.get("minute_bucket") or ""))
        )
        if minute not in expected_session_minutes(minute.date()):
            return {"action": "skipped", "reason": "outside_session"}
        self.ensure_expected_slots(minute.date())
        record_kind = str(item.get("record_kind") or "")
        is_gap = (
            record_kind == "derived_gap_heartbeat"
            if record_kind
            else (
                str(payload.snapshot_id).startswith("mw-heartbeat:")
                if payload is not None
                else str(item.get("snapshot_id") or "").startswith("mw-heartbeat:")
            )
        )
        if is_gap:
            slot = self.record_gap_heartbeat(
                minute,
                recorded_at=datetime.fromisoformat(
                    str(item.get("updated_at") or item.get("recorded_at"))
                ),
            )
            return {"action": "heartbeat", "slot": slot}
        accepted_real = (
            record_kind == "accepted_real"
            if record_kind
            else payload is not None
            and payload.freshness.status
            not in {FreshnessStatus.STALE, FreshnessStatus.UNAVAILABLE}
        )
        if not accepted_real:
            return {"action": "skipped", "reason": "not_accepted_real"}
        digest = str(item.get("payload_digest") or "").strip().lower()
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("history payload_digest must be a SHA-256 digest")
        accepted_at = datetime.fromisoformat(
            str(item.get("updated_at") or item.get("recorded_at"))
        )
        accepted_iso = _aware_shanghai(
            accepted_at,
            name="history accepted_at",
        ).isoformat(timespec="seconds")
        with self._lock:
            self._ensure_open()
            with self._connection:
                row = self._slot_row_locked(minute)
                if (
                    row["status"] in _ACCEPTED_STATUSES
                    and row["source_snapshot_revision"] == digest
                ):
                    return {"action": "unchanged", "slot": self._slot(row)}
                status = (
                    CollectionSlotStatus.REPAIRED
                    if int(row["attempt_count"]) > 0 or bool(row["gap_heartbeat"])
                    else CollectionSlotStatus.ACCEPTED_REAL
                )
                revision = self._bump_revision_locked()
                self._connection.execute(
                    """
                    UPDATE market_watch_collection_slots
                    SET status = ?, next_retry_at = NULL, accepted_at = ?,
                        source_snapshot_id = ?, source_snapshot_revision = ?,
                        gap_heartbeat = 0,
                        last_error_code = NULL, last_error_message = NULL,
                        ledger_revision = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status.value,
                        accepted_iso,
                        (
                            payload.snapshot_id
                            if payload is not None
                            else str(item.get("snapshot_id") or "")
                        ),
                        digest,
                        revision,
                        accepted_iso,
                        row["id"],
                    ),
                )
                if status is CollectionSlotStatus.REPAIRED:
                    self._connection.execute(
                        """
                        UPDATE market_watch_collection_attempts
                        SET outcome = 'reconciled_persisted',
                            source_snapshot_id = ?, source_snapshot_revision = ?
                        WHERE trade_date = ? AND minute_bucket = ?
                            AND config_version = ? AND attempt_number = ?
                        """,
                        (
                            (
                                payload.snapshot_id
                                if payload is not None
                                else str(item.get("snapshot_id") or "")
                            ),
                            digest,
                            minute.date().isoformat(),
                            minute.isoformat(timespec="seconds"),
                            self.config_version,
                            int(row["attempt_count"]),
                        ),
                    )
                return {
                    "action": "imported",
                    "slot": self._slot_by_id_locked(row["id"]),
                }

    def update_runtime(
        self,
        state: CollectorRuntimeState,
        *,
        heartbeat_at: datetime,
    ) -> None:
        self._ensure_writable()
        heartbeat = _aware_shanghai(heartbeat_at, name="heartbeat_at").isoformat(
            timespec="seconds"
        )
        with self._lock:
            self._ensure_open()
            with self._connection:
                self._connection.execute(
                    "UPDATE market_watch_collection_meta SET value = ? WHERE key = 'collector_state'",
                    (CollectorRuntimeState(state).value,),
                )
                self._connection.execute(
                    "UPDATE market_watch_collection_meta SET value = ? WHERE key = 'collector_heartbeat_at'",
                    (heartbeat,),
                )

    def get_slot(self, minute_bucket: datetime) -> CollectionSlotV1 | None:
        minute = _minute(minute_bucket)
        with self._lock:
            if not self._can_read():
                return None
            row = self._connection.execute(
                """
                SELECT * FROM market_watch_collection_slots
                WHERE trade_date = ? AND minute_bucket = ? AND config_version = ?
                """,
                (
                    minute.date().isoformat(),
                    minute.isoformat(timespec="seconds"),
                    self.config_version,
                ),
            ).fetchone()
            return None if row is None else self._slot(row)

    def list_attempts(self, minute_bucket: datetime) -> list[dict[str, Any]]:
        minute = _minute(minute_bucket)
        with self._lock:
            if not self._can_read():
                return []
            rows = self._connection.execute(
                """
                SELECT * FROM market_watch_collection_attempts
                WHERE trade_date = ? AND minute_bucket = ? AND config_version = ?
                ORDER BY id ASC
                """,
                (
                    minute.date().isoformat(),
                    minute.isoformat(timespec="seconds"),
                    self.config_version,
                ),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_recovery_gap_minutes(self, trade_date: date) -> tuple[datetime, ...]:
        """Return exact non-accepted minutes owned by a post-close sweep."""

        with self._lock:
            if not self._can_read():
                return ()
            rows = self._connection.execute(
                """
                SELECT minute_bucket FROM market_watch_collection_slots
                WHERE config_version = ? AND trade_date = ?
                    AND status NOT IN (?, ?, ?)
                ORDER BY minute_bucket ASC
                """,
                (
                    self.config_version,
                    trade_date.isoformat(),
                    *_ACCEPTED_STATUSES,
                    CollectionSlotStatus.EXCLUDED.value,
                ),
            ).fetchall()
        return tuple(datetime.fromisoformat(row["minute_bucket"]) for row in rows)

    def read_completeness(
        self,
        trade_date: date,
        *,
        as_of: datetime,
    ) -> CollectionCompletenessV1:
        observed = _aware_shanghai(as_of, name="as_of")
        with self._lock:
            if not self._can_read():
                return self._empty_completeness(trade_date, observed)
            owns_transaction = not self._connection.in_transaction
            if owns_transaction:
                self._connection.execute("BEGIN")
            try:
                return self._read_completeness_locked(trade_date, observed)
            finally:
                if owns_transaction:
                    self._connection.rollback()

    @staticmethod
    def _empty_completeness(
        trade_date: date,
        observed: datetime,
    ) -> CollectionCompletenessV1:
        return CollectionCompletenessV1(
            trade_date=trade_date,
            as_of=observed,
            expected_minute_buckets=0,
            accepted_real=0,
            repaired=0,
            pending=0,
            retrying=0,
            unresolved=0,
            gap_heartbeat=0,
            ledger_revision=0,
            ledger_digest=stable_sha256([]),
        )

    def _read_completeness_locked(
        self,
        trade_date: date,
        observed: datetime,
    ) -> CollectionCompletenessV1:
        rows = self._connection.execute(
            """
            SELECT status, gap_heartbeat, COUNT(*) AS count
            FROM market_watch_collection_slots
            WHERE trade_date = ? AND config_version = ?
            GROUP BY status, gap_heartbeat
            """,
            (trade_date.isoformat(), self.config_version),
        ).fetchall()
        revision = int(self._meta_locked("ledger_revision"))
        manifest_rows = self._connection.execute(
            """
            SELECT minute_bucket, status, attempt_count,
                source_snapshot_revision, gap_heartbeat, ledger_revision
            FROM market_watch_collection_slots
            WHERE trade_date = ? AND config_version = ?
            ORDER BY minute_bucket ASC
            """,
            (trade_date.isoformat(), self.config_version),
        ).fetchall()
        ledger_digest = stable_sha256(
            [
                {
                    "minute_bucket": row["minute_bucket"],
                    "status": row["status"],
                    "attempt_count": int(row["attempt_count"]),
                    "source_snapshot_revision": row["source_snapshot_revision"],
                    "gap_heartbeat": bool(row["gap_heartbeat"]),
                    "ledger_revision": int(row["ledger_revision"]),
                }
                for row in manifest_rows
            ]
        )
        counts = {status.value: 0 for status in CollectionSlotStatus}
        gap_count = 0
        for row in rows:
            counts[row["status"]] += int(row["count"])
            if (
                row["gap_heartbeat"]
                and row["status"] != CollectionSlotStatus.EXCLUDED.value
            ):
                gap_count += int(row["count"])
        accepted_real = (
            counts[CollectionSlotStatus.ACCEPTED_REAL.value]
            + counts[CollectionSlotStatus.REPAIRED.value]
        )
        return CollectionCompletenessV1(
            trade_date=trade_date,
            as_of=observed,
            expected_minute_buckets=(
                sum(counts.values())
                - counts[CollectionSlotStatus.EXCLUDED.value]
            ),
            accepted_real=accepted_real,
            repaired=counts[CollectionSlotStatus.REPAIRED.value],
            pending=(
                counts[CollectionSlotStatus.EXPECTED.value]
                + counts[CollectionSlotStatus.CAPTURING.value]
            ),
            retrying=counts[CollectionSlotStatus.RETRYING.value],
            unresolved=counts[CollectionSlotStatus.UNRESOLVED.value],
            gap_heartbeat=gap_count,
            ledger_revision=revision,
            ledger_digest=ledger_digest,
        )

    def get_completeness(
        self,
        trade_date: date,
        *,
        as_of: datetime,
    ) -> CollectionCompletenessV1:
        """Compatibility alias for the zero-write completeness reader."""

        return self.read_completeness(trade_date, as_of=as_of)

    def read_latest_daily_recovery(
        self,
        trade_date: date,
        *,
        as_of: datetime,
    ) -> DailyCollectionRecoveryV1 | None:
        observed = _aware_shanghai(as_of, name="as_of")
        with self._lock:
            if not self._can_read() or not self._recovery_table_available_locked():
                return None
            owns_transaction = not self._connection.in_transaction
            if owns_transaction:
                self._connection.execute("BEGIN")
            try:
                completeness = self._read_completeness_locked(trade_date, observed)
                return self._read_latest_recovery_locked(trade_date, completeness)
            finally:
                if owns_transaction:
                    self._connection.rollback()

    def read_envelope(self, *, as_of: datetime) -> MarketWatchCollectorEnvelopeV1:
        observed = _aware_shanghai(as_of, name="as_of")
        with self._lock:
            if not self._can_read():
                return MarketWatchCollectorEnvelopeV1(
                    as_of=observed,
                    collector_state=CollectorRuntimeState.UNKNOWN,
                    collector_heartbeat_at=None,
                    latest_accepted_real=None,
                    collection_cursor=None,
                    latest_published_status=None,
                    collection_completeness=self._empty_completeness(
                        observed.date(), observed
                    ),
                )
            owns_transaction = not self._connection.in_transaction
            if owns_transaction:
                self._connection.execute("BEGIN")
            try:
                accepted = self._connection.execute(
                    """
                    SELECT * FROM market_watch_collection_slots
                    WHERE config_version = ? AND status IN (?, ?)
                    ORDER BY trade_date DESC, minute_bucket DESC LIMIT 1
                    """,
                    (self.config_version, *_ACCEPTED_STATUSES),
                ).fetchone()
                current_minute = _minute(observed)
                cursor = self._connection.execute(
                    """
                    SELECT * FROM market_watch_collection_slots
                    WHERE config_version = ? AND trade_date = ?
                        AND status != ? AND minute_bucket <= ?
                    ORDER BY minute_bucket DESC LIMIT 1
                    """,
                    (
                        self.config_version,
                        observed.date().isoformat(),
                        CollectionSlotStatus.EXCLUDED.value,
                        current_minute.isoformat(timespec="seconds"),
                    ),
                ).fetchone()
                published = self._connection.execute(
                    """
                    SELECT * FROM market_watch_collection_slots
                    WHERE config_version = ?
                        AND status != ?
                        AND (attempt_count > 0 OR gap_heartbeat = 1 OR status IN (?, ?))
                    ORDER BY trade_date DESC, minute_bucket DESC LIMIT 1
                    """,
                    (
                        self.config_version,
                        CollectionSlotStatus.EXCLUDED.value,
                        *_ACCEPTED_STATUSES,
                    ),
                ).fetchone()
                state = CollectorRuntimeState(self._meta_locked("collector_state"))
                heartbeat_raw = self._meta_locked("collector_heartbeat_at")
                completeness = self._read_completeness_locked(
                    observed.date(), observed
                )
                daily_recovery = self._read_latest_recovery_locked(
                    observed.date(), completeness
                )
                pointer = None
                if accepted is not None:
                    pointer = AcceptedSnapshotPointerV1(
                        trade_date=date.fromisoformat(accepted["trade_date"]),
                        minute_bucket=datetime.fromisoformat(accepted["minute_bucket"]),
                        snapshot_id=accepted["source_snapshot_id"],
                        source_snapshot_revision=accepted[
                            "source_snapshot_revision"
                        ],
                        accepted_at=datetime.fromisoformat(accepted["accepted_at"]),
                        repaired=(
                            accepted["status"]
                            == CollectionSlotStatus.REPAIRED.value
                        ),
                    )
                return MarketWatchCollectorEnvelopeV1(
                    as_of=observed,
                    collector_state=state,
                    collector_heartbeat_at=(
                        datetime.fromisoformat(heartbeat_raw)
                        if heartbeat_raw
                        else None
                    ),
                    latest_accepted_real=pointer,
                    collection_cursor=None if cursor is None else self._slot(cursor),
                    latest_published_status=(
                        None if published is None else self._slot(published)
                    ),
                    collection_completeness=completeness,
                    daily_recovery=daily_recovery,
                )
            finally:
                if owns_transaction:
                    self._connection.rollback()

    def get_envelope(self, *, as_of: datetime) -> MarketWatchCollectorEnvelopeV1:
        """Compatibility alias for the zero-write envelope reader."""

        return self.read_envelope(as_of=as_of)

    def _attempt_slot_locked(self, attempt_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
        attempt = self._connection.execute(
            "SELECT * FROM market_watch_collection_attempts WHERE id = ? AND config_version = ?",
            (int(attempt_id), self.config_version),
        ).fetchone()
        if attempt is None:
            raise KeyError(f"unknown collection attempt: {attempt_id}")
        slot = self._connection.execute(
            """
            SELECT * FROM market_watch_collection_slots
            WHERE trade_date = ? AND minute_bucket = ? AND config_version = ?
            """,
            (attempt["trade_date"], attempt["minute_bucket"], self.config_version),
        ).fetchone()
        if slot is None:
            raise RuntimeError("collection attempt has no owning slot")
        return attempt, slot

    def _recovery_table_available_locked(self) -> bool:
        row = self._connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'market_watch_daily_recovery_runs'
            """
        ).fetchone()
        return row is not None

    def _read_latest_recovery_locked(
        self,
        trade_date: date,
        completeness: CollectionCompletenessV1,
    ) -> DailyCollectionRecoveryV1 | None:
        if not self._recovery_table_available_locked():
            return None
        row = self._connection.execute(
            """
            SELECT * FROM market_watch_daily_recovery_runs
            WHERE config_version = ? AND trade_date = ?
            ORDER BY id DESC LIMIT 1
            """,
            (self.config_version, trade_date.isoformat()),
        ).fetchone()
        return None if row is None else self._recovery(row, completeness)

    @staticmethod
    def _require_started_attempt(attempt: sqlite3.Row) -> None:
        if attempt["outcome"] != "started":
            raise ValueError("collection attempt is already complete")

    def _slot_row_locked(self, minute_bucket: datetime) -> sqlite3.Row:
        row = self._connection.execute(
            """
            SELECT * FROM market_watch_collection_slots
            WHERE trade_date = ? AND minute_bucket = ? AND config_version = ?
            """,
            (
                minute_bucket.date().isoformat(),
                minute_bucket.isoformat(timespec="seconds"),
                self.config_version,
            ),
        ).fetchone()
        if row is None:
            raise KeyError(f"collection slot does not exist: {minute_bucket.isoformat()}")
        return row

    def _slot_by_id_locked(self, slot_id: int) -> CollectionSlotV1:
        row = self._connection.execute(
            "SELECT * FROM market_watch_collection_slots WHERE id = ?",
            (slot_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("collection slot disappeared")
        return self._slot(row)

    @staticmethod
    def _recovery(
        row: sqlite3.Row,
        completeness: CollectionCompletenessV1,
    ) -> DailyCollectionRecoveryV1:
        row_keys = set(row.keys())

        def optional(name: str, default=None):
            return row[name] if name in row_keys else default

        stored_status = DailyRecoveryStatus(row["status"])
        accepted_after = completeness.accepted_real
        remaining = completeness.expected_minute_buckets - accepted_after
        status = stored_status
        if stored_status not in {
            DailyRecoveryStatus.PENDING,
            DailyRecoveryStatus.RUNNING,
        }:
            if not remaining:
                status = DailyRecoveryStatus.COMPLETE
            elif stored_status is DailyRecoveryStatus.FAILED:
                status = DailyRecoveryStatus.FAILED
            elif completeness.unresolved:
                status = DailyRecoveryStatus.NEEDS_ATTENTION
            else:
                status = DailyRecoveryStatus.RETRYING
        return DailyCollectionRecoveryV1(
            run_id=int(row["id"]),
            trade_date=date.fromisoformat(row["trade_date"]),
            trigger=DailyRecoveryTrigger(row["trigger"]),
            status=status,
            requested_at=datetime.fromisoformat(row["requested_at"]),
            started_at=(
                datetime.fromisoformat(row["started_at"])
                if row["started_at"]
                else None
            ),
            completed_at=(
                datetime.fromisoformat(row["completed_at"])
                if row["completed_at"]
                else None
            ),
            expected_minute_buckets=completeness.expected_minute_buckets,
            accepted_before=int(row["accepted_before"]),
            accepted_after=accepted_after,
            reconciled_slots=int(row["reconciled_slots"]),
            attempted_slots=int(row["attempted_slots"]),
            failed_attempts=int(optional("failed_attempts", 0) or 0),
            remaining_gaps=remaining,
            manual_action_required=status in {
                DailyRecoveryStatus.NEEDS_ATTENTION,
                DailyRecoveryStatus.FAILED,
            },
            latest_attempt_minute_bucket=(
                datetime.fromisoformat(optional("latest_attempt_minute_bucket"))
                if optional("latest_attempt_minute_bucket")
                else None
            ),
            latest_attempt_at=(
                datetime.fromisoformat(optional("latest_attempt_at"))
                if optional("latest_attempt_at")
                else None
            ),
            latest_attempt_outcome=optional("latest_attempt_outcome"),
            latest_attempt_progress_completed=int(
                optional("latest_attempt_progress_completed", 0) or 0
            ),
            latest_attempt_progress_total=int(
                optional("latest_attempt_progress_total", 0) or 0
            ),
            latest_attempt_progress_stage=optional("latest_attempt_progress_stage"),
            latest_attempt_progress_message=optional("latest_attempt_progress_message"),
            latest_failure_minute_bucket=(
                datetime.fromisoformat(optional("latest_failure_minute_bucket"))
                if optional("latest_failure_minute_bucket")
                else None
            ),
            latest_failure_at=(
                datetime.fromisoformat(optional("latest_failure_at"))
                if optional("latest_failure_at")
                else None
            ),
            latest_failure_next_retry_at=(
                datetime.fromisoformat(optional("latest_failure_next_retry_at"))
                if optional("latest_failure_next_retry_at")
                else None
            ),
            latest_failure_error_code=optional("latest_failure_error_code"),
            latest_failure_error_message=optional("latest_failure_error_message"),
            last_error_code=(
                row["last_error_code"]
                if status is DailyRecoveryStatus.FAILED
                else None
            ),
            last_error_message=(
                optional("last_error_message")
                if status is DailyRecoveryStatus.FAILED
                else None
            ),
        )

    @staticmethod
    def _slot(row: sqlite3.Row) -> CollectionSlotV1:
        return CollectionSlotV1(
            trade_date=date.fromisoformat(row["trade_date"]),
            minute_bucket=datetime.fromisoformat(row["minute_bucket"]),
            status=CollectionSlotStatus(row["status"]),
            attempt_count=int(row["attempt_count"]),
            first_attempt_at=(
                datetime.fromisoformat(row["first_attempt_at"])
                if row["first_attempt_at"]
                else None
            ),
            last_attempt_at=(
                datetime.fromisoformat(row["last_attempt_at"])
                if row["last_attempt_at"]
                else None
            ),
            next_retry_at=(
                datetime.fromisoformat(row["next_retry_at"])
                if row["next_retry_at"]
                else None
            ),
            accepted_at=(
                datetime.fromisoformat(row["accepted_at"])
                if row["accepted_at"]
                else None
            ),
            source_snapshot_id=row["source_snapshot_id"],
            source_snapshot_revision=row["source_snapshot_revision"],
            gap_heartbeat=bool(row["gap_heartbeat"]),
            last_error_code=row["last_error_code"],
            last_error_message=row["last_error_message"],
            ledger_revision=int(row["ledger_revision"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._connection is not None:
                self._connection.close()
            self._closed = True

    def __enter__(self) -> "MarketWatchCollectionStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


__all__ = [
    "COLLECTION_LEDGER_CONTRACT",
    "COLLECTION_LEDGER_SCHEMA_VERSION",
    "MarketWatchCollectionStore",
    "expected_session_minutes",
]
