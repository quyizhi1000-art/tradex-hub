"""Focused tests for executable data-lake maintenance commands."""

from __future__ import annotations

import json

import pytest

from tradex.data_lake import __main__ as lake_cli


def test_label_pending_uses_configured_roots_and_closes_catalog(tmp_path, monkeypatch, capsys):
    lake_root = tmp_path / "lake"
    catalog_path = lake_root / "meta" / "catalog.sqlite3"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.touch()
    cn_root = tmp_path / "cne"
    cn_root.mkdir()
    seen = {}

    class FakeCatalog:
        def __init__(self, path):
            seen["catalog_path"] = path
            self.closed = False
            seen["catalog"] = self

        def close(self):
            self.closed = True

    class FakeBridge:
        def __init__(self, path):
            seen["cn_root"] = path

    class FakeLabeler:
        def __init__(self, catalog, bridge, **kwargs):
            seen["labeler"] = kwargs

        def label_pending(self, through_date):
            seen["through_date"] = through_date
            return [{"label_id": "outcome-1"}]

    monkeypatch.setattr(lake_cli, "Catalog", FakeCatalog)
    monkeypatch.setattr(lake_cli, "CNEquityBridge", FakeBridge)
    monkeypatch.setattr(lake_cli, "ProxyOutcomeLabeler", FakeLabeler)

    result = lake_cli.main(
        [
            "--root",
            str(lake_root),
            "label-pending",
            "--through-date",
            "2026-08-19",
            "--data-root",
            str(cn_root),
            "--proxy",
            "510300.SH",
            "--benchmark",
            "510500.SH",
            "--benchmark-dataset",
            "daily_bars",
        ]
    )

    assert result == 0
    assert seen["catalog_path"] == catalog_path.resolve()
    assert seen["cn_root"] == cn_root
    assert seen["catalog"].closed is True
    assert seen["through_date"] == "2026-08-19"
    assert seen["labeler"]["proxy_symbol"] == "510300.SH"
    assert seen["labeler"]["benchmark_symbol"] == "510500.SH"
    assert seen["labeler"]["benchmark_dataset"] == "daily_bars"
    payload = json.loads(capsys.readouterr().out)
    assert payload["recorded"] == 1
    assert payload["labels"] == [{"label_id": "outcome-1"}]


def test_label_pending_requires_explicit_cnequity_root(tmp_path, monkeypatch):
    lake_root = tmp_path / "lake"
    catalog_path = lake_root / "meta" / "catalog.sqlite3"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.touch()
    monkeypatch.delenv("CNEQUITY_DATA_ROOT", raising=False)

    try:
        lake_cli.main(
            [
                "--root",
                str(lake_root),
                "label-pending",
                "--through-date",
                "2026-08-19",
            ]
        )
    except SystemExit as exc:
        assert "CNEQUITY_DATA_ROOT" in str(exc)
    else:  # pragma: no cover - assertion aid
        raise AssertionError("label-pending unexpectedly accepted a missing data root")


def test_label_pending_requires_explicit_benchmark_dataset(tmp_path):
    lake_root = tmp_path / "lake"
    catalog_path = lake_root / "meta" / "catalog.sqlite3"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.touch()
    cn_root = tmp_path / "cne"
    cn_root.mkdir()

    with pytest.raises(SystemExit, match=r"benchmark-dataset.*index_bars"):
        lake_cli.main(
            [
                "--root",
                str(lake_root),
                "label-pending",
                "--through-date",
                "2026-08-19",
                "--data-root",
                str(cn_root),
                "--benchmark",
                "000300.SH",
            ]
        )
