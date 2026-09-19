from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Lock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from astock_signals.smart_router import SmartRouter

from tradex.data_gateway.contracts import QualityStatus
from tradex.data_gateway.intraday import (
    IntradayMinuteCache,
    fetch_intraday_minute_series,
    fetch_intraday_minute_series_batch,
    fetch_intraday_minute_series_batch_partial,
    intraday_minute_to_legacy_payload,
)


_NOW = datetime(2026, 8, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class _Router:
    def __init__(self, frame: pd.DataFrame, provider: str) -> None:
        self.frame = frame
        self.provider = provider
        self.calls = 0

    def route_validated(self, data_type, validator, **kwargs):
        self.calls += 1
        assert data_type == "minute_data"
        return validator(self.frame, self.provider), self.provider


def _tushare_frame(*, bad_amount: bool = False) -> pd.DataFrame:
    frame = pd.DataFrame(
        [
            {
                "代码": "000001.SZ",
                "时间": "2026-08-24T09:31:00+08:00",
                "开盘": 10.10,
                "收盘": 10.16,
                "最高": 10.20,
                "最低": 10.08,
                "成交量": 20_000,
                "成交额": 1 if bad_amount else 203_000,
            },
            {
                "代码": "000001.SZ",
                "时间": "2026-08-24T09:30:00+08:00",
                "开盘": 10.00,
                "收盘": 10.08,
                "最高": 10.10,
                "最低": 9.98,
                "成交量": 10_000,
                "成交额": 100_500,
            },
        ]
    )
    frame.attrs.update(
        {
            "trading_date": "2026-08-24",
            "frequency_minutes": 1,
            "volume_unit": "shares",
            "amount_unit": "CNY",
            "provider_as_of": "2026-08-24T09:31:00+08:00",
            "request_id": "minute-request-1",
        }
    )
    return frame


def _eltdx_frame() -> pd.DataFrame:
    frame = pd.DataFrame(
        [
            {
                "代码": "sz000001",
                "时间": "09:30",
                "价格": 10.08,
                "均价": 10.05,
                "成交量": 100,
            },
            {
                "代码": "sz000001",
                "时间": "09:31",
                "价格": 10.16,
                "均价": 303_500 / 30_000,
                "成交量": 200,
            },
        ]
    )
    frame.attrs.update({"frequency_minutes": 1, "volume_unit": "lots"})
    return frame


def test_batch_gateway_maps_multiple_stocks_from_one_provider_route() -> None:
    frame = pd.concat(
        [
            _tushare_frame(),
            _tushare_frame().assign(
                代码="600000.SH",
                收盘=[12.0, 11.9],
                开盘=[12.0, 11.9],
                最高=[12.0, 11.9],
                最低=[12.0, 11.9],
                成交额=[240_000, 119_000],
            ),
        ],
        ignore_index=True,
    )
    frame.attrs.update(_tushare_frame().attrs)

    class _BatchRouter:
        def __init__(self) -> None:
            self.calls = 0

        def route_validated(self, data_type, validator, **kwargs):
            self.calls += 1
            assert data_type == "minute_data_batch"
            assert kwargs["symbols"] == ("000001.SZ", "600000.SH")
            return validator(frame, "tushare"), "tushare"

    router = _BatchRouter()
    result = fetch_intraday_minute_series_batch(
        ("600000.SH", "000001.SZ"),
        router=router,
        now=_NOW,
    )

    assert router.calls == 1
    assert tuple(result) == ("000001.SZ", "600000.SH")
    assert all(series.metadata.provider == "tushare" for series in result.values())


def test_partial_batch_preserves_omitted_instrument_for_exact_single_fallback() -> None:
    frame = _tushare_frame()

    class _PartialRouter:
        def route_validated(self, data_type, validator, **kwargs):
            assert data_type == "minute_data_batch_partial"
            assert kwargs["symbols"] == ("000001.SZ", "600000.SH")
            return validator(frame, "tushare"), "tushare"

    result = fetch_intraday_minute_series_batch_partial(
        ("600000.SH", "000001.SZ"),
        router=_PartialRouter(),
        now=_NOW,
    )

    assert tuple(result) == ("000001.SZ",)


def test_batch_contract_rejects_more_than_verified_40_instruments() -> None:
    symbols = tuple(f"{number:06d}.SZ" for number in range(1, 42))
    with pytest.raises(ValueError, match="at most 40"):
        fetch_intraday_minute_series_batch(symbols, router=object(), now=_NOW)


def test_empty_partial_batch_requires_explicit_recovery_opt_in() -> None:
    class Router:
        def route_validated(self, data_type, validator, **kwargs):
            return validator(_tushare_frame().iloc[:0], "tushare"), "tushare"

    with pytest.raises(RuntimeError, match="no exact curves"):
        fetch_intraday_minute_series_batch_partial(["000001.SZ"], router=Router())
    assert fetch_intraday_minute_series_batch_partial(
        ["000001.SZ"], router=Router(), allow_empty=True,
    ) == {}


def test_required_minutes_reject_short_primary_before_fallback_and_skip_cache() -> None:
    owner = IntradayMinuteCache()
    short = _tushare_frame().iloc[:1].copy()
    fetch_intraday_minute_series("000001.SZ", router=_Router(short, "tushare"), cache=owner)
    router = SmartRouter()
    router.register("minute_data", "tushare", lambda **kwargs: short, 1)
    router.register("minute_data", "eastmoney", lambda **kwargs: _tushare_frame(), 2)
    result = fetch_intraday_minute_series(
        "000001.SZ", router=router, cache=owner, now=_NOW,
        expected_trading_date=_NOW.date(),
        required_minutes=(datetime.strptime("09:30", "%H:%M").time(),),
    )
    assert result.metadata.provider == "eastmoney"
    assert len(result.points) == 2


def test_tushare_minutes_sort_and_derive_cumulative_vwap() -> None:
    series = fetch_intraday_minute_series(
        "000001",
        router=_Router(_tushare_frame(), "tushare"),
        now=_NOW,
        use_cache=False,
    )

    assert series.instrument_id == "000001.SZ"
    assert series.trading_date.isoformat() == "2026-08-24"
    assert [point.minute.strftime("%H:%M") for point in series.points] == [
        "09:30",
        "09:31",
    ]
    assert [point.volume_shares for point in series.points] == [10_000, 20_000]
    assert series.points[0].cumulative_average_price == pytest.approx(10.05)
    assert series.points[1].cumulative_average_price == pytest.approx(
        303_500 / 30_000
    )
    assert series.metadata.provider == "tushare"
    assert series.metadata.provider_request_id == "minute-request-1"
    assert series.metadata.quality is QualityStatus.ACCEPTED


def test_tushare_partial_amount_preserves_price_series_as_degraded() -> None:
    frame = _tushare_frame()
    frame.loc[1, "成交额"] = None

    series = fetch_intraday_minute_series(
        "000001",
        router=_Router(frame, "tushare"),
        now=_NOW,
        use_cache=False,
    )

    assert [point.price for point in series.points] == [10.08, 10.16]
    assert series.points[0].amount_cny is None
    assert all(point.cumulative_average_price is None for point in series.points)
    assert series.metadata.quality is QualityStatus.DEGRADED
    assert set(series.metadata.quality_flags) == {
        "amount_partial",
        "cumulative_average_partial",
    }


def test_eltdx_lots_map_to_same_canonical_share_units() -> None:
    series = fetch_intraday_minute_series(
        "000001",
        router=_Router(_eltdx_frame(), "eltdx"),
        now=_NOW,
        use_cache=False,
    )

    assert [point.volume_shares for point in series.points] == [10_000, 20_000]
    assert series.points[1].cumulative_average_price == pytest.approx(
        303_500 / 30_000
    )
    assert series.trading_date is None
    assert series.metadata.quality is QualityStatus.DEGRADED
    assert set(series.metadata.quality_flags) == {
        "trading_date_missing",
        "ohlc_partial",
        "amount_partial",
        "provider_timestamp_missing",
    }


def test_bad_primary_payload_falls_back_before_route_success() -> None:
    router = SmartRouter()
    router.register(
        "minute_data", "tushare", lambda **_kwargs: _tushare_frame(bad_amount=True), 1
    )
    router.register("minute_data", "eltdx", lambda **_kwargs: _eltdx_frame(), 100)

    series = fetch_intraday_minute_series(
        "000001", router=router, now=_NOW, use_cache=False
    )

    assert series.metadata.provider == "eltdx"
    health = {item["source"]: item for item in router.get_health_report()}
    assert health["minute_data:tushare"]["fail_count"] == 1
    assert health["minute_data:eltdx"]["total_calls"] == 1
    assert health["minute_data:eltdx"]["fail_count"] == 0


def test_minutes_reject_duplicate_and_cross_date_rows() -> None:
    duplicate = _tushare_frame()
    duplicate.loc[1, "时间"] = duplicate.loc[0, "时间"]
    with pytest.raises(RuntimeError, match="duplicate minute"):
        fetch_intraday_minute_series(
            "000001",
            router=_Router(duplicate, "tushare"),
            now=_NOW,
            use_cache=False,
        )

    cross_date = _tushare_frame()
    cross_date.loc[1, "时间"] = "2026-08-23T09:30:00+08:00"
    with pytest.raises(RuntimeError, match="multiple trading dates"):
        fetch_intraday_minute_series(
            "000001",
            router=_Router(cross_date, "tushare"),
            now=_NOW,
            use_cache=False,
        )


def test_gateway_cache_singleflights_same_instrument() -> None:
    frame = _tushare_frame()

    class _SlowRouter(_Router):
        def __init__(self) -> None:
            super().__init__(frame, "tushare")
            self._lock = Lock()

        def route_validated(self, data_type, validator, **kwargs):
            with self._lock:
                self.calls += 1
            time.sleep(0.05)
            return validator(self.frame, self.provider), self.provider

    router = _SlowRouter()
    owner = IntradayMinuteCache()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _index: fetch_intraday_minute_series(
                    "000001",
                    router=router,
                    now=_NOW,
                    cache=owner,
                ),
                range(4),
            )
        )

    assert router.calls == 1
    assert all(result is results[0] for result in results)


def test_legacy_payload_shape_and_share_units_are_stable() -> None:
    series = fetch_intraday_minute_series(
        "000001",
        router=_Router(_tushare_frame(), "tushare"),
        now=_NOW,
        use_cache=False,
    )

    assert intraday_minute_to_legacy_payload(series) == {
        "code": "000001",
        "point_count": 2,
        "points": [
            {
                "time": "09:30",
                "price": 10.08,
                "avg_price": 10.05,
                "volume": 10_000,
            },
            {
                "time": "09:31",
                "price": 10.16,
                "avg_price": pytest.approx(303_500 / 30_000),
                "volume": 20_000,
            },
        ],
    }
