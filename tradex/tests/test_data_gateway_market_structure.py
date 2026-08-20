from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradex.dashboard import risk_service
from tradex.data_gateway.contracts import QualityStatus
from tradex.data_gateway.market_structure import (
    fetch_market_breadth_snapshot,
    fetch_sector_quotes,
    market_breadth_to_legacy_records,
    metadata_to_component_status,
    sector_quotes_to_legacy_records,
)


_NOW = datetime(2026, 8, 19, 10, 31, tzinfo=ZoneInfo("Asia/Shanghai"))


class _Router:
    def __init__(self, frame: pd.DataFrame, provider: str) -> None:
        self.frame = frame
        self.provider = provider
        self.calls: list[tuple[str, dict[str, object]]] = []

    def route(self, data_type: str, **kwargs: object):
        self.calls.append((data_type, kwargs))
        return self.frame, self.provider


def test_market_breadth_preserves_counts_and_provider_metadata() -> None:
    frame = pd.DataFrame(
        [
            {
                "上涨": 3200,
                "下跌": 1800,
                "平盘": 100,
                "未分类": 0,
                "涨停": 80,
                "跌停": 12,
            }
        ]
    )
    frame.attrs.update(
        {
            "provider_as_of": "2026-08-19T10:30:00+08:00",
            "request_id": "breadth-request-1",
            "source_valid": True,
        }
    )
    snapshot = fetch_market_breadth_snapshot(
        router=_Router(frame, "ths_fuyao"), now=_NOW
    )

    assert snapshot.total_count == 5100
    assert snapshot.metadata.contract == "market_breadth.v1"
    assert snapshot.metadata.provider_request_id == "breadth-request-1"
    assert snapshot.metadata.quality is QualityStatus.ACCEPTED
    assert market_breadth_to_legacy_records(snapshot) == [
        {
            "上涨": 3200,
            "下跌": 1800,
            "平盘": 100,
            "未分类": 0,
            "涨停": 80,
            "跌停": 12,
        }
    ]


def test_market_breadth_without_provider_time_is_degraded() -> None:
    frame = pd.DataFrame(
        [{"上涨": 3200, "下跌": 1800, "平盘": 100, "涨停": 80, "跌停": 12}]
    )
    snapshot = fetch_market_breadth_snapshot(
        router=_Router(frame, "em_push2ex"), now=_NOW
    )

    assert snapshot.metadata.quality is QualityStatus.DEGRADED
    assert snapshot.metadata.quality_flags == (
        "universe_definition_unverified",
        "provider_timestamp_missing",
    )


def test_market_breadth_reports_unclassified_participation() -> None:
    frame = pd.DataFrame(
        [
            {
                "上涨": 3200,
                "下跌": 1800,
                "平盘": 100,
                "未分类": 1,
                "涨停": 80,
                "跌停": 12,
            }
        ]
    )
    frame.attrs["provider_as_of"] = "2026-08-19T10:30:00+08:00"
    snapshot = fetch_market_breadth_snapshot(
        router=_Router(frame, "ths_fuyao"), now=_NOW
    )

    assert snapshot.total_count == 5101
    assert snapshot.unclassified_count == 1
    assert snapshot.metadata.quality is QualityStatus.DEGRADED
    assert snapshot.metadata.quality_flags == ("participation_unclassified",)


def test_market_breadth_rejects_inconsistent_limit_counts() -> None:
    frame = pd.DataFrame(
        [
            {
                "上涨": 10,
                "下跌": 20,
                "平盘": 1,
                "未分类": 0,
                "涨停": 11,
                "跌停": 2,
            }
        ]
    )
    with pytest.raises(ValueError, match="limit-up count cannot exceed up count"):
        fetch_market_breadth_snapshot(
            router=_Router(frame, "ths_fuyao"), now=_NOW
        )


def _eastmoney_sector_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "板块代码": "BK0475",
                "最新点位": 1234.5,
                "板块名称": "银行",
                "涨跌幅": 2.5,
                "成交额": 50_000_000_000,
                "主力净流入": 1_000_000_000,
                "主力净流入-占比": 3.75,
                "主力净流入排名": 7,
                "上涨家数": 31,
                "下跌家数": 8,
                "领涨股代码": "600928",
                "领涨股票": "西安银行",
                "领涨股涨幅": 9.95,
                "更新时间": "2026-08-19T10:30:00+08:00",
                "source": "push2",
            },
            {
                "板块代码": "BK0473",
                "最新点位": 1000,
                "板块名称": "证券",
                "涨跌幅": 1.5,
                "成交额": 40_000_000_000,
                "主力净流入": 500_000_000,
                "主力净流入-占比": 2.5,
                "主力净流入排名": 9,
                "上涨家数": 40,
                "下跌家数": 10,
                "领涨股代码": "600030",
                "领涨股票": "中信证券",
                "领涨股涨幅": 5.2,
                "更新时间": "2026-08-19T10:30:01+08:00",
                "source": "push2",
            },
        ]
    )


def test_eastmoney_sector_quotes_map_to_stable_contract() -> None:
    router = _Router(_eastmoney_sector_frame(), "em_push2")
    series = fetch_sector_quotes("industry", router=router, now=_NOW)

    assert router.calls == [
        ("industry_quotes", {"board_type": "industry", "exact": None})
    ]
    assert series.metadata.contract == "sector_quote.v1"
    assert series.metadata.quality is QualityStatus.ACCEPTED
    assert [item.name for item in series.quotes] == ["银行", "证券"]
    assert series.quotes[0].sector_key == "industry:银行"
    assert series.quotes[0].leader_instrument_id == "600928.SH"
    legacy = sector_quotes_to_legacy_records(series)[0]
    assert legacy["板块名称"] == "银行"
    assert legacy["领涨股代码"] == "600928"
    assert legacy["领涨股市场"] == "SH"
    assert legacy["source"] == "push2"


def test_biying_sector_quote_keeps_missing_fields_explicit_and_degraded() -> None:
    frame = pd.DataFrame(
        [
            {
                "板块代码": "881155",
                "最新点位": 1234.5,
                "板块名称": "银行",
                "涨跌幅": 2.5,
                "成交额": None,
                "主力净流入": 1_000_000_000,
                "主力净流入-占比": 3.75,
                "领涨股代码": "600928",
                "领涨股票": "西安银行",
                "更新时间": "2026-08-19T10:30:00+08:00",
                "source": "biying_hibk",
            }
        ]
    )
    series = fetch_sector_quotes(
        "industry", router=_Router(frame, "biying"), now=_NOW
    )

    assert series.metadata.quality is QualityStatus.DEGRADED
    assert set(series.metadata.quality_flags) == {
        "amount_partial",
        "constituent_breadth_partial",
    }
    quote = series.quotes[0]
    assert quote.amount_cny is None
    assert quote.up_count is None
    assert quote.down_count is None
    assert quote.main_net_inflow_cny == 1_000_000_000


def test_sector_quotes_reject_duplicate_canonical_names() -> None:
    frame = _eastmoney_sector_frame()
    frame.loc[1, "板块名称"] = "银行"
    with pytest.raises(ValueError, match="duplicate sector keys"):
        fetch_sector_quotes("industry", router=_Router(frame, "em_push2"), now=_NOW)


def test_dashboard_fetchers_expose_gateway_quality(monkeypatch) -> None:
    breadth_frame = pd.DataFrame(
        [{"上涨": 3200, "下跌": 1800, "平盘": 100, "涨停": 80, "跌停": 12}]
    )
    breadth = fetch_market_breadth_snapshot(
        router=_Router(breadth_frame, "em_push2ex"), now=_NOW
    )
    sectors = fetch_sector_quotes(
        "industry", router=_Router(_eastmoney_sector_frame(), "em_push2"), now=_NOW
    )
    monkeypatch.setattr(risk_service, "fetch_market_breadth_snapshot", lambda: breadth)
    monkeypatch.setattr(
        risk_service, "fetch_sector_quotes", lambda _board_type: sectors
    )

    breadth_records, breadth_source, breadth_metadata = (
        risk_service._fetch_market_breadth()
    )
    sector_records, sector_source, sector_metadata = risk_service._fetch_board_quotes(
        "industry"
    )

    assert breadth_records[0]["上涨"] == 3200
    assert breadth_source == "em_push2ex"
    assert breadth_metadata == metadata_to_component_status(breadth.metadata)
    assert breadth_metadata["partial"] is True
    assert sector_records[0]["板块名称"] == "银行"
    assert sector_source == "em_push2"
    assert sector_metadata["quality"] == "accepted"
