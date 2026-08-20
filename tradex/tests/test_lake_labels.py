"""Focused tests for point-in-time-safe proxy outcome labels."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tradex.data_lake.catalog import Catalog, CatalogConflictError
from tradex.data_lake.contracts import (
    ArtifactRef,
    CaptureBundle,
    CapturedDataset,
    DecisionDraft,
    FeatureSnapshot,
)
from tradex.data_lake.labels import (
    LABEL_NAME,
    OutcomeLabelDataError,
    ProxyOutcomeLabeler,
)
from tradex.data_lake.serde import sha256_hex, to_jsonable


SHANGHAI = ZoneInfo("Asia/Shanghai")
LABEL_VERSION = "proxy-next-open-close-v1"


class FakeBridge:
    def __init__(
        self,
        rows,
        *,
        sessions=None,
        index_rows=None,
        generation_sequences=None,
    ):
        self.rows = list(rows)
        self.index_rows = list(index_rows or [])
        if sessions is None:
            sessions = [
                date.fromisoformat(str(row["trade_date"]))
                if not isinstance(row["trade_date"], date)
                else row["trade_date"]
                for row in [*self.rows, *self.index_rows]
            ]
        self.sessions = sorted(set(sessions))
        self.load_calls = []
        self.calendar_calls = []
        self.index_calls = []
        self.generation_calls = []
        self.generation_sequences = {
            key: list(values)
            for key, values in (generation_sequences or {}).items()
        }

    def load_daily_bars(self, **kwargs):
        self.load_calls.append(kwargs)
        return list(self.rows)

    def load_index_bars(self, **kwargs):
        self.index_calls.append(kwargs)
        return list(self.index_rows)

    def load_trading_sessions(self, start, end):
        self.calendar_calls.append({"start": start, "end": end})
        return [session for session in self.sessions if start <= session <= end]

    def describe_generation(self, dataset="daily_bars"):
        self.generation_calls.append(dataset)
        sequence = self.generation_sequences.get(dataset)
        if sequence:
            generation = sequence.pop(0) if len(sequence) > 1 else sequence[0]
        else:
            generation = f"generation-{dataset}"
        return {
            "dataset": dataset,
            "generation_sha256": generation,
            "coverage": {"min": "2026-05-22", "max": "2026-08-19"},
            "file_count": 60,
            "total_size": 123456,
            "files": [{"intentionally": "not persisted in every label"}],
        }


def _publish_decision(catalog: Catalog, trade_date: date, capture_id="capture-1"):
    observed = datetime(
        trade_date.year,
        trade_date.month,
        trade_date.day,
        14,
        45,
        20,
        tzinfo=SHANGHAI,
    )
    dataset = CapturedDataset(name="market_breadth", records=())
    bundle = CaptureBundle(
        capture_id=capture_id,
        observed_at=observed,
        trade_date=trade_date,
        minute_bucket=observed.replace(second=0),
        market_phase="trading",
        market_data={},
        datasets={dataset.name: dataset},
    )
    artifact = ArtifactRef(
        artifact_id=f"artifact-{capture_id}",
        dataset=dataset.name,
        layer="raw",
        object_sha256="a" * 64,
        object_path=f"objects/sha256/aa/{'a' * 64}.json.gz",
        parquet_path=(
            f"parquet/raw/market_breadth/trade_date={trade_date.isoformat()}/"
            f"capture_id={capture_id}/part-0.parquet"
        ),
        parquet_sha256="b" * 64,
        row_count=0,
        schema_version="raw-v1",
    )
    feature = FeatureSnapshot(
        feature_id=f"feature-{capture_id}",
        capture_id=capture_id,
        name="risk_appetite",
        schema_version="risk-feature-v1",
        config_version="config-v1",
        payload={},
        input_artifact_ids=(artifact.artifact_id,),
        code_sha="test-sha",
        created_at=observed,
    )
    decision = DecisionDraft(
        decision_id=f"decision-{capture_id}",
        feature_id=feature.feature_id,
        policy_version="shadow-v1",
        effective_at=observed,
        offense_weight=0.4,
        defense_weight=0.3,
        cash_weight=0.3,
        confidence=0.7,
    )
    catalog.begin_capture(bundle)
    catalog.commit_capture(capture_id, (artifact,), (feature,), decision)
    return decision


def test_weekend_stays_pending_then_uses_calendar_monday_and_hashes_bar(
    tmp_path: Path,
):
    friday_row = {
        "symbol": "510300.SH",
        "trade_date": date(2026, 8, 14),
        "open": 50.0,
        "close": 99.0,
        "source": "tdx_protocol",
    }
    monday_row = {
        "symbol": "510300.SH",
        "trade_date": date(2026, 8, 17),
        "open": 100.0,
        "close": 103.0,
        "source": "tdx_protocol",
    }
    bridge = FakeBridge(
        [friday_row, monday_row],
        sessions=[date(2026, 8, 17)],
    )
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        decision = _publish_decision(catalog, date(2026, 8, 14))
        labeler = ProxyOutcomeLabeler(catalog, bridge)

        assert labeler.label_pending(date(2026, 8, 16)) == []
        assert bridge.load_calls == []
        assert [item["decision_id"] for item in catalog.pending_labels(
            label_name=LABEL_NAME, label_version=LABEL_VERSION
        )] == [decision.decision_id]

        labels = labeler.label_pending(date(2026, 8, 17))

        assert len(labels) == 1
        label = labels[0]
        assert label["entry_at"] == "2026-08-17T09:30:00+08:00"
        assert label["exit_at"] == "2026-08-17T15:00:00+08:00"
        assert label["entry_price"] == 100.0
        assert label["exit_price"] == 103.0
        assert label["gross_return"] == pytest.approx(0.03)
        assert label["excess_return"] is None
        assert label["horizon_sessions"] == 1
        payload = label["payload"]
        assert payload["effective_session"] == "2026-08-17"
        assert payload["horizon_rule"] == "exchange_calendar_next_session"
        assert payload["proxy_only"] is True
        assert payload["portfolio_return"] is None
        assert payload["return_scope"] == "proxy_only"
        assert payload["proxy_bar"]["row"] == to_jsonable(monday_row)
        assert payload["proxy_bar"]["row_sha256"] == sha256_hex(
            to_jsonable(monday_row)
        )
        assert payload["benchmark_bar"] is None
        assert payload["source_generation"] == label["source_generation"]
        assert payload["source_generations"] == {
            "daily_bars": "generation-daily_bars",
            "trading_calendar": "generation-trading_calendar",
        }
        assert "files" not in payload["source_generation_detail"]["daily_bars"]
        assert bridge.calendar_calls == [
            {"start": date(2026, 8, 15), "end": date(2026, 8, 16)},
            {"start": date(2026, 8, 15), "end": date(2026, 8, 17)},
        ]
        assert bridge.load_calls == [
            {
                "symbols": ["510300.SH"],
                "start": date(2026, 8, 17),
                "end": date(2026, 8, 17),
                "adjust": None,
                "strict_adj": True,
            }
        ]


def test_calendar_next_session_missing_bar_never_slides_to_later_bar(tmp_path: Path):
    bridge = FakeBridge(
        [
            {
                "symbol": "510300.SH",
                "trade_date": "2026-08-18",
                "open": 100,
                "close": 110,
            }
        ],
        sessions=[date(2026, 8, 17), date(2026, 8, 18)],
    )
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        decision = _publish_decision(catalog, date(2026, 8, 14))

        assert ProxyOutcomeLabeler(catalog, bridge).label_pending("2026-08-18") == []
        assert bridge.load_calls[0]["start"] == date(2026, 8, 17)
        assert bridge.load_calls[0]["end"] == date(2026, 8, 17)
        assert catalog.get_capture("capture-1")["outcome_labels"] == []
        assert catalog.pending_labels(
            label_name=LABEL_NAME, label_version=LABEL_VERSION
        )[0]["decision_id"] == decision.decision_id


def test_generation_race_rejects_label_and_keeps_decision_pending(tmp_path: Path):
    bridge = FakeBridge(
        [
            {
                "symbol": "510300.SH",
                "trade_date": "2026-08-18",
                "open": 10,
                "close": 11,
            }
        ],
        sessions=[date(2026, 8, 18)],
        generation_sequences={
            "trading_calendar": ["calendar-a", "calendar-a"],
            "daily_bars": ["bars-before", "bars-after"],
        },
    )
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        decision = _publish_decision(catalog, date(2026, 8, 17))

        with pytest.raises(OutcomeLabelDataError, match=r"changed.*remain pending"):
            ProxyOutcomeLabeler(catalog, bridge).label_pending("2026-08-18")

        assert catalog.get_capture("capture-1")["outcome_labels"] == []
        assert catalog.pending_labels(
            label_name=LABEL_NAME, label_version=LABEL_VERSION
        )[0]["decision_id"] == decision.decision_id


def test_no_later_calendar_session_leaves_decision_pending(tmp_path: Path):
    bridge = FakeBridge([], sessions=[])
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        decision = _publish_decision(catalog, date(2026, 8, 19))

        assert ProxyOutcomeLabeler(catalog, bridge).label_pending("2026-08-19") == []
        assert bridge.calendar_calls == []
        assert catalog.get_capture("capture-1")["outcome_labels"] == []
        assert catalog.pending_labels(
            label_name=LABEL_NAME, label_version=LABEL_VERSION
        )[0]["decision_id"] == decision.decision_id


def test_repeated_run_and_catalog_write_are_idempotent_but_revision_conflicts(
    tmp_path: Path,
):
    bridge = FakeBridge(
        [{"symbol": "510300.SH", "trade_date": "2026-08-18", "open": 10, "close": 11}],
        sessions=[date(2026, 8, 18)],
    )
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        _publish_decision(catalog, date(2026, 8, 17))
        labeler = ProxyOutcomeLabeler(catalog, bridge)

        first = labeler.label_pending("2026-08-18")[0]
        assert labeler.label_pending("2026-08-18") == []
        assert len(bridge.load_calls) == 1

        retry = dict(first)
        retry["computed_at"] = "2099-01-01T00:00:00+08:00"
        assert catalog.record_outcome_label(retry) == first

        revised = dict(first)
        revised["source_generation"] = "revised-generation"
        with pytest.raises(CatalogConflictError, match="different content"):
            catalog.record_outcome_label(revised)


def test_index_benchmark_uses_explicit_dataset_and_audits_both_rows(tmp_path: Path):
    proxy_row = {
        "symbol": "510300.SH",
        "trade_date": "2026-08-18",
        "open": 100,
        "close": 102,
        "source": "tdx_protocol",
    }
    index_row = {
        "symbol": "000300.SH",
        "trade_date": "2026-08-18",
        "open": 200,
        "close": 202,
        "source": "tdx_protocol",
        "frequency": "1d",
    }
    bridge = FakeBridge(
        [proxy_row],
        index_rows=[index_row],
        sessions=[date(2026, 8, 18)],
    )
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        _publish_decision(catalog, date(2026, 8, 17))

        label = ProxyOutcomeLabeler(
            catalog,
            bridge,
            benchmark_symbol="000300.SH",
            benchmark_dataset="index_bars",
        ).label_pending("2026-08-18")[0]

        assert label["gross_return"] == pytest.approx(0.02)
        assert label["payload"]["benchmark_gross"] == pytest.approx(0.01)
        assert label["excess_return"] == pytest.approx(0.01)
        assert label["benchmark"] == "000300.SH"
        assert bridge.load_calls[0]["symbols"] == ["510300.SH"]
        assert bridge.index_calls[0]["symbols"] == ["000300.SH"]
        assert label["payload"]["proxy_bar"]["row_sha256"] == sha256_hex(
            to_jsonable(proxy_row)
        )
        assert label["payload"]["benchmark_bar"]["row_sha256"] == sha256_hex(
            to_jsonable(index_row)
        )
        assert label["payload"]["benchmark_bar"]["dataset"] == "index_bars"
        assert set(label["payload"]["source_generations"]) == {
            "daily_bars",
            "index_bars",
            "trading_calendar",
        }


def test_benchmark_dataset_must_be_explicit_and_supported():
    bridge = FakeBridge([])

    with pytest.raises(ValueError, match=r"must be explicit.*index_bars"):
        ProxyOutcomeLabeler(object(), bridge, benchmark_symbol="000300.SH")
    with pytest.raises(ValueError, match=r"daily_bars.*index_bars"):
        ProxyOutcomeLabeler(
            object(),
            bridge,
            benchmark_symbol="000300.SH",
            benchmark_dataset="market_breadth",
        )
