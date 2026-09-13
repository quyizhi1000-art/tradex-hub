from __future__ import annotations

from datetime import date

import pytest

from tradex.market_watch.market_theme_attribution import (
    infer_current_market_category as compatibility_infer,
)
from tradex.instrument_taxonomy.contracts import StockRelationshipProfileV1
from tradex.smart_sector_library import (
    SMART_SECTOR_LIBRARY_NAME,
    ReviewedMarketAttributionV1,
    SmartSectorCandidateV1,
    SmartSectorLibrary,
    infer_current_market_category,
    load_reviewed_market_attributions,
)


def test_smart_sector_library_has_a_stable_public_identity_and_policy() -> None:
    library = SmartSectorLibrary(date(2026, 9, 2), reviewed_attributions={})

    assert SMART_SECTOR_LIBRARY_NAME == "聪明板块库"
    assert library.policy.model_dump(mode="json") == {
        "contract": "smart_sector_policy.v1",
        "schema_version": 1,
        "library_name": "聪明板块库",
        "scope": "exact_trade_date_market_attribution",
        "market_context_required": True,
        "business_evidence_required": True,
        "reviewed_effective_scope": "exact_trade_date",
        "stable_taxonomy_writeback": False,
        "new_business_gate": "commercial_evidence_required",
        "category_granularity": "most_specific_supported_market_node",
        "unresolved_behavior": "abstain",
        "primary_resolution": "reusable_evidence_rule",
        "human_review_role": "calibration_and_exception_only",
    }


def test_smart_sector_library_resolves_a_reusable_crosscheck() -> None:
    decision = SmartSectorLibrary(
        date(2026, 9, 2),
        reviewed_attributions={},
    ).resolve(
        "600001.SH",
        market_context="电网设备和特高压方向走强",
        business_evidence=("1000kV输电线路角钢塔",),
    )

    assert decision.library_name == "聪明板块库"
    assert decision.category_name == "电力电网"
    assert decision.basis == "evidence_candidate_ranking"
    assert decision.logic_type == "direct_product_or_service"
    assert decision.quality == "accepted"
    assert decision.quality_flags == ()


def test_smart_sector_library_prefers_an_exact_date_review() -> None:
    effective_on = date(2026, 9, 2)
    reviewed = load_reviewed_market_attributions(date(2026, 8, 31))
    source = reviewed["603201.SH"]
    review = ReviewedMarketAttributionV1.model_validate({
        **source.model_dump(mode="json"),
        "effective_on": effective_on,
    })

    decision = SmartSectorLibrary(
        effective_on,
        reviewed_attributions={review.instrument_id: review},
    ).resolve(
        review.instrument_id,
        market_context="汽车零部件方向走强",
        business_evidence=("汽车维修保养设备",),
    )

    assert decision.category_name == "物理AI"
    assert decision.basis == "manual_market_review"
    assert decision.review_basis == "human_review_override"
    assert decision.logic_type == "human_exception"


def test_rule_supported_review_is_recomputed_instead_of_copied_as_a_label() -> None:
    effective_on = date(2026, 8, 31)
    reviewed = load_reviewed_market_attributions(effective_on)
    decision = SmartSectorLibrary(
        effective_on,
        reviewed_attributions=reviewed,
    ).resolve(
        "603113.SH",
        market_context="",
        business_evidence=(),
    )

    assert decision.category_name == "煤炭"
    assert decision.basis == "event_business_crosscheck"
    assert decision.review_basis is None


def test_smart_sector_library_abstains_without_dual_evidence() -> None:
    library = SmartSectorLibrary(date(2026, 9, 2), reviewed_attributions={})

    missing_business = library.resolve(
        "600001.SH",
        market_context="算力概念走强",
        business_evidence=(),
    )
    unsupported = library.resolve(
        "600002.SH",
        market_context="算力概念走强",
        business_evidence=("房地产开发和家居商贸",),
    )

    assert missing_business.basis == "unresolved"
    assert missing_business.quality_flags == ("business_evidence_missing",)
    assert unsupported.category_name == "房地产"
    assert unsupported.basis == "evidence_candidate_ranking"


def test_legacy_market_watch_import_is_only_a_compatibility_alias() -> None:
    assert compatibility_infer is infer_current_market_category


def _candidate(
    category_key: str,
    category_name: str,
    *,
    relation_type: str,
    evidence_stage: str,
    materiality: str,
    market_alignment: str,
    granularity: str = "specific",
    direct_business_evidence: bool = True,
) -> SmartSectorCandidateV1:
    return SmartSectorCandidateV1(
        category_key=category_key,
        category_name=category_name,
        relation_type=relation_type,
        evidence_stage=evidence_stage,
        materiality=materiality,
        market_alignment=market_alignment,
        granularity=granularity,
        direct_business_evidence=direct_business_evidence,
        evidence_refs=(f"evidence:{category_key}",),
    )


def test_commercialized_new_business_can_beat_untraded_legacy_business() -> None:
    decision = SmartSectorLibrary(
        date(2026, 9, 2),
        reviewed_attributions={},
    ).resolve_candidates(
        "603701.SH",
        (
            _candidate(
                "automotive",
                "汽车",
                relation_type="direct_product_or_service",
                evidence_stage="revenue",
                materiality="dominant",
                market_alignment="none",
            ),
            _candidate(
                "energy_storage",
                "储能",
                relation_type="commercialized_new_business",
                evidence_stage="revenue",
                materiality="emerging",
                market_alignment="co_movement",
            ),
        ),
    )

    assert decision.category_name == "储能"
    assert decision.logic_type == "commercialized_new_business"
    assert decision.basis == "evidence_candidate_ranking"


def test_investment_concept_cannot_beat_real_dominant_business() -> None:
    decision = SmartSectorLibrary(
        date(2026, 9, 2),
        reviewed_attributions={},
    ).resolve_candidates(
        "603216.SH",
        (
            _candidate(
                "chips",
                "芯片",
                relation_type="capital_relation",
                evidence_stage="investment",
                materiality="emerging",
                market_alignment="reason_only",
            ),
            _candidate(
                "real_estate",
                "房地产",
                relation_type="downstream_demand",
                evidence_stage="revenue",
                materiality="dominant",
                market_alignment="none",
            ),
        ),
    )

    assert decision.category_name == "房地产"
    assert decision.logic_type == "downstream_demand"


def test_specific_supported_child_wins_over_its_parent() -> None:
    library = SmartSectorLibrary(date(2026, 9, 2), reviewed_attributions={})
    decision = library.resolve_candidates(
        "002300.SZ",
        (
            _candidate(
                "electric_power",
                "电力",
                relation_type="direct_product_or_service",
                evidence_stage="revenue",
                materiality="dominant",
                market_alignment="leading_cluster",
                granularity="parent",
            ),
            _candidate(
                "power_grid",
                "电力电网",
                relation_type="direct_product_or_service",
                evidence_stage="revenue",
                materiality="dominant",
                market_alignment="leading_cluster",
            ),
        ),
    )

    assert decision.category_name == "电力电网"


def test_equal_conflicting_candidates_abstain_instead_of_using_rule_order() -> None:
    library = SmartSectorLibrary(date(2026, 9, 2), reviewed_attributions={})
    decision = library.resolve_candidates(
        "600001.SH",
        (
            _candidate(
                "chips",
                "芯片",
                relation_type="direct_product_or_service",
                evidence_stage="product",
                materiality="meaningful",
                market_alignment="co_movement",
            ),
            _candidate(
                "military",
                "军工",
                relation_type="direct_product_or_service",
                evidence_stage="product",
                materiality="meaningful",
                market_alignment="co_movement",
            ),
        ),
    )

    assert decision.basis == "unresolved"
    assert decision.quality_flags == ("ambiguous_top_evidence_candidates",)


def _relationship_profile(
    instrument_id: str,
    name: str,
    *,
    primary_business_key: str,
    primary_business_name: str,
    directory_category_key: str,
    directory_category_name: str,
    business_tags: tuple[str, ...],
    business_summary: str,
    business_segments: tuple[dict, ...],
) -> StockRelationshipProfileV1:
    return StockRelationshipProfileV1(
        instrument_id=instrument_id,
        name=name,
        as_of=date(2026, 9, 2),
        primary_business_key=primary_business_key,
        primary_business_name=primary_business_name,
        directory_category_key=directory_category_key,
        directory_category_name=directory_category_name,
        business_tags=business_tags,
        business_summary=business_summary,
        business_segments=business_segments,
        verification_status="provider_only",
    )


def test_pool_ranking_prefers_verified_liquid_cooling_over_generic_robotics_parts() -> None:
    jindi = _relationship_profile(
        "603270.SH",
        "金帝股份",
        primary_business_key="auto_parts",
        primary_business_name="汽车零部件",
        directory_category_key="automotive",
        directory_category_name="汽车",
        business_tags=(
            "汽车零部件",
            "精密零部件",
            "轴承保持架",
            "向心式油冷",
            "齿部油冷",
            "电机精准散热",
        ),
        business_summary="精密机械零部件的研发、生产和销售。",
        business_segments=(
            {
                "name": "汽车零部件",
                "report_period": "2026-06-30",
                "revenue_cny": 70.0,
                "revenue_share": 0.7,
                "source": "fixture",
            },
            {
                "name": "轴承保持架",
                "report_period": "2026-06-30",
                "revenue_cny": 30.0,
                "revenue_share": 0.3,
                "source": "fixture",
            },
        ),
    )
    cooling_peer = _relationship_profile(
        "002639.SZ",
        "雪人集团",
        primary_business_key="compressor",
        primary_business_name="压缩机",
        directory_category_key="compressor",
        directory_category_name="压缩机",
        business_tags=("压缩机", "数据中心", "中央空调系统"),
        business_summary="制冰设备、压缩机产品及系统应用。",
        business_segments=(),
    )

    decisions = SmartSectorLibrary(
        date(2026, 9, 2),
        reviewed_attributions={},
    ).resolve_relationships(
        (
            ("603270.SH", "液冷散热+人形机器人+电驱动定转子", jindi),
            ("002639.SZ", "液冷服务器+油气服务+压缩机", cooling_peer),
        )
    )

    assert decisions["603270.SH"].category_name == "液冷"
    assert decisions["603270.SH"].basis == "evidence_candidate_ranking"
    assert decisions["603270.SH"].category_name != "机器人"
    assert decisions["002639.SZ"].category_name == "液冷"


def test_research_stage_liquid_cooling_is_kept_as_pilot_evidence() -> None:
    jitai = _relationship_profile(
        "002909.SZ",
        "集泰股份",
        primary_business_key="disclosed_d0bcd436891e",
        primary_business_name="建筑类用胶",
        directory_category_key="disclosed_d0bcd436891e",
        directory_category_name="建筑类用胶",
        business_tags=(
            "建筑类用胶",
            "液冷导热硅油研发验证",
            "数据中心及储能热管理",
            "尚未市场投放",
        ),
        business_summary="建筑类用胶、工业类用胶和涂料。",
        business_segments=(),
    )
    library = SmartSectorLibrary(date(2026, 9, 2), reviewed_attributions={})

    candidates = library.candidates_from_relationship(
        market_context="液冷硅油+新能源胶+密封胶",
        relationship=jitai,
    )
    decision = library.resolve_candidates("002909.SZ", candidates)

    liquid = [item for item in candidates if item.category_name == "液冷"]
    assert liquid
    assert {item.evidence_stage for item in liquid} == {"pilot"}
    assert {item.materiality for item in liquid} == {"emerging"}
    assert decision.category_name == "液冷"


@pytest.mark.parametrize(
    ("market_context", "business_evidence", "expected"),
    (
        ("控制权变更+多元醇+业绩增长", ("多元醇", "纯碱", "精甲醇"), "化工"),
        ("弹药装备+军贸订单+央企背景", ("装备制造", "弹药装备"), "军工"),
    ),
)
def test_current_reason_terms_activate_reusable_business_rules(
    market_context,
    business_evidence,
    expected,
) -> None:
    decision = SmartSectorLibrary(
        date(2026, 9, 2),
        reviewed_attributions={},
    ).resolve(
        "600001.SH",
        market_context=market_context,
        business_evidence=business_evidence,
    )

    assert decision.category_name == expected


def test_dominant_real_business_beats_a_tiny_hot_side_business() -> None:
    gaoxin = _relationship_profile(
        "000628.SZ",
        "高新发展",
        primary_business_key="construction",
        primary_business_name="建筑工程",
        directory_category_key="construction",
        directory_category_name="建筑工程",
        business_tags=("建筑工程", "建筑施工", "功率半导体"),
        business_summary="建筑业，并兼营其他业务。",
        business_segments=(
            {
                "name": "建筑施工",
                "report_period": "2026-06-30",
                "revenue_cny": 93.6,
                "revenue_share": 0.936,
                "source": "fixture",
            },
            {
                "name": "功率半导体",
                "report_period": "2026-06-30",
                "revenue_cny": 1.1,
                "revenue_share": 0.011,
                "source": "fixture",
            },
        ),
    )

    decision = SmartSectorLibrary(
        date(2026, 9, 2),
        reviewed_attributions={},
    ).resolve_relationships((
        (
            "000628.SZ",
            "功率半导体+此前拟收购华鲲振宇股权+虚拟电厂",
            gaoxin,
        ),
    ))["000628.SZ"]

    assert decision.category_name == "房地产"
    assert decision.basis == "evidence_candidate_ranking"


def test_missing_market_context_still_abstains_instead_of_copying_a_business_label() -> None:
    relationship = _relationship_profile(
        "920212.BJ",
        "智新电子",
        primary_business_key="consumer_electronics",
        primary_business_name="消费电子",
        directory_category_key="consumer_electronics",
        directory_category_name="消费电子",
        business_tags=("消费电子", "消费电子连接器线缆组件"),
        business_summary="电子元器件和连接器的生产销售。",
        business_segments=(),
    )

    decision = SmartSectorLibrary(
        date(2026, 9, 2),
        reviewed_attributions={},
    ).resolve_relationships((("920212.BJ", None, relationship),))["920212.BJ"]

    assert decision.basis == "unresolved"
    assert decision.quality_flags == ("market_context_missing",)


@pytest.mark.parametrize(
    ("market_context", "business_evidence", "expected", "logic_type"),
    (
        ("光通信和光纤方向走强", ("A2光纤和光纤光缆",), "光纤", "direct_product_or_service"),
        ("数字人民币与跨境支付活跃", ("支付收单和数字身份认证",), "金融科技", "system_function"),
        ("航空装备和军工板块活跃", ("航空机轮刹车系统",), "军工", "downstream_demand"),
        ("苹果产业链和消费电子活跃", ("消费电子连接器组件",), "消费电子", "downstream_demand"),
        ("机器视觉与人形机器人活跃", ("机器人3D视觉光学镜头",), "物理AI", "system_function"),
        ("液冷服务器方向活跃", ("液冷泵订单开始交付",), "液冷", "commercialized_new_business"),
        ("农业机械和除草机活跃", ("智能激光除草机研发试验",), "农业", "downstream_demand"),
        ("锂盐和化工方向活跃", ("硫酸锂和碳酸锂生产",), "化工", "material_domain"),
        ("页岩气和油气板块活跃", ("页岩气勘探开发",), "油气", "direct_product_or_service"),
        ("垃圾焚烧发电方向活跃", ("垃圾焚烧发电和光伏发电量",), "电力", "operating_asset_or_output"),
        ("电网设备和特高压活跃", ("交联电力电缆和架空绝缘电缆",), "电力电网", "direct_product_or_service"),
        ("储能业务方向活跃", ("储能合同能源管理项目运营收入",), "储能", "commercialized_new_business"),
    ),
)
def test_confirmed_reasoning_is_reusable_without_stock_code_labels(
    market_context,
    business_evidence,
    expected,
    logic_type,
) -> None:
    match = infer_current_market_category(market_context, business_evidence)

    assert match is not None
    assert match.category_name == expected
    assert match.logic_type == logic_type
