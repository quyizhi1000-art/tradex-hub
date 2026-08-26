"""Immutable SQLite archive for daily stock selections and realized outcomes."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from .contracts import DailyStockSelectionOutcomeV1, DailyStockSelectionV1
from .engine import DEFAULT_SELECTION_CONFIG


ARCHIVE_CONTRACT = "daily_stock_selection_archive.v1"
ARCHIVE_SCHEMA_VERSION = 1
ENV_DB_PATH = "TRADEX_DAILY_STOCK_SELECTION_DB"


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _date(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else date.fromisoformat(str(value)).isoformat()


class DailyStockSelectionStore:
    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        configured = db_path or os.environ.get(ENV_DB_PATH)
        if configured is None:
            configured = Path.home() / ".tradex" / "daily_stock_selection.sqlite3"
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
            self._closed = True
            self._connection.close()
            raise

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_stock_selection_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_stock_selections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    config_version TEXT NOT NULL,
                    selection_id TEXT NOT NULL UNIQUE,
                    generated_at TEXT NOT NULL,
                    source_quality TEXT NOT NULL,
                    selected_count INTEGER NOT NULL,
                    payload_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (trade_date, config_version)
                );
                CREATE INDEX IF NOT EXISTS idx_daily_stock_selection_date
                    ON daily_stock_selections (trade_date DESC);
                CREATE TABLE IF NOT EXISTS daily_stock_selection_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    selection_id TEXT NOT NULL UNIQUE,
                    signal_trade_date TEXT NOT NULL,
                    evaluation_trade_date TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    excess_return_pct REAL,
                    payload_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (selection_id) REFERENCES daily_stock_selections(selection_id)
                );
                CREATE INDEX IF NOT EXISTS idx_daily_stock_selection_outcome_date
                    ON daily_stock_selection_outcomes (evaluation_trade_date DESC);
                """
            )
            expected = {
                "contract": ARCHIVE_CONTRACT,
                "schema_version": str(ARCHIVE_SCHEMA_VERSION),
            }
            for key, value in expected.items():
                self._connection.execute(
                    "INSERT OR IGNORE INTO daily_stock_selection_meta (key, value) VALUES (?, ?)",
                    (key, value),
                )
                row = self._connection.execute(
                    "SELECT value FROM daily_stock_selection_meta WHERE key = ?",
                    (key,),
                ).fetchone()
                if row is None or row["value"] != value:
                    raise RuntimeError(f"incompatible daily stock selection archive {key}")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("daily stock selection store is closed")

    @staticmethod
    def _selection(row: sqlite3.Row | None) -> DailyStockSelectionV1 | None:
        return (
            DailyStockSelectionV1.model_validate(json.loads(row["payload_json"]))
            if row is not None
            else None
        )

    @staticmethod
    def _outcome(row: sqlite3.Row | None) -> DailyStockSelectionOutcomeV1 | None:
        return (
            DailyStockSelectionOutcomeV1.model_validate(json.loads(row["payload_json"]))
            if row is not None
            else None
        )

    def get_current(self, trade_date: date | str) -> DailyStockSelectionV1 | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT payload_json FROM daily_stock_selections
                WHERE trade_date = ? AND config_version = ?
                """,
                (_date(trade_date), DEFAULT_SELECTION_CONFIG.config_version),
            ).fetchone()
        return self._selection(row)

    def get(self, trade_date: date | str) -> DailyStockSelectionV1 | None:
        """Read the current archive version, falling back to preserved history."""

        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT payload_json FROM daily_stock_selections
                WHERE trade_date = ?
                ORDER BY CASE WHEN config_version = ? THEN 0 ELSE 1 END, id DESC
                LIMIT 1
                """,
                (_date(trade_date), DEFAULT_SELECTION_CONFIG.config_version),
            ).fetchone()
        return self._selection(row)

    def get_previous_before(self, trade_date: date | str) -> DailyStockSelectionV1 | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT payload_json FROM daily_stock_selections
                WHERE trade_date = (
                    SELECT MAX(trade_date) FROM daily_stock_selections
                    WHERE trade_date < ?
                )
                ORDER BY CASE WHEN config_version = ? THEN 0 ELSE 1 END, id DESC
                LIMIT 1
                """,
                (_date(trade_date), DEFAULT_SELECTION_CONFIG.config_version),
            ).fetchone()
        return self._selection(row)

    def record(
        self,
        selection: DailyStockSelectionV1 | Mapping[str, Any],
    ) -> tuple[str, DailyStockSelectionV1]:
        canonical = DailyStockSelectionV1.model_validate(selection)
        payload = canonical.model_dump(mode="json")
        payload_json = _json(payload)
        payload_digest = _digest(payload)
        with self._lock:
            self._ensure_open()
            with self._connection:
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO daily_stock_selections (
                        trade_date, config_version, selection_id, generated_at,
                        source_quality, selected_count, payload_digest, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        canonical.trade_date.isoformat(),
                        canonical.config_version,
                        canonical.selection_id,
                        canonical.generated_at.isoformat(),
                        canonical.source_quality,
                        canonical.selected_count,
                        payload_digest,
                        payload_json,
                    ),
                )
                row = self._connection.execute(
                    """
                    SELECT payload_digest, payload_json FROM daily_stock_selections
                    WHERE trade_date = ? AND config_version = ?
                    """,
                    (canonical.trade_date.isoformat(), canonical.config_version),
                ).fetchone()
        if row is None:
            raise RuntimeError("daily stock selection insert did not produce a row")
        if row["payload_digest"] != payload_digest:
            raise RuntimeError("daily stock selection archive is immutable")
        stored = self._selection(row)
        if stored is None:
            raise RuntimeError("daily stock selection could not be decoded")
        return ("inserted" if cursor.rowcount == 1 else "existing", stored)

    def record_outcome(
        self,
        outcome: DailyStockSelectionOutcomeV1 | Mapping[str, Any],
    ) -> tuple[str, DailyStockSelectionOutcomeV1]:
        canonical = DailyStockSelectionOutcomeV1.model_validate(outcome)
        payload = canonical.model_dump(mode="json")
        payload_json = _json(payload)
        payload_digest = _digest(payload)
        with self._lock:
            self._ensure_open()
            with self._connection:
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO daily_stock_selection_outcomes (
                        selection_id, signal_trade_date, evaluation_trade_date,
                        verdict, excess_return_pct, payload_digest, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        canonical.selection_id,
                        canonical.signal_trade_date.isoformat(),
                        canonical.evaluation_trade_date.isoformat(),
                        canonical.verdict,
                        canonical.excess_return_pct,
                        payload_digest,
                        payload_json,
                    ),
                )
                row = self._connection.execute(
                    "SELECT payload_digest, payload_json FROM daily_stock_selection_outcomes WHERE selection_id = ?",
                    (canonical.selection_id,),
                ).fetchone()
        if row is None:
            raise RuntimeError("daily stock selection outcome insert did not produce a row")
        if row["payload_digest"] != payload_digest:
            raise RuntimeError("daily stock selection outcome is immutable")
        stored = self._outcome(row)
        if stored is None:
            raise RuntimeError("daily stock selection outcome could not be decoded")
        return ("inserted" if cursor.rowcount == 1 else "existing", stored)

    def get_outcome(self, selection_id: str) -> DailyStockSelectionOutcomeV1 | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT payload_json FROM daily_stock_selection_outcomes WHERE selection_id = ?",
                (str(selection_id),),
            ).fetchone()
        return self._outcome(row)

    def list_dates(self, *, limit: int = 90) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not 1 <= int(limit) <= 365:
            raise ValueError("limit must be between 1 and 365")
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT selections.trade_date, selections.selection_id,
                       selections.config_version, selections.generated_at, selections.source_quality,
                       selections.selected_count, outcomes.verdict,
                       outcomes.excess_return_pct, outcomes.evaluation_trade_date
                FROM daily_stock_selections AS selections
                LEFT JOIN daily_stock_selection_outcomes AS outcomes
                  ON outcomes.selection_id = selections.selection_id
                WHERE selections.id = (
                    SELECT candidate.id FROM daily_stock_selections AS candidate
                    WHERE candidate.trade_date = selections.trade_date
                    ORDER BY CASE WHEN candidate.config_version = ? THEN 0 ELSE 1 END,
                             candidate.id DESC
                    LIMIT 1
                )
                ORDER BY selections.trade_date DESC LIMIT ?
                """,
                (DEFAULT_SELECTION_CONFIG.config_version, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_outcomes(self, *, limit: int = 30) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not 1 <= int(limit) <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT payload_json FROM daily_stock_selection_outcomes
                ORDER BY evaluation_trade_date DESC, id DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True


__all__ = [
    "ARCHIVE_CONTRACT",
    "ARCHIVE_SCHEMA_VERSION",
    "DailyStockSelectionStore",
]
