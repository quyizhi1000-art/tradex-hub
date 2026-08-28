"""Phase-aware observation schedule for ``market_watch.v1``.

Market-watch starts with the verified opening-auction result at 09:25, samples
continuous trading through 14:56, and requires one verified final-close
observation at 15:00 instead of treating the closing auction as three minutes.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tradex.market_calendar import CalendarDayStatus, calendar_day_status


SHANGHAI = ZoneInfo("Asia/Shanghai")
OPENING_AUCTION_RESULT_TIME = time(9, 25)
EXPECTED_MARKET_WATCH_MINUTES = 239
FINAL_CLOSE_TIME = time(15, 0)


def expected_market_watch_minutes(trade_date: date) -> tuple[datetime, ...]:
    """Return auction-result, continuous, and final-close observations."""

    if not isinstance(trade_date, date) or isinstance(trade_date, datetime):
        raise TypeError("trade_date must be a date")
    status = calendar_day_status(trade_date)
    if status is CalendarDayStatus.UNVERIFIED:
        raise ValueError("trade_date is outside the verified A-share calendar")
    if status is not CalendarDayStatus.VERIFIED_TRADING_DAY:
        return ()
    auction_complete = datetime.combine(
        trade_date,
        OPENING_AUCTION_RESULT_TIME,
        SHANGHAI,
    )
    morning = datetime.combine(trade_date, time(9, 30), SHANGHAI)
    afternoon = datetime.combine(trade_date, time(13, 0), SHANGHAI)
    close = datetime.combine(trade_date, FINAL_CLOSE_TIME, SHANGHAI)
    minutes = (
        (auction_complete,)
        + tuple(morning + timedelta(minutes=offset) for offset in range(120))
        + tuple(afternoon + timedelta(minutes=offset) for offset in range(117))
        + (close,)
    )
    if len(minutes) != EXPECTED_MARKET_WATCH_MINUTES:
        raise RuntimeError("market-watch session schedule is inconsistent")
    return minutes


def is_expected_market_watch_minute(value: datetime) -> bool:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("market-watch minute must include a timezone")
    local = value.astimezone(SHANGHAI).replace(second=0, microsecond=0)
    return local in expected_market_watch_minutes(local.date())


def is_continuous_market_watch_minute(value: datetime) -> bool:
    """Return whether a stale heartbeat may represent this live observation."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("market-watch minute must include a timezone")
    local = value.astimezone(SHANGHAI)
    clock = local.time().replace(tzinfo=None)
    return (
        clock == OPENING_AUCTION_RESULT_TIME
        or time(9, 30) <= clock < time(11, 30)
        or time(13, 0) <= clock < time(14, 57)
    )


__all__ = [
    "EXPECTED_MARKET_WATCH_MINUTES",
    "FINAL_CLOSE_TIME",
    "OPENING_AUCTION_RESULT_TIME",
    "expected_market_watch_minutes",
    "is_continuous_market_watch_minute",
    "is_expected_market_watch_minute",
]
