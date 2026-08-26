"""Single owner for refreshing, caching and alerting ``market_watch.v1``.

Cold starts and explicit refreshes are synchronous because the current desktop
dashboard uses request threads.  Normal reads may serve the last snapshot while
the same owner refreshes in the background.  A condition variable supplies
single-flight behavior so concurrent HTTP requests never fan out into duplicate
provider work.
"""

from __future__ import annotations

import copy
import time
from dataclasses import is_dataclass, replace
from datetime import date, datetime, time as datetime_time, timedelta
from enum import Enum
from threading import Condition, RLock, Thread
from typing import Any, Callable, Generic, Mapping, TypeVar
from uuid import uuid4
from zoneinfo import ZoneInfo

from tradex.market_calendar import (
    CalendarDayStatus,
    calendar_day_status,
    is_mainland_a_share_open,
)

from .alerts import MarketAlertEngine
from .policy import DEFAULT_MARKET_WATCH_POLICY


RawSnapshot = TypeVar("RawSnapshot")
Snapshot = TypeVar("Snapshot")


class MarketWatchUnavailableError(RuntimeError):
    """Raised when the first refresh fails and no stale snapshot can be served."""

    def __init__(self, message: str, *, cause: Exception, alerts: tuple[Any, ...]) -> None:
        super().__init__(message)
        self.cause = cause
        self.alerts = alerts


class _UnavailableSnapshot:
    """Minimal duck-typed input used to create the initial data-risk alert."""

    class _Freshness:
        status = "unavailable"

    freshness = _Freshness()
    alerts: tuple[Any, ...] = ()


class MarketWatchService(Generic[RawSnapshot, Snapshot]):
    """Build and cache the canonical market-watch snapshot.

    Args:
        fetcher: Provider-neutral callable returning the raw aggregate payload.
        builder: Pure callable mapping that payload to ``MarketWatchSnapshotV1``.
        trading_ttl_seconds: Cache lifetime while the exchange is open.
        off_session_ttl_seconds: Cache lifetime outside the trading session.
        force_min_interval_seconds: Minimum upstream interval even for forced
            refreshes.  This protects paid providers from button double-clicks.
        failure_retry_seconds: Brief stale-response interval after a failed
            refresh before another upstream attempt is allowed.

    ``get(force=False)`` is the normal request entry point.  ``refresh()`` is a
    convenience alias whose default is forced refresh; both honor single-flight
    and the configured minimum interval.
    """

    def __init__(
        self,
        fetcher: Callable[[], RawSnapshot],
        builder: Callable[[RawSnapshot], Snapshot],
        *,
        initial_snapshot: Snapshot | None = None,
        alert_engine: MarketAlertEngine | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        market_open: Callable[[datetime], bool] | None = None,
        snapshot_id_factory: Callable[[int], str] | None = None,
        trading_ttl_seconds: float = (
            DEFAULT_MARKET_WATCH_POLICY.trading_refresh_interval_seconds
        ),
        off_session_ttl_seconds: float = (
            DEFAULT_MARKET_WATCH_POLICY.off_session_refresh_interval_seconds
        ),
        force_min_interval_seconds: float = (
            DEFAULT_MARKET_WATCH_POLICY.force_min_interval_seconds
        ),
        failure_retry_seconds: float = DEFAULT_MARKET_WATCH_POLICY.failure_retry_seconds,
    ) -> None:
        for name, value in (
            ("trading_ttl_seconds", trading_ttl_seconds),
            ("off_session_ttl_seconds", off_session_ttl_seconds),
            ("force_min_interval_seconds", force_min_interval_seconds),
            ("failure_retry_seconds", failure_retry_seconds),
        ):
            if value < 0:
                raise ValueError(f"{name} cannot be negative")

        self._fetcher = fetcher
        self._builder = builder
        self._clock = clock or _shanghai_now
        self._monotonic = monotonic or time.monotonic
        self._market_open = market_open or is_mainland_a_share_open
        self._snapshot_id_factory = snapshot_id_factory or _new_snapshot_id
        self.trading_ttl_seconds = float(trading_ttl_seconds)
        self.off_session_ttl_seconds = float(off_session_ttl_seconds)
        self.force_min_interval_seconds = float(force_min_interval_seconds)
        self.failure_retry_seconds = float(failure_retry_seconds)
        self._alerts = alert_engine or MarketAlertEngine(clock=self._clock)

        restored = (
            _strict_validate(initial_snapshot)
            if initial_snapshot is not None
            else None
        )
        restored_sequence = int(getattr(restored, "sequence", 0)) if restored else 0
        if restored_sequence < 0:
            raise ValueError("initial snapshot sequence cannot be negative")

        self._condition = Condition(RLock())
        self._refreshing = False
        self._snapshot: Snapshot | None = restored
        self._sequence = restored_sequence
        self._bootstrap_only = restored is not None
        self._last_refresh_mono: float | None = None
        self._last_attempt_mono: float | None = None
        self._last_error: Exception | None = None
        self._last_unavailable: MarketWatchUnavailableError | None = None

    def get(
        self,
        *,
        force: bool = False,
        stale_while_revalidate: bool = False,
    ) -> Snapshot:
        """Return a current snapshot, refreshing once when required.

        Normal desktop reads may opt into stale-while-revalidate.  When a
        previously successful snapshot exists, the caller receives it
        immediately while this service starts the one owned refresh in a
        background thread.  Cold starts and explicit forced refreshes remain
        synchronous so callers never receive an invented empty payload.
        """

        waited_for_refresh = False
        background_refresh: tuple[datetime, float, int, str] | None = None
        cached_snapshot: Snapshot | None = None
        with self._condition:
            if (
                self._refreshing
                and stale_while_revalidate
                and self._snapshot is not None
                and not self._bootstrap_only
            ):
                return self._snapshot
            while self._refreshing:
                waited_for_refresh = True
                self._condition.wait()

            now = self._require_aware_clock()
            tick = self._monotonic()

            # All waiters consume the result of the one refresh they waited on.
            # A forced waiter must not immediately start a second provider call.
            if waited_for_refresh:
                if self._snapshot is not None:
                    return self._snapshot
                if self._last_unavailable is not None:
                    raise self._last_unavailable

            if self._snapshot is not None and not force and self._cache_is_valid(
                now=now, tick=tick
            ):
                return self._snapshot

            if self._minimum_interval_active(tick=tick, force=force):
                if self._snapshot is not None:
                    return self._snapshot
                if self._last_unavailable is not None:
                    raise self._last_unavailable

            next_sequence = self._sequence + 1
            snapshot_id = self._snapshot_id_factory(next_sequence)
            self._refreshing = True
            self._last_attempt_mono = tick
            if (
                stale_while_revalidate
                and not force
                and self._snapshot is not None
                and not self._bootstrap_only
            ):
                cached_snapshot = self._snapshot
                background_refresh = (now, tick, next_sequence, snapshot_id)

        if background_refresh is not None and cached_snapshot is not None:
            now, tick, next_sequence, snapshot_id = background_refresh
            worker = Thread(
                target=self._refresh_in_background,
                kwargs={
                    "now": now,
                    "tick": tick,
                    "sequence": next_sequence,
                    "snapshot_id": snapshot_id,
                },
                name="market-watch-refresh",
                daemon=True,
            )
            try:
                worker.start()
            except Exception as error:
                return self._finish_failed_refresh(
                    error,
                    now=now,
                    tick=tick,
                    sequence=next_sequence,
                    snapshot_id=snapshot_id,
                )
            return cached_snapshot

        return self._perform_refresh(
            now=now,
            tick=tick,
            sequence=next_sequence,
            snapshot_id=snapshot_id,
        )

    def refresh(self, *, force: bool = True) -> Snapshot:
        """Explicit refresh entry point; forced calls still respect min interval."""

        return self.get(force=force)

    def _perform_refresh(
        self,
        *,
        now: datetime,
        tick: float,
        sequence: int,
        snapshot_id: str,
    ) -> Snapshot:
        try:
            raw = self._fetcher()
            prepared = _prepare_builder_input(
                raw,
                sequence=sequence,
                snapshot_id=snapshot_id,
                now=now,
            )
            candidate = self._builder(prepared)
            candidate = _copy_with(
                candidate,
                sequence=sequence,
                snapshot_id=snapshot_id,
            )
            with self._condition:
                previous = self._snapshot
            candidate = _recover_unavailable_turnover(
                candidate,
                previous=previous,
                now=now,
            )
            emitted = self._alerts.evaluate(candidate, now=now)
            completed = _strict_validate(_copy_with(candidate, alerts=emitted))
        except Exception as error:
            return self._finish_failed_refresh(
                error,
                now=now,
                tick=tick,
                sequence=sequence,
                snapshot_id=snapshot_id,
            )

        with self._condition:
            self._snapshot = completed
            self._sequence = sequence
            self._bootstrap_only = False
            completed_tick = max(tick, self._monotonic())
            self._last_refresh_mono = completed_tick
            self._last_attempt_mono = completed_tick
            self._last_error = None
            self._last_unavailable = None
            self._refreshing = False
            self._condition.notify_all()
            return completed

    def _refresh_in_background(
        self,
        *,
        now: datetime,
        tick: float,
        sequence: int,
        snapshot_id: str,
    ) -> None:
        try:
            self._perform_refresh(
                now=now,
                tick=tick,
                sequence=sequence,
                snapshot_id=snapshot_id,
            )
        except MarketWatchUnavailableError:
            # A background refresh is started only when a prior snapshot exists,
            # but keep the worker fail-closed if contract evolution invalidates
            # the fallback while the process is running.
            return

    @property
    def last_snapshot(self) -> Snapshot | None:
        with self._condition:
            return self._snapshot

    @property
    def refreshing(self) -> bool:
        with self._condition:
            return self._refreshing

    @property
    def last_error(self) -> Exception | None:
        with self._condition:
            return self._last_error

    @property
    def sequence(self) -> int:
        with self._condition:
            return self._sequence

    def set_muted(self, muted: bool) -> None:
        self._alerts.set_muted(muted)

    def _finish_failed_refresh(
        self,
        error: Exception,
        *,
        now: datetime,
        tick: float,
        sequence: int,
        snapshot_id: str,
    ) -> Snapshot:
        with self._condition:
            previous = self._snapshot

        if previous is not None:
            try:
                stale = _stale_fallback(
                    previous,
                    error=error,
                    sequence=sequence,
                    snapshot_id=snapshot_id,
                )
                emitted = self._alerts.evaluate(stale, now=now)
                stale = _strict_validate(_copy_with(stale, alerts=emitted))
            except Exception as fallback_error:
                # Contract evolution must fail closed: never serve the previous
                # strong conclusion if it cannot be converted to a valid stale
                # snapshot.
                error = fallback_error
            else:
                with self._condition:
                    self._snapshot = stale
                    self._sequence = sequence
                    self._bootstrap_only = False
                    # Keep last_refresh at its original successful time.  The stale
                    # cache is throttled by last_attempt/failure_retry instead.
                    self._last_attempt_mono = max(tick, self._monotonic())
                    self._last_error = error
                    self._last_unavailable = None
                    self._refreshing = False
                    self._condition.notify_all()
                    return stale

        unavailable_alerts = self._alerts.evaluate(_UnavailableSnapshot(), now=now)
        unavailable = MarketWatchUnavailableError(
            "market_watch.v1 is unavailable and no safe stale snapshot can be served",
            cause=error,
            alerts=unavailable_alerts,
        )
        with self._condition:
            self._last_attempt_mono = max(tick, self._monotonic())
            self._last_error = error
            self._last_unavailable = unavailable
            self._refreshing = False
            self._condition.notify_all()
        raise unavailable from error

    def _cache_is_valid(self, *, now: datetime, tick: float) -> bool:
        if self._last_refresh_mono is None or self._snapshot is None:
            return False
        # A successful refresh may honestly produce degraded or stale market
        # data (for example before the session opens).  Only an actual refresh
        # error gets the short retry window; domain freshness still follows the
        # normal trading/off-session cadence.
        if self._last_error is not None:
            return self._last_attempt_mono is not None and (
                tick - self._last_attempt_mono < self.failure_retry_seconds
            )
        ttl = (
            self.trading_ttl_seconds
            if self._market_open(now)
            else self.off_session_ttl_seconds
        )
        return tick - self._last_refresh_mono < ttl

    def _minimum_interval_active(self, *, tick: float, force: bool) -> bool:
        if self._last_attempt_mono is None:
            return False
        interval = (
            self.force_min_interval_seconds if force else self.failure_retry_seconds
        )
        return tick - self._last_attempt_mono < interval

    def _require_aware_clock(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime):
            raise TypeError("market-watch clock must return datetime")
        if now.tzinfo is None:
            raise ValueError("market-watch clock must return a timezone-aware datetime")
        return now


def _stale_fallback(
    snapshot: Any,
    *,
    error: Exception,
    sequence: int,
    snapshot_id: str,
) -> Any:
    """Create a traceable stale snapshot with conclusions forced to abstain."""

    flag = f"refresh_error:{type(error).__name__}"
    freshness = getattr(snapshot, "freshness")
    components = tuple(
        _copy_with(
            component,
            status=_typed_value(getattr(component, "status", None), "stale"),
            flags=_append_flag(getattr(component, "flags", ()), flag),
        )
        for component in (getattr(freshness, "components", ()) or ())
    )
    stale_freshness = _copy_with(
        freshness,
        status=_typed_value(getattr(freshness, "status", None), "stale"),
        components=components,
        flags=_append_flag(getattr(freshness, "flags", ()), flag),
    )

    changes: dict[str, Any] = {
        "sequence": sequence,
        "snapshot_id": snapshot_id,
        "freshness": stale_freshness,
        "alerts": (),
    }
    guardrail = getattr(snapshot, "guardrail", None)
    if guardrail is not None:
        changes["guardrail"] = _copy_with(
            guardrail,
            regime=_typed_value(getattr(guardrail, "regime", None), "uncertain"),
            severity=_typed_value(getattr(guardrail, "severity", None), "stop"),
            conclusion_strength=_typed_value(
                getattr(guardrail, "conclusion_strength", None), "abstain"
            ),
            current_state="刷新失败，当前只保留上一次盘面数据。",
            supporting_evidence=(),
            counter_evidence=("数据已陈旧，不能据此判断下一步盘面。",),
            behavioral_constraint="数据不可依赖，请暂停追单并重新核对盘面。",
        )
    if hasattr(snapshot, "scenarios") or isinstance(snapshot, Mapping):
        changes["scenarios"] = ()
    return _strict_validate(_copy_with(snapshot, **changes))


def _latest_completed_trading_date(reference_date: date) -> date | None:
    candidate = reference_date - timedelta(days=1)
    while True:
        status = calendar_day_status(candidate)
        if status is CalendarDayStatus.VERIFIED_TRADING_DAY:
            return candidate
        if status is CalendarDayStatus.UNVERIFIED:
            return None
        candidate -= timedelta(days=1)


def _is_complete_same_session_close_turnover(
    turnover: Any,
    *,
    current_date: date,
    phase: str | None,
) -> bool:
    if phase != "closed" or getattr(turnover, "today_date", None) != current_date:
        return False
    baseline_date = getattr(turnover, "previous_date", None)
    if not isinstance(baseline_date, date) or baseline_date >= current_date:
        return False
    raw_as_of = getattr(turnover, "as_of", None)
    if not isinstance(raw_as_of, str):
        return False
    try:
        close_as_of = datetime_time.fromisoformat(raw_as_of)
    except ValueError:
        return False
    if close_as_of < datetime_time(15, 0):
        return False
    return all(
        isinstance(value, (int, float)) and value > 0
        for value in (
            getattr(turnover, "today_amount_cny", None),
            getattr(turnover, "previous_same_time_amount_cny", None),
        )
    )


def _freshness_status_from_components(components: tuple[Any, ...], fallback: Any) -> Any:
    statuses = {
        getattr(getattr(item, "status", None), "value", getattr(item, "status", None))
        for item in components
    }
    value = (
        "stale"
        if "stale" in statuses
        else "unavailable"
        if statuses == {"unavailable"}
        else "degraded"
        if statuses - {"fresh"}
        else "fresh"
    )
    return _typed_value(fallback, value)


def _recover_unavailable_turnover(
    candidate: Snapshot,
    *,
    previous: Snapshot | None,
    now: datetime,
) -> Snapshot:
    """Keep one traceable last-good turnover component across refresh/restart."""

    if previous is None:
        return candidate
    current_turnover = getattr(candidate, "turnover", None)
    previous_turnover = getattr(previous, "turnover", None)
    if (
        current_turnover is None
        or previous_turnover is None
        or getattr(current_turnover, "available", None) is not False
        or getattr(previous_turnover, "available", None) is not True
    ):
        return candidate

    market_state = getattr(candidate, "market_state", None)
    current_date = getattr(market_state, "trading_date", None)
    previous_date = getattr(previous_turnover, "today_date", None)
    raw_phase = getattr(market_state, "phase", None)
    phase = getattr(raw_phase, "value", raw_phase)
    if not isinstance(current_date, date) or not isinstance(previous_date, date):
        return candidate
    same_session = previous_date == current_date
    complete_same_session_close = _is_complete_same_session_close_turnover(
        previous_turnover,
        current_date=current_date,
        phase=phase,
    )
    latest_completed = (
        phase in {"pre_open", "non_trading"}
        and previous_date == _latest_completed_trading_date(current_date)
    )
    if not (same_session or latest_completed):
        return candidate
    recovered_turnover = (
        _copy_with(previous_turnover, as_of="15:00")
        if latest_completed or (same_session and phase == "closed")
        else previous_turnover
    )

    freshness = getattr(candidate, "freshness", None)
    components = getattr(freshness, "components", ()) if freshness else ()
    recovered_components = []
    found_turnover = False
    for component in components:
        if getattr(component, "component", None) != "turnover":
            recovered_components.append(component)
            continue
        found_turnover = True
        previous_as_of = getattr(recovered_turnover, "as_of", None)
        provider_as_of = None
        if isinstance(previous_as_of, str):
            try:
                provider_as_of = datetime.combine(
                    previous_date,
                    datetime_time.fromisoformat(previous_as_of),
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                )
            except ValueError:
                provider_as_of = None
        recovered_components.append(
            _copy_with(
                component,
                status=_typed_value(
                    getattr(component, "status", None),
                    "degraded" if complete_same_session_close else "stale",
                ),
                quality=_typed_value(getattr(component, "quality", None), "degraded"),
                provider_as_of=provider_as_of,
                flags=_append_flag(
                    getattr(component, "flags", ()),
                    (
                        "same_session_closed_turnover_recovered"
                        if complete_same_session_close
                        else "last_good_turnover_recovered"
                    ),
                ),
            )
        )
    if not found_turnover:
        return candidate

    recovered_components_tuple = tuple(recovered_components)
    recovered_flag = (
        "same_session_closed_turnover_recovered"
        if complete_same_session_close
        else "last_good_turnover_recovered"
    )
    recovered_freshness = _copy_with(
        freshness,
        status=_freshness_status_from_components(
            recovered_components_tuple,
            getattr(freshness, "status", None),
        ),
        components=recovered_components_tuple,
        flags=_append_flag(
            getattr(freshness, "flags", ()),
            recovered_flag,
        ),
    )
    guardrail = getattr(candidate, "guardrail", None)
    if complete_same_session_close:
        counter_evidence = tuple(
            item
            for item in getattr(guardrail, "counter_evidence", ())
            if item not in {"turnover:unavailable", "昨日同期成交额比较不可用"}
        )
        counter_evidence = (
            *counter_evidence,
            "成交额使用同交易日完整收盘快照恢复，刷新路径已降级。",
        )
        recovered_guardrail = _copy_with(
            guardrail,
            counter_evidence=counter_evidence,
        )
        return _copy_with(
            candidate,
            turnover=recovered_turnover,
            freshness=recovered_freshness,
            guardrail=recovered_guardrail,
        )

    counter_evidence = tuple(
        "turnover:stale" if item == "turnover:unavailable" else item
        for item in getattr(guardrail, "counter_evidence", ())
    )
    if "turnover:stale" not in counter_evidence:
        counter_evidence = (*counter_evidence, "turnover:stale")
    recovered_guardrail = _copy_with(
        guardrail,
        regime=_typed_value(getattr(guardrail, "regime", None), "uncertain"),
        severity=_typed_value(getattr(guardrail, "severity", None), "stop"),
        conclusion_strength=_typed_value(
            getattr(guardrail, "conclusion_strength", None), "abstain"
        ),
        current_state=(
            "成交额同比仅保留上一份可比快照，整体数据已陈旧；"
            "现有盘面读数仅供回看，不形成新的方向判断。"
        ),
        supporting_evidence=(),
        counter_evidence=counter_evidence,
        behavioral_constraint=(
            "等待成交额与其他关键组件恢复到可判断状态，再重新评估盘面。"
        ),
    )
    return _copy_with(
        candidate,
        turnover=recovered_turnover,
        freshness=recovered_freshness,
        guardrail=recovered_guardrail,
        scenarios=(),
    )


def _prepare_builder_input(
    raw: RawSnapshot,
    *,
    sequence: int,
    snapshot_id: str,
    now: datetime,
) -> RawSnapshot:
    if not isinstance(raw, Mapping):
        return raw
    prepared = dict(raw)
    prepared["sequence"] = sequence
    prepared["snapshot_id"] = snapshot_id
    prepared.setdefault("as_of", now)
    return prepared  # type: ignore[return-value]


def _copy_with(value: Any, **changes: Any) -> Any:
    if hasattr(value, "model_copy"):
        return value.model_copy(update=changes)
    if is_dataclass(value):
        return replace(value, **changes)
    if isinstance(value, Mapping):
        return {**value, **changes}
    cloned = copy.copy(value)
    for name, replacement in changes.items():
        setattr(cloned, name, replacement)
    return cloned


def _strict_validate(value: Snapshot) -> Snapshot:
    validator = getattr(type(value), "model_validate", None)
    dumper = getattr(value, "model_dump", None)
    if callable(validator) and callable(dumper):
        return validator(dumper(mode="python"))
    return value


def _append_flag(flags: Any, flag: str) -> tuple[str, ...]:
    normalized = tuple(str(item) for item in (flags or ()))
    return normalized if flag in normalized else (*normalized, flag)


def _typed_value(existing: Any, value: str) -> Any:
    """Preserve enum fields during unchecked Pydantic ``model_copy`` updates."""

    return type(existing)(value) if isinstance(existing, Enum) else value


def _new_snapshot_id(sequence: int) -> str:
    return f"mw-{sequence:08d}-{uuid4().hex}"


def _shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


__all__ = [
    "MarketWatchService",
    "MarketWatchUnavailableError",
]
