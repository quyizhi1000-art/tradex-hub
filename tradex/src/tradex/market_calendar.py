"""Versioned, offline A-share trading calendar and session classification.

The dashboard must not infer that a weekday is a trading day.  This module is
the provider-neutral source of truth shared by the gateway and market-watch
runtime.  Calendar rows are bundled so normal requests and tests never call a
live or paid provider.

The holiday ranges below were verified against the annual Shanghai Stock
Exchange notices for 2024--2026 and cross-checked against the Shenzhen and
Beijing exchange notices for 2025--2026.  The published sources are retained
as metadata so a future calendar refresh is an explicit, reviewable data
change rather than an implicit weekday fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from itertools import chain
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
CALENDAR_VERSION = "mainland_a_share_2024_2026.v1"
CALENDAR_DATA_AS_OF = date(2026, 8, 24)
CALENDAR_COVERAGE_START = date(2024, 1, 1)
CALENDAR_COVERAGE_END = date(2026, 12, 31)
SESSION_RULE_VERSION = "sse_trading_rules_2026.v1"
SESSION_RULE_SOURCE = (
    "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/"
    "c_20260424_10816482.shtml"
)


@dataclass(frozen=True, slots=True)
class CalendarSource:
    """One authoritative annual calendar notice."""

    exchange: str
    year: int
    notice: str
    url: str


CALENDAR_SOURCES = (
    CalendarSource(
        exchange="SSE",
        year=2024,
        notice="上证公告〔2023〕47号",
        url=(
            "https://www.sse.com.cn/disclosure/dealinstruc/closed/c/"
            "c_20231226_5733941.shtml"
        ),
    ),
    CalendarSource(
        exchange="SSE",
        year=2025,
        notice="上证公告〔2024〕38号",
        url=(
            "https://www.sse.com.cn/disclosure/announcement/general/c/"
            "c_20241223_10767108.shtml"
        ),
    ),
    CalendarSource(
        exchange="SZSE",
        year=2025,
        notice="深证会〔2024〕413号",
        url=(
            "https://www.szse.cn/www/disclosure/notice/general/"
            "t20241223_611283.html"
        ),
    ),
    CalendarSource(
        exchange="BSE",
        year=2025,
        notice="北证公告〔2024〕233号",
        url="https://www.bse.cn/important_news/200024437.html",
    ),
    CalendarSource(
        exchange="SSE",
        year=2026,
        notice="上证公告〔2025〕45号",
        url=(
            "https://www.sse.com.cn/disclosure/announcement/general/c/"
            "c_20251222_10802507.shtml"
        ),
    ),
    CalendarSource(
        exchange="SZSE",
        year=2026,
        notice="深证会〔2025〕481号",
        url="https://www.szse.cn/disclosure/notice/t20251222_618087.html",
    ),
    CalendarSource(
        exchange="BSE",
        year=2026,
        notice="北证公告〔2025〕58号",
        url="https://www.bse.cn/important_news/200027428.html",
    ),
)


class TradingSessionPhase(str, Enum):
    PRE_OPEN = "pre_open"
    OPENING_OBSERVATION = "opening_observation"
    TRADING = "trading"
    MIDDAY_BREAK = "midday_break"
    CLOSED = "closed"
    NON_TRADING = "non_trading"
    UNKNOWN = "unknown"


class CalendarDayStatus(str, Enum):
    VERIFIED_TRADING_DAY = "verified_trading_day"
    VERIFIED_HOLIDAY = "verified_holiday"
    VERIFIED_WEEKEND = "verified_weekend"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class AShareSessionV1:
    """Strict immutable session decision for one Shanghai-local instant."""

    phase: TradingSessionPhase
    is_open: bool
    is_trading_day: bool
    trading_date: date
    calendar_status: CalendarDayStatus
    label: str
    as_of: datetime
    contract: str = field(default="a_share_trading_session.v1", init=False)
    schema_version: int = field(default=1, init=False)
    calendar_version: str = field(default=CALENDAR_VERSION, init=False)

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("session as_of must include a timezone")
        expected_open = self.phase in {
            TradingSessionPhase.OPENING_OBSERVATION,
            TradingSessionPhase.TRADING,
        }
        if self.is_open != expected_open:
            raise ValueError("session is_open does not match phase")
        if not self.is_trading_day and self.phase not in {
            TradingSessionPhase.NON_TRADING,
            TradingSessionPhase.UNKNOWN,
        }:
            raise ValueError("a closed calendar day cannot have a trading phase")
        if (
            self.calendar_status is CalendarDayStatus.UNVERIFIED
            and self.phase is not TradingSessionPhase.UNKNOWN
        ):
            raise ValueError("an unverified calendar day must use the unknown phase")


def _date_range(start: date, end: date) -> tuple[date, ...]:
    if end < start:
        raise ValueError("calendar range end cannot precede start")
    return tuple(
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
    )


_EXCHANGE_HOLIDAY_RANGES = (
    # SSE annual calendar for 2024.
    (date(2024, 1, 1), date(2024, 1, 1)),
    (date(2024, 2, 9), date(2024, 2, 17)),
    (date(2024, 4, 4), date(2024, 4, 6)),
    (date(2024, 5, 1), date(2024, 5, 5)),
    (date(2024, 6, 10), date(2024, 6, 10)),
    (date(2024, 9, 15), date(2024, 9, 17)),
    (date(2024, 10, 1), date(2024, 10, 7)),
    # SSE/SZSE/BSE annual calendars for 2025.
    (date(2025, 1, 1), date(2025, 1, 1)),
    (date(2025, 1, 28), date(2025, 2, 4)),
    (date(2025, 4, 4), date(2025, 4, 6)),
    (date(2025, 5, 1), date(2025, 5, 5)),
    (date(2025, 5, 31), date(2025, 6, 2)),
    (date(2025, 10, 1), date(2025, 10, 8)),
    # SSE/SZSE/BSE annual calendars for 2026.
    (date(2026, 1, 1), date(2026, 1, 3)),
    (date(2026, 2, 15), date(2026, 2, 23)),
    (date(2026, 4, 4), date(2026, 4, 6)),
    (date(2026, 5, 1), date(2026, 5, 5)),
    (date(2026, 6, 19), date(2026, 6, 21)),
    (date(2026, 9, 25), date(2026, 9, 27)),
    (date(2026, 10, 1), date(2026, 10, 7)),
)
EXCHANGE_HOLIDAYS = frozenset(
    chain.from_iterable(
        _date_range(start, end) for start, end in _EXCHANGE_HOLIDAY_RANGES
    )
)


_OPENING_AUCTION_START = time(9, 15)
_OPENING_AUCTION_END = time(9, 25)
_MORNING_OPEN = time(9, 30)
_OPENING_OBSERVATION_END = time(9, 45)
_MORNING_CLOSE = time(11, 30)
_AFTERNOON_OPEN = time(13, 0)
_CLOSING_AUCTION_START = time(14, 57)
_MARKET_CLOSE = time(15, 0)


def calendar_day_status(value: date) -> CalendarDayStatus:
    """Return verified calendar coverage without guessing outside the bundle."""

    if not CALENDAR_COVERAGE_START <= value <= CALENDAR_COVERAGE_END:
        return CalendarDayStatus.UNVERIFIED
    if value.weekday() >= 5:
        return CalendarDayStatus.VERIFIED_WEEKEND
    if value in EXCHANGE_HOLIDAYS:
        return CalendarDayStatus.VERIFIED_HOLIDAY
    return CalendarDayStatus.VERIFIED_TRADING_DAY


def a_share_session(now: datetime) -> AShareSessionV1:
    """Classify one instant using only verified, bundled exchange schedules.

    Dates outside the verified coverage fail closed as ``unknown``.  Callers
    therefore cannot silently treat an unannounced future weekday as open.
    """

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("A-share session time must include a timezone")
    local = now.astimezone(SHANGHAI)
    trading_date = local.date()
    status = calendar_day_status(trading_date)

    if status is CalendarDayStatus.UNVERIFIED:
        return _session(
            local,
            TradingSessionPhase.UNKNOWN,
            status,
            is_trading_day=False,
            label="交易日历待核验",
        )
    if status is CalendarDayStatus.VERIFIED_WEEKEND:
        return _session(
            local,
            TradingSessionPhase.NON_TRADING,
            status,
            is_trading_day=False,
            label="周末休市",
        )
    if status is CalendarDayStatus.VERIFIED_HOLIDAY:
        return _session(
            local,
            TradingSessionPhase.NON_TRADING,
            status,
            is_trading_day=False,
            label="节假日休市",
        )

    current = local.time().replace(tzinfo=None)
    if current < _OPENING_AUCTION_START:
        return _session(local, TradingSessionPhase.PRE_OPEN, status, label="等待开盘")
    if current <= _OPENING_AUCTION_END:
        return _session(local, TradingSessionPhase.PRE_OPEN, status, label="集合竞价")
    if current < _MORNING_OPEN:
        return _session(local, TradingSessionPhase.PRE_OPEN, status, label="开盘准备")
    if current < _OPENING_OBSERVATION_END:
        return _session(
            local,
            TradingSessionPhase.OPENING_OBSERVATION,
            status,
            label="开盘观察",
        )
    if current <= _MORNING_CLOSE:
        return _session(local, TradingSessionPhase.TRADING, status, label="盘中交易")
    if current < _AFTERNOON_OPEN:
        return _session(
            local,
            TradingSessionPhase.MIDDAY_BREAK,
            status,
            label="午间休市",
        )
    if current < _CLOSING_AUCTION_START:
        return _session(local, TradingSessionPhase.TRADING, status, label="盘中交易")
    if current <= _MARKET_CLOSE:
        return _session(
            local,
            TradingSessionPhase.TRADING,
            status,
            label="收盘集合竞价",
        )
    return _session(local, TradingSessionPhase.CLOSED, status, label="今日收盘")


def _session(
    local: datetime,
    phase: TradingSessionPhase,
    status: CalendarDayStatus,
    *,
    is_trading_day: bool = True,
    label: str,
) -> AShareSessionV1:
    return AShareSessionV1(
        phase=phase,
        is_open=phase
        in {
            TradingSessionPhase.OPENING_OBSERVATION,
            TradingSessionPhase.TRADING,
        },
        is_trading_day=is_trading_day,
        trading_date=local.date(),
        calendar_status=status,
        label=label,
        as_of=local,
    )


def is_mainland_a_share_open(now: datetime) -> bool:
    """Return whether the verified mainland A-share session is open now."""

    return a_share_session(now).is_open


__all__ = [
    "AShareSessionV1",
    "CALENDAR_COVERAGE_END",
    "CALENDAR_COVERAGE_START",
    "CALENDAR_DATA_AS_OF",
    "CALENDAR_SOURCES",
    "CALENDAR_VERSION",
    "CalendarDayStatus",
    "CalendarSource",
    "EXCHANGE_HOLIDAYS",
    "SHANGHAI",
    "SESSION_RULE_SOURCE",
    "SESSION_RULE_VERSION",
    "TradingSessionPhase",
    "a_share_session",
    "calendar_day_status",
    "is_mainland_a_share_open",
]
