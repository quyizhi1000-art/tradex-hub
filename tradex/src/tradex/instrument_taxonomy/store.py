"""Single-writer, read-many SQLite catalog for instrument relationships."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .contracts import StockRelationshipCatalogStatusV1, StockRelationshipProfileV1


ENV_DB_PATH = "TRADEX_INSTRUMENT_TAXONOMY_DB"
STORE_CONTRACT = "instrument_taxonomy_store.v1"
STORE_SCHEMA_VERSION = 1


def default_db_path() -> Path:
    configured = os.environ.get(ENV_DB_PATH)
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.home() / ".tradex" / "instrument_taxonomy.sqlite3").resolve()
    )


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


class InstrumentTaxonomyStore:
    """Sole persistent writer; a refresh replaces one complete revision atomically."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        resolved = Path(db_path).expanduser().resolve() if db_path else default_db_path()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(resolved)
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 10000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS instrument_taxonomy_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS instrument_taxonomy_profiles (
                    instrument_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    primary_business_key TEXT,
                    primary_business_name TEXT,
                    sw_l3_code TEXT,
                    sw_l3_name TEXT,
                    verification_status TEXT NOT NULL,
                    catalog_revision TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_instrument_taxonomy_business
                    ON instrument_taxonomy_profiles (primary_business_key, instrument_id);
                CREATE INDEX IF NOT EXISTS idx_instrument_taxonomy_sw_l3
                    ON instrument_taxonomy_profiles (sw_l3_code, instrument_id);
                CREATE INDEX IF NOT EXISTS idx_instrument_taxonomy_status
                    ON instrument_taxonomy_profiles (verification_status, instrument_id);
                """
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO instrument_taxonomy_meta(key, value) VALUES (?, ?)",
                ("store_contract", STORE_CONTRACT),
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO instrument_taxonomy_meta(key, value) VALUES (?, ?)",
                ("store_schema_version", str(STORE_SCHEMA_VERSION)),
            )

    def replace_catalog(
        self,
        status: StockRelationshipCatalogStatusV1,
        profiles: Iterable[StockRelationshipProfileV1],
    ) -> None:
        canonical_status = StockRelationshipCatalogStatusV1.model_validate(status)
        canonical_profiles = tuple(
            StockRelationshipProfileV1.model_validate(item) for item in profiles
        )
        if len(canonical_profiles) != canonical_status.profile_total:
            raise ValueError("catalog profile count does not match status")
        if len(canonical_profiles) != len({item.instrument_id for item in canonical_profiles}):
            raise ValueError("catalog profiles contain duplicate instruments")
        rows = []
        for profile in canonical_profiles:
            payload_json = _json(profile.model_dump(mode="json"))
            sw = profile.statistical_industry
            rows.append((
                profile.instrument_id,
                profile.name,
                profile.primary_business_key,
                profile.primary_business_name,
                sw.level3_code if sw else None,
                sw.level3_name if sw else None,
                profile.verification_status,
                canonical_status.catalog_revision,
                _digest(payload_json),
                payload_json,
            ))
        status_json = _json(canonical_status.model_dump(mode="json"))
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM instrument_taxonomy_profiles")
            self._connection.executemany(
                """
                INSERT INTO instrument_taxonomy_profiles (
                    instrument_id, name, primary_business_key, primary_business_name,
                    sw_l3_code, sw_l3_name, verification_status, catalog_revision,
                    payload_digest, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO instrument_taxonomy_meta(key, value) VALUES (?, ?)",
                ("catalog_status", status_json),
            )

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._connection.close()

    def __enter__(self) -> "InstrumentTaxonomyStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class InstrumentTaxonomyReader:
    """Read-only catalog view that never creates files, tables, or WALs."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        resolved = Path(db_path).expanduser().resolve() if db_path else default_db_path()
        self.db_path = str(resolved)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        if resolved.is_file():
            self._connection = sqlite3.connect(
                f"file:{resolved.as_posix()}?mode=ro",
                uri=True,
                check_same_thread=False,
                timeout=5,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout = 5000")

    @property
    def available(self) -> bool:
        return self._connection is not None

    def status(self) -> StockRelationshipCatalogStatusV1 | None:
        if self._connection is None:
            return None
        with self._lock:
            try:
                row = self._connection.execute(
                    "SELECT value FROM instrument_taxonomy_meta WHERE key = 'catalog_status'"
                ).fetchone()
            except sqlite3.DatabaseError:
                return None
        return StockRelationshipCatalogStatusV1.model_validate_json(row["value"]) if row else None

    @staticmethod
    def _profile(row: sqlite3.Row | None) -> StockRelationshipProfileV1 | None:
        if row is None:
            return None
        payload_json = str(row["payload_json"])
        if _digest(payload_json) != str(row["payload_digest"]):
            raise ValueError("instrument taxonomy profile digest mismatch")
        return StockRelationshipProfileV1.model_validate_json(payload_json)

    def get(self, instrument_id: str) -> StockRelationshipProfileV1 | None:
        if self._connection is None:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json, payload_digest FROM instrument_taxonomy_profiles WHERE instrument_id = ?",
                (str(instrument_id).strip().upper(),),
            ).fetchone()
        return self._profile(row)

    def get_many(self, instrument_ids: Iterable[str]) -> dict[str, StockRelationshipProfileV1]:
        requested = tuple(dict.fromkeys(str(item).strip().upper() for item in instrument_ids if str(item).strip()))
        if self._connection is None or not requested:
            return {}
        result: dict[str, StockRelationshipProfileV1] = {}
        with self._lock:
            for offset in range(0, len(requested), 500):
                batch = requested[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = self._connection.execute(
                    f"SELECT instrument_id, payload_json, payload_digest FROM instrument_taxonomy_profiles WHERE instrument_id IN ({placeholders})",
                    batch,
                ).fetchall()
                for row in rows:
                    profile = self._profile(row)
                    if profile is not None:
                        result[profile.instrument_id] = profile
        return result

    def all_profiles(self) -> tuple[StockRelationshipProfileV1, ...]:
        if self._connection is None:
            return ()
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json, payload_digest FROM instrument_taxonomy_profiles "
                "ORDER BY instrument_id"
            ).fetchall()
        return tuple(
            profile for row in rows if (profile := self._profile(row)) is not None
        )

    def find_by_name(self, name: str) -> tuple[StockRelationshipProfileV1, ...]:
        """Return exact normalized-name matches without guessing a security."""

        if self._connection is None:
            return ()
        value = "".join(str(name or "").split())
        if not value:
            return ()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json, payload_digest
                FROM instrument_taxonomy_profiles
                WHERE REPLACE(REPLACE(REPLACE(REPLACE(name, ' ', ''),
                    CHAR(9), ''), CHAR(10), ''), CHAR(13), '') = ?
                ORDER BY instrument_id
                """,
                (value,),
            ).fetchall()
        return tuple(
            profile for row in rows if (profile := self._profile(row)) is not None
        )

    def members_by_sw_l3(self, code_or_name: str) -> tuple[StockRelationshipProfileV1, ...]:
        if self._connection is None:
            return ()
        value = str(code_or_name or "").strip()
        if not value:
            return ()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json, payload_digest
                FROM instrument_taxonomy_profiles
                WHERE sw_l3_code = ? OR sw_l3_name = ?
                ORDER BY instrument_id
                """,
                (value, value),
            ).fetchall()
        return tuple(profile for row in rows if (profile := self._profile(row)) is not None)

    def members_by_primary_business(self, key_or_name: str) -> tuple[StockRelationshipProfileV1, ...]:
        if self._connection is None:
            return ()
        value = str(key_or_name or "").strip()
        if not value:
            return ()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json, payload_digest
                FROM instrument_taxonomy_profiles
                WHERE primary_business_key = ? OR primary_business_name = ?
                ORDER BY instrument_id
                """,
                (value, value),
            ).fetchall()
        return tuple(profile for row in rows if (profile := self._profile(row)) is not None)

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> "InstrumentTaxonomyReader":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def read_profiles(instrument_ids: Iterable[str]) -> dict[str, StockRelationshipProfileV1]:
    with InstrumentTaxonomyReader() as reader:
        return reader.get_many(instrument_ids)


__all__ = [
    "ENV_DB_PATH",
    "InstrumentTaxonomyReader",
    "InstrumentTaxonomyStore",
    "default_db_path",
    "read_profiles",
]
