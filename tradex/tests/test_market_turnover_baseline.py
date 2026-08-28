"""Regression coverage for the turnover baseline cache."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event, Lock
import time as time_module
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from astock_signals.smart_router import SmartRouter
from tradex.data_gateway.market import (
    TurnoverBaselineCache,
    TurnoverBaselineUnavailable,
    _requires_live_turnover,
    fetch_market_overview,
)
from tradex.data_gateway.providers.market_turnover import (
    map_index_daily_amount,
    map_index_intraday_amount,
)


_CHINA = ZoneInfo("Asia/Shanghai")


def _overview(
    trading_date: str,
    *,
    minute: str = "10:30",
    sh_minute: str | None = None,
    sz_minute: str | None = None,
    sh_amount: float = 100.0,
    sz_amount: float | None = 50.0,
) -> pd.DataFrame:
    sh_provider_as_of = f"{trading_date}T{sh_minute or minute}:00+08:00"
    sz_provider_as_of = f"{trading_date}T{sz_minute or minute}:00+08:00"
    return pd.DataFrame(
        [
            {
                "代码": "sh000001",
                "名称": "上证指数",
                "最新价": 4000.0,
                "涨跌幅": 0.2,
                "成交额": sh_amount,
                "更新时间": sh_provider_as_of,
            },
            {
                "代码": "sz399001",
                "名称": "深证成指",
                "最新价": 14000.0,
                "涨跌幅": 0.4,
                "成交额": sz_amount,
                "更新时间": sz_provider_as_of,
            },
        ]
    )


def _history(symbol: str, **_kwargs) -> list[dict[str, object]]:
    values = {
        "sh000001": ((20.0, 30.0), (25.0, 35.0)),
        "sz399001": ((10.0, 15.0), (12.0, 18.0)),
    }[symbol]
    return [
        {"date": "2026-08-18", "time": "10:29", "amount": values[0][0]},
        {"date": "2026-08-18", "time": "10:30", "amount": values[0][1]},
        {"date": "2026-08-19", "time": "10:29", "amount": values[1][0]},
        {"date": "2026-08-19", "time": "10:30", "amount": values[1][1]},
    ]


def _daily_history(symbol: str, **_kwargs) -> list[dict[str, object]]:
    previous, current = {
        "sh000001": (50.0, 100.0),
        "sz399001": (25.0, 50.0),
    }[symbol]
    return [
        {"日期": "2026-08-18", "成交额": previous},
        {"日期": "2026-08-19", "成交额": current},
    ]


def _partial_timestamp_overview() -> pd.DataFrame:
    frame = _overview("2026-08-19")
    frame.loc[frame["代码"] == "sz399001", "更新时间"] = None
    return frame


class _Router:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.history_calls: list[str] = []
        self.daily_calls: list[str] = []
        self.fail_next_history = False
        self.fail_symbol_once: str | None = None
        self.block_history = False
        self.history_started = Event()
        self.history_release = Event()
        self._calls_lock = Lock()

    def route(self, data_type: str):
        assert data_type == "market_overview"
        return self.frame.copy(), "fixture"

    def route_validated(self, data_type: str, validator, **kwargs):
        if data_type == "market_overview":
            return validator(self.frame.copy(), "fixture"), "fixture"
        if data_type == "index_daily_amount":
            symbol = kwargs["symbol"]
            self.daily_calls.append(symbol)
            return validator(_daily_history(symbol), "fixture"), "fixture"
        assert data_type == "index_intraday_amount"
        symbol = kwargs["symbol"]
        with self._calls_lock:
            self.history_calls.append(symbol)
            call_number = len(self.history_calls)
            should_fail = self.fail_next_history or self.fail_symbol_once == symbol
            self.fail_next_history = False
            if self.fail_symbol_once == symbol:
                self.fail_symbol_once = None
        if should_fail:
            raise RuntimeError("temporary history failure")
        if self.block_history and call_number == 1:
            self.history_started.set()
            assert self.history_release.wait(timeout=3)
        return validator(_history(symbol), "fixture"), "fixture"


def _fetch(router: _Router, cache: TurnoverBaselineCache, day: int):
    return fetch_market_overview(
        now=datetime(2026, 8, day, 10, 30, 5, tzinfo=_CHINA),
        router=router,
        turnover_cache=cache,
    )


def test_today_amount_uses_live_indices_and_same_day_history_loads_once() -> None:
    router = _Router(_overview("2026-08-19"))
    cache = TurnoverBaselineCache()

    first = _fetch(router, cache, 19)
    # A later provider outage cannot clear an already established same-day baseline,
    # because the historical route must not be called again.
    router.fail_next_history = True
    router.frame = _overview(
        "2026-08-19", minute="10:31", sh_amount=120.0, sz_amount=60.0
    )
    second = _fetch(router, cache, 19)

    assert first.market_turnover.today_amount_cny == 150.0
    assert first.market_turnover.previous_same_time_amount_cny == 75.0
    assert second.market_turnover.today_amount_cny == 180.0
    assert second.market_turnover.previous_same_time_amount_cny == 75.0
    assert router.history_calls == ["sh000001", "sz399001"]
    assert router.fail_next_history is True


def test_cold_start_history_failure_retries_on_the_next_refresh() -> None:
    router = _Router(_overview("2026-08-19"))
    router.fail_next_history = True
    cache = TurnoverBaselineCache()

    first = _fetch(router, cache, 19)
    second = _fetch(router, cache, 19)

    assert first.market_turnover.available is False
    assert first.market_turnover.reason == "上一交易日同期成交额暂不可用，稍后自动重试"
    assert second.market_turnover.available is True
    assert router.history_calls == ["sh000001", "sh000001", "sz399001"]


def test_trading_date_change_never_reuses_the_old_baseline() -> None:
    router = _Router(_overview("2026-08-19"))
    cache = TurnoverBaselineCache()

    first = _fetch(router, cache, 19)
    router.frame = _overview("2026-08-20", sh_amount=130.0, sz_amount=70.0)
    second = _fetch(router, cache, 20)

    assert first.market_turnover.previous_date == "2026-08-18"
    assert second.market_turnover.previous_date == "2026-08-19"
    assert second.market_turnover.previous_same_time_amount_cny == 90.0
    assert router.history_calls == [
        "sh000001",
        "sz399001",
        "sh000001",
        "sz399001",
    ]


def test_pre_open_keeps_the_latest_completed_session_turnover_available() -> None:
    router = _Router(_overview("2026-08-19", minute="15:00"))

    snapshot = fetch_market_overview(
        now=datetime(2026, 8, 20, 0, 4, tzinfo=_CHINA),
        router=router,
        turnover_cache=TurnoverBaselineCache(),
    )

    assert snapshot.market_state.label == "等待开盘"
    assert snapshot.market_turnover.available is True
    assert snapshot.market_turnover.today_date == "2026-08-19"
    assert snapshot.market_turnover.previous_date == "2026-08-18"
    assert snapshot.market_turnover.as_of == "15:00"
    assert snapshot.market_turnover.today_amount_cny == 150.0
    assert snapshot.market_turnover.previous_same_time_amount_cny == 75.0
    assert router.daily_calls == ["sh000001", "sz399001"]
    assert router.history_calls == []


def test_live_turnover_is_required_when_the_opening_auction_completes() -> None:
    assert not _requires_live_turnover(
        datetime(2026, 8, 20, 9, 24, 59, tzinfo=_CHINA)
    )
    assert _requires_live_turnover(
        datetime(2026, 8, 20, 9, 25, tzinfo=_CHINA)
    )


def test_partial_history_failure_never_caches_half_a_baseline() -> None:
    router = _Router(_overview("2026-08-19"))
    router.fail_symbol_once = "sz399001"
    cache = TurnoverBaselineCache()

    first = _fetch(router, cache, 19)
    second = _fetch(router, cache, 19)

    assert first.market_turnover.available is False
    assert second.market_turnover.available is True
    assert router.history_calls == [
        "sh000001",
        "sz399001",
        "sh000001",
        "sz399001",
    ]


def test_concurrent_refreshes_share_one_history_load() -> None:
    router = _Router(_overview("2026-08-19"))
    router.block_history = True
    cache = TurnoverBaselineCache()

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_fetch, router, cache, 19) for _ in range(5)]
        assert router.history_started.wait(timeout=3)
        router.history_release.set()
        snapshots = [future.result(timeout=3) for future in futures]

    assert all(item.market_turnover.available for item in snapshots)
    assert router.history_calls == ["sh000001", "sz399001"]


def test_invalid_history_payload_falls_back_before_router_success() -> None:
    router = SmartRouter()
    router.register(
        "index_intraday_amount",
        "bad",
        lambda **_kwargs: [{"date": "2026-08-18", "time": "10:30", "amount": "nan"}],
        priority=1,
    )
    router.register(
        "index_intraday_amount",
        "good",
        lambda **_kwargs: _history("sh000001"),
        priority=2,
    )
    fetched_at = datetime(2026, 8, 19, 10, 30, tzinfo=_CHINA)

    series, provider = router.route_validated(
        "index_intraday_amount",
        lambda payload, source: map_index_intraday_amount(
            payload,
            source,
            instrument_id="000001.SH",
            fetched_at=fetched_at,
        ),
        symbol="sh000001",
        days=5,
    )

    assert provider == "good"
    assert series.instrument_id == "000001.SH"
    health = {item["source"]: item for item in router.get_health_report()}
    assert health["index_intraday_amount:bad"]["fail_count"] == 1


def test_invalid_daily_payload_falls_back_before_router_success() -> None:
    router = SmartRouter()
    router.register(
        "index_daily_amount",
        "bad",
        lambda **_kwargs: [{"日期": "2026-08-19", "成交额": None}],
        priority=1,
    )
    router.register(
        "index_daily_amount",
        "good",
        lambda **_kwargs: _daily_history("sh000001"),
        priority=2,
    )
    fetched_at = datetime(2026, 8, 20, 0, 4, tzinfo=_CHINA)

    series, provider = router.route_validated(
        "index_daily_amount",
        lambda payload, source: map_index_daily_amount(
            payload,
            source,
            instrument_id="000001.SH",
            fetched_at=fetched_at,
        ),
        symbol="sh000001",
        days=5,
    )

    assert provider == "good"
    assert series.instrument_id == "000001.SH"
    assert [point.amount_cny for point in series.points] == [50.0, 100.0]
    health = {item["source"]: item for item in router.get_health_report()}
    assert health["index_daily_amount:bad"]["fail_count"] == 1


@pytest.mark.parametrize(
    "bad_frame",
    [
        _overview("2026-08-19", sz_amount=None),
        _overview("2026-08-19", sh_minute="10:29", sz_minute="10:30"),
        _partial_timestamp_overview(),
    ],
    ids=("amount-missing", "minute-mismatch", "partial-timestamp"),
)
def test_live_turnover_semantics_fall_back_before_market_source_success(
    bad_frame: pd.DataFrame,
) -> None:
    router = SmartRouter()
    router.register("market_overview", "bad", lambda: bad_frame, priority=1)
    router.register(
        "market_overview",
        "good",
        lambda: _overview("2026-08-19"),
        priority=2,
    )

    snapshot = fetch_market_overview(
        now=datetime(2026, 8, 19, 10, 30, 5, tzinfo=_CHINA),
        router=router,
        turnover_fetcher=_history,
        turnover_cache=TurnoverBaselineCache(),
    )

    assert snapshot.metadata.provider == "good"
    assert snapshot.market_turnover.available is True
    health = {item["source"]: item for item in router.get_health_report()}
    assert health["market_overview:bad"]["fail_count"] == 1


def test_concurrent_failed_cohort_shares_one_attempt_then_next_call_retries() -> None:
    cache = TurnoverBaselineCache()
    started = Event()
    release = Event()
    attempts = 0
    attempts_lock = Lock()

    def failed_loader():
        nonlocal attempts
        with attempts_lock:
            attempts += 1
        started.set()
        assert release.wait(timeout=3)
        raise RuntimeError("temporary baseline failure")

    def consume_failure() -> str:
        with pytest.raises(TurnoverBaselineUnavailable):
            cache.get_or_load(
                trading_date=datetime(2026, 8, 19).date(),
                loader=failed_loader,
            )
        return "unavailable"

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(consume_failure) for _ in range(5)]
        assert started.wait(timeout=3)
        time_module.sleep(0.05)
        release.set()
        assert [future.result(timeout=3) for future in futures] == [
            "unavailable"
        ] * 5

    assert attempts == 1
    with pytest.raises(TurnoverBaselineUnavailable):
        cache.get_or_load(
            trading_date=datetime(2026, 8, 19).date(),
            loader=failed_loader,
        )
    assert attempts == 2
