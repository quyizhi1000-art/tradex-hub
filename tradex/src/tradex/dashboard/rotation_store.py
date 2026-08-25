"""Compressed SQLite persistence for the intraday rotation radar."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import zlib
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from .rotation_radar import (
    ROTATION_CONFIG_VERSION,
    ROTATION_SCHEMA_VERSION,
    analyze_rotation_snapshots,
    analyze_sector_flow_snapshots,
    normalize_rotation_snapshot,
)


DEFAULT_MAX_POINTS = 256
DEFAULT_REPLAY_STEPS = 40
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_STORING_PHASES = frozenset({"opening_observation", "trading"})
_COMPACT_FIELDS = (
    "taxonomy",
    "board_code",
    "name",
    "change_pct",
    "up_count",
    "down_count",
    "flow_amount",
    "flow_ratio",
    "flow_rank",
    "provider_as_of",
    "source",
)


def _as_shanghai(value: datetime | str | None, *, default_now: bool = False) -> datetime:
    if value is None:
        if not default_now:
            raise ValueError("timestamp is required")
        return datetime.now(_SHANGHAI)
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    return parsed.astimezone(_SHANGHAI)


def _minute_iso(value: datetime | str) -> str:
    return _as_shanghai(value).replace(second=0, microsecond=0).isoformat(timespec="seconds")


def _received_iso(value: datetime | str | None) -> str:
    return _as_shanghai(value, default_now=True).isoformat(timespec="seconds")


def _trade_date(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else date.fromisoformat(str(value)).isoformat()


def _compact_payload(snapshot: Mapping[str, Any]) -> bytes:
    rows = [
        [record.get(field) for field in _COMPACT_FIELDS]
        for record in snapshot.get("boards", [])
    ]
    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return zlib.compress(raw, level=6)


def _expand_payload(payload: bytes, minute_bucket: str, market_phase: str) -> dict[str, Any]:
    rows = json.loads(zlib.decompress(payload).decode("utf-8"))
    industry: list[dict[str, Any]] = []
    concept: list[dict[str, Any]] = []
    for row in rows:
        record = dict(zip(_COMPACT_FIELDS, row))
        taxonomy = record.pop("taxonomy")
        target = industry if taxonomy == "industry" else concept
        target.append(record)
    return normalize_rotation_snapshot(
        industry,
        concept,
        minute_bucket=minute_bucket,
        market_phase=market_phase,
    )


def _provider_digest(snapshot: Mapping[str, Any]) -> str:
    material = [
        (
            record.get("taxonomy"),
            record.get("board_code"),
            record.get("source"),
            record.get("provider_as_of"),
        )
        for record in snapshot.get("boards", [])
    ]
    encoded = json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RotationRadarStore:
    """Thread-safe minute snapshots with deterministic 40-step replay."""

    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        *,
        max_points: int = DEFAULT_MAX_POINTS,
        replay_steps: int = DEFAULT_REPLAY_STEPS,
    ) -> None:
        if max_points < 1:
            raise ValueError("max_points must be positive")
        if replay_steps < 1:
            raise ValueError("replay_steps must be positive")
        configured = db_path or os.environ.get("TRADEX_ROTATION_DB")
        if configured is None:
            configured = Path.home() / ".tradex" / "rotation_radar.sqlite3"
        self.db_path = str(configured)
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.max_points = int(max_points)
        self.replay_steps = int(replay_steps)
        self._lock = threading.RLock()
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
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS rotation_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    minute_bucket TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    config_version TEXT NOT NULL,
                    market_phase TEXT NOT NULL,
                    provider_digest TEXT NOT NULL,
                    board_count INTEGER NOT NULL,
                    payload_bytes INTEGER NOT NULL,
                    payload_blob BLOB NOT NULL,
                    received_at TEXT NOT NULL,
                    UNIQUE (trade_date, minute_bucket, schema_version, config_version)
                );
                CREATE INDEX IF NOT EXISTS idx_rotation_lookup
                    ON rotation_snapshots (
                        trade_date, schema_version, config_version, minute_bucket
                    );
                """
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "RotationRadarStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def record_snapshot(
        self,
        *,
        trade_date: date | str,
        minute_bucket: datetime | str,
        industry_records: Iterable[Mapping[str, Any]],
        concept_records: Iterable[Mapping[str, Any]],
        sources: Mapping[str, str] | None = None,
        market_phase: str = "trading",
        received_at: datetime | str | None = None,
        config_version: str = ROTATION_CONFIG_VERSION,
        supplemental_points: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        normalized_date = _trade_date(trade_date)
        minute = _minute_iso(minute_bucket)
        if minute[:10] != normalized_date:
            raise ValueError("minute_bucket must belong to trade_date")
        version = str(config_version).strip()
        if not version:
            raise ValueError("config_version must not be empty")
        phase = str(market_phase)
        if phase not in _STORING_PHASES:
            current = self.get_current(
                normalized_date,
                version,
                supplemental_points=supplemental_points,
            )
            current["status"] = "paused" if phase in {"pre_open", "midday_break"} else "closed"
            current["status_label"] = "轮动观察暂停" if current["status"] == "paused" else "轮动信号已定格"
            return {"inserted": False, "reason": current["status"], "current": current}

        snapshot = normalize_rotation_snapshot(
            industry_records,
            concept_records,
            minute_bucket=minute,
            sources=sources,
            market_phase=phase,
        )
        payload = _compact_payload(snapshot)
        digest = _provider_digest(snapshot)
        received = _received_iso(received_at)

        with self._lock:
            existing = self._connection.execute(
                """
                SELECT id FROM rotation_snapshots
                WHERE trade_date = ? AND minute_bucket = ?
                    AND schema_version = ? AND config_version = ?
                """,
                (normalized_date, minute, ROTATION_SCHEMA_VERSION, version),
            ).fetchone()
            if existing is not None:
                return {
                    "inserted": False,
                    "reason": "duplicate_minute",
                    "current": self._get_current_locked(
                        normalized_date,
                        version,
                        supplemental_points=supplemental_points,
                    ),
                }
            latest = self._connection.execute(
                """
                SELECT minute_bucket, provider_digest FROM rotation_snapshots
                WHERE trade_date = ? AND schema_version = ? AND config_version = ?
                ORDER BY minute_bucket DESC LIMIT 1
                """,
                (normalized_date, ROTATION_SCHEMA_VERSION, version),
            ).fetchone()
            if latest is not None and minute < latest["minute_bucket"]:
                return {
                    "inserted": False,
                    "reason": "out_of_order",
                    "current": self._get_current_locked(
                        normalized_date,
                        version,
                        supplemental_points=supplemental_points,
                    ),
                }
            if latest is not None and digest == latest["provider_digest"]:
                return {
                    "inserted": False,
                    "reason": "duplicate_provider_digest",
                    "current": self._get_current_locked(
                        normalized_date,
                        version,
                        supplemental_points=supplemental_points,
                    ),
                }

            self._connection.execute(
                """
                INSERT INTO rotation_snapshots (
                    trade_date, minute_bucket, schema_version, config_version,
                    market_phase, provider_digest, board_count, payload_bytes,
                    payload_blob, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_date,
                    minute,
                    ROTATION_SCHEMA_VERSION,
                    version,
                    phase,
                    digest,
                    len(snapshot["boards"]),
                    len(payload),
                    payload,
                    received,
                ),
            )
            self._connection.execute(
                """
                DELETE FROM rotation_snapshots
                WHERE id IN (
                    SELECT id FROM rotation_snapshots
                    WHERE trade_date = ? AND schema_version = ? AND config_version = ?
                    ORDER BY minute_bucket DESC LIMIT -1 OFFSET ?
                )
                """,
                (normalized_date, ROTATION_SCHEMA_VERSION, version, self.max_points),
            )
            self._connection.execute(
                """
                DELETE FROM rotation_snapshots
                WHERE trade_date NOT IN (
                    SELECT trade_date FROM rotation_snapshots
                    GROUP BY trade_date ORDER BY trade_date DESC LIMIT 2
                )
                """
            )
            self._connection.commit()
            current = self._get_current_locked(
                normalized_date,
                version,
                supplemental_points=supplemental_points,
            )
            current["storage"] = {
                "board_count": len(snapshot["boards"]),
                "payload_bytes": len(payload),
            }
            return {"inserted": True, "reason": None, "current": current}

    def _rows(self, trade_date: str, config_version: str) -> list[sqlite3.Row]:
        return self._connection.execute(
            """
            SELECT * FROM rotation_snapshots
            WHERE trade_date = ? AND schema_version = ? AND config_version = ?
            ORDER BY minute_bucket ASC
            """,
            (trade_date, ROTATION_SCHEMA_VERSION, config_version),
        ).fetchall()

    def _decoded_snapshots(self, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [
            _expand_payload(row["payload_blob"], row["minute_bucket"], row["market_phase"])
            for row in rows
        ]

    def _recent_decoded_snapshots(
        self, decoded: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        effective_seen = {"industry": 0, "concept": 0}
        start = 0
        for index in range(len(decoded) - 1, -1, -1):
            for taxonomy in effective_seen:
                coverage = decoded[index].get("coverage", {}).get(taxonomy, {})
                total = int(coverage.get("total", 0))
                effective = int(coverage.get("effective", 0))
                if total and effective / total >= 0.60:
                    effective_seen[taxonomy] += 1
            start = index
            if all(count >= self.replay_steps for count in effective_seen.values()):
                break
        return decoded[start:] if decoded else []

    def _recent_snapshots(self, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return self._recent_decoded_snapshots(self._decoded_snapshots(rows))

    def _get_current_locked(
        self,
        trade_date: str,
        config_version: str,
        *,
        supplemental_points: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        rows = self._rows(trade_date, config_version)
        decoded = self._decoded_snapshots(rows)
        snapshots = self._recent_decoded_snapshots(decoded)
        result = analyze_rotation_snapshots(snapshots, config_version=config_version)
        # Lifecycle replay stays bounded, while the fund-flow chart needs the
        # complete current trading session.  Both views consume the same
        # decoded owner snapshot and never trigger another provider request.
        result["sector_flow_trajectory"] = analyze_sector_flow_snapshots(
            decoded,
            supplemental_points,
        )
        result["offense_sector_flow_trajectory"] = analyze_sector_flow_snapshots(
            decoded,
            supplemental_points,
            direction="offense",
        )
        if rows:
            latest = rows[-1]
            result["storage"] = {
                "board_count": latest["board_count"],
                "payload_bytes": latest["payload_bytes"],
                "stored_points": len(rows),
                "replayed_points": len(snapshots),
            }
        else:
            result["storage"] = {
                "board_count": 0,
                "payload_bytes": 0,
                "stored_points": 0,
                "replayed_points": 0,
            }
        return result

    def get_current(
        self,
        trade_date: date | str,
        config_version: str = ROTATION_CONFIG_VERSION,
        *,
        supplemental_points: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        normalized_date = _trade_date(trade_date)
        with self._lock:
            return self._get_current_locked(
                normalized_date,
                str(config_version),
                supplemental_points=supplemental_points,
            )

    def get_snapshot_stats(
        self,
        trade_date: date | str,
        config_version: str = ROTATION_CONFIG_VERSION,
    ) -> list[dict[str, Any]]:
        normalized_date = _trade_date(trade_date)
        with self._lock:
            rows = self._rows(normalized_date, str(config_version))
            return [
                {
                    "minute_bucket": row["minute_bucket"],
                    "schema_version": row["schema_version"],
                    "config_version": row["config_version"],
                    "market_phase": row["market_phase"],
                    "provider_digest": row["provider_digest"],
                    "board_count": row["board_count"],
                    "payload_bytes": row["payload_bytes"],
                    "received_at": row["received_at"],
                }
                for row in rows
            ]


__all__ = [
    "DEFAULT_MAX_POINTS",
    "DEFAULT_REPLAY_STEPS",
    "RotationRadarStore",
]
