"""Stable, provider-neutral payload integrity for market-watch contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field, field_validator, model_validator

from .contracts import (
    ContractModel,
    MarketWatchSnapshotV1,
    SectorFlowSeriesV1,
    SectorFlowTrajectoryV1,
)


REVISION_PATTERN = r"^[0-9a-f]{64}$"
CanonicalValue: TypeAlias = BaseModel | Mapping[str, Any] | Sequence[Any]


def _json_ready(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_ready(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def canonical_json_bytes(value: CanonicalValue) -> bytes:
    """Serialize a strict model or JSON-shaped collection deterministically."""

    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def stable_sha256(value: CanonicalValue) -> str:
    """Return the lowercase SHA-256 of :func:`canonical_json_bytes`."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class SectorFlowSeriesIntegrityV1(ContractModel):
    sector_key: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    point_count: int = Field(ge=0, le=256)
    first_provider_as_of: datetime | None = None
    last_provider_as_of: datetime | None = None
    points_revision: str = Field(pattern=REVISION_PATTERN)

    @field_validator("first_provider_as_of", "last_provider_as_of")
    @classmethod
    def aware_times(cls, value: datetime | None, info):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{info.field_name} must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "SectorFlowSeriesIntegrityV1":
        if self.point_count == 0:
            if self.first_provider_as_of is not None or self.last_provider_as_of is not None:
                raise ValueError("empty sector flow integrity cannot carry a time range")
        elif self.first_provider_as_of is None or self.last_provider_as_of is None:
            raise ValueError("non-empty sector flow integrity requires a time range")
        elif self.first_provider_as_of > self.last_provider_as_of:
            raise ValueError("sector flow integrity time range must be ordered")
        return self


class TrajectoryPayloadIntegrityV1(ContractModel):
    direction: Literal["defense", "offense"]
    trajectory_revision: str = Field(pattern=REVISION_PATTERN)
    sector_count: int = Field(ge=0, le=48)
    point_count: int = Field(ge=0, le=48 * 256)
    sectors: tuple[SectorFlowSeriesIntegrityV1, ...] = Field(max_length=48)

    @model_validator(mode="after")
    def validate_counts(self) -> "TrajectoryPayloadIntegrityV1":
        keys = [item.sector_key for item in self.sectors]
        if len(keys) != len(set(keys)):
            raise ValueError("trajectory integrity sector keys must be unique")
        if self.sector_count != len(self.sectors):
            raise ValueError("trajectory integrity sector_count does not match sectors")
        if self.point_count != sum(item.point_count for item in self.sectors):
            raise ValueError("trajectory integrity point_count does not match sectors")
        return self


class PayloadIntegrityV1(ContractModel):
    source_snapshot_revision: str = Field(pattern=REVISION_PATTERN)
    defense: TrajectoryPayloadIntegrityV1 | None = None
    offense: TrajectoryPayloadIntegrityV1 | None = None

    @model_validator(mode="after")
    def validate_directions(self) -> "PayloadIntegrityV1":
        if self.defense is not None and self.defense.direction != "defense":
            raise ValueError("defense integrity must identify the defense trajectory")
        if self.offense is not None and self.offense.direction != "offense":
            raise ValueError("offense integrity must identify the offense trajectory")
        return self


def build_sector_flow_series_integrity(
    series: SectorFlowSeriesV1 | Mapping[str, Any],
) -> SectorFlowSeriesIntegrityV1:
    """Describe an exact canonical series point tuple without copying its points."""

    payload = (
        series.model_dump(mode="json")
        if isinstance(series, BaseModel)
        else series
    )
    canonical = SectorFlowSeriesV1.model_validate(payload)
    point_payloads = payload.get("points", ())
    points = canonical.points
    return SectorFlowSeriesIntegrityV1(
        sector_key=canonical.sector_key,
        point_count=len(points),
        first_provider_as_of=points[0].provider_as_of if points else None,
        last_provider_as_of=points[-1].provider_as_of if points else None,
        points_revision=stable_sha256(point_payloads),
    )


def build_trajectory_payload_integrity(
    trajectory: SectorFlowTrajectoryV1 | Mapping[str, Any],
) -> TrajectoryPayloadIntegrityV1:
    """Build the complete ordered sector/point manifest for one trajectory."""

    payload = (
        trajectory.model_dump(mode="json")
        if isinstance(trajectory, BaseModel)
        else trajectory
    )
    canonical = SectorFlowTrajectoryV1.model_validate(payload)
    sector_payloads = payload.get("sectors", ())
    sectors = tuple(
        build_sector_flow_series_integrity(item) for item in sector_payloads
    )
    return TrajectoryPayloadIntegrityV1(
        direction=canonical.direction,
        trajectory_revision=stable_sha256(payload),
        sector_count=len(sectors),
        point_count=sum(item.point_count for item in sectors),
        sectors=sectors,
    )


def build_payload_integrity(
    snapshot: MarketWatchSnapshotV1 | Mapping[str, Any],
    *,
    source_snapshot_revision: str,
) -> PayloadIntegrityV1:
    """Bind both optional trajectory manifests to one strict snapshot revision."""

    payload = (
        snapshot.model_dump(mode="json")
        if isinstance(snapshot, BaseModel)
        else snapshot
    )
    canonical = MarketWatchSnapshotV1.model_validate(payload)
    actual_revision = stable_sha256(payload)
    if source_snapshot_revision != actual_revision:
        raise ValueError("source_snapshot_revision does not match the strict snapshot payload")
    return PayloadIntegrityV1(
        source_snapshot_revision=source_snapshot_revision,
        defense=(
            build_trajectory_payload_integrity(payload["sector_flow_trajectory"])
            if canonical.sector_flow_trajectory is not None
            else None
        ),
        offense=(
            build_trajectory_payload_integrity(
                payload["offense_sector_flow_trajectory"]
            )
            if canonical.offense_sector_flow_trajectory is not None
            else None
        ),
    )


__all__ = [
    "PayloadIntegrityV1",
    "REVISION_PATTERN",
    "SectorFlowSeriesIntegrityV1",
    "TrajectoryPayloadIntegrityV1",
    "build_payload_integrity",
    "build_sector_flow_series_integrity",
    "build_trajectory_payload_integrity",
    "canonical_json_bytes",
    "stable_sha256",
]
