"""Focused tests for data-lake contracts and canonical serialization."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from tradex.data_lake.contracts import (
    ArtifactRef,
    CaptureBundle,
    CapturedDataset,
    DecisionDraft,
    FeatureSnapshot,
)
from tradex.data_lake.serde import canonical_json, sha256_hex, to_jsonable


SHANGHAI = ZoneInfo("Asia/Shanghai")
OBSERVED_AT = datetime(2026, 8, 19, 10, 1, 23, tzinfo=SHANGHAI)
MINUTE = OBSERVED_AT.replace(second=0)


def _dataset() -> CapturedDataset:
    return CapturedDataset(
        name="market_breadth",
        records=({"上涨": np.int64(3200), "ratio": np.float64(0.64)},),
        source="eastmoney",
        provider_as_of="2026-08-19T10:01:00+08:00",
        status={"stale": False},
    )


def test_canonical_json_normalizes_dates_numpy_and_non_finite_values():
    payload = {
        "z": np.float64(float("nan")),
        "positive_inf": float("inf"),
        "negative_inf": np.float32(float("-inf")),
        "count": np.int64(7),
        "flag": np.bool_(True),
        "day": date(2026, 8, 19),
        "at": OBSERVED_AT,
    }

    encoded = canonical_json(payload)
    decoded = json.loads(encoded)

    assert encoded == canonical_json(dict(reversed(list(payload.items()))))
    assert decoded == {
        "at": "2026-08-19T10:01:23+08:00",
        "count": 7,
        "day": "2026-08-19",
        "flag": True,
        "negative_inf": None,
        "positive_inf": None,
        "z": None,
    }
    assert sha256_hex(payload) == sha256_hex(to_jsonable(payload))
    assert len(sha256_hex(payload)) == 64


def test_capture_bundle_requires_shanghai_aware_exact_minute():
    dataset = _dataset()
    bundle = CaptureBundle(
        capture_id="capture-1",
        observed_at=OBSERVED_AT,
        trade_date=OBSERVED_AT.date(),
        minute_bucket=MINUTE,
        market_phase="trading",
        market_data={"provider_as_of": dataset.provider_as_of},
        datasets={dataset.name: dataset},
    )
    assert bundle.minute_bucket == MINUTE

    with pytest.raises(ValueError, match="timezone-aware"):
        CaptureBundle(
            capture_id="capture-naive",
            observed_at=OBSERVED_AT.replace(tzinfo=None),
            trade_date=OBSERVED_AT.date(),
            minute_bucket=MINUTE,
            market_phase="trading",
            market_data={},
            datasets={},
        )

    with pytest.raises(ValueError, match="Asia/Shanghai"):
        CaptureBundle(
            capture_id="capture-utc",
            observed_at=OBSERVED_AT.astimezone(timezone.utc),
            trade_date=OBSERVED_AT.date(),
            minute_bucket=MINUTE,
            market_phase="trading",
            market_data={},
            datasets={},
        )

    with pytest.raises(ValueError, match="exact minute"):
        CaptureBundle(
            capture_id="capture-seconds",
            observed_at=OBSERVED_AT,
            trade_date=OBSERVED_AT.date(),
            minute_bucket=OBSERVED_AT,
            market_phase="trading",
            market_data={},
            datasets={},
        )


def test_capture_bundle_rejects_dataset_key_mismatch():
    with pytest.raises(ValueError, match="does not match"):
        CaptureBundle(
            capture_id="capture-1",
            observed_at=OBSERVED_AT,
            trade_date=OBSERVED_AT.date(),
            minute_bucket=MINUTE,
            market_phase="trading",
            market_data={},
            datasets={"wrong_name": _dataset()},
        )


def test_artifact_feature_and_decision_contracts_validate_audit_fields():
    artifact = ArtifactRef(
        artifact_id="artifact-1",
        dataset="market_breadth",
        layer="raw",
        object_sha256="a" * 64,
        object_path="objects/sha256/aa/" + "a" * 64 + ".json.gz",
        parquet_path=None,
        parquet_sha256=None,
        row_count=1,
        schema_version="raw-v1",
    )
    feature = FeatureSnapshot(
        feature_id="feature-1",
        capture_id="capture-1",
        name="risk_appetite",
        schema_version="risk-feature-v1",
        config_version="risk-appetite-v1.3",
        payload={"state": "medium"},
        input_artifact_ids=[artifact.artifact_id],
        code_sha="deadbeef",
        created_at=OBSERVED_AT,
    )
    decision = DecisionDraft(
        decision_id="decision-1",
        feature_id=feature.feature_id,
        policy_version="shadow-v1",
        effective_at=OBSERVED_AT,
        offense_weight=0.3,
        defense_weight=0.4,
        cash_weight=0.3,
        contributions={"market": 0.1},
        confidence=0.7,
    )

    assert feature.input_artifact_ids == ("artifact-1",)
    assert decision.mode == "shadow"

    with pytest.raises(ValueError, match="must either both be set"):
        ArtifactRef(
            artifact_id="bad-parquet-ref",
            dataset="market_breadth",
            layer="raw",
            object_sha256="a" * 64,
            object_path="objects/sha256/aa/" + "a" * 64 + ".json.gz",
            parquet_path="parquet/raw/market_breadth/part-0.parquet",
            parquet_sha256=None,
            row_count=1,
            schema_version="raw-v1",
        )

    with pytest.raises(ValueError, match="must equal 1"):
        DecisionDraft(
            decision_id="bad-sum",
            feature_id="feature-1",
            policy_version="shadow-v1",
            effective_at=OBSERVED_AT,
            offense_weight=0.3,
            defense_weight=0.3,
            cash_weight=0.3,
        )

    with pytest.raises(ValueError, match="finite and non-negative"):
        DecisionDraft(
            decision_id="bad-nan",
            feature_id="feature-1",
            policy_version="shadow-v1",
            effective_at=OBSERVED_AT,
            offense_weight=float("nan"),
            defense_weight=0.5,
            cash_weight=0.5,
        )
