"""Versioned, dependency-free contracts for durable Tradex captures."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WEIGHT_TOLERANCE = 1e-9


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_mapping(value: Mapping[str, Any], field_name: str) -> None:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")


def _require_shanghai_datetime(value: datetime, field_name: str) -> datetime:
    """Require an aware datetime expressed in the Shanghai local timezone.

    ZoneInfo-backed values must explicitly use ``Asia/Shanghai``.  Fixed
    ``+08:00`` offsets are also accepted because ISO round-trips lose the IANA
    timezone key while retaining the correct local-time contract.
    """

    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")

    timezone_key = getattr(value.tzinfo, "key", None)
    if timezone_key is not None and timezone_key != "Asia/Shanghai":
        raise ValueError(f"{field_name} must use Asia/Shanghai")

    shanghai_value = value.astimezone(SHANGHAI)
    if (
        value.utcoffset() != shanghai_value.utcoffset()
        or value.replace(tzinfo=None) != shanghai_value.replace(tzinfo=None)
    ):
        raise ValueError(f"{field_name} must be expressed in Asia/Shanghai local time")
    return value


@dataclass(frozen=True)
class CapturedDataset:
    """One named upstream dataset as observed by a capture run."""

    name: str
    records: tuple[Mapping[str, Any], ...] = ()
    source: str | None = None
    provider_as_of: str | None = None
    status: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = "raw-v1"

    def __post_init__(self) -> None:
        _require_text(self.name, "name")
        _require_text(self.schema_version, "schema_version")
        if self.source is not None:
            _require_text(self.source, "source")
        if self.provider_as_of is not None:
            _require_text(self.provider_as_of, "provider_as_of")

        records = tuple(self.records)
        for index, record in enumerate(records):
            _require_mapping(record, f"records[{index}]")
        object.__setattr__(self, "records", records)
        _require_mapping(self.status, "status")


@dataclass(frozen=True)
class CaptureBundle:
    """Complete set of inputs observed in one Shanghai market minute."""

    capture_id: str
    observed_at: datetime
    trade_date: date
    minute_bucket: datetime
    market_phase: str
    market_data: Mapping[str, Any]
    datasets: Mapping[str, CapturedDataset]
    collector_version: str = "tradex-live-v1"

    def __post_init__(self) -> None:
        _require_text(self.capture_id, "capture_id")
        _require_text(self.market_phase, "market_phase")
        _require_text(self.collector_version, "collector_version")
        _require_shanghai_datetime(self.observed_at, "observed_at")
        _require_shanghai_datetime(self.minute_bucket, "minute_bucket")

        if not isinstance(self.trade_date, date) or isinstance(self.trade_date, datetime):
            raise TypeError("trade_date must be a date")
        if self.observed_at.date() != self.trade_date:
            raise ValueError("observed_at must belong to trade_date")
        if self.minute_bucket.date() != self.trade_date:
            raise ValueError("minute_bucket must belong to trade_date")
        if self.minute_bucket.second or self.minute_bucket.microsecond:
            raise ValueError("minute_bucket must be rounded to an exact minute")

        _require_mapping(self.market_data, "market_data")
        _require_mapping(self.datasets, "datasets")
        for key, dataset in self.datasets.items():
            if not isinstance(key, str) or not key:
                raise ValueError("dataset keys must be non-empty strings")
            if not isinstance(dataset, CapturedDataset):
                raise TypeError(f"datasets[{key!r}] must be a CapturedDataset")
            if key != dataset.name:
                raise ValueError(
                    f"dataset key {key!r} does not match dataset name {dataset.name!r}"
                )


@dataclass(frozen=True)
class ArtifactRef:
    """Manifest reference to one immutable object/Parquet artifact."""

    artifact_id: str
    dataset: str
    layer: str
    object_sha256: str
    object_path: str
    parquet_path: str | None
    parquet_sha256: str | None
    row_count: int
    schema_version: str

    def __post_init__(self) -> None:
        for field_name in (
            "artifact_id",
            "dataset",
            "layer",
            "object_path",
            "schema_version",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not _SHA256_RE.fullmatch(self.object_sha256):
            raise ValueError("object_sha256 must be a lowercase SHA-256 hex digest")
        if (self.parquet_path is None) != (self.parquet_sha256 is None):
            raise ValueError(
                "parquet_path and parquet_sha256 must either both be set or both be null"
            )
        if self.parquet_path is not None:
            _require_text(self.parquet_path, "parquet_path")
            if not _SHA256_RE.fullmatch(self.parquet_sha256 or ""):
                raise ValueError("parquet_sha256 must be a lowercase SHA-256 hex digest")
        if isinstance(self.row_count, bool) or not isinstance(self.row_count, int):
            raise TypeError("row_count must be an integer")
        if self.row_count < 0:
            raise ValueError("row_count must not be negative")


@dataclass(frozen=True)
class FeatureSnapshot:
    """A versioned feature result tied to immutable capture artifacts."""

    feature_id: str
    capture_id: str
    name: str
    schema_version: str
    config_version: str
    payload: Mapping[str, Any]
    input_artifact_ids: tuple[str, ...]
    code_sha: str
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "feature_id",
            "capture_id",
            "name",
            "schema_version",
            "config_version",
            "code_sha",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_mapping(self.payload, "payload")
        artifact_ids = tuple(self.input_artifact_ids)
        for artifact_id in artifact_ids:
            _require_text(artifact_id, "input_artifact_ids item")
        object.__setattr__(self, "input_artifact_ids", artifact_ids)
        _require_shanghai_datetime(self.created_at, "created_at")


@dataclass(frozen=True)
class DecisionDraft:
    """A shadow allocation decision whose three weights form one portfolio."""

    decision_id: str
    feature_id: str
    policy_version: str
    effective_at: datetime
    offense_weight: float
    defense_weight: float
    cash_weight: float
    contributions: Mapping[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    abstain_reason: str | None = None
    mode: str = "shadow"

    def __post_init__(self) -> None:
        for field_name in ("decision_id", "feature_id", "policy_version", "mode"):
            _require_text(getattr(self, field_name), field_name)
        _require_shanghai_datetime(self.effective_at, "effective_at")
        _require_mapping(self.contributions, "contributions")
        if self.abstain_reason is not None:
            _require_text(self.abstain_reason, "abstain_reason")

        weights = (
            self.offense_weight,
            self.defense_weight,
            self.cash_weight,
        )
        for field_name, value in zip(
            ("offense_weight", "defense_weight", "cash_weight"),
            weights,
            strict=True,
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{field_name} must be a number")
            if not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError(f"{field_name} must be finite and non-negative")
        if not math.isclose(sum(float(value) for value in weights), 1.0, abs_tol=_WEIGHT_TOLERANCE):
            raise ValueError("offense_weight + defense_weight + cash_weight must equal 1")

        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise TypeError("confidence must be a number")
        confidence = float(self.confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be finite and between 0 and 1")


__all__ = [
    "ArtifactRef",
    "CaptureBundle",
    "CapturedDataset",
    "DecisionDraft",
    "FeatureSnapshot",
    "SHANGHAI",
]
