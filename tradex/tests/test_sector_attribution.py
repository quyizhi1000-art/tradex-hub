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


def reviewed(code, *, key="bank", boards=1, amount=100):
    return {"code": code, "reason": "券商+互联网金融", "board_count": boards,
            "order_amount": amount, "sector_profile": {"industry": "证券Ⅱ"},
            "smart_sector_membership": {"status": "verified", "primary_sector_key": key,
                "primary_sector_name": key, "tags": ["券商", "互联网金融"]}}


def test_library_primary_overrides_reasons_industry_and_secondary_tags():
    result = attribute_limit_up_records([reviewed("000001")])
    assert result["sectors"]["bank"]["matched_count"] == 1
    assert sum(s["matched_count"] for s in result["sectors"].values()) == 1
    leader = result["sectors"]["bank"]["limit_up_leaders"][0]
    assert leader["raw_reason"] == "券商+互联网金融"
    assert leader["sector_profile"]["industry"] == "证券Ⅱ"
    assert {a["source"] for a in leader["attributions"]} == {"smart_sector_library"}


@pytest.mark.parametrize("status", ["unresolved", "stale", "disputed"])
def test_unverified_never_uses_provider_labels_or_implies_neutral_leadership(status):
    row = reviewed("000001")
    row["smart_sector_membership"] = {"status": status}
    result = attribute_limit_up_records([row])
    assert result["membership_coverage"] == 0
    assert all(s["matched_count"] == 0 and s["vote"] == "unknown" for s in result["sectors"].values())


def test_verified_other_sector_counts_as_covered_without_becoming_one_of_eleven():
    result = attribute_limit_up_records([reviewed("002050", key="humanoid_robot")])
    assert result["membership_coverage"] == 1
    assert all(s["matched_count"] == 0 for s in result["sectors"].values())


def test_unique_counts_sorting_and_streak_thresholds_are_preserved():
    result = attribute_limit_up_records([
        reviewed("000003", boards=1, amount=50), reviewed("000002", boards=2, amount=10),
        reviewed("000001", boards=2, amount=20), reviewed("000001", boards=1, amount=5)])
    sector = result["sectors"]["bank"]
    assert result["pool_total"] == sector["matched_count"] == 3
    assert sector["vote"] == "strong" and sector["max_board_count"] == 2
    assert [s["code"] for s in sector["limit_up_leaders"]] == ["000001", "000002", "000003"]
    assert attribute_limit_up_records([reviewed("1"), reviewed("2")])["sectors"]["bank"]["vote"] == "medium"


def test_partial_membership_cannot_claim_complete_board_strength():
    result = attribute_limit_up_records([reviewed("1"), reviewed("2"), reviewed("3"), {"code": "4", "reason": "银行"}])
    assert result["membership_coverage"] == .75
    assert result["sectors"]["bank"]["vote"] == "unknown"


def test_duplicate_stock_with_conflicting_primary_is_not_double_counted():
    result = attribute_limit_up_records([reviewed("1"), reviewed("1", key="securities")])
    assert result["membership_coverage"] == 0
    assert all(s["matched_count"] == 0 for s in result["sectors"].values())


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




def test_agricultural_bank_name_does_not_trigger_agriculture_or_bank():
    result = attribute_limit_up_records(
        [{"代码": "601288", "名称": "农业银行", "涨停原因": "农业银行", "连板": "首板"}]
    )

    assert result["sectors"]["agriculture"]["matched_count"] == 0
    assert result["sectors"]["bank"]["matched_count"] == 0
    assert result["unmapped_tags"] == ["农业银行"]








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






@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("首板", 1),
        ("2天2板", 2),
        ("3天2板", None),
        ("10天3板", None),
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
def test_board_count_parser_only_claims_verified_continuous_streaks(label, expected):
    assert parse_board_count(label) == expected






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



