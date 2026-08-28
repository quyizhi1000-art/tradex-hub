from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from tradex.instrument_taxonomy.builder import build_stock_relationship_catalog
from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader, InstrumentTaxonomyStore


SHANGHAI = ZoneInfo("Asia/Shanghai")


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
        ("900004.SH", "医药样例", "医药制药业务", "药品及中间体", "医药"),
        ("900005.SH", "纺织样例", "纺织品进出口贸易", "纺织品", "纺织"),
        ("900006.SH", "分销样例", "电子元器件交易服务", "电子元器件交易", "电子分销"),
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
            {"ts_code": "900007.SH", "name": "旧简介样例", "industry": "服务"},
        ],
        "companies": [
            {"ts_code": instrument_id, "main_business": summary}
            for instrument_id, _name, summary, _segment, _expected in cases
        ] + [
            {"ts_code": "000560.SZ", "main_business": "商业、房地产业和旅游服务业"},
            {"ts_code": "600479.SH", "main_business": "中成药、化学药和女性卫生用品"},
            {"ts_code": "900007.SH", "main_business": "曾经营中成药，当前提供健康咨询服务"},
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
            {"ts_code": "900007.SH", "end_date": "20251231", "bz_item": "健康咨询服务", "bz_sales": 100.0},
        ],
    }

    _status, profiles = build_stock_relationship_catalog(
        source,
        generated_at=datetime(2026, 8, 29, 12, 0, tzinfo=SHANGHAI),
    )
    by_id = {item.instrument_id: item for item in profiles}

    for instrument_id, _name, _summary, _segment, expected in cases:
        assert by_id[instrument_id].primary_business_name == expected
    assert by_id["000560.SZ"].primary_business_name == "房产经纪服务"
    assert by_id["000560.SZ"].verification_status == "verified"
    assert by_id["600479.SH"].primary_business_name == "中成药"
    assert by_id["600479.SH"].business_segments == ()
    assert by_id["900007.SH"].primary_business_name == "健康咨询服务"
    assert "中成药" not in by_id["900007.SH"].business_tags
