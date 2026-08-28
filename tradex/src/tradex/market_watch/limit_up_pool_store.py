"""Digest-checked persistence for revision-bound limit-up follow pools."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import zlib
from pathlib import Path

from .integrity import canonical_json_bytes, stable_sha256
from .limit_up_pool import LimitUpFollowPoolV1


ENV_DB_PATH = "TRADEX_LIMIT_UP_POOL_DB"
STORE_CONTRACT = "limit_up_follow_pool_store.v1"
STORE_SCHEMA_VERSION = 1


class LimitUpFollowPoolStore:
    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        configured = db_path or os.environ.get(ENV_DB_PATH)
        if configured is None:
            configured = Path.home() / ".tradex" / "limit_up_follow_pool.sqlite3"
        self.read_only = bool(read_only)
        self._lock = threading.RLock()
        self._closed = False
        self._connection: sqlite3.Connection | None
        if str(configured) == ":memory:":
            if self.read_only:
                raise ValueError("read-only limit-up pool store cannot use :memory:")
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
                CREATE TABLE IF NOT EXISTS limit_up_follow_pool_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS limit_up_follow_pools (
                    source_snapshot_revision TEXT PRIMARY KEY,
                    attribution_revision TEXT NOT NULL UNIQUE,
                    source_snapshot_id TEXT NOT NULL,
                    source_as_of TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    pool_total INTEGER NOT NULL,
                    payload_digest TEXT NOT NULL,
                    payload_blob BLOB NOT NULL
                );
                """
            )
            for key, value in {
                "contract": STORE_CONTRACT,
                "schema_version": str(STORE_SCHEMA_VERSION),
            }.items():
                self._connection.execute(
                    "INSERT OR IGNORE INTO limit_up_follow_pool_meta (key, value) VALUES (?, ?)",
                    (key, value),
                )
                stored = self._connection.execute(
                    "SELECT value FROM limit_up_follow_pool_meta WHERE key = ?",
                    (key,),
                ).fetchone()
                if stored is None or stored["value"] != value:
                    raise RuntimeError(f"incompatible limit-up pool store {key}")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("limit-up pool store is closed")

    def record(self, pool: LimitUpFollowPoolV1) -> dict[str, object]:
        if self.read_only:
            raise RuntimeError("read-only limit-up pool store cannot record")
        canonical = LimitUpFollowPoolV1.model_validate(pool)
        raw = canonical_json_bytes(canonical)
        digest = stable_sha256(canonical)
        blob = zlib.compress(raw, level=6)
        with self._lock:
            self._ensure_open()
            assert self._connection is not None
            existing = self._connection.execute(
                "SELECT attribution_revision FROM limit_up_follow_pools "
                "WHERE source_snapshot_revision = ?",
                (canonical.source_snapshot_revision,),
            ).fetchone()
            action = (
                "unchanged"
                if existing is not None
                and existing["attribution_revision"] == canonical.attribution_revision
                else "inserted"
                if existing is None
                else "updated"
            )
            if action != "unchanged":
                with self._connection:
                    self._connection.execute(
                        """
                        INSERT INTO limit_up_follow_pools (
                            source_snapshot_revision, attribution_revision,
                            source_snapshot_id, source_as_of, trade_date,
                            generated_at, pool_total, payload_digest, payload_blob
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (source_snapshot_revision) DO UPDATE SET
                            attribution_revision = excluded.attribution_revision,
                            source_snapshot_id = excluded.source_snapshot_id,
                            source_as_of = excluded.source_as_of,
                            trade_date = excluded.trade_date,
                            generated_at = excluded.generated_at,
                            pool_total = excluded.pool_total,
                            payload_digest = excluded.payload_digest,
                            payload_blob = excluded.payload_blob
                        """,
                        (
                            canonical.source_snapshot_revision,
                            canonical.attribution_revision,
                            canonical.source_snapshot_id,
                            canonical.source_as_of.isoformat(),
                            canonical.trade_date.isoformat(),
                            canonical.generated_at.isoformat(),
                            canonical.pool_total,
                            digest,
                            blob,
                        ),
                    )
        return {
            "action": action,
            "source_snapshot_revision": canonical.source_snapshot_revision,
            "attribution_revision": canonical.attribution_revision,
            "pool_total": canonical.pool_total,
        }

    def get_by_source_revision(
        self,
        source_snapshot_revision: str,
    ) -> LimitUpFollowPoolV1 | None:
        revision = str(source_snapshot_revision).strip().lower()
        with self._lock:
            self._ensure_open()
            if self._connection is None:
                return None
            try:
                row = self._connection.execute(
                    "SELECT payload_digest, payload_blob FROM limit_up_follow_pools "
                    "WHERE source_snapshot_revision = ?",
                    (revision,),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if self.read_only and "no such table" in str(exc).lower():
                    return None
                raise
        return self._decode(row, expected_source_revision=revision)

    @staticmethod
    def _decode(
        row,
        *,
        expected_source_revision: str | None = None,
    ) -> LimitUpFollowPoolV1 | None:
        if row is None:
            return None
        payload = json.loads(zlib.decompress(row["payload_blob"]).decode("utf-8"))
        canonical = LimitUpFollowPoolV1.model_validate(payload)
        if stable_sha256(canonical) != row["payload_digest"]:
            raise RuntimeError("limit-up pool payload digest mismatch")
        if (
            expected_source_revision is not None
            and canonical.source_snapshot_revision != expected_source_revision
        ):
            raise RuntimeError("limit-up pool source revision mismatch")
        return canonical

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._connection is not None:
                self._connection.close()
            self._closed = True

    def __enter__(self) -> "LimitUpFollowPoolStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


__all__ = [
    "ENV_DB_PATH",
    "STORE_CONTRACT",
    "STORE_SCHEMA_VERSION",
    "LimitUpFollowPoolStore",
]
