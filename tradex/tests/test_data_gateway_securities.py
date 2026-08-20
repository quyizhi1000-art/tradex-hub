from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from astock_signals.smart_router import SmartRouter, SourceCapabilityError

from tradex.data_gateway.contracts import QualityStatus
from tradex.data_gateway.securities import (
    fetch_ohlcv_series,
    fetch_quote_snapshot,
    ohlcv_series_to_legacy_records,
    quote_snapshot_to_legacy_records,
)
from tradex.data_sources.eltdx_fetchers import fetch_historical_kline as fetch_eltdx_kline


_NOW = datetime(2026, 8, 19, 10, 31, tzinfo=ZoneInfo("Asia/Shanghai"))


class _Router:
    def __init__(self, frame: pd.DataFrame, provider: str) -> None:
        self.frame = frame
        self.provider = provider
        self.calls: list[tuple[str, dict[str, object]]] = []

    def route(self, data_type: str, **kwargs: object):
        self.calls.append((data_type, kwargs))
        return self.frame, self.provider


@pytest.mark.parametrize(
    ("provider", "raw_volume", "raw_amount"),
    [
        ("ths_fuyao", 100_000, 180_000_000),
        ("biying", 100_000, 180_000_000),
        ("eltdx", 1_000, 180_000_000),
        ("akshare", 1_000, 180_000_000),
        ("tencent_http", 1_000, 18_000),
    ],
)
def test_quote_sources_have_identical_canonical_units(
    provider: str,
    raw_volume: float,
    raw_amount: float,
) -> None:
    frame = pd.DataFrame(
        [
            {
                "代码": "600519",
                "名称": "贵州茅台",
                "最新价": 1800,
                "涨跌额": 10,
                "涨跌幅": 0.56,
                "今开": 1790,
                "最高": 1810,
                "最低": 1788,
                "昨收": 1790,
                "成交量": raw_volume,
                "成交额": raw_amount,
                "更新时间": "2026-08-19T10:30:00+08:00",
            }
        ]
    )
    snapshot = fetch_quote_snapshot(
        "600519", router=_Router(frame, provider), now=_NOW
    )

    assert snapshot.instrument_id == "600519.SH"
    assert snapshot.volume_shares == 100_000
    assert snapshot.amount_cny == 180_000_000
    assert snapshot.metadata.quality is QualityStatus.ACCEPTED
    legacy = quote_snapshot_to_legacy_records(snapshot)[0]
    assert legacy["代码"] == "600519"
    assert legacy["成交量"] == 100_000
    assert legacy["成交额"] == 180_000_000


def test_quote_requires_exact_symbol_even_for_single_row() -> None:
    frame = pd.DataFrame([{"代码": "600519", "最新价": 1800}])
    with pytest.raises(RuntimeError, match="requested symbol 999999"):
        fetch_quote_snapshot("999999", router=_Router(frame, "akshare"), now=_NOW)


@pytest.mark.parametrize(
    ("provider", "raw_volume"),
    [
        ("ths_fuyao", 50_000),
        ("biying", 50_000),
        ("eltdx", 500),
        ("akshare", 500),
    ],
)
def test_ohlcv_sources_have_identical_canonical_volume(
    provider: str,
    raw_volume: float,
) -> None:
    frame = pd.DataFrame(
        [
            {
                "日期": "2025-01-02",
                "开盘": 1750,
                "收盘": 1760,
                "最高": 1770,
                "最低": 1745,
                "成交量": raw_volume,
                "成交额": 88_000_000,
            },
            {
                "日期": "2025-01-03",
                "开盘": 1760,
                "收盘": 1780,
                "最高": 1790,
                "最低": 1755,
                "成交量": raw_volume + (10_000 if provider == "ths_fuyao" else 100),
                "成交额": 106_800_000,
            },
        ]
    )
    series = fetch_ohlcv_series(
        "600519",
        period="day",
        start_date="20250102",
        end_date="20250103",
        adjust="",
        router=_Router(frame, provider),
        now=_NOW,
    )

    assert series.instrument_id == "600519.SH"
    assert series.period == "daily"
    assert series.adjustment == "none"
    assert series.bars[0].volume_shares == 50_000
    assert ohlcv_series_to_legacy_records(series)[0] == {
        "日期": "2025-01-02",
        "开盘": 1750,
        "收盘": 1760,
        "最高": 1770,
        "最低": 1745,
        "成交量": 50_000,
        "成交额": 88_000_000,
        "振幅": None,
        "涨跌幅": None,
        "涨跌额": None,
        "换手率": None,
    }


def test_ohlcv_rejects_duplicate_dates_after_sorting() -> None:
    frame = pd.DataFrame(
        [
            {"日期": "2025-01-02", "开盘": 10, "收盘": 11, "最高": 12, "最低": 9},
            {"日期": "2025-01-02", "开盘": 11, "收盘": 12, "最高": 13, "最低": 10},
        ]
    )
    with pytest.raises(ValueError, match="duplicate trading dates"):
        fetch_ohlcv_series(
            "600519", adjust="", router=_Router(frame, "ths_fuyao"), now=_NOW
        )


def test_fuyao_metadata_and_declared_basis_are_preserved() -> None:
    frame = pd.DataFrame(
        [
            {
                "日期": "2025-01-02",
                "开盘": 10,
                "收盘": 11,
                "最高": 12,
                "最低": 9,
                "成交量": 10_000,
                "成交额": 105_000,
            }
        ]
    )
    frame.attrs.update(
        {
            "provider_as_of": "2026-08-19T10:30:00+08:00",
            "request_id": "fuyao-request-1",
            "period": "daily",
            "adjust": "forward",
        }
    )
    series = fetch_ohlcv_series(
        "600519",
        adjust="qfq",
        router=_Router(frame, "ths_fuyao"),
        now=_NOW,
    )

    assert series.metadata.provider_request_id == "fuyao-request-1"
    assert series.metadata.provider_as_of.isoformat() == "2026-08-19T10:30:00+08:00"
    assert series.metadata.quality is QualityStatus.ACCEPTED


def test_eltdx_rejects_adjustment_it_cannot_apply() -> None:
    with pytest.raises(SourceCapabilityError, match="does not support adjust='qfq'"):
        fetch_eltdx_kline(symbol="600519", adjust="qfq")


def test_eltdx_adjustment_skip_does_not_mark_source_unhealthy() -> None:
    fallback = pd.DataFrame([{"日期": "2025-01-02"}])
    router = SmartRouter()
    router.register("historical_kline", "eltdx", fetch_eltdx_kline, priority=1)
    router.register(
        "historical_kline", "fallback", lambda **_kwargs: fallback, priority=2
    )

    result, provider = router.route(
        "historical_kline", symbol="600519", adjust="qfq"
    )

    assert result is fallback
    assert provider == "fallback"
    health = {
        item["source"]: item for item in router.get_health_report()
    }
    assert health["historical_kline:eltdx"]["fail_count"] == 0
