"""Lossless read projections for the desktop market-watch Web surface.

The collector owns refresh, validation, persistence and publication.  This
module is deliberately pure: it projects one already-accepted strict snapshot
into a compact summary and exact, revision-bound trajectory details.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any, Literal

from pydantic import Field, model_validator

from .contracts import (
    AlertV1,
    BreadthSnapshotV1,
    ChangeSummaryV1,
    ContractModel,
    FreshnessV1,
    GuardrailV1,
    IndexSnapshotV1,
    MarketPhase,
    MarketStateV1,
    MarketWatchSnapshotV1,
    RotationSnapshotV1,
    ScenarioV1,
    SectorFlowLatestV1,
    SectorFlowLeaderSnapshotV1,
    SectorFlowObservationTier,
    SectorFlowSeriesV1,
    SectorFlowTrajectoryStatus,
    SectorFlowTrajectoryV1,
    TurnoverSnapshotV1,
)
from .collection_contracts import MarketWatchCollectorEnvelopeV1
from .integrity import (
    PayloadIntegrityV1,
    REVISION_PATTERN,
    SectorFlowSeriesIntegrityV1,
    TrajectoryPayloadIntegrityV1,
    build_payload_integrity,
    build_sector_flow_series_integrity,
    stable_sha256,
)
from .sector_resonance import SectorResonanceBatchV1
from .read_facade import HistoricalMarketWatchSnapshot, HistoricalSectorFlowProjection


class ProjectionConsistencyError(ValueError):
    """The requested projection cannot be proven consistent."""


class SourceSnapshotRevisionMismatch(ProjectionConsistencyError):
    """The supplied source revision does not identify the supplied snapshot."""


class TrajectoryRevisionMismatch(ProjectionConsistencyError):
    """The requested trajectory revision is no longer current."""


class SectorSelectionError(ProjectionConsistencyError):
    """A trajectory detail selection is incomplete or ambiguous."""


class AcceptedRealUnavailable(ProjectionConsistencyError):
    """No accepted real snapshot exists for a numeric Web projection."""


class SectorFlowSeriesSummaryV1(ContractModel):
    """Every canonical series field except the large point tuple."""

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
    observation_rank: int | None = Field(default=None, ge=1, le=64)
    rank_total: int = Field(default=0, ge=0, le=64)
    observation_tier: SectorFlowObservationTier
    tier_label: str = Field(min_length=1)
    latest: SectorFlowLatestV1 | None = None
    leader_snapshot: SectorFlowLeaderSnapshotV1 | None = None
    supporting_evidence: tuple[str, ...] = ()
    counter_evidence: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    reason: str | None = None
    point_count: int = Field(ge=0, le=256)
    first_provider_as_of: datetime | None = None
    last_provider_as_of: datetime | None = None
    points_revision: str = Field(pattern=REVISION_PATTERN)

    @model_validator(mode="after")
    def validate_point_manifest(self) -> "SectorFlowSeriesSummaryV1":
        integrity = SectorFlowSeriesIntegrityV1(
            sector_key=self.sector_key,
            point_count=self.point_count,
            first_provider_as_of=self.first_provider_as_of,
            last_provider_as_of=self.last_provider_as_of,
            points_revision=self.points_revision,
        )
        if (
            self.latest is not None
            and integrity.last_provider_as_of is not None
            and self.latest.provider_as_of != integrity.last_provider_as_of
        ):
            raise ValueError("summary latest time must match the final trajectory point")
        return self


class SectorFlowTrajectorySummaryV1(ContractModel):
    contract: Literal["sector_flow_trajectory_summary.v1"] = (
        "sector_flow_trajectory_summary.v1"
    )
    schema_version: Literal[1] = 1
    source_snapshot_revision: str = Field(pattern=REVISION_PATTERN)
    trajectory_revision: str = Field(pattern=REVISION_PATTERN)
    direction: Literal["defense", "offense"]
    status: SectorFlowTrajectoryStatus
    trade_date: date | None = None
    as_of: datetime | None = None
    market_phase: MarketPhase
    trajectory_scope: Literal["trading_session_to_as_of"]
    marginal_window_minutes: Literal[5]
    sector_count: int = Field(ge=0, le=64)
    point_count: int = Field(ge=0, le=64 * 256)
    sectors: tuple[SectorFlowSeriesSummaryV1, ...] = Field(max_length=64)
    flags: tuple[str, ...] = ()
    reason: str | None = None

    @model_validator(mode="after")
    def validate_counts(self) -> "SectorFlowTrajectorySummaryV1":
        keys = [item.sector_key for item in self.sectors]
        if len(keys) != len(set(keys)):
            raise ValueError("trajectory summary sector keys must be unique")
        if self.sector_count != len(self.sectors):
            raise ValueError("trajectory summary sector_count does not match sectors")
        if self.point_count != sum(item.point_count for item in self.sectors):
            raise ValueError("trajectory summary point_count does not match sectors")
        return self


class MarketWatchSummaryV1(ContractModel):
    contract: Literal["market_watch_summary.v1"] = "market_watch_summary.v1"
    schema_version: Literal[1] = 1
    source_snapshot_revision: str = Field(pattern=REVISION_PATTERN)
    resonance_revision: str | None = Field(default=None, pattern=REVISION_PATTERN)
    resonance_source_snapshot_revision: str | None = Field(
        default=None, pattern=REVISION_PATTERN
    )
    resonance_as_of: datetime | None = None
    snapshot_contract: Literal["market_watch.v1"] = "market_watch.v1"
    snapshot_schema_version: Literal[1] = 1
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
    sector_flow_trajectory: SectorFlowTrajectorySummaryV1 | None = None
    offense_sector_flow_trajectory: SectorFlowTrajectorySummaryV1 | None = None
    scenarios: tuple[ScenarioV1, ...] = Field(max_length=2)
    change: ChangeSummaryV1
    alerts: tuple[AlertV1, ...] = ()
    payload_integrity: PayloadIntegrityV1

    @model_validator(mode="after")
    def validate_revisions(self) -> "MarketWatchSummaryV1":
        resonance_fields = (
            self.resonance_revision,
            self.resonance_source_snapshot_revision,
            self.resonance_as_of,
        )
        if any(value is not None for value in resonance_fields) and any(
            value is None for value in resonance_fields
        ):
            raise ValueError("resonance revision, source revision and as_of must appear together")
        if self.resonance_as_of is not None:
            if self.resonance_as_of > self.as_of:
                raise ValueError("resonance evidence cannot be newer than the summary")
            if self.as_of - self.resonance_as_of > timedelta(minutes=6):
                raise ValueError("resonance evidence is too old for this summary")
        if self.payload_integrity.source_snapshot_revision != self.source_snapshot_revision:
            raise ValueError("payload integrity must bind the same source snapshot revision")
        for summary, integrity, direction in (
            (self.sector_flow_trajectory, self.payload_integrity.defense, "defense"),
            (
                self.offense_sector_flow_trajectory,
                self.payload_integrity.offense,
                "offense",
            ),
        ):
            if (summary is None) != (integrity is None):
                raise ValueError(f"{direction} summary and integrity must appear together")
            if summary is None:
                continue
            if summary.direction != direction:
                raise ValueError(f"{direction} summary has the wrong direction")
            if summary.source_snapshot_revision != self.source_snapshot_revision:
                raise ValueError(f"{direction} summary has the wrong source revision")
            if summary.trajectory_revision != integrity.trajectory_revision:
                raise ValueError(f"{direction} summary has the wrong trajectory revision")
            if tuple(item.sector_key for item in summary.sectors) != tuple(
                item.sector_key for item in integrity.sectors
            ):
                raise ValueError(f"{direction} summary and integrity sector order differ")
        return self


class SectorFlowTrajectoryDetailV1(ContractModel):
    contract: Literal["sector_flow_trajectory_detail.v1"] = (
        "sector_flow_trajectory_detail.v1"
    )
    schema_version: Literal[1] = 1
    source_snapshot_revision: str = Field(pattern=REVISION_PATTERN)
    trajectory_revision: str = Field(pattern=REVISION_PATTERN)
    direction: Literal["defense", "offense"]
    status: SectorFlowTrajectoryStatus
    trade_date: date | None = None
    as_of: datetime | None = None
    market_phase: MarketPhase
    trajectory_scope: Literal["trading_session_to_as_of"]
    marginal_window_minutes: Literal[5]
    sector_keys: tuple[str, ...] = Field(min_length=1, max_length=64)
    sector_count: int = Field(ge=1, le=64)
    point_count: int = Field(ge=0, le=64 * 256)
    sectors: tuple[SectorFlowSeriesV1, ...] = Field(min_length=1, max_length=64)
    sector_integrity: tuple[SectorFlowSeriesIntegrityV1, ...] = Field(
        min_length=1,
        max_length=64,
    )
    flags: tuple[str, ...] = ()
    reason: str | None = None

    @model_validator(mode="after")
    def validate_selection(self) -> "SectorFlowTrajectoryDetailV1":
        sector_keys = tuple(item.sector_key for item in self.sectors)
        integrity_keys = tuple(item.sector_key for item in self.sector_integrity)
        if len(self.sector_keys) != len(set(self.sector_keys)):
            raise ValueError("detail sector keys must be unique")
        if sector_keys != self.sector_keys or integrity_keys != self.sector_keys:
            raise ValueError("detail sectors and integrity must match the selected order")
        if self.sector_count != len(self.sectors):
            raise ValueError("detail sector_count does not match sectors")
        if self.point_count != sum(len(item.points) for item in self.sectors):
            raise ValueError("detail point_count does not match sectors")
        for series, integrity in zip(self.sectors, self.sector_integrity, strict=True):
            if len(series.points) != integrity.point_count:
                raise ValueError("detail series point count does not match integrity")
            if stable_sha256(series.points) != integrity.points_revision:
                raise ValueError("detail series points do not match integrity revision")
        return self


class SectorFlowFiveDaySliceV1(ContractModel):
    contract: Literal["sector_flow_five_day_slice.v1"] = (
        "sector_flow_five_day_slice.v1"
    )
    schema_version: Literal[1] = 1
    trade_date: date
    snapshot_id: str = Field(min_length=1)
    source_snapshot_revision: str = Field(pattern=REVISION_PATTERN)
    trajectory_revision: str = Field(pattern=REVISION_PATTERN)
    direction: Literal["defense", "offense"]
    status: SectorFlowTrajectoryStatus
    as_of: datetime
    market_phase: MarketPhase
    point_count: int = Field(ge=0, le=64 * 256)
    sectors: tuple[SectorFlowSeriesV1, ...] = Field(default=(), max_length=64)
    flags: tuple[str, ...] = ()
    reason: str | None = None

    @model_validator(mode="after")
    def validate_slice(self) -> "SectorFlowFiveDaySliceV1":
        keys = tuple(item.sector_key for item in self.sectors)
        if len(keys) != len(set(keys)):
            raise ValueError("five-day slice sector keys must be unique")
        if self.point_count != sum(len(item.points) for item in self.sectors):
            raise ValueError("five-day slice point_count does not match sectors")
        if self.as_of.date() != self.trade_date:
            raise ValueError("five-day slice as_of must belong to trade_date")
        for series in self.sectors:
            if any(point.provider_as_of.date() != self.trade_date for point in series.points):
                raise ValueError("five-day slice points must belong to trade_date")
        return self


class SectorFlowFiveDayTrajectoryV1(ContractModel):
    contract: Literal["sector_flow_five_day_trajectory.v1"] = (
        "sector_flow_five_day_trajectory.v1"
    )
    schema_version: Literal[1] = 1
    history_revision: str = Field(pattern=REVISION_PATTERN)
    direction: Literal["defense", "offense"]
    requested_trade_days: Literal[5] = 5
    available_trade_days: int = Field(ge=0, le=5)
    status: Literal["ready", "partial", "unavailable"]
    as_of: datetime | None = None
    trade_dates: tuple[date, ...] = Field(default=(), max_length=5)
    sector_keys: tuple[str, ...] = Field(min_length=1, max_length=64)
    sector_count: int = Field(ge=1, le=64)
    point_count: int = Field(ge=0, le=5 * 64 * 256)
    days: tuple[SectorFlowFiveDaySliceV1, ...] = Field(default=(), max_length=5)
    flags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_history(self) -> "SectorFlowFiveDayTrajectoryV1":
        if len(self.sector_keys) != len(set(self.sector_keys)):
            raise ValueError("five-day sector keys must be unique")
        if self.sector_count != len(self.sector_keys):
            raise ValueError("five-day sector_count does not match sector_keys")
        if self.available_trade_days != len(self.days):
            raise ValueError("available_trade_days does not match days")
        if self.trade_dates != tuple(item.trade_date for item in self.days):
            raise ValueError("trade_dates do not match five-day slices")
        if self.trade_dates != tuple(sorted(self.trade_dates)):
            raise ValueError("five-day slices must be chronological")
        if self.point_count != sum(item.point_count for item in self.days):
            raise ValueError("five-day point_count does not match slices")
        if self.as_of != (self.days[-1].as_of if self.days else None):
            raise ValueError("five-day as_of must match the newest slice")
        expected_status = (
            "unavailable"
            if not self.days
            else "ready"
            if len(self.days) == self.requested_trade_days
            and all(item.status == SectorFlowTrajectoryStatus.READY for item in self.days)
            else "partial"
        )
        if self.status != expected_status:
            raise ValueError("five-day status does not match retained slices")
        return self


def _trajectory_summary(
    trajectory: SectorFlowTrajectoryV1,
    *,
    source_snapshot_revision: str,
    integrity: TrajectoryPayloadIntegrityV1,
) -> SectorFlowTrajectorySummaryV1:
    summaries = []
    for series, item_integrity in zip(
        trajectory.sectors,
        integrity.sectors,
        strict=True,
    ):
        payload = series.model_dump(mode="python", exclude={"points"})
        summaries.append(
            SectorFlowSeriesSummaryV1(
                **payload,
                point_count=item_integrity.point_count,
                first_provider_as_of=item_integrity.first_provider_as_of,
                last_provider_as_of=item_integrity.last_provider_as_of,
                points_revision=item_integrity.points_revision,
            )
        )
    return SectorFlowTrajectorySummaryV1(
        source_snapshot_revision=source_snapshot_revision,
        trajectory_revision=integrity.trajectory_revision,
        direction=trajectory.direction,
        status=trajectory.status,
        trade_date=trajectory.trade_date,
        as_of=trajectory.as_of,
        market_phase=trajectory.market_phase,
        trajectory_scope=trajectory.trajectory_scope,
        marginal_window_minutes=trajectory.marginal_window_minutes,
        sector_count=integrity.sector_count,
        point_count=integrity.point_count,
        sectors=tuple(summaries),
        flags=trajectory.flags,
        reason=trajectory.reason,
    )


def _validated_snapshot(
    snapshot: MarketWatchSnapshotV1 | Mapping[str, Any],
    source_snapshot_revision: str,
    collector_envelope: MarketWatchCollectorEnvelopeV1,
) -> tuple[
    MarketWatchSnapshotV1,
    MarketWatchCollectorEnvelopeV1,
    Mapping[str, Any],
]:
    payload = (
        snapshot.model_dump(mode="json")
        if isinstance(snapshot, MarketWatchSnapshotV1)
        else snapshot
    )
    canonical = MarketWatchSnapshotV1.model_validate(payload)
    envelope = MarketWatchCollectorEnvelopeV1.model_validate(collector_envelope)
    accepted = envelope.latest_accepted_real
    if accepted is None:
        raise AcceptedRealUnavailable(
            "numeric projections require a latest accepted real snapshot"
        )
    if accepted.snapshot_id != canonical.snapshot_id:
        raise SourceSnapshotRevisionMismatch(
            "latest_accepted_real does not identify the supplied snapshot"
        )
    if accepted.source_snapshot_revision != source_snapshot_revision:
        raise SourceSnapshotRevisionMismatch(
            "latest_accepted_real does not identify the supplied source revision"
        )
    snapshot_minute = canonical.as_of.replace(second=0, microsecond=0)
    if accepted.minute_bucket != snapshot_minute:
        raise SourceSnapshotRevisionMismatch(
            "latest_accepted_real minute does not match the supplied snapshot"
        )
    actual_revision = stable_sha256(payload)
    if source_snapshot_revision != actual_revision:
        raise SourceSnapshotRevisionMismatch(
            "source_snapshot_revision does not match the strict snapshot payload"
        )
    return canonical, envelope, payload


def build_market_watch_summary(
    snapshot: MarketWatchSnapshotV1 | Mapping[str, Any],
    *,
    source_snapshot_revision: str,
    collector_envelope: MarketWatchCollectorEnvelopeV1,
) -> MarketWatchSummaryV1:
    """Build a compact lossless manifest for one accepted real snapshot."""

    canonical, envelope, payload = _validated_snapshot(
        snapshot,
        source_snapshot_revision,
        collector_envelope,
    )
    payload_integrity = build_payload_integrity(
        payload,
        source_snapshot_revision=source_snapshot_revision,
    )
    defense_integrity = payload_integrity.defense
    offense_integrity = payload_integrity.offense
    return MarketWatchSummaryV1(
        source_snapshot_revision=source_snapshot_revision,
        snapshot_id=canonical.snapshot_id,
        sequence=canonical.sequence,
        as_of=canonical.as_of,
        market_state=canonical.market_state,
        freshness=canonical.freshness,
        guardrail=canonical.guardrail,
        indices=canonical.indices,
        breadth=canonical.breadth,
        turnover=canonical.turnover,
        rotation=canonical.rotation,
        sector_flow_trajectory=(
            _trajectory_summary(
                canonical.sector_flow_trajectory,
                source_snapshot_revision=source_snapshot_revision,
                integrity=defense_integrity,
            )
            if canonical.sector_flow_trajectory is not None
            and defense_integrity is not None
            else None
        ),
        offense_sector_flow_trajectory=(
            _trajectory_summary(
                canonical.offense_sector_flow_trajectory,
                source_snapshot_revision=source_snapshot_revision,
                integrity=offense_integrity,
            )
            if canonical.offense_sector_flow_trajectory is not None
            and offense_integrity is not None
            else None
        ),
        scenarios=canonical.scenarios,
        change=canonical.change,
        alerts=canonical.alerts,
        payload_integrity=payload_integrity,
    )


def overlay_sector_resonance(
    summary: MarketWatchSummaryV1,
    batch: SectorResonanceBatchV1,
) -> MarketWatchSummaryV1:
    """Overlay a separately revisioned exact-source resonance result."""

    if (
        batch.source_as_of > summary.as_of
        or batch.source_as_of.date() != summary.as_of.date()
        or summary.as_of - batch.source_as_of > timedelta(minutes=6)
    ):
        raise SourceSnapshotRevisionMismatch(
            "resonance batch is outside the summary freshness window"
        )
    by_key = {
        (item.direction, item.sector_key): item.leader_snapshot
        for item in batch.entries
    }

    def update_trajectory(direction, trajectory):
        if trajectory is None:
            return None
        sectors = tuple(
            item.model_copy(
                update={
                    "leader_snapshot": by_key.get(
                        (direction, item.sector_key), item.leader_snapshot
                    )
                }
            )
            for item in trajectory.sectors
        )
        return trajectory.model_copy(update={"sectors": sectors})

    overlaid = summary.model_copy(
        update={
            "resonance_revision": batch.resonance_revision,
            "resonance_source_snapshot_revision": batch.source_snapshot_revision,
            "resonance_as_of": batch.source_as_of,
            "sector_flow_trajectory": update_trajectory(
                "defense", summary.sector_flow_trajectory
            ),
            "offense_sector_flow_trajectory": update_trajectory(
                "offense", summary.offense_sector_flow_trajectory
            ),
        }
    )
    return MarketWatchSummaryV1.model_validate(overlaid.model_dump(mode="python"))


def build_sector_flow_detail(
    snapshot: MarketWatchSnapshotV1 | Mapping[str, Any],
    *,
    source_snapshot_revision: str,
    collector_envelope: MarketWatchCollectorEnvelopeV1,
    direction: Literal["defense", "offense"],
    sector_keys: tuple[str, ...],
    expected_trajectory_revision: str,
) -> SectorFlowTrajectoryDetailV1:
    """Return exact full point tuples for a complete revision-bound selection."""

    canonical, _, payload = _validated_snapshot(
        snapshot,
        source_snapshot_revision,
        collector_envelope,
    )
    trajectory = (
        canonical.sector_flow_trajectory
        if direction == "defense"
        else canonical.offense_sector_flow_trajectory
    )
    if trajectory is None:
        raise SectorSelectionError(f"{direction} trajectory is unavailable")
    trajectory_field = (
        "sector_flow_trajectory"
        if direction == "defense"
        else "offense_sector_flow_trajectory"
    )
    trajectory_payload = payload.get(trajectory_field)
    if not isinstance(trajectory_payload, Mapping):
        raise SectorSelectionError(f"{direction} trajectory payload is unavailable")
    trajectory_revision = stable_sha256(trajectory_payload)
    if expected_trajectory_revision != trajectory_revision:
        raise TrajectoryRevisionMismatch(
            "expected_trajectory_revision does not match the source trajectory"
        )
    if not sector_keys:
        raise SectorSelectionError("at least one sector key is required")
    if len(sector_keys) != len(set(sector_keys)):
        raise SectorSelectionError("sector keys must be unique")

    requested = set(sector_keys)
    selected = tuple(item for item in trajectory.sectors if item.sector_key in requested)
    returned_keys = tuple(item.sector_key for item in selected)
    missing = requested.difference(returned_keys)
    if missing:
        raise SectorSelectionError(
            "requested sector keys are absent from the bound trajectory: "
            + ", ".join(sorted(missing))
        )
    raw_sectors = trajectory_payload.get("sectors", ())
    raw_by_key = {
        str(item.get("sector_key")): item
        for item in raw_sectors
        if isinstance(item, Mapping)
    }
    integrity = tuple(
        build_sector_flow_series_integrity(raw_by_key[item.sector_key])
        for item in selected
    )
    return SectorFlowTrajectoryDetailV1(
        source_snapshot_revision=source_snapshot_revision,
        trajectory_revision=trajectory_revision,
        direction=trajectory.direction,
        status=trajectory.status,
        trade_date=trajectory.trade_date,
        as_of=trajectory.as_of,
        market_phase=trajectory.market_phase,
        trajectory_scope=trajectory.trajectory_scope,
        marginal_window_minutes=trajectory.marginal_window_minutes,
        sector_keys=returned_keys,
        sector_count=len(selected),
        point_count=sum(len(item.points) for item in selected),
        sectors=selected,
        sector_integrity=integrity,
        flags=trajectory.flags,
        reason=trajectory.reason,
    )


def build_five_day_sector_flow_trajectory(
    snapshots: Sequence[HistoricalMarketWatchSnapshot],
    *,
    direction: Literal["defense", "offense"],
    sector_keys: tuple[str, ...],
) -> SectorFlowFiveDayTrajectoryV1:
    """Project exact retained daily minute curves without cross-day synthesis."""

    if not sector_keys or len(sector_keys) != len(set(sector_keys)):
        raise SectorSelectionError("five-day sector keys must be non-empty and unique")
    requested = set(sector_keys)
    slices: list[SectorFlowFiveDaySliceV1] = []
    revision_evidence: list[dict[str, Any]] = []
    for retained in snapshots[-5:]:
        if stable_sha256(retained.source_payload) != retained.source_snapshot_revision:
            raise SourceSnapshotRevisionMismatch(
                "historical source revision does not match its strict payload"
            )
        canonical = MarketWatchSnapshotV1.model_validate(retained.source_payload)
        if (
            canonical.snapshot_id != retained.snapshot_id
            or canonical.market_state.trading_date != retained.trade_date
            or canonical.as_of.replace(second=0, microsecond=0)
            != retained.minute_bucket
        ):
            raise SourceSnapshotRevisionMismatch(
                "historical snapshot identity does not match its retained pointer"
            )
        trajectory = (
            canonical.sector_flow_trajectory
            if direction == "defense"
            else canonical.offense_sector_flow_trajectory
        )
        field = (
            "sector_flow_trajectory"
            if direction == "defense"
            else "offense_sector_flow_trajectory"
        )
        raw_trajectory = retained.source_payload.get(field)
        if trajectory is None or not isinstance(raw_trajectory, Mapping):
            continue
        trajectory_revision = stable_sha256(raw_trajectory)
        selected = tuple(
            item for item in trajectory.sectors if item.sector_key in requested
        )
        slices.append(
            SectorFlowFiveDaySliceV1(
                trade_date=retained.trade_date,
                snapshot_id=retained.snapshot_id,
                source_snapshot_revision=retained.source_snapshot_revision,
                trajectory_revision=trajectory_revision,
                direction=direction,
                status=trajectory.status,
                as_of=trajectory.as_of or canonical.as_of,
                market_phase=trajectory.market_phase,
                point_count=sum(len(item.points) for item in selected),
                sectors=selected,
                flags=trajectory.flags,
                reason=trajectory.reason,
            )
        )
        revision_evidence.append(
            {
                "trade_date": retained.trade_date.isoformat(),
                "source_snapshot_revision": retained.source_snapshot_revision,
                "trajectory_revision": trajectory_revision,
            }
        )
    days = tuple(slices)
    status: Literal["ready", "partial", "unavailable"] = (
        "unavailable"
        if not days
        else "ready"
        if len(days) == 5
        and all(item.status == SectorFlowTrajectoryStatus.READY for item in days)
        else "partial"
    )
    flags = []
    if len(days) < 5:
        flags.append("fewer_than_five_retained_trade_days")
    if any(item.status != SectorFlowTrajectoryStatus.READY for item in days):
        flags.append("one_or_more_daily_trajectories_partial")
    history_revision = stable_sha256(
        {
            "direction": direction,
            "sector_keys": sector_keys,
            "days": revision_evidence,
        }
    )
    return SectorFlowFiveDayTrajectoryV1(
        history_revision=history_revision,
        direction=direction,
        available_trade_days=len(days),
        status=status,
        as_of=days[-1].as_of if days else None,
        trade_dates=tuple(item.trade_date for item in days),
        sector_keys=sector_keys,
        sector_count=len(sector_keys),
        point_count=sum(item.point_count for item in days),
        days=days,
        flags=tuple(flags),
    )


def build_five_day_sector_flow_from_projections(
    projections: Sequence[HistoricalSectorFlowProjection],
    *,
    direction: Literal["defense", "offense"],
    sector_keys: tuple[str, ...],
) -> SectorFlowFiveDayTrajectoryV1:
    """Build the same Web contract from Collector-materialized close series."""

    if not sector_keys or len(sector_keys) != len(set(sector_keys)):
        raise SectorSelectionError("five-day sector keys must be non-empty and unique")
    requested = set(sector_keys)
    slices = []
    revision_evidence = []
    previous_date = None
    for retained in projections[-5:]:
        if retained.direction != direction:
            raise ProjectionConsistencyError("daily sector-flow direction mismatch")
        if previous_date is not None and retained.trade_date <= previous_date:
            raise ProjectionConsistencyError("daily sector-flow projections are not ordered")
        previous_date = retained.trade_date
        keys = tuple(item.sector_key for item in retained.sectors)
        if len(keys) != len(set(keys)) or any(key not in requested for key in keys):
            raise ProjectionConsistencyError("daily sector-flow projection selection mismatch")
        slices.append(
            SectorFlowFiveDaySliceV1(
                trade_date=retained.trade_date,
                snapshot_id=retained.snapshot_id,
                source_snapshot_revision=retained.source_snapshot_revision,
                trajectory_revision=retained.trajectory_revision,
                direction=direction,
                status=retained.status,
                as_of=retained.as_of,
                market_phase=retained.market_phase,
                point_count=sum(len(item.points) for item in retained.sectors),
                sectors=retained.sectors,
                flags=retained.flags,
                reason=retained.reason,
            )
        )
        revision_evidence.append(
            {
                "trade_date": retained.trade_date.isoformat(),
                "source_snapshot_revision": retained.source_snapshot_revision,
                "trajectory_revision": retained.trajectory_revision,
            }
        )
    days = tuple(slices)
    status: Literal["ready", "partial", "unavailable"] = (
        "unavailable"
        if not days
        else "ready"
        if len(days) == 5
        and all(item.status == SectorFlowTrajectoryStatus.READY for item in days)
        else "partial"
    )
    flags = []
    if len(days) < 5:
        flags.append("fewer_than_five_retained_trade_days")
    if any(item.status != SectorFlowTrajectoryStatus.READY for item in days):
        flags.append("one_or_more_daily_trajectories_partial")
    history_revision = stable_sha256(
        {"direction": direction, "sector_keys": sector_keys, "days": revision_evidence}
    )
    return SectorFlowFiveDayTrajectoryV1(
        history_revision=history_revision,
        direction=direction,
        available_trade_days=len(days),
        status=status,
        as_of=days[-1].as_of if days else None,
        trade_dates=tuple(item.trade_date for item in days),
        sector_keys=sector_keys,
        sector_count=len(sector_keys),
        point_count=sum(item.point_count for item in days),
        days=days,
        flags=tuple(flags),
    )


__all__ = [
    "AcceptedRealUnavailable",
    "MarketWatchSummaryV1",
    "PayloadIntegrityV1",
    "ProjectionConsistencyError",
    "SectorFlowSeriesIntegrityV1",
    "SectorFlowSeriesSummaryV1",
    "SectorFlowFiveDaySliceV1",
    "SectorFlowFiveDayTrajectoryV1",
    "SectorFlowTrajectoryDetailV1",
    "SectorFlowTrajectorySummaryV1",
    "SectorSelectionError",
    "SourceSnapshotRevisionMismatch",
    "TrajectoryPayloadIntegrityV1",
    "TrajectoryRevisionMismatch",
    "build_market_watch_summary",
    "build_five_day_sector_flow_trajectory",
    "build_five_day_sector_flow_from_projections",
    "build_sector_flow_detail",
    "overlay_sector_resonance",
]
