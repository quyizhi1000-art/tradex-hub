from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from astock_signals.smart_router import SourceCapabilityError

from tradex.data_sources import (
    akshare_fetchers,
    astock_signals_fetchers,
    fuyao_fetchers,
)


_VALUATION_PATH = "/api/a-share/valuations/snapshot"
_INDEX_CATALOG_PATH = "/api/a-share-index/catalog/ths-index-list"
_INDEX_CONSTITUENTS_PATH = "/api/a-share-index/constituents/ths-stock-list"
_LIMIT_UP_LADDER_PATH = "/api/a-share/special-data/limit-up-ladder"
_STOCK_ANOMALY_PATH = "/api/a-share/special-data/anomaly-analysis-stock"
_LIMIT_UP_POOL_PATH = "/api/a-share/special-data/limit-up-pool"
_LIMIT_DOWN_POOL_PATH = "/api/a-share/special-data/limit-down-pool"
_LIMIT_BREAK_POOL_PATH = "/api/a-share/special-data/limit-break-pool"
_DRAGON_TIGER_PATH = "/api/a-share/special-data/dragon-tiger-list"

_TIMESTAMP = 1_784_275_991_000
_LADDER_KEYS = (
    "two_board",
    "three_board",
    "four_board",
    "five_board",
    "six_board",
    "seven_over",
)


def _empty_boards() -> dict[str, list[dict]]:
    return {key: [] for key in _LADDER_KEYS}


def _pool_payload(items, *, page=1, size=200, total=None, pages=1):
    if total is None:
        total = len(items)
    return {
        "timestamp": _TIMESTAMP,
        "pagination": {
            "total": total,
            "pages": pages,
            "size": size,
            "page": page,
        },
        "item": items,
    }


def _limit_up_item(ticker="600519"):
    return {
        "thscode": f"{ticker}.SH",
        "ticker": ticker,
        "name": "贵州茅台",
        "is_st": False,
        "is_new": False,
        "last_price": 1600.0,
        "price_change_ratio_pct": 10.0,
        "limit_up_time": "14:55:00",
        "limit_up_reason": "业绩增长",
        "continue_day_text": "2 连板",
        "continue_day_cnt": 2,
        "seal_money": 100_000_000.0,
        "max_seal_money": 120_000_000.0,
    }


def _limit_down_item(ticker="000001"):
    return {
        "thscode": f"{ticker}.SZ",
        "ticker": ticker,
        "name": "平安银行",
        "last_price": 9.0,
        "price_change_ratio_pct": -10.0,
        "first_limit_time": "10:00:00",
        "last_limit_time": "14:50:00",
        "turnover_ratio_pct": 5.2,
    }


def _limit_break_item(ticker="300750"):
    return {
        "thscode": f"{ticker}.SZ",
        "ticker": ticker,
        "name": "宁德时代",
        "last_price": 210.0,
        "price_change_ratio_pct": 8.5,
        "open_times": 3,
        "turnover_ratio_pct": 7.8,
        "turnover": 5_000_000_000.0,
    }


def test_valuation_snapshot_maps_batch_request_fields_and_metadata(monkeypatch):
    calls = []

    def fake_request(path, params=None):
        calls.append((path, params))
        return {
            "timestamp": _TIMESTAMP,
            "total": 2,
            "item": [
                {
                    "thscode": "600519.SH",
                    "ticker": "600519",
                    "name": "贵州茅台",
                    "pe_ttm": 20.1,
                    "pe_mrq": 20.5,
                    "pb_mrq": 7.2,
                    "ps_ttm": 9.8,
                    "pcf_ttm": None,
                },
                {
                    "thscode": "000001.SZ",
                    "ticker": "000001",
                    "name": "平安银行",
                    "pe_ttm": 5.2,
                    "pe_mrq": 5.4,
                    "pb_mrq": 0.6,
                    "ps_ttm": 1.1,
                    "pcf_ttm": 4.3,
                },
            ],
        }

    monkeypatch.setattr(fuyao_fetchers, "_request", fake_request)

    result = fuyao_fetchers.fetch_valuation_snapshot(
        "600519, 000001.sz,600519.SH"
    )

    assert calls == [
        (
            _VALUATION_PATH,
            {"thscodes": "600519.SH,000001.SZ"},
        )
    ]
    assert isinstance(result, pd.DataFrame)
    records = result.to_dict("records")
    assert {
        key: value for key, value in records[0].items() if key != "市现率TTM"
    } == {
        "同花顺代码": "600519.SH",
        "代码": "600519",
        "名称": "贵州茅台",
        "市盈率TTM": 20.1,
        "市盈率MRQ": 20.5,
        "市净率MRQ": 7.2,
        "市销率TTM": 9.8,
        "更新时间": result.attrs["provider_as_of"],
    }
    assert pd.isna(records[0]["市现率TTM"])
    assert records[1] == {
        "同花顺代码": "000001.SZ",
        "代码": "000001",
        "名称": "平安银行",
        "市盈率TTM": 5.2,
        "市盈率MRQ": 5.4,
        "市净率MRQ": 0.6,
        "市销率TTM": 1.1,
        "市现率TTM": 4.3,
        "更新时间": result.attrs["provider_as_of"],
    }
    assert result.attrs["total"] == 2
    assert result.attrs["requested_total"] == 2
    assert result.attrs["source_valid"] is True


def test_valuation_snapshot_accepts_documented_empty_result(monkeypatch):
    monkeypatch.setattr(
        fuyao_fetchers,
        "_request",
        lambda *args, **kwargs: {"timestamp": None, "total": 0, "item": []},
    )

    result = fuyao_fetchers.fetch_valuation_snapshot("600519")

    assert result.empty
    assert list(result.columns) == [
        "同花顺代码",
        "代码",
        "名称",
        "市盈率TTM",
        "市盈率MRQ",
        "市净率MRQ",
        "市销率TTM",
        "市现率TTM",
        "更新时间",
    ]
    assert result.attrs["total"] == 0
    assert result.attrs["requested_total"] == 1
    assert result.attrs["source_valid"] is True


def test_ths_index_catalog_maps_tag_and_preserves_ti_codes(monkeypatch):
    calls = []

    def fake_request(path, params=None):
        calls.append((path, params))
        return {
            "timestamp": _TIMESTAMP,
            "item": [
                {"thscode": "886042.TI", "name": "低空经济"},
                {"thscode": "881101.TI", "name": "种植业"},
            ],
        }

    monkeypatch.setattr(fuyao_fetchers, "_request", fake_request)

    result = fuyao_fetchers.fetch_ths_index_catalog("Industry")

    assert calls == [(_INDEX_CATALOG_PATH, {"tag": "industry"})]
    assert result[["同花顺指数代码", "名称", "标签"]].to_dict("records") == [
        {"同花顺指数代码": "886042.TI", "名称": "低空经济", "标签": "industry"},
        {"同花顺指数代码": "881101.TI", "名称": "种植业", "标签": "industry"},
    ]
    assert result.attrs["tag"] == "industry"
    assert result.attrs["total"] == 2


def test_ths_index_catalog_accepts_empty_result_and_rejects_unknown_tag(monkeypatch):
    calls = []

    def fake_request(path, params=None):
        calls.append((path, params))
        return {"timestamp": _TIMESTAMP, "item": []}

    monkeypatch.setattr(fuyao_fetchers, "_request", fake_request)

    result = fuyao_fetchers.fetch_ths_index_catalog()

    assert result.empty
    assert calls == [(_INDEX_CATALOG_PATH, {"tag": "cn_concept"})]
    assert result.attrs["total"] == 0
    with pytest.raises(ValueError, match="cn_concept"):
        fuyao_fetchers.fetch_ths_index_catalog("unsupported")


def test_ths_index_constituents_maps_request_and_stock_fields(monkeypatch):
    calls = []

    def fake_request(path, params=None):
        calls.append((path, params))
        return {
            "timestamp": _TIMESTAMP,
            "item": [
                {
                    "thscode": "600519.SH",
                    "ticker": "600519",
                    "name": "贵州茅台",
                }
            ],
        }

    monkeypatch.setattr(fuyao_fetchers, "_request", fake_request)

    result = fuyao_fetchers.fetch_ths_index_constituents("886042.ti")

    assert calls == [
        (_INDEX_CONSTITUENTS_PATH, {"thscode": "886042.TI"})
    ]
    assert result.iloc[0].to_dict() == {
        "指数代码": "886042.TI",
        "同花顺代码": "600519.SH",
        "代码": "600519",
        "名称": "贵州茅台",
        "更新时间": result.attrs["provider_as_of"],
    }
    assert result.attrs["index_code"] == "886042.TI"
    assert result.attrs["total"] == 1


def test_ths_index_constituents_accepts_empty_result_and_rejects_multiple_codes(
    monkeypatch,
):
    monkeypatch.setattr(
        fuyao_fetchers,
        "_request",
        lambda *args, **kwargs: {"timestamp": _TIMESTAMP, "item": []},
    )

    result = fuyao_fetchers.fetch_ths_index_constituents("000300.sh")

    assert result.empty
    assert result.attrs["index_code"] == "000300.SH"
    assert result.attrs["total"] == 0
    with pytest.raises(ValueError, match="单个完整指数代码"):
        fuyao_fetchers.fetch_ths_index_constituents("886042.TI,000300.SH")


def test_limit_up_ladder_preserves_window_board_matrix_and_nullable_flag(monkeypatch):
    boards = _empty_boards()
    boards["two_board"] = [
        {
            "thscode": "000001.SZ",
            "ticker": "000001",
            "name": "平安银行",
            "board_num": 2,
            "seal_nextday": None,
            "sign_level": 3,
        }
    ]
    payload = {
        "timestamp": _TIMESTAMP,
        "window": {
            "length": 30,
            "date_list": ["2026-08-19"],
            "board_caps": {
                "two_board": 4,
                "three_board": 4,
                "four_board": 4,
                "five_board": 4,
                "six_board": 4,
                "seven_over": 4,
            },
        },
        "item": [{"date": "2026-08-19", "boards": boards}],
    }
    calls = []

    def fake_request(path, params=None):
        calls.append((path, params))
        return deepcopy(payload)

    monkeypatch.setattr(fuyao_fetchers, "_request", fake_request)

    result = fuyao_fetchers.fetch_limit_up_ladder()

    assert calls == [(_LIMIT_UP_LADDER_PATH, None)]
    assert result == payload
    assert result["window"]["board_caps"]["seven_over"] == 4
    assert set(result["item"][0]["boards"]) == set(_LADDER_KEYS)
    assert result["item"][0]["boards"]["two_board"][0]["seal_nextday"] is None


def test_limit_up_ladder_accepts_fixed_empty_board_slots(monkeypatch):
    payload = {
        "timestamp": _TIMESTAMP,
        "window": {
            "length": 30,
            "date_list": ["2026-08-19"],
            "board_caps": {key: 4 for key in _LADDER_KEYS},
        },
        "item": [{"date": "2026-08-19", "boards": _empty_boards()}],
    }
    monkeypatch.setattr(
        fuyao_fetchers, "_request", lambda *args, **kwargs: deepcopy(payload)
    )

    assert fuyao_fetchers.fetch_limit_up_ladder() == payload


def test_limit_up_ladder_rejects_missing_fixed_board_slot(monkeypatch):
    boards = _empty_boards()
    boards.pop("seven_over")
    monkeypatch.setattr(
        fuyao_fetchers,
        "_request",
        lambda *args, **kwargs: {
            "timestamp": _TIMESTAMP,
            "window": {
                "length": 30,
                "date_list": ["2026-08-19"],
                "board_caps": {key: 4 for key in _LADDER_KEYS},
            },
            "item": [{"date": "2026-08-19", "boards": boards}],
        },
    )

    with pytest.raises(RuntimeError, match="seven_over"):
        fuyao_fetchers.fetch_limit_up_ladder()


def test_stock_anomaly_analysis_maps_batch_request_and_nested_keywords(monkeypatch):
    calls = []

    def fake_request(path, params=None):
        calls.append((path, params))
        return {
            "timestamp": _TIMESTAMP,
            "item": [
                {
                    "stock_name": "贵州茅台",
                    "analysis_content": "盘中快速拉升",
                    "keyword_list": [],
                    "thscode": "600519.SH",
                    "tag_name": "火箭发射",
                }
            ],
        }

    monkeypatch.setattr(fuyao_fetchers, "_request", fake_request)

    result = fuyao_fetchers.fetch_stock_anomaly_analysis(
        "600519,000001.sz,600519.SH"
    )

    assert calls == [
        (_STOCK_ANOMALY_PATH, {"thscodes": "600519.SH,000001.SZ"})
    ]
    assert result.iloc[0].to_dict() == {
        "代码": "600519",
        "同花顺代码": "600519.SH",
        "名称": "贵州茅台",
        "异动标签": "火箭发射",
        "异动解读": "盘中快速拉升",
        "关键词": [],
        "更新时间": result.attrs["provider_as_of"],
    }
    assert result.attrs["requested_total"] == 2
    assert result.attrs["total"] == 1
    assert result.attrs["valid_empty"] is False


def test_stock_anomaly_analysis_accepts_documented_empty_result(monkeypatch):
    monkeypatch.setattr(
        fuyao_fetchers,
        "_request",
        lambda *args, **kwargs: {"timestamp": _TIMESTAMP, "item": []},
    )

    result = fuyao_fetchers.fetch_stock_anomaly_analysis("600519")

    assert result.empty
    assert result.attrs["requested_total"] == 1
    assert result.attrs["total"] == 0
    assert result.attrs["valid_empty"] is True
    assert result.attrs["source_valid"] is True


@pytest.mark.parametrize(
    ("call", "payload"),
    [
        (
            lambda: fuyao_fetchers.fetch_valuation_snapshot("600519"),
            {"timestamp": _TIMESTAMP, "total": 1, "item": "invalid"},
        ),
        (
            lambda: fuyao_fetchers.fetch_ths_index_catalog(),
            {"timestamp": _TIMESTAMP, "item": None},
        ),
        (
            lambda: fuyao_fetchers.fetch_ths_index_constituents("886042.TI"),
            {"timestamp": _TIMESTAMP, "item": {}},
        ),
        (
            fuyao_fetchers.fetch_limit_up_ladder,
            {"timestamp": _TIMESTAMP, "window": None, "item": []},
        ),
        (
            lambda: fuyao_fetchers.fetch_stock_anomaly_analysis("600519"),
            {"timestamp": _TIMESTAMP, "item": "invalid"},
        ),
    ],
    ids=["valuation", "catalog", "constituents", "ladder", "anomaly"],
)
def test_capability_fetchers_reject_malformed_success_data(monkeypatch, call, payload):
    monkeypatch.setattr(
        fuyao_fetchers, "_request", lambda *args, **kwargs: deepcopy(payload)
    )

    with pytest.raises(RuntimeError):
        call()


@pytest.mark.parametrize(
    ("board_type", "path", "item", "expected"),
    [
        (
            "zt",
            _LIMIT_UP_POOL_PATH,
            _limit_up_item(),
            {
                "code": "600519",
                "amount": None,
                "turnover": None,
                "continue_day_cnt": 2,
                "limit_up_reason": "业绩增长",
            },
        ),
        (
            "dt",
            _LIMIT_DOWN_POOL_PATH,
            _limit_down_item(),
            {
                "code": "000001",
                "amount": None,
                "turnover": 5.2,
                "first_limit_time": "10:00:00",
            },
        ),
        (
            "zb",
            _LIMIT_BREAK_POOL_PATH,
            _limit_break_item(),
            {
                "code": "300750",
                "amount": 5_000_000_000.0,
                "turnover": 7.8,
                "open_times": 3,
            },
        ),
    ],
)
def test_limit_up_board_adapts_supported_official_pools(
    monkeypatch, board_type, path, item, expected
):
    calls = []

    def fake_request(request_path, params=None):
        calls.append((request_path, params))
        return _pool_payload([deepcopy(item)])

    monkeypatch.setattr(fuyao_fetchers, "_request", fake_request)

    result = fuyao_fetchers.fetch_limit_up_board(board_type)

    assert calls == [(path, {"size": 200, "page": 1})]
    assert result["type"] == board_type
    assert result["count"] == 1
    assert result["source"] == "ths_fuyao"
    assert {key: result["data"][0][key] for key in expected} == expected


def test_limit_up_board_paginates_and_rejects_duplicate_stocks(monkeypatch):
    monkeypatch.setattr(fuyao_fetchers, "_POOL_PAGE_SIZE", 1)
    first = _limit_up_item("600519")
    second = _limit_up_item("601318")
    second.update({"thscode": "601318.SH", "ticker": "601318", "name": "中国平安"})
    calls = []

    def fake_request(path, params=None):
        calls.append((path, dict(params)))
        item = first if params["page"] == 1 else second
        return _pool_payload(
            [deepcopy(item)], page=params["page"], size=1, total=2, pages=2
        )

    monkeypatch.setattr(fuyao_fetchers, "_request", fake_request)

    result = fuyao_fetchers.fetch_limit_up_board("zt")

    assert result["count"] == 2
    assert [row["code"] for row in result["data"]] == ["600519", "601318"]
    assert [params["page"] for _, params in calls] == [1, 2]

    def duplicate_request(path, params=None):
        return _pool_payload(
            [deepcopy(first)], page=params["page"], size=1, total=2, pages=2
        )

    monkeypatch.setattr(fuyao_fetchers, "_request", duplicate_request)
    with pytest.raises(RuntimeError, match="重复股票"):
        fuyao_fetchers.fetch_limit_up_board("zt")


def test_limit_up_board_preserves_valid_empty_and_skips_unsupported_capability(
    monkeypatch,
):
    calls = []

    def fake_request(path, params=None):
        calls.append((path, params))
        return _pool_payload([], page=1, total=0, pages=0)

    monkeypatch.setattr(fuyao_fetchers, "_request", fake_request)

    assert fuyao_fetchers.fetch_limit_up_board("dt") == {
        "type": "dt",
        "data": [],
        "count": 0,
        "source": "ths_fuyao",
    }
    for board_type in ("prev_zt", "sentiment"):
        with pytest.raises(SourceCapabilityError):
            fuyao_fetchers.fetch_limit_up_board(board_type)
    assert len(calls) == 1


def test_market_breadth_combines_snapshot_with_exact_pool_counts(monkeypatch):
    snapshot = pd.DataFrame({"涨跌幅": [1.2, -0.5, 0.0, 2.1, -3.0]})
    snapshot.attrs["provider_as_of"] = "2026-08-19T15:00:00+08:00"
    monkeypatch.setattr(fuyao_fetchers, "fetch_realtime_quote", lambda: snapshot)

    def fake_pool(board_type, *, date_ms=None):
        if board_type == "zt":
            return ([{"code": "600519"}, {"code": "000001"}], "2026-08-19T15:00:00+08:00")
        return ([{"code": "300750"}], "2026-08-19T15:00:00+08:00")

    monkeypatch.setattr(fuyao_fetchers, "_fetch_pool_records", fake_pool)

    result = fuyao_fetchers.fetch_market_breadth()

    assert result.iloc[0].to_dict() == {
        "上涨": 2,
        "下跌": 2,
        "平盘": 1,
        "涨停": 2,
        "跌停": 1,
    }
    assert result.attrs["source"] == "ths_fuyao"


def _dragon_tiger_payload():
    return {
        "timestamp": _TIMESTAMP,
        "board_type": "all",
        "trade_date": "2026-08-19",
        "count": 2,
        "stock_count": 1,
        "stock_items": [
            {
                "thscode": "600519.SH",
                "ticker": "600519",
                "name": "贵州茅台",
                "concept_list": [{"name": "白酒"}],
                "change": 0.052,
                "buy_value": 800_000_000.0,
                "sell_value": 500_000_000.0,
                "net_value": 300_000_000.0,
                "net_rate": 0.032,
                "range_days": 1,
            },
            {
                "thscode": "600519.SH",
                "ticker": "600519",
                "name": "贵州茅台",
                "concept_list": ["白酒"],
                "change": 0.1,
                "buy_value": 900_000_000.0,
                "sell_value": 400_000_000.0,
                "net_value": 500_000_000.0,
                "net_rate": 0.055,
                "org_net_value": 100_000_000.0,
                "hot_money_net_value": 200_000_000.0,
                "hot_rank": 5,
                "range_days": 3,
                "limit_reason": "连续三个交易日偏离值累计达20%",
            },
        ],
        "hot_money_items": [],
    }


def test_dragon_tiger_returns_only_single_day_market_dataframe(monkeypatch):
    calls = []

    def fake_request(path, params=None):
        calls.append((path, params))
        return _dragon_tiger_payload()

    monkeypatch.setattr(fuyao_fetchers, "_request", fake_request)

    result = fuyao_fetchers.fetch_dragon_tiger(
        code="", trade_date="2026-08-19", look_back_days=1
    )

    assert calls == [
        (
            _DRAGON_TIGER_PATH,
            {"board_type": "all", "date": "2026-08-19"},
        )
    ]
    assert result.iloc[0]["代码"] == "600519"
    assert result.iloc[0]["龙虎榜净买额"] == 300_000_000.0
    assert result.iloc[0]["涨跌幅"] == pytest.approx(5.2)
    assert result.iloc[0]["净买额占总成交比"] == pytest.approx(3.2)
    assert result.iloc[0]["概念列表"] == ["白酒"]
    assert pd.isna(result.iloc[0]["机构净买额"])
    assert pd.isna(result.iloc[0]["热度排名"])
    assert result.iloc[0]["解读"] == ""
    assert len(result) == 2
    assert result.attrs["total"] == 2
    assert result.attrs["stock_count"] == 1
    assert result.attrs["trade_date"] == "2026-08-19"
    assert result.attrs["source"] == "ths_fuyao"


def test_dragon_tiger_rejects_per_stock_and_multi_day_without_request(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fuyao_fetchers,
        "_request",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(SourceCapabilityError, match="per-stock"):
        fuyao_fetchers.fetch_dragon_tiger(code="600519")
    with pytest.raises(SourceCapabilityError, match="one trading day"):
        fuyao_fetchers.fetch_dragon_tiger(code="", look_back_days=2)
    with pytest.raises(SourceCapabilityError, match="board_type='all'"):
        fuyao_fetchers.fetch_dragon_tiger(board_type="hot_money")
    assert calls == []


def test_akshare_market_day_fallback_requires_exact_equivalent_contract(monkeypatch):
    frame = pd.DataFrame(
        [
            {
                "代码": "600519",
                "名称": "贵州茅台",
                "上榜日": "2026-08-19",
                "解读": "日涨幅偏离值达7%",
                "涨跌幅": 5.2,
                "龙虎榜买入额": 800_000_000.0,
                "龙虎榜卖出额": 500_000_000.0,
                "龙虎榜净买额": 300_000_000.0,
                "净买额占总成交比": 3.2,
            }
        ]
    )
    provider = type("Provider", (), {})()
    calls = []

    def stock_lhb_detail_em(**kwargs):
        calls.append(kwargs)
        return frame

    provider.stock_lhb_detail_em = stock_lhb_detail_em
    monkeypatch.setattr(akshare_fetchers, "_ak", lambda: provider)

    result = akshare_fetchers.fetch_dragon_tiger_market_day(
        trade_date="2026-08-19", board_type="all", look_back_days=1
    )

    assert result.columns.tolist() == fuyao_fetchers._DRAGON_TIGER_COLUMNS
    assert result.iloc[0]["数据源"] == "akshare_exact_day"
    assert result.iloc[0]["龙虎榜净买额"] == 300_000_000.0
    assert pd.isna(result.iloc[0]["机构净买额"])
    assert result.attrs["trade_date"] == "2026-08-19"
    assert result.attrs["provider_as_of"] is None
    assert calls == [{"start_date": "20260819", "end_date": "20260819"}]

    wrong_day = frame.copy()
    wrong_day.loc[0, "上榜日"] = "2026-08-18"
    provider.stock_lhb_detail_em = lambda **kwargs: wrong_day
    with pytest.raises(RuntimeError, match="错日"):
        akshare_fetchers.fetch_dragon_tiger_market_day(
            trade_date="2026-08-19", board_type="all", look_back_days=1
        )

    with pytest.raises(SourceCapabilityError):
        akshare_fetchers.fetch_dragon_tiger_market_day(
            trade_date="2026-08-19", board_type="org"
        )
    with pytest.raises(SourceCapabilityError):
        akshare_fetchers.fetch_dragon_tiger_market_day(trade_date="")


def test_eastmoney_limit_pool_never_ignores_fuyao_historical_date(monkeypatch):
    called = False

    def provider():
        nonlocal called
        called = True

    monkeypatch.setattr(astock_signals_fetchers, "_as", provider)

    with pytest.raises(SourceCapabilityError, match="date_ms"):
        astock_signals_fetchers.fetch_limit_up_board(
            board_type="zt", date_ms=1_784_275_991_000
        )

    assert called is False


def test_new_fuyao_adapters_reject_malformed_provider_counts(monkeypatch):
    payload = _dragon_tiger_payload()
    payload["stock_count"] = 2
    monkeypatch.setattr(
        fuyao_fetchers, "_request", lambda *args, **kwargs: deepcopy(payload)
    )

    with pytest.raises(RuntimeError, match="stock_count"):
        fuyao_fetchers.fetch_dragon_tiger()
