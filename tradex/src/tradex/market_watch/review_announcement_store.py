"""Immutable persistence for candidate-bound official announcements."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import zlib
from datetime import date
from pathlib import Path

from tradex.data_gateway.review_announcement_contracts import (
    ReviewOfficialAnnouncementArchiveV1,
)

from .integrity import canonical_json_bytes, stable_sha256


ENV_DB_PATH = "TRADEX_REVIEW_ANNOUNCEMENTS_DB"
STORE_CONTRACT = "review_official_announcement_store.v1"
STORE_SCHEMA_VERSION = 1


class ReviewOfficialAnnouncementStore:
    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        configured = db_path or os.environ.get(ENV_DB_PATH)
        if configured is None:
            configured = Path.home() / ".tradex" / "review_official_announcements.sqlite3"
        self.read_only = bool(read_only)
        self._lock = threading.RLock()
        self._closed = False
        self._connection: sqlite3.Connection | None
        if str(configured) == ":memory:":
            if self.read_only:
                raise ValueError("read-only announcement store cannot use :memory:")
            self.db_path = ":memory:"
        else:
            resolved = Path(configured).expanduser().resolve()
            self.db_path = str(resolved)
            if self.read_only and not resolved.exists():
                self._connection = None
                return
            if not self.read_only:
                resolved.parent.mkdir(parents=True, exist_ok=True)
        target = (
            f"file:{Path(self.db_path).as_posix()}?mode=ro"
            if self.read_only
            else self.db_path
        )
        self._connection = sqlite3.connect(
            target,
            uri=self.read_only,
            check_same_thread=False,
            timeout=5,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if self.read_only:
            self._connection.execute("PRAGMA query_only = ON")
        else:
            self._initialize()

    def _initialize(self) -> None:
        assert self._connection is not None
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS review_official_announcement_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_official_announcements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    review_id TEXT NOT NULL,
                    candidate_manifest_revision TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    payload_blob BLOB NOT NULL,
                    UNIQUE (trade_date, candidate_manifest_revision, source_revision)
                );
                CREATE INDEX IF NOT EXISTS idx_review_announcements_latest
                    ON review_official_announcements (trade_date DESC, id DESC);
                """
            )
            for key, value in {
                "contract": STORE_CONTRACT,
                "schema_version": str(STORE_SCHEMA_VERSION),
            }.items():
                self._connection.execute(
                    "INSERT OR IGNORE INTO review_official_announcement_meta "
                    "(key, value) VALUES (?, ?)",
                    (key, value),
                )
                stored = self._connection.execute(
                    "SELECT value FROM review_official_announcement_meta WHERE key = ?",
                    (key,),
                ).fetchone()
                if stored is None or stored["value"] != value:
                    raise RuntimeError(f"incompatible announcement store {key}")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("official announcement store is closed")

    def _open_read_only_if_available(self) -> None:
        if self._connection is not None or not self.read_only:
            return
        resolved = Path(self.db_path)
        if not resolved.is_file():
            return
        connection = sqlite3.connect(
            f"file:{resolved.as_posix()}?mode=ro",
            uri=True,
            check_same_thread=False,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA query_only = ON")
        self._connection = connection

    def record(self, value: ReviewOfficialAnnouncementArchiveV1) -> dict[str, object]:
        if self.read_only:
            raise RuntimeError("read-only announcement store cannot record")
        canonical = ReviewOfficialAnnouncementArchiveV1.model_validate(value)
        digest = stable_sha256(canonical)
        blob = zlib.compress(canonical_json_bytes(canonical), level=6)
        with self._lock:
            self._ensure_open()
            assert self._connection is not None
            existing = self._connection.execute(
                "SELECT 1 FROM review_official_announcements WHERE trade_date = ? "
                "AND candidate_manifest_revision = ? AND source_revision = ?",
                (
                    canonical.trade_date.isoformat(),
                    canonical.candidate_manifest_revision,
                    canonical.source_revision,
                ),
            ).fetchone()
            if existing is None:
                with self._connection:
                    self._connection.execute(
                        """
                        INSERT INTO review_official_announcements (
                            trade_date, review_id, candidate_manifest_revision,
                            source_revision, window_end, fetched_at,
                            payload_digest, payload_blob
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            canonical.trade_date.isoformat(),
                            canonical.review_id,
                            canonical.candidate_manifest_revision,
                            canonical.source_revision,
                            canonical.window_end.isoformat(),
                            canonical.metadata.fetched_at.isoformat(),
                            digest,
                            blob,
                        ),
                    )
        return {
            "action": "existing" if existing is not None else "inserted",
            "trade_date": canonical.trade_date.isoformat(),
            "review_id": canonical.review_id,
            "source_revision": canonical.source_revision,
            "candidate_count": len(canonical.candidates),
            "announcement_count": len(canonical.announcements),
        }

    def get(self, trade_date_value: date | str) -> ReviewOfficialAnnouncementArchiveV1 | None:
        target = (
            trade_date_value.isoformat()
            if isinstance(trade_date_value, date)
            else date.fromisoformat(str(trade_date_value)).isoformat()
        )
        with self._lock:
            self._ensure_open()
            self._open_read_only_if_available()
            if self._connection is None:
                return None
            try:
                row = self._connection.execute(
                    "SELECT payload_digest, payload_blob FROM review_official_announcements "
                    "WHERE trade_date = ? ORDER BY id DESC LIMIT 1",
                    (target,),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if self.read_only and "no such table" in str(exc).lower():
                    return None
                raise
        if row is None:
            return None
        payload = json.loads(zlib.decompress(row["payload_blob"]).decode("utf-8"))
        canonical = ReviewOfficialAnnouncementArchiveV1.model_validate(payload)
        if stable_sha256(canonical) != row["payload_digest"]:
            raise RuntimeError("official announcement payload digest mismatch")
        return canonical

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._connection is not None:
                self._connection.close()
            self._closed = True

    def __enter__(self) -> "ReviewOfficialAnnouncementStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


__all__ = ["ENV_DB_PATH", "ReviewOfficialAnnouncementStore"]
