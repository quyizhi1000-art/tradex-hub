"""Immutable daily persistence for Collector-owned limit sentiment."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import zlib
from datetime import date
from pathlib import Path

from tradex.data_gateway.limit_sentiment_contracts import LimitSentimentDailyV1

from .integrity import canonical_json_bytes, stable_sha256


ENV_DB_PATH = "TRADEX_LIMIT_SENTIMENT_DB"
STORE_CONTRACT = "limit_sentiment_store.v1"
STORE_SCHEMA_VERSION = 1


class LimitSentimentStore:
    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        configured = db_path or os.environ.get(ENV_DB_PATH)
        if configured is None:
            configured = Path.home() / ".tradex" / "limit_sentiment.sqlite3"
        self.read_only = bool(read_only)
        self._lock = threading.RLock()
        self._closed = False
        self._connection: sqlite3.Connection | None
        if str(configured) == ":memory:":
            if self.read_only:
                raise ValueError("read-only limit sentiment store cannot use :memory:")
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
        if not self.read_only:
            self._initialize()

    def _initialize(self) -> None:
        assert self._connection is not None
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS limit_sentiment_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS limit_sentiment_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    payload_blob BLOB NOT NULL,
                    UNIQUE (trade_date, source_revision)
                );
                CREATE INDEX IF NOT EXISTS idx_limit_sentiment_latest
                    ON limit_sentiment_daily (trade_date DESC, id DESC);
                """
            )
            for key, value in {
                "contract": STORE_CONTRACT,
                "schema_version": str(STORE_SCHEMA_VERSION),
            }.items():
                self._connection.execute(
                    "INSERT OR IGNORE INTO limit_sentiment_meta (key, value) VALUES (?, ?)",
                    (key, value),
                )
                stored = self._connection.execute(
                    "SELECT value FROM limit_sentiment_meta WHERE key = ?",
                    (key,),
                ).fetchone()
                if stored is None or stored["value"] != value:
                    raise RuntimeError(f"incompatible limit sentiment store {key}")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("limit sentiment store is closed")

    def record(self, value: LimitSentimentDailyV1) -> dict[str, object]:
        if self.read_only:
            raise RuntimeError("read-only limit sentiment store cannot record")
        canonical = LimitSentimentDailyV1.model_validate(value)
        digest = stable_sha256(canonical)
        blob = zlib.compress(canonical_json_bytes(canonical), level=6)
        with self._lock:
            self._ensure_open()
            assert self._connection is not None
            existing = self._connection.execute(
                "SELECT 1 FROM limit_sentiment_daily "
                "WHERE trade_date = ? AND source_revision = ?",
                (canonical.trade_date.isoformat(), canonical.source_revision),
            ).fetchone()
            if existing is None:
                with self._connection:
                    self._connection.execute(
                        """
                        INSERT INTO limit_sentiment_daily (
                            trade_date, source_revision, fetched_at,
                            payload_digest, payload_blob
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            canonical.trade_date.isoformat(),
                            canonical.source_revision,
                            canonical.metadata.fetched_at.isoformat(),
                            digest,
                            blob,
                        ),
                    )
        return {
            "action": "existing" if existing is not None else "inserted",
            "trade_date": canonical.trade_date.isoformat(),
            "source_revision": canonical.source_revision,
            "limit_up_count": canonical.limit_up_count,
            "broken_count": canonical.broken_count,
        }

    def get(self, trade_date_value: date | str) -> LimitSentimentDailyV1 | None:
        target = (
            trade_date_value.isoformat()
            if isinstance(trade_date_value, date)
            else date.fromisoformat(str(trade_date_value)).isoformat()
        )
        with self._lock:
            self._ensure_open()
            if self._connection is None:
                return None
            try:
                row = self._connection.execute(
                    "SELECT payload_digest, payload_blob FROM limit_sentiment_daily "
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
        canonical = LimitSentimentDailyV1.model_validate(payload)
        if stable_sha256(canonical) != row["payload_digest"]:
            raise RuntimeError("limit sentiment payload digest mismatch")
        return canonical

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._connection is not None:
                self._connection.close()
            self._closed = True

    def __enter__(self) -> "LimitSentimentStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


__all__ = [
    "ENV_DB_PATH",
    "LimitSentimentStore",
    "STORE_CONTRACT",
    "STORE_SCHEMA_VERSION",
]
