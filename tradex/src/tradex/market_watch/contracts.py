"""Strict, provider-neutral contracts for the desktop market watch.

The contract describes market context and behavioural guardrails.  It does not
contain stock recommendations, target prices, probabilities, or return
forecasts.  Percent changes are percentage points; ratios are decimals.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ContractModel(BaseModel):
    """Immutable canonical model that rejects provider fields by default."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class FreshnessStatus(str, Enum):
    FRESH = "fresh"
    DEGRADED = "degraded"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class ComponentQuality(str, Enum):
    ACCEPTED = "accepted"
    DEGRADED = "degraded"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class MarketRegime(str, Enum):
    ATTACK = "attack"
    DEFENSE = "defense"
    MIXED = "mixed"
    UNCERTAIN = "uncertain"


class GuardrailSeverity(str, Enum):
    CALM = "calm"
    CAUTION = "caution"
    STOP = "stop"


class ConclusionStrength(str, Enum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    ABSTAIN = "abstain"


class IndexRole(str, Enum):
    BROAD_MARKET = "broad_market"
    LARGE_CAP = "large_cap"
    SMALL_CAP = "small_cap"
    GROWTH = "growth"


class TurnoverDirection(str, Enum):
    EXPAND = "expand"
    SHRINK = "shrink"
    FLAT = "flat"


class SectorTag(str, Enum):
    ATTACK = "attack"
    DEFENSE = "defense"
    CYCLICAL = "cyclical"
    SAFE_HAVEN = "safe_haven"
    WEIGHT_SUPPORT = "weight_support"
    EVENT = "event"
    MIXED = "mixed"


class RotationDirection(str, Enum):
    STRENGTHENING = "strengthening"
    WEAKENING = "weakening"
    STABLE = "stable"
    UNKNOWN = "unknown"


class EvidenceStrength(str, Enum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    UNKNOWN = "unknown"


class SectorFlowTrajectoryStatus(str, Enum):
    READY = "ready"
    COLLECTING = "collecting"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class SectorFlowObservationTier(str, Enum):
    CONFIRMED_STRENGTHENING = "confirmed_strengthening"
    STRONG_PENDING = "strong_pending"
    FUNDS_LEADING = "funds_leading"
    DIVERGENCE = "divergence"
    RETREAT = "retreat"
    OBSERVING = "observing"
    UNAVAILABLE = "unavailable"


class SectorFlowDirection(str, Enum):
    INFLOW = "inflow"
    OUTFLOW = "outflow"
    FLAT = "flat"
    UNKNOWN = "unknown"


class MarketPhase(str, Enum):
    PRE_OPEN = "pre_open"
    OPENING_OBSERVATION = "opening_observation"
    TRADING = "trading"
    MIDDAY_BREAK = "midday_break"
    CLOSED = "closed"
    NON_TRADING = "non_trading"
    UNKNOWN = "unknown"


def _require_timezone(value: datetime | None, field_name: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field_name} must include a timezone")
    return value


def _require_finite(value: float | None, field_name: str) -> float | None:
    if value is not None and not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    return value


class MarketStateV1(ContractModel):
    phase: MarketPhase
    is_open: bool
    trading_date: date

    @model_validator(mode="after")
    def validate_open_state(self) -> "MarketStateV1":
        open_phases = {MarketPhase.OPENING_OBSERVATION, MarketPhase.TRADING}
        if self.is_open != (self.phase in open_phases):
            raise ValueError("market_state is_open does not match phase")
        return self


class FreshnessComponentV1(ContractModel):
    component: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    status: FreshnessStatus
    quality: ComponentQuality
    provider_as_of: datetime | None = None
    fetched_at: datetime | None = None
    flags: tuple[str, ...] = ()

    @field_validator("provider_as_of", "fetched_at")
    @classmethod
    def require_aware_time(cls, value: datetime | None, info):
        return _require_timezone(value, info.field_name)

    @field_validator("flags")
    @classmethod
    def unique_flags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("freshness component flags must be unique")
        return value

    @model_validator(mode="after")
    def validate_status_quality(self) -> "FreshnessComponentV1":
        if self.status == FreshnessStatus.FRESH:
            if self.quality != ComponentQuality.ACCEPTED:
                raise ValueError("a fresh component must have accepted quality")
            if self.fetched_at is None:
                raise ValueError("a fresh component requires fetched_at")
        if self.status == FreshnessStatus.UNAVAILABLE and self.quality not in {
            ComponentQuality.REJECTED,
            ComponentQuality.UNAVAILABLE,
        }:
            raise ValueError("an unavailable component requires rejected/unavailable quality")
        return self


class FreshnessV1(ContractModel):
    status: FreshnessStatus
    components: tuple[FreshnessComponentV1, ...] = Field(min_length=4)
    flags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_components(self) -> "FreshnessV1":
        names = [item.component for item in self.components]
        if len(names) != len(set(names)):
            raise ValueError("freshness components must be unique")
        if tuple(names) != ("indices", "breadth", "turnover", "rotation"):
            raise ValueError("freshness must contain the four canonical components in order")
        if len(self.flags) != len(set(self.flags)):
            raise ValueError("freshness flags must be unique")
        statuses = {item.status for item in self.components}
        expected = (
            FreshnessStatus.STALE
            if FreshnessStatus.STALE in statuses
            else FreshnessStatus.UNAVAILABLE
            if statuses == {FreshnessStatus.UNAVAILABLE}
            else FreshnessStatus.DEGRADED
            if statuses - {FreshnessStatus.FRESH}
            else FreshnessStatus.FRESH
        )
        if self.status != expected:
            raise ValueError("freshness status does not match component statuses")
        return self


_ROLE_INSTRUMENTS: dict[IndexRole, frozenset[str]] = {
    IndexRole.BROAD_MARKET: frozenset({"000001.SH"}),
    IndexRole.LARGE_CAP: frozenset({"000300.SH"}),
    IndexRole.SMALL_CAP: frozenset({"000852.SH", "399852.SZ"}),
    IndexRole.GROWTH: frozenset({"399006.SZ"}),
}


class IndexSnapshotV1(ContractModel):
    role: IndexRole
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ)$")
    name: str = Field(min_length=1)
    available: bool
    level: float | None = Field(default=None, ge=0)
    change_pct: float | None = None
    provider_as_of: datetime | None = None
    quality: ComponentQuality
    quality_flags: tuple[str, ...] = ()

    @field_validator("level", "change_pct")
    @classmethod
    def finite_numbers(cls, value: float | None, info):
        return _require_finite(value, info.field_name)

    @field_validator("provider_as_of")
    @classmethod
    def aware_provider_time(cls, value: datetime | None):
        return _require_timezone(value, "provider_as_of")

    @model_validator(mode="after")
    def validate_role_quote(self) -> "IndexSnapshotV1":
        if self.instrument_id not in _ROLE_INSTRUMENTS[self.role]:
            raise ValueError("index instrument_id does not match its market role")
        if self.available and self.level is None and self.change_pct is None:
            raise ValueError("an available index requires level or change_pct")
        if not self.available and (self.level is not None or self.change_pct is not None):
            raise ValueError("an unavailable index cannot contain quote values")
        if not self.available and self.quality not in {
            ComponentQuality.REJECTED,
            ComponentQuality.UNAVAILABLE,
        }:
            raise ValueError("an unavailable index requires rejected/unavailable quality")
        if self.available and self.quality in {
            ComponentQuality.REJECTED,
            ComponentQuality.UNAVAILABLE,
        }:
            raise ValueError("an available index cannot have rejected/unavailable quality")
        if self.available and (self.level is None or self.change_pct is None):
            if self.quality != ComponentQuality.DEGRADED:
                raise ValueError("a partial index quote must have degraded quality")
        return self


class BreadthSnapshotV1(ContractModel):
    available: bool
    up_count: int | None = Field(default=None, ge=0)
    down_count: int | None = Field(default=None, ge=0)
    flat_count: int | None = Field(default=None, ge=0)
    unclassified_count: int | None = Field(default=None, ge=0)
    total_count: int | None = Field(default=None, ge=0)
    advance_ratio: float | None = Field(default=None, ge=0, le=1)
    provider_as_of: datetime | None = None
    quality: ComponentQuality
    quality_flags: tuple[str, ...] = ()
    reason: str | None = None

    @field_validator("advance_ratio")
    @classmethod
    def finite_ratio(cls, value: float | None):
        return _require_finite(value, "advance_ratio")

    @field_validator("provider_as_of")
    @classmethod
    def aware_provider_time(cls, value: datetime | None):
        return _require_timezone(value, "provider_as_of")

    @model_validator(mode="after")
    def validate_breadth(self) -> "BreadthSnapshotV1":
        counts = (
            self.up_count,
            self.down_count,
            self.flat_count,
            self.unclassified_count,
            self.total_count,
        )
        if not self.available:
            if not self.reason:
                raise ValueError("unavailable breadth requires a reason")
            if any(value is not None for value in counts) or self.advance_ratio is not None:
                raise ValueError("unavailable breadth cannot contain participation values")
            return self
        if any(value is None for value in counts):
            raise ValueError("available breadth requires all participation counts")
        expected_total = sum(
            value or 0
            for value in (
                self.up_count,
                self.down_count,
                self.flat_count,
                self.unclassified_count,
            )
        )
        if self.total_count != expected_total:
            raise ValueError("breadth total_count does not match participation counts")
        directional = (self.up_count or 0) + (self.down_count or 0)
        if directional:
            expected_ratio = (self.up_count or 0) / directional
            if self.advance_ratio is None or not math.isclose(
                self.advance_ratio, expected_ratio, rel_tol=1e-9, abs_tol=1e-9
            ):
                raise ValueError("breadth advance_ratio does not match up/down counts")
        elif self.advance_ratio is not None:
            raise ValueError("breadth advance_ratio must be null without directional rows")
        if self.reason is not None:
            raise ValueError("available breadth cannot contain an unavailable reason")
        return self


class TurnoverSnapshotV1(ContractModel):
    available: bool
    today_date: date | None = None
    previous_date: date | None = None
    as_of: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    today_amount_cny: float | None = Field(default=None, ge=0)
    previous_same_time_amount_cny: float | None = Field(default=None, gt=0)
    difference_cny: float | None = None
    difference_ratio: float | None = None
    neutral_band_ratio: float = Field(default=0.03, ge=0, le=1)
    direction: TurnoverDirection | None = None
    reason: str | None = None

    @field_validator(
        "today_amount_cny",
        "previous_same_time_amount_cny",
        "difference_cny",
        "difference_ratio",
        "neutral_band_ratio",
    )
    @classmethod
    def finite_numbers(cls, value: float | None, info):
        return _require_finite(value, info.field_name)

    @model_validator(mode="after")
    def validate_turnover(self) -> "TurnoverSnapshotV1":
        values = (
            self.today_date,
            self.previous_date,
            self.as_of,
            self.today_amount_cny,
            self.previous_same_time_amount_cny,
            self.difference_cny,
            self.difference_ratio,
            self.direction,
        )
        if not self.available:
            if not self.reason:
                raise ValueError("unavailable turnover requires a reason")
            if any(value is not None for value in values):
                raise ValueError("unavailable turnover cannot contain comparison values")
            return self
        if any(value is None for value in values):
            raise ValueError("available turnover requires a complete same-time comparison")
        expected_difference = self.today_amount_cny - self.previous_same_time_amount_cny
        expected_ratio = expected_difference / self.previous_same_time_amount_cny
        if not math.isclose(expected_difference, self.difference_cny, rel_tol=1e-9, abs_tol=1e-6):
            raise ValueError("turnover difference_cny does not match its amounts")
        if not math.isclose(expected_ratio, self.difference_ratio, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("turnover difference_ratio does not match its amounts")
        expected_direction = (
            TurnoverDirection.EXPAND
            if expected_ratio > self.neutral_band_ratio
            else TurnoverDirection.SHRINK
            if expected_ratio < -self.neutral_band_ratio
            else TurnoverDirection.FLAT
        )
        if self.direction != expected_direction:
            raise ValueError("turnover direction does not respect its neutral band")
        if self.reason is not None:
            raise ValueError("available turnover cannot contain an unavailable reason")
        return self


class SectorRotationV1(ContractModel):
    sector_key: str = Field(min_length=3)
    name: str = Field(min_length=1)
    tags: tuple[SectorTag, ...] = Field(min_length=1)
    change_pct: float | None = None
    breadth_ratio: float | None = Field(default=None, ge=0, le=1)
    main_net_inflow_cny: float | None = None
    main_net_inflow_ratio: float | None = None
    provider_as_of: datetime | None = None
    direction: RotationDirection
    evidence_strength: EvidenceStrength
    supporting_evidence: tuple[str, ...] = ()
    counter_evidence: tuple[str, ...] = ()

    @field_validator(
        "change_pct", "breadth_ratio", "main_net_inflow_cny", "main_net_inflow_ratio"
    )
    @classmethod
    def finite_numbers(cls, value: float | None, info):
        return _require_finite(value, info.field_name)

    @field_validator("provider_as_of")
    @classmethod
    def aware_provider_time(cls, value: datetime | None):
        return _require_timezone(value, "provider_as_of")

    @model_validator(mode="after")
    def validate_evidence(self) -> "SectorRotationV1":
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("sector tags must be unique")
        if self.evidence_strength == EvidenceStrength.STRONG:
            if self.change_pct is None or self.breadth_ratio is None:
                raise ValueError("strong sector evidence requires both price and breadth")
            if self.direction == RotationDirection.STRENGTHENING and not (
                self.change_pct > 0 and self.breadth_ratio > 0.5
            ):
                raise ValueError("strong strengthening evidence must agree on price and breadth")
            if self.direction == RotationDirection.WEAKENING and not (
                self.change_pct < 0 and self.breadth_ratio < 0.5
            ):
                raise ValueError("strong weakening evidence must agree on price and breadth")
            if self.direction in {RotationDirection.STABLE, RotationDirection.UNKNOWN}:
                raise ValueError("stable/unknown sectors cannot have strong evidence")
        return self


class RotationSnapshotV1(ContractModel):
    regime: MarketRegime
    sectors: tuple[SectorRotationV1, ...]
    leading_tags: tuple[SectorTag, ...] = ()
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_rotation(self) -> "RotationSnapshotV1":
        keys = [item.sector_key for item in self.sectors]
        if len(keys) != len(set(keys)):
            raise ValueError("rotation sectors must be unique")
        if len(self.leading_tags) != len(set(self.leading_tags)):
            raise ValueError("rotation leading_tags must be unique")
        return self


class SectorFlowPointV1(ContractModel):
    sampled_at: datetime
    provider_as_of: datetime
    session_segment: Literal["am", "pm"]
    cumulative_cny: float
    delta_5m_cny: float | None = None
    delta_5m_baseline_as_of: datetime | None = None

    @field_validator("sampled_at", "provider_as_of", "delta_5m_baseline_as_of")
    @classmethod
    def aware_times(cls, value: datetime | None, info):
        return _require_timezone(value, info.field_name)

    @field_validator("cumulative_cny", "delta_5m_cny")
    @classmethod
    def finite_numbers(cls, value: float | None, info):
        return _require_finite(value, info.field_name)

    @model_validator(mode="after")
    def validate_delta_baseline(self) -> "SectorFlowPointV1":
        if (self.delta_5m_cny is None) != (self.delta_5m_baseline_as_of is None):
            raise ValueError("5-minute flow delta and baseline time must appear together")
        if (
            self.delta_5m_baseline_as_of is not None
            and self.delta_5m_baseline_as_of >= self.provider_as_of
        ):
            raise ValueError("5-minute flow baseline must precede provider_as_of")
        return self


class SectorFlowLatestV1(ContractModel):
    provider_as_of: datetime
    change_pct: float | None = None
    breadth_ratio: float | None = Field(default=None, ge=0, le=1)
    cumulative_cny: float | None = None
    main_net_inflow_pct: float | None = None
    price_percentile: float | None = Field(default=None, ge=0, le=1)
    flow_percentile: float | None = Field(default=None, ge=0, le=1)
    delta_5m_cny: float | None = None
    delta_10m_cny: float | None = None
    delta_5m_baseline_as_of: datetime | None = None
    delta_10m_baseline_as_of: datetime | None = None
    change_delta_5m_pct: float | None = None
    current_strength: EvidenceStrength
    fund_strength: EvidenceStrength
    incremental_direction: SectorFlowDirection

    @field_validator(
        "provider_as_of", "delta_5m_baseline_as_of", "delta_10m_baseline_as_of"
    )
    @classmethod
    def aware_times(cls, value: datetime | None, info):
        return _require_timezone(value, info.field_name)

    @field_validator(
        "change_pct",
        "breadth_ratio",
        "cumulative_cny",
        "main_net_inflow_pct",
        "price_percentile",
        "flow_percentile",
        "delta_5m_cny",
        "delta_10m_cny",
        "change_delta_5m_pct",
    )
    @classmethod
    def finite_numbers(cls, value: float | None, info):
        return _require_finite(value, info.field_name)

    @model_validator(mode="after")
    def validate_delta_baselines(self) -> "SectorFlowLatestV1":
        for delta, baseline, label in (
            (self.delta_5m_cny, self.delta_5m_baseline_as_of, "5-minute"),
            (self.delta_10m_cny, self.delta_10m_baseline_as_of, "10-minute"),
        ):
            if (delta is None) != (baseline is None):
                raise ValueError(f"{label} flow delta and baseline time must appear together")
            if baseline is not None and baseline >= self.provider_as_of:
                raise ValueError(f"{label} flow baseline must precede provider_as_of")
        return self


class SectorFlowLeaderV1(ContractModel):
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    change_pct: float | None = None
    price: float | None = Field(default=None, ge=0)
    provider_as_of: datetime | None = None

    @field_validator("change_pct", "price")
    @classmethod
    def finite_numbers(cls, value: float | None, info):
        return _require_finite(value, info.field_name)

    @field_validator("provider_as_of")
    @classmethod
    def aware_provider_time(cls, value: datetime | None):
        return _require_timezone(value, "provider_as_of")


class SectorFlowLeaderSnapshotV1(ContractModel):
    status: Literal["full", "fallback", "loading", "stale", "error", "unavailable"]
    status_label: str = Field(min_length=1)
    source: str | None = None
    provider_as_of: datetime | None = None
    stale: bool = False
    refreshing: bool = False
    leaders: tuple[SectorFlowLeaderV1, ...] = Field(default=(), max_length=3)

    @field_validator("provider_as_of")
    @classmethod
    def aware_provider_time(cls, value: datetime | None):
        return _require_timezone(value, "provider_as_of")

    @model_validator(mode="after")
    def validate_leaders(self) -> "SectorFlowLeaderSnapshotV1":
        ids = [item.instrument_id for item in self.leaders]
        if len(ids) != len(set(ids)):
            raise ValueError("sector flow leaders must be unique")
        if self.status == "full" and not self.leaders:
            raise ValueError("full sector flow leader snapshot requires leaders")
        if self.status in {"loading", "error", "unavailable"} and self.leaders:
            raise ValueError(f"{self.status} sector flow leader snapshot cannot carry leaders")
        return self


class SectorFlowSeriesV1(ContractModel):
    sector_key: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    category_key: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    category_name: str = Field(min_length=1)
    layer: Literal["anchor", "concept"] = "anchor"
    parent_sector_key: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    parent_name: str | None = Field(default=None, min_length=1)
    taxonomy: Literal["industry", "concept"] | None = None
    leader_board_code: str | None = Field(default=None, pattern=r"^BK\d+$")
    status: SectorFlowTrajectoryStatus
    follow_eligible: bool
    eligible_for_rank: bool
    observation_rank: int | None = Field(default=None, ge=1, le=48)
    rank_total: int = Field(default=0, ge=0, le=48)
    observation_tier: SectorFlowObservationTier
    tier_label: str = Field(min_length=1)
    latest: SectorFlowLatestV1 | None = None
    leader_snapshot: SectorFlowLeaderSnapshotV1 | None = None
    points: tuple[SectorFlowPointV1, ...] = Field(default=(), max_length=256)
    supporting_evidence: tuple[str, ...] = ()
    counter_evidence: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    reason: str | None = None

    @model_validator(mode="after")
    def validate_series(self) -> "SectorFlowSeriesV1":
        if self.layer == "concept":
            if self.taxonomy not in {None, "concept"}:
                raise ValueError("concept sector flow must use concept taxonomy")
            if not self.parent_sector_key or not self.parent_name:
                raise ValueError("concept sector flow requires its parent industry")
        elif self.parent_sector_key is not None or self.parent_name is not None:
            raise ValueError("anchor sector flow cannot carry a parent industry")
        if self.eligible_for_rank and (
            not self.follow_eligible or self.observation_rank is None or self.latest is None
        ):
            raise ValueError("rank-eligible sector flow requires follow eligibility, rank and latest data")
        if not self.eligible_for_rank and self.observation_rank is not None:
            raise ValueError("rank-ineligible sector flow cannot carry an observation rank")
        if self.observation_rank is not None and self.observation_rank > self.rank_total:
            raise ValueError("sector flow rank cannot exceed rank_total")
        if self.status == SectorFlowTrajectoryStatus.UNAVAILABLE:
            if self.latest is not None or self.eligible_for_rank:
                raise ValueError("unavailable sector flow cannot carry current rank data")
            if not self.reason:
                raise ValueError("unavailable sector flow requires a reason")
        if self.observation_tier == SectorFlowObservationTier.UNAVAILABLE:
            if self.latest is not None or self.eligible_for_rank:
                raise ValueError("unavailable tier cannot carry current rank data")
        elif self.latest is None:
            raise ValueError("an available observation tier requires latest data")
        point_times = [item.provider_as_of for item in self.points]
        if point_times != sorted(point_times) or len(point_times) != len(set(point_times)):
            raise ValueError("sector flow points must be unique and ordered by provider time")
        if self.latest is not None and self.points:
            last = self.points[-1]
            if last.provider_as_of != self.latest.provider_as_of:
                raise ValueError("latest sector flow time must match the final trajectory point")
            if self.latest.cumulative_cny != last.cumulative_cny:
                raise ValueError("latest cumulative flow must match the final trajectory point")
        return self


class SectorFlowTrajectoryV1(ContractModel):
    contract: Literal["sector_flow_trajectory.v1"] = "sector_flow_trajectory.v1"
    schema_version: Literal[1] = 1
    direction: Literal["defense", "offense"] = "defense"
    status: SectorFlowTrajectoryStatus
    trade_date: date | None = None
    as_of: datetime | None = None
    market_phase: MarketPhase = MarketPhase.UNKNOWN
    trajectory_scope: Literal["trading_session_to_as_of"] = "trading_session_to_as_of"
    marginal_window_minutes: Literal[5] = 5
    sectors: tuple[SectorFlowSeriesV1, ...] = Field(default=(), max_length=48)
    flags: tuple[str, ...] = ()
    reason: str | None = None

    @field_validator("as_of")
    @classmethod
    def aware_as_of(cls, value: datetime | None):
        return _require_timezone(value, "as_of")

    @model_validator(mode="after")
    def validate_trajectory(self) -> "SectorFlowTrajectoryV1":
        keys = [item.sector_key for item in self.sectors]
        if len(keys) != len(set(keys)):
            raise ValueError("sector flow series must be unique")
        ranked = [item for item in self.sectors if item.eligible_for_rank]
        ranks = sorted(item.observation_rank for item in ranked)
        if ranks != list(range(1, len(ranked) + 1)):
            raise ValueError("sector flow observation ranks must be contiguous")
        if any(item.rank_total != len(ranked) for item in self.sectors):
            raise ValueError("sector flow rank_total must match eligible series count")
        if self.status == SectorFlowTrajectoryStatus.UNAVAILABLE:
            if not self.reason:
                raise ValueError("unavailable sector flow trajectory requires a reason")
        elif self.as_of is None or self.trade_date is None:
            raise ValueError("available sector flow trajectory requires trade_date and as_of")
        if self.as_of is not None and self.trade_date != self.as_of.date():
            raise ValueError("sector flow trade_date must match as_of")
        return self


class GuardrailV1(ContractModel):
    regime: MarketRegime
    severity: GuardrailSeverity
    conclusion_strength: ConclusionStrength
    current_state: str = Field(min_length=1)
    supporting_evidence: tuple[str, ...]
    counter_evidence: tuple[str, ...]
    behavioral_constraint: str = Field(min_length=1)


class ScenarioV1(ContractModel):
    scenario_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    horizon_minutes: int = Field(ge=5, le=15)
    if_condition: str = Field(min_length=1)
    then_expectation: str = Field(min_length=1)
    invalidation: str = Field(min_length=1)
    evidence: tuple[str, ...] = ()

    @field_validator("if_condition", "then_expectation", "invalidation")
    @classmethod
    def reject_probability_language(cls, value: str) -> str:
        forbidden = ("概率", "胜率", "收益预测", "目标价")
        if any(word in value for word in forbidden):
            raise ValueError("scenarios cannot contain probabilities or return forecasts")
        return value


class ChangeSummaryV1(ContractModel):
    available: bool
    previous_snapshot_id: str | None = None
    summary: str | None = None
    changed_fields: tuple[str, ...] = ()
    reason: str | None = None

    @model_validator(mode="after")
    def validate_change(self) -> "ChangeSummaryV1":
        if self.available and (not self.previous_snapshot_id or not self.summary):
            raise ValueError("available change requires previous_snapshot_id and summary")
        if not self.available and not self.reason:
            raise ValueError("unavailable change requires a reason")
        return self


class AlertV1(ContractModel):
    code: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    severity: Literal[GuardrailSeverity.CAUTION, GuardrailSeverity.STOP]
    title: str = Field(min_length=1)
    message: str = Field(min_length=1)
    dedupe_key: str = Field(min_length=1)


_REQUIRED_ROLE_ORDER = (
    IndexRole.BROAD_MARKET,
    IndexRole.LARGE_CAP,
    IndexRole.SMALL_CAP,
    IndexRole.GROWTH,
)


class MarketWatchSnapshotV1(ContractModel):
    contract: Literal["market_watch.v1"] = "market_watch.v1"
    schema_version: Literal[1] = 1
    snapshot_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    as_of: datetime
    market_state: MarketStateV1
    freshness: FreshnessV1
    guardrail: GuardrailV1
    indices: tuple[IndexSnapshotV1, ...]
    breadth: BreadthSnapshotV1
    turnover: TurnoverSnapshotV1
    rotation: RotationSnapshotV1
    sector_flow_trajectory: SectorFlowTrajectoryV1 | None = None
    offense_sector_flow_trajectory: SectorFlowTrajectoryV1 | None = None
    scenarios: tuple[ScenarioV1, ...] = Field(max_length=2)
    change: ChangeSummaryV1
    alerts: tuple[AlertV1, ...] = ()

    @field_validator("as_of")
    @classmethod
    def aware_as_of(cls, value: datetime):
        return _require_timezone(value, "as_of")

    @model_validator(mode="after")
    def validate_snapshot(self) -> "MarketWatchSnapshotV1":
        if (
            self.sector_flow_trajectory is not None
            and self.sector_flow_trajectory.direction != "defense"
        ):
            raise ValueError("sector_flow_trajectory must contain the defense direction")
        if (
            self.offense_sector_flow_trajectory is not None
            and self.offense_sector_flow_trajectory.direction != "offense"
        ):
            raise ValueError(
                "offense_sector_flow_trajectory must contain the offense direction"
            )
        roles = tuple(item.role for item in self.indices)
        if roles != _REQUIRED_ROLE_ORDER:
            raise ValueError("indices must contain the four fixed roles in canonical order")
        if self.market_state.trading_date != self.as_of.date():
            raise ValueError("market_state trading_date must match snapshot as_of")
        if self.freshness.status in {
            FreshnessStatus.STALE,
            FreshnessStatus.UNAVAILABLE,
        }:
            if not (
                self.guardrail.regime == MarketRegime.UNCERTAIN
                and self.guardrail.severity == GuardrailSeverity.STOP
                and self.guardrail.conclusion_strength == ConclusionStrength.ABSTAIN
            ):
                raise ValueError("stale/unavailable data must force an uncertain stop guardrail")
        if self.freshness.status == FreshnessStatus.DEGRADED:
            if self.guardrail.conclusion_strength not in {
                ConclusionStrength.WEAK,
                ConclusionStrength.ABSTAIN,
            } or self.guardrail.severity == GuardrailSeverity.CALM:
                raise ValueError("degraded data cannot produce a strong or calm conclusion")
        dedupe_keys = [item.dedupe_key for item in self.alerts]
        if len(dedupe_keys) != len(set(dedupe_keys)):
            raise ValueError("alert dedupe keys must be unique")
        return self


__all__ = [
    "AlertV1",
    "BreadthSnapshotV1",
    "ChangeSummaryV1",
    "ComponentQuality",
    "ConclusionStrength",
    "ContractModel",
    "EvidenceStrength",
    "FreshnessComponentV1",
    "FreshnessStatus",
    "FreshnessV1",
    "GuardrailSeverity",
    "GuardrailV1",
    "IndexRole",
    "IndexSnapshotV1",
    "MarketPhase",
    "MarketRegime",
    "MarketStateV1",
    "MarketWatchSnapshotV1",
    "RotationDirection",
    "RotationSnapshotV1",
    "ScenarioV1",
    "SectorFlowDirection",
    "SectorFlowLatestV1",
    "SectorFlowObservationTier",
    "SectorFlowPointV1",
    "SectorFlowSeriesV1",
    "SectorFlowTrajectoryStatus",
    "SectorFlowTrajectoryV1",
    "SectorRotationV1",
    "SectorTag",
    "TurnoverDirection",
    "TurnoverSnapshotV1",
]
