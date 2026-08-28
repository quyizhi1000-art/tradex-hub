from __future__ import annotations

import pytest

from tradex.data_sources import tushare_fetchers
from tradex.data_sources.tushare_client import TushareResult


def _result(*records, request_id: str = "request-1") -> TushareResult:
    return TushareResult(records=tuple(records), request_id=request_id)


def _bar(
    trade_date: str,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
    pre_close: float,
    vol: float,
    amount: float,
):
    return {
        "ts_code": "000001.SZ",
        "trade_date": trade_date,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "pre_close": pre_close,
        "change": close - pre_close,
        "pct_chg": (close / pre_close - 1) * 100,
        "vol": vol,
        "amount": amount,
    }


def test_realtime_quote_keeps_documented_rt_k_share_and_yuan_units(monkeypatch):
    calls = []

    def fake_request(api_name, params=None, fields=None):
        calls.append((api_name, params, fields))
        return _result(
            {
                "ts_code": "000001.SZ",
                "name": "平安银行",
                "pre_close": 11.20,
                "high": 11.42,
                "open": 11.25,
                "low": 11.18,
                "close": 11.36,
                "vol": 12_345_678,
                "amount": 140_246_908,
                "trade_time": "2026-08-21 10:30:01",
            }
        )

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    frame = tushare_fetchers.fetch_realtime_quote(code="000001")

    assert calls[0][0] == "rt_k"
    assert calls[0][1] == {"ts_code": "000001.SZ"}
    assert frame.loc[0, "代码"] == "000001"
    assert frame.loc[0, "名称"] == "平安银行"
    assert frame.loc[0, "最新价"] == 11.36
    assert frame.loc[0, "成交量"] == 12_345_678
    assert frame.loc[0, "成交额"] == 140_246_908
    assert frame.loc[0, "涨跌额"] == pytest.approx(0.16)
    assert frame.loc[0, "涨跌幅"] == pytest.approx((11.36 / 11.20 - 1) * 100)
    assert frame.attrs == {
        "source_valid": True,
        "provider_as_of": "2026-08-21T10:30:01+08:00",
        "request_id": "request-1",
    }


def test_realtime_quote_normalizes_compatible_proxy_lots_to_shares(monkeypatch):
    def fake_request(api_name, params=None, fields=None):
        return _result(
            {
                "ts_code": "000001.SZ",
                "name": "平安银行",
                "pre_close": 11.40,
                "high": 11.46,
                "open": 11.36,
                "low": 11.32,
                "close": 11.41,
                "vol": 869_128,
                "amount": 990_112_100,
                "trade_time": "2026-08-21 15:00:00",
            }
        )

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    frame = tushare_fetchers.fetch_realtime_quote(code="000001")

    assert frame.loc[0, "成交量"] == 86_912_800
    assert frame.loc[0, "成交额"] == 990_112_100
    implied_average = frame.loc[0, "成交额"] / frame.loc[0, "成交量"]
    assert 11.32 <= implied_average <= 11.46


def test_realtime_quote_rejects_ambiguous_volume_units(monkeypatch):
    def fake_request(api_name, params=None, fields=None):
        return _result(
            {
                "ts_code": "000001.SZ",
                "name": "平安银行",
                "pre_close": 11.40,
                "high": 11.46,
                "open": 11.36,
                "low": 11.32,
                "close": 11.41,
                "vol": 869_128,
                # Neither shares nor lots can reconcile this amount with prices.
                "amount": 1_000,
                "trade_time": "2026-08-21 15:00:00",
            }
        )

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    with pytest.raises(RuntimeError, match="成交量单位无法唯一判定"):
        tushare_fetchers.fetch_realtime_quote(code="000001")


@pytest.mark.parametrize(
    ("period", "api_name", "period_attr"),
    [
        ("daily", "daily", "daily"),
        ("weekly", "weekly", "weekly"),
        ("monthly", "monthly", "monthly"),
    ],
)
def test_unadjusted_history_maps_period_bounds_count_and_units(
    monkeypatch, period, api_name, period_attr
):
    calls = []

    def fake_request(name, params=None, fields=None):
        calls.append((name, params, fields))
        return _result(
            _bar(
                "20260821",
                open_price=10,
                high=12,
                low=9,
                close=11,
                pre_close=10,
                vol=1234.5,
                amount=5678.25,
            )
        )

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    frame = tushare_fetchers.fetch_historical_kline(
        symbol="000001.SZ",
        period=period,
        start="2026-08-01",
        end="20260821",
        count=7,
        adjust="none",
    )

    assert len(calls) == 1
    assert calls[0][0] == api_name
    assert calls[0][1] == {
        "ts_code": "000001.SZ",
        "start_date": "20260801",
        "end_date": "20260821",
        "limit": 7,
    }
    assert frame.loc[0, "日期"] == "2026-08-21"
    assert frame.loc[0, "成交量"] == 123_450
    assert frame.loc[0, "成交额"] == 5_678_250
    assert frame.attrs == {
        "source_valid": True,
        "period": period_attr,
        "adjust": "none",
        "provider_as_of": "2026-08-21T15:00:00+08:00",
        "request_id": "request-1",
    }


@pytest.mark.parametrize(
    ("adjust", "expected_old_close", "expected_new_close", "expected_basis"),
    [
        ("qfq", 5.0, 20.0, "forward"),
        ("hfq", 10.0, 40.0, "backward"),
    ],
)
def test_adjusted_history_uses_adj_factor_official_formula(
    monkeypatch, adjust, expected_old_close, expected_new_close, expected_basis
):
    calls = []
    bars = _result(
        _bar(
            "20240103",
            open_price=19,
            high=21,
            low=18,
            close=20,
            pre_close=18,
            vol=200,
            amount=300,
        ),
        _bar(
            "20240102",
            open_price=9,
            high=11,
            low=8,
            close=10,
            pre_close=8,
            vol=100,
            amount=150,
        ),
        request_id="bars-request",
    )
    factors = _result(
        {"ts_code": "000001.SZ", "trade_date": "20240103", "adj_factor": 2.0},
        {"ts_code": "000001.SZ", "trade_date": "20240102", "adj_factor": 1.0},
        request_id="factor-request",
    )

    def fake_request(name, params=None, fields=None):
        calls.append((name, params, fields))
        return factors if name == "adj_factor" else bars

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    frame = tushare_fetchers.fetch_historical_kline(
        code="000001", period="day", adjust=adjust, count=2
    )

    assert [call[0] for call in calls] == ["daily", "adj_factor"]
    assert calls[1][1] == {
        "ts_code": "000001.SZ",
        "start_date": "20240102",
        "end_date": "20240103",
    }
    assert frame["日期"].tolist() == ["2024-01-02", "2024-01-03"]
    assert frame["收盘"].tolist() == [expected_old_close, expected_new_close]
    assert frame["成交量"].tolist() == [10_000, 20_000]
    assert frame["成交额"].tolist() == [150_000, 300_000]
    assert frame.attrs["adjust"] == expected_basis
    assert frame.attrs["request_id"] == "bars-request"


def test_adjusted_history_fails_closed_when_factor_is_missing(monkeypatch):
    bars = _result(
        _bar(
            "20240103",
            open_price=19,
            high=21,
            low=18,
            close=20,
            pre_close=18,
            vol=200,
            amount=300,
        )
    )
    factors = _result()
    monkeypatch.setattr(
        tushare_fetchers,
        "_request",
        lambda name, params=None, fields=None: factors if name == "adj_factor" else bars,
    )

    with pytest.raises(RuntimeError, match="复权因子"):
        tushare_fetchers.fetch_historical_kline(code="000001", adjust="qfq")


def test_monthly_adjustment_can_use_factor_before_non_trading_period_end(monkeypatch):
    calls = []
    bars = _result(
        _bar(
            "20240630",
            open_price=9,
            high=11,
            low=8,
            close=10,
            pre_close=8,
            vol=100,
            amount=150,
        )
    )
    factors = _result(
        {"ts_code": "000001.SZ", "trade_date": "20240628", "adj_factor": 2.0}
    )

    def fake_request(name, params=None, fields=None):
        calls.append((name, params, fields))
        return factors if name == "adj_factor" else bars

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    frame = tushare_fetchers.fetch_historical_kline(
        code="000001", period="monthly", adjust="qfq", count=1
    )

    assert calls[1][1] == {
        "ts_code": "000001.SZ",
        "start_date": "20240521",
        "end_date": "20240630",
    }
    assert frame.loc[0, "收盘"] == 10


def test_realtime_minutes_use_multi_symbol_capable_endpoint_and_documented_units(
    monkeypatch,
):
    calls = []

    def fake_request(name, params=None, fields=None):
        calls.append((name, params, fields))
        return _result(
            {
                "code": "000001.SZ",
                "time": "2026-08-24 09:31:00",
                "open": 10.10,
                "close": 10.16,
                "high": 10.20,
                "low": 10.08,
                "vol": 20_000,
                "amount": 203_000,
            },
            {
                "code": "000001.SZ",
                "time": "2026-08-24 09:30:00",
                "open": 10.00,
                "close": 10.08,
                "high": 10.10,
                "low": 9.98,
                "vol": 10_000,
                "amount": 100_500,
            },
        )

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    frame = tushare_fetchers.fetch_minute_data(symbol="000001")

    assert calls[0][0] == "rt_min"
    assert calls[0][1] == {"ts_code": "000001.SZ", "freq": "1MIN"}
    assert frame["时间"].tolist() == [
        "2026-08-24T09:30:00+08:00",
        "2026-08-24T09:31:00+08:00",
    ]
    assert frame["成交量"].sum() == 30_000
    assert frame["成交额"].sum() == 303_500
    assert frame.attrs == {
        "source_valid": True,
        "trading_date": "2026-08-24",
        "frequency_minutes": 1,
        "volume_unit": "shares",
        "amount_unit": "CNY",
        "volume_unit_inferred_rows": 0,
        "amount_invalid_rows": 0,
        "provider_as_of": "2026-08-24T09:31:00+08:00",
        "request_id": "request-1",
    }


def test_realtime_minutes_batch_multiple_stocks_in_one_provider_request(monkeypatch):
    calls = []

    def fake_request(name, params=None, fields=None):
        calls.append((name, params, fields))
        records = [
            {
                "ts_code": code,
                "time": f"2026-08-24 09:{minute:02d}:00",
                "open": price,
                "close": price,
                "high": price,
                "low": price,
                "vol": 10_000,
                "amount": price * 10_000,
            }
            for code, price in (("000001.SZ", 10.0), ("600000.SH", 12.0))
            for minute in (30, 31)
        ]
        return _result(*records)

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    frame = tushare_fetchers.fetch_minute_data_batch(
        symbols=("000001.SZ", "600000.SH")
    )

    assert calls[0][0] == "rt_min"
    assert calls[0][1] == {
        "ts_code": "000001.SZ,600000.SH",
        "freq": "1MIN",
    }
    assert frame.groupby("代码").size().to_dict() == {
        "000001.SZ": 2,
        "600000.SH": 2,
    }
    assert frame.attrs["request_id"] == "request-1"
    assert frame.attrs["provider_as_of"] == "2026-08-24T09:31:00+08:00"


def test_realtime_partial_batch_exposes_omission_without_fabricating_rows(monkeypatch):
    monkeypatch.setattr(
        tushare_fetchers,
        "_request",
        lambda name, params=None, fields=None: _result(
            {
                "ts_code": "000001.SZ",
                "time": "2026-08-24 09:30:00",
                "open": 10.0,
                "close": 10.0,
                "high": 10.0,
                "low": 10.0,
                "vol": 10_000,
                "amount": 100_000,
            }
        ),
    )

    frame = tushare_fetchers.fetch_minute_data_batch_partial(
        symbols=("000001.SZ", "600000.SH")
    )

    assert frame["代码"].unique().tolist() == ["000001.SZ"]


def test_realtime_batch_rejects_unverified_41_symbol_request() -> None:
    with pytest.raises(ValueError, match="at most 40"):
        tushare_fetchers.fetch_minute_data_batch(
            symbols=tuple(f"{number:06d}.SZ" for number in range(1, 42))
        )


def test_realtime_minutes_normalize_compatible_proxy_lots_to_shares(monkeypatch):
    monkeypatch.setattr(
        tushare_fetchers,
        "_request",
        lambda name, params=None, fields=None: _result(
            {
                "ts_code": "000001.SZ",
                "time": "2026-08-24 09:30:00",
                "open": 10.00,
                "close": 10.08,
                "high": 10.10,
                "low": 9.98,
                "vol": 100,
                "amount": 100_500,
            }
        ),
    )

    frame = tushare_fetchers.fetch_minute_data(code="000001")

    assert frame.loc[0, "成交量"] == 10_000
    assert frame.loc[0, "成交额"] == 100_500


def test_realtime_minutes_use_full_series_evidence_for_one_ambiguous_row(
    monkeypatch,
):
    records = [
        {
            "ts_code": "000001.SZ",
            "time": f"2026-08-24 09:{minute:02d}:00",
            "open": 10.0,
            "close": 10.0,
            "high": 10.01,
            "low": 9.99,
            "vol": 1_000,
            "amount": 10_000,
        }
        for minute in range(30, 50)
    ]
    records.append(
        {
            "ts_code": "000001.SZ",
            "time": "2026-08-24 09:50:00",
            "open": 50.0,
            "close": 50.0,
            "high": 100.0,
            "low": 1.0,
            "vol": 100,
            "amount": 10_000,
        }
    )
    monkeypatch.setattr(
        tushare_fetchers,
        "_request",
        lambda name, params=None, fields=None: _result(*records),
    )

    frame = tushare_fetchers.fetch_minute_data(code="000001")

    assert frame.loc[20, "成交量"] == 100
    assert frame.attrs["volume_unit_inferred_rows"] == 1


def test_realtime_minutes_drop_only_inconsistent_amount_field(monkeypatch):
    monkeypatch.setattr(
        tushare_fetchers,
        "_request",
        lambda name, params=None, fields=None: _result(
            {
                "ts_code": "688019.SH",
                "time": "2026-08-24 13:15:00",
                "open": 248.10,
                "close": 248.20,
                "high": 248.48,
                "low": 248.00,
                "vol": 5_000,
                "amount": 1_235_417,
            }
        ),
    )

    frame = tushare_fetchers.fetch_minute_data(code="688019")

    assert frame.loc[0, "收盘"] == 248.20
    assert frame.loc[0, "成交量"] == 5_000
    assert frame.loc[0, "成交额"] is None
    assert frame.attrs["amount_invalid_rows"] == 1


def test_realtime_minutes_tolerate_binary_float_price_noise(monkeypatch):
    monkeypatch.setattr(
        tushare_fetchers,
        "_request",
        lambda name, params=None, fields=None: _result(
            {
                "ts_code": "000001.SZ",
                "time": "2026-08-24 15:00:00",
                "open": 11.56,
                "close": 11.559999999999995,
                "high": 11.559999999999995,
                "low": 11.56,
                "vol": 10_000,
                "amount": 115_600,
            }
        ),
    )

    frame = tushare_fetchers.fetch_minute_data(code="000001")

    assert frame.loc[0, "成交量"] == 10_000


def test_realtime_minutes_reject_ambiguous_volume_units(monkeypatch):
    monkeypatch.setattr(
        tushare_fetchers,
        "_request",
        lambda name, params=None, fields=None: _result(
            {
                "ts_code": "000001.SZ",
                "time": "2026-08-24 09:30:00",
                "open": 50,
                "close": 50,
                "high": 100,
                "low": 1,
                "vol": 100,
                "amount": 10_000,
            }
        ),
    )

    with pytest.raises(RuntimeError, match="成交量单位无法唯一判定"):
        tushare_fetchers.fetch_minute_data(code="000001")


def test_realtime_minutes_reject_wrong_instrument(monkeypatch):
    monkeypatch.setattr(
        tushare_fetchers,
        "_request",
        lambda name, params=None, fields=None: _result(
            {
                "ts_code": "000002.SZ",
                "time": "2026-08-24 09:30:00",
                "open": 10,
                "close": 10,
                "high": 10,
                "low": 10,
                "vol": 100,
                "amount": 1_000,
            }
        ),
    )

    with pytest.raises(RuntimeError, match="未返回请求的证券"):
        tushare_fetchers.fetch_minute_data(code="000001")


def test_auction_selects_latest_final_record_and_maps_existing_contract(monkeypatch):
    calls = []

    def fake_request(name, params=None, fields=None):
        calls.append((name, params, fields))
        return _result(
            {
                "ts_code": "000001.SZ",
                "trade_date": "20260820",
                "vol": 100_000,
                "price": 11.20,
                "amount": 1_120_000,
                "pre_close": 11.10,
                "turnover_rate": 0.003,
                "volume_ratio": 0.1,
                "float_share": 1_940_591.8198,
            },
            {
                "ts_code": "000001.SZ",
                "trade_date": "20260821",
                "vol": 304_900,
                "price": 11.36,
                "amount": 3_463_664,
                "pre_close": 11.29,
                "turnover_rate": 0.004,
                "volume_ratio": 0.2,
                "float_share": 1_940_591.8198,
            },
        )

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    frame = tushare_fetchers.fetch_auction_data(
        symbol="000001", start="20260820", end="20260821", count=2
    )

    assert calls[0][0] == "stk_auction"
    assert calls[0][1] == {
        "ts_code": "000001.SZ",
        "start_date": "20260820",
        "end_date": "20260821",
        "limit": 2,
    }
    assert frame.to_dict("records") == [
        {
            "代码": "000001.SZ",
            "交易日期": "20260821",
            "开盘价": 11.36,
            "开盘量": 304_900,
            "开盘额": 3_463_664,
            "开盘涨跌幅": pytest.approx((11.36 / 11.29 - 1) * 100),
            "昨收": 11.29,
            "换手率": 0.004,
            "量比": 0.2,
            "流通股本": pytest.approx(19_405_918_198),
        }
    ]
    assert frame.attrs == {
        "source_valid": True,
        "provider_as_of": "2026-08-21T09:25:00+08:00",
        "request_id": "request-1",
    }


def test_auction_defaults_to_one_bounded_current_record(monkeypatch):
    calls = []

    def fake_request(name, params=None, fields=None):
        calls.append((name, params, fields))
        return _result(
            {
                "ts_code": "600519.SH",
                "trade_date": "20260821",
                "vol": 100,
                "price": 1500,
                "amount": 150_000,
                "pre_close": 1490,
                "turnover_rate": 0.001,
                "volume_ratio": 0.1,
                "float_share": 125_619,
            }
        )

    monkeypatch.setattr(tushare_fetchers, "_request", fake_request)

    tushare_fetchers.fetch_auction_data(code="600519")

    assert calls[0][0] == "stk_auction"
    assert calls[0][1] == {"ts_code": "600519.SH", "limit": 1}


@pytest.mark.parametrize("symbol", ["", "abc", "000001.HK"])
def test_fetchers_reject_missing_or_non_a_share_symbols(symbol):
    with pytest.raises(ValueError):
        tushare_fetchers.fetch_realtime_quote(symbol=symbol)
