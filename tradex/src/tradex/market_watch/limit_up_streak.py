"""Verified continuous limit-up streaks from immutable daily memberships."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import MappingProxyType

from tradex.data_gateway.contracts import DailyLimitUpMembershipV1
from tradex.market_calendar import CalendarDayStatus, calendar_day_status


MembershipFetcher = Callable[..., DailyLimitUpMembershipV1]
PreviousTradingDate = Callable[[date], date]


def _previous_verified_trading_date(value: date) -> date:
    candidate = value - timedelta(days=1)
    for _ in range(15):
        status = calendar_day_status(candidate)
        if status is CalendarDayStatus.VERIFIED_TRADING_DAY:
            return candidate
        if status is CalendarDayStatus.UNVERIFIED:
            raise RuntimeError("previous trading day is outside the verified calendar")
        candidate -= timedelta(days=1)
    raise RuntimeError("previous trading day is outside the bounded calendar lookup")


@dataclass(frozen=True, slots=True)
class LimitUpStreakResolution:
    board_counts: Mapping[str, int | None]
    quality_flags: tuple[str, ...] = ()


class LimitUpStreakResolver:
    """Resolve live streaks from prior post-close limit-up memberships.

    The current live pool contributes one board. Historical membership sets are
    immutable after close and cached by trading date for the process lifetime.
    """

    def __init__(
        self,
        *,
        membership_fetcher: MembershipFetcher | None = None,
        previous_trading_date: PreviousTradingDate | None = None,
        max_lookback_sessions: int = 20,
    ) -> None:
        if max_lookback_sessions < 1:
            raise ValueError("max_lookback_sessions must be positive")
        if membership_fetcher is None:
            from tradex.data_gateway.limit_events import (
                fetch_daily_limit_up_membership,
            )

            membership_fetcher = fetch_daily_limit_up_membership
        self._membership_fetcher = membership_fetcher
        self._previous_trading_date = (
            previous_trading_date or _previous_verified_trading_date
        )
        self._max_lookback_sessions = max_lookback_sessions
        self._cache: dict[date, frozenset[str]] = {}
        self._lock = threading.RLock()

    def _membership(self, trade_date: date, *, now: datetime) -> frozenset[str]:
        cached = self._cache.get(trade_date)
        if cached is not None:
            return cached
        canonical = self._membership_fetcher(trade_date.isoformat(), now=now)
        if canonical.trading_date != trade_date:
            raise RuntimeError("daily limit-up membership returned the wrong date")
        result = frozenset(canonical.instrument_ids)
        self._cache[trade_date] = result
        return result

    def resolve(
        self,
        *,
        trade_date: date,
        instrument_ids: tuple[str, ...],
        now: datetime,
    ) -> LimitUpStreakResolution:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("streak resolution time must include a timezone")
        if len(instrument_ids) != len(set(instrument_ids)):
            raise ValueError("live limit-up pool cannot contain duplicate instruments")

        counts: dict[str, int | None] = {
            instrument_id: 1 for instrument_id in instrument_ids
        }
        unresolved = set(instrument_ids)
        flags: list[str] = []
        with self._lock:
            candidate = trade_date
            for _ in range(self._max_lookback_sessions):
                if not unresolved:
                    break
                try:
                    candidate = self._previous_trading_date(candidate)
                    membership = self._membership(candidate, now=now)
                except Exception:
                    for instrument_id in unresolved:
                        counts[instrument_id] = None
                    flags.append("daily_limit_up_history_unavailable")
                    unresolved.clear()
                    break
                continuing = unresolved.intersection(membership)
                for instrument_id in continuing:
                    current = counts[instrument_id]
                    assert current is not None
                    counts[instrument_id] = current + 1
                unresolved = continuing
            if unresolved:
                for instrument_id in unresolved:
                    counts[instrument_id] = None
                flags.append("daily_limit_up_history_lookback_exhausted")

        return LimitUpStreakResolution(
            board_counts=MappingProxyType(dict(sorted(counts.items()))),
            quality_flags=tuple(dict.fromkeys(flags)),
        )


__all__ = ["LimitUpStreakResolution", "LimitUpStreakResolver"]
