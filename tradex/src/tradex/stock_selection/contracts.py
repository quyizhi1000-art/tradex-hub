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
    screen_version: Literal["long-upper-shadow-main-board.v1"] = (
        "long-upper-shadow-main-board.v1"
    )
    title: str = Field(min_length=1)
    quality: Literal["accepted", "degraded", "unavailable"]
    lookback_sessions: Literal[15] = 15
    minimum_occurrences: Literal[2] = 2
    upper_shadow_min_pct_of_close: Literal[3.0] = 3.0
    upper_shadow_min_body_multiple: Literal[2.0] = 2.0
    upper_shadow_min_range_ratio: Literal[0.5] = 0.5
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
    "StockPatternCandidateV1",
    "StockPatternEvidenceV1",
    "StockPatternScreenV1",
    "SelectionBacktestReportV1",
    "SelectionBacktestTradeV1",
    "SelectionCandidateV1",
]
