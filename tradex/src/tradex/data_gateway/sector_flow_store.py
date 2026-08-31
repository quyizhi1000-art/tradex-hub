"""Success-only persistent cache for canonical sector intraday flow curves."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import zlib
from collections.abc import Iterable, Mapping
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
                CREATE TABLE IF NOT EXISTS sector_flow_targets (
                    trade_date TEXT NOT NULL,
                    sector_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    taxonomy TEXT NOT NULL,
                    provider_sector_code TEXT NOT NULL,
                    source_family TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, sector_key)
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

    def record_target(
        self,
        series: SectorFundFlowIntradayV1,
        provider_sector_code: str,
    ) -> None:
        canonical = SectorFundFlowIntradayV1.model_validate(series)
        self.record_target_identities(
            canonical.trading_date,
            ({
                "sector_key": canonical.sector_key,
                "name": canonical.name,
                "taxonomy": canonical.taxonomy,
                "provider_sector_code": provider_sector_code,
                "source_family": canonical.metadata.provider,
            },),
        )

    def record_target_identities(
        self,
        trading_date: date,
        targets: Iterable[Mapping[str, Any]],
    ) -> int:
        """Persist resolved target identities before their serial curve sweep."""

        normalized: list[tuple[str, str, str, str, str]] = []
        seen_keys: set[str] = set()
        for target in targets:
            sector_key = str(target.get("sector_key") or "").strip()
            name = str(target.get("name") or "").strip()
            taxonomy = str(target.get("taxonomy") or "").strip()
            code = str(target.get("provider_sector_code") or "").strip().upper()
            source_family = str(target.get("source_family") or "eastmoney").strip()
            if not sector_key or not name:
                raise ValueError("sector fund-flow target lacks identity")
            if sector_key in seen_keys:
                raise ValueError("sector fund-flow target identities must be unique")
            if taxonomy not in {"industry", "concept"}:
                raise ValueError("sector fund-flow target taxonomy is invalid")
            if not code.startswith("BK") or not code[2:].isdigit():
                raise ValueError("sector fund-flow target code must match BK plus digits")
            if not source_family:
                raise ValueError("sector fund-flow target source family is required")
            seen_keys.add(sector_key)
            normalized.append((sector_key, name, taxonomy, code, source_family))
        if not normalized:
            return 0
        updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            self._ensure_open()
            with self._connection:
                self._connection.executemany(
                    """
                    INSERT INTO sector_flow_targets (
                        trade_date, sector_key, name, taxonomy,
                        provider_sector_code, source_family, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (trade_date, sector_key) DO UPDATE SET
                        name = excluded.name,
                        taxonomy = excluded.taxonomy,
                        provider_sector_code = excluded.provider_sector_code,
                        source_family = excluded.source_family,
                        updated_at = excluded.updated_at
                    """,
                    [
                        (trading_date.isoformat(), *target, updated_at)
                        for target in normalized
                    ],
                )
        return len(normalized)

    def get_targets(self, trading_date: date) -> tuple[dict[str, str], ...]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT sector_key, name, taxonomy, provider_sector_code,
                    source_family
                FROM sector_flow_targets
                WHERE trade_date = ?
                ORDER BY sector_key ASC
                """,
                (trading_date.isoformat(),),
            ).fetchall()
        return tuple(dict(row) for row in rows)

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
        return self._decode(row)

    def get_all_best(
        self,
        trading_date: date,
    ) -> dict[str, SectorFundFlowIntradayV1]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT * FROM sector_flow_curves
                WHERE trade_date = ?
                ORDER BY sector_key ASC, point_count DESC,
                    last_provider_as_of DESC, fetched_at DESC, provider ASC
                """,
                (trading_date.isoformat(),),
            ).fetchall()
        result: dict[str, SectorFundFlowIntradayV1] = {}
        for row in rows:
            sector_key = str(row["sector_key"])
            if sector_key not in result:
                result[sector_key] = self._decode(row)
        return result

    @staticmethod
    def _decode(row: sqlite3.Row) -> SectorFundFlowIntradayV1:
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
