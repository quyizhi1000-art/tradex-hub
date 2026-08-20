"""Tests for exact, versioned limit-up sector attribution."""

from __future__ import annotations

import pytest

from tradex.dashboard.sector_attribution import (
    ATTRIBUTION_TAXONOMY_VERSION,
    CANONICAL_TAG_RULES,
    SECTOR_KEYS,
    attribute_board_names,
    attribute_limit_up_records,
    attribute_reason,
    parse_board_count,
    parse_reason_tags,
)


@pytest.mark.parametrize(
    ("sector_key", "positive_tag", "negative_tag"),
    [
        ("bank", "银行", "农业银行"),
        ("securities", "券商", "证券软件"),
        ("internet_finance", "金融科技", "科技"),
        ("oil_gas", "油气开采", "油气设备制造"),
        ("agriculture", "复合肥", "食品"),
        ("nonferrous", "有色金属", "金属"),
        ("rare_earth", "稀土永磁", "永磁"),
        ("precious_metals", "黄金概念", "珠宝"),
        ("electric_power", "电力行业", "风电设备"),
        ("retail", "商业百货", "商业地产"),
        ("baijiu", "白酒", "啤酒"),
    ],
)
def test_all_eleven_sectors_have_exact_positive_and_negative_examples(
    sector_key: str,
    positive_tag: str,
    negative_tag: str,
):
    positive = attribute_reason(positive_tag)
    negative = attribute_reason(negative_tag)

    assert {item["sector_key"] for item in positive["attributions"]} == {sector_key}
    assert negative["attributions"] == []
    assert negative["unmapped_tags"] == [negative_tag]


def test_taxonomy_is_versioned_and_covers_every_observed_sector():
    assert ATTRIBUTION_TAXONOMY_VERSION == "sector-attribution-v2"
    assert SECTOR_KEYS == (
        "bank",
        "securities",
        "internet_finance",
        "oil_gas",
        "agriculture",
        "nonferrous",
        "rare_earth",
        "precious_metals",
        "electric_power",
        "retail",
        "baijiu",
    )
    assert {rule["sector_key"] for rule in CANONICAL_TAG_RULES.values()} == set(SECTOR_KEYS)


def test_reason_parser_uses_only_controlled_separators_and_removes_whitespace():
    assert parse_reason_tags(
        " 银行 + 券商＋金融科技、油气，粮食,有色金属；稀土;黄金|电力/零售 "
    ) == [
        "银行",
        "券商",
        "金融科技",
        "油气",
        "粮食",
        "有色金属",
        "稀土",
        "黄金",
        "电力",
        "零售",
    ]
    # A colon is not a controlled separator and therefore cannot manufacture
    # two registered matches from a provider's arbitrary prose.
    assert attribute_reason("银行:券商")["attributions"] == []


def test_red_four_square_is_agriculture_only_because_of_compound_fertilizer():
    result = attribute_limit_up_records(
        [
            {
                "代码": "603395",
                "名称": "红四方",
                "涨停原因": "新疆煤化工项目+复合肥+央企",
                "连板": "首板",
                "封单额": 88_000_000,
            }
        ]
    )

    agriculture = result["sectors"]["agriculture"]
    leader = agriculture["limit_up_leaders"][0]
    assert agriculture["matched_count"] == 1
    assert leader["matched_tags"] == ["复合肥"]
    assert leader["parsed_tags"] == ["新疆煤化工项目", "复合肥", "央企"]
    assert leader["raw_reason"] == "新疆煤化工项目+复合肥+央企"
    assert leader["attributions"] == [
        {
            "matched_tag": "复合肥",
            "canonical_tag": "化肥",
            "sector_key": "agriculture",
            "chain_node": "农业投入品",
            "rule_id": "agriculture.化肥",
            "source": "reason",
        }
    ]
    assert result["unmapped_tags"] == ["央企", "新疆煤化工项目"]
    assert all(
        sector["matched_count"] == 0
        for key, sector in result["sectors"].items()
        if key != "agriculture"
    )


def test_agricultural_bank_name_does_not_trigger_agriculture_or_bank():
    result = attribute_limit_up_records(
        [{"代码": "601288", "名称": "农业银行", "涨停原因": "农业银行", "连板": "首板"}]
    )

    assert result["sectors"]["agriculture"]["matched_count"] == 0
    assert result["sectors"]["bank"]["matched_count"] == 0
    assert result["unmapped_tags"] == ["农业银行"]


def test_one_stock_can_belong_to_multiple_sectors_and_keeps_all_evidence():
    result = attribute_limit_up_records(
        [
            {
                "code": "300059",
                "name": "东方财富",
                "reason": "券商+互联网金融",
                "high_days": "2天2板",
                "order_amount": 200,
            }
        ]
    )

    securities_leader = result["sectors"]["securities"]["limit_up_leaders"][0]
    finance_leader = result["sectors"]["internet_finance"]["limit_up_leaders"][0]
    assert securities_leader == finance_leader
    assert securities_leader["matched_tags"] == ["券商", "互联网金融"]
    assert {item["sector_key"] for item in securities_leader["attributions"]} == {
        "securities",
        "internet_finance",
    }


def test_industry_profile_finds_bank_when_editorial_reason_omits_bank():
    result = attribute_limit_up_records([
        {
            "代码": "600928",
            "名称": "西安银行",
            "涨停原因": "半年报增长+西安国资+养老金融+数字人民币",
            "连板": "首板",
            "sector_profile": {
                "industry": "银行Ⅱ",
                "region": "陕西板块",
                "concept_tags": ["互联网金融", "移动支付"],
                "source": "eastmoney_ulist",
            },
        }
    ])

    bank = result["sectors"]["bank"]
    leader = bank["limit_up_leaders"][0]
    assert bank["matched_count"] == 1
    assert result["sectors"]["internet_finance"]["matched_count"] == 0
    assert leader["sector_profile"]["industry"] == "银行Ⅱ"
    assert leader["matched_tags"] == ["银行Ⅱ"]
    assert leader["attributions"] == [{
        "matched_tag": "银行Ⅱ",
        "canonical_tag": "商业银行",
        "sector_key": "bank",
        "chain_node": "银行",
        "rule_id": "bank.商业银行",
        "source": "industry_profile",
    }]
    assert result["industry_profile_coverage"] == 1.0


def test_reason_and_industry_profile_can_attribute_one_stock_to_multiple_sectors():
    result = attribute_limit_up_records([
        {
            "代码": "300059",
            "名称": "东方财富",
            "涨停原因": "互联网金融",
            "连板": "2天2板",
            "sector_profile": {
                "industry": "证券Ⅱ",
                "concept_tags": ["互联网金融"],
            },
        }
    ])

    assert result["sectors"]["securities"]["matched_count"] == 1
    assert result["sectors"]["internet_finance"]["matched_count"] == 1
    leader = result["sectors"]["securities"]["limit_up_leaders"][0]
    assert {item["source"] for item in leader["attributions"]} == {
        "reason",
        "industry_profile",
    }


def test_unknown_profile_industry_is_audited_without_substring_fallback():
    result = attribute_limit_up_records([
        {
            "代码": "1",
            "涨停原因": "人工智能",
            "连板": "首板",
            "sector_profile": {"industry": "银行软件"},
        }
    ])

    assert all(sector["matched_count"] == 0 for sector in result["sectors"].values())
    assert result["unmapped_industries"] == ["银行软件"]


def test_downstream_agriculture_industry_requires_an_active_reason_tag():
    profile_only = attribute_limit_up_records([{
        "代码": "000001",
        "涨停原因": "食品+国企",
        "连板": "首板",
        "sector_profile": {"industry": "农产品加工"},
    }])
    reason_tagged = attribute_limit_up_records([{
        "代码": "000002",
        "涨停原因": "农产品加工+国企",
        "连板": "首板",
        "sector_profile": {"industry": "农产品加工"},
    }])

    assert profile_only["sectors"]["agriculture"]["matched_count"] == 0
    assert profile_only["unmapped_industries"] == ["农产品加工"]
    leader = reason_tagged["sectors"]["agriculture"]["limit_up_leaders"][0]
    assert leader["attributions"] == [{
        "matched_tag": "农产品加工",
        "canonical_tag": "农产品加工",
        "sector_key": "agriculture",
        "chain_node": "农产品加工",
        "rule_id": "agriculture.农产品加工",
        "source": "reason",
    }]


def test_multiple_tags_and_duplicate_records_do_not_inflate_sector_count():
    result = attribute_limit_up_records(
        [
            {
                "代码": "000019",
                "名称": "深粮控股",
                "涨停原因": "粮食安全+农业种植",
                "连板": "首板",
            },
            {
                "代码": "000019",
                "名称": "深粮控股",
                "涨停原因": "复合肥+粮食安全",
                "连板": "2天2板",
            },
        ]
    )

    agriculture = result["sectors"]["agriculture"]
    leader = agriculture["limit_up_leaders"][0]
    assert result["pool_total"] == 1
    assert agriculture["matched_count"] == 1
    assert agriculture["max_board_count"] == 2
    assert leader["matched_tags"] == ["粮食安全", "农业种植", "复合肥"]
    assert leader["parsed_tags"] == ["粮食安全", "农业种植", "复合肥"]


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("首板", 1),
        ("2天2板", 2),
        ("3天2板", 2),
        ("10天3板", 3),
        ("4连板", 4),
        ("5板", 5),
        (2, 2),
        ("2026年3天2板", None),
        ("3天", None),
        ("第2板", None),
        ("", None),
        (None, None),
        ("未知", None),
    ],
)
def test_board_count_parser_uses_the_board_number_not_the_first_number(label, expected):
    assert parse_board_count(label) == expected


def test_leaders_have_deterministic_board_amount_code_order():
    result = attribute_limit_up_records(
        [
            {"代码": "000004", "名称": "丁", "涨停原因": "种业", "连板": "首板", "封单额": 9999},
            {"代码": "000003", "名称": "丙", "涨停原因": "粮食", "连板": "2天2板", "封单额": 50},
            {"代码": "000002", "名称": "乙", "涨停原因": "农机", "连板": "3天2板", "封单额": 100},
            {"代码": "000001", "名称": "甲", "涨停原因": "饲料", "连板": "2天2板", "封单额": 100},
        ]
    )

    agriculture = result["sectors"]["agriculture"]
    assert [item["code"] for item in agriculture["limit_up_leaders"]] == [
        "000001",
        "000002",
        "000003",
        "000004",
    ]
    assert agriculture["matched_count"] == 4
    assert agriculture["max_board_count"] == 2
    assert agriculture["vote"] == "strong"


def test_leadership_vote_uses_unique_count_and_streak_thresholds():
    three_first_boards = attribute_limit_up_records(
        [
            {"代码": "1", "涨停原因": "粮食", "连板": "首板"},
            {"代码": "2", "涨停原因": "种业", "连板": "首板"},
            {"代码": "3", "涨停原因": "饲料", "连板": "首板"},
        ]
    )
    two_with_streak = attribute_limit_up_records(
        [
            {"代码": "1", "涨停原因": "粮食", "连板": "首板"},
            {"代码": "2", "涨停原因": "种业", "连板": "3天2板"},
        ]
    )
    one_stock = attribute_limit_up_records(
        [{"代码": "1", "涨停原因": "粮食", "连板": "5板"}]
    )
    empty = attribute_limit_up_records([])

    assert three_first_boards["sectors"]["agriculture"]["vote"] == "strong"
    assert two_with_streak["sectors"]["agriculture"]["vote"] == "strong"
    assert one_stock["sectors"]["agriculture"]["vote"] == "medium"
    assert one_stock["sectors"]["bank"]["vote"] == "medium"
    assert empty["reason_coverage"] == 1.0
    assert all(sector["vote"] == "unknown" for sector in empty["sectors"].values())


def test_reason_coverage_unmapped_tags_and_all_sector_outputs_are_stable():
    result = attribute_limit_up_records(
        [
            {"代码": "1", "涨停原因": "粮食+央企", "连板": "首板"},
            {"代码": "2", "涨停原因": "减肥药+煤化工", "连板": "首板"},
            {"代码": "3", "涨停原因": "", "连板": "首板"},
        ]
    )

    assert result["taxonomy_version"] == ATTRIBUTION_TAXONOMY_VERSION
    assert result["pool_total"] == 3
    assert result["reason_coverage"] == pytest.approx(2 / 3)
    assert result["unmapped_tags"] == ["减肥药", "央企", "煤化工"]
    assert tuple(result["sectors"]) == SECTOR_KEYS
    assert all(sector["sector_key"] == key for key, sector in result["sectors"].items())


def test_stock_board_helper_reuses_rules_and_marks_its_source():
    result = attribute_board_names(["农化制品", "金融科技", "机械"])

    assert result["parsed_tags"] == ["农化制品", "金融科技", "机械"]
    assert result["matched_tags"] == ["农化制品", "金融科技"]
    assert {item["sector_key"] for item in result["attributions"]} == {
        "agriculture",
        "internet_finance",
    }
    assert {item["source"] for item in result["attributions"]} == {"stock_board"}
    assert result["unmapped_tags"] == ["机械"]


def test_august_19_cross_sector_calibration_keeps_two_first_boards_neutral():
    result = attribute_limit_up_records(
        [
            {"代码": "600371", "名称": "万向德农", "涨停原因": "转基因+粮食概念+玉米种业", "连板": "2天2板"},
            {"代码": "001338", "名称": "永顺泰", "涨停原因": "粮食概念+麦芽+啤酒+国企", "连板": "首板"},
            {"代码": "000059", "名称": "华锦股份", "涨停原因": "石化+化肥+央企+半年报预增", "连板": "首板"},
            {"代码": "001277", "名称": "速达股份", "涨停原因": "煤炭设备后市场+农机+海外布局", "连板": "首板"},
            {"代码": "603395", "名称": "红四方", "涨停原因": "新疆煤化工项目+复合肥+央企", "连板": "3天3板"},
            {"代码": "000505", "名称": "京粮控股", "涨停原因": "粮食概念+油脂加工+国企改革", "连板": "3天3板"},
            {"代码": "600127", "名称": "金健米业", "涨停原因": "粮食安全+粮油加工+湖南国资", "连板": "3天3板"},
            {"代码": "002040", "名称": "南京港", "涨停原因": "原油中转+国企+业绩增长", "连板": "首板"},
            {"代码": "001400", "名称": "江顺科技", "涨停原因": "商业航天+3D打印+铝挤压模具+液冷", "连板": "首板"},
            {"代码": "002333", "名称": "罗普斯金", "涨停原因": "拟收购盟萤科技+检测业务+铝型材", "连板": "首板"},
        ]
    )

    agriculture = result["sectors"]["agriculture"]
    oil_gas = result["sectors"]["oil_gas"]
    nonferrous = result["sectors"]["nonferrous"]
    assert (agriculture["matched_count"], agriculture["max_board_count"], agriculture["vote"]) == (
        7,
        3,
        "strong",
    )
    assert (oil_gas["matched_count"], oil_gas["max_board_count"], oil_gas["vote"]) == (
        2,
        1,
        "medium",
    )
    assert (nonferrous["matched_count"], nonferrous["max_board_count"], nonferrous["vote"]) == (
        2,
        1,
        "medium",
    )
    # 华锦股份同时属于农业投入品与石油炼化，但各板块都只计一次。
    huajin_sectors = {
        attribution["sector_key"]
        for sector in result["sectors"].values()
        for leader in sector["limit_up_leaders"]
        if leader["code"] == "000059"
        for attribution in leader["attributions"]
    }
    assert huajin_sectors == {"agriculture", "oil_gas"}


@pytest.mark.parametrize(
    ("trade_date", "records", "expected_strong", "expected_medium"),
    [
        (
            "20260807",
            [
                {"代码": "1", "涨停原因": "稀土", "连板": "3天3板"},
                {"代码": "2", "涨停原因": "稀土永磁", "连板": "首板"},
                {"代码": "3", "涨停原因": "钕铁硼", "连板": "首板"},
                {"代码": "4", "涨停原因": "黄金", "连板": "5板"},
                {"代码": "5", "涨停原因": "金矿", "连板": "首板"},
            ],
            {"rare_earth", "precious_metals"},
            set(),
        ),
        (
            "20260811",
            [{"代码": "1", "涨停原因": "黄金", "连板": "首板"}],
            set(),
            {"precious_metals"},
        ),
        (
            "20260814",
            [
                {"代码": "1", "涨停原因": "稀土", "连板": "首板"},
                {"代码": "2", "涨停原因": "稀土永磁", "连板": "首板"},
                {"代码": "3", "涨停原因": "钕铁硼", "连板": "首板"},
            ],
            {"rare_earth"},
            set(),
        ),
        (
            "20260817",
            [
                *[
                    {"代码": f"A{index}", "涨停原因": "粮食", "连板": "首板"}
                    for index in range(5)
                ],
                {"代码": "N1", "涨停原因": "铝型材", "连板": "首板"},
                {"代码": "N2", "涨停原因": "铝挤压模具", "连板": "首板"},
            ],
            {"agriculture"},
            {"nonferrous"},
        ),
        (
            "20260818",
            [
                {
                    "代码": f"A{index:02d}",
                    "涨停原因": "种业" if index % 2 else "化肥",
                    "连板": "2天2板" if index == 0 else "首板",
                }
                for index in range(18)
            ],
            {"agriculture"},
            set(),
        ),
        (
            "20260813",
            [{"代码": "1", "涨停原因": "机器人+创新药", "连板": "首板"}],
            set(),
            set(),
        ),
    ],
)
# These compact records reproduce observed aggregate shapes for deterministic
# threshold regression.  They are not frozen, full-pool historical evidence
# and must not be presented as an independent leaveout data set.
def test_observed_historical_shapes_are_stable_across_hot_and_cold_pools(
    trade_date,
    records,
    expected_strong,
    expected_medium,
):
    result = attribute_limit_up_records(records)
    actual_strong = {
        key for key, sector in result["sectors"].items() if sector["vote"] == "strong"
    }

    assert actual_strong == expected_strong, trade_date
    assert all(result["sectors"][key]["vote"] == "medium" for key in expected_medium), trade_date
