"""Atomic live-capture pipeline: observe, persist immutable facts, then publish."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .contracts import CaptureBundle, FeatureSnapshot, SHANGHAI
from .live_source import TradexLiveSource
from .object_store import ObjectStore
from .replay import replay_marker
from .serde import sha256_hex


_CAPTURE_NAMESPACE = uuid.UUID("b6fab4a4-d6e9-4b56-b036-252921fd3518")
FEATURE_SCHEMA_VERSION = "risk-feature-v1"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CaptureResult:
    capture_id: str
    status: str
    artifact_count: int
    feature_id: str | None = None
    decision_id: str | None = None
    error: str | None = None


def capture_id_for_minute(minute: datetime, collector_version: str = "tradex-live-v1") -> str:
    if minute.tzinfo is None or minute.utcoffset() is None:
        raise ValueError("capture minute must be timezone-aware")
    local = minute.astimezone(SHANGHAI).replace(second=0, microsecond=0)
    material = f"{collector_version}|{local.isoformat()}"
    return str(uuid.uuid5(_CAPTURE_NAMESPACE, material))


def _feature_id(capture_id: str) -> str:
    return str(uuid.uuid5(_CAPTURE_NAMESPACE, f"feature|{capture_id}|risk-appetite"))


def _git_code_sha() -> str | None:
    candidates = [Path.cwd(), *Path(__file__).resolve().parents]
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        try:
            head = subprocess.run(
                ["git", "-C", str(candidate), "rev-parse", "--verify", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        revision = head.stdout.strip().lower()
        if head.returncode != 0 or len(revision) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in revision
        ):
            continue
        try:
            status = subprocess.run(
                ["git", "-C", str(candidate), "status", "--porcelain"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if status.returncode != 0:
            continue
        return f"{revision}+dirty" if status.stdout else revision
    return None


def _default_code_sha() -> str:
    configured = (os.environ.get("TRADEX_CODE_SHA") or "").strip()
    if configured:
        return configured
    return _git_code_sha() or "working-tree"


class CapturePipeline:
    """Publish one completed capture or leave an explicitly failed run.

    Files are immutable and may be written before the SQLite transaction.  A
    crash can therefore leave harmless orphan files, but readers never see a
    half-published capture because the catalog exposes only ``completed`` runs.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        source: Any | None = None,
        object_store: ObjectStore | None = None,
        catalog: Catalog | None = None,
        parquet_writer: Any | None = None,
        policy: Any | None = None,
        code_sha: str | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.source = source or TradexLiveSource()
        self.object_store = object_store or ObjectStore(self.root)
        self.catalog = catalog or Catalog(self.root / "meta" / "catalog.sqlite3")
        self._preflight_default_writer = parquet_writer is None
        if parquet_writer is None:
            from .parquet_writer import ParquetWriter

            parquet_writer = ParquetWriter(self.root)
        self.parquet_writer = parquet_writer
        if policy is None:
            from .decision_policy import ShadowPolicyV1

            policy = ShadowPolicyV1()
        self.policy = policy
        self.code_sha = (code_sha or _default_code_sha()).strip()
        if not self.code_sha:
            raise ValueError("code_sha must not be empty")

    @staticmethod
    def _existing_result(
        capture_id: str,
        existing: dict[str, Any] | None,
    ) -> CaptureResult | None:
        if existing is None:
            return None
        if existing.get("status") == "completed":
            decisions = existing.get("decisions") or []
            features = existing.get("features") or []
            return CaptureResult(
                capture_id=capture_id,
                status="already_completed",
                artifact_count=len(existing.get("artifacts") or []),
                feature_id=features[0].get("feature_id") if features else None,
                decision_id=decisions[0].get("decision_id") if decisions else None,
            )
        if existing.get("status") == "failed":
            prior_error = existing.get("error") or "unknown prior failure"
            return CaptureResult(
                capture_id=capture_id,
                status="already_failed",
                artifact_count=len(existing.get("artifacts") or []),
                error=(
                    f"This capture identity already failed: {prior_error}. "
                    "Wait for the next minute or use a new capture identity; "
                    "the failed minute is immutable."
                ),
            )
        return None

    @staticmethod
    def _reidentify(bundle: CaptureBundle, capture_id: str) -> CaptureBundle:
        if bundle.capture_id == capture_id:
            return bundle
        return CaptureBundle(
            capture_id=capture_id,
            observed_at=bundle.observed_at,
            trade_date=bundle.trade_date,
            minute_bucket=bundle.minute_bucket,
            market_phase=bundle.market_phase,
            market_data=bundle.market_data,
            datasets=bundle.datasets,
            collector_version=bundle.collector_version,
        )

    def capture_once(self, now: datetime | None = None) -> CaptureResult:
        requested_at = now or datetime.now(SHANGHAI)
        requested_id = capture_id_for_minute(requested_at)
        existing_result = self._existing_result(
            requested_id,
            self.catalog.get_capture(requested_id, include_incomplete=True),
        )
        if existing_result is not None:
            return existing_result

        begun = False
        capture_id = requested_id
        try:
            if self._preflight_default_writer:
                # This must precede TradexLiveSource.capture(): that call may
                # reach several live providers and must not run when the lake
                # runtime cannot publish its result.
                self.parquet_writer.preflight()
            live = self.source.capture(requested_id)
            actual_id = capture_id_for_minute(
                live.bundle.minute_bucket,
                live.bundle.collector_version,
            )
            capture_id = actual_id
            bundle = self._reidentify(live.bundle, actual_id)
            existing_result = self._existing_result(
                actual_id,
                self.catalog.get_capture(actual_id, include_incomplete=True),
            )
            if existing_result is not None:
                return existing_result

            started = self.catalog.begin_capture(bundle)
            begun = True
            artifacts = []
            for name in sorted(bundle.datasets):
                dataset = bundle.datasets[name]
                object_ref = self.object_store.put_json(
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
                artifacts.append(
                    self.parquet_writer.write_capture(dataset, bundle, object_ref)
                )

            feature_payload = dict(live.feature_payload)
            feature_payload["_lake_replay"] = replay_marker(bundle)
            feature = FeatureSnapshot(
                feature_id=_feature_id(bundle.capture_id),
                capture_id=bundle.capture_id,
                name="risk_appetite",
                schema_version=FEATURE_SCHEMA_VERSION,
                config_version=str(live.feature_payload.get("version") or "unknown"),
                payload=feature_payload,
                input_artifact_ids=tuple(sorted(item.artifact_id for item in artifacts)),
                code_sha=self.code_sha,
                created_at=bundle.observed_at,
            )
            decision = self.policy.decide(feature, effective_at=bundle.observed_at)
            current_bundle_sha = sha256_hex(bundle)
            if current_bundle_sha != started.get("bundle_sha256"):
                raise RuntimeError(
                    "CaptureBundle changed after begin_capture; refusing to publish "
                    "artifacts derived from drifting input"
                )
            self.catalog.commit_capture(
                bundle.capture_id,
                artifacts,
                features=(feature,),
                decision=decision,
            )
            return CaptureResult(
                capture_id=bundle.capture_id,
                status="completed",
                artifact_count=len(artifacts),
                feature_id=feature.feature_id,
                decision_id=decision.decision_id,
            )
        except Exception as exc:
            if begun:
                try:
                    self.catalog.fail_capture(capture_id, str(exc) or type(exc).__name__)
                except Exception:
                    pass
            return CaptureResult(
                capture_id=capture_id,
                status="failed",
                artifact_count=0,
                error=f"{type(exc).__name__}: {exc}",
            )

    def run(self, stop_event: threading.Event, interval_seconds: float = 60.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        consecutive_failures = 0
        while not stop_event.is_set():
            now = datetime.now(SHANGHAI)
            minute = now.hour * 60 + now.minute
            in_session = now.weekday() < 5 and (
                9 * 60 + 30 <= minute <= 11 * 60 + 30
                or 13 * 60 <= minute <= 15 * 60
            )
            if in_session:
                result = self.capture_once(now)
                is_failure = result.status in {"failed", "already_failed"}
                consecutive_failures = consecutive_failures + 1 if is_failure else 0
                log_level = logging.ERROR if is_failure else logging.INFO
                logger.log(
                    log_level,
                    "lake capture status=%s capture_id=%s artifacts=%d "
                    "consecutive_failures=%d error=%s",
                    result.status,
                    result.capture_id,
                    result.artifact_count,
                    consecutive_failures,
                    result.error or "-",
                )
            stop_event.wait(interval_seconds)


__all__ = [
    "CapturePipeline",
    "CaptureResult",
    "FEATURE_SCHEMA_VERSION",
    "capture_id_for_minute",
]
