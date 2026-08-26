"""Immutable SQLite archive for daily post-market reviews and outcomes."""

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

from .review import (
    OutcomeVerdict,
    PostMarketReviewV1,
    REVIEW_CONFIG_VERSION,
    ReviewLearningV1,
    ReviewOutcomeV1,
)


ARCHIVE_CONTRACT = "post_market_review_archive.v1"
ARCHIVE_SCHEMA_VERSION = 1
ENV_DB_PATH = "TRADEX_POST_MARKET_REVIEW_DB"
DEFAULT_HISTORY_LIMIT = 90


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _trade_date(value: date | str) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value).strip()).isoformat()


class PostMarketReviewStore:
    """Own one immutable review per trade date and append-only evaluations."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        configured = db_path or os.environ.get(ENV_DB_PATH)
        if configured is None:
            configured = Path.home() / ".tradex" / "post_market_reviews.sqlite3"
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
                CREATE TABLE IF NOT EXISTS post_market_review_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS post_market_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    review_id TEXT NOT NULL UNIQUE,
                    contract TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    config_version TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    quality TEXT NOT NULL,
                    outlook_bias TEXT NOT NULL,
                    outlook_confidence TEXT NOT NULL,
                    source_snapshot_id TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (trade_date, config_version)
                );

                CREATE INDEX IF NOT EXISTS idx_post_market_review_dates
                    ON post_market_reviews (config_version, trade_date DESC);

                CREATE TABLE IF NOT EXISTS post_market_review_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_id TEXT NOT NULL UNIQUE,
                    forecast_trade_date TEXT NOT NULL,
                    evaluated_on TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    realized_bias TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (review_id) REFERENCES post_market_reviews(review_id)
                );

                CREATE INDEX IF NOT EXISTS idx_post_market_review_outcome_dates
                    ON post_market_review_outcomes (evaluated_on DESC);
                """
            )
            expected = {
                "contract": ARCHIVE_CONTRACT,
                "schema_version": str(ARCHIVE_SCHEMA_VERSION),
            }
            for key, value in expected.items():
                self._connection.execute(
                    "INSERT OR IGNORE INTO post_market_review_meta (key, value) VALUES (?, ?)",
                    (key, value),
                )
                row = self._connection.execute(
                    "SELECT value FROM post_market_review_meta WHERE key = ?",
                    (key,),
                ).fetchone()
                if row is None or row["value"] != value:
                    raise RuntimeError(f"incompatible post-market review archive {key}")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("post-market review store is closed")

    @staticmethod
    def _decode_review(row: sqlite3.Row | None) -> PostMarketReviewV1 | None:
        if row is None:
            return None
        return PostMarketReviewV1.model_validate(json.loads(row["payload_json"]))

    @staticmethod
    def _decode_outcome(row: sqlite3.Row | None) -> ReviewOutcomeV1 | None:
        if row is None:
            return None
        return ReviewOutcomeV1.model_validate(json.loads(row["payload_json"]))

    def get(self, trade_date: date | str) -> PostMarketReviewV1 | None:
        """Return the archive for the active review policy only."""

        target = _trade_date(trade_date)
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT payload_json FROM post_market_reviews
                WHERE trade_date = ? AND config_version = ?
                """,
                (target, REVIEW_CONFIG_VERSION),
            ).fetchone()
        return self._decode_review(row)

    def get_current_or_latest(self, trade_date: date | str) -> PostMarketReviewV1 | None:
        """Prefer the active policy, while keeping older immutable dates readable."""

        target = _trade_date(trade_date)
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT payload_json FROM post_market_reviews
                WHERE trade_date = ?
                ORDER BY CASE WHEN config_version = ? THEN 0 ELSE 1 END,
                         id DESC
                LIMIT 1
                """,
                (target, REVIEW_CONFIG_VERSION),
            ).fetchone()
        return self._decode_review(row)

    def get_previous_before(self, trade_date: date | str) -> PostMarketReviewV1 | None:
        """Return the latest same-policy archive before ``trade_date``."""

        target = _trade_date(trade_date)
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT payload_json FROM post_market_reviews
                WHERE trade_date < ?
                ORDER BY trade_date DESC,
                         CASE WHEN config_version = ? THEN 0 ELSE 1 END,
                         id DESC
                LIMIT 1
                """,
                (target, REVIEW_CONFIG_VERSION),
            ).fetchone()
        return self._decode_review(row)

    def get_outcome(self, review_id: str) -> ReviewOutcomeV1 | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT payload_json FROM post_market_review_outcomes WHERE review_id = ?",
                (str(review_id),),
            ).fetchone()
        return self._decode_outcome(row)

    def record(
        self,
        review: PostMarketReviewV1 | Mapping[str, Any],
    ) -> tuple[str, PostMarketReviewV1]:
        canonical = PostMarketReviewV1.model_validate(review)
        payload = canonical.model_dump(mode="json")
        payload_json = _canonical_json(payload)
        payload_digest = _digest(payload)
        with self._lock:
            self._ensure_open()
            with self._connection:
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO post_market_reviews (
                        trade_date, review_id, contract, schema_version,
                        config_version, generated_at, trigger, quality,
                        outlook_bias, outlook_confidence, source_snapshot_id,
                        payload_digest, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        canonical.trade_date.isoformat(),
                        canonical.review_id,
                        canonical.contract,
                        canonical.schema_version,
                        canonical.config_version,
                        canonical.generated_at.isoformat(),
                        canonical.trigger.value,
                        canonical.quality.value,
                        canonical.next_day_outlook.bias.value,
                        canonical.next_day_outlook.confidence.value,
                        canonical.source_snapshot_id,
                        payload_digest,
                        payload_json,
                    ),
                )
                row = self._connection.execute(
                    """
                    SELECT payload_json FROM post_market_reviews
                    WHERE trade_date = ? AND config_version = ?
                    """,
                    (canonical.trade_date.isoformat(), canonical.config_version),
                ).fetchone()
        stored = self._decode_review(row)
        if stored is None:
            raise RuntimeError("post-market review insert did not produce a row")
        return ("inserted" if cursor.rowcount == 1 else "existing", stored)

    def record_outcome(
        self,
        outcome: ReviewOutcomeV1 | Mapping[str, Any],
    ) -> tuple[str, ReviewOutcomeV1]:
        canonical = ReviewOutcomeV1.model_validate(outcome)
        payload = canonical.model_dump(mode="json")
        payload_json = _canonical_json(payload)
        payload_digest = _digest(payload)
        with self._lock:
            self._ensure_open()
            with self._connection:
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO post_market_review_outcomes (
                        review_id, forecast_trade_date, evaluated_on, verdict,
                        realized_bias, payload_digest, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        canonical.review_id,
                        canonical.forecast_trade_date.isoformat(),
                        canonical.evaluated_on.isoformat(),
                        canonical.verdict.value,
                        canonical.realized_bias.value,
                        payload_digest,
                        payload_json,
                    ),
                )
                row = self._connection.execute(
                    "SELECT payload_digest, payload_json FROM post_market_review_outcomes WHERE review_id = ?",
                    (canonical.review_id,),
                ).fetchone()
        if row is None:
            raise RuntimeError("post-market review outcome insert did not produce a row")
        if row["payload_digest"] != payload_digest:
            raise RuntimeError("post-market review outcome is immutable")
        stored = self._decode_outcome(row)
        if stored is None:
            raise RuntimeError("post-market review outcome could not be decoded")
        return ("inserted" if cursor.rowcount == 1 else "existing", stored)

    def list_unevaluated_before(
        self,
        trade_date: date | str,
        *,
        limit: int = 30,
    ) -> list[PostMarketReviewV1]:
        target = _trade_date(trade_date)
        if isinstance(limit, bool) or not 1 <= int(limit) <= 90:
            raise ValueError("limit must be between 1 and 90")
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT reviews.payload_json
                FROM post_market_reviews AS reviews
                LEFT JOIN post_market_review_outcomes AS outcomes
                    ON outcomes.review_id = reviews.review_id
                WHERE reviews.config_version = ?
                    AND reviews.trade_date < ?
                    AND outcomes.review_id IS NULL
                ORDER BY reviews.trade_date ASC
                LIMIT ?
                """,
                (REVIEW_CONFIG_VERSION, target, int(limit)),
            ).fetchall()
        return [self._decode_review(row) for row in rows if row is not None]

    def list_dates(self, limit: int = DEFAULT_HISTORY_LIMIT) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not 1 <= int(limit) <= 365:
            raise ValueError("limit must be between 1 and 365")
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                WITH ranked AS (
                    SELECT reviews.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY reviews.trade_date
                               ORDER BY CASE WHEN reviews.config_version = ? THEN 0 ELSE 1 END,
                                        reviews.id DESC
                           ) AS revision_rank
                    FROM post_market_reviews AS reviews
                )
                SELECT reviews.trade_date, reviews.generated_at, reviews.trigger,
                       reviews.quality, reviews.outlook_bias,
                       reviews.outlook_confidence, reviews.review_id,
                       outcomes.verdict, outcomes.evaluated_on
                FROM ranked AS reviews
                LEFT JOIN post_market_review_outcomes AS outcomes
                    ON outcomes.review_id = reviews.review_id
                WHERE reviews.revision_rank = 1
                ORDER BY reviews.trade_date DESC
                LIMIT ?
                """,
                (REVIEW_CONFIG_VERSION, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def learning_summary(self, *, limit: int = 20) -> ReviewLearningV1:
        if isinstance(limit, bool) or not 1 <= int(limit) <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT verdict FROM post_market_review_outcomes
                ORDER BY evaluated_on DESC, id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        counts = {verdict: 0 for verdict in OutcomeVerdict}
        for row in rows:
            counts[OutcomeVerdict(row["verdict"])] += 1
        verifiable = len(rows) - counts[OutcomeVerdict.UNVERIFIABLE]
        support_ratio = (
            (
                counts[OutcomeVerdict.SUPPORTED]
                + 0.5 * counts[OutcomeVerdict.PARTIAL]
            )
            / verifiable
            if verifiable
            else None
        )
        if verifiable == 0:
            note = "尚无可核验的历史日复盘，先以低置信度积累样本。"
        elif verifiable < 5:
            note = f"已有 {verifiable} 份可核验结论，样本仍少，暂不提高置信度。"
        elif support_ratio is not None and support_ratio < 0.45:
            note = "近期历史支持度偏低，后续方向置信度已限制为弱。"
        else:
            note = "历史回看用于校准置信度，不替代当日收盘证据。"
        return ReviewLearningV1(
            evaluated_count=len(rows),
            supported_count=counts[OutcomeVerdict.SUPPORTED],
            partial_count=counts[OutcomeVerdict.PARTIAL],
            not_supported_count=counts[OutcomeVerdict.NOT_SUPPORTED],
            unverifiable_count=counts[OutcomeVerdict.UNVERIFIABLE],
            support_ratio=support_ratio,
            calibration_note=note,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def __enter__(self) -> "PostMarketReviewStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


__all__ = [
    "ARCHIVE_CONTRACT",
    "ARCHIVE_SCHEMA_VERSION",
    "DEFAULT_HISTORY_LIMIT",
    "ENV_DB_PATH",
    "PostMarketReviewStore",
]
