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


def test_limit_up_item_uses_only_real_relationship_catalog_fields() -> None:
    item = _item()

    assert item.instrument_id == "001309.SZ"
    assert item.board_count == 2
    assert item.first_sealed_at == TARGET
    assert item.relationship_match_status == "matched"
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


def test_limit_up_pool_groups_only_by_primary_business() -> None:
    item = _item()
    pool = _pool(item)

    assert pool.contract == "limit_up_pool.v2"
    assert pool.catalog_matched_count == 1
    assert pool.business_classified_count == 1
    assert pool.unmatched_count == 0
    assert [(entry.business_key, entry.label, entry.count) for entry in pool.categories] == [
        ("memory", "存储", 1)
    ]
    assert pool.pool_revision == stable_sha256(
        pool.model_dump(mode="json", exclude={"pool_revision"})
    )


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
