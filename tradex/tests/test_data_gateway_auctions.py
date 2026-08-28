from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from astock_signals.smart_router import SmartRouter
from tradex.data_gateway.auctions import (
    fetch_opening_auction_market,
    fetch_opening_auction_snapshot,
    opening_auction_to_legacy_payload,
)
from tradex.data_gateway.contracts import QualityStatus


_NOW = datetime(2026, 8, 21, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


class _Router:
    def __init__(self, frame: pd.DataFrame, provider: str) -> None:
        self.frame = frame
        self.provider = provider

    def route_validated(self, data_type: str, validator, **kwargs):
        assert data_type == "auction_data"
        assert kwargs == {"code": "000001"}
        return validator(self.frame, self.provider), self.provider


def _valid_frame() -> pd.DataFrame:
    frame = pd.DataFrame(
        [
            {
                "代码": "000001.SZ",
                "交易日期": "20260821",
                "开盘价": 11.36,
                "开盘量": 304_900,
                "开盘额": 3_463_664,
                "开盘涨跌幅": 0.62,
                "昨收": 11.29,
                "换手率": 0.004,
                "量比": 0.2,
                "流通股本": 19_405_918_198,
            }
        ]
    )
    frame.attrs["provider_as_of"] = "2026-08-21T09:25:00+08:00"
    frame.attrs["request_id"] = "request-1"
    return frame


def test_opening_auction_maps_to_versioned_contract_and_legacy_payload() -> None:
    snapshot = fetch_opening_auction_snapshot(
        "000001", router=_Router(_valid_frame(), "tushare"), now=_NOW
    )

    assert snapshot.instrument_id == "000001.SZ"
    assert snapshot.trading_date.isoformat() == "2026-08-21"
    assert snapshot.price == 11.36
    assert snapshot.volume_shares == 304_900
    assert snapshot.amount_cny == 3_463_664
    assert snapshot.metadata.contract == "opening_auction_snapshot.v1"
    assert snapshot.metadata.provider == "tushare"
    assert snapshot.metadata.provider_request_id == "request-1"
    assert snapshot.metadata.quality is QualityStatus.ACCEPTED

    legacy = opening_auction_to_legacy_payload(snapshot)
    assert legacy["code"] == "000001"
    assert legacy["open_price"] == 11.36
    assert legacy["open_volume"] == 304_900
    assert legacy["open_amount"] == 3_463_664
    assert legacy["provider"] == "tushare"


def test_invalid_tushare_auction_falls_back_before_router_success() -> None:
    invalid = _valid_frame()
    invalid.loc[0, "开盘价"] = 0
    fallback = _valid_frame()
    # eltdx helpers.auction_data returns 09:25 volume in lots while amount is CNY.
    fallback.loc[0, "开盘量"] = 3_049
    router = SmartRouter()
    router.register("auction_data", "tushare", lambda **_kwargs: invalid, priority=1)
    router.register("auction_data", "eltdx", lambda **_kwargs: fallback, priority=2)

    snapshot = fetch_opening_auction_snapshot("000001", router=router, now=_NOW)

    assert snapshot.metadata.provider == "eltdx"
    assert snapshot.volume_shares == 304_900
    assert snapshot.amount_cny == 3_463_664
    health = {item["source"]: item for item in router.get_health_report()}
    assert health["auction_data:tushare"]["fail_count"] == 1
    assert health["auction_data:eltdx"]["success_rate"] == 100.0


def test_full_market_opening_auction_maps_exact_breadth_and_amount() -> None:
    frame = pd.DataFrame(
        [
            {
                "代码": "000001.SZ",
                "交易日期": "20260821",
                "开盘价": 11.36,
                "开盘量": 304_900,
                "开盘额": 3_463_664,
                "昨收": 11.29,
            },
            {
                "代码": "600000.SH",
                "交易日期": "20260821",
                "开盘价": 9.01,
                "开盘量": 648_500,
                "开盘额": 5_842_985,
                "昨收": 9.07,
            },
            {
                "代码": "830001.BJ",
                "交易日期": "20260821",
                "开盘价": 5.0,
                "开盘量": 100,
                "开盘额": 500,
                "昨收": 5.0,
            },
        ]
    )
    frame.attrs["provider_as_of"] = "2026-08-21T09:25:00+08:00"
    frame.attrs["request_id"] = "market-request-1"

    class MarketRouter:
        def route_validated(self, data_type, validator, **kwargs):
            assert data_type == "opening_auction_market"
            assert kwargs == {"trade_date": "2026-08-21"}
            return validator(frame, "tushare"), "tushare"

    snapshot = fetch_opening_auction_market(
        date(2026, 8, 21),
        router=MarketRouter(),
        now=_NOW,
    )

    assert snapshot.metadata.contract == "opening_auction_market.v1"
    assert snapshot.metadata.quality is QualityStatus.ACCEPTED
    assert snapshot.instrument_count == 3
    assert snapshot.provider_row_count == 3
    assert snapshot.excluded_row_count == 0
    assert (snapshot.up_count, snapshot.down_count, snapshot.flat_count) == (1, 1, 1)
    assert snapshot.total_volume_shares == 953_500
    assert snapshot.total_amount_cny == 9_307_149
