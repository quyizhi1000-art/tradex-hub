from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from astock_signals.smart_router import SmartRouter
from tradex.data_gateway.contracts import QualityStatus
from tradex.data_gateway.leadership import (
    board_leader_snapshot_to_legacy_payload,
    fetch_board_leader_snapshot,
    fetch_leader_quotes,
    fetch_stock_sector_profiles,
    leader_quotes_to_legacy_records,
    stock_sector_profiles_to_legacy_records,
)


_NOW = datetime(2026, 8, 19, 10, 31, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_leader_quotes_use_canonical_contract_and_preserve_legacy_fields() -> None:
    frame = pd.DataFrame(
        [
            {"代码": "600030", "名称": "中信证券", "最新价": 30.0, "涨跌幅": 3.2},
            {"代码": "000001", "名称": "平安银行", "最新价": 12.5, "涨跌幅": -0.4},
        ]
    )
    router = SmartRouter()
    router.register("leader_quotes", "tencent_http", lambda symbols: frame, priority=1)

    series = fetch_leader_quotes(
        ["600030", "000001"],
        router=router,
        now=_NOW,
    )

    assert series.metadata.contract == "leader_quote.v1"
    assert series.metadata.provider == "tencent_http"
    assert series.metadata.quality is QualityStatus.DEGRADED
    assert series.metadata.quality_flags == ("provider_timestamp_missing",)
    assert [item.instrument_id for item in series.quotes] == [
        "600030.SH",
        "000001.SZ",
    ]
    assert leader_quotes_to_legacy_records(series) == [
        {"代码": "600030", "名称": "中信证券", "最新价": 30.0, "涨跌幅": 3.2},
        {"代码": "000001", "名称": "平安银行", "最新价": 12.5, "涨跌幅": -0.4},
    ]


def test_leader_quote_partial_coverage_is_explicitly_degraded() -> None:
    frame = pd.DataFrame(
        [{"代码": "600030", "名称": "中信证券", "最新价": 30.0, "涨跌幅": 3.2}]
    )
    router = SmartRouter()
    router.register("leader_quotes", "tencent_http", lambda symbols: frame, priority=1)

    series = fetch_leader_quotes(
        ["600030", "000001"],
        router=router,
        now=_NOW,
    )

    assert set(series.metadata.quality_flags) == {
        "quote_coverage_partial",
        "provider_timestamp_missing",
    }


def _profile_frame(provider_as_of: str = "2026-08-19T15:00:00+08:00") -> pd.DataFrame:
    frame = pd.DataFrame(
        [
            {
                "代码": "600928",
                "名称": "西安银行",
                "行业": "银行Ⅱ",
                "地域": "陕西板块",
                "概念标签": ["互联网金融", "移动支付"],
                "provider_as_of": provider_as_of,
                "source": "push2delay",
            }
        ]
    )
    frame.attrs.update(
        {"source": "push2delay", "profile_total": 1, "source_valid": True}
    )
    return frame


def test_stock_sector_profiles_validate_coverage_and_trade_date() -> None:
    router = SmartRouter()
    router.register(
        "stock_sector_profiles",
        "em_push2delay",
        lambda codes: _profile_frame(),
        priority=1,
    )

    series = fetch_stock_sector_profiles(
        ["600928"],
        trade_date="2026-08-19",
        router=router,
        now=_NOW,
    )

    assert series.metadata.contract == "stock_sector_profile.v1"
    assert series.metadata.quality is QualityStatus.ACCEPTED
    assert stock_sector_profiles_to_legacy_records(series) == [
        {
            "代码": "600928",
            "名称": "西安银行",
            "行业": "银行Ⅱ",
            "地域": "陕西板块",
            "概念标签": ["互联网金融", "移动支付"],
            "provider_as_of": "2026-08-19T15:00:00+08:00",
            "source": "push2delay",
        }
    ]


def test_stock_sector_profiles_reject_wrong_trade_date_inside_route() -> None:
    router = SmartRouter()
    router.register(
        "stock_sector_profiles",
        "em_push2delay",
        lambda codes: _profile_frame("2026-08-18T15:00:00+08:00"),
        priority=1,
    )

    with pytest.raises(RuntimeError, match="All sources"):
        fetch_stock_sector_profiles(
            ["600928"],
            trade_date="2026-08-19",
            router=router,
            now=_NOW,
        )


def _board_frame(*, name: str | None, source: str) -> pd.DataFrame:
    frame = pd.DataFrame(
        [
            {
                "code": "600030",
                "name": name,
                "price": 30.0,
                "change_pct": 3.2,
                "amount": 5_000_000_000,
                "turnover": 2.1,
                "flow_amount": 500_000_000,
                "flow_ratio": 5.2,
                "provider_as_of": "2026-08-19T10:30:00+08:00",
                "source": source,
            }
        ]
    )
    frame.attrs.update({"source": source, "board_code": "BK0473"})
    return frame


def test_board_leader_mapping_failure_falls_back_to_next_provider() -> None:
    router = SmartRouter()
    router.register(
        "board_leaders",
        "bad_primary",
        lambda **kwargs: _board_frame(name=None, source="bad_primary"),
        priority=1,
    )
    router.register(
        "board_leaders",
        "em_push2delay",
        lambda **kwargs: _board_frame(name="中信证券", source="push2delay"),
        priority=2,
    )

    snapshot = fetch_board_leader_snapshot(
        "BK0473",
        limit=3,
        router=router,
        now=_NOW,
    )
    payload = board_leader_snapshot_to_legacy_payload(snapshot)

    assert snapshot.metadata.contract == "board_leader.v1"
    assert snapshot.metadata.provider == "push2delay"
    assert snapshot.metadata.quality is QualityStatus.ACCEPTED
    assert payload == {
        "status": "ready",
        "source": "push2delay",
        "provider_as_of": "2026-08-19T10:30:00+08:00",
        "fetched_at": "2026-08-19T10:31:00+08:00",
        "stale": False,
        "method": "board_constituents",
        "items": [
            {
                "code": "600030",
                "name": "中信证券",
                "price": 30.0,
                "change_pct": 3.2,
                "amount": 5_000_000_000.0,
                "turnover": 2.1,
                "flow_amount": 500_000_000.0,
                "flow_ratio": 5.2,
                "provider_as_of": "2026-08-19T10:30:00+08:00",
                "source": "push2delay",
            }
        ],
    }
