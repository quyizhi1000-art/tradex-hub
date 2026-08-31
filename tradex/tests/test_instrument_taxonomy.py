from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tradex.instrument_taxonomy.builder import (
    build_stock_relationship_catalog,
    load_official_evidence,
)
from tradex.instrument_taxonomy.normalization import (
    BUSINESS_DOMAINS,
    BUSINESS_RULES,
    business_domain_by_business_key,
    matched_business_rules,
    reviewed_directory_category,
)
from tradex.instrument_taxonomy.service import InstrumentTaxonomyService
from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader, InstrumentTaxonomyStore


SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_second_market_review_round_keeps_verified_business_identity_separate() -> None:
    evidence = load_official_evidence()

    assert {
        instrument_id: (
            evidence[instrument_id]["primary_business_name"],
            expected_tag in evidence[instrument_id]["business_tags"],
        )
        for instrument_id, expected_tag in {
            "002396.SZ": "数据中心交换机",
            "300378.SZ": "AI智能体",
            "002229.SZ": "算力及相关服务",
            "601700.SH": "输电线路铁塔",
            "603045.SH": "贵金属回收",
            "600654.SH": "人工智能算力中心",
            "688152.SH": "智能运维",
            "003005.SZ": "星空AI智能体平台",
            "002328.SZ": "自主液冷机柜",
            "605287.SH": "AI漫剧",
            "603533.SH": "泡漫AI漫剧一站式生成平台",
            "600551.SH": "图书期刊出版",
            "000917.SZ": "广播电视节目传送",
            "603139.SH": "算力服务",
            "601929.SH": "IPTV集成播控",
            "603058.SH": "AIGC漫剧",
            "301262.SZ": "海看鲸灵AI影视创作平台",
            "603068.SH": "边缘AI芯片",
            "002757.SZ": "行业智能体",
            "300911.SZ": "亿田算力中心",
            "603950.SH": "燃气轮机发电机组",
            "603236.SH": "机器人大小脑域控制器",
            "603090.SH": "数据中心散热",
            "603559.SH": "IDC运维及增值服务",
            "001330.SZ": "博卡云平台",
            "003018.SZ": "液冷组件",
            "002881.SZ": "0.5TOPS到700TOPS算力模组",
            "000977.SZ": "X400超级AI以太网交换机",
            "603001.SH": "皮鞋",
            "603297.SH": "空间激光通信光学元组件",
            "601136.SH": "财富管理类业务",
            "600892.SH": "网络游戏",
            "601595.SH": "电影发行",
            "603721.SH": "AI智审",
            "601882.SH": "高端数控机床",
            "600671.SH": "中药制剂",
            "002875.SZ": "童装",
            "002303.SZ": "包装一体化服务",
            "603201.SH": "汽车维修保养设备",
            "603113.SH": "焦炭",
        }.items()
    } == {
        "002396.SZ": ("通信设备", True),
        "300378.SZ": ("数智技术服务", True),
        "002229.SZ": ("票证", True),
        "601700.SH": ("角钢塔", True),
        "603045.SH": ("触头材料", True),
        "600654.SH": ("网络安全", True),
        "688152.SH": ("操作系统", True),
        "003005.SZ": ("智慧教育", True),
        "002328.SZ": ("汽车零部件", True),
        "605287.SH": ("建筑装饰", True),
        "603533.SH": ("数字内容", True),
        "600551.SH": ("出版传媒", True),
        "000917.SZ": ("综合传媒", True),
        "603139.SH": ("中成药", True),
        "601929.SH": ("广电传媒", True),
        "603058.SH": ("精品纸包装", True),
        "301262.SZ": ("IPTV与视听内容", True),
        "603068.SH": ("无线通信芯片", True),
        "002757.SZ": ("数字基础设施", True),
        "300911.SZ": ("厨房电器", True),
        "603950.SH": ("发动机零部件", True),
        "603236.SH": ("无线通信模组", True),
        "603090.SH": ("换热器与热管理", True),
        "603559.SH": ("通信技术服务", True),
        "001330.SZ": ("影视与院线", True),
        "003018.SZ": ("包装制品与液冷散热", True),
        "002881.SZ": ("无线通信模组及解决方案", True),
        "000977.SZ": ("服务器及人工智能计算系统", True),
        "603001.SH": ("鞋履品牌与零售", True),
        "603297.SH": ("精密光学元组件与显微镜", True),
        "601136.SH": ("综合证券服务", True),
        "600892.SH": ("游戏与影视", True),
        "601595.SH": ("电影发行与院线", True),
        "603721.SH": ("视频内容制作与运营", True),
        "601882.SH": ("高端数控机床", True),
        "600671.SH": ("中药制剂与医药流通", True),
        "002875.SZ": ("童装品牌运营", True),
        "002303.SZ": ("包装一体化服务", True),
        "603201.SH": ("汽车维修保养设备与零部件", True),
        "603113.SH": ("焦炭与循环化工", True),
    }


def test_every_controlled_business_leaf_has_one_top_level_domain() -> None:
    memberships = {
        rule.key: [
            domain.name
            for domain in BUSINESS_DOMAINS
            if rule.key in domain.business_keys
        ]
        for rule in BUSINESS_RULES
    }

    assert all(len(domains) == 1 for domains in memberships.values()), memberships
    assert business_domain_by_business_key("food_beverage").name == "消费"
    assert business_domain_by_business_key("chemical").name == "化工"


@pytest.mark.parametrize(
    ("disclosed_leaf", "expected_key"),
    (
        ("冲饮系列", "food_beverage"),
        ("化工", "chemical"),
    ),
)
def test_common_disclosed_leaf_names_enter_the_controlled_vocabulary(
    disclosed_leaf,
    expected_key,
) -> None:
    assert matched_business_rules((disclosed_leaf,))[0].key == expected_key


def _source() -> dict:
    return {
        "contract": "instrument_taxonomy_source_bundle.v1",
        "schema_version": 1,
        "as_of": "2026-08-27",
        "source_providers": ["tushare"],
        "source_request_ids": ["req-1"],
        "flags": [],
        "stocks": [
            {"ts_code": "001309.SZ", "name": "德明利", "industry": "半导体"},
            {"ts_code": "002916.SZ", "name": "深南电路", "industry": "元件"},
            {"ts_code": "601869.SH", "name": "长飞光纤", "industry": "通信设备"},
            {"ts_code": "600000.SH", "name": "无主营样例", "industry": "银行"},
        ],
        "companies": [
            {
                "ts_code": "001309.SZ",
                "main_business": "存储控制芯片、固态硬盘、嵌入式存储和内存条",
            },
            {
                "ts_code": "002916.SZ",
                "main_business": "印制电路板、封装基板和电子装联",
            },
            {
                "ts_code": "601869.SH",
                "main_business": "光纤预制棒、光纤和光缆",
            },
        ],
        "sw_memberships": [
            {
                "ts_code": "001309.SZ",
                "l1_code": "801080.SI",
                "l1_name": "电子",
                "l2_code": "801081.SI",
                "l2_name": "半导体",
                "l3_code": "850814.SI",
                "l3_name": "数字芯片设计",
                "in_date": "20240730",
                "out_date": None,
            },
            {
                "ts_code": "002916.SZ",
                "l1_code": "801080.SI",
                "l1_name": "电子",
                "l2_code": "801083.SI",
                "l2_name": "元件",
                "l3_code": "850822.SI",
                "l3_name": "印制电路板",
                "in_date": "20171208",
                "out_date": None,
            },
        ],
        "business_segments": [
            {"ts_code": "001309.SZ", "end_date": "20251231", "bz_item": "固态硬盘类产品", "bz_sales": 45.0},
            {"ts_code": "001309.SZ", "end_date": "20251231", "bz_item": "嵌入式存储类产品", "bz_sales": 36.0},
            {"ts_code": "002916.SZ", "end_date": "20251231", "bz_item": "印制电路板", "bz_sales": 143.0},
            {"ts_code": "002916.SZ", "end_date": "20251231", "bz_item": "封装基板", "bz_sales": 41.0},
            {"ts_code": "601869.SH", "end_date": "20260630", "bz_item": "光传输产品分部", "bz_sales": 61.0},
        ],
    }


def test_builds_typed_verified_profiles_without_promoting_unverified_abf():
    status, profiles = build_stock_relationship_catalog(
        _source(),
        generated_at=datetime(2026, 8, 28, 1, 0, tzinfo=SHANGHAI),
    )
    by_id = {item.instrument_id: item for item in profiles}

    assert status.profile_total == 4
    assert by_id["001309.SZ"].business_domain_name == "芯片"
    assert by_id["001309.SZ"].directory_category_name == "存储"
    assert by_id["001309.SZ"].primary_business_name == "存储"
    assert by_id["001309.SZ"].statistical_industry.level3_name == "数字芯片设计"
    assert by_id["002916.SZ"].primary_business_name == "PCB"
    assert "封装基板" in by_id["002916.SZ"].business_tags
    assert "FC-BGA" in by_id["002916.SZ"].business_tags
    assert "ABF" not in by_id["002916.SZ"].business_tags
    assert "abf_unverified" in by_id["002916.SZ"].flags
    assert by_id["601869.SH"].primary_business_name == "光纤光缆"
    assert by_id["600000.SH"].verification_status == "unresolved"
    assert by_id["600000.SH"].primary_business_name is None


def test_store_atomically_round_trips_and_queries_relationship_basis(tmp_path):
    status, profiles = build_stock_relationship_catalog(
        _source(),
        generated_at=datetime(2026, 8, 28, 1, 0, tzinfo=SHANGHAI),
    )
    db_path = tmp_path / "taxonomy.sqlite3"
    with InstrumentTaxonomyStore(db_path) as store:
        store.replace_catalog(status, profiles)

    with InstrumentTaxonomyReader(db_path) as reader:
        assert reader.status() == status
        assert reader.get("001309.SZ").primary_business_name == "存储"
        assert [item.instrument_id for item in reader.members_by_sw_l3("850822.SI")] == [
            "002916.SZ"
        ]
        assert [item.instrument_id for item in reader.members_by_primary_business("存储")] == [
            "001309.SZ"
        ]


def test_refresh_preserves_prior_catalog_when_latest_business_period_is_missing(tmp_path):
    initial_status, initial_profiles = build_stock_relationship_catalog(
        _source(),
        generated_at=datetime(2026, 8, 28, 1, 0, tzinfo=SHANGHAI),
    )
    db_path = tmp_path / "taxonomy.sqlite3"
    with InstrumentTaxonomyStore(db_path) as store:
        store.replace_catalog(initial_status, initial_profiles)
        degraded_source = {
            **_source(),
            "reporting_periods": ["20251231", "20250630"],
            "flags": ["business_segments_unavailable:20251231:RuntimeError"],
        }
        service = InstrumentTaxonomyService(store)
        with pytest.raises(RuntimeError, match="latest business segments are unavailable"):
            service.refresh(
                as_of="2026-08-29",
                source_loader=lambda _as_of: degraded_source,
            )

        with InstrumentTaxonomyReader(db_path) as reader:
            assert reader.status() == initial_status


def test_current_segments_and_official_evidence_prevent_broad_keyword_promotions():
    source = {
        "contract": "instrument_taxonomy_source_bundle.v1",
        "schema_version": 1,
        "as_of": "2026-08-29",
        "source_providers": ["tushare"],
        "source_request_ids": ["req-dominance"],
        "flags": [],
        "stocks": [
            {"ts_code": "000829.SZ", "name": "天音控股", "industry": "其他商业"},
            {"ts_code": "300475.SZ", "name": "香农芯创", "industry": "元器件"},
            {"ts_code": "000333.SZ", "name": "美的集团", "industry": "家用电器"},
            {"ts_code": "601012.SH", "name": "隆基绿能", "industry": "电气设备"},
            {"ts_code": "600257.SH", "name": "大湖股份", "industry": "渔业"},
            {"ts_code": "600645.SH", "name": "中源协和", "industry": "医疗保健"},
            {"ts_code": "688347.SH", "name": "华虹宏力", "industry": "半导体"},
        ],
        "companies": [
            {
                "ts_code": "000829.SZ",
                "main_business": "通信产品销售、零售电商、彩票和白酒产品销售",
            },
            {
                "ts_code": "300475.SZ",
                "main_business": "电子元器件分销、海普存储产品和减速器业务",
            },
            {
                "ts_code": "000333.SZ",
                "main_business": "智能家居以及商业和工业解决方案",
            },
            {
                "ts_code": "601012.SH",
                "main_business": "全球光伏企业，主要从事单晶硅片和电池组件业务",
            },
            {
                "ts_code": "600257.SH",
                "main_business": "健康医疗、水产品和白酒产品业务",
            },
            {
                "ts_code": "600645.SH",
                "main_business": "检测试剂、细胞检测制备及存储和科研试剂",
            },
            {
                "ts_code": "688347.SH",
                "main_business": "集成电路晶圆代工，覆盖嵌入式存储等特色工艺",
            },
        ],
        "sw_memberships": [],
        "business_segments": [
            {
                "ts_code": "000829.SZ",
                "end_date": "20251231",
                "bz_item": "通信产品销售",
                "bz_sales": 56_515_723_804.08,
            },
            {
                "ts_code": "000829.SZ",
                "end_date": "20251231",
                "bz_item": "零售电商",
                "bz_sales": 31_789_676_836.90,
            },
            {
                "ts_code": "300475.SZ",
                "end_date": "20251231",
                "bz_item": "电子元器件分销",
                "bz_sales": 33_222_929_646.02,
            },
            {
                "ts_code": "300475.SZ",
                "end_date": "20251231",
                "bz_item": "海普存储",
                "bz_sales": 1_658_312_650.23,
            },
            {
                "ts_code": "300475.SZ",
                "end_date": "20251231",
                "bz_item": "减速器",
                "bz_sales": 342_076_156.05,
            },
            {
                "ts_code": "000333.SZ",
                "end_date": "20251231",
                "bz_item": "智能家居",
                "bz_sales": 100_000_000_000.0,
            },
            {
                "ts_code": "000333.SZ",
                "end_date": "20251231",
                "bz_item": "机器人与自动化",
                "bz_sales": 30_000_000_000.0,
            },
            {
                "ts_code": "601012.SH",
                "end_date": "20251231",
                "bz_item": "组件及电池",
                "bz_sales": 59_920_375_950.77,
            },
            {
                "ts_code": "601012.SH",
                "end_date": "20251231",
                "bz_item": "硅片及硅棒",
                "bz_sales": 6_539_863_524.01,
            },
            {
                "ts_code": "600257.SH",
                "end_date": "20251231",
                "bz_item": "医疗服务",
                "bz_sales": 212_552_806.08,
            },
            {
                "ts_code": "600257.SH",
                "end_date": "20251231",
                "bz_item": "白酒",
                "bz_sales": 29_383_983.59,
            },
            {
                "ts_code": "600645.SH",
                "end_date": "20251231",
                "bz_item": "检测试剂",
                "bz_sales": 858_031_749.0,
            },
            {
                "ts_code": "600645.SH",
                "end_date": "20251231",
                "bz_item": "细胞检测制备及存储",
                "bz_sales": 367_821_830.72,
            },
            {
                "ts_code": "688347.SH",
                "end_date": "20251231",
                "bz_item": "集成电路晶圆代工",
                "bz_sales": 9_160_589_039.81,
            },
        ],
    }

    _status, profiles = build_stock_relationship_catalog(
        source,
        generated_at=datetime(2026, 8, 29, 12, 0, tzinfo=SHANGHAI),
    )
    by_id = {item.instrument_id: item for item in profiles}

    tianyin = by_id["000829.SZ"]
    assert tianyin.primary_business_name == "电子分销"
    assert "白酒" not in tianyin.business_tags
    assert "零售电商" in tianyin.business_tags
    assert tianyin.business_segments[0].revenue_share > 0.6

    shannon = by_id["300475.SZ"]
    assert shannon.primary_business_name == "存储"
    assert shannon.verification_status == "verified"
    assert "存储器件分销" in shannon.business_tags
    assert shannon.business_segments[0].revenue_share > 0.9

    midea = by_id["000333.SZ"]
    assert midea.primary_business_name == "家用电器"
    assert "机器人与自动化" in midea.business_tags

    assert by_id["601012.SH"].primary_business_name == "光伏"
    assert "半导体材料" not in by_id["601012.SH"].business_tags
    assert by_id["600257.SH"].primary_business_name == "医疗服务"
    assert "白酒" in by_id["600257.SH"].business_tags
    assert by_id["600645.SH"].primary_business_name == "医疗器械"
    assert "存储" not in by_id["600645.SH"].business_tags
    assert by_id["688347.SH"].primary_business_name == "晶圆制造"
    assert "存储" in by_id["688347.SH"].business_tags


def test_dominant_recognized_segment_keeps_the_controlled_business_name():
    source = {
        "contract": "instrument_taxonomy_source_bundle.v1",
        "schema_version": 1,
        "as_of": "2026-08-29",
        "source_providers": ["tushare"],
        "source_request_ids": ["req-controlled"],
        "flags": [],
        "stocks": [
            {"ts_code": "000001.SZ", "name": "主营样例", "industry": "半导体"},
        ],
        "companies": [
            {"ts_code": "000001.SZ", "main_business": "固态硬盘和其他电子产品"},
        ],
        "sw_memberships": [],
        "business_segments": [
            {
                "ts_code": "000001.SZ",
                "end_date": "20251231",
                "bz_item": "固态硬盘类产品",
                "bz_sales": 800.0,
            },
            {
                "ts_code": "000001.SZ",
                "end_date": "20251231",
                "bz_item": "其他电子产品",
                "bz_sales": 200.0,
            },
        ],
    }

    _status, profiles = build_stock_relationship_catalog(
        source,
        generated_at=datetime(2026, 8, 29, 12, 0, tzinfo=SHANGHAI),
    )

    assert profiles[0].primary_business_key == "storage"
    assert profiles[0].primary_business_name == "存储"


def test_hot_pool_regressions_prefer_current_specific_business_evidence():
    cases = (
        ("900001.SH", "珠宝样例", "黄金珠宝及自行车业务", "金银珠宝", "贵金属"),
        ("900002.SH", "氟材料样例", "高端氟材料及工程技术服务", "高端氟材料", "氟化工"),
        ("900005.SH", "纺织样例", "纺织品进出口贸易", "纺织品", "纺织"),
        ("900006.SH", "分销样例", "电子元器件交易服务", "电子元器件交易", "电子分销"),
        ("900013.SH", "高分子样例", "特种高分子材料研发和销售", "特种高分子材料", "特种高分子材料"),
    )
    source = {
        "contract": "instrument_taxonomy_source_bundle.v1",
        "schema_version": 1,
        "as_of": "2026-08-29",
        "source_providers": ["tushare"],
        "source_request_ids": ["req-hot-pool-regressions"],
        "flags": [],
        "stocks": [
            {"ts_code": instrument_id, "name": name, "industry": "样例"}
            for instrument_id, name, _summary, _segment, _expected in cases
        ] + [
            {"ts_code": "000560.SZ", "name": "我爱我家", "industry": "房地产"},
            {"ts_code": "600479.SH", "name": "千金药业", "industry": "医药"},
            {"ts_code": "002742.SZ", "name": "冀衡医药", "industry": "医药"},
            {"ts_code": "600689.SH", "name": "上海三毛", "industry": "综合"},
            {"ts_code": "000062.SZ", "name": "深圳华强", "industry": "其他电子"},
            {"ts_code": "002949.SZ", "name": "华阳国际", "industry": "建筑工程"},
            {"ts_code": "002855.SZ", "name": "捷荣技术", "industry": "元器件"},
            {"ts_code": "002084.SZ", "name": "海鸥住工", "industry": "家居用品"},
            {"ts_code": "600371.SH", "name": "万向德农", "industry": "种植业"},
            {"ts_code": "600378.SH", "name": "昊华科技", "industry": "化工"},
            {"ts_code": "600227.SH", "name": "赤天化", "industry": "化工"},
            {"ts_code": "002942.SZ", "name": "新农股份", "industry": "农药"},
            {"ts_code": "603082.SH", "name": "北自科技", "industry": "自动化设备"},
            {"ts_code": "600714.SH", "name": "金瑞矿业", "industry": "化工"},
            {"ts_code": "002886.SZ", "name": "沃特股份", "industry": "塑料"},
            {"ts_code": "900007.SH", "name": "旧简介样例", "industry": "服务"},
            {"ts_code": "900008.SH", "name": "小比例样例", "industry": "服务"},
            {"ts_code": "900009.SH", "name": "既有回退样例", "industry": "服务"},
            {"ts_code": "900010.SH", "name": "小副业样例", "industry": "化工"},
            {"ts_code": "900011.SH", "name": "农药样例", "industry": "化工"},
            {"ts_code": "900012.SH", "name": "智能物流样例", "industry": "自动化设备"},
        ],
        "companies": [
            {"ts_code": instrument_id, "main_business": summary}
            for instrument_id, _name, summary, _segment, _expected in cases
        ] + [
            {"ts_code": "000560.SZ", "main_business": "商业、房地产业和旅游服务业"},
            {"ts_code": "600479.SH", "main_business": "中成药、化学药和女性卫生用品"},
            {"ts_code": "002742.SZ", "main_business": "建材化工业务、医药制药业务"},
            {"ts_code": "600689.SH", "main_business": "进出口贸易、安防服务和园区物业租赁"},
            {"ts_code": "000062.SZ", "main_business": "电子元器件交易及服务平台"},
            {"ts_code": "002949.SZ", "main_business": "建筑设计和研发及其延伸业务"},
            {"ts_code": "002855.SZ", "main_business": "消费电子精密结构件研发和制造"},
            {"ts_code": "002084.SZ", "main_business": "卫浴及厨房产品的制造服务与销售"},
            {"ts_code": "600371.SH", "main_business": "玉米种子研发、生产与销售"},
            {"ts_code": "600378.SH", "main_business": "高端氟材料、电子特气及工程技术服务"},
            {"ts_code": "600227.SH", "main_business": "尿素、甲醇、复合肥和医药医疗业务"},
            {"ts_code": "002942.SZ", "main_business": "农药制剂、原药及中间体研发生产销售"},
            {"ts_code": "603082.SH", "main_business": "智能物流系统和装备研发设计制造集成"},
            {"ts_code": "600714.SH", "main_business": "锶盐系列产品研发生产和销售"},
            {"ts_code": "002886.SZ", "main_business": "特种高分子材料研发生产和销售"},
            {"ts_code": "900007.SH", "main_business": "曾经营中成药，当前提供健康咨询服务"},
            {"ts_code": "900008.SH", "main_business": "健康咨询服务及中成药销售"},
            {"ts_code": "900009.SH", "main_business": "化学原料药及其他服务"},
            {"ts_code": "900010.SH", "main_business": "尿素、甲醇和少量煤炭销售"},
            {"ts_code": "900011.SH", "main_business": "化学农药原药及制剂"},
            {"ts_code": "900012.SH", "main_business": "自动化立体仓库为核心的智能物流系统"},
        ],
        "sw_memberships": [],
        "business_segments": [
            {
                "ts_code": instrument_id,
                "end_date": "20251231",
                "bz_item": segment,
                "bz_sales": 100.0,
            }
            for instrument_id, _name, _summary, segment, _expected in cases
        ] + [
            {"ts_code": "000560.SZ", "end_date": "20251231", "bz_item": "资产管理", "bz_sales": 50.1},
            {"ts_code": "000560.SZ", "end_date": "20251231", "bz_item": "经纪", "bz_sales": 36.3},
            {"ts_code": "600479.SH", "end_date": "20251231", "bz_item": "销售商品", "bz_sales": 100.0},
            {"ts_code": "002742.SZ", "end_date": "20251231", "bz_item": "药品及中间体", "bz_sales": 100.0},
            {"ts_code": "600689.SH", "end_date": "20251231", "bz_item": "服装及纺织制品进出口贸易", "bz_sales": 70.0},
            {"ts_code": "600689.SH", "end_date": "20251231", "bz_item": "安防服务", "bz_sales": 24.0},
            {"ts_code": "000062.SZ", "end_date": "20251231", "bz_item": "电子元器件交易", "bz_sales": 96.0},
            {"ts_code": "002949.SZ", "end_date": "20251231", "bz_item": "公共建筑设计", "bz_sales": 38.7},
            {"ts_code": "002855.SZ", "end_date": "20251231", "bz_item": "精密结构件", "bz_sales": 89.0},
            {"ts_code": "002084.SZ", "end_date": "20251231", "bz_item": "五金龙头", "bz_sales": 52.0},
            {"ts_code": "600371.SH", "end_date": "20251231", "bz_item": "玉米", "bz_sales": 96.0},
            {"ts_code": "600378.SH", "end_date": "20251231", "bz_item": "高端氟材料", "bz_sales": 45.0},
            {"ts_code": "600227.SH", "end_date": "20251231", "bz_item": "尿素", "bz_sales": 47.7},
            {"ts_code": "600227.SH", "end_date": "20251231", "bz_item": "甲醇", "bz_sales": 35.2},
            {"ts_code": "600227.SH", "end_date": "20251231", "bz_item": "煤炭", "bz_sales": 1.4},
            {"ts_code": "002942.SZ", "end_date": "20251231", "bz_item": "制剂", "bz_sales": 51.0},
            {"ts_code": "603082.SH", "end_date": "20251231", "bz_item": "智能生产物流系统", "bz_sales": 57.2},
            {"ts_code": "600714.SH", "end_date": "20251231", "bz_item": "锶盐", "bz_sales": 93.0},
            {"ts_code": "002886.SZ", "end_date": "20251231", "bz_item": "特种高分子材料", "bz_sales": 48.9},
            {"ts_code": "900007.SH", "end_date": "20251231", "bz_item": "健康咨询服务", "bz_sales": 100.0},
            {"ts_code": "900008.SH", "end_date": "20251231", "bz_item": "健康咨询服务", "bz_sales": 90.0},
            {"ts_code": "900008.SH", "end_date": "20251231", "bz_item": "中成药", "bz_sales": 10.0},
            {"ts_code": "900009.SH", "end_date": "20251231", "bz_item": "其他服务", "bz_sales": 90.0},
            {"ts_code": "900009.SH", "end_date": "20251231", "bz_item": "化学原料药", "bz_sales": 10.0},
            {"ts_code": "900010.SH", "end_date": "20251231", "bz_item": "尿素", "bz_sales": 47.7},
            {"ts_code": "900010.SH", "end_date": "20251231", "bz_item": "甲醇", "bz_sales": 35.2},
            {"ts_code": "900010.SH", "end_date": "20251231", "bz_item": "煤炭", "bz_sales": 1.4},
            {"ts_code": "900011.SH", "end_date": "20251231", "bz_item": "制剂", "bz_sales": 51.0},
            {"ts_code": "900011.SH", "end_date": "20251231", "bz_item": "原药及中间体", "bz_sales": 49.0},
            {"ts_code": "900012.SH", "end_date": "20251231", "bz_item": "智能生产物流系统", "bz_sales": 57.2},
            {"ts_code": "900012.SH", "end_date": "20251231", "bz_item": "智能仓储物流系统", "bz_sales": 40.4},
        ],
    }

    _status, profiles = build_stock_relationship_catalog(
        source,
        generated_at=datetime(2026, 8, 29, 12, 0, tzinfo=SHANGHAI),
    )
    by_id = {item.instrument_id: item for item in profiles}

    for instrument_id, _name, _summary, _segment, expected in cases:
        assert by_id[instrument_id].primary_business_name == expected
    assert by_id["900005.SH"].business_domain_name == "消费"
    assert by_id["900005.SH"].directory_category_name == "消费"
    assert by_id["900006.SH"].business_domain_name == "芯片"
    assert by_id["900006.SH"].directory_category_name == "电子分销"
    assert by_id["900013.SH"].directory_category_name == "特种高分子材料"
    assert by_id["000560.SZ"].primary_business_name == "房产经纪服务"
    assert by_id["000560.SZ"].business_domain_name == "房地产"
    assert by_id["000560.SZ"].verification_status == "verified"
    assert by_id["600479.SH"].primary_business_name == "中成药"
    assert by_id["600479.SH"].business_domain_name == "医疗医药"
    assert by_id["600479.SH"].directory_category_name == "创新药"
    assert "创新药研发" in by_id["600479.SH"].business_tags
    assert by_id["600479.SH"].business_segments[0].name == "销售商品"
    assert by_id["002742.SZ"].primary_business_name == "医药"
    assert by_id["002742.SZ"].business_domain_name == "医疗医药"
    assert by_id["002742.SZ"].verification_status == "verified"
    assert by_id["600689.SH"].business_domain_name == "消费"
    assert by_id["600689.SH"].directory_category_name == "消费"
    assert by_id["600689.SH"].primary_business_name == "纺织贸易"
    assert by_id["600689.SH"].verification_status == "verified"
    assert by_id["000062.SZ"].business_domain_name == "芯片"
    assert by_id["000062.SZ"].directory_category_name == "芯片"
    assert by_id["000062.SZ"].primary_business_name == "电子分销"
    assert by_id["000062.SZ"].verification_status == "verified"
    expected_reviewed_directories = {
        "002949.SZ": ("AI应用", "建筑设计"),
        "002855.SZ": ("消费电子", "精密结构件"),
        "002084.SZ": ("消费", "卫浴及厨房产品"),
        "600371.SH": ("农业", "玉米种业"),
        "600378.SH": ("六氟化钨", "氟化工"),
        "002942.SZ": ("农业", "农药"),
        "603082.SH": ("机器人", "智能物流"),
        "600714.SH": ("小金属", "锶盐"),
        "002886.SZ": ("PCB", "特种高分子材料"),
    }
    for instrument_id, (directory_name, primary_name) in expected_reviewed_directories.items():
        assert by_id[instrument_id].directory_category_name == directory_name
        assert by_id[instrument_id].primary_business_name == primary_name
        assert by_id[instrument_id].verification_status == "verified"
        assert "official_web_evidence_applied" in by_id[instrument_id].flags
    assert "AI建筑设计" in by_id["002949.SZ"].business_tags
    assert "3C" in by_id["002855.SZ"].business_tags
    assert "水龙头" in by_id["002084.SZ"].business_tags
    assert "玉米种子" in by_id["600371.SH"].business_tags
    assert "电子级六氟化钨" in by_id["600378.SH"].business_tags
    assert "农业生产资料" in by_id["002942.SZ"].business_tags
    assert "机器人工作站" in by_id["603082.SH"].business_tags
    assert "金属锶" in by_id["600714.SH"].business_tags
    assert "服务器用PCB" in by_id["002886.SZ"].business_tags
    assert by_id["900007.SH"].primary_business_name == "健康咨询服务"
    assert "中成药" not in by_id["900007.SH"].business_tags
    assert by_id["900008.SH"].primary_business_name == "健康咨询服务"
    assert by_id["900009.SH"].primary_business_name == "其他服务"
    assert by_id["900010.SH"].primary_business_name == "化肥"
    assert by_id["900010.SH"].directory_category_name == "化肥"
    assert by_id["900011.SH"].primary_business_name == "农药"
    assert by_id["900011.SH"].directory_category_name == "农业"
    assert by_id["900012.SH"].primary_business_name == "智能物流"
    assert by_id["900012.SH"].directory_category_name == "智能物流"
    assert by_id["600227.SH"].primary_business_name == "化肥"
    assert by_id["600227.SH"].directory_category_name == "化肥"


@pytest.mark.parametrize(
    ("business_key", "evidence_texts", "expected"),
    (
        ("storage", ("存储器件分销",), None),
        ("precious_metals", ("金银珠宝",), None),
        ("fertilizer", ("尿素和复合肥",), None),
        ("pharmaceuticals", ("药品及中间体",), ("medical_pharma", "医疗医药")),
        ("real_estate_services", ("房产经纪服务",), ("real_estate", "房地产")),
        ("textiles", ("纺织品进出口",), ("consumer", "消费")),
        ("bathroom_kitchen_products", ("卫浴及厨房产品",), ("consumer", "消费")),
        ("corn_seed", ("玉米种子",), ("agriculture", "农业")),
        ("pesticide", ("农药制剂",), ("agriculture", "农业")),
        ("electronics_distribution", ("普通电子元器件交易",), None),
        ("electronics_distribution", ("半导体授权分销",), ("chips", "芯片")),
        ("traditional_chinese_medicine", ("中成药",), None),
        ("traditional_chinese_medicine", ("中药创新药研发",), ("innovative_drugs", "创新药")),
        ("architectural_design", ("建筑设计",), None),
        ("architectural_design", ("AI建筑设计和自动成图",), ("ai_application", "AI应用")),
        ("precision_structural_components", ("3C智能终端精密结构件",), ("consumer_electronics", "消费电子")),
        ("fluorochemicals", ("含氟气体",), None),
        ("fluorochemicals", ("电子级六氟化钨",), ("tungsten_hexafluoride", "六氟化钨")),
        ("smart_logistics", ("智能仓储物流系统",), None),
        ("smart_logistics", ("AGV和机器人工作站",), ("robotics", "机器人")),
        ("strontium_salts", ("碳酸锶和金属锶",), ("small_metals", "小金属")),
        ("specialty_polymer_materials", ("LCP高频材料",), None),
        ("specialty_polymer_materials", ("服务器用PCB材料",), ("pcb", "PCB")),
    ),
)
def test_human_reviewed_directory_logic_generalizes_with_evidence_gates(
    business_key,
    evidence_texts,
    expected,
):
    assert reviewed_directory_category(business_key, evidence_texts) == expected


def test_current_market_review_evidence_does_not_overwrite_long_term_primary_business() -> None:
    source = {
        "contract": "instrument_taxonomy_source_bundle.v1",
        "schema_version": 1,
        "as_of": "2026-08-28",
        "source_providers": ["tushare"],
        "source_request_ids": ["req-current-market-review"],
        "flags": [],
        "stocks": [
            {"ts_code": "603900.SH", "name": "莱绅通灵", "industry": "服饰"},
            {"ts_code": "603269.SH", "name": "海鸥股份", "industry": "专用机械"},
            {"ts_code": "600162.SH", "name": "香江控股", "industry": "房地产"},
            {"ts_code": "603011.SH", "name": "合锻智能", "industry": "机床制造"},
            {"ts_code": "002354.SZ", "name": "天娱数科", "industry": "互联网"},
        ],
        "companies": [
            {"ts_code": "603900.SH", "main_business": "珠宝首饰品牌运营管理、产品设计研发及零售"},
            {"ts_code": "603269.SH", "main_business": "各类冷却塔研发、设计、制造及安装"},
            {"ts_code": "600162.SH", "main_business": "家居商贸运营和房地产开发"},
            {"ts_code": "603011.SH", "main_business": "高端成形装备和智能分选设备"},
            {"ts_code": "002354.SZ", "main_business": "传媒、数字营销和AI应用业务"},
        ],
        "sw_memberships": [],
        "business_segments": [
            {"ts_code": "603900.SH", "end_date": "20251231", "bz_item": "镶嵌钻石饰品", "bz_sales": 100.0},
            {"ts_code": "603269.SH", "end_date": "20251231", "bz_item": "常规冷却塔", "bz_sales": 100.0},
            {"ts_code": "600162.SH", "end_date": "20251231", "bz_item": "商贸流通运营", "bz_sales": 100.0},
            {"ts_code": "603011.SH", "end_date": "20251231", "bz_item": "色选机", "bz_sales": 100.0},
            {"ts_code": "002354.SZ", "end_date": "20251231", "bz_item": "数字效果营销", "bz_sales": 100.0},
        ],
    }

    _status, profiles = build_stock_relationship_catalog(
        source,
        generated_at=datetime(2026, 8, 29, 23, 0, tzinfo=SHANGHAI),
    )
    by_id = {item.instrument_id: item for item in profiles}

    expected = {
        "603900.SH": ("珠宝首饰零售", "珠宝首饰零售", "黄金珠宝"),
        "603269.SH": ("冷却塔", "冷却塔", "液冷配套"),
        "600162.SH": ("商贸流通运营", "商贸流通运营", "互联网数据中心业务"),
        "603011.SH": ("色选机", "色选机", "CCL/PCB层压设备"),
        "002354.SZ": ("传媒", "传媒", "AI营销"),
    }
    for instrument_id, (primary, directory, supporting_tag) in expected.items():
        profile = by_id[instrument_id]
        assert profile.primary_business_name == primary
        assert profile.directory_category_name == directory
        assert supporting_tag in profile.business_tags
        assert profile.verification_status == "verified"
