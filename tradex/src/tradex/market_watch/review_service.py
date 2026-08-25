"""Single runtime owner for manual and scheduled post-market review creation."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from tradex.market_calendar import (
    CalendarDayStatus,
    TradingSessionPhase,
    a_share_session,
)

from .contracts import MarketPhase, MarketWatchSnapshotV1
from .review import (
    PostMarketReviewV1,
    ReviewTrigger,
    build_post_market_review,
    build_post_market_review_presentation,
    evaluate_review_outcome,
)
from .review_evidence import (
    DailyMarketReviewEvidenceV1,
    collect_daily_market_review_evidence,
)
from .review_store import (
    ARCHIVE_CONTRACT,
    ARCHIVE_SCHEMA_VERSION,
    PostMarketReviewStore,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
MANUAL_REVIEW_START = time(20, 30)
AUTOMATIC_REVIEW_START = time(21, 0)
AUTOMATIC_RETRY_SECONDS = 10 * 60
AUTOMATIC_MAX_ATTEMPTS = 3


class PostMarketReviewError(RuntimeError):
    """Base error carrying a user-safe message."""

    def __init__(self, safe_message: str, *, cause: Exception | None = None) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.cause = cause


class ReviewTooEarlyError(PostMarketReviewError):
    pass


class ReviewCalendarError(PostMarketReviewError):
    pass


class ReviewSnapshotUnavailableError(PostMarketReviewError):
    pass


class PostMarketReviewService:
    """Create at most one immutable review per verified Shanghai trade date."""

    def __init__(
        self,
        snapshot_loader: Callable[[], MarketWatchSnapshotV1 | Mapping[str, Any]],
        store: PostMarketReviewStore,
        *,
        clock: Callable[[], datetime] | None = None,
        evidence_loader: Callable[
            [MarketWatchSnapshotV1, datetime],
            DailyMarketReviewEvidenceV1 | Mapping[str, Any],
        ]
        | None = None,
        history_loader: Callable[[date], Sequence[Mapping[str, Any]]] | None = None,
    ) -> None:
        self._snapshot_loader = snapshot_loader
        self.store = store
        self._clock = clock or (lambda: datetime.now(SHANGHAI))
        self._evidence_loader = evidence_loader
        self._history_loader = history_loader
        self._lock = threading.RLock()
        self._auto_date: date | None = None
        self._auto_attempts = 0
        self._auto_last_attempt: datetime | None = None

    @staticmethod
    def _local(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("review clock must return a timezone-aware datetime")
        return value.astimezone(SHANGHAI)

    def _now(self, value: datetime | None = None) -> datetime:
        return self._local(value or self._clock())

    @staticmethod
    def _validate_due(now: datetime, trigger: ReviewTrigger) -> None:
        session = a_share_session(now)
        if session.calendar_status != CalendarDayStatus.VERIFIED_TRADING_DAY:
            if session.calendar_status == CalendarDayStatus.UNVERIFIED:
                message = "当日交易日历尚未核验，不能生成日复盘。"
            else:
                message = "当日不是交易日，不生成日复盘。"
            raise ReviewCalendarError(message)
        threshold = (
            MANUAL_REVIEW_START
            if trigger == ReviewTrigger.MANUAL
            else AUTOMATIC_REVIEW_START
        )
        if now.time().replace(tzinfo=None) < threshold:
            label = "20:30" if trigger == ReviewTrigger.MANUAL else "21:00"
            raise ReviewTooEarlyError(f"当日 {label} 后才允许生成这份日复盘。")
        if session.phase != TradingSessionPhase.CLOSED:
            raise ReviewTooEarlyError("市场尚未进入收盘阶段，不能生成日复盘。")

    def _load_snapshot(self, now: datetime) -> MarketWatchSnapshotV1:
        try:
            snapshot = MarketWatchSnapshotV1.model_validate(self._snapshot_loader())
        except Exception as exc:
            raise ReviewSnapshotUnavailableError(
                "收盘盘面快照暂不可用，日复盘尚未生成。",
                cause=exc,
            ) from exc
        if snapshot.market_state.trading_date != now.date():
            raise ReviewSnapshotUnavailableError(
                "当前盘面快照不属于今天，日复盘尚未生成。"
            )
        if snapshot.market_state.phase != MarketPhase.CLOSED:
            raise ReviewSnapshotUnavailableError(
                "当前盘面快照尚未确认收盘，日复盘尚未生成。"
            )
        return snapshot

    def _load_evidence(
        self,
        snapshot: MarketWatchSnapshotV1,
        now: datetime,
    ) -> DailyMarketReviewEvidenceV1:
        try:
            if self._evidence_loader is not None:
                raw = self._evidence_loader(snapshot, now)
            else:
                history = (
                    self._history_loader(snapshot.market_state.trading_date)
                    if self._history_loader is not None
                    else ()
                )
                raw = collect_daily_market_review_evidence(
                    snapshot,
                    history_samples=history,
                    collected_at=now,
                )
            evidence = DailyMarketReviewEvidenceV1.model_validate(raw)
        except Exception as exc:
            raise ReviewSnapshotUnavailableError(
                "全市场收盘证据暂不可用，日复盘尚未生成。",
                cause=exc,
            ) from exc
        if evidence.trade_date != now.date():
            raise ReviewSnapshotUnavailableError(
                "全市场收盘证据不属于今天，日复盘尚未生成。"
            )
        return evidence

    def _evaluate_pending(
        self,
        evidence: DailyMarketReviewEvidenceV1,
    ) -> None:
        for prior in self.store.list_unevaluated_before(
            evidence.trade_date,
            limit=30,
        ):
            outcome = evaluate_review_outcome(prior, evidence)
            self.store.record_outcome(outcome)

    def generate(
        self,
        *,
        trigger: ReviewTrigger = ReviewTrigger.MANUAL,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = self._now(now)
        with self._lock:
            existing = self.store.get(current.date())
            if existing is not None:
                return self._result("existing", existing)
            self._validate_due(current, trigger)
            snapshot = self._load_snapshot(current)
            evidence = self._load_evidence(snapshot, current)
            self._evaluate_pending(evidence)
            learning = self.store.learning_summary(limit=20)
            review = build_post_market_review(
                evidence,
                generated_at=current,
                trigger=trigger,
                learning=learning,
            )
            action, stored = self.store.record(review)
            return self._result(action, stored)

    def _presentation(self, review: PostMarketReviewV1) -> dict[str, Any]:
        previous = self.store.get_previous_before(review.trade_date)
        return build_post_market_review_presentation(
            review,
            previous_review=previous,
        ).model_dump(mode="json")

    def _result(self, action: str, review: PostMarketReviewV1) -> dict[str, Any]:
        return {
            "contract": "post_market_review_result.v1",
            "schema_version": 1,
            "action": action,
            "review": review.model_dump(mode="json"),
            "presentation": self._presentation(review),
        }

    def history(
        self,
        *,
        trade_date: date | str | None = None,
        limit: int = 90,
    ) -> dict[str, Any]:
        dates = self.store.list_dates(limit=limit)
        target = trade_date or (dates[0]["trade_date"] if dates else None)
        review = self.store.get(target) if target is not None else None
        outcome = self.store.get_outcome(review.review_id) if review is not None else None
        presentation = (
            self._presentation(review)
            if review is not None
            else None
        )
        return {
            "contract": ARCHIVE_CONTRACT,
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "trade_date": review.trade_date.isoformat() if review is not None else None,
            "dates": dates,
            "review": review.model_dump(mode="json") if review is not None else None,
            "presentation": presentation,
            "outcome": outcome.model_dump(mode="json") if outcome is not None else None,
            "learning": self.store.learning_summary(limit=20).model_dump(mode="json"),
            "schedule": {
                "manual_after": "20:30",
                "automatic_if_missing_after": "21:00",
                "timezone": "Asia/Shanghai",
            },
        }

    def maybe_generate_automatic(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = self._now(now)
        session = a_share_session(current)
        if (
            session.calendar_status != CalendarDayStatus.VERIFIED_TRADING_DAY
            or current.time().replace(tzinfo=None) < AUTOMATIC_REVIEW_START
        ):
            return {"action": "not_due"}
        with self._lock:
            existing = self.store.get(current.date())
            if existing is not None:
                return self._result("existing", existing)
            if self._auto_date != current.date():
                self._auto_date = current.date()
                self._auto_attempts = 0
                self._auto_last_attempt = None
            if self._auto_attempts >= AUTOMATIC_MAX_ATTEMPTS:
                return {"action": "retry_exhausted"}
            if (
                self._auto_last_attempt is not None
                and (current - self._auto_last_attempt).total_seconds()
                < AUTOMATIC_RETRY_SECONDS
            ):
                return {"action": "retry_cooldown"}
            self._auto_attempts += 1
            self._auto_last_attempt = current
        return self.generate(trigger=ReviewTrigger.AUTOMATIC, now=current)


__all__ = [
    "AUTOMATIC_MAX_ATTEMPTS",
    "AUTOMATIC_RETRY_SECONDS",
    "AUTOMATIC_REVIEW_START",
    "MANUAL_REVIEW_START",
    "PostMarketReviewError",
    "PostMarketReviewService",
    "ReviewCalendarError",
    "ReviewSnapshotUnavailableError",
    "ReviewTooEarlyError",
]
