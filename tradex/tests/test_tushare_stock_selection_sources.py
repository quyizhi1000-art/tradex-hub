from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tradex.data_gateway.stock_selection import fetch_daily_stock_factor_snapshot
from tradex.data_gateway.providers.stock_selection import _candlestick_bar
from tradex.data_sources import tushare_fetchers
from tradex.data_sources.tushare_client import TushareResult


TRADE_DATE = date(2026, 8, 26)
COMPACT_DATE = "20260826"


def _result(*records: dict, request_id: str = "fixture") -> TushareResult:
    return TushareResult(records=tuple(records), request_id=request_id)


def _bar(ts_code: str, trade_date: str, close: float) -> dict:
    return {
        "ts_code": ts_code,
        "trade_date": trade_date,
        "open": close - 0.2,
        "high": close + 0.3,
        "low": close - 0.4,
        "close": close,
        "pre_close": close - 0.1,
        "change": 0.1,
        "pct_chg": 1.0,
        "vol": 1000,
        "amount": 250000,
    }


def test_mapper_excludes_zero_range_bar_without_rejecting_the_market_batch():
    row = _bar("600000.SH", COMPACT_DATE, 10.0)
    row.update({"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0})

    assert _candlestick_bar(row, trade_date=TRADE_DATE) is None


def test_mapper_requires_previous_close_for_limit_up_detection():
    row = _bar("600000.SH", COMPACT_DATE, 10.0)
    row.pop("pre_close")

    with pytest.raises(RuntimeError, match="previous close"):
        _candlestick_bar(row, trade_date=TRADE_DATE)


def test_tushare_daily_stock_factor_fetcher_uses_full_market_point_in_time_tables(
    monkeypatch,
):
    calls = []
    open_dates = [
        (TRADE_DATE - timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(100, -1, -1)
    ]

    def fake_request(api_name, params=None, fields=None):
        calls.append((api_name, dict(params or {}), tuple(fields or ())))
        params = dict(params or {})
        if api_name == "trade_cal":
            return _result(
                *(
                    {"exchange": "SSE", "cal_date": value, "is_open": 1}
                    for value in open_dates
                ),
                request_id="calendar",
            )
        if api_name == "daily":
            day = params["trade_date"]
            if params.get("offset"):
                return _result(request_id=f"daily-{day}-{params['offset']}")
            return _result(_bar("600000.SH", day, 10.0), request_id=f"daily-{day}")
        if api_name == "daily_basic":
            if params.get("offset"):
                return _result(request_id=f"daily-basic-{params['offset']}")
            return _result(
                {
                    "ts_code": "600000.SH",
                    "trade_date": COMPACT_DATE,
                    "turnover_rate": 2.1,
                    "volume_ratio": 1.2,
                    "pe_ttm": 12.0,
                    "pb": 1.1,
                    "dv_ratio": 2.0,
                    "total_mv": 500000,
                    "circ_mv": 400000,
                },
                request_id="daily-basic",
            )
        if api_name == "stock_basic":
            if params["list_status"] != "L":
                return _result(request_id=f"stock-{params['list_status']}")
            return _result(
                {
                    "ts_code": "600000.SH",
                    "name": "浦发银行",
                    "industry": "银行",
                    "market": "主板",
                    "list_date": "19991110",
                    "delist_date": None,
                    "list_status": "L",
                },
                request_id="stock-L",
            )
        if api_name == "fina_indicator_vip":
            if params["period"] != "20260630":
                return _result(request_id=f"finance-{params['period']}")
            return _result(
                {
                    "ts_code": "600000.SH",
                    "ann_date": "20260815",
                    "end_date": "20260630",
                    "roe_waa": 8.2,
                    "grossprofit_margin": 35.0,
                    "debt_to_assets": 70.0,
                    "or_yoy": 5.0,
                    "netprofit_yoy": 7.0,
                    "update_flag": "1",
                },
                request_id="finance-current",
            )
        if api_name == "index_daily":
            return _result(_bar("000300.SH", COMPACT_DATE, 4000.0), request_id="benchmark")
        raise AssertionError(api_name)

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    bundle = tushare_fetchers.fetch_daily_stock_factors(trade_date=TRADE_DATE)

    assert bundle["trade_date"] == "2026-08-26"
    assert len(bundle["daily"]) == 1
    assert len(bundle["daily_window"]) == 15
    assert len(bundle["candlestick_window_trade_dates"]) == 15
    assert len(bundle["financials"]) == 1
    assert bundle["unit_contract"]["daily.amount"] == "thousand_CNY"
    assert [name for name, _params, _fields in calls].count("daily") == 17
    assert [name for name, _params, _fields in calls].count("daily_basic") == 1
    assert [name for name, _params, _fields in calls].count("fina_indicator_vip") == 8
    assert {params.get("list_status") for name, params, _fields in calls if name == "stock_basic"} == {
        "L",
        "D",
        "P",
    }
    assert all(
        params.get("limit") == 6000 and params.get("offset") == 0
        for name, params, _fields in calls
        if name in {"stock_basic", "fina_indicator_vip"}
    )
    assert all(
        params.get("limit") == 6000 and params.get("offset") == 0
        for name, params, _fields in calls
        if name in {"daily", "daily_basic"}
    )


class _Router:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def route_validated(self, data_type, validate, **kwargs):
        self.calls.append((data_type, kwargs))
        payload = self.payloads[data_type]
        if callable(payload):
            payload = payload(kwargs)
        return validate(payload, "tushare"), "tushare"


def test_gateway_normalizes_units_and_rejects_future_financial_announcement():
    current = _bar("600000.SH", COMPACT_DATE, 10.0)
    second = _bar("000001.SZ", COMPACT_DATE, 12.0)
    prior_20 = "20260729"
    prior_60 = "20260603"
    candlestick_dates = tuple(
        TRADE_DATE - timedelta(days=offset) for offset in range(14, -1, -1)
    )
    candlestick_compact_dates = tuple(
        value.strftime("%Y%m%d") for value in candlestick_dates
    )
    calendar = {
        "trade_date": TRADE_DATE.isoformat(),
        "prior_20d_trade_date": "2026-07-29",
        "prior_60d_trade_date": "2026-06-03",
        "candlestick_window_trade_dates": [
            value.isoformat() for value in candlestick_dates
        ],
        "provider_as_of": "2026-08-26T18:00:00+08:00",
        "request_id": "calendar-request",
        "benchmark": [_bar("000300.SH", COMPACT_DATE, 4000.0)],
    }
    daily = {
        "trade_date": TRADE_DATE.isoformat(),
        "session_date": TRADE_DATE.isoformat(),
        "request_id": "daily-request",
        "daily": [current, second],
    }
    daily_basic = {
        "trade_date": TRADE_DATE.isoformat(),
        "request_id": "daily-basic-request",
        "daily_basic": [
            {
                "ts_code": code,
                "trade_date": COMPACT_DATE,
                "turnover_rate": 2,
                "volume_ratio": 1,
                "pe_ttm": 12,
                "pb": 1.2,
                "dv_ratio": 2,
                "total_mv": 500000,
                "circ_mv": 400000,
            }
            for code in ("600000.SH", "000001.SZ")
        ],
    }
    daily_20d = {
        "trade_date": TRADE_DATE.isoformat(),
        "session_date": "2026-07-29",
        "request_id": "daily-20d-request",
        "daily": [_bar("600000.SH", prior_20, 9.0), _bar("000001.SZ", prior_20, 10.0)],
    }
    daily_60d = {
        "trade_date": TRADE_DATE.isoformat(),
        "session_date": "2026-06-03",
        "request_id": "daily-60d-request",
        "daily": [_bar("600000.SH", prior_60, 8.0), _bar("000001.SZ", prior_60, 9.0)],
    }
    daily_slices = {
        COMPACT_DATE: daily,
        prior_20: daily_20d,
        prior_60: daily_60d,
        **{
            compact: {
                "trade_date": TRADE_DATE.isoformat(),
                "session_date": value.isoformat(),
                "request_id": f"daily-window-{compact}-request",
                "daily": [
                    _bar("600000.SH", compact, 9.5),
                    _bar("000001.SZ", compact, 11.5),
                ],
            }
            for value, compact in zip(
                candlestick_dates,
                candlestick_compact_dates,
                strict=True,
            )
            if compact != COMPACT_DATE
        },
    }
    master = {
        "trade_date": TRADE_DATE.isoformat(),
        "request_id": "master-request",
        "stock_basic": [
            {
                "ts_code": code,
                "name": name,
                "industry": "银行",
                "market": "主板",
                "list_date": "19991110",
                "list_status": "L",
            }
            for code, name in (("600000.SH", "浦发银行"), ("000001.SZ", "平安银行"))
        ]
        + [
            {
                "ts_code": "TS0018.SH",
                "name": "Tushare测试证券",
                "industry": "测试",
                "market": "测试",
                "list_date": "20200101",
                "list_status": "L",
            }
        ],
    }
    financials = {
        "trade_date": TRADE_DATE.isoformat(),
        "request_id": "financials-request",
        "financials": [
            {
                "ts_code": code,
                "ann_date": "20260815",
                "end_date": "20260630",
                "roe_waa": 8,
                "grossprofit_margin": 30,
                "debt_to_assets": 70,
                "or_yoy": 5,
                "netprofit_yoy": 6,
                "update_flag": "1",
            }
            for code in ("600000.SH", "000001.SZ")
        ]
        + [
            {
                "ts_code": "600000.SH",
                "ann_date": "20260827",
                "end_date": "20260630",
                "roe_waa": 99,
                "update_flag": "1",
            },
            {
                "ts_code": "TS0018.SH",
                "ann_date": "20260815",
                "end_date": "20260630",
                "roe_waa": 99,
                "update_flag": "1",
            },
        ],
    }
    financials["financials"].append(dict(financials["financials"][0]))
    periods = (
        "20260630",
        "20260331",
        "20251231",
        "20250930",
        "20250630",
        "20250331",
        "20241231",
        "20240930",
    )
    financial_slices = {
        period: {
            "trade_date": TRADE_DATE.isoformat(),
            "period": period,
            "request_id": f"financials-{period}-request",
            "financials": financials["financials"] if period == "20260630" else [],
        }
        for period in periods
    }
    router = _Router(
        {
            "stock_selection_calendar": calendar,
            "stock_selection_daily": lambda kwargs: daily_slices[kwargs["session_date"]],
            "stock_selection_daily_basic": daily_basic,
            "stock_selection_master": master,
            "stock_selection_financial_period": lambda kwargs: financial_slices[
                kwargs["period"]
            ],
        }
    )

    snapshot = fetch_daily_stock_factor_snapshot(
        TRADE_DATE,
        router=router,
        now=datetime(2026, 8, 26, 18, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    conflict_row = {
        **financials["financials"][0],
        "roe_waa": 88,
    }
    conflict_slices = {
        **financial_slices,
        "20260630": {
            **financial_slices["20260630"],
            "financials": [*financials["financials"], conflict_row],
        },
    }
    conflict_router = _Router(
        {
            "stock_selection_calendar": calendar,
            "stock_selection_daily": lambda kwargs: daily_slices[
                kwargs["session_date"]
            ],
            "stock_selection_daily_basic": daily_basic,
            "stock_selection_master": master,
            "stock_selection_financial_period": lambda kwargs: conflict_slices[
                kwargs["period"]
            ],
        }
    )
    with pytest.raises(RuntimeError, match="conflicted financial row"):
        fetch_daily_stock_factor_snapshot(
            TRADE_DATE,
            router=conflict_router,
            now=datetime(2026, 8, 26, 18, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

    invalid_prices = {
        **daily,
        "daily": [_bar("TS0018.SH", COMPACT_DATE, 10.0)],
    }
    invalid_daily_slices = {**daily_slices, COMPACT_DATE: invalid_prices}
    invalid_router = _Router(
        {
            "stock_selection_calendar": calendar,
            "stock_selection_daily": lambda kwargs: invalid_daily_slices[
                kwargs["session_date"]
            ],
            "stock_selection_daily_basic": daily_basic,
            "stock_selection_master": master,
            "stock_selection_financial_period": lambda kwargs: financial_slices[
                kwargs["period"]
            ],
        }
    )
    with pytest.raises(ValueError, match="invalid A-share symbol"):
        fetch_daily_stock_factor_snapshot(
            TRADE_DATE,
            router=invalid_router,
            now=datetime(2026, 8, 26, 18, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

    assert router.calls[:6] == [
        ("stock_selection_calendar", {"trade_date": COMPACT_DATE}),
        (
            "stock_selection_daily",
            {"trade_date": COMPACT_DATE, "session_date": COMPACT_DATE},
        ),
        ("stock_selection_daily_basic", {"trade_date": COMPACT_DATE}),
        (
            "stock_selection_daily",
            {"trade_date": COMPACT_DATE, "session_date": prior_20},
        ),
        (
            "stock_selection_daily",
            {"trade_date": COMPACT_DATE, "session_date": prior_60},
        ),
        ("stock_selection_master", {"trade_date": COMPACT_DATE}),
    ]
    assert {
        (data_type, kwargs["trade_date"], kwargs["session_date"])
        for data_type, kwargs in router.calls[6:20]
    } == {
        ("stock_selection_daily", COMPACT_DATE, compact)
        for compact in candlestick_compact_dates
        if compact != COMPACT_DATE
    }
    assert {
        (data_type, kwargs["trade_date"], kwargs["period"])
        for data_type, kwargs in router.calls[20:]
    } == {
        ("stock_selection_financial_period", COMPACT_DATE, period)
        for period in periods
    }
    assert snapshot.metadata.contract == "daily_stock_factor_snapshot.v1"
    assert snapshot.metadata.quality.value == "accepted"
    assert snapshot.metadata.provider_request_id == (
        "calendar:calendar-request|daily:daily-request|"
        "daily_basic:daily-basic-request|daily_20d:daily-20d-request|"
        "daily_60d:daily-60d-request|"
        "master:master-request|"
        + "|".join(
            f"daily_window:{value.isoformat()}:daily-window-{compact}-request"
            for value, compact in zip(
                candlestick_dates,
                candlestick_compact_dates,
                strict=True,
            )
            if compact != COMPACT_DATE
        )
        + "|"
        + "|".join(
            f"financials:{period}:financials-{period}-request"
            for period in periods
        )
    )
    assert snapshot.factors[1].amount_cny == 250_000_000
    assert snapshot.factors[1].total_market_cap_cny == 5_000_000_000
    assert snapshot.factors[1].roe_pct == 8
    assert snapshot.factors[1].financial_announcement_date == date(2026, 8, 15)
    assert snapshot.factors[1].momentum_20d_pct == pytest.approx((10 / 9 - 1) * 100)
    assert snapshot.coverage.candlestick_history_count == 2
    assert len(snapshot.candlestick_histories[0].bars) == 15
    assert snapshot.candlestick_histories[0].bars[0].previous_close == pytest.approx(11.4)
