"""Canonical post-close A-share limit-sentiment contracts."""

from __future__ import annotations

import math
from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from .contracts import ContractMetadata, ContractModel


class LimitSentimentDailyV1(ContractModel):
    """One provider-consistent daily limit-up quality and payoff snapshot."""

    metadata: ContractMetadata
    trade_date: date
    previous_trade_date: date
    source_revision: str = Field(pattern=r"^[0-9a-f]{64}$")

    limit_up_count: int = Field(ge=0)
    broken_count: int = Field(ge=0)
    attempted_count: int = Field(ge=0)
    seal_rate_pct: float | None = Field(default=None, ge=0, le=100)
    break_rate_pct: float | None = Field(default=None, ge=0, le=100)

    previous_limit_up_count: int = Field(ge=0)
    previous_feedback_eligible_count: int = Field(ge=0)
    previous_feedback_coverage: float = Field(ge=0, le=1)
    previous_limit_up_continued_count: int = Field(ge=0)
    continuation_rate_pct: float | None = Field(default=None, ge=0, le=100)
    previous_first_board_count: int = Field(ge=0)
    first_board_promoted_count: int = Field(ge=0)
    first_board_promotion_rate_pct: float | None = Field(
        default=None, ge=0, le=100
    )
    previous_limit_up_avg_open_premium_pct: float | None = None
    previous_limit_up_avg_close_premium_pct: float | None = None
    previous_limit_up_median_close_premium_pct: float | None = None
    previous_limit_up_red_close_rate_pct: float | None = Field(
        default=None, ge=0, le=100
    )

    @model_validator(mode="after")
    def validate_limit_sentiment(self) -> "LimitSentimentDailyV1":
        if (
            self.metadata.contract != "limit_sentiment_daily.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError(
                "limit sentiment requires limit_sentiment_daily.v1 metadata"
            )
        if self.previous_trade_date >= self.trade_date:
            raise ValueError("previous trade date must precede sentiment trade date")
        if self.attempted_count != self.limit_up_count + self.broken_count:
            raise ValueError("attempted count must equal limit-up plus broken count")
        if self.attempted_count:
            if self.seal_rate_pct is None or self.break_rate_pct is None:
                raise ValueError("non-empty attempted pool requires seal and break rates")
            if not math.isclose(
                self.seal_rate_pct + self.break_rate_pct,
                100.0,
                rel_tol=1e-9,
                abs_tol=1e-9,
            ):
                raise ValueError("seal and break rates must sum to 100 percent")
        elif self.seal_rate_pct is not None or self.break_rate_pct is not None:
            raise ValueError("empty attempted pool must not fabricate rates")
        if self.previous_feedback_eligible_count > self.previous_limit_up_count:
            raise ValueError("eligible previous feedback exceeds previous limit-up pool")
        expected_coverage = (
            self.previous_feedback_eligible_count / self.previous_limit_up_count
            if self.previous_limit_up_count
            else 1.0
        )
        if not math.isclose(
            self.previous_feedback_coverage,
            expected_coverage,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise ValueError("previous feedback coverage is inconsistent")
        if self.previous_limit_up_continued_count > self.previous_limit_up_count:
            raise ValueError("continued count exceeds previous limit-up pool")
        if self.first_board_promoted_count > self.previous_first_board_count:
            raise ValueError("promoted first-board count exceeds its denominator")
        if self.previous_limit_up_count == 0 and self.continuation_rate_pct is not None:
            raise ValueError("empty previous pool must not fabricate continuation rate")
        if (
            self.previous_first_board_count == 0
            and self.first_board_promotion_rate_pct is not None
        ):
            raise ValueError("empty first-board pool must not fabricate promotion rate")
        for value in (
            self.previous_limit_up_avg_open_premium_pct,
            self.previous_limit_up_avg_close_premium_pct,
            self.previous_limit_up_median_close_premium_pct,
        ):
            if value is not None and not math.isfinite(value):
                raise ValueError("premium values must be finite")
        return self


__all__ = ["LimitSentimentDailyV1"]
