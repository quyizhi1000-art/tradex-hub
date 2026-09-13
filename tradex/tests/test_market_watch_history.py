from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

import tradex.market_watch.history as history_module
from tradex.market_calendar import CalendarDayStatus, calendar_day_status
from tradex.market_watch import AlertV1, MarketWatchSnapshotV1, build_market_watch_snapshot
from tradex.market_watch.collection import build_collection_gap_snapshots
from tradex.market_watch.history import (
    DEFAULT_CONFIG_VERSION,
    ENV_DB_PATH,
    HISTORY_CONTRACT,
    HISTORY_SCHEMA_VERSION,
    MarketWatchHistoryStore,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _history_fingerprint(
    db_path: Path,
) -> tuple[int, tuple[str, ...], int, int]:
    modified_at = db_path.stat().st_mtime_ns
    with sqlite3.connect(db_path) as connection:
        tables = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "ORDER BY name"
            ).fetchall()
        )
        snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM market_watch_snapshots"
        ).fetchone()[0]
        alert_count = connection.execute(
            "SELECT COUNT(*) FROM market_watch_alert_events"
        ).fetchone()[0]
    return modified_at, tables, int(snapshot_count), int(alert_count)


def test_read_only_history_lazily_recovers_after_collector_creates_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "late-history.sqlite3"
    observed = datetime(2026, 8, 24, 10, 30, 5, tzinfo=SHANGHAI)
    reader = MarketWatchHistoryStore(db_path, read_only=True)
    try:
        assert reader.get_timeline(observed.date()) == []
        assert not db_path.exists()

        with sqlite3.connect(db_path) as partial:
            partial.execute("CREATE TABLE collector_bootstrap_in_progress (id INTEGER)")
        assert reader.get_timeline(observed.date()) == []
        assert reader._connection is None

        snapshot = _snapshot(
            observed,
            snapshot_id="mw-late-history",
            sequence=1,
        )
        with MarketWatchHistoryStore(db_path, clock=lambda: observed) as owner:
            owner.record(snapshot)
        before = _history_fingerprint(db_path)

        real_connect = sqlite3.connect
        connect_count = 0
        count_lock = threading.Lock()

        def counting_connect(*args, **kwargs):
            nonlocal connect_count
            if kwargs.get("uri") is True:
                with count_lock:
                    connect_count += 1
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", counting_connect)
        with ThreadPoolExecutor(max_workers=8) as executor:
            timelines = list(
                executor.map(
                    lambda _index: reader.get_timeline(observed.date()),
                    range(16),
                )
            )

        assert connect_count == 1
        assert all(
            len(items) == 1
            and items[0]["payload"]["snapshot_id"] == "mw-late-history"
            for items in timelines
        )
        assert _history_fingerprint(db_path) == before
    finally:
        reader.close()


def _sector_flow_trajectory(observed_at: datetime, *, direction: str) -> dict:
    previous_at = observed_at - timedelta(minutes=1)
    sector_key = "electric_power" if direction == "defense" else "semiconductor"
    name = "电力" if direction == "defense" else "半导体"
    category_key = "steady_defense" if direction == "defense" else "technology_growth"
    category_name = "稳态防御" if direction == "defense" else "科技成长"
    points = [
        {
            "sampled_at": previous_at.isoformat(),
            "provider_as_of": previous_at.isoformat(),
            "session_segment": "am",
            "cumulative_cny": 500_000_000.0,
            "delta_5m_cny": None,
            "delta_5m_baseline_as_of": None,
        },
        {
            "sampled_at": observed_at.isoformat(),
            "provider_as_of": observed_at.isoformat(),
            "session_segment": "am",
            "cumulative_cny": 600_000_000.0,
            "delta_5m_cny": None,
            "delta_5m_baseline_as_of": None,
        },
    ]
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
        "sectors": [{
            "sector_key": sector_key,
            "name": name,
            "category_key": category_key,
            "category_name": category_name,
            "taxonomy": "industry",
            "status": "collecting",
            "follow_eligible": True,
            "eligible_for_rank": True,
            "observation_rank": 1,
            "rank_total": 1,
            "observation_tier": "strong_pending",
            "tier_label": "强势待确认",
            "latest": {
                "provider_as_of": observed_at.isoformat(),
                "change_pct": 1.2,
                "breadth_ratio": 0.68,
                "cumulative_cny": 600_000_000.0,
                "main_net_inflow_pct": 2.4,
                "price_percentile": 0.88,
                "flow_percentile": 0.82,
                "delta_5m_cny": None,
                "delta_10m_cny": None,
                "delta_5m_baseline_as_of": None,
                "delta_10m_baseline_as_of": None,
                "current_strength": "strong",
                "fund_strength": "strong",
                "incremental_direction": "unknown",
            },
            "points": points,
            "supporting_evidence": ["当日累计估算净流入"],
            "counter_evidence": ["近5分钟同源基线仍在积累"],
            "flags": ["five_minute_baseline_collecting"],
            "reason": None,
        }],
        "flags": ["provider_estimated_flow"],
        "reason": None,
    }


def _snapshot(
    observed_at: datetime,
    *,
    snapshot_id: str,
    sequence: int,
    alert_code: str | None = None,
) -> MarketWatchSnapshotV1:
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
            "previous_date": "2026-08-21",
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
            "sectors": [
                {
                    "sector_key": "industry:securities",
                    "name": "证券",
                    "tags": ["attack"],
                    "change_pct": 1.5,
                    "breadth_ratio": 0.66,
                    "main_net_inflow_cny": 5_000_000_000.0,
                    "provider_as_of": observed_at.isoformat(),
                }
            ]
        },
        "sector_flow_trajectory": _sector_flow_trajectory(
            observed_at,
            direction="defense",
        ),
        "offense_sector_flow_trajectory": _sector_flow_trajectory(
            observed_at,
            direction="offense",
        ),
    }
    built = build_market_watch_snapshot(
        market,
        risk,
        as_of=observed_at,
        sequence=sequence,
        snapshot_id=snapshot_id,
    )
    if alert_code is None:
        return built
    alert = AlertV1(
        code=alert_code,
        severity="stop" if alert_code.endswith("stop") else "caution",
        title=f"提醒 {alert_code}",
        message=f"事件 {alert_code} 已确认。",
        dedupe_key=f"market_watch:{alert_code}",
    )
    payload = built.model_dump(mode="python")
    payload["alerts"] = (alert,)
    return MarketWatchSnapshotV1.model_validate(payload)


def _closed_snapshot(
    observed_at: datetime,
    *,
    snapshot_id: str,
    sequence: int,
) -> MarketWatchSnapshotV1:
    snapshot = _snapshot(observed_at, snapshot_id=snapshot_id, sequence=sequence)
    payload = snapshot.model_dump(mode="python")
    payload["market_state"] = {
        **payload["market_state"],
        "phase": "closed",
        "is_open": False,
    }
    for field in ("sector_flow_trajectory", "offense_sector_flow_trajectory"):
        payload[field] = {**payload[field], "market_phase": "closed"}
    return MarketWatchSnapshotV1.model_validate(payload)


def test_record_replays_the_complete_strict_payload_as_json_safe(tmp_path: Path) -> None:
    observed_at = datetime(2026, 8, 24, 10, 30, 8, tzinfo=SHANGHAI)
    snapshot = _snapshot(observed_at, snapshot_id="mw-1", sequence=1)
    store = MarketWatchHistoryStore(tmp_path / "history.sqlite3")
    try:
        result = store.record(snapshot)
        timeline = store.get_timeline("2026-08-24")

        assert result["action"] == "inserted"
        assert result["history_contract"] == HISTORY_CONTRACT
        assert result["history_schema_version"] == HISTORY_SCHEMA_VERSION
        assert result["config_version"] == DEFAULT_CONFIG_VERSION
        assert result["minute_bucket"] == "2026-08-24T10:30:00+08:00"
        assert result["payload_bytes"] > 0
        assert len(result["payload_digest"]) == 64
        assert len(timeline) == 1
        assert timeline[0]["payload"] == snapshot.model_dump(mode="json")
        assert len(timeline[0]["payload"]["sector_flow_trajectory"]["sectors"][0]["points"]) == 2
        assert len(timeline[0]["payload"]["offense_sector_flow_trajectory"]["sectors"][0]["points"]) == 2
        canonical_bytes = json.dumps(
            timeline[0]["payload"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        assert hashlib.sha256(canonical_bytes).hexdigest() == result["payload_digest"]
        assert timeline[0]["history_schema_version"] == HISTORY_SCHEMA_VERSION
        assert timeline[0]["config_version"] == DEFAULT_CONFIG_VERSION
        json.dumps(timeline, ensure_ascii=False, allow_nan=False)
    finally:
        store.close()


def test_closed_snapshot_materializes_selected_daily_sector_flow_series(
    tmp_path: Path,
) -> None:
    observed_at = datetime(2026, 8, 24, 15, 0, tzinfo=SHANGHAI)
    snapshot = _closed_snapshot(
        observed_at,
        snapshot_id="mw-closed-projection",
        sequence=238,
    )
    db_path = tmp_path / "daily-sector-flow.sqlite3"

    with MarketWatchHistoryStore(db_path, clock=lambda: observed_at) as owner:
        result = owner.record(snapshot)
        projection = owner.get_daily_sector_flow_projection(
            observed_at.date(),
            direction="defense",
            sector_keys=("electric_power",),
        )

    assert projection is not None
    assert projection["contract"] == "sector_flow_daily_projection.v1"
    assert projection["source_snapshot_revision"] == result["payload_digest"]
    assert projection["direction"] == "defense"
    assert projection["trade_date"] == observed_at.date().isoformat()
    assert projection["minute_bucket"] == observed_at.isoformat()
    assert [item["sector_key"] for item in projection["sectors"]] == [
        "electric_power"
    ]
    assert len(projection["sectors"][0]["points"]) == 2

    with MarketWatchHistoryStore(db_path, read_only=True) as reader:
        reopened = reader.get_daily_sector_flow_projection(
            observed_at.date(),
            direction="offense",
            sector_keys=("semiconductor",),
        )
    assert reopened is not None
    assert reopened["source_snapshot_revision"] == result["payload_digest"]
    assert reopened["sectors"][0]["sector_key"] == "semiconductor"


def test_writable_reopen_backfills_missing_recent_close_projection(
    tmp_path: Path,
) -> None:
    observed_at = datetime(2026, 8, 24, 15, 0, tzinfo=SHANGHAI)
    snapshot = _closed_snapshot(
        observed_at,
        snapshot_id="mw-legacy-close",
        sequence=238,
    )
    db_path = tmp_path / "legacy-close.sqlite3"
    with MarketWatchHistoryStore(db_path, clock=lambda: observed_at) as owner:
        owner.record(snapshot)
        with owner._connection:
            owner._connection.execute("DELETE FROM market_watch_daily_sector_flow")
            owner._connection.execute(
                "DELETE FROM market_watch_daily_sector_flow_series"
            )

    with MarketWatchHistoryStore(db_path, clock=lambda: observed_at) as migrated:
        projection = migrated.get_daily_sector_flow_projection(
            observed_at.date(),
            direction="defense",
            sector_keys=("electric_power",),
        )
    assert projection is not None
    assert projection["snapshot_id"] == "mw-legacy-close"


def test_replay_timeline_uses_compact_metadata_without_decoding_snapshot_blob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = datetime(2026, 8, 24, 10, 30, 8, tzinfo=SHANGHAI)
    snapshot = _snapshot(observed_at, snapshot_id="mw-compact", sequence=7)
    store = MarketWatchHistoryStore(tmp_path / "compact-replay.sqlite3")
    try:
        store.record(snapshot)

        def fail_decode(_payload_blob: bytes) -> dict:
            raise AssertionError("compact replay must not decode the full snapshot blob")

        monkeypatch.setattr(history_module, "_decode_snapshot", fail_decode)
        timeline = store.get_replay_timeline("2026-08-24")

        assert len(timeline) == 1
        assert timeline[0]["snapshot_id"] == "mw-compact"
        replay = timeline[0]["payload"]
        assert replay == {
            "contract": "market_watch_replay_sample.v1",
            "schema_version": 1,
            "snapshot_id": "mw-compact",
            "sequence": 7,
            "as_of": observed_at.isoformat(),
            "market_state": {
                "phase": "trading",
                "is_open": True,
                "trading_date": "2026-08-24",
            },
            "freshness": {"status": "fresh"},
            "guardrail": {"regime": "attack", "severity": "calm"},
            "alerts": [],
        }
        assert "indices" not in replay
        assert "sector_flow_trajectory" not in replay
        assert len(json.dumps(timeline, ensure_ascii=False)) < 2_000
    finally:
        store.close()


def test_read_only_history_never_creates_or_mutates_storage(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing" / "history.sqlite3"
    observed = datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI)
    with MarketWatchHistoryStore(missing_path, read_only=True) as reader:
        assert reader.list_dates() == []
        assert reader.get_timeline(observed.date()) == []
        assert reader.get_collection_records(observed.date()) == []
        assert reader.get_snapshot_by_pointer(
            trade_date=observed.date(),
            minute_bucket=observed,
            snapshot_id="mw-missing",
            payload_digest="0" * 64,
        ) is None
        with pytest.raises(RuntimeError, match="read-only"):
            reader.record(_snapshot(observed, snapshot_id="mw-forbidden", sequence=1))
        with pytest.raises(RuntimeError, match="read-only"):
            reader.apply_retention()
    assert not missing_path.exists()
    assert not missing_path.parent.exists()

    tableless_path = tmp_path / "tableless.sqlite3"
    with sqlite3.connect(tableless_path):
        pass
    tableless_before = tableless_path.stat().st_mtime_ns
    with MarketWatchHistoryStore(tableless_path, read_only=True) as reader:
        assert reader.list_dates() == []
        assert reader.get_timeline(observed.date()) == []
    with sqlite3.connect(tableless_path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall() == []
    assert tableless_path.stat().st_mtime_ns == tableless_before

    db_path = tmp_path / "existing.sqlite3"
    snapshot = _snapshot(observed, snapshot_id="mw-readable", sequence=2)
    with MarketWatchHistoryStore(db_path) as writer:
        recorded = writer.record(snapshot)
    before = _history_fingerprint(db_path)

    with MarketWatchHistoryStore(db_path, read_only=True) as reader:
        item = reader.get_snapshot_by_pointer(
            trade_date=observed.date(),
            minute_bucket=observed,
            snapshot_id=recorded["snapshot_id"],
            payload_digest=recorded["payload_digest"],
        )
        assert item is not None
        assert item["payload"]["snapshot_id"] == "mw-readable"
        with pytest.raises(RuntimeError, match="read-only"):
            reader.record(snapshot)
        with pytest.raises(RuntimeError, match="read-only"):
            reader.apply_retention(1)

    after = _history_fingerprint(db_path)
    assert before == after
    assert before[2:] == (1, 0)


def test_same_minute_update_keeps_old_alerts_and_adds_new_events(tmp_path: Path) -> None:
    first = _snapshot(
        datetime(2026, 8, 24, 10, 30, 5, tzinfo=SHANGHAI),
        snapshot_id="mw-first",
        sequence=1,
        alert_code="breadth_warning",
    )
    second = _snapshot(
        datetime(2026, 8, 24, 10, 30, 50, tzinfo=SHANGHAI),
        snapshot_id="mw-second",
        sequence=2,
        alert_code="risk_stop",
    )
    store = MarketWatchHistoryStore(tmp_path / "upsert.sqlite3")
    try:
        assert store.record(first)["alerts_added"] == 1
        updated = store.record(second)
        repeated = store.record(second)

        assert updated["action"] == "updated"
        assert updated["alerts_added"] == 1
        assert repeated["action"] == "unchanged"
        assert repeated["alerts_added"] == 0

        timeline = store.get_timeline("2026-08-24")
        alerts = store.get_alerts("2026-08-24")
        assert len(timeline) == 1
        assert timeline[0]["payload"]["snapshot_id"] == "mw-second"
        assert [item["code"] for item in alerts] == [
            "breadth_warning",
            "risk_stop",
        ]
        assert alerts[0]["snapshot_id"] == "mw-first"
        assert alerts[1]["snapshot_id"] == "mw-second"
        assert alerts[0]["alert"]["dedupe_key"] == "market_watch:breadth_warning"
    finally:
        store.close()


def test_gap_snapshot_never_overwrites_a_real_same_minute_sample(tmp_path: Path) -> None:
    observed_at = datetime(2026, 8, 24, 10, 30, 5, tzinfo=SHANGHAI)
    real = _snapshot(observed_at, snapshot_id="mw-real", sequence=1)
    replacement = _snapshot(
        observed_at.replace(second=50),
        snapshot_id="mw-gap",
        sequence=1,
    )

    with MarketWatchHistoryStore(tmp_path / "preserve.sqlite3") as store:
        assert store.record(real)["action"] == "inserted"
        result = store.record(replacement, overwrite=False)
        timeline = store.get_timeline("2026-08-24")

    assert result["action"] == "preserved"
    assert timeline[0]["payload"]["snapshot_id"] == "mw-real"


def test_collection_gap_snapshots_fill_only_missing_continuous_session_minutes() -> None:
    started = datetime(2026, 8, 24, 11, 29, 10, tzinfo=SHANGHAI)
    completed = datetime(2026, 8, 24, 13, 2, 40, tzinfo=SHANGHAI)
    anchor = _snapshot(started, snapshot_id="mw-anchor", sequence=7)

    gap_snapshots = build_collection_gap_snapshots(
        anchor,
        started_at=started,
        completed_at=completed,
    )

    assert [item.as_of.strftime("%H:%M") for item in gap_snapshots] == [
        "13:00",
        "13:01",
        "13:02",
    ]
    assert all(item.freshness.status.value == "stale" for item in gap_snapshots)
    assert all(item.guardrail.severity.value == "stop" for item in gap_snapshots)
    assert all(item.scenarios == () for item in gap_snapshots)
    assert all(
        "collection_gap_backfilled" in item.freshness.flags
        for item in gap_snapshots
    )


def test_collection_records_are_lightweight_ordered_and_prefix_identifies_heartbeat(
    tmp_path: Path,
) -> None:
    first_minute = datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI)
    real = _snapshot(first_minute, snapshot_id="mw-real", sequence=1)
    gaps = build_collection_gap_snapshots(
        real,
        started_at=first_minute,
        completed_at=first_minute + timedelta(minutes=2),
    )
    heartbeat = gaps[0]
    stale_without_reserved_id = gaps[1].model_copy(
        update={"snapshot_id": "mw-stale-real", "sequence": 2}
    )

    with MarketWatchHistoryStore(tmp_path / "collection-records.sqlite3") as store:
        store.record(stale_without_reserved_id)
        store.record(real)
        store.record(heartbeat, overwrite=False)
        records = store.get_collection_records(first_minute.date())

    assert [item["minute_bucket"] for item in records] == [
        "2026-08-24T10:30:00+08:00",
        "2026-08-24T10:31:00+08:00",
        "2026-08-24T10:32:00+08:00",
    ]
    assert [item["record_kind"] for item in records] == [
        "accepted_real",
        "derived_gap_heartbeat",
        "stale_snapshot",
    ]
    assert all("payload" not in item and "payload_blob" not in item for item in records)
    assert records[2]["freshness_status"] == "stale"


def test_exact_pointer_reads_accepted_row_before_later_heartbeat_without_scanning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    accepted_minute = datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI)
    accepted = _snapshot(accepted_minute, snapshot_id="mw-accepted", sequence=1)
    heartbeat = build_collection_gap_snapshots(
        accepted,
        started_at=accepted_minute,
        completed_at=accepted_minute + timedelta(minutes=1),
    )[0]
    with MarketWatchHistoryStore(tmp_path / "exact-pointer.sqlite3") as store:
        accepted_record = store.record(accepted)
        store.record(heartbeat, overwrite=False)

        actual = store.get_snapshot_by_pointer(
            trade_date=accepted_minute.date(),
            minute_bucket=accepted_record["minute_bucket"],
            snapshot_id=accepted_record["snapshot_id"],
            payload_digest=accepted_record["payload_digest"],
        )

        monkeypatch.setattr(
            "tradex.market_watch.history.zlib.decompress",
            lambda _payload: (_ for _ in ()).throw(
                AssertionError("pointer mismatch must be rejected before decompression")
            ),
        )
        mismatch = store.get_snapshot_by_pointer(
            trade_date=accepted_minute.date(),
            minute_bucket=accepted_record["minute_bucket"],
            snapshot_id="mw-wrong",
            payload_digest=accepted_record["payload_digest"],
        )

    assert actual is not None
    assert actual["payload"]["snapshot_id"] == "mw-accepted"
    assert actual["payload_digest"] == accepted_record["payload_digest"]
    assert mismatch is None


def test_exact_pointer_preserves_legacy_canonical_payload_missing_new_defaults(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-exact-pointer.sqlite3"
    observed = datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI)
    snapshot = _snapshot(
        observed,
        snapshot_id="mw-legacy-alert",
        sequence=1,
        alert_code="breadth_warning",
    )
    with MarketWatchHistoryStore(db_path) as store:
        recorded = store.record(snapshot)

    legacy_payload = snapshot.model_dump(mode="json")
    for field in (
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
        legacy_payload["alerts"][0].pop(field, None)
    legacy_raw = json.dumps(
        legacy_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    legacy_digest = hashlib.sha256(legacy_raw).hexdigest()
    legacy_blob = zlib.compress(legacy_raw, level=6)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE market_watch_snapshots
            SET payload_digest = ?, payload_bytes = ?, payload_blob = ?
            WHERE trade_date = ? AND minute_bucket = ?
            """,
            (
                legacy_digest,
                len(legacy_blob),
                legacy_blob,
                recorded["trade_date"],
                recorded["minute_bucket"],
            ),
        )

    with MarketWatchHistoryStore(db_path) as store:
        actual = store.get_snapshot_by_pointer(
            trade_date=recorded["trade_date"],
            minute_bucket=recorded["minute_bucket"],
            snapshot_id=recorded["snapshot_id"],
            payload_digest=legacy_digest,
        )

    assert actual is not None
    assert actual["payload_digest"] == legacy_digest
    assert actual["payload"] == legacy_payload
    assert "kind" not in actual["payload"]["alerts"][0]


def test_invalid_noncanonical_mapping_is_rejected_without_writing(tmp_path: Path) -> None:
    snapshot = _snapshot(
        datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI),
        snapshot_id="mw-valid",
        sequence=1,
    )
    invalid = {**snapshot.model_dump(mode="json"), "provider": "must-not-leak"}
    store = MarketWatchHistoryStore(tmp_path / "strict.sqlite3")
    try:
        with pytest.raises(ValidationError):
            store.record(invalid)
        assert store.list_dates() == []
    finally:
        store.close()


def test_lists_dates_and_limits_recent_rows_in_chronological_order(tmp_path: Path) -> None:
    store = MarketWatchHistoryStore(tmp_path / "dates.sqlite3")
    try:
        for day, hour, minute, sequence in (
            (24, 10, 30, 1),
            (24, 10, 31, 2),
            (24, 10, 32, 3),
            (25, 10, 30, 4),
        ):
            observed_at = datetime(2026, 8, day, hour, minute, tzinfo=SHANGHAI)
            store.record(
                _snapshot(
                    observed_at,
                    snapshot_id=f"mw-{sequence}",
                    sequence=sequence,
                )
            )

        dates = store.list_dates(limit=1)
        latest = store.get_timeline("2026-08-24", limit=2)
        assert [item["trade_date"] for item in dates] == ["2026-08-25"]
        assert dates[0]["snapshot_count"] == 1
        assert [item["payload"]["snapshot_id"] for item in latest] == [
            "mw-2",
            "mw-3",
        ]
        assert [item["minute_bucket"] for item in latest] == sorted(
            item["minute_bucket"] for item in latest
        )
        matched = store.find_latest_snapshot(
            lambda payload: payload["snapshot_id"] == "mw-2"
        )
        assert matched is not None
        assert matched["payload"]["snapshot_id"] == "mw-2"
    finally:
        store.close()


def test_retention_prunes_complete_old_dates_including_alerts(tmp_path: Path) -> None:
    store = MarketWatchHistoryStore(
        tmp_path / "retention.sqlite3",
        retention_trade_days=2,
    )
    try:
        for day in (24, 25, 26):
            observed_at = datetime(2026, 8, day, 10, 30, tzinfo=SHANGHAI)
            store.record(
                _snapshot(
                    observed_at,
                    snapshot_id=f"mw-{day}",
                    sequence=day,
                    alert_code=f"warning_{day}",
                )
            )

        assert [item["trade_date"] for item in store.list_dates()] == [
            "2026-08-26",
            "2026-08-25",
        ]
        assert store.get_timeline("2026-08-24") == []
        assert store.get_alerts("2026-08-24") == []
        assert len(store.get_alerts("2026-08-25")) == 1
    finally:
        store.close()


def test_env_path_persists_across_reopen_and_close_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "configured" / "market-watch.sqlite3"
    monkeypatch.setenv(ENV_DB_PATH, str(db_path))
    snapshot = _snapshot(
        datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI),
        snapshot_id="mw-persisted",
        sequence=1,
    )

    first = MarketWatchHistoryStore()
    assert Path(first.db_path) == db_path.resolve()
    first.record(snapshot)
    first.close()
    first.close()

    second = MarketWatchHistoryStore()
    try:
        assert second.get_timeline("2026-08-24")[0]["payload"]["snapshot_id"] == "mw-persisted"
    finally:
        second.close()

    with pytest.raises(RuntimeError, match="closed"):
        second.list_dates()
    with pytest.raises(RuntimeError, match="closed"):
        second.get_timeline("2026-02-16")
    with pytest.raises(RuntimeError, match="closed"):
        second.get_alerts("2026-02-16")
    with pytest.raises(RuntimeError, match="closed"):
        second.record(
            _snapshot(
                datetime(2026, 2, 16, 10, 0, tzinfo=SHANGHAI),
                snapshot_id="mw-closed-holiday",
                sequence=2,
            )
        )


def test_config_versions_have_independent_snapshots_and_alert_events(tmp_path: Path) -> None:
    db_path = tmp_path / "versions.sqlite3"
    snapshot = _snapshot(
        datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI),
        snapshot_id="mw-versioned",
        sequence=1,
        alert_code="versioned_warning",
    )
    first = MarketWatchHistoryStore(db_path, config_version="policy.v1")
    second = MarketWatchHistoryStore(db_path, config_version="policy.v2")
    try:
        assert first.record(snapshot)["alerts_added"] == 1
        assert second.record(snapshot)["alerts_added"] == 1
        assert len(first.get_timeline("2026-08-24")) == 1
        assert len(second.get_timeline("2026-08-24")) == 1
        assert first.get_alerts("2026-08-24")[0]["config_version"] == "policy.v1"
        assert second.get_alerts("2026-08-24")[0]["config_version"] == "policy.v2"
    finally:
        first.close()
        second.close()


def test_non_trading_day_snapshot_is_not_persisted(tmp_path: Path) -> None:
    holiday = _snapshot(
        datetime(2026, 2, 16, 10, 0, tzinfo=SHANGHAI),
        snapshot_id="mw-holiday",
        sequence=1,
        alert_code="holiday_warning",
    )
    assert holiday.market_state.phase.value == "non_trading"

    with MarketWatchHistoryStore(tmp_path / "holiday.sqlite3") as store:
        result = store.record(holiday)

        assert result["action"] == "skipped"
        assert result["reason"] == "non_trading_day"
        assert store.list_dates() == []
        assert store.get_timeline("2026-02-16") == []
        assert store.get_alerts("2026-02-16") == []


def test_legacy_non_trading_pollution_is_hidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tradex.market_watch.history as history_module

    holiday = _snapshot(
        datetime(2026, 2, 16, 10, 0, tzinfo=SHANGHAI),
        snapshot_id="mw-legacy-holiday",
        sequence=1,
        alert_code="legacy_holiday_warning",
    )
    with MarketWatchHistoryStore(tmp_path / "legacy-holiday.sqlite3") as store:
        monkeypatch.setattr(
            history_module,
            "calendar_day_status",
            lambda _value: CalendarDayStatus.VERIFIED_TRADING_DAY,
        )
        assert store.record(holiday)["action"] == "inserted"
        monkeypatch.setattr(history_module, "calendar_day_status", calendar_day_status)

        assert store.list_dates() == []
        assert store.get_timeline("2026-02-16") == []
        assert store.get_alerts("2026-02-16") == []


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_query_limits_must_be_positive_integers(tmp_path: Path, limit: object) -> None:
    store = MarketWatchHistoryStore(tmp_path / "limits.sqlite3")
    try:
        with pytest.raises(ValueError):
            store.get_timeline("2026-08-24", limit=limit)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            store.get_alerts("2026-08-24", limit=limit)  # type: ignore[arg-type]
    finally:
        store.close()
