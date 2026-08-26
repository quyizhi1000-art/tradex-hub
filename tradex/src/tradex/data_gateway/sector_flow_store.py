"""Success-only persistent cache for canonical sector intraday flow curves."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import zlib
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .contracts import SectorFundFlowIntradayV1


ENV_DB_PATH = "TRADEX_SECTOR_FLOW_DB"
STORE_CONTRACT = "sector_intraday_fund_flow_store.v1"
STORE_SCHEMA_VERSION = 1


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class SectorFundFlowStore:
    """Store the best validated curve per day, sector and source family."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        configured = db_path or os.environ.get(ENV_DB_PATH)
        if configured is None:
            configured = Path.home() / ".tradex" / "sector_flow.sqlite3"
        if str(configured) == ":memory:":
            self.db_path = ":memory:"
        else:
            resolved = Path(configured).expanduser().resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(resolved)
        self._lock = threading.RLock()
        self._closed = False
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
        except Exception:
            self._connection.close()
            self._closed = True
            raise

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sector_flow_store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sector_flow_curves (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    sector_key TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    point_count INTEGER NOT NULL,
                    first_provider_as_of TEXT NOT NULL,
                    last_provider_as_of TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    payload_bytes INTEGER NOT NULL,
                    payload_blob BLOB NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (trade_date, sector_key, provider, contract, schema_version)
                );
                CREATE INDEX IF NOT EXISTS idx_sector_flow_curve_best
                    ON sector_flow_curves (
                        trade_date, sector_key, point_count DESC,
                        last_provider_as_of DESC, fetched_at DESC
                    );
                """
            )
            expected = {
                "contract": STORE_CONTRACT,
                "schema_version": str(STORE_SCHEMA_VERSION),
            }
            for key, value in expected.items():
                self._connection.execute(
                    "INSERT OR IGNORE INTO sector_flow_store_meta (key, value) VALUES (?, ?)",
                    (key, value),
                )
                stored = self._connection.execute(
                    "SELECT value FROM sector_flow_store_meta WHERE key = ?",
                    (key,),
                ).fetchone()
                if stored is None or stored["value"] != value:
                    raise RuntimeError(f"incompatible sector-flow store {key}")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("sector fund-flow store is closed")

    def record(self, series: SectorFundFlowIntradayV1) -> dict[str, Any]:
        canonical = SectorFundFlowIntradayV1.model_validate(series)
        payload = canonical.model_dump(mode="json")
        raw = _canonical_bytes(payload)
        digest = hashlib.sha256(raw).hexdigest()
        blob = zlib.compress(raw, level=6)
        provider = canonical.metadata.provider
        first_at = canonical.points[0].provider_as_of.isoformat()
        last_at = canonical.points[-1].provider_as_of.isoformat()
        fetched_at = canonical.metadata.fetched_at.isoformat()
        updated_at = datetime.now(canonical.metadata.fetched_at.tzinfo).isoformat(
            timespec="seconds"
        )
        with self._lock:
            self._ensure_open()
            with self._connection:
                existing = self._connection.execute(
                    """
                    SELECT * FROM sector_flow_curves
                    WHERE trade_date = ? AND sector_key = ? AND provider = ?
                        AND contract = ? AND schema_version = ?
                    """,
                    (
                        canonical.trading_date.isoformat(),
                        canonical.sector_key,
                        provider,
                        canonical.metadata.contract,
                        canonical.metadata.schema_version,
                    ),
                ).fetchone()
                if existing is not None:
                    existing_rank = (
                        int(existing["point_count"]),
                        existing["last_provider_as_of"],
                        existing["fetched_at"],
                    )
                    candidate_rank = (len(canonical.points), last_at, fetched_at)
                    if candidate_rank < existing_rank:
                        return {
                            "action": "preserved",
                            "payload_digest": existing["payload_digest"],
                            "point_count": existing["point_count"],
                        }
                    if existing["payload_digest"] == digest:
                        return {
                            "action": "unchanged",
                            "payload_digest": digest,
                            "point_count": len(canonical.points),
                        }
                action = "inserted" if existing is None else "updated"
                self._connection.execute(
                    """
                    INSERT INTO sector_flow_curves (
                        trade_date, sector_key, provider, contract, schema_version,
                        point_count, first_provider_as_of, last_provider_as_of,
                        fetched_at, payload_digest, payload_bytes, payload_blob,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (
                        trade_date, sector_key, provider, contract, schema_version
                    ) DO UPDATE SET
                        point_count = excluded.point_count,
                        first_provider_as_of = excluded.first_provider_as_of,
                        last_provider_as_of = excluded.last_provider_as_of,
                        fetched_at = excluded.fetched_at,
                        payload_digest = excluded.payload_digest,
                        payload_bytes = excluded.payload_bytes,
                        payload_blob = excluded.payload_blob,
                        updated_at = excluded.updated_at
                    """,
                    (
                        canonical.trading_date.isoformat(),
                        canonical.sector_key,
                        provider,
                        canonical.metadata.contract,
                        canonical.metadata.schema_version,
                        len(canonical.points),
                        first_at,
                        last_at,
                        fetched_at,
                        digest,
                        len(blob),
                        blob,
                        updated_at,
                    ),
                )
                return {
                    "action": action,
                    "payload_digest": digest,
                    "point_count": len(canonical.points),
                }

    def get_best(
        self,
        trading_date: date,
        sector_key: str,
    ) -> SectorFundFlowIntradayV1 | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT * FROM sector_flow_curves
                WHERE trade_date = ? AND sector_key = ?
                ORDER BY point_count DESC, last_provider_as_of DESC,
                    fetched_at DESC, provider ASC
                LIMIT 1
                """,
                (trading_date.isoformat(), str(sector_key)),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(zlib.decompress(row["payload_blob"]).decode("utf-8"))
        canonical = SectorFundFlowIntradayV1.model_validate(payload)
        if hashlib.sha256(_canonical_bytes(canonical.model_dump(mode="json"))).hexdigest() != row[
            "payload_digest"
        ]:
            raise RuntimeError("sector fund-flow payload digest mismatch")
        return canonical

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def __enter__(self) -> "SectorFundFlowStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


__all__ = [
    "ENV_DB_PATH",
    "STORE_CONTRACT",
    "STORE_SCHEMA_VERSION",
    "SectorFundFlowStore",
]
