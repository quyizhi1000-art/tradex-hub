from __future__ import annotations

import pandas as pd
import pytest
from astock_signals.smart_router import SourceCapabilityError

from tradex.data_gateway.providers.etfs import map_etf_quote_payload
from tradex.data_gateway.providers.market_structure import map_sector_quote_frame
from tradex.data_gateway.providers.market_universe import (
    map_a_share_universe_payload,
)
from tradex.data_sources import registry, tushare_fetchers
from tradex.data_sources.tushare_client import TushareResult


TRADE_DATE = "2026-08-21"
COMPACT_DATE = "20260821"


def _result(*records: dict, request_id: str = "fixture-request") -> TushareResult:
    return TushareResult(records=tuple(records), request_id=request_id)


def test_market_universe_uses_paid_rt_k_full_market_and_cny_units(
    monkeypatch,
) -> None:
    calls = []

    def fake_request(api_name, params=None, fields=None):
        calls.append((api_name, params, fields))
        return _result(
            {
                "ts_code": "600000.SH",
                "name": "浦发银行",
                "open": 10.2,
                "high": 12.0,
                "low": 10.0,
                "close": 11.0,
                "pre_close": 10.0,
                "vol": 112_227,
                "amount": 1_234_500,
                "trade_time": "2026-08-21 15:00:00",
            },
            request_id="rt-k-request",
        )

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    frame = tushare_fetchers.fetch_market_universe(trade_date=TRADE_DATE)
    quotes, row_count, excluded, provider_as_of = map_a_share_universe_payload(
        frame, provider="tushare"
    )

    assert [item[0] for item in calls] == ["rt_k"]
    assert calls[0][1] == {"ts_code": "3*.SZ,6*.SH,0*.SZ,9*.BJ"}
    assert frame.loc[0, "amount"] == 1_234_500
    assert frame.attrs["unit_contract"] == {
        "vol": "shares",
        "amount": "CNY",
    }
    assert row_count == 1
    assert excluded == 0
    assert provider_as_of.isoformat() == "2026-08-21T15:00:00+08:00"
    quote = quotes[0]
    assert quote.instrument_id == "600000.SH"
    assert quote.amount_cny == 1_234_500
    assert quote.total_market_cap_cny is None
    assert quote.float_market_cap_cny is None
    assert quote.amplitude_pct == pytest.approx(20.0)


def test_etf_quotes_use_paid_rt_etf_k_for_both_markets_with_cny_units(
    monkeypatch,
) -> None:
    calls = []

    def fake_request(api_name, params=None, fields=None):
        calls.append((api_name, params, fields))
        if params == {"ts_code": "1*.SZ"}:
            return _result(
                {
                    "ts_code": "159915.SZ",
                    "name": "创业板ETF",
                    "pre_close": 1.0,
                    "open": 1.0,
                    "high": 1.03,
                    "low": 0.99,
                    "close": 1.02,
                    "vol": 10_000,
                    "amount": 10_200,
                    "trade_time": "2026-08-21 14:59:59",
                },
                request_id="sz-etf-request",
            )
        return _result(
            {
                "ts_code": "510300.SH",
                "name": "沪深300ETF",
                "pre_close": 4.0,
                "open": 4.0,
                "high": 4.3,
                "low": 4.0,
                "close": 4.2,
                "vol": 10_000,
                "amount": 42_000,
                "trade_time": "2026-08-21 15:00:00",
            },
            {
                "ts_code": "516720.SH",
                "name": "轻量成交ETF",
                "pre_close": 1.14,
                "open": 1.141,
                "high": 1.151,
                "low": 1.141,
                "close": 1.141,
                # Compatible proxy returns lots and rounded turnover here.
                "vol": 16,
                "amount": 1_800,
                "trade_time": "2026-08-21 15:00:00",
            },
            request_id="sh-etf-request",
        )

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    frame = tushare_fetchers.fetch_etf_quotes(trade_date=TRADE_DATE)
    quotes = map_etf_quote_payload(frame, provider="tushare", limit=10)

    assert [item[0] for item in calls] == ["rt_etf_k", "rt_etf_k"]
    assert calls[0][1] == {"ts_code": "1*.SZ"}
    assert calls[1][1] == {"ts_code": "5*.SH", "topic": "HQ_FND_TICK"}
    assert frame.attrs["unit_contract"] == {"vol": "shares", "amount": "CNY"}
    assert frame.loc[frame["ts_code"] == "516720.SH", "vol"].item() == 1_600
    assert quotes[0].instrument_id == "510300.SH"
    assert quotes[0].name == "沪深300ETF"
    assert quotes[0].amount_cny == 42_000
    assert quotes[0].provider_as_of.isoformat() == "2026-08-21T15:00:00+08:00"


def test_etf_quotes_use_batch_unit_evidence_for_sparse_rounding_outlier(
    monkeypatch,
) -> None:
    def fake_request(api_name, params=None, fields=None):
        assert api_name == "rt_etf_k"
        if params == {"ts_code": "1*.SZ"}:
            return _result(
                {
                    "ts_code": "159915.SZ",
                    "name": "创业板ETF",
                    "pre_close": 1.0,
                    "open": 1.0,
                    "high": 1.03,
                    "low": 0.99,
                    "close": 1.02,
                    "vol": 10_000,
                    "amount": 10_200,
                    "trade_time": "2026-08-21 11:29:59",
                }
            )
        regular = [
            {
                "ts_code": f"510{index:03d}.SH",
                "name": f"单位证据ETF{index}",
                "pre_close": 1.0,
                "open": 1.0,
                "high": 1.01,
                "low": 0.99,
                "close": 1.0,
                "vol": 1_000,
                "amount": 100_000,
                "trade_time": "2026-08-21 11:29:59",
            }
            for index in range(20)
        ]
        return _result(
            *regular,
            {
                "ts_code": "516720.SH",
                "name": "轻量成交ETF",
                "pre_close": 1.14,
                "open": 1.148,
                "high": 1.155,
                "low": 1.148,
                "close": 1.155,
                "vol": 5,
                # 实盘低成交样本按百元取整，逐行均价无法落入日内区间。
                "amount": 600,
                "trade_time": "2026-08-21 11:29:59",
            },
        )

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    frame = tushare_fetchers.fetch_etf_quotes(trade_date=TRADE_DATE)

    assert frame.loc[frame["ts_code"] == "516720.SH", "vol"].item() == 500
    assert frame.attrs["volume_consensus_fallback_count"] == 1


def test_etf_quotes_reject_unresolved_unit_without_batch_evidence(monkeypatch) -> None:
    def fake_request(api_name, params=None, fields=None):
        assert api_name == "rt_etf_k"
        suffix = "SZ" if params == {"ts_code": "1*.SZ"} else "SH"
        code = "159915.SZ" if suffix == "SZ" else "516720.SH"
        return _result(
            {
                "ts_code": code,
                "name": "单位未知ETF",
                "pre_close": 1.14,
                "open": 1.148,
                "high": 1.155,
                "low": 1.148,
                "close": 1.155,
                "vol": 5,
                "amount": 600,
                "trade_time": "2026-08-21 11:29:59",
            }
        )

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    with pytest.raises(RuntimeError, match="成交量单位无法唯一判定"):
        tushare_fetchers.fetch_etf_quotes(trade_date=TRADE_DATE)


def test_sector_quotes_use_paid_rt_sw_k_and_preserve_cny(monkeypatch) -> None:
    calls = []

    def fake_request(name_arg, params=None, fields=None):
        calls.append((name_arg, params, fields))
        return _result(
            {
                "ts_code": "801010.SI",
                "name": "农林牧渔",
                "trade_time": "2026-08-21 14:59:58",
                "close": 2931.136,
                "pre_close": 2915.17,
                "high": 2950.392,
                "open": 2910.821,
                "low": 2904.044,
                "vol": 2_594_579_310,
                "amount": 22_663_408_519,
                "pct_change": 0.55,
            }
        )

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    frame = tushare_fetchers.fetch_sector_quotes(board_type="industry")
    quotes = map_sector_quote_frame(
        frame, provider="tushare", sector_type="industry"
    )

    assert calls[0][0:2] == ("rt_sw_k", {})
    assert frame.attrs["unit_contract"] == {"vol": "shares", "amount": "CNY"}
    assert quotes[0].name == "农林牧渔"
    assert quotes[0].value == 2931.136
    assert quotes[0].amount_cny == 22_663_408_519
    assert quotes[0].main_net_inflow_cny is None
    assert quotes[0].provider_as_of.isoformat() == "2026-08-21T14:59:58+08:00"


def test_tushare_sector_quotes_reject_concept_before_spending_quota(monkeypatch) -> None:
    monkeypatch.setattr(
        tushare_fetchers,
        "_request",
        lambda *_args, **_kwargs: pytest.fail("concept must fall back before request"),
    )

    with pytest.raises(SourceCapabilityError, match="Shenwan industry"):
        tushare_fetchers.fetch_sector_quotes(board_type="concept")


def test_stock_fund_flow_stays_raw_and_declares_unit_contract(monkeypatch) -> None:
    calls = []

    def fake_request(api_name, params=None, fields=None):
        calls.append((api_name, params, fields))
        return _result(
            {
                "ts_code": "000001.SZ",
                "trade_date": COMPACT_DATE,
                "buy_sm_vol": 10,
                "buy_sm_amount": 12.5,
                "net_mf_vol": 3,
                "net_mf_amount": -2.25,
            }
        )

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    frame = tushare_fetchers.fetch_stock_fund_flow(trade_date=TRADE_DATE)

    assert calls[0][0:2] == ("moneyflow", {"trade_date": COMPACT_DATE})
    assert frame.loc[0, "buy_sm_amount"] == 12.5
    assert frame.loc[0, "net_mf_amount"] == -2.25
    assert frame.attrs["unit_contract"] == {
        "*_vol": "lots",
        "*_amount": "ten_thousand_CNY",
    }
    assert frame.attrs["provider_as_of"] == "2026-08-21T19:00:00+08:00"


def test_dragon_tiger_stays_raw_and_declares_cny_fields(monkeypatch) -> None:
    calls = []

    def fake_request(api_name, params=None, fields=None):
        calls.append((api_name, params, fields))
        return _result(
            {
                "trade_date": COMPACT_DATE,
                "ts_code": "600519.SH",
                "name": "贵州茅台",
                "close": 1500.0,
                "pct_change": 5.2,
                "turnover_rate": 1.1,
                "amount": 1_000_000_000,
                "l_sell": 200_000_000,
                "l_buy": 300_000_000,
                "l_amount": 500_000_000,
                "net_amount": 100_000_000,
                "net_rate": 10.0,
                "amount_rate": 50.0,
                "float_values": 300_000_000_000,
                "reason": "日涨幅偏离值达7%",
            }
        )

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    frame = tushare_fetchers.fetch_dragon_tiger_market_day(
        trade_date=TRADE_DATE, board_type="all", look_back_days=1
    )

    assert calls[0][0:2] == ("top_list", {"trade_date": COMPACT_DATE})
    assert frame.loc[0, "net_amount"] == 100_000_000
    assert frame.attrs["unit_contract"]["net_amount"] == "CNY"
    assert frame.attrs["trade_date"] == TRADE_DATE
    assert frame.attrs["provider_as_of"] == "2026-08-21T20:00:00+08:00"


def test_registry_inserts_daily_review_routes_without_removing_fallbacks(
    monkeypatch,
) -> None:
    class CaptureRouter:
        def __init__(self) -> None:
            self.entries = []

        def register(
            self, data_type, source_name, fetcher, priority=1, exclusive=False
        ) -> None:
            self.entries.append(
                {
                    "data_type": data_type,
                    "source_name": source_name,
                    "fetcher": fetcher,
                    "priority": priority,
                    "exclusive": exclusive,
                }
            )

        def get_registry_report(self):
            return self.entries

    router = CaptureRouter()
    enabled = {
        "market_universe",
        "etf_quotes",
        "sector_quotes",
        "stock_fund_flow",
        "dragon_tiger_market_day",
    }
    monkeypatch.setattr(registry, "_registered", False)
    monkeypatch.setattr(registry, "get_router", lambda: router)
    monkeypatch.setattr(registry, "tushare_provides", lambda item: item in enabled)
    monkeypatch.setattr(registry, "biying_provides", lambda _item: False)
    monkeypatch.setattr(registry, "fuyao_is_configured", lambda: False)

    registry._register_all_sources_unlocked()

    selected = [
        (item["data_type"], item["source_name"], item["priority"])
        for item in router.entries
        if item["data_type"]
        in {
            "stock_list",
            "market_universe",
            "etf_data",
            "etf_quotes",
            "industry_quotes",
            "stock_fund_flow_day",
            "dragon_tiger_market_day",
        }
    ]
    assert selected == [
        ("stock_list", "akshare", 1),
        ("market_universe", "tushare", 1),
        ("market_universe", "akshare", 100),
        ("etf_data", "astock_signals", 1),
        ("etf_quotes", "tushare", 1),
        ("etf_quotes", "astock_signals", 100),
        ("stock_fund_flow_day", "tushare", 1),
        ("dragon_tiger_market_day", "tushare", 1),
        ("dragon_tiger_market_day", "akshare_exact_day", 100),
        ("industry_quotes", "tushare", 1),
        ("industry_quotes", "em_push2", 100),
    ]
