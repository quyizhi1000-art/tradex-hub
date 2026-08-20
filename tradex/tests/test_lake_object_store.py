"""Focused tests for the atomic content-addressed JSON object store."""

from __future__ import annotations

import gzip
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tradex.data_lake.object_store import ObjectStore, ObjectStoreCorruptionError


def test_put_json_is_content_addressed_deterministic_and_round_trips(tmp_path: Path):
    store = ObjectStore(tmp_path / "lake")
    first = store.put_json("capture", {"b": 2, "a": 1})
    second = store.put_json("feature", {"a": 1, "b": 2})

    assert first.sha256 == second.sha256
    assert first.absolute_path == second.absolute_path
    assert first.relative_path == (
        f"objects/sha256/{first.sha256[:2]}/{first.sha256}.json.gz"
    )
    assert first.absolute_path.is_file()
    assert store.get_json(first) == {"a": 1, "b": 2}
    assert store.contains(first.sha256) is True
    assert list(first.absolute_path.parent.glob("*.tmp")) == []

    with gzip.open(first.absolute_path, "rb") as stream:
        assert stream.read() == b'{"a":1,"b":2}'


def test_concurrent_duplicate_writes_publish_one_verified_object(tmp_path: Path):
    root = tmp_path / "lake"
    payload = {"records": [{"code": "600519.SH", "value": index} for index in range(50)]}

    def publish(index: int):
        # Separate instances exercise the process-wide target lock registry.
        return ObjectStore(root).put_json(f"worker-{index}", payload)

    with ThreadPoolExecutor(max_workers=12) as executor:
        refs = list(executor.map(publish, range(48)))

    assert len({ref.sha256 for ref in refs}) == 1
    assert len({ref.absolute_path for ref in refs}) == 1
    objects = list((root / "objects" / "sha256").glob("*/*.json.gz"))
    assert objects == [refs[0].absolute_path]
    assert list((root / "objects" / "sha256").rglob("*.tmp")) == []
    assert ObjectStore(root).get_json(refs[0]) == payload


def test_existing_corrupt_object_is_reported_not_overwritten(tmp_path: Path):
    store = ObjectStore(tmp_path / "lake")
    reference = store.put_json("capture", {"safe": True})
    reference.absolute_path.write_bytes(b"not-gzip")

    with pytest.raises(ObjectStoreCorruptionError, match="cannot read object"):
        store.put_json("capture", {"safe": True})
    assert reference.absolute_path.read_bytes() == b"not-gzip"


def test_atomic_replace_uses_a_temporary_file_in_target_directory(
    tmp_path: Path, monkeypatch
):
    store = ObjectStore(tmp_path / "lake")
    observed: dict[str, Path] = {}
    real_replace = __import__("os").replace

    def recording_replace(source, destination):
        observed["source"] = Path(source)
        observed["destination"] = Path(destination)
        assert observed["source"].parent == observed["destination"].parent
        assert observed["source"].suffix == ".tmp"
        return real_replace(source, destination)

    monkeypatch.setattr("tradex.data_lake.object_store.os.replace", recording_replace)
    reference = store.put_json("capture", {"atomic": True})

    assert observed["destination"] == reference.absolute_path
    assert not observed["source"].exists()
