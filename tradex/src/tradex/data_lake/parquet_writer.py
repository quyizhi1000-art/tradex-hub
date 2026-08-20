"""Atomic, idempotent Parquet publication for raw Tradex captures."""

from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from .contracts import ArtifactRef, CaptureBundle, CapturedDataset
from .object_store import ObjectRef
from .serde import canonical_json, sha256_hex, to_jsonable


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")

_COMMON_COLUMNS = (
    "capture_id",
    "observed_at",
    "minute_bucket",
    "provider_as_of",
    "source",
    "record_index",
    "schema_version",
    "payload_json",
    "payload_sha256",
)
_BOARD_COLUMNS = (
    "taxonomy",
    "board_code",
    "name",
    "change_pct",
    "up_count",
    "down_count",
    "flow_amount",
    "flow_ratio",
    "flow_rank",
)
_PUBLISHED_COLUMNS = _COMMON_COLUMNS + _BOARD_COLUMNS

_TAXONOMY_FIELDS = ("taxonomy", "板块类型", "分类")
_CODE_FIELDS = ("board_code", "板块代码", "代码", "code")
_NAME_FIELDS = ("name", "板块名称", "板块", "名称", "行业名称")
_CHANGE_FIELDS = ("change_pct", "涨跌幅", "涨跌幅(%)")
_UP_FIELDS = ("up_count", "上涨家数", "上涨股数", "上涨")
_DOWN_FIELDS = ("down_count", "下跌家数", "下跌股数", "下跌")
_FLOW_AMOUNT_FIELDS = ("flow_amount", "main_net_amount", "主力净流入", "主力净流入额")
_FLOW_RATIO_FIELDS = (
    "flow_ratio",
    "main_net_ratio",
    "主力净流入-占比",
    "主力净流入占比",
    "主力净流入比例",
)
_FLOW_RANK_FIELDS = ("flow_rank", "main_net_rank", "主力净流入排名")


class ParquetPublicationError(RuntimeError):
    """Base error for an invalid or incomplete Parquet publication."""


class ParquetConflictError(ParquetPublicationError):
    """Raised when an immutable capture path already holds different content."""


def _safe_component(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value or not _SAFE_COMPONENT.fullmatch(value):
        raise ValueError(
            f"{field_name} must be one safe path component containing only "
            "letters, numbers, dot, underscore, or hyphen"
        )
    if value in {".", ".."}:
        raise ValueError(f"{field_name} must not be a path traversal component")
    return value


def _first(record: Mapping[str, Any], fields: tuple[str, ...]) -> Any:
    for field in fields:
        value = record.get(field)
        if value is not None and value != "":
            return value
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = to_jsonable(value)
    except TypeError:
        normalized = value
    if isinstance(normalized, str):
        normalized = normalized.strip().replace(",", "")
        if normalized.endswith("%"):
            normalized = normalized[:-1]
        if normalized in {"", "-", "--", "None", "null"}:
            return None
    try:
        number = float(normalized)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _taxonomy(record: Mapping[str, Any], dataset_name: str) -> str | None:
    explicit = _text(_first(record, _TAXONOMY_FIELDS))
    if explicit:
        lowered = explicit.lower()
        if lowered in {"industry", "concept"}:
            return lowered
        return explicit
    lowered_name = dataset_name.lower()
    if "concept" in lowered_name or "概念" in dataset_name:
        return "concept"
    if "industry" in lowered_name or "行业" in dataset_name:
        return "industry"
    return None


def _board_values(record: Mapping[str, Any], dataset_name: str) -> dict[str, Any]:
    return {
        "taxonomy": _taxonomy(record, dataset_name),
        "board_code": _text(_first(record, _CODE_FIELDS)),
        "name": _text(_first(record, _NAME_FIELDS)),
        "change_pct": _number(_first(record, _CHANGE_FIELDS)),
        "up_count": _integer(_first(record, _UP_FIELDS)),
        "down_count": _integer(_first(record, _DOWN_FIELDS)),
        "flow_amount": _number(_first(record, _FLOW_AMOUNT_FIELDS)),
        "flow_ratio": _number(_first(record, _FLOW_RATIO_FIELDS)),
        "flow_rank": _integer(_first(record, _FLOW_RANK_FIELDS)),
    }


def _load_dependencies():
    try:
        import duckdb
    except (ImportError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            "DuckDB is required to publish Tradex Parquet artifacts. Install the "
            "lake dependencies in this environment with `pip install -e .[lake]`."
        ) from exc
    try:
        import pandas as pd
    except (ImportError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            "pandas is required to publish Tradex Parquet artifacts. Reinstall "
            "Tradex in this Python environment."
        ) from exc
    return duckdb, pd


def preflight_dependencies() -> dict[str, str | bool]:
    """Verify the optional lake runtime can write and read real Parquet.

    Importing DuckDB alone does not prove that its Parquet path is usable. The
    small temporary round trip catches broken native wheels or extension
    loading before a live market request is made.
    """

    duckdb, pd = _load_dependencies()
    connection = None
    try:
        with tempfile.TemporaryDirectory(prefix="tradex-parquet-preflight-") as temp_dir:
            target = Path(temp_dir) / "preflight.parquet"
            relation_name = "_tradex_parquet_preflight"
            frame = pd.DataFrame({"preflight_value": [1]})
            connection = duckdb.connect(database=":memory:")
            connection.register(relation_name, frame)
            sql_path = target.as_posix().replace("'", "''")
            try:
                connection.execute(
                    f"COPY {relation_name} TO '{sql_path}' "
                    "(FORMAT PARQUET, COMPRESSION ZSTD)"
                )
            finally:
                unregister = getattr(connection, "unregister", None)
                if callable(unregister):
                    unregister(relation_name)
            row = connection.execute(
                "SELECT preflight_value FROM read_parquet(?)",
                [str(target)],
            ).fetchone()
            if row != (1,):
                raise RuntimeError(f"unexpected Parquet round-trip result: {row!r}")
    except Exception as exc:
        raise RuntimeError(
            "DuckDB Parquet preflight failed. Reinstall the lake dependencies "
            "with `pip install -e .[lake]` before starting live capture."
        ) from exc
    finally:
        if connection is not None:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
    return {
        "duckdb": str(getattr(duckdb, "__version__", "unknown")),
        "pandas": str(getattr(pd, "__version__", "unknown")),
        "parquet_roundtrip": True,
    }


def _build_frame(pd, dataset: CapturedDataset, bundle: CaptureBundle):
    payload_json = [canonical_json(record) for record in dataset.records]
    payload_sha256 = [sha256_hex(record) for record in dataset.records]
    board_rows = [_board_values(record, dataset.name) for record in dataset.records]
    row_count = len(dataset.records)

    frame = pd.DataFrame(index=range(row_count))
    frame["capture_id"] = pd.Series([bundle.capture_id] * row_count, dtype="string")
    frame["observed_at"] = pd.Series(
        pd.array(
            [bundle.observed_at] * row_count,
            dtype="datetime64[ns, Asia/Shanghai]",
        )
    )
    frame["minute_bucket"] = pd.Series(
        pd.array(
            [bundle.minute_bucket] * row_count,
            dtype="datetime64[ns, Asia/Shanghai]",
        )
    )
    frame["provider_as_of"] = pd.Series(
        [dataset.provider_as_of] * row_count, dtype="string"
    )
    frame["source"] = pd.Series([dataset.source] * row_count, dtype="string")
    frame["record_index"] = pd.Series(range(row_count), dtype="int64")
    frame["schema_version"] = pd.Series(
        [dataset.schema_version] * row_count, dtype="string"
    )
    frame["payload_json"] = pd.Series(payload_json, dtype="string")
    frame["payload_sha256"] = pd.Series(payload_sha256, dtype="string")

    frame["taxonomy"] = pd.Series(
        [row["taxonomy"] for row in board_rows], dtype="string"
    )
    frame["board_code"] = pd.Series(
        [row["board_code"] for row in board_rows], dtype="string"
    )
    frame["name"] = pd.Series([row["name"] for row in board_rows], dtype="string")
    for column in ("change_pct", "flow_amount", "flow_ratio"):
        frame[column] = pd.Series([row[column] for row in board_rows], dtype="Float64")
    for column in ("up_count", "down_count", "flow_rank"):
        frame[column] = pd.Series([row[column] for row in board_rows], dtype="Int64")
    return frame.loc[:, list(_PUBLISHED_COLUMNS)]


def _write_parquet(connection, frame, temp_path: Path) -> None:
    relation_name = "_tradex_capture_rows"
    connection.register(relation_name, frame)
    sql_path = temp_path.as_posix().replace("'", "''")
    try:
        connection.execute(
            f"COPY {relation_name} TO '{sql_path}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        unregister = getattr(connection, "unregister", None)
        if callable(unregister):
            unregister(relation_name)


def _summary_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    try:
        return to_jsonable(value)
    except TypeError:
        return str(value)


def _parquet_summary(connection, path: Path) -> tuple[int, str]:
    # DuckDB's Python conversion of TIMESTAMPTZ values imports ``pytz`` even
    # though pandas and this project use stdlib zoneinfo.  Hash the timestamp
    # columns through a stable SQL text representation so publication does not
    # acquire an otherwise unnecessary runtime dependency.
    selected = ", ".join(
        f'CAST("{column}" AS VARCHAR) AS "{column}"'
        if column in {"observed_at", "minute_bucket"}
        else f'"{column}"'
        for column in _PUBLISHED_COLUMNS
    )
    cursor = connection.execute(
        f"SELECT {selected} FROM read_parquet(?) ORDER BY record_index",
        [str(path)],
    )
    rows = [
        [_summary_value(value) for value in row]
        for row in cursor.fetchall()
    ]
    return len(rows), sha256_hex(rows)


def _fsync(path: Path) -> None:
    # Windows requires a writable descriptor for FlushFileBuffers, which is
    # what os.fsync delegates to there.  Opening in update mode does not alter
    # the bytes but keeps the durability step portable.
    with path.open("rb+") as stream:
        os.fsync(stream.fileno())


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_relative_path(relative_path: str) -> Path:
    path = Path(relative_path)
    if path.is_absolute() or path.drive or not path.parts:
        raise ValueError("object_ref.relative_path must be a non-empty relative path")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("object_ref.relative_path must not contain path traversal")
    return path


def _infer_root(object_ref: ObjectRef) -> Path:
    relative = _validate_relative_path(object_ref.relative_path)
    root = Path(object_ref.absolute_path).expanduser().resolve()
    try:
        for _ in relative.parts:
            root = root.parent
    except IndexError as exc:
        raise ValueError("object_ref paths cannot identify a lake root") from exc
    expected = (root / relative).resolve()
    actual = Path(object_ref.absolute_path).expanduser().resolve()
    if expected != actual:
        raise ValueError(
            "object_ref.absolute_path does not match object_ref.relative_path under one lake root"
        )
    return root


class ParquetWriter:
    """Publish raw captures beneath one Tradex lake root."""

    _locks_guard = threading.Lock()
    _target_locks: dict[str, threading.Lock] = {}

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).expanduser().resolve()
        self._preflight_lock = threading.Lock()
        self._preflight_result: dict[str, str | bool] | None = None

    def preflight(self) -> dict[str, str | bool]:
        """Run the optional-dependency check once for this writer instance."""

        if self._preflight_result is not None:
            return dict(self._preflight_result)
        with self._preflight_lock:
            if self._preflight_result is None:
                self._preflight_result = preflight_dependencies()
            return dict(self._preflight_result)

    @classmethod
    def _target_lock(cls, target: Path) -> threading.Lock:
        key = os.path.normcase(str(target.resolve()))
        with cls._locks_guard:
            lock = cls._target_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                cls._target_locks[key] = lock
            return lock

    def _validate_object_ref(self, object_ref: ObjectRef) -> None:
        relative = _validate_relative_path(object_ref.relative_path)
        expected = (self.root / relative).resolve()
        actual = Path(object_ref.absolute_path).expanduser().resolve()
        if expected != actual:
            raise ValueError("object_ref is outside this ParquetWriter lake root")

    def _target(self, dataset: CapturedDataset, bundle: CaptureBundle) -> Path:
        dataset_name = _safe_component(dataset.name, "dataset.name")
        capture_id = _safe_component(bundle.capture_id, "bundle.capture_id")
        target = (
            self.root
            / "parquet"
            / "raw"
            / dataset_name
            / f"trade_date={bundle.trade_date.isoformat()}"
            / f"capture_id={capture_id}"
            / "part-0.parquet"
        )
        try:
            target.resolve().relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Parquet target escapes the configured lake root") from exc
        return target

    @staticmethod
    def _validate_bundle_dataset(dataset: CapturedDataset, bundle: CaptureBundle) -> None:
        registered = bundle.datasets.get(dataset.name)
        if registered is None:
            raise ValueError(f"dataset {dataset.name!r} is not present in the capture bundle")
        if registered != dataset:
            raise ValueError(
                f"dataset {dataset.name!r} does not match the capture bundle's immutable value"
            )

    @staticmethod
    def _artifact(
        dataset: CapturedDataset,
        bundle: CaptureBundle,
        object_ref: ObjectRef,
        target: Path,
        root: Path,
        parquet_sha256: str,
    ) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=f"raw:{bundle.capture_id}:{dataset.name}",
            dataset=dataset.name,
            layer="raw",
            object_sha256=object_ref.sha256,
            object_path=object_ref.relative_path,
            parquet_path=target.relative_to(root).as_posix(),
            parquet_sha256=parquet_sha256,
            row_count=len(dataset.records),
            schema_version=dataset.schema_version,
        )

    def write_capture(
        self,
        dataset: CapturedDataset,
        bundle: CaptureBundle,
        object_ref: ObjectRef,
    ) -> ArtifactRef:
        """Atomically publish one dataset capture, or verify an idempotent retry."""

        _safe_component(dataset.name, "dataset.name")
        _safe_component(bundle.capture_id, "bundle.capture_id")
        self._validate_bundle_dataset(dataset, bundle)
        self._validate_object_ref(object_ref)
        duckdb, pd = _load_dependencies()

        target = self._target(dataset, bundle)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".part-0.", suffix=".tmp", dir=target.parent
        )
        os.close(descriptor)
        temp_path = Path(temp_name)
        # DuckDB COPY expects to create its destination rather than inherit an
        # already-open mkstemp file. The unique name remains in the target dir.
        temp_path.unlink()

        connection = duckdb.connect(database=":memory:")
        try:
            frame = _build_frame(pd, dataset, bundle)
            _write_parquet(connection, frame, temp_path)
            _fsync(temp_path)
            temp_file_hash = _file_sha256(temp_path)
            temp_count, temp_hash = _parquet_summary(connection, temp_path)
            if temp_count != len(dataset.records):
                raise ParquetPublicationError(
                    f"temporary Parquet row count mismatch: expected {len(dataset.records)}, "
                    f"got {temp_count}"
                )

            with self._target_lock(target):
                if target.exists():
                    try:
                        existing_file_hash = _file_sha256(target)
                        existing_count, existing_hash = _parquet_summary(connection, target)
                    except Exception as exc:  # corrupt/partial Parquet is an immutable conflict
                        raise ParquetConflictError(
                            f"existing Parquet artifact cannot be verified: {target}"
                        ) from exc
                    if (
                        (existing_count, existing_hash) != (temp_count, temp_hash)
                        or existing_file_hash != temp_file_hash
                    ):
                        raise ParquetConflictError(
                            f"capture path already contains different content: {target} "
                            f"(existing rows/content/file hash={existing_count}/"
                            f"{existing_hash}/{existing_file_hash}, new={temp_count}/"
                            f"{temp_hash}/{temp_file_hash})"
                        )
                    return self._artifact(
                        dataset,
                        bundle,
                        object_ref,
                        target,
                        self.root,
                        existing_file_hash,
                    )

                os.replace(temp_path, target)
                published_file_hash = _file_sha256(target)
                published_count, published_hash = _parquet_summary(connection, target)
                if (
                    (published_count, published_hash) != (temp_count, temp_hash)
                    or published_file_hash != temp_file_hash
                ):
                    raise ParquetPublicationError(
                        f"published Parquet verification failed for {target}"
                    )
                return self._artifact(
                    dataset,
                    bundle,
                    object_ref,
                    target,
                    self.root,
                    published_file_hash,
                )
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
            if temp_path.exists():
                temp_path.unlink()


def write_capture(
    dataset: CapturedDataset,
    bundle: CaptureBundle,
    object_ref: ObjectRef,
) -> ArtifactRef:
    """Publish using the lake root carried by *object_ref*."""

    return ParquetWriter(_infer_root(object_ref)).write_capture(dataset, bundle, object_ref)


__all__ = [
    "ParquetConflictError",
    "ParquetPublicationError",
    "ParquetWriter",
    "preflight_dependencies",
    "write_capture",
]
