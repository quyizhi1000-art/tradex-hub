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
