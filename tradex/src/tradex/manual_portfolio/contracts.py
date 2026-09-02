"""Strict business contracts for manually maintained portfolio observation."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_ENABLED_INSTRUMENTS = 40


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ManualPortfolioEntryV1(ContractModel):
    contract: Literal["manual_portfolio_entry.v1"] = "manual_portfolio_entry.v1"
    schema_version: Literal[1] = 1
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    display_name: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=500)
    enabled: bool
    code_validation_status: Literal["catalog_verified", "format_valid_unverified"]
    attribution_status: Literal[
        "verified", "corroborated", "provider_only", "disputed", "stale",
        "unresolved", "not_available"
    ]
    added_at: datetime
    updated_at: datetime
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("added_at", "updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("manual portfolio timestamps must include a timezone")
        return value


class ManualPortfolioV1(ContractModel):
    contract: Literal["manual_portfolio.v1"] = "manual_portfolio.v1"
    schema_version: Literal[1] = 1
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    manual_fact_notice: Literal[
        "手动维护，并非券商账户事实"
    ] = "手动维护，并非券商账户事实"
    enabled_limit: Literal[40] = 40
    enabled_count: int = Field(ge=0, le=40)
    items: tuple[ManualPortfolioEntryV1, ...]

    @field_validator("generated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("manual portfolio timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_counts(self) -> "ManualPortfolioV1":
        if self.enabled_count != sum(item.enabled for item in self.items):
            raise ValueError("enabled_count must match portfolio items")
        if len({item.instrument_id for item in self.items}) != len(self.items):
            raise ValueError("manual portfolio instruments must be unique")
        return self


class ManualPortfolioSampleV1(ContractModel):
    observed_at: datetime
    price: float = Field(gt=0)

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("portfolio sample timestamps must include a timezone")
        return value

    @field_validator("price")
    @classmethod
    def require_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("portfolio price must be finite")
        return value


class ManualPortfolioQuoteV1(ContractModel):
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    trading_date: date | None = None
    status: Literal["accepted", "degraded", "stale", "unavailable"]
    reason: str | None = None
    last_price: float | None = Field(default=None, gt=0)
    session_change_pct: float | None = None
    session_high: float | None = Field(default=None, gt=0)
    session_low: float | None = Field(default=None, gt=0)
    provider: str | None = None
    provider_request_id: str | None = None
    provider_as_of: datetime | None = None
    fetched_at: datetime | None = None
    quality_flags: tuple[str, ...] = ()
    samples: tuple[ManualPortfolioSampleV1, ...] = ()

    @field_validator("provider_as_of", "fetched_at")
    @classmethod
    def require_optional_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("portfolio quote timestamps must include a timezone")
        return value

    @field_validator("session_change_pct")
    @classmethod
    def require_finite_change(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("portfolio change must be finite")
        return value

    @model_validator(mode="after")
    def validate_status(self) -> "ManualPortfolioQuoteV1":
        if self.status == "unavailable" and not self.reason:
            raise ValueError("unavailable quote requires a reason")
        if self.status != "unavailable" and self.last_price is None:
            raise ValueError("displayable quote requires a last price")
        return self


class ManualPortfolioAlertV1(ContractModel):
    contract: Literal["manual_portfolio_alert.v1"] = "manual_portfolio_alert.v1"
    schema_version: Literal[1] = 1
    alert_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    direction: Literal["up", "down"]
    observed_at: datetime
    title: str = Field(min_length=1)
    message: str = Field(min_length=1)
    evidence: tuple[ManualPortfolioSampleV1, ...] = Field(min_length=2)
    invalidation_condition: str = Field(min_length=1)
    cooldown_minutes: Literal[30] = 30
    observation_only: Literal[True] = True

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("portfolio alert timestamp must include a timezone")
        return value


class ManualPortfolioMarketSnapshotV1(ContractModel):
    contract: Literal[
        "manual_portfolio_market_snapshot.v1"
    ] = "manual_portfolio_market_snapshot.v1"
    schema_version: Literal[1] = 1
    portfolio_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    trading_date: date | None = None
    refresh_owner: Literal["tradex.dashboard.collector_worker"] = (
        "tradex.dashboard.collector_worker"
    )
    item_count: int = Field(ge=0, le=40)
    items: tuple[ManualPortfolioQuoteV1, ...]
    alerts: tuple[ManualPortfolioAlertV1, ...] = ()

    @field_validator("generated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("portfolio snapshot timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_count(self) -> "ManualPortfolioMarketSnapshotV1":
        if self.item_count != len(self.items):
            raise ValueError("item_count must match quote items")
        return self


class ManualPortfolioPricePlanV1(ContractModel):
    basis: Literal["previous_session_range"] = "previous_session_range"
    previous_close: float = Field(gt=0)
    previous_low: float = Field(gt=0)
    previous_midpoint: float = Field(gt=0)
    previous_high: float = Field(gt=0)
    pullback_observation_zone: tuple[float, float]
    pressure_observation_zone: tuple[float, float]
    risk_reference: float = Field(gt=0)
    deterministic_target: Literal[False] = False
    note: Literal[
        "区间来自上一交易日价格路径，只作条件观察，不是必到价或买卖指令"
    ] = "区间来自上一交易日价格路径，只作条件观察，不是必到价或买卖指令"

    @model_validator(mode="after")
    def validate_levels(self) -> "ManualPortfolioPricePlanV1":
        pullback_low, pullback_high = self.pullback_observation_zone
        pressure_low, pressure_high = self.pressure_observation_zone
        values = (
            self.previous_low,
            self.previous_midpoint,
            self.previous_high,
            pullback_low,
            pullback_high,
            pressure_low,
            pressure_high,
            self.risk_reference,
        )
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("portfolio price-plan levels must be finite and positive")
        if not self.previous_low <= self.previous_midpoint <= self.previous_high:
            raise ValueError("portfolio price-plan midpoint must be inside prior range")
        if pullback_low > pullback_high or pressure_low > pressure_high:
            raise ValueError("portfolio price-plan zones must be ordered")
        return self


class ManualPortfolioMarketContextV1(ContractModel):
    contract: Literal[
        "manual_portfolio_market_context.v1"
    ] = "manual_portfolio_market_context.v1"
    schema_version: Literal[1] = 1
    source_trade_date: date | None = None
    bias: Literal["constructive", "balanced", "defensive", "uncertain"]
    confidence: Literal["strong", "moderate", "weak", "abstain"]
    thesis: str
    expected_shape: str
    confirmation: str
    invalidation: str
    risk_control: str
    supporting_evidence: tuple[str, ...] = ()
    counter_evidence: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


class ManualPortfolioOutlookItemV1(ContractModel):
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    status: Literal["conditional", "abstain"]
    evidence_status: Literal["accepted", "degraded", "stale", "unavailable"]
    next_session: str
    next_2_to_5_sessions: str
    confirmation_conditions: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    limitations: tuple[str, ...]
    price_plan: ManualPortfolioPricePlanV1 | None = None
    opening_scenarios: tuple[str, ...] = ()
    market_scenarios: tuple[str, ...] = ()


class ManualPortfolioOutlookV1(ContractModel):
    contract: Literal[
        "manual_portfolio_outlook.v1"
    ] = "manual_portfolio_outlook.v1"
    schema_version: Literal[1] = 1
    portfolio_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_snapshot_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_trading_date: date | None = None
    generated_at: datetime
    horizon: Literal["next_session_and_2_to_5_sessions"] = (
        "next_session_and_2_to_5_sessions"
    )
    deterministic_price_prediction: Literal[False] = False
    market_context: ManualPortfolioMarketContextV1 | None = None
    items: tuple[ManualPortfolioOutlookItemV1, ...]

    @field_validator("generated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("portfolio outlook timestamp must include a timezone")
        return value


class ManualPortfolioIntradayAnalysisItemV1(ContractModel):
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    status: Literal["conditional", "abstain"]
    evidence_status: Literal["accepted", "degraded", "stale", "unavailable"]
    current_observation: str
    confirmation_conditions: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    limitations: tuple[str, ...]


class ManualPortfolioIntradayAnalysisV1(ContractModel):
    contract: Literal[
        "manual_portfolio_intraday_analysis.v1"
    ] = "manual_portfolio_intraday_analysis.v1"
    schema_version: Literal[1] = 1
    portfolio_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_snapshot_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_trading_date: date | None = None
    generated_at: datetime
    analysis_scope: Literal["current_session"] = "current_session"
    deterministic_price_prediction: Literal[False] = False
    items: tuple[ManualPortfolioIntradayAnalysisItemV1, ...]

    @field_validator("generated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("portfolio intraday analysis timestamp must include a timezone")
        return value


class ManualPortfolioOutlookReadinessV1(ContractModel):
    contract: Literal[
        "manual_portfolio_outlook_readiness.v1"
    ] = "manual_portfolio_outlook_readiness.v1"
    schema_version: Literal[1] = 1
    state: Literal["ready", "waiting_for_market", "empty"]
    portfolio_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_portfolio_revision: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    market_generated_at: datetime | None = None
    next_collection_at: datetime | None = None
    automatic_generation_requested: bool = False

    @field_validator("market_generated_at", "next_collection_at")
    @classmethod
    def require_optional_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("portfolio outlook readiness timestamps must include a timezone")
        return value


__all__ = [
    "MAX_ENABLED_INSTRUMENTS",
    "ManualPortfolioAlertV1",
    "ManualPortfolioEntryV1",
    "ManualPortfolioIntradayAnalysisItemV1",
    "ManualPortfolioIntradayAnalysisV1",
    "ManualPortfolioMarketSnapshotV1",
    "ManualPortfolioMarketContextV1",
    "ManualPortfolioOutlookItemV1",
    "ManualPortfolioOutlookReadinessV1",
    "ManualPortfolioOutlookV1",
    "ManualPortfolioPricePlanV1",
    "ManualPortfolioQuoteV1",
    "ManualPortfolioSampleV1",
    "ManualPortfolioV1",
]
