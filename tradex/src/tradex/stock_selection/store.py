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

from .contracts import (
    DailyStockSelectionOutcomeV1,
    DailyStockSelectionV1,
    StockSelectionStrategyOutcomeV1,
    StockSelectionStrategyResultV1,
)
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
                CREATE TABLE IF NOT EXISTS stock_selection_strategy_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    result_id TEXT NOT NULL UNIQUE,
                    generated_at TEXT NOT NULL,
                    source_snapshot_revision TEXT NOT NULL,
                    source_quality TEXT NOT NULL,
                    quality TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (trade_date, strategy_id, strategy_version)
                );
                CREATE INDEX IF NOT EXISTS idx_stock_selection_strategy_result_date
                    ON stock_selection_strategy_results (trade_date DESC, id);
                CREATE TABLE IF NOT EXISTS stock_selection_strategy_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    outcome_id TEXT NOT NULL UNIQUE,
                    result_id TEXT NOT NULL UNIQUE,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    signal_trade_date TEXT NOT NULL,
                    evaluation_trade_date TEXT NOT NULL,
                    evaluation_status TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (result_id)
                        REFERENCES stock_selection_strategy_results(result_id)
                );
                CREATE INDEX IF NOT EXISTS idx_stock_selection_strategy_outcome_date
                    ON stock_selection_strategy_outcomes (evaluation_trade_date DESC, id);
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

    @staticmethod
    def _strategy_result(
        row: sqlite3.Row | None,
    ) -> StockSelectionStrategyResultV1 | None:
        return (
            StockSelectionStrategyResultV1.model_validate(
                json.loads(row["payload_json"])
            )
            if row is not None
            else None
        )

    @staticmethod
    def _strategy_outcome(
        row: sqlite3.Row | None,
    ) -> StockSelectionStrategyOutcomeV1 | None:
        return (
            StockSelectionStrategyOutcomeV1.model_validate(
                json.loads(row["payload_json"])
            )
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

    def record_strategy_result(
        self,
        result: StockSelectionStrategyResultV1 | Mapping[str, Any],
    ) -> tuple[str, StockSelectionStrategyResultV1]:
        canonical = StockSelectionStrategyResultV1.model_validate(result)
        payload = canonical.model_dump(mode="json")
        payload_json = _json(payload)
        payload_digest = _digest(payload)
        with self._lock:
            self._ensure_open()
            with self._connection:
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO stock_selection_strategy_results (
                        trade_date, strategy_id, strategy_version, result_id,
                        generated_at, source_snapshot_revision, source_quality,
                        quality, payload_digest, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        canonical.trade_date.isoformat(),
                        canonical.strategy_id,
                        canonical.strategy_version,
                        canonical.result_id,
                        canonical.generated_at.isoformat(),
                        canonical.source_snapshot_revision,
                        canonical.source_quality,
                        canonical.quality,
                        payload_digest,
                        payload_json,
                    ),
                )
                row = self._connection.execute(
                    """
                    SELECT payload_digest, payload_json
                    FROM stock_selection_strategy_results
                    WHERE trade_date = ? AND strategy_id = ? AND strategy_version = ?
                    """,
                    (
                        canonical.trade_date.isoformat(),
                        canonical.strategy_id,
                        canonical.strategy_version,
                    ),
                ).fetchone()
        if row is None:
            raise RuntimeError("strategy result insert did not produce a row")
        if row["payload_digest"] != payload_digest:
            raise RuntimeError("stock selection strategy result is immutable")
        stored = self._strategy_result(row)
        if stored is None:
            raise RuntimeError("stock selection strategy result could not be decoded")
        return ("inserted" if cursor.rowcount == 1 else "existing", stored)

    def get_strategy_result(
        self,
        trade_date: date | str,
        strategy_id: str,
        *,
        strategy_version: str | None = None,
    ) -> StockSelectionStrategyResultV1 | None:
        parameters: list[Any] = [_date(trade_date), str(strategy_id)]
        version_clause = ""
        if strategy_version is not None:
            version_clause = " AND strategy_version = ?"
            parameters.append(str(strategy_version))
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                f"""
                SELECT payload_json FROM stock_selection_strategy_results
                WHERE trade_date = ? AND strategy_id = ?{version_clause}
                ORDER BY id DESC LIMIT 1
                """,
                parameters,
            ).fetchone()
        return self._strategy_result(row)

    def get_previous_strategy_result_before(
        self,
        trade_date: date | str,
        strategy_id: str,
        strategy_version: str,
    ) -> StockSelectionStrategyResultV1 | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT payload_json FROM stock_selection_strategy_results
                WHERE strategy_id = ? AND strategy_version = ?
                  AND trade_date = (
                    SELECT MAX(trade_date) FROM stock_selection_strategy_results
                    WHERE strategy_id = ? AND strategy_version = ? AND trade_date < ?
                  )
                ORDER BY id DESC LIMIT 1
                """,
                (
                    str(strategy_id),
                    str(strategy_version),
                    str(strategy_id),
                    str(strategy_version),
                    _date(trade_date),
                ),
            ).fetchone()
        return self._strategy_result(row)

    def list_strategy_results(
        self,
        trade_date: date | str,
    ) -> list[StockSelectionStrategyResultV1]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT payload_json FROM stock_selection_strategy_results
                WHERE trade_date = ? ORDER BY id
                """,
                (_date(trade_date),),
            ).fetchall()
        return [
            result
            for row in rows
            if (result := self._strategy_result(row)) is not None
        ]

    def list_strategy_dates(self, *, limit: int = 365) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not 1 <= int(limit) <= 365:
            raise ValueError("limit must be between 1 and 365")
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT trade_date, COUNT(*) AS strategy_count,
                       MAX(generated_at) AS generated_at,
                       GROUP_CONCAT(result_id, ',') AS result_ids
                FROM stock_selection_strategy_results
                GROUP BY trade_date
                ORDER BY trade_date DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_strategy_outcome(
        self,
        outcome: StockSelectionStrategyOutcomeV1 | Mapping[str, Any],
    ) -> tuple[str, StockSelectionStrategyOutcomeV1]:
        canonical = StockSelectionStrategyOutcomeV1.model_validate(outcome)
        payload = canonical.model_dump(mode="json")
        payload_json = _json(payload)
        payload_digest = _digest(payload)
        with self._lock:
            self._ensure_open()
            with self._connection:
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO stock_selection_strategy_outcomes (
                        outcome_id, result_id, strategy_id, strategy_version,
                        signal_trade_date, evaluation_trade_date,
                        evaluation_status, payload_digest, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        canonical.outcome_id,
                        canonical.result_id,
                        canonical.strategy_id,
                        canonical.strategy_version,
                        canonical.signal_trade_date.isoformat(),
                        canonical.evaluation_trade_date.isoformat(),
                        canonical.evaluation_status,
                        payload_digest,
                        payload_json,
                    ),
                )
                row = self._connection.execute(
                    """
                    SELECT payload_digest, payload_json
                    FROM stock_selection_strategy_outcomes WHERE result_id = ?
                    """,
                    (canonical.result_id,),
                ).fetchone()
        if row is None:
            raise RuntimeError("strategy outcome insert did not produce a row")
        if row["payload_digest"] != payload_digest:
            raise RuntimeError("stock selection strategy outcome is immutable")
        stored = self._strategy_outcome(row)
        if stored is None:
            raise RuntimeError("stock selection strategy outcome could not be decoded")
        return ("inserted" if cursor.rowcount == 1 else "existing", stored)

    def get_strategy_outcome(
        self,
        result_id: str,
    ) -> StockSelectionStrategyOutcomeV1 | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT payload_json FROM stock_selection_strategy_outcomes
                WHERE result_id = ?
                """,
                (str(result_id),),
            ).fetchone()
        return self._strategy_outcome(row)

    def list_strategy_outcomes(
        self,
        *,
        limit: int = 100,
    ) -> list[StockSelectionStrategyOutcomeV1]:
        if isinstance(limit, bool) or not 1 <= int(limit) <= 365:
            raise ValueError("limit must be between 1 and 365")
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT payload_json FROM stock_selection_strategy_outcomes
                ORDER BY evaluation_trade_date DESC, id DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [
            outcome
            for row in rows
            if (outcome := self._strategy_outcome(row)) is not None
        ]

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
