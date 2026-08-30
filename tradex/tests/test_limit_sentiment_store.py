from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from tradex.data_gateway.contracts import ContractMetadata, QualityStatus
from tradex.data_gateway.limit_sentiment_contracts import LimitSentimentDailyV1
from tradex.market_watch.limit_sentiment_store import LimitSentimentStore


NOW = datetime(2026, 8, 28, 16, 15, tzinfo=ZoneInfo("Asia/Shanghai"))


def _sentiment(revision: str = "a" * 64) -> LimitSentimentDailyV1:
    return LimitSentimentDailyV1(
        metadata=ContractMetadata(
            contract="limit_sentiment_daily.v1",
            provider="fixture",
            fetched_at=NOW,
            quality=QualityStatus.DEGRADED,
            quality_flags=("provider_timestamp_missing",),
        ),
        trade_date=date(2026, 8, 28),
        previous_trade_date=date(2026, 8, 27),
        source_revision=revision,
        limit_up_count=81,
        broken_count=19,
        attempted_count=100,
        seal_rate_pct=81,
        break_rate_pct=19,
        previous_limit_up_count=77,
        previous_feedback_eligible_count=77,
        previous_feedback_coverage=1,
        previous_limit_up_continued_count=17,
        continuation_rate_pct=17 / 77 * 100,
        previous_first_board_count=59,
        first_board_promoted_count=9,
        first_board_promotion_rate_pct=9 / 59 * 100,
        previous_limit_up_avg_open_premium_pct=3.36,
        previous_limit_up_avg_close_premium_pct=2.73,
        previous_limit_up_median_close_premium_pct=1.84,
        previous_limit_up_red_close_rate_pct=61.04,
    )


def test_limit_sentiment_store_is_immutable_and_returns_latest_revision(tmp_path):
    path = tmp_path / "sentiment.sqlite3"
    with LimitSentimentStore(path) as store:
        assert store.record(_sentiment())["action"] == "inserted"
        assert store.record(_sentiment())["action"] == "existing"
        assert store.record(_sentiment("b" * 64))["action"] == "inserted"
        result = store.get("2026-08-28")
        assert result is not None
        assert result.source_revision == "b" * 64
        assert result.break_rate_pct == 19

    with LimitSentimentStore(path, read_only=True) as reader:
        result = reader.get(date(2026, 8, 28))
        assert result is not None
        assert result.limit_up_count == 81
