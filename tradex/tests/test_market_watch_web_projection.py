from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tradex.market_watch import AlertV1, MarketWatchSnapshotV1, build_market_watch_snapshot
from tradex.market_watch.collection_contracts import MarketWatchCollectorEnvelopeV1
from tradex.market_watch.collection_store import MarketWatchCollectionStore
from tradex.market_watch.history import MarketWatchHistoryStore
from tradex.market_watch.web_projection import (
    AcceptedRealUnavailable,
    SectorSelectionError,
    SourceSnapshotRevisionMismatch,
    TrajectoryRevisionMismatch,
    build_market_watch_summary,
    build_five_day_sector_flow_trajectory,
    build_sector_flow_detail,
)
from tradex.market_watch.web_payload_service import (
    MarketWatchWebPayloadService,
    WebAcceptedRealUnavailable,
    WebRevisionConflict,
    WebSectorSelectionRejected,
)
from tradex.market_watch.web_api import MarketWatchWebApi
from tradex.market_watch.integrity import (
    build_trajectory_payload_integrity,
    canonical_json_bytes,
    stable_sha256,
)
from tradex.market_watch.read_facade import (
    AcceptedSnapshotReadError,
    HistoricalMarketWatchSnapshot,
    MarketWatchReadFacade,
)
from tradex.market_watch.contracts import SectorFlowLeaderSnapshotV1
from tradex.market_watch.sector_resonance import (
    SectorResonanceBatchV1,
    SectorResonanceEntryV1,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _trajectory(observed_at: datetime, *, direction: str) -> dict:
    sectors = []
    specs = (
        (("electric_power", "电力"), ("bank", "银行"))
        if direction == "defense"
        else (("semiconductor", "半导体"), ("software", "软件"))
    )
    for rank, (sector_key, name) in enumerate(specs, start=1):
        points = [
            {
                "sampled_at": (observed_at - timedelta(minutes=offset)).isoformat(),
                "provider_as_of": (
                    observed_at - timedelta(minutes=offset)
                ).isoformat(),
                "session_segment": "am",
                "cumulative_cny": float((4 - offset) * rank * 100_000_000),
                "delta_5m_cny": None,
                "delta_5m_baseline_as_of": None,
            }
            for offset in (2, 1, 0)
        ]
        sectors.append({
            "sector_key": sector_key,
            "name": name,
            "category_key": (
                "steady_defense" if direction == "defense" else "technology_growth"
            ),
            "category_name": "稳态防御" if direction == "defense" else "科技成长",
            "taxonomy": "industry",
            "status": "collecting",
            "follow_eligible": True,
            "eligible_for_rank": True,
            "observation_rank": rank,
            "rank_total": len(specs),
            "observation_tier": "strong_pending",
            "tier_label": "强势待确认",
            "latest": {
                "provider_as_of": observed_at.isoformat(),
                "change_pct": float(rank),
                "breadth_ratio": 0.60 + rank / 10,
                "cumulative_cny": float(4 * rank * 100_000_000),
                "main_net_inflow_pct": float(rank),
                "price_percentile": 0.80,
                "flow_percentile": 0.75,
                "delta_5m_cny": None,
                "delta_10m_cny": None,
                "delta_5m_baseline_as_of": None,
                "delta_10m_baseline_as_of": None,
                "change_delta_5m_pct": None,
                "current_strength": "strong",
                "fund_strength": "strong",
                "incremental_direction": "unknown",
            },
            "points": points,
            "supporting_evidence": ["当日累计估算净流入"],
            "counter_evidence": ["近5分钟同源基线仍在积累"],
            "flags": ["five_minute_baseline_collecting"],
            "reason": None,
        })
    return {
        "contract": "sector_flow_trajectory.v1",
        "schema_version": 1,
        "direction": direction,
        "status": "collecting",
        "trade_date": observed_at.date().isoformat(),
        "as_of": observed_at.isoformat(),
        "market_phase": "trading",
        "trajectory_scope": "trading_session_to_as_of",
        "marginal_window_minutes": 5,
        "sectors": sectors,
        "flags": ["provider_estimated_flow"],
        "reason": None,
    }


def _snapshot() -> MarketWatchSnapshotV1:
    observed_at = datetime(2026, 8, 26, 10, 30, tzinfo=SHANGHAI)
    specs = (
        ("broad_market", "000001.SH", "上证指数", 3400.0, 0.4),
        ("large_cap", "000300.SH", "沪深300", 4100.0, 0.5),
        ("small_cap", "000852.SH", "中证1000", 6800.0, 0.8),
        ("growth", "399006.SZ", "创业板指", 2250.0, 0.9),
    )
    market = {
        "timestamp": observed_at.isoformat(),
        "provider_as_of": observed_at.isoformat(),
        "market_state": {"phase": "trading", "is_open": True},
        "quality": "accepted",
        "indices": [
            {
                "role": role,
                "instrument_id": instrument_id,
                "name": name,
                "available": True,
                "level": level,
                "change_pct": change,
                "provider_as_of": observed_at.isoformat(),
                "quality": "accepted",
            }
            for role, instrument_id, name, level, change in specs
        ],
        "market_turnover": {
            "available": True,
            "today_date": observed_at.date().isoformat(),
            "previous_date": "2026-08-25",
            "as_of": observed_at.strftime("%H:%M"),
            "today_amount": 110_000_000_000.0,
            "previous_same_time_amount": 100_000_000_000.0,
        },
    }
    risk = {
        "timestamp": observed_at.isoformat(),
        "breadth": {
            "up_count": 3000,
            "down_count": 1800,
            "flat_count": 100,
            "unclassified_count": 100,
            "total_count": 5000,
            "provider_as_of": observed_at.isoformat(),
            "quality": "accepted",
        },
        "rotation": {
            "sectors": [{
                "sector_key": "industry:securities",
                "name": "证券",
                "tags": ["attack"],
                "change_pct": 1.5,
                "breadth_ratio": 0.66,
                "main_net_inflow_cny": 5_000_000_000.0,
                "provider_as_of": observed_at.isoformat(),
            }]
        },
        "sector_flow_trajectory": _trajectory(observed_at, direction="defense"),
        "offense_sector_flow_trajectory": _trajectory(observed_at, direction="offense"),
    }
    return build_market_watch_snapshot(
        market,
        risk,
        as_of=observed_at,
        sequence=7,
        snapshot_id="mw-projection-test",
    )


def _collector_envelope(
    snapshot: MarketWatchSnapshotV1,
    source_revision: str,
    *,
    include_accepted_real: bool = True,
) -> MarketWatchCollectorEnvelopeV1:
    minute_bucket = snapshot.as_of.replace(second=0, microsecond=0)
    cursor_minute = minute_bucket + timedelta(minutes=1)
    accepted_real = (
        {
            "trade_date": snapshot.market_state.trading_date,
            "minute_bucket": minute_bucket,
            "snapshot_id": snapshot.snapshot_id,
            "source_snapshot_revision": source_revision,
            "accepted_at": snapshot.as_of + timedelta(seconds=5),
            "repaired": False,
        }
        if include_accepted_real
        else None
    )
    cursor = {
        "trade_date": snapshot.market_state.trading_date,
        "minute_bucket": cursor_minute,
        "status": "retrying",
        "attempt_count": 1,
        "first_attempt_at": cursor_minute,
        "last_attempt_at": cursor_minute,
        "next_retry_at": cursor_minute + timedelta(seconds=10),
        "accepted_at": None,
        "source_snapshot_id": None,
        "source_snapshot_revision": None,
        "gap_heartbeat": True,
        "last_error_code": "provider_timeout",
        "last_error_message": "retry scheduled",
        "ledger_revision": 2,
        "updated_at": cursor_minute + timedelta(seconds=1),
    }
    return MarketWatchCollectorEnvelopeV1.model_validate({
        "as_of": cursor_minute + timedelta(seconds=1),
        "collector_state": "degraded",
        "collector_heartbeat_at": cursor_minute + timedelta(seconds=1),
        "latest_accepted_real": accepted_real,
        "collection_cursor": cursor,
        "latest_published_status": cursor,
        "collection_completeness": {
            "trade_date": snapshot.market_state.trading_date,
            "as_of": cursor_minute + timedelta(seconds=1),
            "expected_minute_buckets": 2,
            "accepted_real": 1 if include_accepted_real else 0,
            "repaired": 0,
            "pending": 0 if include_accepted_real else 1,
            "retrying": 1,
            "unresolved": 0,
            "gap_heartbeat": 1,
            "ledger_revision": 2,
            "ledger_digest": stable_sha256({"ledger_revision": 2}),
        },
    })


class _CollectionReader:
    read_only = True

    def __init__(self, envelope: MarketWatchCollectorEnvelopeV1) -> None:
        self.envelope = envelope
        self.calls = 0

    def read_envelope(self, *, as_of: datetime) -> MarketWatchCollectorEnvelopeV1:
        self.calls += 1
        return self.envelope.model_copy(update={"as_of": as_of})


class _HistoryReader:
    read_only = True

    def __init__(
        self,
        snapshot: MarketWatchSnapshotV1,
        source_revision: str,
    ) -> None:
        self.snapshot = snapshot
        self.source_revision = source_revision
        self.metadata_kind = "accepted_real"
        self.payload = snapshot.model_dump(mode="json")
        self.metadata_calls = 0
        self.payload_calls = 0

    def list_dates(self, limit: int = 20) -> list[dict]:
        return [{"trade_date": self.snapshot.as_of.date().isoformat()}][:limit]

    def get_collection_records(self, trade_date) -> list[dict]:
        self.metadata_calls += 1
        return [{
            "trade_date": trade_date.isoformat(),
            "minute_bucket": self.snapshot.as_of.replace(
                second=0,
                microsecond=0,
            ).isoformat(),
            "snapshot_id": self.snapshot.snapshot_id,
            "payload_digest": self.source_revision,
            "record_kind": self.metadata_kind,
        }]

    def get_snapshot_by_pointer(self, **pointer) -> dict | None:
        self.payload_calls += 1
        return {
            "trade_date": pointer["trade_date"].isoformat(),
            "minute_bucket": pointer["minute_bucket"].isoformat(),
            "payload_digest": pointer["payload_digest"],
            "payload": self.payload,
        }


class _ResonanceReader:
    read_only = True

    def __init__(self, batch: SectorResonanceBatchV1) -> None:
        self.batch = batch

    def get_by_source_revision(self, source_revision: str):
        if self.batch.source_snapshot_revision != source_revision:
            return None
        return self.batch

    def get_latest_before(self, as_of, *, max_age_minutes):
        assert max_age_minutes == 6
        return self.batch if self.batch.source_as_of <= as_of else None


def _resonance_batch(
    snapshot: MarketWatchSnapshotV1,
    source_revision: str,
    *,
    status_label: str = "暂无上行共振快涨股",
) -> SectorResonanceBatchV1:
    return SectorResonanceBatchV1.create(
        source_snapshot_revision=source_revision,
        source_snapshot_id=snapshot.snapshot_id,
        source_as_of=snapshot.as_of,
        generated_at=snapshot.as_of + timedelta(minutes=10),
        entries=(
            SectorResonanceEntryV1(
                direction="defense",
                sector_key="electric_power",
                sector_name="电力",
                leader_snapshot=SectorFlowLeaderSnapshotV1(
                    status="no_match",
                    status_label=status_label,
                    selection_method="sector_fund_flow_path_resonance.v2",
                    marginal_window_minutes=5,
                    resonance_direction="up",
                    provider_as_of=snapshot.as_of,
                ),
            ),
        ),
    )


def test_summary_keeps_every_series_and_exact_point_manifest_without_points() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    collector_envelope = _collector_envelope(snapshot, source_revision)

    summary = build_market_watch_summary(
        snapshot,
        source_snapshot_revision=source_revision,
        collector_envelope=collector_envelope,
    )

    assert summary.source_snapshot_revision == source_revision
    assert collector_envelope.collection_cursor is not None
    assert collector_envelope.collection_cursor.gap_heartbeat is True
    assert collector_envelope.latest_accepted_real is not None
    assert collector_envelope.latest_accepted_real.snapshot_id == snapshot.snapshot_id
    for original, projected, integrity in (
        (
            snapshot.sector_flow_trajectory,
            summary.sector_flow_trajectory,
            summary.payload_integrity.defense,
        ),
        (
            snapshot.offense_sector_flow_trajectory,
            summary.offense_sector_flow_trajectory,
            summary.payload_integrity.offense,
        ),
    ):
        assert original is not None
        assert projected is not None
        assert integrity is not None
        assert projected.trajectory_revision == stable_sha256(original)
        assert projected.sector_count == len(original.sectors)
        assert projected.point_count == sum(len(item.points) for item in original.sectors)
        assert [item.sector_key for item in projected.sectors] == [
            item.sector_key for item in original.sectors
        ]
        assert [item.sector_key for item in integrity.sectors] == [
            item.sector_key for item in original.sectors
        ]
        for source_series, summary_series, item_integrity in zip(
            original.sectors,
            projected.sectors,
            integrity.sectors,
            strict=True,
        ):
            expected_metadata = source_series.model_dump(
                mode="json",
                exclude={"points"},
            )
            actual_metadata = summary_series.model_dump(
                mode="json",
                exclude={
                    "point_count",
                    "first_provider_as_of",
                    "last_provider_as_of",
                    "points_revision",
                },
            )
            assert actual_metadata == expected_metadata
            assert summary_series.point_count == len(source_series.points)
            assert summary_series.first_provider_as_of == source_series.points[0].provider_as_of
            assert summary_series.last_provider_as_of == source_series.points[-1].provider_as_of
            assert summary_series.points_revision == stable_sha256(source_series.points)
            assert item_integrity.points_revision == summary_series.points_revision
    assert '"points":' not in summary.model_dump_json()


def test_integrity_api_matches_persistence_canonical_json_for_models_and_sequences() -> None:
    snapshot = _snapshot()
    payload = snapshot.model_dump(mode="json")
    expected = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

    assert canonical_json_bytes(snapshot) == expected
    assert canonical_json_bytes(payload) == expected
    assert stable_sha256(snapshot) == hashlib.sha256(expected).hexdigest()

    trajectory = snapshot.sector_flow_trajectory
    assert trajectory is not None
    integrity = build_trajectory_payload_integrity(trajectory)
    assert integrity.trajectory_revision == stable_sha256(trajectory)
    assert integrity.point_count == sum(len(item.points) for item in trajectory.sectors)
    assert integrity.sectors[0].points_revision == stable_sha256(
        trajectory.sectors[0].points
    )


def test_persisted_payload_revision_is_checked_before_current_model_defaults() -> None:
    snapshot = _snapshot()
    payload_with_alert = snapshot.model_dump(mode="python")
    payload_with_alert["alerts"] = (
        AlertV1(
            code="data_delay",
            severity="caution",
            title="数据延迟",
            message="等待下一次已验证采样。",
            dedupe_key="market_watch:data_delay",
        ),
    )
    canonical = MarketWatchSnapshotV1.model_validate(payload_with_alert)
    persisted_payload = canonical.model_dump(mode="json")
    for default_field in (
        "kind",
        "sector_key",
        "sector_label",
        "sector_direction",
        "move_direction",
        "trigger_threshold_pct",
        "change_delta_5m_pct",
        "change_pct",
        "flow_delta_5m_cny",
        "provider_as_of",
        "leaders",
    ):
        persisted_payload["alerts"][0].pop(default_field)
    source_revision = stable_sha256(persisted_payload)

    summary = build_market_watch_summary(
        persisted_payload,
        source_snapshot_revision=source_revision,
        collector_envelope=_collector_envelope(canonical, source_revision),
    )

    assert summary.source_snapshot_revision == source_revision
    assert summary.alerts[0].kind == "market_state"
    assert stable_sha256(canonical) != source_revision


def test_detail_returns_original_points_for_complete_canonical_selection() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    collector_envelope = _collector_envelope(snapshot, source_revision)
    trajectory = snapshot.offense_sector_flow_trajectory
    assert trajectory is not None

    detail = build_sector_flow_detail(
        snapshot,
        source_snapshot_revision=source_revision,
        collector_envelope=collector_envelope,
        direction="offense",
        sector_keys=("software", "semiconductor"),
        expected_trajectory_revision=stable_sha256(trajectory),
    )

    assert detail.source_snapshot_revision == source_revision
    assert detail.trajectory_revision == stable_sha256(trajectory)
    assert detail.sector_keys == tuple(item.sector_key for item in trajectory.sectors)
    assert detail.point_count == sum(len(item.points) for item in trajectory.sectors)
    assert [item.model_dump(mode="json") for item in detail.sectors] == [
        item.model_dump(mode="json") for item in trajectory.sectors
    ]
    for returned, integrity in zip(detail.sectors, detail.sector_integrity, strict=True):
        assert integrity.point_count == len(returned.points)
        assert integrity.points_revision == stable_sha256(returned.points)


def test_five_day_projection_keeps_each_daily_minute_curve_separate() -> None:
    base = _snapshot()
    retained = []
    for offset in (6, 5, 2, 1, 0):
        shifted = base.model_copy(deep=True)
        payload = shifted.model_dump(mode="python")

        def move_dates(value):
            if isinstance(value, datetime):
                return value - timedelta(days=offset)
            if isinstance(value, date):
                return value - timedelta(days=offset)
            if isinstance(value, dict):
                return {key: move_dates(item) for key, item in value.items()}
            if isinstance(value, tuple):
                return tuple(move_dates(item) for item in value)
            if isinstance(value, list):
                return [move_dates(item) for item in value]
            return value

        payload = move_dates(payload)
        payload["snapshot_id"] = f"mw-five-day-{offset}"
        snapshot = MarketWatchSnapshotV1.model_validate(payload)
        source_payload = snapshot.model_dump(mode="json")
        retained.append(
            HistoricalMarketWatchSnapshot(
                trade_date=snapshot.market_state.trading_date,
                minute_bucket=snapshot.as_of.replace(second=0, microsecond=0),
                snapshot_id=snapshot.snapshot_id,
                source_snapshot_revision=stable_sha256(source_payload),
                snapshot=snapshot,
                source_payload=source_payload,
            )
        )

    history = build_five_day_sector_flow_trajectory(
        retained,
        direction="defense",
        sector_keys=("electric_power",),
    )

    assert history.contract == "sector_flow_five_day_trajectory.v1"
    assert history.available_trade_days == 5
    assert history.trade_dates == tuple(item.trade_date for item in retained)
    assert history.point_count == 15
    assert history.status == "partial"
    assert [len(day.sectors[0].points) for day in history.days] == [3] * 5
    assert all(
        point.provider_as_of.date() == day.trade_date
        for day in history.days
        for point in day.sectors[0].points
    )


def test_projection_fails_closed_on_revision_or_selection_mismatch() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    collector_envelope = _collector_envelope(snapshot, source_revision)
    trajectory = snapshot.sector_flow_trajectory
    assert trajectory is not None

    with pytest.raises(SourceSnapshotRevisionMismatch):
        build_market_watch_summary(
            snapshot,
            source_snapshot_revision="0" * 64,
            collector_envelope=collector_envelope,
        )
    with pytest.raises(AcceptedRealUnavailable):
        build_market_watch_summary(
            snapshot,
            source_snapshot_revision=source_revision,
            collector_envelope=_collector_envelope(
                snapshot,
                source_revision,
                include_accepted_real=False,
            ),
        )
    with pytest.raises(TrajectoryRevisionMismatch):
        build_sector_flow_detail(
            snapshot,
            source_snapshot_revision=source_revision,
            collector_envelope=collector_envelope,
            direction="defense",
            sector_keys=("electric_power",),
            expected_trajectory_revision="0" * 64,
        )
    for invalid_keys in (
        ("electric_power", "missing_sector"),
        ("electric_power", "electric_power"),
        (),
    ):
        with pytest.raises(SectorSelectionError):
            build_sector_flow_detail(
                snapshot,
                source_snapshot_revision=source_revision,
                collector_envelope=collector_envelope,
                direction="defense",
                sector_keys=invalid_keys,
                expected_trajectory_revision=stable_sha256(trajectory),
            )


def test_projection_is_deterministic_and_does_not_mutate_the_snapshot() -> None:
    snapshot = _snapshot()
    before = deepcopy(snapshot.model_dump(mode="json"))
    source_revision = stable_sha256(snapshot)
    collector_envelope = _collector_envelope(snapshot, source_revision)

    first = build_market_watch_summary(
        snapshot,
        source_snapshot_revision=source_revision,
        collector_envelope=collector_envelope,
    )
    second = build_market_watch_summary(
        snapshot,
        source_snapshot_revision=source_revision,
        collector_envelope=collector_envelope,
    )

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert stable_sha256(first) == stable_sha256(second)
    assert snapshot.model_dump(mode="json") == before


def test_read_facade_uses_exact_history_once_and_reuses_only_the_same_revision() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    collection = _CollectionReader(_collector_envelope(snapshot, source_revision))
    history = _HistoryReader(snapshot, source_revision)
    facade = MarketWatchReadFacade(
        collection_reader=collection,
        history_reader=history,
        clock=lambda: snapshot.as_of + timedelta(minutes=1),
    )

    first = facade.read()
    second = facade.read()

    assert first.accepted is not None
    assert first.accepted.snapshot.snapshot_id == snapshot.snapshot_id
    assert first.payload_cache_hit is False
    assert second.accepted is not None
    assert second.accepted.snapshot is first.accepted.snapshot
    assert second.accepted.source_payload is first.accepted.source_payload
    assert second.payload_cache_hit is True
    assert collection.calls == 2
    assert history.metadata_calls == 1
    assert history.payload_calls == 1
    summary = build_market_watch_summary(
        second.accepted.source_payload,
        source_snapshot_revision=second.accepted.pointer.source_snapshot_revision,
        collector_envelope=second.collector_envelope,
    )
    assert summary.snapshot_id == snapshot.snapshot_id
    with pytest.raises(TypeError):
        first.accepted.source_payload["snapshot_id"] = "mutated"


def test_read_facade_returns_state_only_without_accepted_real_or_history_reads() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    collection = _CollectionReader(
        _collector_envelope(
            snapshot,
            source_revision,
            include_accepted_real=False,
        )
    )
    history = _HistoryReader(snapshot, source_revision)
    facade = MarketWatchReadFacade(
        collection_reader=collection,
        history_reader=history,
        clock=lambda: snapshot.as_of + timedelta(minutes=1),
    )

    view = facade.read()

    assert view.accepted is None
    assert view.collector_envelope.collection_cursor is not None
    assert view.collector_envelope.collection_cursor.gap_heartbeat is True
    assert history.metadata_calls == 0
    assert history.payload_calls == 0


def test_read_facade_rejects_derived_gap_heartbeat_before_payload_read() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    collection = _CollectionReader(_collector_envelope(snapshot, source_revision))
    history = _HistoryReader(snapshot, source_revision)
    history.metadata_kind = "derived_gap_heartbeat"
    facade = MarketWatchReadFacade(
        collection_reader=collection,
        history_reader=history,
        clock=lambda: snapshot.as_of + timedelta(minutes=1),
    )

    with pytest.raises(AcceptedSnapshotReadError):
        facade.read()

    assert history.metadata_calls == 1
    assert history.payload_calls == 0


def test_read_facade_accepts_exact_ledger_pointer_to_stale_classified_history() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    collection = _CollectionReader(_collector_envelope(snapshot, source_revision))
    history = _HistoryReader(snapshot, source_revision)
    history.metadata_kind = "stale_snapshot"
    facade = MarketWatchReadFacade(
        collection_reader=collection,
        history_reader=history,
        clock=lambda: snapshot.as_of + timedelta(minutes=1),
    )

    view = facade.read()

    assert view.accepted is not None
    assert view.accepted.pointer.snapshot_id == snapshot.snapshot_id
    assert view.accepted.pointer.source_snapshot_revision == source_revision
    assert history.metadata_calls == 1
    assert history.payload_calls == 1


def test_read_facade_fails_closed_on_exact_payload_revision_mismatch() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    collection = _CollectionReader(_collector_envelope(snapshot, source_revision))
    history = _HistoryReader(snapshot, source_revision)
    history.payload = {**history.payload, "sequence": snapshot.sequence + 1}
    facade = MarketWatchReadFacade(
        collection_reader=collection,
        history_reader=history,
        clock=lambda: snapshot.as_of + timedelta(minutes=1),
    )

    with pytest.raises(AcceptedSnapshotReadError, match="source revision"):
        facade.read()

    assert history.payload_calls == 1


def test_read_facade_rejects_write_capable_store_dependencies() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    collection = _CollectionReader(_collector_envelope(snapshot, source_revision))
    history = _HistoryReader(snapshot, source_revision)
    collection.read_only = False

    with pytest.raises(ValueError, match="collection_reader"):
        MarketWatchReadFacade(
            collection_reader=collection,
            history_reader=history,
        )


def test_read_only_sqlite_facade_resolves_ledger_pointer_to_exact_history(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "market-watch.sqlite3"
    snapshot = _snapshot()
    observed = snapshot.as_of + timedelta(minutes=1)

    with MarketWatchHistoryStore(db_path) as history_writer:
        recorded = history_writer.record(snapshot)
        records = history_writer.get_collection_records(
            snapshot.market_state.trading_date
        )
    with MarketWatchCollectionStore(db_path) as collection_writer:
        collection_writer.ensure_expected_slots(snapshot.market_state.trading_date)
        imported = collection_writer.reconcile_history_record(records[0])
        collection_writer.update_runtime("running", heartbeat_at=observed)

    assert recorded["payload_digest"] == stable_sha256(snapshot)
    assert imported["action"] == "imported"

    with (
        MarketWatchCollectionStore(db_path, read_only=True) as collection_reader,
        MarketWatchHistoryStore(db_path, read_only=True) as history_reader,
    ):
        facade = MarketWatchReadFacade(
            collection_reader=collection_reader,
            history_reader=history_reader,
            clock=lambda: observed,
        )
        first = facade.read()
        second = facade.read()

    assert first.accepted is not None
    assert first.accepted.pointer.source_snapshot_revision == recorded["payload_digest"]
    assert first.accepted.snapshot.model_dump(mode="json") == snapshot.model_dump(
        mode="json"
    )
    assert first.payload_cache_hit is False
    assert second.payload_cache_hit is True
    summary = build_market_watch_summary(
        second.accepted.source_payload,
        source_snapshot_revision=second.accepted.pointer.source_snapshot_revision,
        collector_envelope=second.collector_envelope,
    )
    assert summary.snapshot_id == snapshot.snapshot_id
    assert summary.payload_integrity.defense is not None
    assert summary.payload_integrity.offense is not None


def test_payload_service_status_is_small_and_never_reads_history_payload() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    collection = _CollectionReader(_collector_envelope(snapshot, source_revision))
    history = _HistoryReader(snapshot, source_revision)
    service = MarketWatchWebPayloadService(
        MarketWatchReadFacade(
            collection_reader=collection,
            history_reader=history,
            clock=lambda: snapshot.as_of + timedelta(minutes=1),
        )
    )

    first = service.get_collection_status()
    unchanged = service.get_collection_status(if_none_match=f"W/{first.etag}")

    payload = json.loads(first.body)
    assert first.status_code == 200
    assert payload["contract"] == "market_watch_collector_envelope.v1"
    assert payload["latest_accepted_real"]["source_snapshot_revision"] == source_revision
    assert len(first.body) < 10_000
    assert unchanged.status_code == 304
    assert unchanged.body == b""
    assert history.metadata_calls == 0
    assert history.payload_calls == 0


def test_web_reader_uses_previous_day_snapshot_before_auction_completes() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    pre_open = (snapshot.as_of + timedelta(days=1)).replace(
        hour=9,
        minute=24,
        second=0,
        microsecond=0,
    )
    previous_envelope = _collector_envelope(snapshot, source_revision)
    current_envelope = previous_envelope.model_copy(update={
        "as_of": pre_open,
        "collection_cursor": None,
        "collection_completeness": (
            previous_envelope.collection_completeness.model_copy(update={
                "trade_date": pre_open.date(),
                "as_of": pre_open,
                "expected_minute_buckets": 239,
                "accepted_real": 0,
                "pending": 239,
                "retrying": 0,
                "gap_heartbeat": 0,
            })
        ),
    })
    history = _HistoryReader(snapshot, source_revision)
    facade = MarketWatchReadFacade(
        collection_reader=_CollectionReader(current_envelope),
        history_reader=history,
        clock=lambda: pre_open,
    )

    status = facade.read_collection_status()
    view = facade.read()

    assert status.collection_completeness.trade_date == pre_open.date()
    assert status.latest_accepted_real is not None
    assert status.latest_accepted_real.trade_date == snapshot.as_of.date()
    assert status.collection_cursor is None
    assert status.latest_published_status is None
    assert view.accepted is not None
    assert view.accepted.snapshot.snapshot_id == snapshot.snapshot_id
    assert history.metadata_calls == 1
    assert history.payload_calls == 1


def test_web_reader_hides_previous_day_snapshot_when_auction_completes() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    next_session = (snapshot.as_of + timedelta(days=1)).replace(
        hour=9,
        minute=25,
        second=0,
        microsecond=0,
    )
    previous_envelope = _collector_envelope(snapshot, source_revision)
    current_envelope = previous_envelope.model_copy(update={
        "as_of": next_session,
        "collection_cursor": None,
        "collection_completeness": (
            previous_envelope.collection_completeness.model_copy(update={
                "trade_date": next_session.date(),
                "as_of": next_session,
                "expected_minute_buckets": 239,
                "accepted_real": 0,
                "pending": 239,
                "retrying": 0,
                "gap_heartbeat": 0,
            })
        ),
    })
    history = _HistoryReader(snapshot, source_revision)
    facade = MarketWatchReadFacade(
        collection_reader=_CollectionReader(current_envelope),
        history_reader=history,
        clock=lambda: next_session,
    )

    status = facade.read_collection_status()
    view = facade.read()

    assert status.collection_completeness.trade_date == next_session.date()
    assert status.latest_accepted_real is None
    assert status.collection_cursor is None
    assert status.latest_published_status is None
    assert view.accepted is None
    assert history.metadata_calls == 0
    assert history.payload_calls == 0


def test_payload_service_summary_caches_compact_bytes_and_honors_etag() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    collection = _CollectionReader(_collector_envelope(snapshot, source_revision))
    history = _HistoryReader(snapshot, source_revision)
    service = MarketWatchWebPayloadService(
        MarketWatchReadFacade(
            collection_reader=collection,
            history_reader=history,
            clock=lambda: snapshot.as_of + timedelta(minutes=1),
        )
    )

    first = service.get_summary(
        expected_source_snapshot_revision=source_revision,
    )
    unchanged = service.get_summary(
        expected_source_snapshot_revision=source_revision,
        if_none_match=first.etag,
    )

    payload = json.loads(first.body)
    assert first.status_code == 200
    assert first.cache_hit is False
    assert first.source_snapshot_revision == source_revision
    assert first.view_revision == stable_sha256(payload)
    assert payload["contract"] == "market_watch_summary.v1"
    assert payload["source_snapshot_revision"] == source_revision
    assert '"points":' not in first.body.decode("utf-8")
    assert unchanged.status_code == 304
    assert unchanged.cache_hit is True
    assert unchanged.body == b""
    assert history.metadata_calls == 1
    assert history.payload_calls == 1


def test_payload_service_overlays_exact_source_resonance_and_invalidates_cache() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    collection = _CollectionReader(_collector_envelope(snapshot, source_revision))
    history = _HistoryReader(snapshot, source_revision)
    resonance = _ResonanceReader(_resonance_batch(snapshot, source_revision))
    service = MarketWatchWebPayloadService(
        MarketWatchReadFacade(
            collection_reader=collection,
            history_reader=history,
            clock=lambda: snapshot.as_of + timedelta(minutes=1),
        ),
        resonance_reader=resonance,
    )

    first = service.get_summary(
        expected_source_snapshot_revision=source_revision,
    )
    payload = json.loads(first.body)
    assert payload["resonance_revision"] == resonance.batch.resonance_revision
    assert (
        payload["resonance_source_snapshot_revision"]
        == resonance.batch.source_snapshot_revision
    )
    assert payload["resonance_as_of"] == resonance.batch.source_as_of.isoformat()
    electric_power = payload["sector_flow_trajectory"]["sectors"][0]
    assert (
        electric_power["leader_snapshot"]["selection_method"]
        == "sector_fund_flow_path_resonance.v2"
    )

    resonance.batch = _resonance_batch(
        snapshot,
        source_revision,
        status_label="更新后的无匹配",
    )
    refreshed = service.get_summary(
        expected_source_snapshot_revision=source_revision,
        if_none_match=first.etag,
    )
    updated = json.loads(refreshed.body)
    assert refreshed.status_code == 200
    assert refreshed.etag != first.etag
    assert (
        updated["sector_flow_trajectory"]["sectors"][0]["leader_snapshot"][
            "status_label"
        ]
        == "更新后的无匹配"
    )


def test_payload_service_detail_is_exact_revision_bound_and_selection_cached() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    trajectory = snapshot.offense_sector_flow_trajectory
    assert trajectory is not None
    trajectory_revision = stable_sha256(trajectory)
    collection = _CollectionReader(_collector_envelope(snapshot, source_revision))
    history = _HistoryReader(snapshot, source_revision)
    service = MarketWatchWebPayloadService(
        MarketWatchReadFacade(
            collection_reader=collection,
            history_reader=history,
            clock=lambda: snapshot.as_of + timedelta(minutes=1),
        )
    )

    first = service.get_detail(
        direction="offense",
        sector_keys=("software", "semiconductor"),
        expected_source_snapshot_revision=source_revision,
        expected_trajectory_revision=trajectory_revision,
    )
    unchanged = service.get_detail(
        direction="offense",
        sector_keys=("semiconductor", "software"),
        expected_source_snapshot_revision=source_revision,
        expected_trajectory_revision=trajectory_revision,
        if_none_match=first.etag,
    )

    payload = json.loads(first.body)
    assert first.status_code == 200
    assert first.trajectory_revision == trajectory_revision
    assert payload["source_snapshot_revision"] == source_revision
    assert payload["trajectory_revision"] == trajectory_revision
    assert payload["point_count"] == sum(len(item.points) for item in trajectory.sectors)
    assert payload["sectors"] == [
        item.model_dump(mode="json") for item in trajectory.sectors
    ]
    assert unchanged.status_code == 304
    assert unchanged.cache_hit is True
    assert history.payload_calls == 1


def test_payload_service_returns_typed_409_503_and_422_fail_closed_errors() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    trajectory = snapshot.sector_flow_trajectory
    assert trajectory is not None
    collection = _CollectionReader(_collector_envelope(snapshot, source_revision))
    history = _HistoryReader(snapshot, source_revision)
    service = MarketWatchWebPayloadService(
        MarketWatchReadFacade(
            collection_reader=collection,
            history_reader=history,
            clock=lambda: snapshot.as_of + timedelta(minutes=1),
        )
    )

    with pytest.raises(WebRevisionConflict) as source_error:
        service.get_summary(expected_source_snapshot_revision="0" * 64)
    assert source_error.value.status_code == 409
    assert source_error.value.payload.scope == "source_snapshot"
    assert source_error.value.payload.action == "discard_batch_and_retry"

    with pytest.raises(WebRevisionConflict) as trajectory_error:
        service.get_detail(
            direction="defense",
            sector_keys=("electric_power",),
            expected_source_snapshot_revision=source_revision,
            expected_trajectory_revision="0" * 64,
        )
    assert trajectory_error.value.status_code == 409
    assert trajectory_error.value.payload.scope == "trajectory"
    assert trajectory_error.value.payload.current_revision == stable_sha256(trajectory)

    with pytest.raises(WebSectorSelectionRejected) as selection_error:
        service.get_detail(
            direction="defense",
            sector_keys=("electric_power", "missing_sector"),
            expected_source_snapshot_revision=source_revision,
            expected_trajectory_revision=stable_sha256(trajectory),
        )
    assert selection_error.value.status_code == 422

    unavailable_collection = _CollectionReader(
        _collector_envelope(
            snapshot,
            source_revision,
            include_accepted_real=False,
        )
    )
    unavailable_history = _HistoryReader(snapshot, source_revision)
    unavailable_service = MarketWatchWebPayloadService(
        MarketWatchReadFacade(
            collection_reader=unavailable_collection,
            history_reader=unavailable_history,
            clock=lambda: snapshot.as_of + timedelta(minutes=1),
        )
    )
    with pytest.raises(WebAcceptedRealUnavailable) as unavailable_error:
        unavailable_service.get_summary(
            expected_source_snapshot_revision=source_revision,
        )
    assert unavailable_error.value.status_code == 503
    assert unavailable_error.value.payload.reason == "no_accepted_real"
    assert unavailable_history.payload_calls == 0


def test_web_api_maps_summary_detail_etag_and_304_headers() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    trajectory = snapshot.sector_flow_trajectory
    assert trajectory is not None
    trajectory_revision = stable_sha256(trajectory)
    api = MarketWatchWebApi(
        MarketWatchWebPayloadService(
            MarketWatchReadFacade(
                collection_reader=_CollectionReader(
                    _collector_envelope(snapshot, source_revision)
                ),
                history_reader=_HistoryReader(snapshot, source_revision),
                clock=lambda: snapshot.as_of + timedelta(minutes=1),
            )
        )
    )

    summary = api.get_summary(source_snapshot_revision=source_revision)
    summary_headers = dict(summary.headers)
    unchanged = api.get_summary(
        source_snapshot_revision=source_revision,
        if_none_match=summary_headers["ETag"],
    )
    detail = api.get_detail(
        direction="defense",
        sector_keys=("electric_power",),
        source_snapshot_revision=source_revision,
        trajectory_revision=trajectory_revision,
    )
    detail_headers = dict(detail.headers)

    assert summary.status_code == 200
    assert summary_headers["Cache-Control"] == "private, no-cache"
    assert summary_headers["X-Source-Snapshot-Revision"] == source_revision
    assert unchanged.status_code == 304
    assert unchanged.body == b""
    assert detail.status_code == 200
    assert detail_headers["X-Source-Snapshot-Revision"] == source_revision
    assert detail_headers["X-Trajectory-Revision"] == trajectory_revision


def test_web_api_serves_etagged_five_day_minutes_from_retained_history() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    api = MarketWatchWebApi(
        MarketWatchWebPayloadService(
            MarketWatchReadFacade(
                collection_reader=_CollectionReader(
                    _collector_envelope(snapshot, source_revision)
                ),
                history_reader=_HistoryReader(snapshot, source_revision),
                clock=lambda: snapshot.as_of + timedelta(minutes=1),
            )
        )
    )

    first = api.get_five_day_trajectory(
        direction="defense",
        sector_keys=("electric_power",),
    )
    payload = json.loads(first.body)
    headers = dict(first.headers)
    unchanged = api.get_five_day_trajectory(
        direction="defense",
        sector_keys=("electric_power",),
        if_none_match=headers["ETag"],
    )

    assert first.status_code == 200
    assert payload["contract"] == "sector_flow_five_day_trajectory.v1"
    assert payload["available_trade_days"] == 1
    assert payload["trade_dates"] == [snapshot.as_of.date().isoformat()]
    assert payload["point_count"] == 3
    assert headers["X-Trajectory-Revision"] == payload["history_revision"]
    assert unchanged.status_code == 304
    assert unchanged.body == b""


def test_web_api_uses_materialized_daily_projection_without_full_snapshot_decode(
    tmp_path,
    monkeypatch,
) -> None:
    base = _snapshot()
    payload = base.model_dump(mode="python")
    payload["market_state"] = {
        **payload["market_state"],
        "phase": "closed",
        "is_open": False,
    }
    for field in ("sector_flow_trajectory", "offense_sector_flow_trajectory"):
        payload[field] = {**payload[field], "market_phase": "closed"}
    snapshot = MarketWatchSnapshotV1.model_validate(payload)
    db_path = tmp_path / "projection-fast-path.sqlite3"
    with MarketWatchHistoryStore(db_path, clock=lambda: snapshot.as_of) as owner:
        persisted = owner.record(snapshot)

    with MarketWatchHistoryStore(db_path, read_only=True) as history:
        def fail_full_snapshot_decode(**_pointer):
            raise AssertionError("five-day fast path decoded the full snapshot")

        monkeypatch.setattr(history, "get_snapshot_by_pointer", fail_full_snapshot_decode)
        api = MarketWatchWebApi(
            MarketWatchWebPayloadService(
                MarketWatchReadFacade(
                    collection_reader=_CollectionReader(
                        _collector_envelope(snapshot, persisted["payload_digest"])
                    ),
                    history_reader=history,
                    clock=lambda: snapshot.as_of + timedelta(minutes=1),
                )
            )
        )
        response = api.get_five_day_trajectory(
            direction="defense",
            sector_keys=("electric_power",),
        )

    body = json.loads(response.body)
    fallback = MarketWatchWebApi(
        MarketWatchWebPayloadService(
            MarketWatchReadFacade(
                collection_reader=_CollectionReader(
                    _collector_envelope(snapshot, persisted["payload_digest"])
                ),
                history_reader=_HistoryReader(snapshot, persisted["payload_digest"]),
                clock=lambda: snapshot.as_of + timedelta(minutes=1),
            )
        )
    ).get_five_day_trajectory(
        direction="defense",
        sector_keys=("electric_power",),
    )
    assert response.status_code == 200
    assert body == json.loads(fallback.body)
    assert body["contract"] == "sector_flow_five_day_trajectory.v1"
    assert body["available_trade_days"] == 1
    assert body["days"][0]["source_snapshot_revision"] == persisted["payload_digest"]
    assert body["days"][0]["sectors"][0]["sector_key"] == "electric_power"


def test_web_summary_integrity_and_detail_accept_64_sector_slots() -> None:
    snapshot = _snapshot()
    trajectory = snapshot.offense_sector_flow_trajectory
    assert trajectory is not None
    payload = trajectory.model_dump(mode="python")
    base = payload["sectors"][0]
    payload["sectors"] = []
    for rank in range(1, 65):
        item = deepcopy(base)
        item.update({
            "sector_key": f"capacity_{rank:02d}",
            "name": f"容量方向{rank}",
            "observation_rank": rank,
            "rank_total": 64,
        })
        payload["sectors"].append(item)
    expanded = trajectory.__class__.model_validate(payload)
    snapshot = snapshot.model_copy(
        update={"offense_sector_flow_trajectory": expanded}
    )
    source_revision = stable_sha256(snapshot)
    trajectory_revision = stable_sha256(expanded)
    sector_keys = tuple(item.sector_key for item in expanded.sectors)
    api = MarketWatchWebApi(
        MarketWatchWebPayloadService(
            MarketWatchReadFacade(
                collection_reader=_CollectionReader(
                    _collector_envelope(snapshot, source_revision)
                ),
                history_reader=_HistoryReader(snapshot, source_revision),
                clock=lambda: snapshot.as_of + timedelta(minutes=1),
            )
        )
    )

    summary_response = api.get_summary(source_snapshot_revision=source_revision)
    detail_response = api.get_detail(
        direction="offense",
        sector_keys=sector_keys,
        source_snapshot_revision=source_revision,
        trajectory_revision=trajectory_revision,
    )
    summary = json.loads(summary_response.body)
    detail = json.loads(detail_response.body)

    assert summary_response.status_code == 200
    assert summary["offense_sector_flow_trajectory"]["sector_count"] == 64
    assert summary["payload_integrity"]["offense"]["sector_count"] == 64
    assert detail_response.status_code == 200
    assert detail["sector_count"] == 64
    assert len(detail["sectors"]) == 64


def test_web_api_maps_invalid_conflict_selection_and_unavailable_statuses() -> None:
    snapshot = _snapshot()
    source_revision = stable_sha256(snapshot)
    trajectory = snapshot.sector_flow_trajectory
    assert trajectory is not None
    api = MarketWatchWebApi(
        MarketWatchWebPayloadService(
            MarketWatchReadFacade(
                collection_reader=_CollectionReader(
                    _collector_envelope(snapshot, source_revision)
                ),
                history_reader=_HistoryReader(snapshot, source_revision),
                clock=lambda: snapshot.as_of + timedelta(minutes=1),
            )
        )
    )

    invalid = api.get_summary(source_snapshot_revision=None)
    conflict = api.get_summary(source_snapshot_revision="0" * 64)
    selection = api.get_detail(
        direction="defense",
        sector_keys=("missing_sector",),
        source_snapshot_revision=source_revision,
        trajectory_revision=stable_sha256(trajectory),
    )

    assert invalid.status_code == 400
    assert json.loads(invalid.body)["field"] == "source_snapshot_revision"
    assert conflict.status_code == 409
    assert json.loads(conflict.body)["action"] == "discard_batch_and_retry"
    assert selection.status_code == 422

    unavailable_api = MarketWatchWebApi(
        MarketWatchWebPayloadService(
            MarketWatchReadFacade(
                collection_reader=_CollectionReader(
                    _collector_envelope(
                        snapshot,
                        source_revision,
                        include_accepted_real=False,
                    )
                ),
                history_reader=_HistoryReader(snapshot, source_revision),
                clock=lambda: snapshot.as_of + timedelta(minutes=1),
            )
        )
    )
    unavailable = unavailable_api.get_summary(
        source_snapshot_revision=source_revision,
    )
    assert unavailable.status_code == 503
    assert dict(unavailable.headers)["Retry-After"] == "5"
    assert json.loads(unavailable.body)["reason"] == "no_accepted_real"
