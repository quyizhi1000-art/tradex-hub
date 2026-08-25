from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from tradex.data_gateway import fetch_dragon_tiger_day, fetch_stock_fund_flow_day
from tradex.data_gateway.contracts import QualityStatus


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 24, 20, 30, tzinfo=SHANGHAI)


class Router:
    def __init__(self, *attempts):
        self.attempts = attempts
        self.calls = []

    def route_validated(self, data_type, validator, **kwargs):
        self.calls.append((data_type, kwargs))
        last_error = None
        for provider, payload in self.attempts:
            try:
                return validator(payload, provider), provider
            except Exception as exc:
                last_error = exc
        raise last_error


def _frame(*rows, trade_date="20260824", provider_as_of="2026-08-24T20:00:00+08:00", **attrs):
    frame = pd.DataFrame(rows)
    frame.attrs.update(
        {
            "source_valid": True,
            "trade_date": trade_date,
            "provider_as_of": provider_as_of,
            "request_id": "daily-review-fixture",
            **attrs,
        }
    )
    return frame


def test_tushare_stock_fund_flow_maps_ten_thousand_cny_and_sorts_ids():
    frame = _frame(
        {
            "ts_code": "600000.SH",
            "trade_date": "20260824",
            "buy_lg_amount": 20,
            "sell_lg_amount": 5,
            "buy_elg_amount": 8,
            "sell_elg_amount": 3,
            "net_mf_amount": 24,
        },
        {
            "ts_code": "000001.SZ",
            "trade_date": "20260824",
            "buy_lg_amount": 4,
            "sell_lg_amount": 6,
            "buy_elg_amount": 1,
            "sell_elg_amount": 2,
            "net_mf_amount": -3,
        },
    )
    router = Router(("tushare", frame))

    series = fetch_stock_fund_flow_day("2026-08-24", router=router, now=NOW)

    assert router.calls == [("stock_fund_flow_day", {"trade_date": "20260824"})]
    assert series.metadata.contract == "stock_fund_flow_day.v1"
    assert series.metadata.quality == QualityStatus.DEGRADED
    assert [item.instrument_id for item in series.flows] == ["000001.SZ", "600000.SH"]
    assert series.flows[1].net_amount_cny == 240_000
    assert series.flows[1].large_net_amount_cny == 150_000
    assert series.flows[1].extra_large_net_amount_cny == 50_000


def test_wrong_day_stock_flow_is_rejected_inside_route_and_falls_back():
    old = _frame(
        {
            "ts_code": "600000.SH",
            "trade_date": "20260823",
            "buy_lg_amount": 1,
            "sell_lg_amount": 0,
            "buy_elg_amount": 1,
            "sell_elg_amount": 0,
            "net_mf_amount": 2,
        },
        trade_date="20260823",
    )
    current = _frame(
        {
            "ts_code": "000001.SZ",
            "trade_date": "20260824",
            "buy_lg_amount": 1,
            "sell_lg_amount": 0,
            "buy_elg_amount": 1,
            "sell_elg_amount": 0,
            "net_mf_amount": 2,
        }
    )
    series = fetch_stock_fund_flow_day(
        "20260824",
        router=Router(("tushare", old), ("tushare", current)),
        now=NOW,
    )
    assert series.flows[0].instrument_id == "000001.SZ"


def test_tushare_dragon_tiger_preserves_top_list_cny_and_orders_net_buy():
    frame = _frame(
        {
            "trade_date": "20260824",
            "ts_code": "600000.SH",
            "name": "浦发银行",
            "close": 12.3,
            "pct_change": 5.2,
            "turnover_rate": 3.1,
            "amount": 5000,
            "l_buy": 800,
            "l_sell": 300,
            "net_amount": 500,
            "reason": "日涨幅偏离值达到7%",
        },
        {
            "trade_date": "20260824",
            "ts_code": "000001.SZ",
            "name": "平安银行",
            "close": 10.1,
            "pct_change": -4.2,
            "turnover_rate": 2.2,
            "amount": 4000,
            "l_buy": 200,
            "l_sell": 350,
            "net_amount": -150,
            "reason": "日跌幅偏离值达到7%",
        },
    )

    series = fetch_dragon_tiger_day("20260824", router=Router(("tushare", frame)), now=NOW)

    assert series.metadata.contract == "dragon_tiger_market_day.v1"
    assert series.trades[0].instrument_id == "600000.SH"
    assert series.trades[0].buy_amount_cny == 800
    assert series.trades[0].net_amount_cny == 500
    assert series.trades[1].net_amount_cny == -150


def test_verified_empty_dragon_tiger_day_is_degraded_but_safe():
    frame = _frame(valid_empty=True)
    series = fetch_dragon_tiger_day(
        "2026-08-24", router=Router(("tushare", frame)), now=NOW
    )
    assert series.valid_empty is True
    assert series.trades == ()
    assert series.metadata.quality == QualityStatus.DEGRADED
    assert "verified_empty_day" in series.metadata.quality_flags


def test_akshare_exact_day_fallback_keeps_missing_close_explicit():
    frame = pd.DataFrame(
        [
            {
                "代码": "600000",
                "名称": "浦发银行",
                "上榜日": "2026-08-24",
                "解读": "日涨幅偏离值达到7%",
                "涨跌幅": 8.1,
                "龙虎榜买入额": 8_000_000,
                "龙虎榜卖出额": 3_000_000,
                "龙虎榜净买额": 5_000_000,
            }
        ]
    )
    frame.attrs.update(
        {
            "source_valid": True,
            "trade_date": "2026-08-24",
            "provider_as_of": None,
            "valid_empty": False,
        }
    )

    series = fetch_dragon_tiger_day(
        "2026-08-24",
        router=Router(("akshare_exact_day", frame)),
        now=NOW,
    )

    assert series.trades[0].close is None
    assert series.trades[0].net_amount_cny == 5_000_000
    assert series.trades[0].reason == "日涨幅偏离值达到7%"
    assert series.metadata.quality == QualityStatus.DEGRADED
    assert "provider_timestamp_missing" in series.metadata.quality_flags
