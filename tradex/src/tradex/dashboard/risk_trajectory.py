"""SQLite-backed, descriptive intraday trajectories for risk-appetite states.

This module stores observed states and applies a deliberately small confirmation
rule.  It does not predict returns or turn ordinal states into probabilities.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


STATE_VALUES = {"weak": -1, "medium": 0, "strong": 1, "unknown": None}
STATE_LABELS = {"weak": "弱", "medium": "中", "strong": "强", "unknown": "数据不足"}
VALID_STATES = frozenset(STATE_VALUES)
SCHEMA_VERSION = "risk-trajectory-v1"
MAX_GAP_SECONDS = 120
DEFAULT_MAX_POINTS = 256
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_STORING_PHASES = frozenset({"opening_observation", "trading"})


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def _as_shanghai(value: datetime | str | None, *, default_now: bool = False) -> datetime:
    if value is None:
        if not default_now:
            raise ValueError("a timestamp is required")
        return datetime.now(_SHANGHAI)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    return parsed.astimezone(_SHANGHAI)


def _minute_iso(value: datetime | str) -> str:
    return _as_shanghai(value).replace(second=0, microsecond=0).isoformat(timespec="seconds")


def _received_iso(value: datetime | str | None) -> str:
    return _as_shanghai(value, default_now=True).isoformat(timespec="seconds")


def _normalize_trade_date(value: date | str) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)).isoformat()


def _normalize_axes(raw_axes: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for axis, state in raw_axes.items():
        key = str(axis).strip()
        normalized = str(state).strip().lower()
        if not key:
            raise ValueError("axis names must not be empty")
        if normalized not in VALID_STATES:
            raise ValueError(f"invalid state for {key}: {state}")
        result[key] = normalized
    if not result:
        raise ValueError("raw_axes must contain at least one axis")
    return result


def _normalize_provider_as_of(value: Mapping[str, Any] | None) -> dict[str, str | None]:
    if not value:
        return {}
    return {
        str(key): None if item is None else str(item)
        for key, item in value.items()
    }


def _payload_digest(
    raw_axes: Mapping[str, str],
    metrics: Mapping[str, Any],
    provider_as_of: Mapping[str, Any],
    headline: Mapping[str, Any],
) -> str:
    material = _json_dumps({
        "raw_axes": raw_axes,
        "metrics": metrics,
        "provider_as_of": provider_as_of,
        "headline": headline,
    })
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _derive_segment(minute_bucket: str) -> str:
    local_time = datetime.fromisoformat(minute_bucket).astimezone(_SHANGHAI).time()
    return "am" if local_time.hour < 12 else "pm"


class RiskTrajectoryStore:
    """Thread-safe trajectory storage and two-fresh-sample confirmation."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None, *, max_points: int = DEFAULT_MAX_POINTS):
        if max_points < 1:
            raise ValueError("max_points must be positive")
        configured = db_path or os.environ.get("TRADEX_RISK_TRAJECTORY_DB")
        if configured is None:
            configured = Path.home() / ".tradex" / "risk_trajectory.sqlite3"
        self.db_path = str(configured)
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.max_points = max_points
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
        self._pending: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self._active_key: tuple[str, str] | None = None
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS trajectory_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    minute_bucket TEXT NOT NULL,
                    config_version TEXT NOT NULL,
                    session_segment TEXT NOT NULL,
                    market_phase TEXT NOT NULL,
                    fresh INTEGER NOT NULL,
                    eligible INTEGER NOT NULL,
                    eligibility_reason TEXT,
                    raw_axes_json TEXT NOT NULL,
                    confirmed_axes_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    provider_as_of_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    headline_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    confirmed_changed INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (trade_date, minute_bucket, config_version)
                );
                CREATE INDEX IF NOT EXISTS idx_trajectory_lookup
                    ON trajectory_samples (trade_date, config_version, minute_bucket);
                """
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "RiskTrajectoryStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _latest_row(self, key: tuple[str, str]) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT * FROM trajectory_samples
            WHERE trade_date = ? AND config_version = ?
            ORDER BY minute_bucket DESC LIMIT 1
            """,
            key,
        ).fetchone()

    def _has_other_history(self, key: tuple[str, str]) -> bool:
        row = self._connection.execute(
            """
            SELECT 1 FROM trajectory_samples
            WHERE trade_date != ? OR config_version != ?
            LIMIT 1
            """,
            key,
        ).fetchone()
        return row is not None

    def _activate(self, key: tuple[str, str]) -> bool:
        if self._active_key == key:
            return False
        reset = self._active_key is not None or (
            self._latest_row(key) is None and self._has_other_history(key)
        )
        self._pending.clear()
        self._active_key = key
        return reset

    def _phase_dynamics(self, market_phase: str) -> str | None:
        if market_phase == "midday_break":
            return "paused"
        if market_phase in {"closed", "post_close"}:
            return "closed"
        if market_phase == "pre_open":
            return "paused"
        return None

    def record_snapshot(
        self,
        *,
        trade_date: date | str,
        minute_bucket: datetime | str,
        config_version: str,
        raw_axes: Mapping[str, str],
        metrics: Mapping[str, Any] | None = None,
        provider_as_of: Mapping[str, Any] | None = None,
        payload_hash: str | None = None,
        market_phase: str = "trading",
        received_at: datetime | str | None = None,
        fresh: bool = True,
        session_segment: str | None = None,
        headline: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record one minute observation and return its current trajectory view."""

        normalized_date = _normalize_trade_date(trade_date)
        version = str(config_version).strip()
        if not version:
            raise ValueError("config_version must not be empty")
        bucket = _minute_iso(minute_bucket)
        if bucket[:10] != normalized_date:
            raise ValueError("minute_bucket must belong to trade_date")
        received = _received_iso(received_at)
        axes = _normalize_axes(raw_axes)
        metric_values = dict(metrics or {})
        provider_values = _normalize_provider_as_of(provider_as_of)
        headline_values = dict(headline or {})
        digest = str(payload_hash or _payload_digest(axes, metric_values, provider_values, headline_values))
        segment = str(session_segment or _derive_segment(bucket))
        phase = str(market_phase)
        key = (normalized_date, version)

        with self._lock:
            reset = self._activate(key)
            phase_dynamics = self._phase_dynamics(phase)
            if phase not in _STORING_PHASES:
                self._pending.pop(key, None)
                current = self._current_locked(key, market_phase=phase, dynamics_override=phase_dynamics)
                return {"inserted": False, "reason": phase_dynamics or "non_trading", "current": current}

            existing = self._connection.execute(
                """
                SELECT * FROM trajectory_samples
                WHERE trade_date = ? AND minute_bucket = ? AND config_version = ?
                """,
                (normalized_date, bucket, version),
            ).fetchone()
            if existing is not None:
                current = self._current_locked(key, market_phase=phase)
                return {"inserted": False, "reason": "duplicate_minute", "current": current}

            latest = self._latest_row(key)
            if latest is not None and bucket < latest["minute_bucket"]:
                current = self._current_locked(key, market_phase=phase)
                return {"inserted": False, "reason": "out_of_order", "current": current}

            confirmed = _json_loads(latest["confirmed_axes_json"], {}) if latest else {}
            for axis in axes:
                confirmed.setdefault(axis, "unknown")

            previous_hash = latest["payload_hash"] if latest else None
            previous_provider = _json_loads(latest["provider_as_of_json"], {}) if latest else {}
            duplicate_hash = latest is not None and digest == previous_hash
            duplicate_watermark = bool(provider_values) and provider_values == previous_provider

            if not fresh:
                self._pending.pop(key, None)
                current = self._current_locked(
                    key,
                    market_phase=phase,
                    dynamics_override="stale",
                )
                return {"inserted": False, "reason": "stale", "current": current}
            if duplicate_hash or duplicate_watermark:
                reason = "duplicate_payload" if duplicate_hash else "duplicate_watermark"
                current = self._current_locked(
                    key,
                    market_phase=phase,
                    dynamics_override="stale" if duplicate_watermark else None,
                )
                return {"inserted": False, "reason": reason, "current": current}

            if phase == "opening_observation":
                eligible = False
                eligibility_reason = "opening_observation"
                self._pending.pop(key, None)
            else:
                eligible = True
                eligibility_reason = None

            pending = self._pending.setdefault(key, {})
            confirmed_changed = False
            if eligible:
                bucket_dt = datetime.fromisoformat(bucket)
                for axis, candidate in axes.items():
                    item = pending.get(axis)
                    if item is not None:
                        gap = (bucket_dt - datetime.fromisoformat(item["minute_bucket"])).total_seconds()
                        if gap > MAX_GAP_SECONDS or item["session_segment"] != segment:
                            pending.pop(axis, None)
                            item = None

                    if candidate == "unknown":
                        pending.pop(axis, None)
                        continue
                    if candidate == confirmed.get(axis):
                        pending.pop(axis, None)
                        continue
                    if item is not None and item["candidate"] == candidate:
                        item["samples"] += 1
                        item["minute_bucket"] = bucket
                        if item["samples"] >= 2:
                            confirmed[axis] = candidate
                            pending.pop(axis, None)
                            confirmed_changed = True
                    else:
                        pending[axis] = {
                            "candidate": candidate,
                            "samples": 1,
                            "required_samples": 2,
                            "minute_bucket": bucket,
                            "session_segment": segment,
                        }

            self._connection.execute(
                """
                INSERT INTO trajectory_samples (
                    trade_date, minute_bucket, config_version, session_segment,
                    market_phase, fresh, eligible, eligibility_reason,
                    raw_axes_json, confirmed_axes_json, metrics_json,
                    provider_as_of_json, payload_hash, headline_json, received_at,
                    confirmed_changed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_date,
                    bucket,
                    version,
                    segment,
                    phase,
                    int(bool(fresh)),
                    int(eligible),
                    eligibility_reason,
                    _json_dumps(axes),
                    _json_dumps(confirmed),
                    _json_dumps(metric_values),
                    _json_dumps(provider_values),
                    digest,
                    _json_dumps(headline_values),
                    received,
                    int(confirmed_changed),
                ),
            )
            self._connection.execute(
                """
                DELETE FROM trajectory_samples
                WHERE id IN (
                    SELECT id FROM trajectory_samples
                    WHERE trade_date = ? AND config_version = ?
                    ORDER BY minute_bucket DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (normalized_date, version, self.max_points),
            )
            self._connection.commit()

            if reset:
                dynamics_override = "reset"
            else:
                dynamics_override = None
            current = self._current_locked(key, market_phase=phase, dynamics_override=dynamics_override)
            return {"inserted": True, "reason": eligibility_reason, "current": current}

    def get_series(
        self,
        trade_date: date | str,
        config_version: str,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        key = (_normalize_trade_date(trade_date), str(config_version))
        with self._lock:
            return self._series_locked(key, limit=limit)

    def _series_locked(self, key: tuple[str, str], *, limit: int | None = None) -> list[dict[str, Any]]:
        row_limit = self.max_points if limit is None else min(max(0, int(limit)), self.max_points)
        if row_limit == 0:
            return []
        rows = self._connection.execute(
            """
            SELECT * FROM (
                SELECT * FROM trajectory_samples
                WHERE trade_date = ? AND config_version = ?
                ORDER BY minute_bucket DESC LIMIT ?
            ) ORDER BY minute_bucket ASC
            """,
            (*key, row_limit),
        ).fetchall()
        return [self._row_view(row) for row in rows]

    def _row_view(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "minute_bucket": row["minute_bucket"],
            "session_segment": row["session_segment"],
            "market_phase": row["market_phase"],
            "fresh": bool(row["fresh"]),
            "eligible": bool(row["eligible"]),
            "eligibility_reason": row["eligibility_reason"],
            "raw_axes": _json_loads(row["raw_axes_json"], {}),
            "confirmed_axes": _json_loads(row["confirmed_axes_json"], {}),
            "metrics": _json_loads(row["metrics_json"], {}),
            "provider_as_of": _json_loads(row["provider_as_of_json"], {}),
            "payload_hash": row["payload_hash"],
            "headline_raw": _json_loads(row["headline_json"], {}),
            "received_at": row["received_at"],
            "confirmed_changed": bool(row["confirmed_changed"]),
        }

    def get_current(
        self,
        trade_date: date | str,
        config_version: str,
        *,
        market_phase: str | None = None,
    ) -> dict[str, Any]:
        key = (_normalize_trade_date(trade_date), str(config_version))
        with self._lock:
            return self._current_locked(key, market_phase=market_phase)

    def _current_locked(
        self,
        key: tuple[str, str],
        *,
        market_phase: str | None,
        dynamics_override: str | None = None,
    ) -> dict[str, Any]:
        latest = self._latest_row(key)
        phase = market_phase or (latest["market_phase"] if latest else "trading")
        phase_dynamics = self._phase_dynamics(phase)
        confirmed = _json_loads(latest["confirmed_axes_json"], {}) if latest else {}
        raw = _json_loads(latest["raw_axes_json"], {}) if latest else {}
        pending = self._pending.get(key, {})

        if dynamics_override:
            dynamics = dynamics_override
        elif phase_dynamics:
            dynamics = phase_dynamics
        elif latest is not None and (
            not bool(latest["fresh"])
            or latest["eligibility_reason"] in {"duplicate_payload", "duplicate_watermark"}
        ):
            dynamics = "stale"
        elif any(state != "unknown" for state in confirmed.values()):
            dynamics = "ready"
        else:
            dynamics = "collecting"

        axes = self._axis_dynamics(key, latest, raw, confirmed, pending)
        last_confirmed_row = self._connection.execute(
            """
            SELECT minute_bucket FROM trajectory_samples
            WHERE trade_date = ? AND config_version = ? AND confirmed_changed = 1
            ORDER BY minute_bucket DESC LIMIT 1
            """,
            key,
        ).fetchone()
        return {
            "schema_version": SCHEMA_VERSION,
            "trade_date": key[0],
            "config_version": key[1],
            "dynamics": dynamics,
            "market_phase": phase,
            "last_sample_at": latest["minute_bucket"] if latest else None,
            "last_confirmed_at": last_confirmed_row["minute_bucket"] if last_confirmed_row else None,
            "headline_raw": _json_loads(latest["headline_json"], {}) if latest else {},
            "axes": axes,
            "series": self._series_locked(key),
        }

    def _axis_dynamics(
        self,
        key: tuple[str, str],
        latest: sqlite3.Row | None,
        raw: Mapping[str, str],
        confirmed: Mapping[str, str],
        pending: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        if latest is None:
            return {}
        cutoff = (datetime.fromisoformat(latest["minute_bucket"]) - timedelta(minutes=10)).isoformat(timespec="seconds")
        rows = self._connection.execute(
            """
            SELECT raw_axes_json, confirmed_axes_json FROM trajectory_samples
            WHERE trade_date = ? AND config_version = ? AND minute_bucket >= ?
            ORDER BY minute_bucket ASC
            """,
            (*key, cutoff),
        ).fetchall()
        raw_history = [_json_loads(row["raw_axes_json"], {}) for row in rows]
        confirmed_history = [_json_loads(row["confirmed_axes_json"], {}) for row in rows]
        axis_names = set(raw) | set(confirmed) | set(pending)
        for history in raw_history:
            axis_names.update(history)

        result: dict[str, dict[str, Any]] = {}
        for axis in sorted(axis_names):
            path: list[str] = []
            for history in confirmed_history:
                state = history.get(axis, "unknown")
                if state == "unknown":
                    continue
                if not path or state != path[-1]:
                    path.append(state)

            if len(path) < 2:
                direction = "insufficient" if not path else "flat"
            else:
                previous = STATE_VALUES[path[-2]]
                last = STATE_VALUES[path[-1]]
                if last > previous:
                    direction = "strengthening"
                elif last < previous:
                    direction = "weakening"
                else:
                    direction = "flat"

            if not path:
                summary = "近10分钟有效确认样本不足。"
            elif direction == "flat":
                summary = f"近10分钟维持{STATE_LABELS[path[-1]]}。"
            elif len(path) >= 3 and len({
                1 if STATE_VALUES[current] > STATE_VALUES[previous] else -1
                for previous, current in zip(path, path[1:])
            }) > 1:
                summary = (
                    f"近10分钟出现往返，最近由{STATE_LABELS[path[-2]]}转为"
                    f"{STATE_LABELS[path[-1]]}。"
                )
            else:
                summary = (
                    f"近10分钟由{STATE_LABELS[path[0]]}转为"
                    f"{STATE_LABELS[path[-1]]}。"
                )

            pending_item = pending.get(axis)
            result[axis] = {
                "raw": raw.get(axis, "unknown"),
                "confirmed": confirmed.get(axis, "unknown"),
                "value": STATE_VALUES[confirmed.get(axis, "unknown")],
                "direction": direction,
                "path": path,
                "pending": (
                    {
                        "candidate": pending_item["candidate"],
                        "samples": pending_item["samples"],
                        "required_samples": pending_item["required_samples"],
                    }
                    if pending_item is not None
                    else None
                ),
                "summary": summary,
            }
        return result


__all__ = [
    "DEFAULT_MAX_POINTS",
    "MAX_GAP_SECONDS",
    "SCHEMA_VERSION",
    "STATE_LABELS",
    "STATE_VALUES",
    "RiskTrajectoryStore",
]
