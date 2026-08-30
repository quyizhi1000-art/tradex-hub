from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from astock_signals.smart_router import SmartRouter
from tradex.data_gateway.contracts import QualityStatus
from tradex.data_gateway.limit_sentiment import fetch_limit_sentiment_daily


NOW = datetime(2026, 8, 28, 16, 15, tzinfo=ZoneInfo("Asia/Shanghai"))


def _payload(*, overlap: bool = False):
    return {
        "trade_date": "2026-08-28",
        "previous_trade_date": "2026-08-27",
        "request_id": "provider-bundle-1",
        "limit_up": [
            {
                "trade_date": "20260828",
                "ts_code": "000001.SZ",
                "name": "晋级股",
                "pct_chg": 10.0,
                "tag": "2天2板",
            },
            {
                "trade_date": "20260828",
                "ts_code": "600001.SH",
                "name": "新首板",
                "pct_chg": 10.0,
                "tag": "首板",
            },
        ],
        "broken": [
            {
                "trade_date": "20260828",
                "ts_code": "000001.SZ" if overlap else "000002.SZ",
                "name": "炸板股",
                "pct_chg": 4.0,
                "tag": "首板",
            }
        ],
        "previous_limit_up": [
            {
                "trade_date": "20260827",
                "ts_code": "000001.SZ",
                "name": "晋级股",
                "pct_chg": 10.0,
                "tag": "首板",
            },
            {
                "trade_date": "20260827",
                "ts_code": "000003.SZ",
                "name": "断板股",
                "pct_chg": 10.0,
                "tag": "2天2板",
            },
        ],
        "daily": [
            {
                "trade_date": "20260828",
                "ts_code": "000001.SZ",
                "open": 11.0,
                "pre_close": 10.0,
                "close": 11.0,
                "pct_chg": 10.0,
            },
            {
                "trade_date": "20260828",
                "ts_code": "000003.SZ",
                "open": 9.5,
                "pre_close": 10.0,
                "close": 9.8,
                "pct_chg": -2.0,
            },
        ],
    }


def test_limit_sentiment_uses_one_provider_consistent_denominator():
    router = SmartRouter()
    router.register(
        "limit_sentiment_daily",
        "tushare",
        lambda trade_date, previous_trade_date: _payload(),
        priority=1,
    )

    result = fetch_limit_sentiment_daily(
        "2026-08-28",
        "2026-08-27",
        router=router,
        now=NOW,
    )

    assert result.metadata.contract == "limit_sentiment_daily.v1"
    assert result.metadata.provider == "tushare_limit_list_ths"
    assert result.metadata.quality is QualityStatus.DEGRADED
    assert result.metadata.quality_flags == ("provider_timestamp_missing",)
    assert result.limit_up_count == 2
    assert result.broken_count == 1
    assert result.attempted_count == 3
    assert result.seal_rate_pct == pytest.approx(66.6666667)
    assert result.break_rate_pct == pytest.approx(33.3333333)
    assert result.previous_limit_up_count == 2
    assert result.previous_limit_up_continued_count == 1
    assert result.continuation_rate_pct == 50
    assert result.previous_first_board_count == 1
    assert result.first_board_promoted_count == 1
    assert result.first_board_promotion_rate_pct == 100
    assert result.previous_limit_up_avg_open_premium_pct == pytest.approx(2.5)
    assert result.previous_limit_up_avg_close_premium_pct == 4
    assert result.previous_limit_up_median_close_premium_pct == 4
    assert result.previous_limit_up_red_close_rate_pct == 50
    assert len(result.source_revision) == 64


def test_limit_sentiment_rejects_overlapping_success_and_broken_pools():
    router = SmartRouter()
    router.register(
        "limit_sentiment_daily",
        "tushare",
        lambda trade_date, previous_trade_date: _payload(overlap=True),
        priority=1,
    )

    with pytest.raises(Exception, match="overlap"):
        fetch_limit_sentiment_daily(
            "2026-08-28",
            "2026-08-27",
            router=router,
            now=NOW,
        )
