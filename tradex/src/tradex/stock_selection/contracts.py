"""Strict business contracts for daily stock selection and evaluation."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SelectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FactorContributionV1(SelectionModel):
    factor: str = Field(min_length=1)
    label: str = Field(min_length=1)
    raw_value: float
    z_score: float
    weighted_contribution: float

    @field_validator("raw_value", "z_score", "weighted_contribution")
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("factor contributions must be finite")
        return value


class SelectionCandidateV1(SelectionModel):
    rank: int = Field(ge=1)
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    industry: str | None = None
    primary_business_name: str | None = None
    score: float = Field(ge=0, le=100)
    factor_coverage: float = Field(ge=0, le=1)
    reference_close: float = Field(gt=0)
    amount_cny: float = Field(ge=0)
    total_market_cap_cny: float = Field(gt=0)
    contributions: tuple[FactorContributionV1, ...]
    reasons: tuple[str, ...]
    risks: tuple[str, ...]

    @field_validator("score", "factor_coverage", "reference_close", "amount_cny", "total_market_cap_cny")
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("candidate values must be finite")
        return value


class LimitUpTendencyContributionV1(SelectionModel):
    factor: str = Field(min_length=1)
    label: str = Field(min_length=1)
    raw_value: float
    normalized_score: float = Field(ge=0, le=1)
    weighted_points: float = Field(ge=0, le=100)

    @field_validator("raw_value", "normalized_score", "weighted_points")
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("limit-up tendency contributions must be finite")
        return value


class LimitUpTendencyCandidateV1(SelectionModel):
    rank: int = Field(ge=1)
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    industry: str | None = None
    primary_business_name: str | None = None
    market: Literal["主板"]
    score: float = Field(ge=0, le=100)
    reference_close: float = Field(gt=0)
    daily_return_pct: float
    five_day_return_pct: float
    close_position_ratio: float = Field(ge=0, le=1)
    turnover_rate_pct: float = Field(ge=0)
    volume_ratio: float = Field(ge=0)
    amount_cny: float = Field(gt=0)
    amount_expansion_ratio: float = Field(gt=0)
    float_market_cap_cny: float = Field(gt=0)
    recent_limit_up_count: int = Field(ge=0, le=15)
    closed_at_limit_up: bool
    opportunity_stage: Literal["pre_limit_up", "limit_up_continuation"] | None = None
    breakout_distance_pct: float | None = None
    industry_positive_ratio: float | None = Field(default=None, ge=0, le=1)
    industry_peer_count: int = Field(default=0, ge=0)
    consecutive_limit_up_count: int = Field(default=0, ge=0, le=15)
    opened_at_limit_up: bool = False
    entry_feasibility_factor: float = Field(default=1.0, gt=0, le=1)
    contributions: tuple[LimitUpTendencyContributionV1, ...] = Field(min_length=1)
    reasons: tuple[str, ...] = Field(min_length=1)
    risks: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "score",
        "reference_close",
        "daily_return_pct",
        "five_day_return_pct",
        "close_position_ratio",
        "turnover_rate_pct",
        "volume_ratio",
        "amount_cny",
        "amount_expansion_ratio",
        "float_market_cap_cny",
        "entry_feasibility_factor",
    )
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("limit-up tendency candidate values must be finite")
        return value

    @field_validator("breakout_distance_pct", "industry_positive_ratio")
    @classmethod
    def finite_optional(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("limit-up tendency optional values must be finite")
        return value


class LimitUpTendencyScreenV1(SelectionModel):
    contract: Literal["stock_limit_up_tendency_screen.v1"] = (
        "stock_limit_up_tendency_screen.v1"
    )
    schema_version: Literal[1] = 1
    screen_version: Literal[
        "next-session-limit-up-tendency-main-board.v1",
        "next-session-limit-up-tendency-main-board.v2",
    ] = "next-session-limit-up-tendency-main-board.v2"
    title: str = Field(min_length=1)
    quality: Literal["accepted", "degraded", "unavailable"]
    target_count: Literal[20] = 20
    lookback_sessions: Literal[15] = 15
    universe_count: int = Field(ge=1)
    board_eligible_count: int = Field(ge=0)
    evaluated_count: int = Field(ge=0)
    selected_count: int = Field(ge=0, le=20)
    excluded_counts: dict[str, int]
    candidates: tuple[LimitUpTendencyCandidateV1, ...]
    methodology: tuple[str, ...]
    limitations: tuple[str, ...]

    @model_validator(mode="after")
    def validate_ranking(self) -> "LimitUpTendencyScreenV1":
        if self.selected_count != len(self.candidates):
            raise ValueError("limit-up tendency selected_count must match candidates")
        if self.board_eligible_count < self.evaluated_count:
            raise ValueError("limit-up tendency evaluated_count exceeds board eligibility")
        if self.evaluated_count < self.selected_count:
            raise ValueError("limit-up tendency selected_count exceeds evaluated_count")
        if [item.rank for item in self.candidates] != list(
            range(1, len(self.candidates) + 1)
        ):
            raise ValueError("limit-up tendency ranks must be contiguous")
        ids = [item.instrument_id for item in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("limit-up tendency candidates must be unique")
        if self.screen_version.endswith(".v2"):
            if any(item.opportunity_stage is None for item in self.candidates):
                raise ValueError("limit-up tendency v2 requires opportunity stages")
            if any(item.breakout_distance_pct is None for item in self.candidates):
                raise ValueError("limit-up tendency v2 requires breakout evidence")
            if any(
                item.closed_at_limit_up
                != (item.opportunity_stage == "limit_up_continuation")
                for item in self.candidates
            ):
                raise ValueError("limit-up tendency v2 stage must match limit-up state")
        return self


class StockPatternEvidenceV1(SelectionModel):
    trade_date: date
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    upper_shadow_pct_of_close: float = Field(ge=0)
    upper_shadow_body_multiple: float | None = Field(default=None, ge=0)
    upper_shadow_range_ratio: float = Field(ge=0, le=1)

    @field_validator(
        "open",
        "high",
        "low",
        "close",
        "upper_shadow_pct_of_close",
        "upper_shadow_body_multiple",
        "upper_shadow_range_ratio",
    )
    @classmethod
    def finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("pattern evidence values must be finite")
        return value


class StockPatternCandidateV1(SelectionModel):
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    industry: str | None = None
    primary_business_name: str | None = None
    market: Literal["主板"]
    reference_close: float = Field(gt=0)
    occurrence_count: int = Field(ge=1)
    latest_occurrence_date: date
    evidence: tuple[StockPatternEvidenceV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence(self) -> "StockPatternCandidateV1":
        dates = [item.trade_date for item in self.evidence]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise ValueError("pattern evidence dates must be unique and sorted")
        if self.occurrence_count != len(self.evidence):
            raise ValueError("pattern occurrence_count must match evidence")
        if self.latest_occurrence_date != dates[-1]:
            raise ValueError("latest pattern date must match the final evidence row")
        return self


class StockPatternScreenV1(SelectionModel):
    contract: Literal["stock_pattern_screen.v1"] = "stock_pattern_screen.v1"
    schema_version: Literal[1] = 1
    screen_version: Literal[
        "long-upper-shadow-main-board.v1",
        "long-upper-shadow-main-board.v2",
        "long-upper-shadow-main-board.v3",
    ] = (
        "long-upper-shadow-main-board.v3"
    )
    title: str = Field(min_length=1)
    quality: Literal["accepted", "degraded", "unavailable"]
    lookback_sessions: Literal[10, 15] = 10
    minimum_occurrences: Literal[2] = 2
    upper_shadow_min_pct_of_close: Literal[3.0] = 3.0
    upper_shadow_min_body_multiple: Literal[2.0] = 2.0
    upper_shadow_min_range_ratio: Literal[0.5] = 0.5
    limit_up_exclusion_lookback_sessions: Literal[10] | None = None
    universe_count: int = Field(ge=1)
    board_eligible_count: int = Field(ge=0)
    evaluated_count: int = Field(ge=0)
    matched_count: int = Field(ge=0)
    excluded_counts: dict[str, int]
    candidates: tuple[StockPatternCandidateV1, ...]
    methodology: tuple[str, ...]
    limitations: tuple[str, ...]

    @model_validator(mode="after")
    def validate_counts(self) -> "StockPatternScreenV1":
        if (
            self.screen_version == "long-upper-shadow-main-board.v1"
            and (
                self.lookback_sessions != 15
                or self.limit_up_exclusion_lookback_sessions is not None
            )
        ):
            raise ValueError("long-upper-shadow v1 requires 15 sessions without a limit-up gate")
        if (
            self.screen_version == "long-upper-shadow-main-board.v2"
            and (
                self.lookback_sessions != 15
                or self.limit_up_exclusion_lookback_sessions != 10
            )
        ):
            raise ValueError("long-upper-shadow v2 requires 15 sessions and a 10-session limit-up gate")
        if (
            self.screen_version == "long-upper-shadow-main-board.v3"
            and (
                self.lookback_sessions != 10
                or self.limit_up_exclusion_lookback_sessions != 10
            )
        ):
            raise ValueError("long-upper-shadow v3 requires one 10-session rule window")
        if self.matched_count != len(self.candidates):
            raise ValueError("pattern matched_count must match candidates")
        if self.board_eligible_count < self.evaluated_count:
            raise ValueError("pattern evaluated_count exceeds board eligibility")
        if self.evaluated_count < self.matched_count:
            raise ValueError("pattern matched_count exceeds evaluated_count")
        ids = [item.instrument_id for item in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("pattern candidates must be unique")
        return self


class DailyStockSelectionV1(SelectionModel):
    contract: Literal["daily_stock_selection.v1"] = "daily_stock_selection.v1"
    schema_version: Literal[1] = 1
    config_version: str = Field(min_length=1)
    selection_id: str = Field(pattern=r"^daily-stock-selection:[0-9a-f]{24}$")
    trade_date: date
    generated_at: datetime
    source_contract: Literal["daily_stock_factor_snapshot.v1"]
    source_quality: Literal["accepted", "degraded"]
    source_provider_as_of: datetime | None = None
    universe_count: int = Field(ge=1)
    eligible_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    excluded_counts: dict[str, int]
    candidates: tuple[SelectionCandidateV1, ...]
    pattern_screens: tuple[StockPatternScreenV1, ...] = ()
    limit_up_tendency_screens: tuple[LimitUpTendencyScreenV1, ...] = ()
    methodology: tuple[str, ...]
    limitations: tuple[str, ...]

    @field_validator("generated_at", "source_provider_as_of")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("selection timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_ranking(self) -> "DailyStockSelectionV1":
        if self.selected_count != len(self.candidates):
            raise ValueError("selected_count must match candidates")
        if self.eligible_count < self.selected_count:
            raise ValueError("eligible_count cannot be below selected_count")
        if [item.rank for item in self.candidates] != list(
            range(1, len(self.candidates) + 1)
        ):
            raise ValueError("candidate ranks must be contiguous")
        ids = [item.instrument_id for item in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("selection candidates must be unique")
        screen_versions = [item.screen_version for item in self.pattern_screens]
        if len(screen_versions) != len(set(screen_versions)):
            raise ValueError("selection pattern screens must be unique by version")
        tendency_versions = [
            item.screen_version for item in self.limit_up_tendency_screens
        ]
        if len(tendency_versions) != len(set(tendency_versions)):
            raise ValueError("selection limit-up tendency screens must be unique by version")
        return self


class DailyStockSelectionOutcomeV1(SelectionModel):
    contract: Literal["daily_stock_selection_outcome.v1"] = (
        "daily_stock_selection_outcome.v1"
    )
    schema_version: Literal[1] = 1
    selection_id: str
    signal_trade_date: date
    evaluation_trade_date: date
    evaluated_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    coverage: float = Field(ge=0, le=1)
    portfolio_return_pct: float | None = None
    benchmark_return_pct: float | None = None
    excess_return_pct: float | None = None
    verdict: Literal["supported", "not_supported", "mixed", "unverifiable"]

    @field_validator(
        "coverage",
        "portfolio_return_pct",
        "benchmark_return_pct",
        "excess_return_pct",
    )
    @classmethod
    def finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("outcome values must be finite")
        return value


class SelectionBacktestTradeV1(SelectionModel):
    signal_trade_date: date
    entry_trade_date: date
    exit_trade_date: date
    selected_count: int = Field(ge=1)
    evaluated_count: int = Field(ge=1)
    coverage: float = Field(gt=0, le=1)
    portfolio_return_pct: float
    benchmark_return_pct: float
    excess_return_pct: float


class SelectionBacktestReportV1(SelectionModel):
    contract: Literal["daily_stock_selection_backtest.v1"] = (
        "daily_stock_selection_backtest.v1"
    )
    schema_version: Literal[1] = 1
    config_version: str
    horizon_sessions: int = Field(ge=1)
    round_trip_cost_pct: float = Field(ge=0)
    signal_count: int = Field(ge=0)
    average_return_pct: float | None = None
    average_excess_return_pct: float | None = None
    hit_rate: float | None = Field(default=None, ge=0, le=1)
    sharpe_ratio: float | None = None
    max_drawdown_pct: float | None = None
    trades: tuple[SelectionBacktestTradeV1, ...]


__all__ = [
    "DailyStockSelectionOutcomeV1",
    "DailyStockSelectionV1",
    "FactorContributionV1",
    "LimitUpTendencyCandidateV1",
    "LimitUpTendencyContributionV1",
    "LimitUpTendencyScreenV1",
    "StockPatternCandidateV1",
    "StockPatternEvidenceV1",
    "StockPatternScreenV1",
    "SelectionBacktestReportV1",
    "SelectionBacktestTradeV1",
    "SelectionCandidateV1",
]
