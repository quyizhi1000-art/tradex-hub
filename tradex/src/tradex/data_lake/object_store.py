"""Atomic, content-addressed storage for canonical gzip JSON objects."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .serde import canonical_json_bytes


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ObjectStoreCorruptionError(RuntimeError):
    """Raised when a stored object no longer matches its content address."""


@dataclass(frozen=True)
class ObjectRef:
    """Reference returned after publishing one immutable JSON object."""

    kind: str
    sha256: str
    relative_path: str
    absolute_path: Path
    size_bytes: int


class ObjectStore:
    """Store canonical JSON once under ``objects/sha256/<prefix>/<hash>``."""

    _locks_guard = threading.Lock()
    _target_locks: dict[str, threading.Lock] = {}

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).expanduser().resolve()
        self.objects_root = self.root / "objects" / "sha256"

    @classmethod
    def _target_lock(cls, target: Path) -> threading.Lock:
        key = os.path.normcase(str(target.resolve()))
        with cls._locks_guard:
            lock = cls._target_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                cls._target_locks[key] = lock
            return lock

    def _path_for_digest(self, digest: str) -> Path:
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError("digest must be a lowercase SHA-256 hex string")
        return self.objects_root / digest[:2] / f"{digest}.json.gz"

    @staticmethod
    def _digest_payload(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def _verify(self, target: Path, digest: str) -> None:
        try:
            with gzip.open(target, "rb") as stream:
                payload = stream.read()
        except (OSError, EOFError) as exc:
            raise ObjectStoreCorruptionError(f"cannot read object {target}: {exc}") from exc
        actual = self._digest_payload(payload)
        if actual != digest:
            raise ObjectStoreCorruptionError(
                f"object hash mismatch for {target}: expected {digest}, got {actual}"
            )

    def _reference(self, kind: str, digest: str, target: Path) -> ObjectRef:
        return ObjectRef(
            kind=kind,
            sha256=digest,
            relative_path=target.relative_to(self.root).as_posix(),
            absolute_path=target,
            size_bytes=target.stat().st_size,
        )

    def put_json(self, kind: str, payload: Any) -> ObjectRef:
        """Publish canonical JSON and return its stable content address.

        Gzip headers use ``mtime=0`` so all writers produce identical bytes.
        The temporary file is created beside the destination before
        ``os.replace``; therefore publication cannot cross filesystem volumes.
        """

        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("kind must be a non-empty string")
        encoded = canonical_json_bytes(payload)
        digest = self._digest_payload(encoded)
        target = self._path_for_digest(digest)
        target.parent.mkdir(parents=True, exist_ok=True)

        with self._target_lock(target):
            if target.exists():
                self._verify(target, digest)
                return self._reference(kind, digest, target)

            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{digest}.",
                suffix=".tmp",
                dir=target.parent,
            )
            temp_path = Path(temp_name)
            try:
                with os.fdopen(descriptor, "wb") as raw_stream:
                    with gzip.GzipFile(
                        filename="",
                        mode="wb",
                        compresslevel=6,
                        fileobj=raw_stream,
                        mtime=0,
                    ) as gzip_stream:
                        gzip_stream.write(encoded)
                    raw_stream.flush()
                    os.fsync(raw_stream.fileno())
                os.replace(temp_path, target)
            finally:
                if temp_path.exists():
                    temp_path.unlink()

            self._verify(target, digest)
            return self._reference(kind, digest, target)

    def get_json(self, reference: ObjectRef | str) -> Any:
        """Read, verify, and decode an object reference or digest."""

        digest = reference.sha256 if isinstance(reference, ObjectRef) else reference
        target = self._path_for_digest(digest)
        if not target.is_file():
            raise FileNotFoundError(target)
        self._verify(target, digest)
        with gzip.open(target, "rt", encoding="utf-8") as stream:
            return json.load(stream)

    def contains(self, digest: str) -> bool:
        """Return whether a digest exists and still verifies."""

        target = self._path_for_digest(digest)
        if not target.is_file():
            return False
        self._verify(target, digest)
        return True


__all__ = ["ObjectRef", "ObjectStore", "ObjectStoreCorruptionError"]
