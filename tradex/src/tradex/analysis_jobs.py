"""Persistent queue and materialized display artifacts for offline analysis.

The Dashboard may enqueue a user command and read status/artifacts.  Only the
independent analysis worker claims jobs, runs domain services and publishes
display-ready results.  Keeping this ledger provider-neutral prevents a page
read from becoming an implicit refresh or analysis request.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping


JOB_CONTRACT = "tradex_analysis_job.v1"
JOB_SCHEMA_VERSION = 1
ARTIFACT_CONTRACT = "tradex_analysis_artifact.v1"
ARTIFACT_SCHEMA_VERSION = 1
RUNTIME_CONTRACT = "tradex_analysis_worker_status.v1"
RUNTIME_SCHEMA_VERSION = 1
ENV_DB_PATH = "TRADEX_ANALYSIS_DB"

POST_MARKET_REVIEW = "post_market_review"
DAILY_STOCK_SELECTION = "daily_stock_selection"
MARKET_WATCH_EVALUATION = "market_watch_evaluation"
MANUAL_PORTFOLIO_OUTLOOK = "manual_portfolio_outlook"
CAPABILITIES = frozenset(
    {
        POST_MARKET_REVIEW,
        DAILY_STOCK_SELECTION,
        MARKET_WATCH_EVALUATION,
        MANUAL_PORTFOLIO_OUTLOOK,
    }
)
ACTIVE_STATES = ("queued", "running")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    current = value or _utc_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("analysis timestamps must be timezone-aware")
    return current.astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _capability(value: str) -> str:
    normalized = str(value).strip()
    if normalized not in CAPABILITIES:
        raise ValueError(f"unsupported analysis capability: {normalized}")
    return normalized


def _trade_date(value: date | str) -> str:
    if isinstance(value, datetime):
        raise TypeError("trade_date must be a date or ISO date string")
    return (value if isinstance(value, date) else date.fromisoformat(str(value))).isoformat()


def _db_path(db_path: str | os.PathLike[str] | None = None) -> str:
    configured = db_path or os.environ.get(ENV_DB_PATH)
    if configured is None:
        configured = Path.home() / ".tradex" / "analysis_jobs.sqlite3"
    if str(configured) == ":memory:":
        return ":memory:"
    return str(Path(configured).expanduser().resolve())


class AnalysisStateUnavailable(LookupError):
    """The worker-owned analysis ledger is not yet available for reading."""


class AnalysisJobStore:
    """SQLite/WAL owner for commands, worker health and display artifacts."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = _db_path(db_path)
        if self.db_path != ":memory:":
            resolved = Path(self.db_path)
            resolved.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=5,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if self.db_path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS analysis_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL UNIQUE,
                    capability TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    state TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    error TEXT,
                    failure_code TEXT,
                    failed_phase TEXT,
                    result_json TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_jobs_active
                    ON analysis_jobs (capability, scope_key)
                    WHERE state IN ('queued', 'running');

                CREATE INDEX IF NOT EXISTS idx_analysis_jobs_latest
                    ON analysis_jobs (capability, trade_date DESC, id DESC);

                CREATE TABLE IF NOT EXISTS analysis_artifacts (
                    capability TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    artifact_contract TEXT NOT NULL,
                    artifact_schema_version INTEGER NOT NULL,
                    source_revision TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    PRIMARY KEY (capability, scope_key)
                );

                CREATE TABLE IF NOT EXISTS analysis_runtime_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("analysis job store is closed")

    @staticmethod
    def _job(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "contract": JOB_CONTRACT,
            "schema_version": JOB_SCHEMA_VERSION,
            "job_id": row["job_id"],
            "capability": row["capability"],
            "scope_key": row["scope_key"],
            "trade_date": row["trade_date"],
            "trigger": row["trigger"],
            "state": row["state"],
            "phase": row["phase"],
            "requested_at": row["requested_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "updated_at": row["updated_at"],
            "error": row["error"],
            "failure_code": row["failure_code"],
            "failed_phase": row["failed_phase"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
        }

    def enqueue(
        self,
        capability: str,
        *,
        trade_date: date | str,
        trigger: str,
        scope_key: str | None = None,
        requested_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Create or reuse the sole active job for a capability/scope."""

        normalized_capability = _capability(capability)
        normalized_date = _trade_date(trade_date)
        normalized_scope = str(scope_key or f"date:{normalized_date}").strip()
        normalized_trigger = str(trigger).strip()
        if not normalized_scope or not normalized_trigger:
            raise ValueError("analysis job scope and trigger must not be empty")
        timestamp = _iso(requested_at)
        job_id = f"{normalized_capability}:{uuid.uuid4().hex}"
        with self._lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    """
                    SELECT * FROM analysis_jobs
                    WHERE capability = ? AND scope_key = ?
                      AND state IN ('queued', 'running')
                    ORDER BY id DESC LIMIT 1
                    """,
                    (normalized_capability, normalized_scope),
                ).fetchone()
                if row is None:
                    self._connection.execute(
                        """
                        INSERT INTO analysis_jobs (
                            job_id, capability, scope_key, trade_date, trigger,
                            state, phase, requested_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'queued', 'queued', ?, ?)
                        """,
                        (
                            job_id,
                            normalized_capability,
                            normalized_scope,
                            normalized_date,
                            normalized_trigger,
                            timestamp,
                            timestamp,
                        ),
                    )
                    row = self._connection.execute(
                        "SELECT * FROM analysis_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        job = self._job(row)
        if job is None:
            raise RuntimeError("analysis job enqueue did not produce a row")
        return job

    def latest_job(
        self,
        capability: str,
        *,
        trade_date: date | str | None = None,
        scope_key: str | None = None,
    ) -> dict[str, Any] | None:
        normalized_capability = _capability(capability)
        clauses = ["capability = ?"]
        params: list[Any] = [normalized_capability]
        if trade_date is not None:
            clauses.append("trade_date = ?")
            params.append(_trade_date(trade_date))
        if scope_key is not None:
            clauses.append("scope_key = ?")
            params.append(str(scope_key))
        query = (
            "SELECT * FROM analysis_jobs WHERE "
            + " AND ".join(clauses)
            + " ORDER BY id DESC LIMIT 1"
        )
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(query, params).fetchone()
        return self._job(row)

    def recover_interrupted(self) -> int:
        """Return interrupted worker-owned jobs to the queue for idempotent retry."""

        timestamp = _iso()
        with self._lock, self._connection:
            self._ensure_open()
            cursor = self._connection.execute(
                """
                UPDATE analysis_jobs
                SET state = 'queued', phase = 'queued', started_at = NULL,
                    updated_at = ?, error = NULL, failure_code = NULL,
                    failed_phase = NULL
                WHERE state = 'running'
                """,
                (timestamp,),
            )
        return max(cursor.rowcount, 0)

    def claim_next(self) -> dict[str, Any] | None:
        timestamp = _iso()
        with self._lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    """
                    SELECT * FROM analysis_jobs
                    WHERE state = 'queued'
                    ORDER BY id ASC LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return None
                self._connection.execute(
                    """
                    UPDATE analysis_jobs
                    SET state = 'running', phase = 'starting', started_at = ?,
                        updated_at = ?, finished_at = NULL, error = NULL,
                        failure_code = NULL, failed_phase = NULL, result_json = NULL
                    WHERE job_id = ? AND state = 'queued'
                    """,
                    (timestamp, timestamp, row["job_id"]),
                )
                claimed = self._connection.execute(
                    "SELECT * FROM analysis_jobs WHERE job_id = ?",
                    (row["job_id"],),
                ).fetchone()
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return self._job(claimed)

    def set_phase(self, job_id: str, phase: str) -> None:
        normalized_phase = str(phase).strip()
        if not normalized_phase:
            raise ValueError("analysis job phase must not be empty")
        with self._lock, self._connection:
            self._ensure_open()
            self._connection.execute(
                """
                UPDATE analysis_jobs SET phase = ?, updated_at = ?
                WHERE job_id = ? AND state = 'running'
                """,
                (normalized_phase, _iso(), str(job_id)),
            )

    def succeed(self, job_id: str, *, result: Mapping[str, Any] | None = None) -> None:
        timestamp = _iso()
        result_json = _json(dict(result)) if result is not None else None
        with self._lock, self._connection:
            self._ensure_open()
            cursor = self._connection.execute(
                """
                UPDATE analysis_jobs
                SET state = 'succeeded', phase = 'completed', finished_at = ?,
                    updated_at = ?, error = NULL, failure_code = NULL,
                    failed_phase = NULL, result_json = ?
                WHERE job_id = ? AND state = 'running'
                """,
                (timestamp, timestamp, result_json, str(job_id)),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("analysis job is not running")

    def fail(
        self,
        job_id: str,
        *,
        error: str,
        failure_code: str,
    ) -> None:
        timestamp = _iso()
        with self._lock, self._connection:
            self._ensure_open()
            cursor = self._connection.execute(
                """
                UPDATE analysis_jobs
                SET state = 'failed', failed_phase = phase, phase = 'failed',
                    finished_at = ?, updated_at = ?, error = ?, failure_code = ?
                WHERE job_id = ? AND state = 'running'
                """,
                (
                    timestamp,
                    timestamp,
                    str(error),
                    str(failure_code),
                    str(job_id),
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("analysis job is not running")

    def put_artifact(
        self,
        capability: str,
        *,
        scope_key: str,
        source_revision: str,
        payload: Mapping[str, Any],
        generated_at: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_capability = _capability(capability)
        normalized_scope = str(scope_key).strip()
        normalized_revision = str(source_revision).strip()
        if not normalized_scope or not normalized_revision:
            raise ValueError("artifact scope and source revision must not be empty")
        material = dict(payload)
        payload_json = _json(material)
        payload_digest = _digest(material)
        timestamp = _iso(generated_at)
        with self._lock, self._connection:
            self._ensure_open()
            self._connection.execute(
                """
                INSERT INTO analysis_artifacts (
                    capability, scope_key, artifact_contract,
                    artifact_schema_version, source_revision, payload_digest,
                    payload_json, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (capability, scope_key) DO UPDATE SET
                    artifact_contract = excluded.artifact_contract,
                    artifact_schema_version = excluded.artifact_schema_version,
                    source_revision = excluded.source_revision,
                    payload_digest = excluded.payload_digest,
                    payload_json = excluded.payload_json,
                    generated_at = excluded.generated_at
                """,
                (
                    normalized_capability,
                    normalized_scope,
                    ARTIFACT_CONTRACT,
                    ARTIFACT_SCHEMA_VERSION,
                    normalized_revision,
                    payload_digest,
                    payload_json,
                    timestamp,
                ),
            )
        return self.get_artifact(normalized_capability, scope_key=normalized_scope) or {}

    def get_artifact(
        self,
        capability: str,
        *,
        scope_key: str,
    ) -> dict[str, Any] | None:
        normalized_capability = _capability(capability)
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT * FROM analysis_artifacts
                WHERE capability = ? AND scope_key = ?
                """,
                (normalized_capability, str(scope_key)),
            ).fetchone()
        if row is None:
            return None
        return {
            "contract": ARTIFACT_CONTRACT,
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "capability": row["capability"],
            "scope_key": row["scope_key"],
            "source_revision": row["source_revision"],
            "payload_digest": row["payload_digest"],
            "generated_at": row["generated_at"],
            "payload": json.loads(row["payload_json"]),
        }

    def set_runtime_state(
        self,
        state: str,
        *,
        heartbeat_at: datetime | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        timestamp = _iso(heartbeat_at)
        values = {
            "state": str(state),
            "heartbeat_at": timestamp,
            "process_pid": str(os.getpid()),
            "detail": str(detail or ""),
        }
        with self._lock, self._connection:
            self._ensure_open()
            for key, value in values.items():
                self._connection.execute(
                    """
                    INSERT INTO analysis_runtime_meta (key, value) VALUES (?, ?)
                    ON CONFLICT (key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )
        return self.runtime_status()

    def runtime_status(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT key, value FROM analysis_runtime_meta"
            ).fetchall()
        values = {row["key"]: row["value"] for row in rows}
        return {
            "contract": RUNTIME_CONTRACT,
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "state": values.get("state", "unknown"),
            "heartbeat_at": values.get("heartbeat_at"),
            "process_pid": int(values["process_pid"]) if values.get("process_pid") else None,
            "detail": values.get("detail") or None,
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    def __enter__(self) -> "AnalysisJobStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class AnalysisJobCommandWriter:
    """Enqueue explicit Web commands without owning schema or artifacts."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = _db_path(db_path)
        if self.db_path == ":memory:" or not Path(self.db_path).is_file():
            raise AnalysisStateUnavailable("后台分析状态尚未建立")
        self._lock = threading.RLock()
        self._closed = False
        try:
            self._connection = sqlite3.connect(
                f"{Path(self.db_path).as_uri()}?mode=rw",
                uri=True,
                check_same_thread=False,
                timeout=5,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout = 5000")
        except sqlite3.DatabaseError as exc:
            raise AnalysisStateUnavailable("后台分析状态暂不可写") from exc

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("analysis command writer is closed")

    def enqueue(
        self,
        capability: str,
        *,
        trade_date: date | str,
        trigger: str,
        scope_key: str | None = None,
        requested_at: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_capability = _capability(capability)
        normalized_date = _trade_date(trade_date)
        normalized_scope = str(scope_key or f"date:{normalized_date}").strip()
        normalized_trigger = str(trigger).strip()
        if not normalized_scope or not normalized_trigger:
            raise ValueError("analysis job scope and trigger must not be empty")
        timestamp = _iso(requested_at)
        job_id = f"{normalized_capability}:{uuid.uuid4().hex}"
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    """
                    SELECT * FROM analysis_jobs
                    WHERE capability = ? AND scope_key = ?
                      AND state IN ('queued', 'running')
                    ORDER BY id DESC LIMIT 1
                    """,
                    (normalized_capability, normalized_scope),
                ).fetchone()
                if row is None:
                    self._connection.execute(
                        """
                        INSERT INTO analysis_jobs (
                            job_id, capability, scope_key, trade_date, trigger,
                            state, phase, requested_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'queued', 'queued', ?, ?)
                        """,
                        (
                            job_id,
                            normalized_capability,
                            normalized_scope,
                            normalized_date,
                            normalized_trigger,
                            timestamp,
                            timestamp,
                        ),
                    )
                    row = self._connection.execute(
                        "SELECT * FROM analysis_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                self._connection.commit()
            except sqlite3.DatabaseError as exc:
                self._connection.rollback()
                raise AnalysisStateUnavailable("后台分析任务暂不能排队") from exc
        job = AnalysisJobStore._job(row)
        if job is None:
            raise RuntimeError("analysis job enqueue did not produce a row")
        return job

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    def __enter__(self) -> "AnalysisJobCommandWriter":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class AnalysisJobReader:
    """Strictly read worker-owned job status and display artifacts.

    The reader never creates a directory, database, table or WAL.  A missing or
    incomplete ledger is an honest not-ready state for Web GET handlers.
    """

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = _db_path(db_path)
        if self.db_path == ":memory:" or not Path(self.db_path).is_file():
            raise AnalysisStateUnavailable("后台分析状态尚未建立")
        self._lock = threading.RLock()
        self._closed = False
        try:
            self._connection = sqlite3.connect(
                f"{Path(self.db_path).as_uri()}?mode=ro",
                uri=True,
                check_same_thread=False,
                timeout=5,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA query_only = ON")
        except sqlite3.DatabaseError as exc:
            raise AnalysisStateUnavailable("后台分析状态暂不可读") from exc

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("analysis job reader is closed")

    def _fetchone(self, query: str, params: list[Any] | tuple[Any, ...]):
        with self._lock:
            self._ensure_open()
            try:
                return self._connection.execute(query, params).fetchone()
            except sqlite3.DatabaseError as exc:
                raise AnalysisStateUnavailable("后台分析状态暂不可读") from exc

    def _fetchall(self, query: str):
        with self._lock:
            self._ensure_open()
            try:
                return self._connection.execute(query).fetchall()
            except sqlite3.DatabaseError as exc:
                raise AnalysisStateUnavailable("后台分析状态暂不可读") from exc

    def latest_job(
        self,
        capability: str,
        *,
        trade_date: date | str | None = None,
        scope_key: str | None = None,
    ) -> dict[str, Any] | None:
        normalized_capability = _capability(capability)
        clauses = ["capability = ?"]
        params: list[Any] = [normalized_capability]
        if trade_date is not None:
            clauses.append("trade_date = ?")
            params.append(_trade_date(trade_date))
        if scope_key is not None:
            clauses.append("scope_key = ?")
            params.append(str(scope_key))
        row = self._fetchone(
            "SELECT * FROM analysis_jobs WHERE "
            + " AND ".join(clauses)
            + " ORDER BY id DESC LIMIT 1",
            params,
        )
        return AnalysisJobStore._job(row)

    def get_artifact(
        self,
        capability: str,
        *,
        scope_key: str,
    ) -> dict[str, Any] | None:
        normalized_capability = _capability(capability)
        row = self._fetchone(
            """
            SELECT * FROM analysis_artifacts
            WHERE capability = ? AND scope_key = ?
            """,
            (normalized_capability, str(scope_key)),
        )
        if row is None:
            return None
        return {
            "contract": ARTIFACT_CONTRACT,
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "capability": row["capability"],
            "scope_key": row["scope_key"],
            "source_revision": row["source_revision"],
            "payload_digest": row["payload_digest"],
            "generated_at": row["generated_at"],
            "payload": json.loads(row["payload_json"]),
        }

    def runtime_status(self) -> dict[str, Any]:
        rows = self._fetchall("SELECT key, value FROM analysis_runtime_meta")
        values = {row["key"]: row["value"] for row in rows}
        return {
            "contract": RUNTIME_CONTRACT,
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "state": values.get("state", "unknown"),
            "heartbeat_at": values.get("heartbeat_at"),
            "process_pid": int(values["process_pid"]) if values.get("process_pid") else None,
            "detail": values.get("detail") or None,
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    def __enter__(self) -> "AnalysisJobReader":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


__all__ = [
    "ACTIVE_STATES",
    "AnalysisJobCommandWriter",
    "AnalysisJobReader",
    "AnalysisJobStore",
    "AnalysisStateUnavailable",
    "DAILY_STOCK_SELECTION",
    "ENV_DB_PATH",
    "MARKET_WATCH_EVALUATION",
    "POST_MARKET_REVIEW",
]
