"""Digest-checked persistence for independently revisioned resonance batches."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import zlib
from datetime import datetime, timedelta
from pathlib import Path

from .integrity import canonical_json_bytes, stable_sha256
from .sector_resonance import SectorResonanceBatchV1


ENV_DB_PATH = "TRADEX_SECTOR_RESONANCE_DB"
STORE_CONTRACT = "sector_resonance_store.v1"
STORE_SCHEMA_VERSION = 1


class SectorResonanceStore:
    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        configured = db_path or os.environ.get(ENV_DB_PATH)
        if configured is None:
            configured = Path.home() / ".tradex" / "sector_resonance.sqlite3"
        self.read_only = bool(read_only)
        self._lock = threading.RLock()
        self._closed = False
        self._connection: sqlite3.Connection | None
        if str(configured) == ":memory:":
            if self.read_only:
                raise ValueError("read-only resonance store cannot use :memory:")
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
                CREATE TABLE IF NOT EXISTS sector_resonance_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sector_resonance_batches (
                    source_snapshot_revision TEXT PRIMARY KEY,
                    resonance_revision TEXT NOT NULL UNIQUE,
                    source_snapshot_id TEXT NOT NULL,
                    source_as_of TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    entry_count INTEGER NOT NULL,
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
                    "INSERT OR IGNORE INTO sector_resonance_meta (key, value) VALUES (?, ?)",
                    (key, value),
                )
                stored = self._connection.execute(
                    "SELECT value FROM sector_resonance_meta WHERE key = ?",
                    (key,),
                ).fetchone()
                if stored is None or stored["value"] != value:
                    raise RuntimeError(f"incompatible sector-resonance store {key}")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("sector resonance store is closed")

    def record(self, batch: SectorResonanceBatchV1) -> dict[str, object]:
        if self.read_only:
            raise RuntimeError("read-only resonance store cannot record")
        canonical = SectorResonanceBatchV1.model_validate(batch)
        raw = canonical_json_bytes(canonical)
        digest = stable_sha256(canonical)
        blob = zlib.compress(raw, level=6)
        with self._lock:
            self._ensure_open()
            assert self._connection is not None
            existing = self._connection.execute(
                "SELECT resonance_revision FROM sector_resonance_batches "
                "WHERE source_snapshot_revision = ?",
                (canonical.source_snapshot_revision,),
            ).fetchone()
            action = (
                "unchanged"
                if existing is not None
                and existing["resonance_revision"] == canonical.resonance_revision
                else "inserted"
                if existing is None
                else "updated"
            )
            if action != "unchanged":
                with self._connection:
                    self._connection.execute(
                        """
                        INSERT INTO sector_resonance_batches (
                            source_snapshot_revision, resonance_revision,
                            source_snapshot_id, source_as_of, generated_at,
                            entry_count, payload_digest, payload_blob
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (source_snapshot_revision) DO UPDATE SET
                            resonance_revision = excluded.resonance_revision,
                            source_snapshot_id = excluded.source_snapshot_id,
                            source_as_of = excluded.source_as_of,
                            generated_at = excluded.generated_at,
                            entry_count = excluded.entry_count,
                            payload_digest = excluded.payload_digest,
                            payload_blob = excluded.payload_blob
                        """,
                        (
                            canonical.source_snapshot_revision,
                            canonical.resonance_revision,
                            canonical.source_snapshot_id,
                            canonical.source_as_of.isoformat(),
                            canonical.generated_at.isoformat(),
                            len(canonical.entries),
                            digest,
                            blob,
                        ),
                    )
        return {
            "action": action,
            "source_snapshot_revision": canonical.source_snapshot_revision,
            "resonance_revision": canonical.resonance_revision,
            "entry_count": len(canonical.entries),
        }

    def get_by_source_revision(
        self,
        source_snapshot_revision: str,
    ) -> SectorResonanceBatchV1 | None:
        with self._lock:
            self._ensure_open()
            if self._connection is None:
                return None
            try:
                row = self._connection.execute(
                    "SELECT payload_digest, payload_blob FROM sector_resonance_batches "
                    "WHERE source_snapshot_revision = ?",
                    (str(source_snapshot_revision).strip().lower(),),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if self.read_only and "no such table" in str(exc).lower():
                    return None
                raise
        return self._decode(row, expected_source_revision=source_snapshot_revision)

    def get_latest_before(
        self,
        as_of: datetime,
        *,
        max_age_minutes: int = 6,
    ) -> SectorResonanceBatchV1 | None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("resonance lookup time must include a timezone")
        if not 1 <= max_age_minutes <= 30:
            raise ValueError("max_age_minutes must be between 1 and 30")
        earliest = as_of - timedelta(minutes=max_age_minutes)
        with self._lock:
            self._ensure_open()
            if self._connection is None:
                return None
            try:
                row = self._connection.execute(
                    "SELECT payload_digest, payload_blob FROM sector_resonance_batches "
                    "WHERE source_as_of <= ? AND source_as_of >= ? "
                    "ORDER BY source_as_of DESC, generated_at DESC LIMIT 1",
                    (as_of.isoformat(), earliest.isoformat()),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if self.read_only and "no such table" in str(exc).lower():
                    return None
                raise
        return self._decode(row)

    @staticmethod
    def _decode(
        row,
        *,
        expected_source_revision: str | None = None,
    ) -> SectorResonanceBatchV1 | None:
        if row is None:
            return None
        payload = json.loads(zlib.decompress(row["payload_blob"]).decode("utf-8"))
        canonical = SectorResonanceBatchV1.model_validate(payload)
        if stable_sha256(canonical) != row["payload_digest"]:
            raise RuntimeError("sector resonance payload digest mismatch")
        if (
            expected_source_revision is not None
            and canonical.source_snapshot_revision != expected_source_revision
        ):
            raise RuntimeError("sector resonance source revision mismatch")
        return canonical

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._connection is not None:
                self._connection.close()
            self._closed = True

    def __enter__(self) -> "SectorResonanceStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


__all__ = [
    "ENV_DB_PATH",
    "STORE_CONTRACT",
    "STORE_SCHEMA_VERSION",
    "SectorResonanceStore",
]
