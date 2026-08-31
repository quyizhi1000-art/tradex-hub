"""Durable manual portfolio entries and collector-owned materialized reads."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .contracts import (
    MAX_ENABLED_INSTRUMENTS,
    ManualPortfolioAlertV1,
    ManualPortfolioEntryV1,
    ManualPortfolioMarketSnapshotV1,
)


ENV_DB_PATH = "TRADEX_MANUAL_PORTFOLIO_DB"


def default_db_path() -> Path:
    configured = os.environ.get(ENV_DB_PATH)
    return Path(configured).expanduser().resolve() if configured else (
        Path.home() / ".tradex" / "manual_portfolio.sqlite3"
    ).resolve()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class ManualPortfolioStore:
    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        resolved = Path(db_path).expanduser().resolve() if db_path else default_db_path()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(resolved)
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._initialize()

    def _initialize(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS manual_portfolio_entries (
                    instrument_id TEXT PRIMARY KEY,
                    display_name TEXT,
                    note TEXT,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    code_validation_status TEXT NOT NULL,
                    attribution_status TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revision TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS manual_portfolio_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_revision TEXT NOT NULL UNIQUE,
                    portfolio_revision TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_manual_portfolio_snapshots_latest
                    ON manual_portfolio_snapshots (id DESC);
                CREATE TABLE IF NOT EXISTS manual_portfolio_alerts (
                    alert_id TEXT PRIMARY KEY,
                    instrument_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    emitted_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_manual_portfolio_alerts_latest
                    ON manual_portfolio_alerts (instrument_id, emitted_at DESC);
                """
            )

    @staticmethod
    def _entry(row: sqlite3.Row) -> ManualPortfolioEntryV1:
        return ManualPortfolioEntryV1(
            instrument_id=row["instrument_id"],
            display_name=row["display_name"],
            note=row["note"],
            enabled=bool(row["enabled"]),
            code_validation_status=row["code_validation_status"],
            attribution_status=row["attribution_status"],
            added_at=datetime.fromisoformat(row["added_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            revision=row["revision"],
        )

    def list_entries(self, *, enabled_only: bool = False) -> tuple[ManualPortfolioEntryV1, ...]:
        clause = " WHERE enabled = 1" if enabled_only else ""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM manual_portfolio_entries" + clause + " ORDER BY added_at, instrument_id"
            ).fetchall()
        return tuple(self._entry(row) for row in rows)

    def get_entry(self, instrument_id: str) -> ManualPortfolioEntryV1 | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM manual_portfolio_entries WHERE instrument_id = ?",
                (instrument_id,),
            ).fetchone()
        return self._entry(row) if row else None

    def put_entry(self, entry: ManualPortfolioEntryV1, *, require_absent: bool = False) -> ManualPortfolioEntryV1:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT instrument_id FROM manual_portfolio_entries WHERE instrument_id = ?",
                    (entry.instrument_id,),
                ).fetchone()
                if require_absent and existing is not None:
                    raise ValueError("证券代码已在持仓观察中")
                if entry.enabled:
                    enabled_count = self._connection.execute(
                        "SELECT COUNT(*) AS count FROM manual_portfolio_entries WHERE enabled = 1 AND instrument_id != ?",
                        (entry.instrument_id,),
                    ).fetchone()["count"]
                    if enabled_count >= MAX_ENABLED_INSTRUMENTS:
                        raise ValueError(
                            f"最多只能启用 {MAX_ENABLED_INSTRUMENTS} 个证券代码"
                        )
                self._connection.execute(
                    """
                    INSERT INTO manual_portfolio_entries (
                        instrument_id, display_name, note, enabled,
                        code_validation_status, attribution_status,
                        added_at, updated_at, revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(instrument_id) DO UPDATE SET
                        display_name=excluded.display_name,
                        note=excluded.note,
                        enabled=excluded.enabled,
                        code_validation_status=excluded.code_validation_status,
                        attribution_status=excluded.attribution_status,
                        updated_at=excluded.updated_at,
                        revision=excluded.revision
                    """,
                    (
                        entry.instrument_id,
                        entry.display_name,
                        entry.note,
                        int(entry.enabled),
                        entry.code_validation_status,
                        entry.attribution_status,
                        entry.added_at.isoformat(),
                        entry.updated_at.isoformat(),
                        entry.revision,
                    ),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return self.get_entry(entry.instrument_id) or entry

    def delete_entry(self, instrument_id: str) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM manual_portfolio_entries WHERE instrument_id = ?",
                (instrument_id,),
            )
        return cursor.rowcount == 1

    def record_snapshot(self, snapshot: ManualPortfolioMarketSnapshotV1) -> ManualPortfolioMarketSnapshotV1:
        payload = snapshot.model_dump(mode="json")
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO manual_portfolio_snapshots (
                    snapshot_revision, portfolio_revision, generated_at, payload_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_revision,
                    snapshot.portfolio_revision,
                    snapshot.generated_at.isoformat(),
                    _json(payload),
                ),
            )
            for alert in snapshot.alerts:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO manual_portfolio_alerts (
                        alert_id, instrument_id, direction, emitted_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        alert.alert_id,
                        alert.instrument_id,
                        alert.direction,
                        alert.observed_at.isoformat(),
                        _json(alert.model_dump(mode="json")),
                    ),
                )
        return snapshot

    def latest_snapshot(self) -> ManualPortfolioMarketSnapshotV1 | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM manual_portfolio_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return ManualPortfolioMarketSnapshotV1.model_validate_json(row["payload_json"]) if row else None

    def latest_alert(self, instrument_id: str, direction: str) -> ManualPortfolioAlertV1 | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT payload_json FROM manual_portfolio_alerts
                WHERE instrument_id = ? AND direction = ?
                ORDER BY emitted_at DESC LIMIT 1
                """,
                (instrument_id, direction),
            ).fetchone()
        return ManualPortfolioAlertV1.model_validate_json(row["payload_json"]) if row else None

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._connection.close()

    def __enter__(self) -> "ManualPortfolioStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class ManualPortfolioReader:
    """Read-only view that never creates portfolio state."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        resolved = Path(db_path).expanduser().resolve() if db_path else default_db_path()
        self.db_path = str(resolved)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._closed = False
        self._connect_if_available()

    def _connect_if_available(self) -> None:
        """Attach lazily when a long-running worker predates the first entry."""

        with self._lock:
            if self._closed or self._connection is not None:
                return
            resolved = Path(self.db_path)
            if not resolved.is_file():
                return
            try:
                self._connection = sqlite3.connect(
                    f"file:{resolved.as_posix()}?mode=ro", uri=True, check_same_thread=False, timeout=5
                )
                self._connection.row_factory = sqlite3.Row
                self._connection.execute("PRAGMA query_only = ON")
            except sqlite3.DatabaseError:
                self._connection = None

    @property
    def available(self) -> bool:
        self._connect_if_available()
        return self._connection is not None

    def list_entries(self, *, enabled_only: bool = False) -> tuple[ManualPortfolioEntryV1, ...]:
        self._connect_if_available()
        if self._connection is None:
            return ()
        clause = " WHERE enabled = 1" if enabled_only else ""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM manual_portfolio_entries" + clause + " ORDER BY added_at, instrument_id"
            ).fetchall()
        return tuple(ManualPortfolioStore._entry(row) for row in rows)

    def latest_snapshot(self) -> ManualPortfolioMarketSnapshotV1 | None:
        self._connect_if_available()
        if self._connection is None:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM manual_portfolio_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return ManualPortfolioMarketSnapshotV1.model_validate_json(row["payload_json"]) if row else None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> "ManualPortfolioReader":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def portfolio_revision(entries: Iterable[ManualPortfolioEntryV1]) -> str:
    return digest([item.model_dump(mode="json") for item in entries])


__all__ = [
    "ENV_DB_PATH",
    "ManualPortfolioReader",
    "ManualPortfolioStore",
    "default_db_path",
    "digest",
    "portfolio_revision",
]
