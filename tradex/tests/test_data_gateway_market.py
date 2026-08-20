"""Canonical market gateway and legacy-view compatibility tests."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from pydantic import ValidationError

from tradex.data_gateway.contracts import IndexQuoteV1, QualityStatus
from tradex.data_gateway.market import (
    fetch_market_overview,
    market_overview_to_legacy_payload,
)
from tradex.data_gateway.quality import DataQualityError


class _Router:
    def __init__(self, frame: pd.DataFrame, provider: str = "fixture") -> None:
        self.frame = frame
        self.provider = provider
        self.calls = []

    def route(self, data_type: str):
        self.calls.append(data_type)
        return self.frame, self.provider


def _records() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "代码": "sh000001",
                "名称": "上证指数",
                "最新价": 3990.30,
                "涨跌额": 7.65,
                "涨跌幅": 0.19,
                "昨收": 3982.65,
                "今开": 3979.49,
                "最高": 3994.18,
                "最低": 3955.60,
                "成交额": 1_135_187_666_395,
                "更新时间": "2026-08-19T10:30:00+08:00",
            },
            {
                "代码": "sz399001",
                "名称": "深证成指",
                "最新价": 14622.50,
                "涨跌额": -81.77,
                "涨跌幅": -0.56,
                "昨收": 14704.27,
                "更新时间": "2026-08-19T10:30:01+08:00",
            },
            {
                "代码": "sz399006",
                "名称": "创业板指",
                "最新价": 3120.0,
                "涨跌幅": 1.4,
                "更新时间": "2026-08-19T10:30:01+08:00",
            },
        ]
    )


def _turnover(*, symbol: str, days: int):
    assert days == 5
    previous, today = (8, 10) if symbol == "sh000001" else (4, 5)
    return [
        {"date": "2026-08-18", "time": "10:30", "amount": previous},
        {"date": "2026-08-19", "time": "10:30", "amount": today},
    ]


def test_gateway_returns_provider_neutral_contract_and_legacy_view():
    china = ZoneInfo("Asia/Shanghai")
    router = _Router(_records(), provider="akshare")

    snapshot = fetch_market_overview(
        now=datetime(2026, 8, 19, 10, 30, 5, tzinfo=china),
        router=router,
        turnover_fetcher=_turnover,
    )

    assert router.calls == ["market_overview"]
    assert snapshot.metadata.contract == "market_overview.v1"
    assert snapshot.metadata.quality is QualityStatus.ACCEPTED
    assert snapshot.metadata.provider == "akshare"
    assert snapshot.metadata.provider_as_of.isoformat() == "2026-08-19T10:30:01+08:00"
    assert [item.instrument_id for item in snapshot.indices] == [
        "000001.SH",
        "399001.SZ",
    ]
    assert snapshot.indices[0].amount_cny == 1_135_187_666_395
    assert snapshot.market_turnover.difference_cny == 3

    payload = market_overview_to_legacy_payload(snapshot)
    assert payload["indices"][0]["code"] == "sh000001"
    assert payload["indices"][0]["amount"] == 1_135_187_666_395
    assert payload["participation_indices"][2]["代码"] == "sz399006"
    assert payload["market_turnover"]["metric"] == "amount"
    assert payload["market_turnover"]["difference"] == 3
    assert payload["contract"] == "market_overview.v1"
    assert payload["quality"] == "accepted"


def test_gateway_rejects_payload_without_a_required_index():
    frame = pd.DataFrame(
        [{"代码": "sz399006", "名称": "创业板指", "涨跌幅": 1.0}]
    )

    with pytest.raises(DataQualityError, match="上证指数或深证成指"):
        fetch_market_overview(
            now=datetime(2026, 8, 19, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
            router=_Router(frame),
            turnover_fetcher=_turnover,
        )


def test_gateway_preserves_provider_metadata_from_frame_attributes():
    frame = _records().drop(columns=["更新时间"])
    frame.attrs.update(
        {
            "provider_as_of": "2026-08-19T10:30:01+08:00",
            "request_id": "provider-request-1",
        }
    )

    snapshot = fetch_market_overview(
        now=datetime(2026, 8, 19, 10, 30, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
        router=_Router(frame, provider="paid_b"),
        turnover_fetcher=_turnover,
    )

    assert snapshot.metadata.provider_as_of.isoformat() == "2026-08-19T10:30:01+08:00"
    assert snapshot.metadata.provider_request_id == "provider-request-1"
    assert snapshot.metadata.quality is QualityStatus.ACCEPTED


def test_contract_rejects_semantically_impossible_high_low():
    with pytest.raises(ValidationError, match="high cannot be lower"):
        IndexQuoteV1(
            instrument_id="000001.SH",
            name="上证指数",
            available=True,
            value=3990,
            high=3900,
            low=4000,
        )
