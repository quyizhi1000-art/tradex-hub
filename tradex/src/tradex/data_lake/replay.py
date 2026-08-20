"""Deterministic, offline reconstruction of base risk-appetite snapshots."""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from tradex.dashboard.risk_appetite import build_risk_appetite_snapshot

from .catalog import Catalog
from .contracts import (
    ArtifactRef,
    CaptureBundle,
    CapturedDataset,
    DecisionDraft,
    FeatureSnapshot,
)
from .decision_policy import POLICY_VERSION, ShadowPolicyV1
from .object_store import ObjectStore, ObjectStoreCorruptionError
from .serde import sha256_hex


REPLAY_SCHEMA_VERSION = "lake-base-replay-v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReplayError(RuntimeError):
    """Base error for a deterministic replay that cannot be completed."""


class ReplayNotFoundError(ReplayError):
    """Raised when a capture is absent or has not been atomically published."""


class ReplayArtifactError(ReplayError):
    """Raised for a missing, corrupt, or internally inconsistent input object."""


class ReplayMetadataError(ReplayError):
    """Raised when the stored feature lacks a valid replay anchor."""


@dataclass(frozen=True)
class ReplayResult:
    capture_id: str
    stored_hash: str
    recomputed_hash: str
    match: bool
    versions: Mapping[str, str | None]
    checks: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    match_scope: str = "declared_checks_only"
    final_feature_recomputed: bool = False
    limitations: tuple[str, ...] = (
        "final_feature_recomputed=false: the stored risk feature can contain "
        "stateful trajectory fields that raw capture objects cannot reconstruct",
    )


_DECISION_FIELDS = (
    "decision_id",
    "feature_id",
    "policy_version",
    "effective_at",
    "mode",
    "offense_weight",
    "defense_weight",
    "cash_weight",
    "contributions",
    "confidence",
    "abstain_reason",
)


def _records(bundle: CaptureBundle, name: str) -> list[Mapping[str, Any]]:
    dataset = bundle.datasets.get(name)
    return list(dataset.records) if dataset is not None else []


def compute_base_snapshot(bundle: CaptureBundle) -> dict[str, Any]:
    """Recompute the pure dashboard domain snapshot from one capture bundle.

    This function performs no source registration, router call, clock read, or
    filesystem access.  It deliberately uses the same pure domain builder as
    the live dashboard calculation.
    """

    if not isinstance(bundle, CaptureBundle):
        raise TypeError("bundle must be a CaptureBundle")

    leadership_dataset = bundle.datasets.get("leadership_pool")
    leadership_status = (
        copy.deepcopy(dict(leadership_dataset.status))
        if leadership_dataset is not None
        else {}
    )
    leadership_records = (
        copy.deepcopy(list(leadership_dataset.records))
        if leadership_dataset is not None
        else []
    )
    profile_status = leadership_status.get("industry_profile_status")
    profile_eligible = bool(
        isinstance(profile_status, Mapping)
        and profile_status.get("eligible_for_attribution")
    )
    if not profile_eligible:
        for record in leadership_records:
            if isinstance(record, dict):
                record.pop("sector_profile", None)

    breadth_records = _records(bundle, "market_breadth")
    market_data = bundle.market_data
    return build_risk_appetite_snapshot(
        _records(bundle, "industry_quotes"),
        _records(bundle, "concept_quotes"),
        industry_flow_records=_records(bundle, "industry_flow"),
        concept_flow_records=_records(bundle, "concept_flow"),
        index_records=(
            market_data.get("participation_indices")
            or market_data.get("indices")
            or []
        ),
        market_breadth=breadth_records[0] if breadth_records else None,
        market_turnover=market_data.get("market_turnover"),
        etf_records=_records(bundle, "etfs"),
        leadership_records=leadership_records,
        leadership_source_status=leadership_status,
        trade_date=bundle.trade_date.isoformat(),
        as_of=bundle.observed_at,
    )


def replay_marker(bundle: CaptureBundle) -> dict[str, str]:
    """Build the stable feature marker persisted by the capture pipeline."""

    return {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "base_snapshot_sha256": sha256_hex(compute_base_snapshot(bundle)),
    }


class ReplayEngine:
    """Rebuild completed captures strictly from content-addressed objects."""

    def __init__(
        self,
        root: str | Path,
        *,
        catalog: Catalog | None = None,
        object_store: ObjectStore | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.catalog = catalog or Catalog(
            self.root / "meta" / "catalog.sqlite3",
            read_only=True,
        )
        self.object_store = object_store or ObjectStore(self.root)

    @staticmethod
    def _canonical_object_path(digest: str) -> str:
        return f"objects/sha256/{digest[:2]}/{digest}.json.gz"

    def _verify_parquet(self, artifact: Mapping[str, Any]) -> None:
        artifact_id = artifact.get("artifact_id")
        relative_value = artifact.get("parquet_path")
        digest = str(artifact.get("parquet_sha256") or "")
        if not isinstance(relative_value, str) or not relative_value:
            raise ReplayArtifactError(
                f"artifact {artifact_id!r} has no published Parquet path"
            )
        if not _SHA256_RE.fullmatch(digest):
            raise ReplayArtifactError(
                f"artifact {artifact_id!r} has an invalid Parquet SHA-256"
            )
        relative = Path(relative_value)
        if (
            relative.is_absolute()
            or relative.drive
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ReplayArtifactError(
                f"artifact {artifact_id!r} has an unsafe Parquet path"
            )
        target = (self.root / relative).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ReplayArtifactError(
                f"artifact {artifact_id!r} Parquet path escapes the lake root"
            ) from exc
        if not target.is_file():
            raise ReplayArtifactError(
                f"Parquet artifact is missing for {artifact_id!r}: {relative_value}"
            )
        actual = hashlib.sha256()
        try:
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    actual.update(chunk)
        except OSError as exc:
            raise ReplayArtifactError(
                f"Parquet artifact cannot be read for {artifact_id!r}: {relative_value}"
            ) from exc
        if actual.hexdigest() != digest:
            raise ReplayArtifactError(
                f"Parquet artifact hash mismatch for {artifact_id!r}: {relative_value}"
            )

    def _read_artifact_object(self, artifact: Mapping[str, Any]) -> dict[str, Any]:
        self._verify_parquet(artifact)
        digest = str(artifact.get("object_sha256") or "")
        if not _SHA256_RE.fullmatch(digest):
            raise ReplayArtifactError(
                f"artifact {artifact.get('artifact_id')!r} has an invalid object SHA-256"
            )
        expected_path = self._canonical_object_path(digest)
        if artifact.get("object_path") != expected_path:
            raise ReplayArtifactError(
                f"artifact {artifact.get('artifact_id')!r} object path does not match its hash"
            )
        try:
            value = self.object_store.get_json(digest)
        except FileNotFoundError as exc:
            raise ReplayArtifactError(
                f"artifact object is missing for {artifact.get('artifact_id')!r}: {expected_path}"
            ) from exc
        except ObjectStoreCorruptionError as exc:
            raise ReplayArtifactError(
                f"artifact object is corrupt for {artifact.get('artifact_id')!r}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise ReplayArtifactError(
                f"artifact {artifact.get('artifact_id')!r} object must be a JSON mapping"
            )
        return value

    @staticmethod
    def _dataset_from_object(
        artifact: Mapping[str, Any],
        value: Mapping[str, Any],
    ) -> CapturedDataset:
        raw = value.get("dataset")
        if not isinstance(raw, Mapping):
            raise ReplayArtifactError(
                f"artifact {artifact.get('artifact_id')!r} has no captured dataset payload"
            )
        try:
            dataset = CapturedDataset(
                name=str(raw.get("name") or ""),
                records=tuple(raw.get("records") or ()),
                source=raw.get("source"),
                provider_as_of=raw.get("provider_as_of"),
                status=dict(raw.get("status") or {}),
                schema_version=str(raw.get("schema_version") or "raw-v1"),
            )
        except (TypeError, ValueError) as exc:
            raise ReplayArtifactError(
                f"artifact {artifact.get('artifact_id')!r} dataset contract is invalid: {exc}"
            ) from exc
        if dataset.name != artifact.get("dataset"):
            raise ReplayArtifactError(
                f"artifact dataset mismatch: catalog={artifact.get('dataset')!r}, "
                f"object={dataset.name!r}"
            )
        if dataset.schema_version != artifact.get("schema_version"):
            raise ReplayArtifactError(
                f"artifact {artifact.get('artifact_id')!r} schema version mismatch"
            )
        if len(dataset.records) != artifact.get("row_count"):
            raise ReplayArtifactError(
                f"artifact {artifact.get('artifact_id')!r} row count mismatch"
            )
        return dataset

    def _rebuild_bundle(self, record: Mapping[str, Any]) -> CaptureBundle:
        artifacts = list(record.get("artifacts") or [])
        expected_names = set(record.get("dataset_names") or [])
        if not artifacts:
            raise ReplayArtifactError("completed capture has no input artifacts")

        header: tuple[Any, ...] | None = None
        datasets: dict[str, CapturedDataset] = {}
        for artifact in artifacts:
            value = self._read_artifact_object(artifact)
            current_header = (
                value.get("capture_id"),
                value.get("observed_at"),
                value.get("trade_date"),
                value.get("minute_bucket"),
                value.get("market_phase"),
                value.get("collector_version"),
            )
            if header is None:
                header = current_header
            elif current_header != header:
                raise ReplayArtifactError("capture artifact objects disagree on bundle metadata")
            dataset = self._dataset_from_object(artifact, value)
            if dataset.name in datasets:
                raise ReplayArtifactError(f"duplicate dataset artifact {dataset.name!r}")
            datasets[dataset.name] = dataset

        if set(datasets) != expected_names:
            missing = sorted(expected_names - set(datasets))
            extra = sorted(set(datasets) - expected_names)
            raise ReplayArtifactError(
                f"capture dataset set mismatch (missing={missing!r}, extra={extra!r})"
            )
        assert header is not None
        catalog_header = (
            record.get("capture_id"),
            record.get("observed_at"),
            record.get("trade_date"),
            record.get("minute_bucket"),
            record.get("market_phase"),
            record.get("collector_version"),
        )
        if header != catalog_header:
            raise ReplayArtifactError(
                "artifact bundle header does not match the catalog run metadata"
            )

        overview = datasets.get("market_overview")
        market_data: Mapping[str, Any] = {}
        if overview is not None and overview.records:
            market_data = overview.records[0]
        try:
            bundle = CaptureBundle(
                capture_id=str(header[0]),
                observed_at=datetime.fromisoformat(str(header[1])),
                trade_date=date.fromisoformat(str(header[2])),
                minute_bucket=datetime.fromisoformat(str(header[3])),
                market_phase=str(header[4]),
                market_data=market_data,
                datasets=datasets,
                collector_version=str(header[5]),
            )
        except (TypeError, ValueError) as exc:
            raise ReplayArtifactError(f"reconstructed CaptureBundle is invalid: {exc}") from exc
        expected_bundle_hash = str(record.get("bundle_sha256") or "")
        if not _SHA256_RE.fullmatch(expected_bundle_hash):
            raise ReplayArtifactError("catalog run has an invalid CaptureBundle SHA-256")
        actual_bundle_hash = sha256_hex(bundle)
        if actual_bundle_hash != expected_bundle_hash:
            raise ReplayArtifactError(
                "reconstructed CaptureBundle hash does not match capture_runs.bundle_sha256"
            )
        return bundle

    @staticmethod
    def _artifact_contract(value: Mapping[str, Any]) -> ArtifactRef:
        try:
            return ArtifactRef(
                artifact_id=value.get("artifact_id"),
                dataset=value.get("dataset"),
                layer=value.get("layer"),
                object_sha256=value.get("object_sha256"),
                object_path=value.get("object_path"),
                parquet_path=value.get("parquet_path"),
                parquet_sha256=value.get("parquet_sha256"),
                row_count=value.get("row_count"),
                schema_version=value.get("schema_version"),
            )
        except (TypeError, ValueError) as exc:
            raise ReplayArtifactError(f"catalog ArtifactRef is invalid: {exc}") from exc

    @staticmethod
    def _feature_contract(value: Mapping[str, Any]) -> FeatureSnapshot:
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise ReplayMetadataError("catalog FeatureSnapshot payload is not a mapping")
        try:
            return FeatureSnapshot(
                feature_id=value.get("feature_id"),
                capture_id=value.get("capture_id"),
                name=value.get("name"),
                schema_version=value.get("schema_version"),
                config_version=value.get("config_version"),
                payload=payload,
                input_artifact_ids=tuple(value.get("input_artifact_ids") or ()),
                code_sha=value.get("code_sha"),
                created_at=datetime.fromisoformat(str(value.get("created_at"))),
            )
        except (TypeError, ValueError) as exc:
            raise ReplayMetadataError(
                f"catalog FeatureSnapshot is invalid: {exc}"
            ) from exc

    @staticmethod
    def _decision_contract(value: Mapping[str, Any]) -> DecisionDraft:
        contributions = value.get("contributions")
        if not isinstance(contributions, Mapping):
            raise ReplayMetadataError("catalog DecisionDraft contributions are not a mapping")
        try:
            return DecisionDraft(
                decision_id=value.get("decision_id"),
                feature_id=value.get("feature_id"),
                policy_version=value.get("policy_version"),
                effective_at=datetime.fromisoformat(str(value.get("effective_at"))),
                offense_weight=value.get("offense_weight"),
                defense_weight=value.get("defense_weight"),
                cash_weight=value.get("cash_weight"),
                contributions=contributions,
                confidence=value.get("confidence"),
                abstain_reason=value.get("abstain_reason"),
                mode=value.get("mode"),
            )
        except (TypeError, ValueError) as exc:
            raise ReplayMetadataError(
                f"catalog DecisionDraft is invalid: {exc}"
            ) from exc

    @staticmethod
    def _publication_hash(
        artifacts: tuple[ArtifactRef, ...],
        features: tuple[FeatureSnapshot, ...],
        decision: DecisionDraft | None,
    ) -> str:
        return sha256_hex(
            {
                "artifacts": sorted(artifacts, key=lambda item: item.artifact_id),
                "features": sorted(features, key=lambda item: item.feature_id),
                "decision": decision,
            }
        )

    def _stored_contracts(
        self,
        record: Mapping[str, Any],
    ) -> tuple[
        tuple[ArtifactRef, ...],
        tuple[FeatureSnapshot, ...],
        FeatureSnapshot,
        DecisionDraft | None,
    ]:
        raw_artifacts = record.get("artifacts") or []
        raw_features = record.get("features") or []
        raw_decisions = record.get("decisions") or []
        if not isinstance(raw_artifacts, list) or not all(
            isinstance(value, Mapping) for value in raw_artifacts
        ):
            raise ReplayArtifactError("catalog artifacts are not mappings")
        if not isinstance(raw_features, list) or not all(
            isinstance(value, Mapping) for value in raw_features
        ):
            raise ReplayMetadataError("catalog features are not mappings")
        if not isinstance(raw_decisions, list) or not all(
            isinstance(value, Mapping) for value in raw_decisions
        ):
            raise ReplayMetadataError("catalog decisions are not mappings")

        artifacts = tuple(self._artifact_contract(value) for value in raw_artifacts)
        features = tuple(self._feature_contract(value) for value in raw_features)
        risk_features = [feature for feature in features if feature.name == "risk_appetite"]
        if len(risk_features) != 1:
            raise ReplayMetadataError(
                "completed capture must contain exactly one risk_appetite FeatureSnapshot"
            )
        risk_feature = risk_features[0]
        capture_id = record.get("capture_id")
        if risk_feature.capture_id != capture_id:
            raise ReplayMetadataError(
                "stored risk FeatureSnapshot does not belong to the catalog capture"
            )
        known_artifact_ids = {artifact.artifact_id for artifact in artifacts}
        unknown_inputs = set(risk_feature.input_artifact_ids) - known_artifact_ids
        if unknown_inputs:
            raise ReplayMetadataError(
                "stored risk FeatureSnapshot references unknown artifacts: "
                f"{sorted(unknown_inputs)!r}"
            )

        if len(raw_decisions) > 1:
            raise ReplayMetadataError(
                "completed capture contains more than one DecisionDraft"
            )
        decision = (
            self._decision_contract(raw_decisions[0]) if raw_decisions else None
        )
        if decision is not None and decision.feature_id != risk_feature.feature_id:
            raise ReplayMetadataError(
                "stored DecisionDraft does not reference the risk FeatureSnapshot"
            )
        return artifacts, features, risk_feature, decision

    @staticmethod
    def _decision_check(
        feature: FeatureSnapshot,
        stored: DecisionDraft | None,
    ) -> tuple[dict[str, Any], DecisionDraft | None]:
        if stored is None:
            return (
                {
                    "status": "missing",
                    "match": False,
                    "supported": False,
                    "reason": "the completed capture has no stored shadow DecisionDraft",
                    "supported_policy_versions": [POLICY_VERSION],
                },
                None,
            )
        if stored.mode != "shadow":
            return (
                {
                    "status": "unsupported",
                    "match": False,
                    "supported": False,
                    "reason": f"decision mode {stored.mode!r} is not replayable as shadow",
                    "stored_policy_version": stored.policy_version,
                    "supported_policy_versions": [POLICY_VERSION],
                },
                None,
            )
        if stored.policy_version != POLICY_VERSION:
            return (
                {
                    "status": "unsupported",
                    "match": False,
                    "supported": False,
                    "reason": (
                        f"stored policy_version {stored.policy_version!r} is unsupported; "
                        f"this runtime can replay only {POLICY_VERSION!r}"
                    ),
                    "stored_policy_version": stored.policy_version,
                    "supported_policy_versions": [POLICY_VERSION],
                },
                None,
            )

        recomputed = ShadowPolicyV1().decide(
            feature,
            effective_at=stored.effective_at,
        )
        mismatched_fields = [
            field_name
            for field_name in _DECISION_FIELDS
            if sha256_hex(getattr(stored, field_name))
            != sha256_hex(getattr(recomputed, field_name))
        ]
        stored_hash = sha256_hex(stored)
        recomputed_hash = sha256_hex(recomputed)
        matched = not mismatched_fields and stored_hash == recomputed_hash
        return (
            {
                "status": "matched" if matched else "mismatch",
                "match": matched,
                "supported": True,
                "policy_version": stored.policy_version,
                "effective_at": stored.effective_at.isoformat(),
                "decision_id_match": stored.decision_id == recomputed.decision_id,
                "economic_fields_match": not any(
                    field_name != "decision_id" for field_name in mismatched_fields
                ),
                "mismatched_fields": mismatched_fields,
                "stored_hash": stored_hash,
                "recomputed_hash": recomputed_hash,
            },
            recomputed,
        )

    def rebuild(self, capture_id: str) -> ReplayResult:
        """Reconcile the verifiable parts of one capture without live access.

        The raw objects deterministically rebuild the base domain snapshot,
        but they do not rebuild the complete stored feature because live risk
        features can include stateful trajectory fields.  The stored typed
        FeatureSnapshot is instead integrity-checked through the catalog's
        publication envelope and used to replay a compatible shadow policy.
        """

        record = self.catalog.get_capture(capture_id)
        if record is None:
            raise ReplayNotFoundError(
                f"completed capture {capture_id!r} was not found"
            )
        bundle = self._rebuild_bundle(record)
        artifacts, features, feature, stored_decision = self._stored_contracts(record)
        payload = feature.payload
        marker = payload.get("_lake_replay")
        if not isinstance(marker, Mapping):
            raise ReplayMetadataError("feature is missing the _lake_replay anchor")
        stored_hash = str(marker.get("base_snapshot_sha256") or "")
        schema_version = str(marker.get("schema_version") or "")
        if not _SHA256_RE.fullmatch(stored_hash):
            raise ReplayMetadataError("feature replay anchor has an invalid base snapshot hash")
        if not schema_version:
            raise ReplayMetadataError("feature replay anchor has no schema_version")

        try:
            snapshot = compute_base_snapshot(bundle)
        except Exception as exc:
            raise ReplayArtifactError(f"captured inputs cannot be recomputed: {exc}") from exc
        recomputed_hash = sha256_hex(snapshot)
        base_snapshot_match = stored_hash == recomputed_hash

        decision_check, recomputed_decision = self._decision_check(
            feature,
            stored_decision,
        )
        expected_publication_hash = str(record.get("publication_sha256") or "")
        stored_publication_hash = self._publication_hash(
            artifacts,
            features,
            stored_decision,
        )
        publication_with_stored_matches = (
            bool(_SHA256_RE.fullmatch(expected_publication_hash))
            and stored_publication_hash == expected_publication_hash
        )
        publication_with_recomputed_hash: str | None = None
        publication_with_recomputed_matches = False
        if recomputed_decision is not None:
            publication_with_recomputed_hash = self._publication_hash(
                artifacts,
                features,
                recomputed_decision,
            )
            publication_with_recomputed_matches = (
                bool(_SHA256_RE.fullmatch(expected_publication_hash))
                and publication_with_recomputed_hash == expected_publication_hash
            )
        stored_feature_integrity = (
            publication_with_stored_matches or publication_with_recomputed_matches
        )
        stored_feature_hash = sha256_hex(feature)

        checks: dict[str, dict[str, Any]] = {
            "object_parquet_bundle": {
                "status": "matched",
                "match": True,
                "artifact_count": len(artifacts),
                "bundle_sha256": sha256_hex(bundle),
                "catalog_bundle_sha256": record.get("bundle_sha256"),
            },
            "base_snapshot": {
                "status": "matched" if base_snapshot_match else "mismatch",
                "match": base_snapshot_match,
                "stored_hash": stored_hash,
                "recomputed_hash": recomputed_hash,
                "schema_version": schema_version,
            },
            "stored_final_feature_hash": {
                "status": "matched" if stored_feature_integrity else "mismatch",
                "match": stored_feature_integrity,
                "stored_hash": stored_feature_hash,
                "final_feature_recomputed": False,
                "integrity_anchor": "capture_runs.publication_sha256",
                "catalog_publication_sha256": expected_publication_hash,
                "publication_hash_with_stored_decision": stored_publication_hash,
                "publication_match_with_stored_decision": publication_with_stored_matches,
                "publication_hash_with_recomputed_decision": (
                    publication_with_recomputed_hash
                ),
                "publication_match_with_recomputed_decision": (
                    publication_with_recomputed_matches
                ),
                "limitation": (
                    "this is the canonical hash of the catalog-stored FeatureSnapshot; "
                    "the final stateful feature was not rebuilt from raw objects"
                ),
            },
            "shadow_decision": decision_check,
        }
        declared_matches = [check.get("match") is True for check in checks.values()]
        return ReplayResult(
            capture_id=capture_id,
            stored_hash=stored_hash,
            recomputed_hash=recomputed_hash,
            match=all(declared_matches),
            versions={
                "replay_schema_version": schema_version,
                "feature_schema_version": feature.schema_version,
                "feature_config_version": feature.config_version,
                "stored_policy_version": (
                    stored_decision.policy_version if stored_decision is not None else None
                ),
                "supported_policy_version": POLICY_VERSION,
                "base_snapshot_version": str(snapshot.get("version") or "") or None,
                "attribution_version": str(snapshot.get("attribution_version") or "") or None,
            },
            checks=checks,
            final_feature_recomputed=False,
        )


__all__ = [
    "REPLAY_SCHEMA_VERSION",
    "ReplayArtifactError",
    "ReplayEngine",
    "ReplayError",
    "ReplayMetadataError",
    "ReplayNotFoundError",
    "ReplayResult",
    "compute_base_snapshot",
    "replay_marker",
]
