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
    _events_not_after_source_snapshot,
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


def _pool_at(
    *,
    source_snapshot_revision: str,
    source_as_of: datetime,
) -> LimitUpPoolV2:
    return LimitUpPoolV2.create(
        source_snapshot_revision=source_snapshot_revision,
        source_snapshot_id=f"mw-{source_as_of:%Y%m%d-%H%M}",
        source_as_of=source_as_of,
        trade_date=TRADE_DATE,
        generated_at=source_as_of + timedelta(seconds=5),
        limit_event_provider="fixture",
        relationship_catalog_revision="b" * 64,
        quality="accepted",
        pool_total=1,
        catalog_matched_count=1,
        business_classified_count=1,
        unmatched_count=0,
        items=(_item(),),
    )


def test_limit_up_item_keeps_real_relationship_fields_beneath_the_display_category() -> None:
    item = _item()

    assert item.instrument_id == "001309.SZ"
    assert item.board_count == 2
    assert item.first_sealed_at == TARGET
    assert item.relationship_match_status == "matched"
    assert item.display_category_name is None
    assert item.display_category_basis == "unresolved"
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


def test_limit_up_events_cannot_leak_past_the_bound_source_snapshot() -> None:
    source_as_of = datetime(2026, 8, 31, 13, 28, 49, tzinfo=SHANGHAI)
    visible = _event("600001.SH", "已封板").model_copy(
        update={"first_sealed_at": time(13, 28, 49)}
    )
    future = _event("600002.SH", "午后新增").model_copy(
        update={"first_sealed_at": time(13, 29, 49)}
    )
    unresolved = _event("600003.SH", "封板时间缺失").model_copy(
        update={"first_sealed_at": None}
    )

    assert _events_not_after_source_snapshot(
        (visible, future, unresolved),
        source_as_of,
    ) == (visible,)


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
        ("unresolved_business", "主显示待核验", 1)
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
        ("人形机器人+汽车热管理", ("汽车零部件和精密零部件",), "机器人"),
        ("液冷油泵+机器人+汽车热管理", ("汽车油泵和热管理产品",), "机器人"),
        ("机器人+精密制造", ("商用车精密零件",), "机器人"),
        ("液冷服务器+家电制冷", ("制冷管路及配件",), "液冷"),
        ("液冷机柜+海外交付", ("自主液冷机柜和液冷配套结构件",), "液冷"),
        ("AIGC长剧+AI应用+微短剧", ("新媒体互动娱乐内容制作",), "传媒"),
        ("AIGC长剧+AI应用+微短剧", ("AIGC短剧内容生成和IP运营",), "AI应用"),
        ("AI教育+智能体+智慧招考", ("星空教育大模型和AI智能体平台",), "AI应用"),
        ("AI漫剧+AI应用+建筑装饰", ("AI漫剧和AIGC智能体",), "AI应用"),
        ("短剧出海+数字阅读", ("数字阅读平台和版权",), "传媒"),
        ("短剧出海+数字阅读", ("泡漫AI漫剧一站式生成平台",), "AI应用"),
        ("AI出版+教育服务+出版主业", ("图书期刊出版和新媒体出版",), "传媒"),
        ("文化传媒+文旅+AI创投", ("影视节目制作和广告运营",), "传媒"),
        (
            "AI应用+智慧广电+算力租赁",
            ("有线电视运营、IPTV集成播控和数据中心业务",),
            "传媒",
        ),
        (
            "中报扭亏+算力服务+中药",
            ("算力服务包括服务器组网和7×24小时运维",),
            "算力",
        ),
        (
            "AIGC短剧+管制药品+半年报增长",
            ("AIGC漫剧内容生成和IP运营",),
            "AI应用",
        ),
        (
            "微短剧+AI内容+现金储备",
            ("海看鲸灵AI影视创作平台和视听传媒智创大模型",),
            "AI应用",
        ),
        (
            "AI应用+MaaS平台+行业智能体",
            ("一站式MaaS服务平台和行业智能体",),
            "AI应用",
        ),
        (
            "机器人+边缘AI+AIoT芯片",
            ("无线通信芯片设计和边缘AI芯片",),
            "芯片",
        ),
        (
            "端侧AI+人形机器人+AI算力模组",
            ("AI算力模组和100TOPS以上智能模组",),
            "算力",
        ),
        (
            "物理AI+端侧AI+具身智能机器人",
            ("面向物理AI的AI模组+Agent，具备连接、感知、决策、执行能力",),
            "物理AI",
        ),
        (
            "端侧AI+智能模组",
            ("AI算力模组和100TOPS以上智能模组",),
            "算力",
        ),
        (
            "人形机器人+PTFE薄膜+PCB概念",
            ("特种高分子材料、PCB材料和PTFE薄膜",),
            "PCB",
        ),
        (
            "AI应用+IDC+MaaS平台+行业智能体",
            ("IDC数据中心业务、一站式MaaS服务平台和行业智能体",),
            "AI应用",
        ),
        (
            "算力大单+算力中心+集成灶",
            ("算力服务和智算中心建设运营",),
            "算力",
        ),
        (
            "海外算力资本开支带动燃机发电链",
            ("发电用大缸径柴油发动机核心零部件和燃气轮机发电机组",),
            "算力",
        ),
        ("房地产产业链+住宅装饰", ("住宅装饰设计及施工",), "房地产"),
        ("转基因玉米+种业", ("玉米种子和品种研发",), "农业"),
        ("化肥+煤气化", ("化肥和煤炭",), "化肥"),
        ("端侧AI+AIoT芯片", ("芯片设计和智能应用处理器芯片",), "芯片"),
        ("影视院线+重点影片", ("影院及院线和电影",), "传媒"),
        ("院线经营+IP商业化", ("电影发行和院线",), "传媒"),
        ("游戏+短剧", ("网络游戏研发发行运营",), "游戏"),
        ("券商+大金融", ("综合性证券公司和财富管理类业务",), "证券"),
        ("CPO+高速光通信", ("空间激光通信光学元组件",), "CPO"),
        ("中药制造+国企背景", ("中药制剂和药品流通",), "医疗医药"),
        ("跨境电商+童装品牌", ("童装品牌运营和儿童服饰",), "消费"),
        ("包装一体化+消费电子包装", ("包装一体化和精品包装",), "消费"),
        (
            "物理AI+拟收购汽车智能控制系统",
            ("换挡操纵控制系统、汽车组合开关和车辆智能控制软件",),
            None,
        ),
        ("汽车电子+汽车维修保养设备", ("汽车维修保养设备和汽车配套零部件",), "汽车"),
        ("焦炭+烯烃+循环化工", ("焦炭和煤焦业务",), "煤炭"),
        ("新零售+连锁零售", ("百货超市零售",), "消费"),
        ("木作转型+高端全屋定制", ("家居定制和家具",), "消费"),
        ("算力储能+在手订单", ("电力和清洁环保能源装备",), "储能"),
        ("六氟磷酸锂+铝基新材", ("六氟磷酸锂和铝基材料",), "六氟磷酸锂"),
        ("绿色电力+垃圾焚烧", ("垃圾处理及发电",), "电力"),
        ("工业母机+五轴机床", ("数控机床研发制造和销售",), "工业母机"),
        ("算力+IDC", ("IDC机房服务",), "算力"),
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


def test_kylinsec_manual_review_uses_ai_application_without_rewriting_its_business() -> None:
    reviewed = load_reviewed_market_attributions(date(2026, 8, 28))

    assert reviewed["688152.SH"].category_name == "AI应用"
    assert reviewed["688152.SH"].rule_id == "ai_application_from_deployed_product"


def test_third_manual_review_round_preserves_reviewed_display_semantics() -> None:
    reviewed = load_reviewed_market_attributions(date(2026, 8, 31))

    assert {
        instrument_id: reviewed[instrument_id].category_name
        for instrument_id in (
            "601330.SH",
            "603533.SH",
            "600551.SH",
            "000917.SZ",
            "603139.SH",
            "601929.SH",
            "603058.SH",
            "301262.SZ",
            "603068.SH",
            "002757.SZ",
            "300911.SZ",
            "603950.SH",
            "603236.SH",
            "603090.SH",
            "603559.SH",
            "001330.SZ",
            "003018.SZ",
            "002881.SZ",
            "000977.SZ",
            "603001.SH",
            "603297.SH",
            "601136.SH",
            "600892.SH",
            "601595.SH",
            "603721.SH",
            "601882.SH",
            "600671.SH",
            "002875.SZ",
            "002303.SZ",
            "603201.SH",
            "603113.SH",
        )
    } == {
        "601330.SH": "电力",
        "603533.SH": "AI应用",
        "600551.SH": "传媒",
        "000917.SZ": "传媒",
        "603139.SH": "算力",
        "601929.SH": "传媒",
        "603058.SH": "AI应用",
        "301262.SZ": "AI应用",
        "603068.SH": "芯片",
        "002757.SZ": "AI应用",
        "300911.SZ": "算力",
        "603950.SH": "算力",
        "603236.SH": "物理AI",
        "603090.SH": "液冷",
        "603559.SH": "算力",
        "001330.SZ": "AI应用",
        "003018.SZ": "液冷",
        "002881.SZ": "物理AI",
        "000977.SZ": "交换机",
        "603001.SH": "消费",
        "603297.SH": "CPO",
        "601136.SH": "证券",
        "600892.SH": "游戏",
        "601595.SH": "传媒",
        "603721.SH": "AI应用",
        "601882.SH": "机器人",
        "600671.SH": "医疗医药",
        "002875.SZ": "消费",
        "002303.SZ": "消费",
        "603201.SH": "物理AI",
        "603113.SH": "煤炭",
    }
    assert reviewed["603201.SH"].review_basis == "human_review_override"
    assert reviewed["603201.SH"].rule_id == "manual_market_review_override"
    assert load_reviewed_market_attributions(date(2026, 9, 1)) == {}


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


def test_limit_up_pool_store_reads_latest_same_day_pool_not_after_snapshot(
    tmp_path,
) -> None:
    path = tmp_path / "limit-up.sqlite3"
    earlier_as_of = TARGET - timedelta(minutes=1)
    earlier = _pool_at(
        source_snapshot_revision="c" * 64,
        source_as_of=earlier_as_of,
    )
    latest = _pool_at(
        source_snapshot_revision="d" * 64,
        source_as_of=TARGET,
    )
    with LimitUpPoolStore(path) as store:
        store.record(earlier)
        store.record(latest)

    with LimitUpPoolStore(path, read_only=True) as store:
        assert store.get_latest_for_trade_date(
            TRADE_DATE,
            not_after=earlier_as_of + timedelta(seconds=30),
        ) == earlier
        assert store.get_latest_for_trade_date(
            TRADE_DATE,
            not_after=TARGET + timedelta(seconds=30),
        ) == latest
        assert store.get_latest_for_trade_date(
            TRADE_DATE,
            not_after=earlier_as_of - timedelta(seconds=1),
        ) is None
