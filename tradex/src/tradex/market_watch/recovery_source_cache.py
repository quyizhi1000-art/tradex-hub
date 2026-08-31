"""Compact, success-only cache for same-day post-close source matrices."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zlib
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
ENV_DB_PATH = "TRADEX_MARKET_WATCH_RECOVERY_SOURCE_DB"
CONTRACT = "market_watch_recovery_source_matrix.v1"
SCHEMA_VERSION = 1


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _time_key(value: time) -> str:
    if value.second or value.microsecond:
        raise ValueError("recovery source minute must be minute aligned")
    return value.strftime("%H:%M")


def _positive_number(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return float(value)


def _nonnegative_number(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{name} must be a nonnegative number")
    return float(value)


class RecoverySourceMatrixStore:
    """Persist the exact normalized inputs already accepted by reconstruction.

    The cache owns no provider calls and never widens recovery semantics.  A row
    is reusable only for the same trading day and when it contains every exact
    minute requested by the restarted Collector.
    """

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        configured = db_path or os.environ.get(ENV_DB_PATH)
        if configured is None:
            configured = Path.home() / ".tradex" / "market_watch_recovery_source.sqlite3"
        resolved = Path(configured).expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(resolved)

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS recovery_source_matrices (
                trade_date TEXT PRIMARY KEY,
                contract TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                provider_as_of TEXT NOT NULL,
                instrument_count INTEGER NOT NULL,
                minute_count INTEGER NOT NULL,
                payload_digest TEXT NOT NULL,
                payload_blob BLOB NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    def record(
        self,
        *,
        trade_date: date,
        provider_as_of: datetime,
        previous_close: Mapping[str, float],
        stock_prices: Mapping[time, Mapping[str, float]],
        index_points: Mapping[str, Mapping[time, Any]],
        index_previous_close: Mapping[str, tuple[str, float]],
    ) -> dict[str, Any]:
        if provider_as_of.tzinfo is None or provider_as_of.utcoffset() is None:
            raise ValueError("recovery source provider_as_of must include a timezone")
        observed = provider_as_of.astimezone(SHANGHAI)
        if observed.date() != trade_date or observed.time() < time(15, 0):
            raise ValueError("recovery source cache requires a proven same-day close")
        closes = {
            str(instrument_id): _positive_number(value, name="previous close")
            for instrument_id, value in previous_close.items()
        }
        if not closes:
            raise ValueError("recovery source cache requires an instrument universe")
        expected_ids = set(closes)
        prices: dict[str, dict[str, float]] = {}
        for minute, values in stock_prices.items():
            key = _time_key(minute)
            normalized = {
                str(instrument_id): _positive_number(value, name="stock minute price")
                for instrument_id, value in values.items()
            }
            if set(normalized) != expected_ids:
                raise ValueError("recovery source cache requires complete stock-minute coverage")
            prices[key] = normalized
        if not prices:
            raise ValueError("recovery source cache requires at least one exact minute")
        required_minutes = set(prices)
        indices: dict[str, dict[str, dict[str, float]]] = {}
        for instrument_id, series in index_points.items():
            normalized_series: dict[str, dict[str, float]] = {}
            for minute, point in series.items():
                key = _time_key(minute)
                close = point.get("close") if isinstance(point, Mapping) else point.close
                amount = (
                    point.get("amount_cny")
                    if isinstance(point, Mapping)
                    else point.amount_cny
                )
                normalized_series[key] = {
                    "close": _positive_number(close, name="index minute close"),
                    "amount_cny": _nonnegative_number(
                        amount,
                        name="index minute amount",
                    ),
                }
            if not required_minutes.issubset(normalized_series):
                raise ValueError("recovery source cache requires complete index-minute coverage")
            indices[str(instrument_id)] = normalized_series
        identities = {
            str(instrument_id): [str(name), _positive_number(value, name="index previous close")]
            for instrument_id, (name, value) in index_previous_close.items()
        }
        payload = {
            "contract": CONTRACT,
            "schema_version": SCHEMA_VERSION,
            "trade_date": trade_date.isoformat(),
            "provider_as_of": observed.isoformat(timespec="seconds"),
            "previous_close": closes,
            "stock_prices": prices,
            "index_points": indices,
            "index_previous_close": identities,
        }
        raw = _canonical_bytes(payload)
        digest = hashlib.sha256(raw).hexdigest()
        blob = zlib.compress(raw, level=6)
        updated_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        with sqlite3.connect(self.db_path, timeout=5) as connection:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            self._initialize(connection)
            existing = connection.execute(
                "SELECT minute_count FROM recovery_source_matrices WHERE trade_date = ?",
                (trade_date.isoformat(),),
            ).fetchone()
            if existing is not None and int(existing[0]) > len(prices):
                return {"action": "preserved", "payload_digest": digest}
            action = "inserted" if existing is None else "updated"
            connection.execute(
                """
                INSERT INTO recovery_source_matrices (
                    trade_date, contract, schema_version, provider_as_of,
                    instrument_count, minute_count, payload_digest, payload_blob,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    contract = excluded.contract,
                    schema_version = excluded.schema_version,
                    provider_as_of = excluded.provider_as_of,
                    instrument_count = excluded.instrument_count,
                    minute_count = excluded.minute_count,
                    payload_digest = excluded.payload_digest,
                    payload_blob = excluded.payload_blob,
                    updated_at = excluded.updated_at
                """,
                (
                    trade_date.isoformat(),
                    CONTRACT,
                    SCHEMA_VERSION,
                    observed.isoformat(timespec="seconds"),
                    len(closes),
                    len(prices),
                    digest,
                    blob,
                    updated_at,
                ),
            )
            connection.execute(
                """
                DELETE FROM recovery_source_matrices
                WHERE trade_date NOT IN (
                    SELECT trade_date FROM recovery_source_matrices
                    ORDER BY trade_date DESC LIMIT 2
                )
                """
            )
        return {"action": action, "payload_digest": digest}

    def read(
        self,
        trade_date: date,
        required_times: set[time],
    ) -> dict[str, Any] | None:
        if not required_times:
            raise ValueError("recovery source cache read requires exact minutes")
        if not Path(self.db_path).exists():
            return None
        connection = sqlite3.connect(
            f"file:{self.db_path}?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM recovery_source_matrices WHERE trade_date = ?",
                (trade_date.isoformat(),),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        finally:
            connection.close()
        if row is None or row["contract"] != CONTRACT or int(row["schema_version"]) != SCHEMA_VERSION:
            return None
        raw = zlib.decompress(row["payload_blob"])
        if hashlib.sha256(raw).hexdigest() != row["payload_digest"]:
            raise RuntimeError("recovery source cache digest mismatch")
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("trade_date") != trade_date.isoformat():
            raise RuntimeError("recovery source cache trade date mismatch")
        provider_as_of = datetime.fromisoformat(str(payload.get("provider_as_of") or ""))
        if (
            provider_as_of.tzinfo is None
            or provider_as_of.astimezone(SHANGHAI).date() != trade_date
            or provider_as_of.astimezone(SHANGHAI).time() < time(15, 0)
        ):
            raise RuntimeError("recovery source cache lacks same-day close proof")
        required_keys = {_time_key(value) for value in required_times}
        stored_prices = payload.get("stock_prices") or {}
        if not required_keys.issubset(stored_prices):
            return None
        return payload


__all__ = ["RecoverySourceMatrixStore"]
