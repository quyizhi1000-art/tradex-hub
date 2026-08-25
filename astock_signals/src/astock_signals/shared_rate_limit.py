"""Small SQLite-backed admission budgets shared by local Python processes.

The state contains only constant provider bucket names and timestamps.  It is
safe to share between the dashboard, MCP server, and diagnostic processes;
credentials, URLs, request parameters, and response data are never stored.
"""

from __future__ import annotations

import math
import os
import sqlite3
import tempfile
import time
from pathlib import Path


class SharedRateLimitExceeded(RuntimeError):
    """The provider bucket has no admission capacity for this request."""


class SharedRateLimitUnavailable(RuntimeError):
    """The shared state could not be checked, so admission fails closed."""


def _state_file(value: str | os.PathLike[str] | None = None) -> Path:
    if value is None:
        configured = os.getenv("TRADEX_RATE_LIMIT_STATE_FILE", "").strip()
        if configured:
            value = configured
        else:
            root = Path(os.getenv("LOCALAPPDATA") or tempfile.gettempdir())
            value = root / "Tradex" / "provider-rate-limits.sqlite3"
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SharedRateLimitUnavailable(
            "TRADEX_RATE_LIMIT_STATE_FILE must be an absolute path"
        )
    return path


def _positive_number(value: float | int, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return result


def _non_negative_number(value: float | int, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative finite number") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a non-negative finite number")
    return result


def _bucket_name(value: str) -> str:
    bucket = str(value or "").strip()
    if not bucket or len(bucket) > 160 or any(char.isspace() for char in bucket):
        raise ValueError("bucket must be a short non-empty name without whitespace")
    return bucket


def _connect(state_file: str | os.PathLike[str] | None) -> sqlite3.Connection:
    path = _state_file(state_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(path),
            timeout=0.25,
            isolation_level=None,
        )
        connection.execute("PRAGMA busy_timeout = 250")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS rate_events ("
            "bucket TEXT NOT NULL, occurred_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS rate_events_bucket_time "
            "ON rate_events(bucket, occurred_at)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS request_slots ("
            "bucket TEXT PRIMARY KEY, next_at REAL NOT NULL)"
        )
        return connection
    except (OSError, sqlite3.Error) as exc:
        raise SharedRateLimitUnavailable(
            "provider rate-limit state is unavailable"
        ) from exc


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.rollback()
    except sqlite3.Error:
        pass


def consume_shared_rate_budget(
    bucket: str,
    limit: int,
    *,
    window_seconds: float = 60.0,
    state_file: str | os.PathLike[str] | None = None,
    now: float | None = None,
) -> None:
    """Atomically consume one sliding-window request from a shared bucket."""

    name = _bucket_name(bucket)
    try:
        maximum = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be a positive integer") from exc
    if isinstance(limit, bool) or maximum <= 0:
        raise ValueError("limit must be a positive integer")
    window = _positive_number(window_seconds, "window_seconds")
    current = time.time() if now is None else _non_negative_number(now, "now")
    cutoff = current - window
    connection = _connect(state_file)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM rate_events WHERE bucket = ? AND occurred_at <= ?",
            (name, cutoff),
        )
        count = connection.execute(
            "SELECT COUNT(*) FROM rate_events WHERE bucket = ?",
            (name,),
        ).fetchone()[0]
        if int(count) >= maximum:
            connection.rollback()
            raise SharedRateLimitExceeded(f"provider bucket is full: {name}")
        connection.execute(
            "INSERT INTO rate_events(bucket, occurred_at) VALUES (?, ?)",
            (name, current),
        )
        connection.commit()
    except SharedRateLimitExceeded:
        raise
    except sqlite3.Error as exc:
        _rollback(connection)
        raise SharedRateLimitUnavailable(
            "provider rate-limit state is unavailable"
        ) from exc
    finally:
        connection.close()


def reserve_shared_request_slot(
    bucket: str,
    *,
    min_interval: float,
    max_wait: float,
    state_file: str | os.PathLike[str] | None = None,
    now: float | None = None,
) -> float:
    """Reserve one cross-process provider slot and return its bounded wait."""

    name = _bucket_name(bucket)
    interval = _non_negative_number(min_interval, "min_interval")
    wait_budget = _non_negative_number(max_wait, "max_wait")
    if interval == 0:
        return 0.0
    current = time.time() if now is None else _non_negative_number(now, "now")
    connection = _connect(state_file)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT next_at FROM request_slots WHERE bucket = ?",
            (name,),
        ).fetchone()
        previous = float(row[0]) if row is not None else current
        slot = max(current, previous)
        wait = max(0.0, slot - current)
        if wait > wait_budget:
            connection.rollback()
            raise SharedRateLimitExceeded(f"provider queue is full: {name}")
        connection.execute(
            "INSERT INTO request_slots(bucket, next_at) VALUES (?, ?) "
            "ON CONFLICT(bucket) DO UPDATE SET next_at = excluded.next_at",
            (name, slot + interval),
        )
        connection.commit()
        return wait
    except SharedRateLimitExceeded:
        raise
    except sqlite3.Error as exc:
        _rollback(connection)
        raise SharedRateLimitUnavailable(
            "provider rate-limit state is unavailable"
        ) from exc
    finally:
        connection.close()


__all__ = [
    "SharedRateLimitExceeded",
    "SharedRateLimitUnavailable",
    "consume_shared_rate_budget",
    "reserve_shared_request_slot",
]
