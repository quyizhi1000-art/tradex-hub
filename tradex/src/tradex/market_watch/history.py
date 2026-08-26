"""Persistent, provider-neutral history for ``market_watch.v1`` snapshots.

The history store is deliberately below the dashboard layer.  It receives only
the strict canonical contract, stores one complete snapshot per Shanghai
minute, and keeps alert emissions in an append-only event table so a later
same-minute snapshot update cannot erase an earlier notification.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import zlib
from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import model_validator

from tradex.market_calendar import CalendarDayStatus, calendar_day_status

from .contracts import (
    AlertV1,
    ContractModel,
    FreshnessStatus,
    GuardrailSeverity,
    MarketPhase,
    MarketRegime,
    MarketStateV1,
    MarketWatchSnapshotV1,
)
from .policy import DEFAULT_MARKET_WATCH_POLICY


HISTORY_CONTRACT = "market_watch_history.v1"
HISTORY_SCHEMA_VERSION = 1
REPLAY_SAMPLE_CONTRACT = "market_watch_replay_sample.v1"
REPLAY_SAMPLE_SCHEMA_VERSION = 1
DEFAULT_CONFIG_VERSION = DEFAULT_MARKET_WATCH_POLICY.config_version
DEFAULT_RETENTION_TRADE_DAYS = (
    DEFAULT_MARKET_WATCH_POLICY.history_retention_trade_days
)
DEFAULT_DATE_LIMIT = 90
ENV_DB_PATH = "TRADEX_MARKET_WATCH_DB"

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class ReplayFreshnessV1(ContractModel):
    """Freshness fields required by replay display and evaluation only."""

    status: FreshnessStatus


class ReplayGuardrailV1(ContractModel):
    """Guardrail fields required by replay display and evaluation only."""

    regime: MarketRegime
    severity: GuardrailSeverity


class MarketWatchReplaySampleV1(ContractModel):
    """Lossless metadata projection for bounded replay and evaluation."""

    contract: Literal["market_watch_replay_sample.v1"] = REPLAY_SAMPLE_CONTRACT
    schema_version: Literal[1] = REPLAY_SAMPLE_SCHEMA_VERSION
    snapshot_id: str
    sequence: int
    as_of: datetime
    market_state: MarketStateV1
    freshness: ReplayFreshnessV1
    guardrail: ReplayGuardrailV1
    alerts: tuple[AlertV1, ...] = ()

    @model_validator(mode="after")
    def validate_replay_sample(self) -> "MarketWatchReplaySampleV1":
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("replay as_of must include a timezone")
        if self.market_state.trading_date != self.as_of.astimezone(_SHANGHAI).date():
            raise ValueError("replay trading_date must match Shanghai as_of")
        if self.freshness.status in {
            FreshnessStatus.STALE,
            FreshnessStatus.UNAVAILABLE,
        } and not (
            self.guardrail.regime == MarketRegime.UNCERTAIN
            and self.guardrail.severity == GuardrailSeverity.STOP
        ):
            raise ValueError("stale replay data must preserve uncertain stop guardrail")
        if (
            self.freshness.status == FreshnessStatus.DEGRADED
            and self.guardrail.severity == GuardrailSeverity.CALM
        ):
            raise ValueError("degraded replay data cannot preserve a calm guardrail")
        return self


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _normalize_trade_date(value: date | str) -> str:
    if isinstance(value, datetime):
        raise TypeError("trade_date must be a date or ISO date string")
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value).strip()).isoformat()


def _positive_limit(value: int | None, *, name: str, allow_none: bool) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _aware_shanghai(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("clock must return datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(_SHANGHAI)


def _minute_iso(value: datetime) -> str:
    return _aware_shanghai(value).replace(second=0, microsecond=0).isoformat(
        timespec="seconds"
    )


def _decode_snapshot(payload_blob: bytes) -> dict[str, Any]:
    raw = json.loads(zlib.decompress(payload_blob).decode("utf-8"))
    # Revalidation makes corrupt or obsolete storage fail visibly instead of
    # leaking a partially trusted replay to a consumer.
    return MarketWatchSnapshotV1.model_validate(raw).model_dump(mode="json")


def _decode_alert(alert_json: str) -> dict[str, Any]:
    return AlertV1.model_validate(json.loads(alert_json)).model_dump(mode="json")


class MarketWatchHistoryStore:
    """Thread-safe SQLite/WAL storage for deterministic market-watch replay.

    ``record`` validates every input as :class:`MarketWatchSnapshotV1`.  The
    store owns persistence and retention only; refresh, cache, alert policy and
    provider routing remain responsibilities of their existing domain owners.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        *,
        config_version: str = DEFAULT_CONFIG_VERSION,
        retention_trade_days: int = DEFAULT_RETENTION_TRADE_DAYS,
        clock: Callable[[], datetime] | None = None,
        read_only: bool = False,
    ) -> None:
        version = str(config_version).strip()
        if not version:
            raise ValueError("config_version must not be empty")
        _positive_limit(
            retention_trade_days,
            name="retention_trade_days",
            allow_none=False,
        )

        configured = db_path or os.environ.get(ENV_DB_PATH)
        if configured is None:
            configured = Path.home() / ".tradex" / "market_watch.sqlite3"
        self.read_only = bool(read_only)
        if str(configured) == ":memory:":
            if self.read_only:
                raise ValueError("read-only history store requires a file path")
            self.db_path = ":memory:"
        else:
            resolved = Path(configured).expanduser().resolve()
            if not self.read_only:
                resolved.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(resolved)

        self.config_version = version
        self.retention_trade_days = int(retention_trade_days)
        self._clock = clock or (lambda: datetime.now(_SHANGHAI))
        self._lock = threading.RLock()
        self._closed = False
        self._schema_available = False
        self._connection: sqlite3.Connection | None = None
        if self.read_only:
            with self._lock:
                self._open_read_only_locked()
            return
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
            self._schema_available = True
        except Exception:
            self._connection.close()
            self._closed = True
            raise

    def _open_read_only_locked(self) -> bool:
        """Open a newly available history database without writing to it."""

        if self._connection is not None and self._schema_available:
            return True
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._schema_available = False
        target = Path(self.db_path)
        if not target.exists():
            return False

        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{target.as_uri()}?mode=ro",
                check_same_thread=False,
                timeout=5,
                uri=True,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA query_only = ON")
            required = {
                "market_watch_history_meta",
                "market_watch_snapshots",
                "market_watch_alert_events",
            }
            present = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not required <= present:
                connection.close()
                return False
            expected = {
                "contract": HISTORY_CONTRACT,
                "schema_version": str(HISTORY_SCHEMA_VERSION),
            }
            stored = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM market_watch_history_meta "
                    "WHERE key IN ('contract', 'schema_version')"
                ).fetchall()
            }
            if not expected.keys() <= stored.keys():
                connection.close()
                return False
            for key, value in expected.items():
                if stored[key] != value:
                    raise RuntimeError(
                        f"incompatible market-watch history {key}: {stored[key]}"
                    )
        except sqlite3.OperationalError:
            if connection is not None:
                connection.close()
            return False
        except Exception:
            if connection is not None:
                connection.close()
            raise

        self._connection = connection
        self._schema_available = True
        return True

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_watch_history_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS market_watch_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    minute_bucket TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    snapshot_schema_version INTEGER NOT NULL,
                    history_schema_version INTEGER NOT NULL,
                    config_version TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    as_of TEXT NOT NULL,
                    market_phase TEXT NOT NULL,
                    freshness_status TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    payload_bytes INTEGER NOT NULL,
                    payload_blob BLOB NOT NULL,
                    recorded_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (trade_date, minute_bucket, config_version)
                );

                CREATE INDEX IF NOT EXISTS idx_market_watch_timeline
                    ON market_watch_snapshots (
                        config_version, trade_date, minute_bucket
                    );

                CREATE TABLE IF NOT EXISTS market_watch_alert_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    trade_date TEXT NOT NULL,
                    minute_bucket TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    history_schema_version INTEGER NOT NULL,
                    config_version TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    code TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    alert_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_market_watch_alert_history
                    ON market_watch_alert_events (
                        config_version, trade_date, observed_at, id
                    );
                """
            )
            expected = {
                "contract": HISTORY_CONTRACT,
                "schema_version": str(HISTORY_SCHEMA_VERSION),
            }
            for key, value in expected.items():
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO market_watch_history_meta (key, value)
                    VALUES (?, ?)
                    """,
                    (key, value),
                )
                stored = self._connection.execute(
                    "SELECT value FROM market_watch_history_meta WHERE key = ?",
                    (key,),
                ).fetchone()
                if stored is None or stored["value"] != value:
                    raise RuntimeError(
                        f"incompatible market-watch history {key}: "
                        f"{None if stored is None else stored['value']}"
                    )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("market-watch history store is closed")

    def _ensure_writable(self) -> None:
        self._ensure_open()
        if self.read_only:
            raise RuntimeError("market-watch history store is read-only")

    def _can_read(self) -> bool:
        self._ensure_open()
        if self._connection is not None and self._schema_available:
            return True
        if not self.read_only:
            return False
        with self._lock:
            return self._open_read_only_locked()

    def record(
        self,
        snapshot: MarketWatchSnapshotV1 | Mapping[str, Any],
        *,
        overwrite: bool = True,
    ) -> dict[str, Any]:
        """Insert or update one Shanghai-minute snapshot and append its alerts.

        Re-recording byte-identical content is idempotent.  Different content
        for the same trade date, minute and config version replaces the replay
        snapshot, while alert events already observed in that minute remain.
        ``overwrite=False`` is reserved for collection-gap heartbeats: a real
        same-minute row always wins over the derived stale continuity row.
        """

        with self._lock:
            self._ensure_writable()
        canonical = MarketWatchSnapshotV1.model_validate(snapshot)
        local_as_of = canonical.as_of.astimezone(_SHANGHAI)
        trade_date = canonical.market_state.trading_date.isoformat()
        if local_as_of.date().isoformat() != trade_date:
            raise ValueError("snapshot as_of must belong to its Shanghai trading_date")
        if (
            calendar_day_status(canonical.market_state.trading_date)
            is not CalendarDayStatus.VERIFIED_TRADING_DAY
        ):
            return {
                "history_contract": HISTORY_CONTRACT,
                "history_schema_version": HISTORY_SCHEMA_VERSION,
                "config_version": self.config_version,
                "action": "skipped",
                "reason": "non_trading_day",
                "inserted": False,
                "updated": False,
                "unchanged": False,
                "trade_date": trade_date,
                "snapshot_id": canonical.snapshot_id,
                "alerts_added": 0,
            }

        minute_bucket = _minute_iso(canonical.as_of)
        payload_dict = canonical.model_dump(mode="json")
        raw_payload = _json_bytes(payload_dict)
        payload_digest = hashlib.sha256(raw_payload).hexdigest()
        payload_blob = zlib.compress(raw_payload, level=6)
        recorded_at = _aware_shanghai(self._clock()).isoformat(timespec="seconds")

        with self._lock:
            self._ensure_open()
            with self._connection:
                existing = self._connection.execute(
                    """
                    SELECT payload_digest, payload_bytes, snapshot_id
                    FROM market_watch_snapshots
                    WHERE trade_date = ? AND minute_bucket = ?
                        AND config_version = ?
                    """,
                    (trade_date, minute_bucket, self.config_version),
                ).fetchone()
                action = (
                    "inserted"
                    if existing is None
                    else "unchanged"
                    if existing["payload_digest"] == payload_digest
                    else "updated"
                )

                if existing is not None and not overwrite:
                    return {
                        "history_contract": HISTORY_CONTRACT,
                        "history_schema_version": HISTORY_SCHEMA_VERSION,
                        "config_version": self.config_version,
                        "action": "preserved",
                        "inserted": False,
                        "updated": False,
                        "unchanged": False,
                        "trade_date": trade_date,
                        "minute_bucket": minute_bucket,
                        "snapshot_id": existing["snapshot_id"],
                        "payload_digest": existing["payload_digest"],
                        "payload_bytes": existing["payload_bytes"],
                        "alerts_added": 0,
                        "retention": None,
                    }

                self._connection.execute(
                    """
                    INSERT INTO market_watch_snapshots (
                        trade_date, minute_bucket, contract,
                        snapshot_schema_version, history_schema_version,
                        config_version, snapshot_id, sequence, as_of,
                        market_phase, freshness_status, regime, severity,
                        payload_digest, payload_bytes, payload_blob,
                        recorded_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (trade_date, minute_bucket, config_version)
                    DO UPDATE SET
                        contract = excluded.contract,
                        snapshot_schema_version = excluded.snapshot_schema_version,
                        history_schema_version = excluded.history_schema_version,
                        snapshot_id = excluded.snapshot_id,
                        sequence = excluded.sequence,
                        as_of = excluded.as_of,
                        market_phase = excluded.market_phase,
                        freshness_status = excluded.freshness_status,
                        regime = excluded.regime,
                        severity = excluded.severity,
                        payload_digest = excluded.payload_digest,
                        payload_bytes = excluded.payload_bytes,
                        payload_blob = excluded.payload_blob,
                        updated_at = excluded.updated_at
                    """,
                    (
                        trade_date,
                        minute_bucket,
                        canonical.contract,
                        canonical.schema_version,
                        HISTORY_SCHEMA_VERSION,
                        self.config_version,
                        canonical.snapshot_id,
                        canonical.sequence,
                        canonical.as_of.isoformat(),
                        canonical.market_state.phase.value,
                        canonical.freshness.status.value,
                        canonical.guardrail.regime.value,
                        canonical.guardrail.severity.value,
                        payload_digest,
                        len(payload_blob),
                        payload_blob,
                        recorded_at,
                        recorded_at,
                    ),
                )

                alerts_added = self._record_alerts_locked(
                    canonical,
                    trade_date=trade_date,
                    minute_bucket=minute_bucket,
                    recorded_at=recorded_at,
                )
                retention = self._apply_retention_locked(
                    self.retention_trade_days
                )

        return {
            "history_contract": HISTORY_CONTRACT,
            "history_schema_version": HISTORY_SCHEMA_VERSION,
            "config_version": self.config_version,
            "action": action,
            "inserted": action == "inserted",
            "updated": action == "updated",
            "unchanged": action == "unchanged",
            "trade_date": trade_date,
            "minute_bucket": minute_bucket,
            "snapshot_id": canonical.snapshot_id,
            "payload_digest": payload_digest,
            "payload_bytes": len(payload_blob),
            "alerts_added": alerts_added,
            "retention": retention,
        }

    def _record_alerts_locked(
        self,
        snapshot: MarketWatchSnapshotV1,
        *,
        trade_date: str,
        minute_bucket: str,
        recorded_at: str,
    ) -> int:
        added = 0
        for alert in snapshot.alerts:
            alert_dict = alert.model_dump(mode="json")
            alert_bytes = _json_bytes(alert_dict)
            event_material = _json_bytes(
                {
                    "config_version": self.config_version,
                    "minute_bucket": minute_bucket,
                    "snapshot_id": snapshot.snapshot_id,
                    "alert": alert_dict,
                }
            )
            event_key = hashlib.sha256(event_material).hexdigest()
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO market_watch_alert_events (
                    event_key, trade_date, minute_bucket, observed_at,
                    history_schema_version, config_version, snapshot_id,
                    sequence, dedupe_key, code, severity, alert_json,
                    recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    trade_date,
                    minute_bucket,
                    snapshot.as_of.isoformat(),
                    HISTORY_SCHEMA_VERSION,
                    self.config_version,
                    snapshot.snapshot_id,
                    snapshot.sequence,
                    alert.dedupe_key,
                    alert.code,
                    alert.severity.value,
                    alert_bytes.decode("utf-8"),
                    recorded_at,
                ),
            )
            added += max(cursor.rowcount, 0)
        return added

    def list_dates(self, limit: int = DEFAULT_DATE_LIMIT) -> list[dict[str, Any]]:
        """Return newest retained trade dates with snapshot/alert counts."""

        normalized_limit = _positive_limit(limit, name="limit", allow_none=False)
        with self._lock:
            if not self._can_read():
                return []
            rows = self._connection.execute(
                """
                WITH alert_counts AS (
                    SELECT trade_date, COUNT(*) AS alert_count
                    FROM market_watch_alert_events
                    WHERE config_version = ?
                    GROUP BY trade_date
                )
                SELECT
                    snapshots.trade_date AS trade_date,
                    COUNT(*) AS snapshot_count,
                    COALESCE(alert_counts.alert_count, 0) AS alert_count,
                    MIN(snapshots.minute_bucket) AS first_minute,
                    MAX(snapshots.minute_bucket) AS last_minute
                FROM market_watch_snapshots AS snapshots
                LEFT JOIN alert_counts
                    ON alert_counts.trade_date = snapshots.trade_date
                WHERE snapshots.config_version = ?
                GROUP BY snapshots.trade_date
                ORDER BY snapshots.trade_date DESC
                """,
                (self.config_version, self.config_version),
            ).fetchall()
        visible_rows = [
            row
            for row in rows
            if calendar_day_status(date.fromisoformat(row["trade_date"]))
            is CalendarDayStatus.VERIFIED_TRADING_DAY
        ][:normalized_limit]
        return [
            {
                "history_contract": HISTORY_CONTRACT,
                "history_schema_version": HISTORY_SCHEMA_VERSION,
                "config_version": self.config_version,
                "trade_date": row["trade_date"],
                "snapshot_count": row["snapshot_count"],
                "alert_count": row["alert_count"],
                "first_minute": row["first_minute"],
                "last_minute": row["last_minute"],
            }
            for row in visible_rows
        ]

    def get_timeline(
        self,
        trade_date: date | str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return a chronological deterministic replay for one trade date.

        When ``limit`` is provided, the most recent N rows are selected and
        then returned in chronological order.
        """

        with self._lock:
            self._ensure_open()
        normalized_date = _normalize_trade_date(trade_date)
        if (
            calendar_day_status(date.fromisoformat(normalized_date))
            is not CalendarDayStatus.VERIFIED_TRADING_DAY
        ):
            return []
        normalized_limit = _positive_limit(limit, name="limit", allow_none=True)
        params: list[Any] = [normalized_date, self.config_version]
        query = """
            SELECT * FROM market_watch_snapshots
            WHERE trade_date = ? AND config_version = ?
            ORDER BY minute_bucket DESC
        """
        if normalized_limit is not None:
            query += " LIMIT ?"
            params.append(normalized_limit)

        with self._lock:
            if not self._can_read():
                return []
            rows = self._connection.execute(query, params).fetchall()

        return [self._timeline_item(row) for row in reversed(rows)]

    def get_replay_timeline(
        self,
        trade_date: date | str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return compact replay metadata without selecting snapshot blobs.

        The complete canonical payload remains available through ``get_timeline``
        for bounded domain consumers. Desktop Web replay and evaluation need
        only fields already indexed beside the compressed payload, so this path
        never decompresses unrelated trajectory or provider data.
        """

        with self._lock:
            self._ensure_open()
        normalized_date = _normalize_trade_date(trade_date)
        if (
            calendar_day_status(date.fromisoformat(normalized_date))
            is not CalendarDayStatus.VERIFIED_TRADING_DAY
        ):
            return []
        normalized_limit = _positive_limit(limit, name="limit", allow_none=True)
        params: list[Any] = [normalized_date, self.config_version]
        query = """
            SELECT trade_date, minute_bucket, history_schema_version,
                   config_version, snapshot_id, sequence, as_of,
                   market_phase, freshness_status, regime, severity,
                   payload_digest, payload_bytes, recorded_at, updated_at
            FROM market_watch_snapshots
            WHERE trade_date = ? AND config_version = ?
            ORDER BY minute_bucket DESC
        """
        if normalized_limit is not None:
            query += " LIMIT ?"
            params.append(normalized_limit)

        with self._lock:
            if not self._can_read():
                return []
            rows = self._connection.execute(query, params).fetchall()

        return [self._replay_timeline_item(row) for row in reversed(rows)]

    def get_collection_records(
        self,
        trade_date: date | str,
    ) -> list[dict[str, Any]]:
        """Return lightweight ordered rows for collector-ledger reconciliation.

        This path deliberately does not select or decompress ``payload_blob``.
        A derived gap heartbeat is identified by its reserved deterministic
        snapshot-id prefix, not merely by a stale freshness status.
        """

        normalized_date = _normalize_trade_date(trade_date)
        if (
            calendar_day_status(date.fromisoformat(normalized_date))
            is not CalendarDayStatus.VERIFIED_TRADING_DAY
        ):
            return []
        with self._lock:
            if not self._can_read():
                return []
            rows = self._connection.execute(
                """
                SELECT trade_date, minute_bucket, config_version, snapshot_id,
                    sequence, as_of, market_phase, freshness_status,
                    payload_digest, payload_bytes, recorded_at, updated_at
                FROM market_watch_snapshots
                WHERE trade_date = ? AND config_version = ?
                ORDER BY minute_bucket ASC
                """,
                (normalized_date, self.config_version),
            ).fetchall()
        return [
            {
                **dict(row),
                "record_kind": (
                    "derived_gap_heartbeat"
                    if str(row["snapshot_id"]).startswith("mw-heartbeat:")
                    else "accepted_real"
                    if row["freshness_status"] in {
                        "fresh",
                        "degraded",
                    }
                    else "stale_snapshot"
                ),
            }
            for row in rows
        ]

    def get_snapshot_by_pointer(
        self,
        *,
        trade_date: date | str,
        minute_bucket: datetime | str,
        snapshot_id: str,
        payload_digest: str,
    ) -> dict[str, Any] | None:
        """Read exactly one strict snapshot identified by a ledger pointer.

        Pointer fields are checked against row metadata before decompression.
        The stored canonical bytes and the revalidated canonical model are both
        hashed so corruption or non-canonical storage cannot cross the facade.
        """

        normalized_date = _normalize_trade_date(trade_date)
        parsed_minute = (
            minute_bucket
            if isinstance(minute_bucket, datetime)
            else datetime.fromisoformat(str(minute_bucket))
        )
        minute_iso = _minute_iso(parsed_minute)
        if minute_iso[:10] != normalized_date:
            raise ValueError("minute_bucket must belong to trade_date")
        expected_snapshot_id = str(snapshot_id).strip()
        if not expected_snapshot_id:
            raise ValueError("snapshot_id must not be empty")
        expected_digest = str(payload_digest).strip().lower()
        if len(expected_digest) != 64 or any(
            character not in "0123456789abcdef" for character in expected_digest
        ):
            raise ValueError("payload_digest must be a SHA-256 digest")
        with self._lock:
            if not self._can_read():
                return None
            row = self._connection.execute(
                """
                SELECT * FROM market_watch_snapshots
                WHERE trade_date = ? AND minute_bucket = ? AND config_version = ?
                LIMIT 1
                """,
                (normalized_date, minute_iso, self.config_version),
            ).fetchone()
        if row is None:
            return None
        if (
            row["snapshot_id"] != expected_snapshot_id
            or row["payload_digest"] != expected_digest
        ):
            return None
        raw = zlib.decompress(row["payload_blob"])
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            raise RuntimeError("market-watch history raw payload digest mismatch")
        decoded = json.loads(raw.decode("utf-8"))
        if _json_bytes(decoded) != raw:
            raise RuntimeError("market-watch history payload bytes are not canonical JSON")
        validated = MarketWatchSnapshotV1.model_validate(decoded)
        if (
            decoded.get("snapshot_id") != expected_snapshot_id
            or validated.snapshot_id != expected_snapshot_id
        ):
            raise RuntimeError("market-watch history snapshot_id mismatch")
        return {
            "history_contract": HISTORY_CONTRACT,
            "history_schema_version": row["history_schema_version"],
            "config_version": row["config_version"],
            "trade_date": row["trade_date"],
            "minute_bucket": row["minute_bucket"],
            "recorded_at": row["recorded_at"],
            "updated_at": row["updated_at"],
            "payload_digest": row["payload_digest"],
            "payload_bytes": row["payload_bytes"],
            # Preserve the exact historical JSON shape. Current-model default
            # fields may be newer than the bytes whose digest is the pointer.
            "payload": decoded,
        }

    def find_latest_snapshot(
        self,
        predicate: Callable[[Mapping[str, Any]], bool],
        *,
        limit: int = 1000,
    ) -> dict[str, Any] | None:
        """Return the newest strict snapshot accepted by a caller predicate."""

        if not callable(predicate):
            raise TypeError("snapshot predicate must be callable")
        normalized_limit = _positive_limit(limit, name="limit", allow_none=False)
        with self._lock:
            if not self._can_read():
                return None
            rows = self._connection.execute(
                """
                SELECT * FROM market_watch_snapshots
                WHERE config_version = ?
                ORDER BY trade_date DESC, minute_bucket DESC
                LIMIT ?
                """,
                (self.config_version, normalized_limit),
            ).fetchall()
        for row in rows:
            if (
                calendar_day_status(date.fromisoformat(row["trade_date"]))
                is not CalendarDayStatus.VERIFIED_TRADING_DAY
            ):
                continue
            item = self._timeline_item(row)
            if predicate(item["payload"]):
                return item
        return None

    def _timeline_item(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = _decode_snapshot(row["payload_blob"])
        return {
            "history_contract": HISTORY_CONTRACT,
            "history_schema_version": row["history_schema_version"],
            "config_version": row["config_version"],
            "trade_date": row["trade_date"],
            "minute_bucket": row["minute_bucket"],
            "recorded_at": row["recorded_at"],
            "updated_at": row["updated_at"],
            "payload_digest": row["payload_digest"],
            "payload_bytes": row["payload_bytes"],
            "payload": payload,
        }

    @staticmethod
    def _replay_timeline_item(row: sqlite3.Row) -> dict[str, Any]:
        phase = MarketPhase(row["market_phase"])
        payload = MarketWatchReplaySampleV1(
            snapshot_id=row["snapshot_id"],
            sequence=row["sequence"],
            as_of=row["as_of"],
            market_state=MarketStateV1(
                phase=phase,
                is_open=phase
                in {MarketPhase.OPENING_OBSERVATION, MarketPhase.TRADING},
                trading_date=row["trade_date"],
            ),
            freshness=ReplayFreshnessV1(status=row["freshness_status"]),
            guardrail=ReplayGuardrailV1(
                regime=row["regime"],
                severity=row["severity"],
            ),
        ).model_dump(mode="json")
        return {
            "history_contract": HISTORY_CONTRACT,
            "history_schema_version": row["history_schema_version"],
            "config_version": row["config_version"],
            "trade_date": row["trade_date"],
            "minute_bucket": row["minute_bucket"],
            "snapshot_id": row["snapshot_id"],
            "sequence": row["sequence"],
            "as_of": row["as_of"],
            "recorded_at": row["recorded_at"],
            "updated_at": row["updated_at"],
            "payload_digest": row["payload_digest"],
            "payload_bytes": row["payload_bytes"],
            "payload": payload,
        }

    def get_alerts(
        self,
        trade_date: date | str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return chronological alert events retained for one trade date."""

        with self._lock:
            self._ensure_open()
        normalized_date = _normalize_trade_date(trade_date)
        if (
            calendar_day_status(date.fromisoformat(normalized_date))
            is not CalendarDayStatus.VERIFIED_TRADING_DAY
        ):
            return []
        normalized_limit = _positive_limit(limit, name="limit", allow_none=True)
        params: list[Any] = [normalized_date, self.config_version]
        query = """
            SELECT * FROM market_watch_alert_events
            WHERE trade_date = ? AND config_version = ?
            ORDER BY observed_at DESC, id DESC
        """
        if normalized_limit is not None:
            query += " LIMIT ?"
            params.append(normalized_limit)

        with self._lock:
            if not self._can_read():
                return []
            rows = self._connection.execute(query, params).fetchall()

        return [
            {
                "history_contract": HISTORY_CONTRACT,
                "history_schema_version": row["history_schema_version"],
                "config_version": row["config_version"],
                "event_key": row["event_key"],
                "trade_date": row["trade_date"],
                "minute_bucket": row["minute_bucket"],
                "observed_at": row["observed_at"],
                "recorded_at": row["recorded_at"],
                "snapshot_id": row["snapshot_id"],
                "sequence": row["sequence"],
                "dedupe_key": row["dedupe_key"],
                "code": row["code"],
                "severity": row["severity"],
                "alert": _decode_alert(row["alert_json"]),
            }
            for row in reversed(rows)
        ]

    def apply_retention(
        self,
        retention_trade_days: int | None = None,
    ) -> dict[str, Any]:
        """Prune complete dates older than the configured distinct-day cap."""

        keep = self.retention_trade_days if retention_trade_days is None else retention_trade_days
        _positive_limit(keep, name="retention_trade_days", allow_none=False)
        with self._lock:
            self._ensure_writable()
            with self._connection:
                return self._apply_retention_locked(int(keep))

    def _apply_retention_locked(self, keep: int) -> dict[str, Any]:
        cutoff = self._connection.execute(
            """
            SELECT trade_date
            FROM (
                SELECT trade_date FROM market_watch_snapshots
                UNION
                SELECT trade_date FROM market_watch_alert_events
            )
            ORDER BY trade_date DESC
            LIMIT 1 OFFSET ?
            """,
            (keep - 1,),
        ).fetchone()
        if cutoff is None:
            return {
                "retention_trade_days": keep,
                "oldest_retained_date": None,
                "snapshots_deleted": 0,
                "alerts_deleted": 0,
            }

        oldest_retained = cutoff["trade_date"]
        snapshots = self._connection.execute(
            "DELETE FROM market_watch_snapshots WHERE trade_date < ?",
            (oldest_retained,),
        )
        alerts = self._connection.execute(
            "DELETE FROM market_watch_alert_events WHERE trade_date < ?",
            (oldest_retained,),
        )
        return {
            "retention_trade_days": keep,
            "oldest_retained_date": oldest_retained,
            "snapshots_deleted": max(snapshots.rowcount, 0),
            "alerts_deleted": max(alerts.rowcount, 0),
        }

    def close(self) -> None:
        """Close the SQLite connection; repeated calls are harmless."""

        with self._lock:
            if self._closed:
                return
            if self._connection is not None:
                self._connection.close()
            self._closed = True

    def __enter__(self) -> "MarketWatchHistoryStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


__all__ = [
    "DEFAULT_CONFIG_VERSION",
    "DEFAULT_DATE_LIMIT",
    "DEFAULT_RETENTION_TRADE_DAYS",
    "ENV_DB_PATH",
    "HISTORY_CONTRACT",
    "HISTORY_SCHEMA_VERSION",
    "MarketWatchHistoryStore",
]
