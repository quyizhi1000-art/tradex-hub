from __future__ import annotations

import pytest

from astock_signals.smart_router import SmartRouter, SourceCapabilityError
from tradex.data_sources import biying_fetchers


def test_quote_uses_raw_share_volume_and_provider_timestamp(monkeypatch):
    monkeypatch.setattr(
        biying_fetchers,
        "_request",
        lambda path, params=None: {
            "p": 11.2,
            "o": 11.0,
            "h": 11.3,
            "l": 10.9,
            "yc": 11.1,
            "cje": 123_000_000,
            "v": 1000,
            "pv": 100_000,
            "pc": 0.9,
            "t": "2026-08-20 10:30:00",
        },
    )

    frame = biying_fetchers.fetch_realtime_quote(symbol="000001")

    assert frame.loc[0, "代码"] == "000001"
    assert frame.loc[0, "成交量"] == 100_000
    assert frame.loc[0, "成交额"] == 123_000_000
    assert frame.attrs["provider_as_of"] == "2026-08-20T10:30:00+08:00"


def test_quote_falls_back_to_lots_when_raw_share_volume_is_absent(monkeypatch):
    monkeypatch.setattr(
        biying_fetchers,
        "_request",
        lambda path, params=None: {"p": 11.2, "v": 1234, "t": "2026-08-20 10:30:00"},
    )

    frame = biying_fetchers.fetch_realtime_quote(symbol="920547.BJ")

    assert frame.loc[0, "成交量"] == 123_400


def test_market_overview_fetches_the_four_decision_roles_and_legacy_shenzhen(monkeypatch):
    requested = []

    def fake_index_quote(symbol):
        requested.append(symbol)
        return {
            "代码": symbol,
            "指数名称": biying_fetchers._INDEX_NAMES[symbol],
            "最新价": 1000,
            "涨跌幅": 0.1,
            "更新时间": "2026-08-20T10:30:00+08:00",
        }

    monkeypatch.setattr(biying_fetchers, "_index_quote", fake_index_quote)

    frame = biying_fetchers.fetch_market_overview()

    assert requested == [
        "000001.SH",
        "399001.SZ",
        "000300.SH",
        "399852.SZ",
        "399006.SZ",
    ]
    assert set(frame["指数名称"]) >= {"上证指数", "沪深300", "中证1000", "创业板指"}


def test_history_converts_lots_to_shares_and_declares_contract_basis(monkeypatch):
    monkeypatch.setattr(
        biying_fetchers,
        "_request",
        lambda path, params=None: [
            {
                "t": "2026-08-19 00:00:00",
                "o": 10,
                "h": 12,
                "l": 9,
                "c": 11,
                "v": 500,
                "a": 550_000,
                "pc": 10,
                "sf": 0,
            }
        ],
    )

    frame = biying_fetchers.fetch_historical_kline(
        symbol="600519", period="daily", adjust="qfq", count=1
    )

    assert frame.loc[0, "成交量"] == 50_000
    assert frame.loc[0, "涨跌额"] == 1
    assert frame.loc[0, "涨跌幅"] == 10
    assert frame.attrs["period"] == "daily"
    assert frame.attrs["adjust"] == "forward"


def test_financial_mapper_preserves_raw_fields_and_adds_existing_contract_names(monkeypatch):
    monkeypatch.setattr(
        biying_fetchers,
        "_request",
        lambda path, params=None: [
            {"jzrq": "20260630", "yyzsr": 100_000_000, "jlr": 12_000_000}
        ],
    )

    frame = biying_fetchers.fetch_financial_stmt(
        endpoint="profit", symbol="SZ000001"
    )

    assert frame.loc[0, "REPORT_DATE_NAME"] == "20260630"
    assert frame.loc[0, "TOTAL_OPERATE_INCOME"] == 100_000_000
    assert frame.loc[0, "NETPROFIT"] == 12_000_000
    assert frame.loc[0, "yyzsr"] == 100_000_000


def test_suffix_symbols_preserve_explicit_exchange():
    assert biying_fetchers._market_symbol("000001.SZ") == "000001.SZ"
    assert biying_fetchers._market_symbol("000001.SH") == "000001.SH"
    assert biying_fetchers._market_symbol("920547.BJ") == "920547.BJ"
    assert biying_fetchers._market_symbol("920547") == "920547.BJ"


def test_bse_financial_statement_uses_bse_endpoint(monkeypatch):
    calls = []

    def fake_request(path, params=None):
        calls.append(path)
        return [{"jzrq": "20260630", "yyzsr": 10}]

    monkeypatch.setattr(biying_fetchers, "_request", fake_request)

    biying_fetchers.fetch_financial_stmt(endpoint="profit", symbol="920547.BJ")

    assert calls == [["bj", "financial", "income", "920547.BJ"]]


def test_lockup_converts_ten_thousand_shares_without_mislabeling_value(monkeypatch):
    monkeypatch.setattr(
        biying_fetchers,
        "_request",
        lambda path, params=None: [
            {
                "rdate": "2026-09-01",
                "batch": "首发原股东限售股份",
                "ramount": 12.5,
                "rprice": 1.7,
                "pdate": "2026-08-01",
            }
        ],
    )

    result = biying_fetchers.fetch_lockup_expiry(
        symbol="600519", trade_date="2026-08-20", forward_days=30
    )

    assert result["upcoming"][0]["shares"] == 125_000
    assert result["upcoming"][0]["market_value_100m_cny"] == 1.7
    assert "price" not in result["upcoming"][0]


def test_non_equivalent_subcapabilities_fall_through_without_health_penalty():
    with pytest.raises(SourceCapabilityError):
        biying_fetchers.fetch_financial_stmt(endpoint="segments", symbol="000001")
    with pytest.raises(SourceCapabilityError):
        biying_fetchers.fetch_valuation(endpoint="rank_forecast")
    with pytest.raises(SourceCapabilityError):
        biying_fetchers.fetch_limit_up_board(board_type="sentiment")


def test_non_equivalent_subcapability_uses_fallback_without_health_penalty():
    expected = object()
    router = SmartRouter()
    router.register("financial_stmt", "biying", biying_fetchers.fetch_financial_stmt, priority=1)
    router.register("financial_stmt", "fallback", lambda **kwargs: expected, priority=2)

    result, provider = router.route(
        "financial_stmt", endpoint="segments", symbol="600519"
    )

    assert result is expected
    assert provider == "fallback"
    health = {item["source"]: item for item in router.get_health_report()}
    assert health["financial_stmt:biying"]["fail_count"] == 0


def test_limit_up_pool_keeps_outer_tool_contract(monkeypatch):
    monkeypatch.setattr(
        biying_fetchers,
        "_request",
        lambda path, params=None: [
            {"dm": "600000", "mc": "浦发银行", "p": 10, "zf": 10, "lbc": 2}
        ],
    )

    result = biying_fetchers.fetch_limit_up_board(board_type="zt")

    assert result["source"] == "biying"
    assert result["count"] == 1
    assert result["data"][0]["code"] == "600000"
    assert result["data"][0]["board_count"] == 2


def test_concept_attribution_preserves_structured_outer_contract(monkeypatch):
    monkeypatch.setattr(
        biying_fetchers,
        "_request",
        lambda path, params=None: [
            {"code": "sw_yx", "name": "A股-申万行业-银行"},
            {"code": "gn_rzrq", "name": "A股-概念板块-融资融券"},
            {"code": "dy_bj", "name": "A股-地域板块-北京"},
        ],
    )

    result = biying_fetchers.fetch_concept_attribution(symbol="000001")

    assert result["source"] == "biying"
    assert result["industries"] == [{"code": "sw_yx", "name": "银行"}]
    assert result["concepts"] == [{"code": "gn_rzrq", "name": "融资融券"}]
    assert result["regions"] == [{"code": "dy_bj", "name": "北京"}]
