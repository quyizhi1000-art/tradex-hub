from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from astock_signals.smart_router import SmartRouter

from tradex.data_gateway.contracts import (
    ContractMetadata,
    DailyLimitUpMembershipV1,
    QualityStatus,
)
from tradex.data_gateway.limit_events import fetch_daily_limit_up_membership
from tradex.data_sources import tushare_fetchers
from tradex.market_watch.limit_up_streak import LimitUpStreakResolver


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 31, 10, 30, tzinfo=SHANGHAI)


def _membership(trade_date: date, *instrument_ids: str) -> DailyLimitUpMembershipV1:
    return DailyLimitUpMembershipV1(
        metadata=ContractMetadata(
            contract="daily_limit_up_membership.v1",
            provider="fixture",
            fetched_at=NOW,
            quality=QualityStatus.DEGRADED,
            quality_flags=("provider_timestamp_missing",),
        ),
        trading_date=trade_date,
        instrument_ids=tuple(sorted(instrument_ids)),
    )


def test_daily_limit_up_membership_maps_exact_date_and_unique_instruments() -> None:
    router = SmartRouter()
    router.register(
        "limit_up_daily_membership",
        "tushare",
        lambda trade_date: {
            "trade_date": "2026-08-28",
            "members": [
                {"ts_code": "600000.SH", "trade_date": "20260828"},
                {"ts_code": "002418.SZ", "trade_date": "20260828"},
            ],
            "request_id": "request-1",
            "provider_as_of": None,
            "source_valid": True,
        },
        priority=1,
    )

    result = fetch_daily_limit_up_membership(
        "2026-08-28",
        router=router,
        now=NOW,
    )

    assert result.metadata.contract == "daily_limit_up_membership.v1"
    assert result.metadata.provider == "tushare"
    assert result.metadata.provider_request_id == "request-1"
    assert result.metadata.quality is QualityStatus.DEGRADED
    assert result.instrument_ids == ("002418.SZ", "600000.SH")


def test_tushare_daily_membership_uses_one_bounded_post_close_pool(monkeypatch) -> None:
    observed = {}

    def paged_records(api_name, params, fields, **kwargs):
        observed.update(
            api_name=api_name,
            params=dict(params),
            fields=tuple(fields),
            kwargs=dict(kwargs),
        )
        return (
            [{"ts_code": "002418.SZ", "trade_date": "20260828"}],
            [("limit_list_ths:0", "request-1")],
        )

    monkeypatch.setattr(tushare_fetchers, "_paged_records", paged_records)

    result = tushare_fetchers.fetch_limit_up_daily_membership("20260828")

    assert observed == {
        "api_name": "limit_list_ths",
        "params": {"trade_date": "20260828", "limit_type": "涨停池"},
        "fields": ("ts_code", "trade_date"),
        "kwargs": {
            "context": "连板历史:日级收盘涨停池",
            "allow_empty": True,
            "page_size": 4000,
            "max_pages": 2,
        },
    }
    assert result["trade_date"] == "2026-08-28"
    assert result["members"] == [
        {"ts_code": "002418.SZ", "trade_date": "20260828"}
    ]
    assert result["source_valid"] is True


@pytest.mark.parametrize(
    "members",
    [
        [{"ts_code": "002418.SZ", "trade_date": "20260827"}],
        [
            {"ts_code": "002418.SZ", "trade_date": "20260828"},
            {"ts_code": "002418.SZ", "trade_date": "20260828"},
        ],
    ],
)
def test_daily_limit_up_membership_rejects_wrong_date_or_duplicates(members) -> None:
    router = SmartRouter()
    router.register(
        "limit_up_daily_membership",
        "tushare",
        lambda trade_date: {
            "trade_date": "2026-08-28",
            "members": members,
            "request_id": None,
            "provider_as_of": None,
            "source_valid": True,
        },
        priority=1,
    )

    with pytest.raises(RuntimeError, match="All sources"):
        fetch_daily_limit_up_membership("2026-08-28", router=router, now=NOW)


def test_streak_resolver_counts_only_consecutive_closed_limit_up_days() -> None:
    previous_dates = {
        date(2026, 8, 31): date(2026, 8, 28),
        date(2026, 8, 28): date(2026, 8, 27),
        date(2026, 8, 27): date(2026, 8, 26),
        date(2026, 8, 26): date(2026, 8, 25),
        date(2026, 8, 25): date(2026, 8, 24),
    }
    memberships = {
        date(2026, 8, 28): _membership(date(2026, 8, 28), "600000.SH"),
        date(2026, 8, 27): _membership(
            date(2026, 8, 27), "002418.SZ", "600000.SH"
        ),
        date(2026, 8, 26): _membership(
            date(2026, 8, 26), "002418.SZ", "600000.SH"
        ),
        date(2026, 8, 25): _membership(
            date(2026, 8, 25), "002418.SZ", "600000.SH"
        ),
        date(2026, 8, 24): _membership(date(2026, 8, 24)),
    }
    calls: list[date] = []

    def fetcher(value: str, *, now: datetime):
        requested = date.fromisoformat(value)
        calls.append(requested)
        return memberships[requested]

    resolver = LimitUpStreakResolver(
        membership_fetcher=fetcher,
        previous_trading_date=lambda value: previous_dates[value],
    )

    result = resolver.resolve(
        trade_date=date(2026, 8, 31),
        instrument_ids=("002418.SZ", "600000.SH"),
        now=NOW,
    )

    assert result.board_counts == {"002418.SZ": 1, "600000.SH": 5}
    assert result.quality_flags == ()
    assert calls == [
        date(2026, 8, 28),
        date(2026, 8, 27),
        date(2026, 8, 26),
        date(2026, 8, 25),
        date(2026, 8, 24),
    ]

    resolver.resolve(
        trade_date=date(2026, 8, 31),
        instrument_ids=("002418.SZ", "600000.SH"),
        now=NOW,
    )
    assert calls == [
        date(2026, 8, 28),
        date(2026, 8, 27),
        date(2026, 8, 26),
        date(2026, 8, 25),
        date(2026, 8, 24),
    ]


def test_streak_resolver_fails_closed_only_for_still_unresolved_instruments() -> None:
    def fetcher(value: str, *, now: datetime):
        if value == "2026-08-28":
            return _membership(date(2026, 8, 28), "600000.SH")
        raise RuntimeError("history unavailable")

    previous_dates = {
        date(2026, 8, 31): date(2026, 8, 28),
        date(2026, 8, 28): date(2026, 8, 27),
    }
    resolver = LimitUpStreakResolver(
        membership_fetcher=fetcher,
        previous_trading_date=lambda value: previous_dates[value],
    )

    result = resolver.resolve(
        trade_date=date(2026, 8, 31),
        instrument_ids=("002418.SZ", "600000.SH"),
        now=NOW,
    )

    assert result.board_counts == {"002418.SZ": 1, "600000.SH": None}
    assert result.quality_flags == ("daily_limit_up_history_unavailable",)
