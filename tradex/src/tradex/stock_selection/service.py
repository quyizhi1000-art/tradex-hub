"""Single runtime owner for daily selection refresh, archive and evaluation."""

from __future__ import annotations

import copy
import logging
import statistics
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from tradex.data_gateway.stock_selection import fetch_daily_stock_factor_snapshot
from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1
from tradex.market_calendar import (
    CalendarDayStatus,
    TradingSessionPhase,
    a_share_session,
    calendar_day_status,
)

from .contracts import DailyStockSelectionOutcomeV1, DailyStockSelectionV1
from .engine import DEFAULT_SELECTION_CONFIG, select_daily_stocks
from .industry_display import load_selection_industry_display
from .strategies import (
    REGISTERED_STOCK_SELECTION_STRATEGIES,
    build_strategy_results,
    evaluate_strategy_result,
    strategy_catalog,
)
from .store import ARCHIVE_CONTRACT, ARCHIVE_SCHEMA_VERSION, DailyStockSelectionStore


SHANGHAI = ZoneInfo("Asia/Shanghai")
MANUAL_SELECTION_START = time(18, 0)
AUTOMATIC_SELECTION_START = time(18, 30)
AUTOMATIC_RETRY_SECONDS = 10 * 60
AUTOMATIC_MAX_ATTEMPTS = 3
ROUND_TRIP_COST_PCT = 0.15
GENERATION_CONTRACT = "daily_stock_selection_generation.v1"
GENERATION_SCHEMA_VERSION = 1


logger = logging.getLogger(__name__)


def _load_factor_snapshot_with_relationships(
    trade_date: date,
) -> DailyStockFactorSnapshotV1:
    return fetch_daily_stock_factor_snapshot(
        trade_date,
        apply_relationship_catalog=True,
    )


class DailyStockSelectionError(RuntimeError):
    def __init__(self, safe_message: str, *, cause: Exception | None = None) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.cause = cause


class SelectionTooEarlyError(DailyStockSelectionError):
    pass


class SelectionCalendarError(DailyStockSelectionError):
    pass


class SelectionDataUnavailableError(DailyStockSelectionError):
    pass


def _previous_trading_date(value: date) -> date | None:
    candidate = value - timedelta(days=1)
    for _ in range(14):
        status = calendar_day_status(candidate)
        if status is CalendarDayStatus.VERIFIED_TRADING_DAY:
            return candidate
        if status is CalendarDayStatus.UNVERIFIED:
            return None
        candidate -= timedelta(days=1)
    return None


def evaluate_next_session(
    selection: DailyStockSelectionV1,
    snapshot: DailyStockFactorSnapshotV1,
) -> DailyStockSelectionOutcomeV1:
    rows = {item.instrument_id: item for item in snapshot.factors}
    returns = []
    for candidate in selection.candidates:
        row = rows.get(candidate.instrument_id)
        if row is None or row.open <= 0:
            continue
        returns.append((row.close / row.open - 1.0) * 100.0 - ROUND_TRIP_COST_PCT)
    selected_count = selection.selected_count
    coverage = len(returns) / selected_count if selected_count else 0.0
    if coverage < 0.70 or not returns:
        portfolio = benchmark = excess = None
        verdict = "unverifiable"
    else:
        portfolio = statistics.mean(returns)
        benchmark = (snapshot.benchmark_close / snapshot.benchmark_open - 1.0) * 100.0
        excess = portfolio - benchmark
        verdict = "supported" if excess > 0.30 else "not_supported" if excess < -0.30 else "mixed"
    return DailyStockSelectionOutcomeV1(
        selection_id=selection.selection_id,
        signal_trade_date=selection.trade_date,
        evaluation_trade_date=snapshot.trade_date,
        evaluated_count=len(returns),
        selected_count=selected_count,
        coverage=coverage,
        portfolio_return_pct=portfolio,
        benchmark_return_pct=benchmark,
        excess_return_pct=excess,
        verdict=verdict,
    )


class DailyStockSelectionService:
    def __init__(
        self,
        store: DailyStockSelectionStore,
        *,
        factor_loader: Callable[[date], DailyStockFactorSnapshotV1] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self._factor_loader = factor_loader or _load_factor_snapshot_with_relationships
        self._clock = clock or (lambda: datetime.now(SHANGHAI))
        self._lock = threading.RLock()
        self._execution_lock = threading.Lock()
        self._generation_thread: threading.Thread | None = None
        self._generation_sequence = 0
        self._generation: dict[str, Any] = self._idle_generation()
        self._auto_date: date | None = None
        self._auto_attempts = 0
        self._auto_last_attempt: datetime | None = None

    @staticmethod
    def _local(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("selection clock must return a timezone-aware datetime")
        return value.astimezone(SHANGHAI)

    def _now(self, value: datetime | None = None) -> datetime:
        return self._local(value or self._clock())

    @staticmethod
    def _idle_generation() -> dict[str, Any]:
        return {
            "contract": GENERATION_CONTRACT,
            "schema_version": GENERATION_SCHEMA_VERSION,
            "job_id": None,
            "trade_date": None,
            "trigger": None,
            "state": "idle",
            "phase": "idle",
            "started_at": None,
            "finished_at": None,
            "error": None,
            "failure_code": None,
            "failed_phase": None,
            "result": None,
        }

    @staticmethod
    def _validate_due(now: datetime, *, automatic: bool) -> None:
        session = a_share_session(now)
        if session.calendar_status is not CalendarDayStatus.VERIFIED_TRADING_DAY:
            message = (
                "当日交易日历尚未核验，不能生成每日选股。"
                if session.calendar_status is CalendarDayStatus.UNVERIFIED
                else "当日不是交易日，不生成每日选股。"
            )
            raise SelectionCalendarError(message)
        threshold = AUTOMATIC_SELECTION_START if automatic else MANUAL_SELECTION_START
        if now.time().replace(tzinfo=None) < threshold:
            label = "18:30" if automatic else "18:00"
            raise SelectionTooEarlyError(f"当日 {label} 后才允许生成每日选股。")
        if session.phase is not TradingSessionPhase.CLOSED:
            raise SelectionTooEarlyError("市场尚未收盘，不能生成每日选股。")

    def _load(self, trade_date: date) -> DailyStockFactorSnapshotV1:
        try:
            snapshot = DailyStockFactorSnapshotV1.model_validate(
                self._factor_loader(trade_date)
            )
        except Exception as exc:
            raise SelectionDataUnavailableError(
                "Tushare 每日选股数据暂不可用，未生成候选池。",
                cause=exc,
            ) from exc
        if snapshot.trade_date != trade_date:
            raise SelectionDataUnavailableError("选股数据不属于请求的交易日。")
        return snapshot

    def _evaluate_previous(self, snapshot: DailyStockFactorSnapshotV1) -> None:
        previous_trade_date = _previous_trading_date(snapshot.trade_date)
        if previous_trade_date is None:
            return
        previous = self.store.get_previous_before(snapshot.trade_date)
        if previous is not None and previous.trade_date == previous_trade_date:
            self.store.record_outcome(evaluate_next_session(previous, snapshot))
        for strategy in REGISTERED_STOCK_SELECTION_STRATEGIES:
            if strategy.evaluation_policy == "not_defined":
                continue
            result = self.store.get_strategy_result(
                previous_trade_date,
                strategy.strategy_id,
                strategy_version=strategy.strategy_version,
            )
            if result is None:
                continue
            self.store.record_strategy_outcome(
                evaluate_strategy_result(result, snapshot)
            )

    def _strategy_results_complete(self, trade_date: date) -> bool:
        return all(
            self.store.get_strategy_result(
                trade_date,
                strategy.strategy_id,
                strategy_version=strategy.strategy_version,
            )
            is not None
            for strategy in REGISTERED_STOCK_SELECTION_STRATEGIES
        )

    def _generate_once(
        self,
        *,
        current: datetime,
        automatic: bool = False,
        trade_date: date | None = None,
        phase: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        target = trade_date or current.date()
        update_phase = phase or (lambda _value: None)
        with self._execution_lock:
            existing = self.store.get_current(target)
            if existing is not None and self._strategy_results_complete(target):
                return self._result("existing", existing)
            self._validate_target(current, target, automatic=automatic)
            update_phase("acquiring")
            snapshot = self._load(target)
            update_phase("selecting")
            missing_strategies = tuple(
                strategy for strategy in REGISTERED_STOCK_SELECTION_STRATEGIES
                if self.store.get_strategy_result(
                    target, strategy.strategy_id,
                    strategy_version=strategy.strategy_version,
                ) is None
            )
            if existing is not None and all(
                not strategy.requires_legacy_selection for strategy in missing_strategies
            ):
                # Independent snapshot strategies do not rebuild or replace the
                # immutable legacy candidate pool. Record their actual evidence
                # revision, acquisition metadata and backfill generation time.
                context = existing.model_copy(update={
                    "generated_at": current,
                    "source_quality": snapshot.metadata.quality.value,
                    "source_provider_as_of": snapshot.metadata.provider_as_of,
                })
                results = build_strategy_results(
                    snapshot, context, strategies=missing_strategies,
                )
                update_phase("archiving")
                for result in results:
                    self.store.record_strategy_result(result)
                return self._result("existing", existing)
            selection = select_daily_stocks(
                snapshot,
                config=DEFAULT_SELECTION_CONFIG,
                generated_at=existing.generated_at if existing is not None else current,
            )
            if existing is not None and selection.selection_id != existing.selection_id:
                raise SelectionDataUnavailableError(
                    "现有不可变选股档案与当前证据不一致，未补写策略档案。"
                )
            if selection.selected_count == 0:
                raise SelectionDataUnavailableError(
                    "当日没有通过完整性和流动性门槛的候选股票。"
                )
            strategy_results = build_strategy_results(snapshot, selection)
            update_phase("archiving")
            self._evaluate_previous(snapshot)
            action, stored = self.store.record(selection)
            stored_strategy_results = []
            for result in strategy_results:
                _strategy_action, stored_result = self.store.record_strategy_result(result)
                stored_strategy_results.append(stored_result)
            return self._result(
                action,
                stored,
                strategy_results=stored_strategy_results,
            )

    def generate(
        self,
        *,
        now: datetime | None = None,
        automatic: bool = False,
        trade_date: date | None = None,
    ) -> dict[str, Any]:
        """Synchronously generate a result for internal and test callers."""

        return self._generate_once(
            current=self._now(now),
            automatic=automatic,
            trade_date=trade_date,
        )

    def _validate_target(self, current: datetime, target: date, *, automatic: bool) -> None:
        if target > current.date():
            raise SelectionTooEarlyError("Cannot generate a future trading date.")
        if target == current.date():
            self._validate_due(current, automatic=automatic)
        else:
            # Validate the historical session without backdating generated_at.
            threshold = AUTOMATIC_SELECTION_START if automatic else MANUAL_SELECTION_START
            self._validate_due(
                datetime.combine(target, threshold, tzinfo=SHANGHAI),
                automatic=automatic,
            )

    def _completed_generation(
        self,
        *,
        current: datetime,
        trigger: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "contract": GENERATION_CONTRACT,
            "schema_version": GENERATION_SCHEMA_VERSION,
            "job_id": None,
            "trade_date": current.date().isoformat(),
            "trigger": trigger,
            "state": "succeeded",
            "phase": "completed",
            "started_at": None,
            "finished_at": self._now().isoformat(),
            "error": None,
            "failure_code": None,
            "failed_phase": None,
            "result": result,
        }

    def generation_status(self) -> dict[str, Any]:
        """Return a mutation-safe snapshot of the current background job."""

        with self._lock:
            return copy.deepcopy(self._generation)

    def _set_generation_phase(self, job_id: str, phase: str) -> None:
        with self._lock:
            if (
                self._generation.get("job_id") == job_id
                and self._generation.get("state") == "running"
            ):
                self._generation["phase"] = phase

    def _finish_generation(
        self,
        job_id: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        failure_code: str | None = None,
    ) -> None:
        with self._lock:
            if self._generation.get("job_id") != job_id:
                return
            succeeded = result is not None
            failed_phase = None if succeeded else self._generation.get("phase")
            self._generation.update(
                {
                    "state": "succeeded" if succeeded else "failed",
                    "phase": "completed" if succeeded else "failed",
                    "finished_at": self._now().isoformat(),
                    "error": None if succeeded else error,
                    "failure_code": None if succeeded else failure_code,
                    "failed_phase": failed_phase,
                    "result": result,
                }
            )

    def _run_generation(
        self,
        job_id: str,
        *,
        current: datetime,
        automatic: bool,
        trade_date: date | None = None,
    ) -> None:
        try:
            result = self._generate_once(
                current=current,
                automatic=automatic,
                trade_date=trade_date,
                phase=lambda value: self._set_generation_phase(job_id, value),
            )
        except DailyStockSelectionError as exc:
            cause = exc.cause or exc
            failed_phase = self.generation_status().get("phase")
            logger.warning(
                "daily stock selection job failed: job_id=%s trade_date=%s phase=%s "
                "failure=%s detail=%s",
                job_id,
                current.date().isoformat(),
                failed_phase,
                type(cause).__name__,
                str(cause),
                exc_info=True,
            )
            self._finish_generation(
                job_id,
                error=exc.safe_message,
                failure_code=type(cause).__name__,
            )
        except Exception as exc:  # noqa: BLE001 - preserve safe public job state
            logger.exception(
                "daily stock selection job crashed: job_id=%s trade_date=%s",
                job_id,
                current.date().isoformat(),
            )
            self._finish_generation(
                job_id,
                error="每日选股生成失败，请稍后重试。",
                failure_code=type(exc).__name__,
            )
        else:
            self._finish_generation(job_id, result=result)

    def start_generation(
        self,
        *,
        now: datetime | None = None,
        automatic: bool = False,
        trade_date: date | None = None,
    ) -> dict[str, Any]:
        """Start or reuse the sole background generation for the current date."""

        current = self._now(now)
        target = trade_date or current.date()
        trigger = "automatic" if automatic else "manual"
        with self._lock:
            existing = self.store.get_current(target)
            if existing is not None and self._strategy_results_complete(target):
                completed = self._completed_generation(
                    current=current,
                    trigger=trigger,
                    result=self._result("existing", existing),
                )
                completed["trade_date"] = target.isoformat()
                return completed
            if self._generation.get("state") == "running":
                return copy.deepcopy(self._generation)
            self._validate_target(current, target, automatic=automatic)
            self._generation_sequence += 1
            job_id = (
                f"daily-stock-selection:{target.isoformat()}:"
                f"{self._generation_sequence}"
            )
            self._generation = {
                "contract": GENERATION_CONTRACT,
                "schema_version": GENERATION_SCHEMA_VERSION,
                "job_id": job_id,
                "trade_date": target.isoformat(),
                "trigger": trigger,
                "state": "running",
                "phase": "queued",
                "started_at": current.isoformat(),
                "finished_at": None,
                "error": None,
                "failure_code": None,
                "failed_phase": None,
                "result": None,
            }
            worker = threading.Thread(
                target=self._run_generation,
                kwargs={
                    "job_id": job_id,
                    "current": current,
                    "automatic": automatic,
                    "trade_date": target,
                },
                name=f"daily-stock-selection-job-{self._generation_sequence}",
                daemon=False,
            )
            self._generation_thread = worker
            try:
                worker.start()
            except Exception as exc:
                self._generation.update(
                    {
                        "state": "failed",
                        "phase": "failed",
                        "finished_at": self._now().isoformat(),
                        "error": "每日选股后台任务启动失败。",
                        "failure_code": type(exc).__name__,
                    }
                )
                raise
            return copy.deepcopy(self._generation)

    def wait_for_generation(self, timeout: float | None = None) -> bool:
        """Wait for the current worker during tests or process shutdown."""

        with self._lock:
            worker = self._generation_thread
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()

    def _result(
        self,
        action: str,
        selection: DailyStockSelectionV1,
        *,
        strategy_results=None,
    ) -> dict[str, Any]:
        results = list(strategy_results or self.store.list_strategy_results(selection.trade_date))
        return {
            "contract": "daily_stock_selection_result.v1",
            "schema_version": 1,
            "action": action,
            "selection": selection.model_dump(mode="json"),
            "outcome": (
                outcome.model_dump(mode="json")
                if (outcome := self.store.get_outcome(selection.selection_id))
                else None
            ),
            "strategy_results": [item.model_dump(mode="json") for item in results],
            "strategy_outcomes": [
                outcome.model_dump(mode="json")
                for item in results
                if (outcome := self.store.get_strategy_outcome(item.result_id)) is not None
            ],
        }

    def strategy_history(
        self,
        *,
        trade_date: date | str | None = None,
        limit: int = 90,
    ) -> dict[str, Any]:
        legacy_dates = self.store.list_dates(limit=limit)
        strategy_dates = self.store.list_strategy_dates(limit=limit)
        date_entries: dict[str, dict[str, Any]] = {
            str(item["trade_date"]): {
                "trade_date": str(item["trade_date"]),
                "strategy_count": 0,
                "generated_at": item.get("generated_at"),
            }
            for item in legacy_dates
        }
        for item in strategy_dates:
            normalized = str(item["trade_date"])
            date_entries[normalized] = {
                "trade_date": normalized,
                "strategy_count": int(item["strategy_count"]),
                "generated_at": item.get("generated_at"),
            }
        dates = sorted(
            date_entries.values(),
            key=lambda item: item["trade_date"],
            reverse=True,
        )[: int(limit)]
        target = str(trade_date) if trade_date is not None else (
            dates[0]["trade_date"] if dates else None
        )
        results = self.store.list_strategy_results(target) if target else []
        instrument_ids = {
            candidate.instrument_id for result in results
            for candidate in result.payload.candidates
        }
        legacy = self.store.get(target) if target else None
        if legacy is not None:
            instrument_ids.update(candidate.instrument_id for candidate in legacy.candidates)
            for screen in (*legacy.pattern_screens, *legacy.limit_up_tendency_screens):
                instrument_ids.update(candidate.instrument_id for candidate in screen.candidates)
        industry_display = load_selection_industry_display(instrument_ids)
        outcomes = [
            outcome
            for result in results
            if (outcome := self.store.get_strategy_outcome(result.result_id)) is not None
        ]
        return {
            "contract": "stock_selection_strategy_archive.v1",
            "schema_version": 1,
            "trade_date": target,
            "dates": dates,
            "catalog": strategy_catalog().model_dump(mode="json"),
            "industry_display": industry_display.model_dump(mode="json"),
            "results": [item.model_dump(mode="json") for item in results],
            "outcomes": [item.model_dump(mode="json") for item in outcomes],
            "recent_outcomes": [
                item.model_dump(mode="json")
                for item in self.store.list_strategy_outcomes(limit=100)
            ],
            "schedule": {
                "manual_after": "18:00",
                "automatic_if_missing_after": "18:30",
                "timezone": "Asia/Shanghai",
            },
        }

    def history(
        self,
        *,
        trade_date: date | str | None = None,
        limit: int = 90,
    ) -> dict[str, Any]:
        dates = self.store.list_dates(limit=limit)
        target = trade_date or (dates[0]["trade_date"] if dates else None)
        selection = self.store.get(target) if target is not None else None
        outcome = self.store.get_outcome(selection.selection_id) if selection else None
        history = {
            "contract": ARCHIVE_CONTRACT,
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "trade_date": selection.trade_date.isoformat() if selection else None,
            "dates": dates,
            "selection": selection.model_dump(mode="json") if selection else None,
            "outcome": outcome.model_dump(mode="json") if outcome else None,
            "recent_outcomes": self.store.list_outcomes(limit=20),
            "schedule": {
                "manual_after": "18:00",
                "automatic_if_missing_after": "18:30",
                "timezone": "Asia/Shanghai",
            },
        }
        history["strategy_archive"] = self.strategy_history(
            trade_date=target,
            limit=limit,
        )
        return history

    def maybe_generate_automatic(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = self._now(now)
        session = a_share_session(current)
        if session.calendar_status is CalendarDayStatus.UNVERIFIED:
            return {"action": "not_due"}
        target = current.date()
        if (
            session.calendar_status is not CalendarDayStatus.VERIFIED_TRADING_DAY
            or current.time().replace(tzinfo=None) < AUTOMATIC_SELECTION_START
        ):
            target = _previous_trading_date(current.date())
            if target is None:
                return {"action": "not_due"}
        with self._lock:
            existing = self.store.get_current(target)
            if existing is not None and self._strategy_results_complete(target):
                return self._result("existing", existing)
            if self._generation.get("state") == "running":
                return copy.deepcopy(self._generation)
            if self._auto_date != target:
                self._auto_date = target
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
        return self.start_generation(now=current, automatic=True, trade_date=target)


__all__ = [
    "AUTOMATIC_MAX_ATTEMPTS",
    "AUTOMATIC_RETRY_SECONDS",
    "AUTOMATIC_SELECTION_START",
    "GENERATION_CONTRACT",
    "GENERATION_SCHEMA_VERSION",
    "DailyStockSelectionError",
    "DailyStockSelectionService",
    "MANUAL_SELECTION_START",
    "SelectionCalendarError",
    "SelectionDataUnavailableError",
    "SelectionTooEarlyError",
    "evaluate_next_session",
]
