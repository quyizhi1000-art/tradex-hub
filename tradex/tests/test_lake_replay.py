"""Focused tests for deterministic, object-only base snapshot replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tradex.data_lake.catalog import Catalog
from tradex.data_lake.contracts import (
    ArtifactRef,
    CaptureBundle,
    CapturedDataset,
    FeatureSnapshot,
)
from tradex.data_lake.live_source import LiveCaptureResult
from tradex.data_lake.object_store import ObjectRef, ObjectStore
from tradex.data_lake.decision_policy import POLICY_VERSION, ShadowPolicyV1
from tradex.data_lake.pipeline import CapturePipeline
from tradex.data_lake.replay import (
    REPLAY_SCHEMA_VERSION,
    ReplayArtifactError,
    ReplayEngine,
    compute_base_snapshot,
    replay_marker,
)
from tradex.data_lake.serde import canonical_json


SHANGHAI = ZoneInfo("Asia/Shanghai")
OBSERVED = datetime(2026, 8, 19, 10, 5, 20, tzinfo=SHANGHAI)
MINUTE = OBSERVED.replace(second=0)


def _ready_status(*, profile_eligible: bool = True) -> dict:
    return {
        "source": "fixture",
        "provider_as_of": "2026-08-19T10:05:00+08:00",
        "stale": False,
        "partial": False,
        "expired": False,
        "eligible_for_vote": True,
        "source_valid": True,
        "data_date": "20260819",
        "industry_profile_status": {
            "eligible_for_attribution": profile_eligible,
        },
    }


def _bundle(capture_id: str = "capture-1", *, profile_eligible: bool = True) -> CaptureBundle:
    industry = (
        {"板块代码": "BK001", "板块名称": "种植业", "涨跌幅": 3.0,
         "上涨家数": 70, "下跌家数": 30, "主力净流入-占比": 2.0},
        {"板块代码": "BK002", "板块名称": "证券", "涨跌幅": 1.0,
         "上涨家数": 60, "下跌家数": 40, "主力净流入-占比": 1.0},
    )
    leadership = ({
        "代码": "600001",
        "涨停原因": "未映射原因",
        "连板": "首板",
        "sector_profile": {"industry": "种植业", "concept_tags": []},
    },)
    datasets = {
        "industry_quotes": CapturedDataset(
            "industry_quotes", industry, "fixture", "2026-08-19T10:05:00+08:00", _ready_status()
        ),
        "concept_quotes": CapturedDataset(
            "concept_quotes", ({"板块代码": "C1", "板块名称": "人工智能", "涨跌幅": 0.5},),
            "fixture", "2026-08-19T10:05:00+08:00", _ready_status()
        ),
        "industry_flow": CapturedDataset(
            "industry_flow", ({"板块": "种植业", "主力净流入-占比": 2.0},),
            "fixture", "2026-08-19T10:05:00+08:00", _ready_status()
        ),
        "concept_flow": CapturedDataset(
            "concept_flow", (), "fixture", "2026-08-19T10:05:00+08:00", _ready_status()
        ),
        "market_breadth": CapturedDataset(
            "market_breadth", ({"上涨": 3200, "下跌": 1800},),
            "fixture", "2026-08-19T10:05:00+08:00", _ready_status()
        ),
        "etfs": CapturedDataset(
            "etfs", (), "fixture", "2026-08-19T10:05:00+08:00", _ready_status()
        ),
        "leadership_pool": CapturedDataset(
            "leadership_pool", leadership, "fixture", "20260819",
            _ready_status(profile_eligible=profile_eligible)
        ),
    }
    market_data = {
        "provider_as_of": "2026-08-19T10:05:00+08:00",
        "participation_indices": [
            {"名称": "上证指数", "涨跌幅": 0.8},
            {"名称": "深证成指", "涨跌幅": 0.9},
            {"名称": "创业板指", "涨跌幅": 1.2},
        ],
        "market_turnover": {"available": True, "direction": "expand", "difference": 1},
        "source": "fixture",
    }
    datasets["market_overview"] = CapturedDataset(
        "market_overview", (market_data,), "fixture",
        "2026-08-19T10:05:00+08:00", _ready_status()
    )
    return CaptureBundle(
        capture_id=capture_id,
        observed_at=OBSERVED,
        trade_date=OBSERVED.date(),
        minute_bucket=MINUTE,
        market_phase="trading",
        market_data=market_data,
        datasets=datasets,
    )


def _publish(
    root: Path,
    bundle: CaptureBundle,
    *,
    stored_hash: str | None = None,
    policy_version: str | None = None,
):
    store = ObjectStore(root)
    catalog = Catalog(root / "meta" / "catalog.sqlite3")
    catalog.begin_capture(bundle)
    artifacts = []
    for name in sorted(bundle.datasets):
        dataset = bundle.datasets[name]
        reference = store.put_json(
            f"capture/{name}",
            {
                "capture_id": bundle.capture_id,
                "observed_at": bundle.observed_at,
                "trade_date": bundle.trade_date,
                "minute_bucket": bundle.minute_bucket,
                "market_phase": bundle.market_phase,
                "collector_version": bundle.collector_version,
                "dataset": dataset,
            },
        )
        parquet_path = f"parquet/raw/{name}/part-0.parquet"
        parquet_bytes = f"fixture-parquet:{bundle.capture_id}:{name}".encode("utf-8")
        parquet_target = root / parquet_path
        parquet_target.parent.mkdir(parents=True, exist_ok=True)
        parquet_target.write_bytes(parquet_bytes)
        artifacts.append(ArtifactRef(
            artifact_id=f"raw:{bundle.capture_id}:{name}",
            dataset=name,
            layer="raw",
            object_sha256=reference.sha256,
            object_path=reference.relative_path,
            parquet_path=parquet_path,
            parquet_sha256=hashlib.sha256(parquet_bytes).hexdigest(),
            row_count=len(dataset.records),
            schema_version=dataset.schema_version,
        ))
    marker = replay_marker(bundle)
    if stored_hash is not None:
        marker["base_snapshot_sha256"] = stored_hash
    feature = FeatureSnapshot(
        feature_id=f"feature-{bundle.capture_id}",
        capture_id=bundle.capture_id,
        name="risk_appetite",
        schema_version="risk-feature-v1",
        config_version="risk-appetite-v1.3",
        payload={"version": "risk-appetite-v1.3", "_lake_replay": marker},
        input_artifact_ids=tuple(artifact.artifact_id for artifact in artifacts),
        code_sha="fixture",
        created_at=bundle.observed_at,
    )
    decision = ShadowPolicyV1().decide(feature, effective_at=bundle.observed_at)
    if policy_version is not None:
        decision = replace(decision, policy_version=policy_version)
    catalog.commit_capture(
        bundle.capture_id,
        artifacts,
        features=(feature,),
        decision=decision,
    )
    return catalog, store, artifacts


def test_compute_base_snapshot_removes_ineligible_industry_profile():
    eligible = compute_base_snapshot(_bundle(profile_eligible=True))
    ineligible = compute_base_snapshot(_bundle(profile_eligible=False))

    assert eligible["version"] == "risk-appetite-v1.3"
    assert eligible["sectors"]["agriculture"]["leadership"]["matched_count"] == 1
    assert ineligible["sectors"]["agriculture"]["leadership"]["matched_count"] == 0
    assert eligible["market_participation"]["metrics"]["market_breadth"][
        "ratio"
    ] == pytest.approx(0.64)


def test_replay_rebuilds_completed_objects_and_reports_versions_without_network(tmp_path: Path):
    bundle = _bundle()
    catalog, store, _ = _publish(tmp_path, bundle)
    try:
        result = ReplayEngine(tmp_path, catalog=catalog, object_store=store).rebuild(
            bundle.capture_id
        )
    finally:
        catalog.close()

    assert result.match is True
    assert result.stored_hash == result.recomputed_hash
    assert result.versions == {
        "replay_schema_version": REPLAY_SCHEMA_VERSION,
        "feature_schema_version": "risk-feature-v1",
        "feature_config_version": "risk-appetite-v1.3",
        "stored_policy_version": POLICY_VERSION,
        "supported_policy_version": POLICY_VERSION,
        "base_snapshot_version": "risk-appetite-v1.3",
        "attribution_version": result.versions["attribution_version"],
    }
    assert result.versions["attribution_version"]
    assert set(result.checks) == {
        "object_parquet_bundle",
        "base_snapshot",
        "stored_final_feature_hash",
        "shadow_decision",
    }
    assert all(check["match"] is True for check in result.checks.values())
    assert result.match_scope == "declared_checks_only"
    assert result.checks["shadow_decision"]["stored_hash"] == result.checks[
        "shadow_decision"
    ]["recomputed_hash"]
    assert result.checks["shadow_decision"]["mismatched_fields"] == []
    assert result.final_feature_recomputed is False
    assert "stateful trajectory" in result.limitations[0]
    assert result.checks["stored_final_feature_hash"][
        "final_feature_recomputed"
    ] is False


def test_replay_reports_a_deterministic_hash_mismatch(tmp_path: Path):
    bundle = _bundle()
    catalog, store, _ = _publish(tmp_path, bundle, stored_hash="0" * 64)
    try:
        result = ReplayEngine(tmp_path, catalog=catalog, object_store=store).rebuild(
            bundle.capture_id
        )
    finally:
        catalog.close()

    assert result.match is False
    assert result.stored_hash == "0" * 64
    assert result.recomputed_hash == replay_marker(bundle)["base_snapshot_sha256"]
    assert result.checks["base_snapshot"]["match"] is False
    assert result.checks["shadow_decision"]["match"] is True


@pytest.mark.parametrize(
    ("damage", "expected_field"),
    [
        ("decision_id", "decision_id"),
        ("economic_weights", "offense_weight"),
    ],
)
def test_replay_detects_valid_but_tampered_shadow_decision(
    tmp_path: Path,
    damage: str,
    expected_field: str,
):
    bundle = _bundle()
    catalog, store, _ = _publish(tmp_path, bundle)
    if damage == "decision_id":
        catalog._connection.execute(
            "UPDATE decisions SET decision_id = 'shadow-tampered' WHERE capture_id = ?",
            (bundle.capture_id,),
        )
    else:
        catalog._connection.execute(
            """
            UPDATE decisions
            SET offense_weight = 0.1, defense_weight = 0.0, cash_weight = 0.9
            WHERE capture_id = ?
            """,
            (bundle.capture_id,),
        )
    try:
        result = ReplayEngine(tmp_path, catalog=catalog, object_store=store).rebuild(
            bundle.capture_id
        )
    finally:
        catalog.close()

    decision = result.checks["shadow_decision"]
    assert result.match is False
    assert decision["status"] == "mismatch"
    assert decision["match"] is False
    assert expected_field in decision["mismatched_fields"]
    assert decision["stored_hash"] != decision["recomputed_hash"]
    # Replacing only the damaged decision with the deterministic replay still
    # reaches the original publication anchor, isolating the stored feature.
    assert result.checks["stored_final_feature_hash"]["match"] is True
    assert result.checks["stored_final_feature_hash"][
        "publication_match_with_recomputed_decision"
    ] is True


def test_replay_detects_tampered_stored_final_feature_without_claiming_rebuild(
    tmp_path: Path,
):
    bundle = _bundle()
    catalog, store, _ = _publish(tmp_path, bundle)
    row = catalog._connection.execute(
        "SELECT payload_json FROM features WHERE capture_id = ?",
        (bundle.capture_id,),
    ).fetchone()
    payload = json.loads(row[0])
    payload["stateful_tamper"] = {"trajectory": 999}
    catalog._connection.execute(
        "UPDATE features SET payload_json = ? WHERE capture_id = ?",
        (canonical_json(payload), bundle.capture_id),
    )
    try:
        result = ReplayEngine(tmp_path, catalog=catalog, object_store=store).rebuild(
            bundle.capture_id
        )
    finally:
        catalog.close()

    assert result.match is False
    assert result.checks["object_parquet_bundle"]["match"] is True
    assert result.checks["base_snapshot"]["match"] is True
    assert result.checks["shadow_decision"]["match"] is True
    feature_check = result.checks["stored_final_feature_hash"]
    assert feature_check["match"] is False
    assert feature_check["final_feature_recomputed"] is False
    assert "not rebuilt" in feature_check["limitation"]


def test_replay_marks_unknown_policy_version_unsupported_not_matched(tmp_path: Path):
    bundle = _bundle()
    catalog, store, _ = _publish(
        tmp_path,
        bundle,
        policy_version="shadow-allocation-v0",
    )
    try:
        result = ReplayEngine(tmp_path, catalog=catalog, object_store=store).rebuild(
            bundle.capture_id
        )
    finally:
        catalog.close()

    decision = result.checks["shadow_decision"]
    assert result.match is False
    assert decision["status"] == "unsupported"
    assert decision["supported"] is False
    assert decision["match"] is False
    assert "shadow-allocation-v0" in decision["reason"]
    assert POLICY_VERSION in decision["supported_policy_versions"]
    assert result.checks["stored_final_feature_hash"]["match"] is True


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_replay_fails_explicitly_for_missing_or_corrupt_objects(tmp_path: Path, damage: str):
    bundle = _bundle()
    catalog, store, artifacts = _publish(tmp_path, bundle)
    target = root_object = tmp_path / artifacts[0].object_path
    if damage == "missing":
        target.unlink()
    else:
        target.write_bytes(b"not-gzip")
    try:
        with pytest.raises(ReplayArtifactError, match=damage):
            ReplayEngine(tmp_path, catalog=catalog, object_store=store).rebuild(
                bundle.capture_id
            )
    finally:
        catalog.close()
    assert root_object == target


@pytest.mark.parametrize("damage", ["missing", "hash"])
def test_replay_fails_for_missing_or_hash_mismatched_parquet(
    tmp_path: Path,
    damage: str,
):
    bundle = _bundle()
    catalog, store, artifacts = _publish(tmp_path, bundle)
    target = tmp_path / artifacts[0].parquet_path
    if damage == "missing":
        target.unlink()
        match = "missing"
    else:
        target.write_bytes(b"tampered-parquet")
        match = "hash mismatch"
    try:
        with pytest.raises(ReplayArtifactError, match=match):
            ReplayEngine(tmp_path, catalog=catalog, object_store=store).rebuild(
                bundle.capture_id
            )
    finally:
        catalog.close()


@pytest.mark.parametrize("damage", ["catalog_header", "bundle_hash"])
def test_replay_binds_artifacts_to_catalog_header_and_bundle_hash(
    tmp_path: Path,
    damage: str,
):
    bundle = _bundle()
    catalog, store, _ = _publish(tmp_path, bundle)
    if damage == "catalog_header":
        catalog._connection.execute(
            "UPDATE capture_runs SET market_phase = 'closed' WHERE capture_id = ?",
            (bundle.capture_id,),
        )
        match = "header does not match"
    else:
        catalog._connection.execute(
            "UPDATE capture_runs SET bundle_sha256 = ? WHERE capture_id = ?",
            ("0" * 64, bundle.capture_id),
        )
        match = "bundle_sha256"
    try:
        with pytest.raises(ReplayArtifactError, match=match):
            ReplayEngine(tmp_path, catalog=catalog, object_store=store).rebuild(
                bundle.capture_id
            )
    finally:
        catalog.close()


def test_replay_default_catalog_is_read_only(tmp_path: Path):
    bundle = _bundle()
    catalog, _, _ = _publish(tmp_path, bundle)
    catalog.close()
    db_path = tmp_path / "meta" / "catalog.sqlite3"
    wal_path = Path(f"{db_path}-wal")
    shm_path = Path(f"{db_path}-shm")

    engine = ReplayEngine(tmp_path)
    try:
        assert engine.catalog.read_only is True
        assert engine.rebuild(bundle.capture_id).match is True
    finally:
        engine.catalog.close()

    assert not wal_path.exists()
    assert not shm_path.exists()


class _FakeSource:
    def __init__(self, bundle: CaptureBundle) -> None:
        self.bundle = bundle

    def capture(self, capture_id: str) -> LiveCaptureResult:
        return LiveCaptureResult(
            bundle=CapturePipeline._reidentify(self.bundle, capture_id),
            feature_payload={"version": "risk-appetite-v1.3", "existing": "kept"},
        )


class _FakeParquetWriter:
    def write_capture(
        self, dataset: CapturedDataset, bundle: CaptureBundle, reference: ObjectRef
    ) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=f"raw:{bundle.capture_id}:{dataset.name}",
            dataset=dataset.name,
            layer="raw",
            object_sha256=reference.sha256,
            object_path=reference.relative_path,
            parquet_path=f"parquet/raw/{dataset.name}/part-0.parquet",
            parquet_sha256="c" * 64,
            row_count=len(dataset.records),
            schema_version=dataset.schema_version,
        )


def test_pipeline_keeps_feature_fields_and_adds_replay_anchor(tmp_path: Path):
    bundle = _bundle()
    pipeline = CapturePipeline(
        tmp_path,
        source=_FakeSource(bundle),
        parquet_writer=_FakeParquetWriter(),
        code_sha="fixture",
    )
    try:
        result = pipeline.capture_once(OBSERVED)
        published = pipeline.catalog.get_capture(result.capture_id)
    finally:
        pipeline.catalog.close()

    assert result.status == "completed"
    payload = published["features"][0]["payload"]
    assert payload["version"] == "risk-appetite-v1.3"
    assert payload["existing"] == "kept"
    assert payload["_lake_replay"]["schema_version"] == REPLAY_SCHEMA_VERSION
    rebuilt_bundle = CapturePipeline._reidentify(bundle, result.capture_id)
    assert payload["_lake_replay"] == replay_marker(rebuilt_bundle)
