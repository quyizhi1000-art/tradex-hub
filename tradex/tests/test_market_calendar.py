"""Offline acceptance tests for the shared mainland A-share calendar."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import tradex.market_watch.service as service_module
from tradex.data_gateway.market import market_state
from tradex.market_calendar import (
    CALENDAR_COVERAGE_END,
    CALENDAR_COVERAGE_START,
    CALENDAR_SOURCES,
    SESSION_RULE_SOURCE,
    CalendarDayStatus,
    TradingSessionPhase,
    a_share_session,
    calendar_day_status,
    is_mainland_a_share_open,
)
from tradex.market_watch.analysis import _infer_phase
from tradex.market_watch.contracts import MarketPhase
from tradex.market_watch.service import MarketWatchService


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _at(year: int, month: int, day: int, hour: int, minute: int, second: int = 0):
    return datetime(year, month, day, hour, minute, second, tzinfo=SHANGHAI)


def test_calendar_bundle_is_versioned_and_has_authoritative_exchange_sources() -> None:
    assert CALENDAR_COVERAGE_START == date(2024, 1, 1)
    assert CALENDAR_COVERAGE_END == date(2026, 12, 31)
    assert {(item.exchange, item.year) for item in CALENDAR_SOURCES} >= {
        ("SSE", 2024),
        ("SSE", 2025),
        ("SZSE", 2025),
        ("BSE", 2025),
        ("SSE", 2026),
        ("SZSE", 2026),
        ("BSE", 2026),
    }
    assert all(item.url.startswith("https://") for item in CALENDAR_SOURCES)
    assert SESSION_RULE_SOURCE.startswith("https://www.sse.com.cn/")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2024, 2, 9), CalendarDayStatus.VERIFIED_HOLIDAY),
        (date(2025, 6, 2), CalendarDayStatus.VERIFIED_HOLIDAY),
        (date(2026, 2, 16), CalendarDayStatus.VERIFIED_HOLIDAY),
        # Government make-up working days remain exchange weekends.
        (date(2026, 2, 28), CalendarDayStatus.VERIFIED_WEEKEND),
        (date(2026, 8, 24), CalendarDayStatus.VERIFIED_TRADING_DAY),
        (date(2027, 1, 4), CalendarDayStatus.UNVERIFIED),
    ],
)
def test_calendar_day_status_never_treats_holidays_or_future_dates_as_open(
    value: date,
    expected: CalendarDayStatus,
) -> None:
    assert calendar_day_status(value) is expected


@pytest.mark.parametrize(
    ("stamp", "phase", "is_open", "label"),
    [
        (_at(2026, 8, 24, 9, 14), TradingSessionPhase.PRE_OPEN, False, "等待开盘"),
        (_at(2026, 8, 24, 9, 15), TradingSessionPhase.PRE_OPEN, False, "集合竞价"),
        (_at(2026, 8, 24, 9, 25), TradingSessionPhase.PRE_OPEN, False, "集合竞价"),
        (_at(2026, 8, 24, 9, 25, 1), TradingSessionPhase.PRE_OPEN, False, "开盘准备"),
        (
            _at(2026, 8, 24, 9, 30),
            TradingSessionPhase.OPENING_OBSERVATION,
            True,
            "开盘观察",
        ),
        (_at(2026, 8, 24, 9, 45), TradingSessionPhase.TRADING, True, "盘中交易"),
        (_at(2026, 8, 24, 11, 30), TradingSessionPhase.TRADING, True, "盘中交易"),
        (
            _at(2026, 8, 24, 11, 30, 1),
            TradingSessionPhase.MIDDAY_BREAK,
            False,
            "午间休市",
        ),
        (_at(2026, 8, 24, 13, 0), TradingSessionPhase.TRADING, True, "盘中交易"),
        (
            _at(2026, 8, 24, 14, 57),
            TradingSessionPhase.TRADING,
            True,
            "收盘集合竞价",
        ),
        (
            _at(2026, 8, 24, 15, 0),
            TradingSessionPhase.TRADING,
            True,
            "收盘集合竞价",
        ),
        (_at(2026, 8, 24, 15, 0, 1), TradingSessionPhase.CLOSED, False, "今日收盘"),
    ],
)
def test_verified_trading_day_session_boundaries(
    stamp: datetime,
    phase: TradingSessionPhase,
    is_open: bool,
    label: str,
) -> None:
    session = a_share_session(stamp)
    assert session.phase is phase
    assert session.is_open is is_open
    assert session.is_trading_day is True
    assert session.label == label
    assert session.contract == "a_share_trading_session.v1"


def test_session_normalizes_input_to_shanghai_timezone() -> None:
    session = a_share_session(datetime(2026, 8, 24, 2, 30, tzinfo=timezone.utc))
    assert session.as_of.isoformat() == "2026-08-24T10:30:00+08:00"
    assert session.phase is TradingSessionPhase.TRADING


def test_holiday_and_unverified_dates_fail_closed_with_explicit_reason() -> None:
    holiday = a_share_session(_at(2026, 2, 16, 10, 30))
    assert holiday.phase is TradingSessionPhase.NON_TRADING
    assert holiday.is_open is False
    assert holiday.is_trading_day is False
    assert holiday.label == "节假日休市"

    future = a_share_session(_at(2027, 1, 4, 10, 30))
    assert future.phase is TradingSessionPhase.UNKNOWN
    assert future.is_open is False
    assert future.is_trading_day is False
    assert future.label == "交易日历待核验"


def test_naive_datetimes_are_rejected_instead_of_assuming_a_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        a_share_session(datetime(2026, 8, 24, 10, 30))


def test_gateway_and_market_watch_share_the_same_holiday_decision() -> None:
    holiday = _at(2026, 2, 16, 10, 30)

    assert market_state(holiday).model_dump() == {
        "label": "节假日休市",
        "is_open": False,
    }
    assert _infer_phase(holiday, True, "trading") is MarketPhase.NON_TRADING
    assert is_mainland_a_share_open(holiday) is False


def test_market_watch_phase_fails_closed_on_unverified_or_conflicting_state() -> None:
    future = _at(2027, 1, 4, 10, 30)
    open_day = _at(2026, 8, 24, 10, 30)

    assert _infer_phase(future, True, "trading") is MarketPhase.UNKNOWN
    assert _infer_phase(open_day, False, "closed") is MarketPhase.UNKNOWN


def test_market_watch_service_uses_the_shared_calendar_by_default(monkeypatch) -> None:
    def sentinel(_now):
        return False

    monkeypatch.setattr(service_module, "is_mainland_a_share_open", sentinel)

    service = MarketWatchService(lambda: {}, lambda value: value)

    assert service._market_open is sentinel
