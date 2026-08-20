"""Canonical JSON conversion and hashing for immutable lake artifacts."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class CanonicalJSONError(TypeError):
    """Raised when a value cannot be represented without ambiguity in JSON."""


def _numpy_scalar(value: Any) -> Any:
    """Return a Python scalar for NumPy scalar values without importing NumPy."""

    value_type = type(value)
    if value_type.__module__.split(".", 1)[0] != "numpy":
        return value
    item = getattr(value, "item", None)
    if not callable(item):
        return value
    try:
        return item()
    except (TypeError, ValueError):
        return value


def to_jsonable(value: Any) -> Any:
    """Convert supported values into a deterministic JSON-compatible tree.

    Non-finite floating-point observations are represented as ``null``.  This
    keeps hashes stable while preserving the important distinction between a
    usable numeric value and missing/unusable data.
    """

    value = _numpy_scalar(value)

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(asdict(value))
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, (str, int, float, bool)):
                raise CanonicalJSONError(
                    f"unsupported mapping key type: {type(raw_key).__name__}"
                )
            key = str(raw_key)
            if key in result:
                raise CanonicalJSONError(f"mapping keys collide after string conversion: {key!r}")
            result[key] = to_jsonable(raw_value)
        return result
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]

    raise CanonicalJSONError(f"unsupported value type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return canonical UTF-8 JSON text with stable ordering and no whitespace."""

    return json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Return canonical JSON encoded as UTF-8 bytes."""

    return canonical_json(value).encode("utf-8")


def sha256_hex(value: Any) -> str:
    """Hash a value's canonical JSON representation with SHA-256."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


__all__ = [
    "CanonicalJSONError",
    "canonical_json",
    "canonical_json_bytes",
    "sha256_hex",
    "to_jsonable",
]
