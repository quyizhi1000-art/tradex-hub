"""SQLite control plane for atomically published Tradex lake captures."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import (
    SHANGHAI,
    ArtifactRef,
    CaptureBundle,
    DecisionDraft,
    FeatureSnapshot,
)
from .serde import canonical_json, sha256_hex


CATALOG_SCHEMA_VERSION = 1


class CatalogConflictError(RuntimeError):
    """Raised when an immutable capture identifier is reused inconsistently."""


class CaptureNotFoundError(KeyError):
    """Raised when an operation references an unknown capture identifier."""


class CatalogSchemaError(RuntimeError):
    """Raised when a catalog does not implement the current durable schema."""


class CatalogReadOnlyError(RuntimeError):
    """Raised when a publication operation is attempted through a reader."""


def _now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="microseconds")


def _json_load(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


class Catalog:
    """Transactional publication catalog backed by SQLite WAL.

    Object and Parquet files are written before :meth:`commit_capture`.
    Readers only see runs whose control-plane transaction reached
    ``completed``, so orphaned immutable files never look published.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        read_only: bool = False,
    ) -> None:
        configured = str(db_path)
        self.read_only = bool(read_only)
        if self.read_only and configured == ":memory:":
            raise ValueError("read_only catalogs require an existing filesystem database")
        self.db_path = configured
        if configured != ":memory:":
            path = Path(configured).expanduser().resolve()
            if self.read_only:
                if not path.is_file():
                    raise FileNotFoundError(f"catalog database does not exist: {path}")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(path)

        self._lock = threading.RLock()
        connection_target = self.db_path
        connection_kwargs: dict[str, Any] = {}
        if self.read_only:
            connection_target = f"{Path(self.db_path).as_uri()}?mode=ro"
            wal_path = Path(f"{self.db_path}-wal")
            shm_path = Path(f"{self.db_path}-shm")
            # A checkpointed WAL database with no sidecars is a stable
            # immutable snapshot.  Marking that case immutable prevents
            # SQLite on Windows from creating empty -wal/-shm files merely to
            # read the persisted WAL-mode header.  When a writer already owns
            # sidecars, retain ordinary mode=ro so committed WAL pages remain
            # visible to concurrent readers.
            if not wal_path.exists() and not shm_path.exists():
                connection_target += "&immutable=1"
            connection_kwargs["uri"] = True
        self._connection = sqlite3.connect(
            connection_target,
            check_same_thread=False,
            timeout=5,
            isolation_level=None,
            **connection_kwargs,
        )
        self._connection.row_factory = sqlite3.Row
        if self.read_only:
            try:
                self._validate_schema()
            except Exception:
                self._connection.close()
                raise
        else:
            try:
                self._connection.execute("PRAGMA foreign_keys = ON")
                self._connection.execute("PRAGMA busy_timeout = 5000")
                self._connection.execute("PRAGMA synchronous = NORMAL")
                self._initialize()
                if self.db_path != ":memory:":
                    self._connection.execute("PRAGMA journal_mode = WAL")
            except Exception:
                self._connection.close()
                raise

    def _schema_version(self) -> int:
        return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    def _validate_schema(self) -> None:
        version = self._schema_version()
        if version != CATALOG_SCHEMA_VERSION:
            raise CatalogSchemaError(
                "incompatible Tradex catalog schema: "
                f"user_version={version}, expected={CATALOG_SCHEMA_VERSION}; "
                "use a new lake root or an explicit catalog migration"
            )
        required_columns = {
            "capture_runs": {"bundle_sha256", "dataset_names_json", "status"},
            "artifacts": {"object_sha256", "parquet_path", "parquet_sha256"},
            "features": {"input_artifact_ids_json"},
            "decisions": {"mode"},
            "outcome_labels": {"source_generation"},
        }
        for table, required in required_columns.items():
            columns = {
                str(row[1])
                for row in self._connection.execute(
                    f'PRAGMA table_info("{table}")'
                ).fetchall()
            }
            missing = sorted(required - columns)
            if missing:
                raise CatalogSchemaError(
                    f"catalog user_version={version} is missing {table} columns: {missing!r}"
                )

    def _initialize(self) -> None:
        with self._lock:
            version = self._schema_version()
            if version not in {0, CATALOG_SCHEMA_VERSION}:
                raise CatalogSchemaError(
                    "incompatible Tradex catalog schema: "
                    f"user_version={version}, expected={CATALOG_SCHEMA_VERSION}; "
                    "use a new lake root or an explicit catalog migration"
                )
            existing_tables = {
                str(row[0])
                for row in self._connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            if version == 0 and existing_tables:
                raise CatalogSchemaError(
                    "incompatible unversioned Tradex catalog; use a new lake root "
                    "or an explicit catalog migration"
                )
            if version == CATALOG_SCHEMA_VERSION:
                self._validate_schema()
                return
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS capture_runs (
                    capture_id TEXT PRIMARY KEY,
                    bundle_sha256 TEXT NOT NULL,
                    publication_sha256 TEXT,
                    observed_at TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    minute_bucket TEXT NOT NULL,
                    market_phase TEXT NOT NULL,
                    collector_version TEXT NOT NULL,
                    market_data_json TEXT NOT NULL,
                    dataset_names_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('started', 'completed', 'failed')),
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    failed_at TEXT,
                    error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_capture_runs_completed
                    ON capture_runs (status, minute_bucket DESC, capture_id DESC);

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    capture_id TEXT NOT NULL REFERENCES capture_runs(capture_id),
                    dataset TEXT NOT NULL,
                    layer TEXT NOT NULL,
                    object_sha256 TEXT NOT NULL,
                    object_path TEXT NOT NULL,
                    parquet_path TEXT,
                    parquet_sha256 TEXT,
                    row_count INTEGER NOT NULL CHECK (row_count >= 0),
                    schema_version TEXT NOT NULL,
                    CHECK (
                        (parquet_path IS NULL AND parquet_sha256 IS NULL)
                        OR (parquet_path IS NOT NULL AND parquet_sha256 IS NOT NULL)
                    )
                );

                CREATE INDEX IF NOT EXISTS idx_artifacts_capture
                    ON artifacts (capture_id, dataset, artifact_id);

                CREATE TABLE IF NOT EXISTS features (
                    feature_id TEXT PRIMARY KEY,
                    capture_id TEXT NOT NULL REFERENCES capture_runs(capture_id),
                    name TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    config_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    input_artifact_ids_json TEXT NOT NULL,
                    code_sha TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_features_capture
                    ON features (capture_id, name, feature_id);

                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    capture_id TEXT NOT NULL REFERENCES capture_runs(capture_id),
                    feature_id TEXT NOT NULL REFERENCES features(feature_id),
                    policy_version TEXT NOT NULL,
                    effective_at TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    offense_weight REAL NOT NULL,
                    defense_weight REAL NOT NULL,
                    cash_weight REAL NOT NULL,
                    contributions_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    abstain_reason TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_decisions_capture
                    ON decisions (capture_id, effective_at, decision_id);

                CREATE TABLE IF NOT EXISTS outcome_labels (
                    label_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
                    label_name TEXT NOT NULL,
                    horizon_sessions INTEGER NOT NULL CHECK (horizon_sessions > 0),
                    instrument TEXT,
                    benchmark TEXT,
                    entry_at TEXT,
                    entry_price REAL,
                    exit_at TEXT,
                    exit_price REAL,
                    gross_return REAL,
                    excess_return REAL,
                    label_version TEXT NOT NULL,
                    source_generation TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    computed_at TEXT NOT NULL,
                    UNIQUE (decision_id, label_name, label_version)
                );

                CREATE INDEX IF NOT EXISTS idx_outcome_labels_decision
                    ON outcome_labels (decision_id, label_name, label_version);
                """
            )
            self._connection.execute(
                f"PRAGMA user_version = {CATALOG_SCHEMA_VERSION}"
            )
            self._validate_schema()

    def _require_writable(self) -> None:
        if self.read_only:
            raise CatalogReadOnlyError("catalog was opened with read_only=True")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @staticmethod
    def _bundle_hash(bundle: CaptureBundle) -> str:
        return sha256_hex(bundle)

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

    def begin_capture(self, bundle: CaptureBundle) -> dict[str, Any]:
        """Begin or idempotently resume one immutable capture identifier."""

        self._require_writable()
        if not isinstance(bundle, CaptureBundle):
            raise TypeError("bundle must be a CaptureBundle")
        bundle_hash = self._bundle_hash(bundle)
        now = _now_iso()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT * FROM capture_runs WHERE capture_id = ?",
                    (bundle.capture_id,),
                ).fetchone()
                if existing is None:
                    self._connection.execute(
                        """
                        INSERT INTO capture_runs (
                            capture_id, bundle_sha256, observed_at, trade_date,
                            minute_bucket, market_phase, collector_version,
                            market_data_json, dataset_names_json, status, started_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'started', ?)
                        """,
                        (
                            bundle.capture_id,
                            bundle_hash,
                            bundle.observed_at.isoformat(),
                            bundle.trade_date.isoformat(),
                            bundle.minute_bucket.isoformat(),
                            bundle.market_phase,
                            bundle.collector_version,
                            canonical_json(bundle.market_data),
                            canonical_json(sorted(bundle.datasets)),
                            now,
                        ),
                    )
                else:
                    if existing["bundle_sha256"] != bundle_hash:
                        raise CatalogConflictError(
                            f"capture_id {bundle.capture_id!r} already belongs to a different bundle"
                        )
                    if existing["status"] == "failed":
                        self._connection.execute(
                            """
                            UPDATE capture_runs
                            SET status = 'started', started_at = ?, failed_at = NULL,
                                error = NULL
                            WHERE capture_id = ?
                            """,
                            (now, bundle.capture_id),
                        )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            record = self._get_capture_locked(bundle.capture_id, include_incomplete=True)
            assert record is not None
            return record

    @staticmethod
    def _validate_publication(
        capture_id: str,
        artifacts: tuple[ArtifactRef, ...],
        features: tuple[FeatureSnapshot, ...],
        decision: DecisionDraft | None,
    ) -> None:
        for artifact in artifacts:
            if not isinstance(artifact, ArtifactRef):
                raise TypeError("artifacts must contain ArtifactRef values")
        for feature in features:
            if not isinstance(feature, FeatureSnapshot):
                raise TypeError("features must contain FeatureSnapshot values")
        artifact_ids = [artifact.artifact_id for artifact in artifacts]
        feature_ids = [feature.feature_id for feature in features]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("artifact_id values must be unique within a commit")
        if len(set(feature_ids)) != len(feature_ids):
            raise ValueError("feature_id values must be unique within a commit")

        published_artifacts = set(artifact_ids)
        for feature in features:
            if feature.capture_id != capture_id:
                raise ValueError(
                    f"feature {feature.feature_id!r} belongs to capture {feature.capture_id!r}"
                )
            unknown_inputs = set(feature.input_artifact_ids) - published_artifacts
            if unknown_inputs:
                raise ValueError(
                    f"feature {feature.feature_id!r} references unpublished artifacts: "
                    f"{sorted(unknown_inputs)!r}"
                )
        if decision is not None:
            if not isinstance(decision, DecisionDraft):
                raise TypeError("decision must be a DecisionDraft")
            if decision.feature_id not in set(feature_ids):
                raise ValueError(
                    f"decision {decision.decision_id!r} references an unpublished feature"
                )
            if decision.mode != "shadow":
                raise ValueError("the initial lake publication phase only accepts shadow decisions")

    def commit_capture(
        self,
        capture_id: str,
        artifacts: Iterable[ArtifactRef],
        features: Iterable[FeatureSnapshot] = (),
        decision: DecisionDraft | None = None,
    ) -> dict[str, Any]:
        """Atomically publish artifacts, features, an optional decision, and run status."""

        self._require_writable()
        artifact_values = tuple(artifacts)
        feature_values = tuple(features)
        self._validate_publication(capture_id, artifact_values, feature_values, decision)
        publication_hash = self._publication_hash(
            artifact_values,
            feature_values,
            decision,
        )

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._connection.execute(
                    "SELECT * FROM capture_runs WHERE capture_id = ?",
                    (capture_id,),
                ).fetchone()
                if run is None:
                    raise CaptureNotFoundError(capture_id)
                expected_datasets = _json_load(run["dataset_names_json"], [])
                if (
                    not isinstance(expected_datasets, list)
                    or any(
                        not isinstance(name, str) or not name
                        for name in expected_datasets
                    )
                    or len(set(expected_datasets)) != len(expected_datasets)
                ):
                    raise CatalogConflictError(
                        f"capture {capture_id!r} contains invalid dataset_names_json"
                    )
                artifact_datasets = [artifact.dataset for artifact in artifact_values]
                duplicate_datasets = sorted(
                    {
                        name
                        for name in artifact_datasets
                        if artifact_datasets.count(name) > 1
                    }
                )
                missing_datasets = sorted(set(expected_datasets) - set(artifact_datasets))
                extra_datasets = sorted(set(artifact_datasets) - set(expected_datasets))
                if duplicate_datasets or missing_datasets or extra_datasets:
                    raise ValueError(
                        "capture artifact datasets must match begin_capture exactly once "
                        f"(missing={missing_datasets!r}, extra={extra_datasets!r}, "
                        f"duplicates={duplicate_datasets!r})"
                    )
                if run["status"] == "completed":
                    if run["publication_sha256"] != publication_hash:
                        raise CatalogConflictError(
                            f"completed capture {capture_id!r} cannot be republished differently"
                        )
                    self._connection.execute("COMMIT")
                    record = self._get_capture_locked(capture_id, include_incomplete=False)
                    assert record is not None
                    return record
                if run["status"] != "started":
                    raise CatalogConflictError(
                        f"capture {capture_id!r} must be begun again before commit"
                    )

                for artifact in artifact_values:
                    self._connection.execute(
                        """
                        INSERT INTO artifacts (
                            artifact_id, capture_id, dataset, layer,
                            object_sha256, object_path, parquet_path,
                            parquet_sha256, row_count, schema_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            artifact.artifact_id,
                            capture_id,
                            artifact.dataset,
                            artifact.layer,
                            artifact.object_sha256,
                            artifact.object_path,
                            artifact.parquet_path,
                            artifact.parquet_sha256,
                            artifact.row_count,
                            artifact.schema_version,
                        ),
                    )

                for feature in feature_values:
                    self._connection.execute(
                        """
                        INSERT INTO features (
                            feature_id, capture_id, name, schema_version,
                            config_version, payload_json,
                            input_artifact_ids_json, code_sha, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            feature.feature_id,
                            capture_id,
                            feature.name,
                            feature.schema_version,
                            feature.config_version,
                            canonical_json(feature.payload),
                            canonical_json(feature.input_artifact_ids),
                            feature.code_sha,
                            feature.created_at.isoformat(),
                        ),
                    )

                if decision is not None:
                    self._connection.execute(
                        """
                        INSERT INTO decisions (
                            decision_id, capture_id, feature_id, policy_version,
                            effective_at, mode, offense_weight, defense_weight,
                            cash_weight, contributions_json, confidence,
                            abstain_reason
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            decision.decision_id,
                            capture_id,
                            decision.feature_id,
                            decision.policy_version,
                            decision.effective_at.isoformat(),
                            decision.mode,
                            decision.offense_weight,
                            decision.defense_weight,
                            decision.cash_weight,
                            canonical_json(decision.contributions),
                            decision.confidence,
                            decision.abstain_reason,
                        ),
                    )

                self._connection.execute(
                    """
                    UPDATE capture_runs
                    SET status = 'completed', publication_sha256 = ?,
                        completed_at = ?, failed_at = NULL, error = NULL
                    WHERE capture_id = ?
                    """,
                    (publication_hash, _now_iso(), capture_id),
                )
                self._connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._connection.execute("ROLLBACK")
                raise CatalogConflictError(f"capture publication conflicts with catalog: {exc}") from exc
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

            record = self._get_capture_locked(capture_id, include_incomplete=False)
            assert record is not None
            return record

    def fail_capture(self, capture_id: str, error: str) -> dict[str, Any]:
        """Mark an unfinished capture failed without ever downgrading completed data."""

        self._require_writable()
        if not isinstance(error, str) or not error.strip():
            raise ValueError("error must be a non-empty string")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._connection.execute(
                    "SELECT status FROM capture_runs WHERE capture_id = ?",
                    (capture_id,),
                ).fetchone()
                if run is None:
                    raise CaptureNotFoundError(capture_id)
                if run["status"] != "completed":
                    self._connection.execute(
                        """
                        UPDATE capture_runs
                        SET status = 'failed', failed_at = ?, error = ?
                        WHERE capture_id = ?
                        """,
                        (_now_iso(), error, capture_id),
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            record = self._get_capture_locked(capture_id, include_incomplete=True)
            assert record is not None
            return record

    @staticmethod
    def _artifact_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "artifact_id": row["artifact_id"],
            "dataset": row["dataset"],
            "layer": row["layer"],
            "object_sha256": row["object_sha256"],
            "object_path": row["object_path"],
            "parquet_path": row["parquet_path"],
            "parquet_sha256": row["parquet_sha256"],
            "row_count": row["row_count"],
            "schema_version": row["schema_version"],
        }

    @staticmethod
    def _feature_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "feature_id": row["feature_id"],
            "capture_id": row["capture_id"],
            "name": row["name"],
            "schema_version": row["schema_version"],
            "config_version": row["config_version"],
            "payload": _json_load(row["payload_json"], {}),
            "input_artifact_ids": _json_load(row["input_artifact_ids_json"], []),
            "code_sha": row["code_sha"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _decision_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "decision_id": row["decision_id"],
            "capture_id": row["capture_id"],
            "feature_id": row["feature_id"],
            "policy_version": row["policy_version"],
            "effective_at": row["effective_at"],
            "mode": row["mode"],
            "offense_weight": row["offense_weight"],
            "defense_weight": row["defense_weight"],
            "cash_weight": row["cash_weight"],
            "contributions": _json_load(row["contributions_json"], {}),
            "confidence": row["confidence"],
            "abstain_reason": row["abstain_reason"],
        }

    @staticmethod
    def _label_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            key: row[key]
            for key in row.keys()
            if key != "payload_json"
        } | {"payload": _json_load(row["payload_json"], {})}

    def _get_capture_locked(
        self,
        capture_id: str,
        *,
        include_incomplete: bool,
    ) -> dict[str, Any] | None:
        sql = "SELECT * FROM capture_runs WHERE capture_id = ?"
        parameters: tuple[Any, ...] = (capture_id,)
        if not include_incomplete:
            sql += " AND status = 'completed'"
        row = self._connection.execute(sql, parameters).fetchone()
        if row is None:
            return None

        artifacts = self._connection.execute(
            "SELECT * FROM artifacts WHERE capture_id = ? ORDER BY dataset, artifact_id",
            (capture_id,),
        ).fetchall()
        features = self._connection.execute(
            "SELECT * FROM features WHERE capture_id = ? ORDER BY name, feature_id",
            (capture_id,),
        ).fetchall()
        decisions = self._connection.execute(
            "SELECT * FROM decisions WHERE capture_id = ? ORDER BY effective_at, decision_id",
            (capture_id,),
        ).fetchall()
        labels = self._connection.execute(
            """
            SELECT outcome_labels.* FROM outcome_labels
            JOIN decisions USING (decision_id)
            WHERE decisions.capture_id = ?
            ORDER BY outcome_labels.computed_at, outcome_labels.label_id
            """,
            (capture_id,),
        ).fetchall()
        return {
            "capture_id": row["capture_id"],
            "bundle_sha256": row["bundle_sha256"],
            "publication_sha256": row["publication_sha256"],
            "observed_at": row["observed_at"],
            "trade_date": row["trade_date"],
            "minute_bucket": row["minute_bucket"],
            "market_phase": row["market_phase"],
            "collector_version": row["collector_version"],
            "market_data": _json_load(row["market_data_json"], {}),
            "dataset_names": _json_load(row["dataset_names_json"], []),
            "status": row["status"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "failed_at": row["failed_at"],
            "error": row["error"],
            "artifacts": [self._artifact_view(item) for item in artifacts],
            "features": [self._feature_view(item) for item in features],
            "decisions": [self._decision_view(item) for item in decisions],
            "outcome_labels": [self._label_view(item) for item in labels],
        }

    def get_capture(
        self,
        capture_id: str,
        *,
        include_incomplete: bool = False,
    ) -> dict[str, Any] | None:
        """Return one capture; incomplete runs are hidden by default."""

        with self._lock:
            return self._get_capture_locked(
                capture_id,
                include_incomplete=include_incomplete,
            )

    def latest_completed(self) -> dict[str, Any] | None:
        """Return the most recent atomically published capture."""

        with self._lock:
            row = self._connection.execute(
                """
                SELECT capture_id FROM capture_runs
                WHERE status = 'completed'
                ORDER BY minute_bucket DESC, completed_at DESC, capture_id DESC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            return self._get_capture_locked(
                row["capture_id"],
                include_incomplete=False,
            )

    def pending_labels(
        self,
        *,
        label_name: str = "benchmark_1d",
        label_version: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return completed shadow decisions missing the requested outcome label."""

        if not isinstance(label_name, str) or not label_name.strip():
            raise ValueError("label_name must be a non-empty string")
        if label_version is not None and (
            not isinstance(label_version, str) or not label_version.strip()
        ):
            raise ValueError("label_version must be a non-empty string when supplied")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise ValueError("limit must be a positive integer when supplied")

        version_clause = ""
        parameters: list[Any] = [label_name]
        if label_version is not None:
            version_clause = " AND labels.label_version = ?"
            parameters.append(label_version)
        sql = f"""
            SELECT decisions.*, capture_runs.trade_date, capture_runs.minute_bucket
            FROM decisions
            JOIN capture_runs USING (capture_id)
            WHERE capture_runs.status = 'completed'
              AND decisions.mode = 'shadow'
              AND NOT EXISTS (
                  SELECT 1 FROM outcome_labels AS labels
                  WHERE labels.decision_id = decisions.decision_id
                    AND labels.label_name = ?{version_clause}
              )
            ORDER BY decisions.effective_at, decisions.decision_id
        """
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)

        with self._lock:
            rows = self._connection.execute(sql, tuple(parameters)).fetchall()
            result = []
            for row in rows:
                view = self._decision_view(row)
                view["trade_date"] = row["trade_date"]
                view["minute_bucket"] = row["minute_bucket"]
                result.append(view)
            return result

    def record_outcome_label(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically record one immutable outcome label.

        ``computed_at`` is audit metadata rather than label content: a retry
        may supply a later computation timestamp and still receive the
        original row.  Every economic field, the nested payload, and the
        source generation remain immutable and conflict on revision.
        """

        self._require_writable()
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")

        def required_text(field_name: str) -> str:
            value = payload.get(field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
            return value

        def optional_text(field_name: str) -> str | None:
            value = payload.get(field_name)
            if value is None:
                return None
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be null or a non-empty string")
            return value

        def optional_number(field_name: str) -> float | None:
            value = payload.get(field_name)
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{field_name} must be a finite number or null")
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"{field_name} must be a finite number or null")
            return number

        horizon = payload.get("horizon_sessions")
        if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1:
            raise ValueError("horizon_sessions must be a positive integer")
        detail = payload.get("payload", {})
        if not isinstance(detail, Mapping):
            raise TypeError("payload['payload'] must be a mapping")

        normalized = {
            "label_id": required_text("label_id"),
            "decision_id": required_text("decision_id"),
            "label_name": required_text("label_name"),
            "horizon_sessions": horizon,
            "instrument": optional_text("instrument"),
            "benchmark": optional_text("benchmark"),
            "entry_at": optional_text("entry_at"),
            "entry_price": optional_number("entry_price"),
            "exit_at": optional_text("exit_at"),
            "exit_price": optional_number("exit_price"),
            "gross_return": optional_number("gross_return"),
            "excess_return": optional_number("excess_return"),
            "label_version": required_text("label_version"),
            "source_generation": required_text("source_generation"),
            "payload": dict(detail),
            "computed_at": required_text("computed_at"),
        }

        def immutable_content(value: Mapping[str, Any]) -> dict[str, Any]:
            return {key: item for key, item in value.items() if key != "computed_at"}

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                decision = self._connection.execute(
                    """
                    SELECT capture_runs.status
                    FROM decisions
                    JOIN capture_runs USING (capture_id)
                    WHERE decisions.decision_id = ?
                    """,
                    (normalized["decision_id"],),
                ).fetchone()
                if decision is None:
                    raise ValueError(
                        f"unknown decision_id {normalized['decision_id']!r}"
                    )
                if decision["status"] != "completed":
                    raise CatalogConflictError(
                        f"decision {normalized['decision_id']!r} is not published "
                        "by a completed capture"
                    )

                existing_rows = self._connection.execute(
                    """
                    SELECT * FROM outcome_labels
                    WHERE label_id = ?
                       OR (decision_id = ? AND label_name = ? AND label_version = ?)
                    ORDER BY label_id
                    """,
                    (
                        normalized["label_id"],
                        normalized["decision_id"],
                        normalized["label_name"],
                        normalized["label_version"],
                    ),
                ).fetchall()
                if existing_rows:
                    for row in existing_rows:
                        existing = self._label_view(row)
                        if sha256_hex(immutable_content(existing)) == sha256_hex(
                            immutable_content(normalized)
                        ):
                            self._connection.execute("COMMIT")
                            return existing
                    raise CatalogConflictError(
                        f"outcome label {normalized['label_id']!r} or its immutable "
                        "decision/name/version key already contains different content"
                    )

                self._connection.execute(
                    """
                    INSERT INTO outcome_labels (
                        label_id, decision_id, label_name, horizon_sessions,
                        instrument, benchmark, entry_at, entry_price,
                        exit_at, exit_price, gross_return, excess_return,
                        label_version, source_generation, payload_json, computed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized["label_id"],
                        normalized["decision_id"],
                        normalized["label_name"],
                        normalized["horizon_sessions"],
                        normalized["instrument"],
                        normalized["benchmark"],
                        normalized["entry_at"],
                        normalized["entry_price"],
                        normalized["exit_at"],
                        normalized["exit_price"],
                        normalized["gross_return"],
                        normalized["excess_return"],
                        normalized["label_version"],
                        normalized["source_generation"],
                        canonical_json(normalized["payload"]),
                        normalized["computed_at"],
                    ),
                )
                self._connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._connection.execute("ROLLBACK")
                raise CatalogConflictError(
                    f"outcome label conflicts with catalog: {exc}"
                ) from exc
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

            row = self._connection.execute(
                "SELECT * FROM outcome_labels WHERE label_id = ?",
                (normalized["label_id"],),
            ).fetchone()
            assert row is not None
            return self._label_view(row)


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "CaptureNotFoundError",
    "Catalog",
    "CatalogConflictError",
    "CatalogReadOnlyError",
    "CatalogSchemaError",
]
