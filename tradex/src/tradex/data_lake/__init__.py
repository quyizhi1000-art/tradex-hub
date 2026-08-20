"""Durable data-lake primitives for Tradex captures.

This package intentionally keeps its core contracts and object store free of
optional analytics dependencies.  Parquet, replay, and CNEquity adapters are
built on top of these primitives.
"""

from .contracts import (
    ArtifactRef,
    CaptureBundle,
    CapturedDataset,
    DecisionDraft,
    FeatureSnapshot,
)
from .object_store import ObjectRef, ObjectStore, ObjectStoreCorruptionError
from .serde import canonical_json, canonical_json_bytes, sha256_hex, to_jsonable

__all__ = [
    "ArtifactRef",
    "CaptureBundle",
    "CapturedDataset",
    "DecisionDraft",
    "FeatureSnapshot",
    "ObjectRef",
    "ObjectStore",
    "ObjectStoreCorruptionError",
    "canonical_json",
    "canonical_json_bytes",
    "sha256_hex",
    "to_jsonable",
]
