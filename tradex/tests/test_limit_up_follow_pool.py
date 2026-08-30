from __future__ import annotations

import json
import sqlite3
import zlib
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from tradex.data_gateway.contracts import LimitUpEventV1
from tradex.instrument_taxonomy.contracts import StockRelationshipProfileV1
from tradex.market_watch.integrity import stable_sha256
from tradex.market_watch.limit_up_pool import (
    LimitUpPoolItemV2,
    LimitUpPoolV2,
    _pool_items,
)
from tradex.market_watch.limit_up_pool_store import LimitUpPoolStore
from tradex.market_watch.market_theme_attribution import (
    infer_current_market_category,
    load_reviewed_market_attributions,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 8, 27)
TARGET = datetime(2026, 8, 27, 14, 57, tzinfo=SHANGHAI)


def _event(
    instrument_id: str = "001309.SZ",
    name: str = "德明利",
) -> LimitUpEventV1:
    return LimitUpEventV1(
        instrument_id=instrument_id,
        name=name,
        reason="供应商题材不得参与真实归属",
        limit_up_type="换手板",
        board_label="2连板",
        board_count=2,
        first_sealed_at=time(14, 57),
    )


def _relationship(
    instrument_id: str = "001309.SZ",
    name: str = "德明利",
) -> StockRelationshipProfileV1:
    return StockRelationshipProfileV1(
        instrument_id=instrument_id,
        name=name,
        as_of=TRADE_DATE,
        statistical_industry={
            "taxonomy": "sw",
            "taxonomy_version": "2021",
            "level3_code": "270103",
            "level3_name": "数字芯片设计",
            "source": "fixture",
        },
        provider_industry="半导体",
        business_domain_key="chips",
        business_domain_name="芯片",
        directory_category_key="chips",
        directory_category_name="芯片",
        primary_business_key="memory",
        primary_business_name="存储",
        business_tags=("存储", "存储芯片"),
        verification_status="provider_only",
        flags=("official_review_pending",),
    )


def _item() -> LimitUpPoolItemV2:
    return _pool_items(
        (_event(),),
        {"001309.SZ": _relationship()},
        trade_date=TRADE_DATE,
        tzinfo=SHANGHAI,
    )[0]


def _pool(item: LimitUpPoolItemV2) -> LimitUpPoolV2:
    return LimitUpPoolV2.create(
        source_snapshot_revision="a" * 64,
        source_snapshot_id="mw-20260827-1457",
        source_as_of=TARGET,
        trade_date=TRADE_DATE,
        generated_at=TARGET + timedelta(seconds=5),
        limit_event_provider="fixture",
        relationship_catalog_revision="b" * 64,
        quality="accepted",
        pool_total=1,
        catalog_matched_count=1,
        business_classified_count=1,
        unmatched_count=0,
        items=(item,),
    )


def test_limit_up_item_keeps_real_relationship_fields_beneath_the_display_category() -> None:
    item = _item()

    assert item.instrument_id == "001309.SZ"
    assert item.board_count == 2
    assert item.first_sealed_at == TARGET
    assert item.relationship_match_status == "matched"
    assert item.display_category_name == "芯片"
    assert item.display_category_basis == "relationship_directory"
    assert item.display_category_effective_on == TRADE_DATE
    assert item.business_domain_name == "芯片"
    assert item.directory_category_name == "芯片"
    assert item.primary_business_name == "存储"
    assert item.business_tags == ("存储", "存储芯片")
    assert item.statistical_industry_name == "数字芯片设计"
    assert item.relationship_verification_status == "provider_only"
    assert "source_reason" not in LimitUpPoolItemV2.model_fields
    assert "follow_status" not in LimitUpPoolItemV2.model_fields
    assert "followed_sector_name" not in LimitUpPoolItemV2.model_fields
    assert "evidence" not in LimitUpPoolItemV2.model_fields


def test_unmatched_stock_stays_explicit_and_cannot_carry_guessed_tags() -> None:
    event = _event("600001.SH", "目录缺失")
    item = _pool_items(
        (event,),
        {},
        trade_date=TRADE_DATE,
        tzinfo=SHANGHAI,
    )[0]

    assert item.relationship_match_status == "unmatched"
    assert item.display_category_name is None
    assert item.display_category_basis == "unresolved"
    assert item.primary_business_name is None
    assert item.business_tags == ()
    assert item.statistical_industry_name is None
    assert item.relationship_flags == ("stock_relationship_unavailable",)
    with pytest.raises(ValidationError):
        LimitUpPoolItemV2.model_validate({
            **item.model_dump(mode="json"),
            "business_tags": ["供应商猜测题材"],
        })


def test_v2_contract_rejects_removed_analysis_fields() -> None:
    item = _item()

    with pytest.raises(ValidationError):
        LimitUpPoolItemV2.model_validate({
            **item.model_dump(mode="json"),
            "follow_status": "confirmed",
        })


def test_limit_up_pool_groups_by_display_category_and_preserves_primary_business_leaf() -> None:
    item = _item()
    pool = _pool(item)

    assert pool.contract == "limit_up_pool.v2"
    assert pool.catalog_matched_count == 1
    assert pool.business_classified_count == 1
    assert pool.unmatched_count == 0
    assert [(entry.business_key, entry.label, entry.count) for entry in pool.categories] == [
        ("chips", "芯片", 1)
    ]
    assert pool.pool_revision == stable_sha256(
        pool.model_dump(mode="json", exclude={"pool_revision"})
    )


def test_limit_up_pool_uses_specific_display_categories_instead_of_broad_domains() -> None:
    first = _item().model_copy(update={
        "instrument_id": "600722.SH",
        "name": "金牛化工",
        "business_domain_key": "chemicals",
        "business_domain_name": "化工",
        "directory_category_key": "disclosed_4d9eb814c78d",
        "directory_category_name": "化工",
        "primary_business_key": "disclosed_4d9eb814c78d",
        "primary_business_name": "化工",
        "business_tags": ("化工",),
        "display_category_key": "pcb",
        "display_category_name": "PCB",
        "display_category_basis": "manual_market_review",
    })
    second = _item().model_copy(update={
        "instrument_id": "600929.SH",
        "name": "雪天盐业",
        "business_domain_key": "chemicals",
        "business_domain_name": "化工",
        "directory_category_key": "chemical",
        "directory_category_name": "化工",
        "primary_business_key": "chemical",
        "primary_business_name": "化工",
        "business_tags": ("化工", "纯碱"),
        "display_category_key": "liquid_cooling",
        "display_category_name": "液冷",
        "display_category_basis": "manual_market_review",
    })
    uncategorized = _item().model_copy(update={
        "instrument_id": "600001.SH",
        "name": "待归大类样例",
        "business_domain_key": None,
        "business_domain_name": None,
        "directory_category_key": "disclosed_leaf",
        "directory_category_name": "财务分部细类",
        "primary_business_key": "disclosed_leaf",
        "primary_business_name": "财务分部细类",
        "business_tags": ("财务分部细类",),
        "display_category_key": None,
        "display_category_name": None,
        "display_category_basis": "unresolved",
    })

    pool = LimitUpPoolV2.create(
        source_snapshot_revision="c" * 64,
        source_snapshot_id="mw-20260827-1458",
        source_as_of=TARGET,
        trade_date=TRADE_DATE,
        generated_at=TARGET + timedelta(seconds=5),
        limit_event_provider="fixture",
        relationship_catalog_revision="d" * 64,
        quality="accepted",
        pool_total=3,
        catalog_matched_count=3,
        business_classified_count=3,
        unmatched_count=0,
        items=(first, second, uncategorized),
    )

    assert [(entry.business_key, entry.label, entry.count) for entry in pool.categories] == [
        ("pcb", "PCB", 1),
        ("liquid_cooling", "液冷", 1),
        ("unresolved_business", "主显示待核验", 1),
    ]


@pytest.mark.parametrize(
    ("reason", "business_evidence", "expected"),
    (
        ("黄金概念继续活跃", ("黄金珠宝首饰零售",), "贵金属"),
        ("数据中心液冷+核电冷却", ("数据中心液冷配套冷却塔",), "液冷"),
        ("算力+IDC+家居商贸", ("互联网数据中心业务协议",), "算力"),
        ("PCB层压机+高端成形机床", ("用于CCL、PCB的层压设备",), "PCB"),
        ("AI营销概念走高", ("AI营销和AI应用分发",), "AI应用"),
        ("超节点+算力网络", ("数据中心800G交换机",), "交换机"),
        ("AI应用+工业软件", ("AI智能体和企业智能运行空间EIOSpace",), "AI应用"),
        ("算力租赁反弹", ("算力出租和智算中心托管运营",), "算力"),
        ("特高压+电网设备", ("1000kV输电线路角钢塔",), "电力电网"),
        ("黄金和小金属走强", ("触头材料和贵金属回收",), "小金属"),
        ("安防+AI算力", ("1000P人工智能算力中心设备采购及运营",), "算力"),
        ("算力概念走高", ("房地产开发和家居商贸",), None),
        ("PCB概念走高", ("色选机和液压机",), None),
        ("超节点概念走高", ("企业管理软件",), None),
        ("", ("互联网数据中心业务协议",), None),
    ),
)
def test_current_market_theme_requires_both_market_and_business_evidence(
    reason,
    business_evidence,
    expected,
) -> None:
    result = infer_current_market_category(reason, business_evidence)
    assert (result.category_name if result else None) == expected


def test_second_manual_review_round_is_exact_date_and_preserves_requested_granularity() -> None:
    reviewed = load_reviewed_market_attributions(date(2026, 8, 28))

    assert {
        instrument_id: reviewed[instrument_id].category_name
        for instrument_id in (
            "002396.SZ",
            "300378.SZ",
            "002229.SZ",
            "601700.SH",
            "603045.SH",
            "600654.SH",
        )
    } == {
        "002396.SZ": "交换机",
        "300378.SZ": "AI应用",
        "002229.SZ": "算力",
        "601700.SH": "电力电网",
        "603045.SH": "小金属",
        "600654.SH": "算力",
    }
    assert load_reviewed_market_attributions(date(2026, 8, 29)) == {}


def test_exact_date_manual_review_can_override_the_stable_relationship_directory() -> None:
    trade_date = date(2026, 8, 28)
    target = datetime(2026, 8, 28, 10, 2, tzinfo=SHANGHAI)
    event = LimitUpEventV1(
        instrument_id="600162.SH",
        name="香江控股",
        reason="算力+IDC+家居商贸",
        board_count=2,
        first_sealed_at=time(10, 2),
    )
    relationship = StockRelationshipProfileV1(
        instrument_id="600162.SH",
        name="香江控股",
        as_of=trade_date,
        business_domain_key="real_estate",
        business_domain_name="房地产",
        directory_category_key="commercial_real_estate_operations",
        directory_category_name="商贸流通运营",
        primary_business_key="commercial_real_estate_operations",
        primary_business_name="商贸流通运营",
        business_tags=("商贸流通运营", "互联网数据中心业务", "IDC"),
        business_summary="房地产开发、家居商贸和互联网数据中心业务",
        verification_status="provider_only",
    )

    item = _pool_items(
        (event,),
        {"600162.SH": relationship},
        trade_date=trade_date,
        tzinfo=SHANGHAI,
    )[0]

    assert item.display_category_name == "算力"
    assert item.display_category_basis == "manual_market_review"
    assert item.directory_category_name == "商贸流通运营"
    assert item.primary_business_name == "商贸流通运营"
    assert item.first_sealed_at == target


def test_limit_up_pool_store_round_trips_v2_and_ignores_legacy_payload(tmp_path) -> None:
    pool = _pool(_item())
    path = tmp_path / "limit-up.sqlite3"

    with LimitUpPoolStore(path) as store:
        assert store.record(pool)["action"] == "inserted"
        assert store.record(pool)["action"] == "unchanged"
    with LimitUpPoolStore(path, read_only=True) as store:
        assert store.get_by_source_revision("a" * 64) == pool

    legacy_payload = {
        "contract": "limit_up_follow_pool.v1",
        "schema_version": 1,
        "source_snapshot_revision": "c" * 64,
    }
    legacy_json = json.dumps(legacy_payload, separators=(",", ":")).encode("utf-8")
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO limit_up_follow_pools (
                source_snapshot_revision, attribution_revision,
                source_snapshot_id, source_as_of, trade_date,
                generated_at, pool_total, payload_digest, payload_blob
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "c" * 64,
                "d" * 64,
                "legacy",
                TARGET.isoformat(),
                TRADE_DATE.isoformat(),
                TARGET.isoformat(),
                0,
                stable_sha256(legacy_payload),
                zlib.compress(legacy_json),
            ),
        )
    with LimitUpPoolStore(path, read_only=True) as store:
        assert store.get_by_source_revision("c" * 64) is None
