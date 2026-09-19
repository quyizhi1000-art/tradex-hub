"""Risk-appetite V1 domain model tests."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tradex.dashboard.risk_appetite import (
    CONFIG_VERSION,
    SECTOR_DEFINITIONS,
    build_risk_appetite_snapshot,
    percentile_rank,
)


def _board(name: str, level: str) -> dict:
    values = {
        "strong": (8.0, 65, 35, 8.0),
        "medium": (0.0, 50, 50, 0.0),
        "weak": (-8.0, 35, 65, -8.0),
    }
    change, up_count, down_count, flow_ratio = values[level]
    return {
        "板块": name,
        "涨跌幅": change,
        "上涨家数": up_count,
        "下跌家数": down_count,
        "主力净流入-占比": flow_ratio,
    }


def _background(prefix: str) -> list[dict]:
    return [
        {
            "板块": f"{prefix}{position}",
            "涨跌幅": value,
            "上涨家数": 50,
            "下跌家数": 50,
            "主力净流入-占比": value,
        }
        for position, value in enumerate((-4.0, -2.0, 0.0, 2.0, 4.0), start=1)
    ]


def _market(level: str) -> dict:
    if level == "strong":
        return {
            "market_breadth": {"上涨家数": 3300, "下跌家数": 1700},
            "index_records": [
                {"名称": "上证指数", "涨跌幅": 0.5},
                {"名称": "深证成指", "涨跌幅": 0.7},
                {"名称": "创业板指", "涨跌幅": 1.5},
            ],
            "market_turnover": {"available": True, "direction": "expand", "difference": 100},
        }
    if level == "weak":
        return {
            "market_breadth": {"上涨家数": 1700, "下跌家数": 3300},
            "index_records": [
                {"名称": "上证指数", "涨跌幅": -0.5},
                {"名称": "深证成指", "涨跌幅": -0.7},
                {"名称": "创业板指", "涨跌幅": -1.5},
            ],
            "market_turnover": {"available": True, "direction": "shrink", "difference": -100},
        }
    return {
        "market_breadth": {"上涨家数": 2500, "下跌家数": 2500},
        "index_records": [
            {"名称": "上证指数", "涨跌幅": 0.2},
            {"名称": "深证成指", "涨跌幅": -0.2},
            {"名称": "创业板指", "涨跌幅": 0.0},
        ],
        "market_turnover": {"available": True, "direction": "flat", "difference": 0},
    }


def _snapshot(
    *,
    industry: list[dict] | None = None,
    concept: list[dict] | None = None,
    market: str = "medium",
    as_of: str = "2026-08-19T10:30:00+08:00",
    etfs: list[dict] | None = None,
) -> dict:
    market_inputs = _market(market)
    return build_risk_appetite_snapshot(
        industry_records=[*_background("行业样本"), *(industry or [])],
        concept_records=[*_background("概念样本"), *(concept or [])],
        market_breadth=market_inputs["market_breadth"],
        index_records=market_inputs["index_records"],
        market_turnover=market_inputs["market_turnover"],
        etf_records=etfs or [],
        as_of=as_of,
    )


def test_configuration_contains_the_eleven_versioned_user_sectors():
    assert CONFIG_VERSION == "risk-appetite-v1.3"
    assert [definition.label for definition in SECTOR_DEFINITIONS] == [
        "银行",
        "证券",
        "互联网金融",
        "油气",
        "农业",
        "有色金属",
        "稀土",
        "贵金属",
        "电力",
        "零售",
        "白酒",
    ]
    assert all(definition.etf_keywords for definition in SECTOR_DEFINITIONS)
    assert all(len(definition.leaders) == 2 for definition in SECTOR_DEFINITIONS)


def test_percentile_rank_is_inclusive_and_tie_adjusted():
    universe = [-2.0, 0.0, 2.0]
    assert percentile_rank(-2.0, universe) == 0.0
    assert percentile_rank(0.0, universe) == 0.5
    assert percentile_rank(2.0, universe) == 1.0
    assert percentile_rank(1.0, [1.0, 1.0, 1.0]) == 0.5
    assert percentile_rank(None, universe) is None


def test_market_breadth_uses_provider_schema_without_summing_industries():
    market_inputs = _market("medium")
    result = build_risk_appetite_snapshot(
        industry_records=[_board("银行", "strong") for _ in range(3)],
        market_breadth={"上涨": 3300, "下跌": 1700},
        index_records=market_inputs["index_records"],
        market_turnover=market_inputs["market_turnover"],
        as_of="2026-08-19T10:30:00+08:00",
    )

    breadth = result["market_participation"]["metrics"]["market_breadth"]
    assert breadth == {"up_count": 3300.0, "down_count": 1700.0, "ratio": 0.66}
    assert result["market_participation"]["votes"]["market_breadth"] == "strong"


def test_alias_matching_is_exact_not_substring_based():
    result = _snapshot(industry=[_board("银行服务", "strong")])

    bank = result["sectors"]["bank"]
    assert bank["matched_alias"] is None
    assert bank["level"] == "unknown"


def test_bank_strength_alone_is_support_not_financial_attack():
    result = _snapshot(
        industry=[_board("银行", "strong"), _board("证券", "weak")],
        concept=[_board("互联网金融", "weak")],
        market="weak",
    )

    assert result["sectors"]["bank"]["level"] == "strong"
    assert result["groups"]["financial_core"]["level"] == "weak"
    assert result["emotion"] == "谨慎"
    assert result["structure"] == "银行护盘"


def test_financial_core_requires_securities_and_internet_finance_resonance():
    result = _snapshot(
        industry=[_board("证券", "strong")],
        concept=[_board("互联网金融", "strong")],
        market="strong",
    )

    assert result["groups"]["financial_core"]["level"] == "strong"
    assert result["market_participation"]["level"] == "strong"
    assert result["emotion"] == "积极"
    assert result["structure"] == "金融进攻"


def test_strong_market_with_bank_only_is_other_theme_not_bank_support():
    result = _snapshot(
        industry=[_board("银行", "strong"), _board("证券", "weak")],
        concept=[_board("互联网金融", "weak")],
        market="strong",
    )

    assert result["emotion"] == "积极"
    assert result["structure"] == "其他主线"


def test_cashflow_defense_is_not_collapsed_into_resource_cycle():
    result = _snapshot(
        industry=[_board("电力", "strong")],
        market="weak",
    )

    assert result["groups"]["cashflow_defense"]["level"] == "strong"
    assert result["groups"]["cyclical_resource"]["level"] == "unknown"
    assert result["structure"] == "防御升温"


def test_resource_cycle_has_its_own_resource_inflation_state():
    result = _snapshot(
        industry=[_board("有色金属", "strong")],
        concept=[_board("稀土", "strong")],
        market="medium",
    )

    assert result["groups"]["cyclical_resource"]["level"] == "strong"
    assert result["structure"] == "资源通胀"


def test_market_strength_without_selected_leaders_is_other_theme():
    result = _snapshot(market="strong")

    assert result["market_participation"]["votes"] == {
        "market_breadth": "strong",
        "index_participation": "strong",
        "same_time_turnover": "strong",
    }
    assert result["emotion"] == "积极"
    assert result["structure"] == "其他主线"


def test_weak_market_without_a_protective_group_is_broad_retreat():
    result = _snapshot(market="weak")

    assert result["emotion"] == "谨慎"
    assert result["structure"] == "普遍退潮"


def test_missing_values_are_not_converted_to_zero_or_neutral_votes():
    result = build_risk_appetite_snapshot(
        industry_records=[{"板块": "银行", "涨跌幅": 2.0}],
        as_of="2026-08-19T10:30:00+08:00",
    )

    bank = result["sectors"]["bank"]
    assert bank["metrics"]["breadth_ratio"] is None
    assert bank["metrics"]["main_net_ratio"] is None
    assert bank["evidence"]["breadth"] is None
    assert bank["evidence"]["fund_flow"] is None
    assert bank["level"] == "unknown"
    assert result["market_participation"]["level"] == "unknown"
    assert result["emotion"] == "中性"
    assert result["structure"] == "分化观察"
    assert result["data_quality"]["level"] == "low"


def test_pre_0945_snapshot_is_marked_as_opening_observation():
    china = ZoneInfo("Asia/Shanghai")
    result = _snapshot(
        market="strong",
        as_of=datetime(2026, 8, 19, 9, 44, 59, tzinfo=china),
    )

    assert result["phase"] == "opening_observation"
    assert result["opening_observation"] is True

    regular = _snapshot(market="strong", as_of="2026-08-19T09:45:00+08:00")
    assert regular["phase"] == "regular"
    assert regular["opening_observation"] is False

    pre_open = _snapshot(market="strong", as_of="2026-08-19T09:29:59+08:00")
    assert pre_open["phase"] == "regular"
    assert pre_open["opening_observation"] is False


def test_etf_and_fixed_leaders_are_display_only():
    without_etf = _snapshot(industry=[_board("银行", "medium")])
    with_etf = _snapshot(
        industry=[_board("银行", "medium")],
        etfs=[
            {"名称": "小银行ETF", "成交额": 100, "涨跌幅": -9.0},
            {"名称": "大银行ETF", "成交额": 500, "涨跌幅": 9.0},
        ],
    )

    assert without_etf["sectors"]["bank"]["level"] == with_etf["sectors"]["bank"]["level"]
    assert with_etf["sectors"]["bank"]["etf"]["名称"] == "大银行ETF"
    assert with_etf["sectors"]["bank"]["leaders"] == [
        {"name": "工商银行", "code": "601398"},
        {"name": "招商银行", "code": "600036"},
    ]


def test_board_strength_uses_two_of_three_available_evidence_votes():
    result = build_risk_appetite_snapshot(
        industry_records=[
            *_background("行业样本"),
            {"板块": "电力", "涨跌幅": 8.0, "上涨家数": 70, "下跌家数": 30},
        ],
        as_of="10:30:00",
    )

    power = result["sectors"]["electric_power"]
    assert power["evidence"] == {
        "price": "strong",
        "breadth": "strong",
        "fund_flow": None,
        "leadership": None,
    }
    assert power["level"] == "strong"
    assert power["metrics"]["price_percentile"] == pytest.approx(1.0)


def test_agriculture_replay_uses_chain_leadership_without_rewriting_weak_breadth():
    result = build_risk_appetite_snapshot(
        industry_records=[
            *_background("行业样本"),
            {
                "板块": "种植业",
                "涨跌幅": 8.0,
                "上涨家数": 7,
                "下跌家数": 14,
                "主力净流入-占比": -1.0,
            },
        ],
        leadership_records=[
            {
                "代码": "600371",
                "名称": "万向德农",
                "smart_sector_membership": {"status": "verified", "primary_sector_key": "agriculture", "primary_sector_name": "农业"},
                "涨停原因": "转基因+粮食概念+玉米种业",
                "连板": "2天2板",
            },
            {
                "代码": "000505",
                "名称": "京粮控股",
                "smart_sector_membership": {"status": "verified", "primary_sector_key": "agriculture", "primary_sector_name": "农业"},
                "涨停原因": "粮食概念+油脂加工+国企改革",
                "连板": "3天3板",
            },
            {
                "代码": "600127",
                "名称": "金健米业",
                "smart_sector_membership": {"status": "verified", "primary_sector_key": "agriculture", "primary_sector_name": "农业"},
                "涨停原因": "粮食安全+粮油加工+湖南国资",
                "连板": "3天3板",
            },
            {
                "代码": "001338",
                "名称": "永顺泰",
                "smart_sector_membership": {"status": "verified", "primary_sector_key": "agriculture", "primary_sector_name": "农业"},
                "涨停原因": "粮食概念+麦芽+啤酒+国企",
                "连板": "首板",
            },
            {
                "代码": "603395",
                "名称": "红四方",
                "smart_sector_membership": {"status": "verified", "primary_sector_key": "agriculture", "primary_sector_name": "农业"},
                "涨停原因": "新疆煤化工项目+复合肥+央企",
                "连板": "3天3板",
            },
        ],
        leadership_source_status={
            "source_valid": True,
            "eligible_for_vote": True,
            "data_date": "20260819",
        },
        trade_date="2026-08-19",
        as_of="2026-08-19T10:30:00+08:00",
    )

    agriculture = result["sectors"]["agriculture"]
    assert agriculture["core_level"] == "medium"
    assert agriculture["evidence"] == {
        "price": "strong",
        "breadth": "weak",
        "fund_flow": "medium",
        "leadership": "strong",
    }
    assert agriculture["level"] == "strong"
    assert agriculture["strength_profile"] == "leader_concentrated"
    assert agriculture["metrics"]["breadth_ratio"] == pytest.approx(1 / 3)
    assert agriculture["leadership"]["matched_count"] == 5
    assert agriculture["leadership"]["max_board_count"] == 3
    assert agriculture["leadership"]["leadership_rank"] == 1
    red_four = next(
        leader
        for leader in agriculture["leadership"]["limit_up_leaders"]
        if leader["code"] == "603395"
    )
    assert red_four["raw_reason"] == "新疆煤化工项目+复合肥+央企"
    assert any(
        item["matched_tag"] == "农业" and item["source"] == "smart_sector_library"
        for item in red_four["attributions"]
    )


def test_unavailable_leadership_preserves_the_original_three_vote_result():
    baseline = _snapshot(industry=[_board("银行", "strong")])
    unavailable = build_risk_appetite_snapshot(
        industry_records=[*_background("行业样本"), _board("银行", "strong")],
        leadership_records=[
            {"代码": "1", "涨停原因": "银行", "连板": "3天3板"},
            {"代码": "2", "涨停原因": "银行", "连板": "2天2板"},
        ],
        leadership_source_status={
            "source_valid": False,
            "eligible_for_vote": False,
            "data_date": "20260818",
        },
        trade_date="2026-08-19",
        as_of="2026-08-19T10:30:00+08:00",
    )

    assert unavailable["sectors"]["bank"]["level"] == baseline["sectors"]["bank"]["level"]
    assert unavailable["sectors"]["bank"]["evidence"]["leadership"] is None


def test_valid_empty_leadership_cannot_upgrade_sparse_quote_evidence():
    result = build_risk_appetite_snapshot(
        industry_records=[{"板块": "银行", "涨跌幅": 2.0}],
        leadership_records=[],
        leadership_source_status={
            "source_valid": True,
            "eligible_for_vote": True,
            "valid_empty": True,
            "data_date": "20260819",
        },
        trade_date="2026-08-19",
        as_of="2026-08-19T10:30:00+08:00",
    )

    bank = result["sectors"]["bank"]
    assert bank["evidence"]["leadership"] == "medium"
    assert bank["core_evidence_available"] == 1
    assert bank["level"] == "unknown"


def test_four_vote_two_to_two_conflict_is_medium():
    from tradex.dashboard.risk_appetite import _vote_level

    assert _vote_level(["strong", "strong", "weak", "weak"]) == "medium"
