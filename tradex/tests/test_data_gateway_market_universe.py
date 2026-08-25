from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from tradex.data_gateway.contracts import QualityStatus
from tradex.data_gateway.market_universe import fetch_a_share_universe_snapshot


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 24, 17, 40, tzinfo=SHANGHAI)


class Router:
    def __init__(self, *attempts):
        self.attempts = attempts
        self.providers = []

    def route_validated(self, data_type, validator, **kwargs):
        assert data_type == "market_universe"
        assert kwargs == {"symbol": ""}
        last_error = None
        for provider, payload in self.attempts:
            self.providers.append(provider)
            try:
                return validator(payload, provider), provider
            except Exception as exc:  # test router models real fallback
                last_error = exc
        raise last_error


def _frame(*rows, provider_as_of="2026-08-24T15:00:00+08:00"):
    frame = pd.DataFrame(rows)
    frame.attrs["provider_as_of"] = provider_as_of
    frame.attrs["request_id"] = "universe-fixture"
    return frame


def test_full_market_rows_map_to_strict_canonical_units_and_count_exclusions():
    frame = _frame(
        {
            "代码": "600000",
            "名称": "浦发银行",
            "最新价": 12.1,
            "涨跌幅": 1.2,
            "成交额": 1_200_000_000,
            "今开": 12.0,
            "最高": 12.2,
            "最低": 11.9,
            "昨收": 11.95,
            "换手率": 1.8,
            "振幅": 2.5,
            "总市值": 360_000_000_000,
            "流通市值": 360_000_000_000,
        },
        {
            "代码": "000001",
            "名称": "平安银行",
            "最新价": 10.2,
            "涨跌幅": -0.5,
            "成交额": 900_000_000,
            "换手率": 1.2,
        },
        {"代码": "300001", "名称": "停牌样本", "最新价": None, "涨跌幅": None},
    )

    snapshot = fetch_a_share_universe_snapshot(
        router=Router(("akshare", frame)),
        now=NOW,
    )

    assert snapshot.metadata.contract == "a_share_universe_quote.v1"
    assert snapshot.metadata.quality == QualityStatus.DEGRADED
    assert snapshot.metadata.provider_request_id == "universe-fixture"
    assert snapshot.provider_row_count == 3
    assert snapshot.active_quote_count == 2
    assert snapshot.excluded_row_count == 1
    assert [item.instrument_id for item in snapshot.quotes] == [
        "000001.SZ",
        "600000.SH",
    ]
    assert snapshot.quotes[1].amount_cny == 1_200_000_000
    dumped = snapshot.model_dump(mode="json")
    assert "代码" not in str(dumped)
    assert "成交额" not in str(dumped)


def test_english_fallback_shape_maps_to_the_same_contract():
    frame = _frame({
        "symbol": "BJ.430001",
        "name": "北交样本",
        "last": 8.5,
        "change_pct": 2.1,
        "amount_cny": 20_000_000,
        "turnover_pct": 3.4,
    })

    snapshot = fetch_a_share_universe_snapshot(
        router=Router(("akshare", frame)),
        now=NOW,
    )

    quote = snapshot.quotes[0]
    assert quote.instrument_id == "430001.BJ"
    assert quote.name == "北交样本"
    assert quote.change_pct == 2.1
    assert quote.amount_cny == 20_000_000


def test_wrong_day_primary_is_rejected_inside_route_and_falls_back():
    old = _frame(
        {"代码": "600000", "名称": "旧数据", "最新价": 10, "涨跌幅": 1, "成交额": 1},
        provider_as_of="2026-08-22T15:00:00+08:00",
    )
    current = _frame(
        {"代码": "000001", "名称": "当日数据", "最新价": 11, "涨跌幅": 2, "成交额": 2}
    )
    router = Router(("primary", old), ("akshare", current))

    snapshot = fetch_a_share_universe_snapshot(router=router, now=NOW)

    assert router.providers == ["primary", "akshare"]
    assert snapshot.quotes[0].name == "当日数据"
    assert snapshot.metadata.provider == "akshare"


def test_market_cap_filter_requirement_rejects_incomplete_paid_shape_and_falls_back():
    paid_without_cap = _frame(
        {
            "ts_code": "600000.SH",
            "name": "浦发银行",
            "close": 11,
            "pct_chg": 1,
            "amount": 1_000_000,
        }
    )
    complete = _frame(
        {
            "代码": "000001",
            "名称": "平安银行",
            "最新价": 10,
            "涨跌幅": 2,
            "成交额": 2_000_000,
            "总市值": 200_000_000_000,
        }
    )
    router = Router(("tushare", paid_without_cap), ("akshare", complete))

    snapshot = fetch_a_share_universe_snapshot(
        require_market_cap=True,
        router=router,
        now=NOW,
    )

    assert router.providers == ["tushare", "akshare"]
    assert snapshot.metadata.provider == "akshare"
    assert snapshot.quotes[0].total_market_cap_cny == 200_000_000_000
