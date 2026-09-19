from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tradex.data_gateway.sector_flow_store import SectorFundFlowStore
from tradex.market_watch.collection_store import MarketWatchCollectionStore
from tradex.market_watch.collector import MarketWatchCollector
from tradex.market_watch.contracts import MarketWatchSnapshotV1
from tradex.market_watch.history import MarketWatchHistoryStore
from tradex.market_watch.trajectory_publication import IntradayTrajectoryPublisher
from test_market_watch_collection_store import _snapshot
from test_market_watch_history import _sector_flow_trajectory


NOW = datetime(2026, 8, 24, 9, 33, tzinfo=ZoneInfo("Asia/Shanghai"))
FIELDS = ("sector_flow_trajectory", "offense_sector_flow_trajectory")


def rotation(as_of):
    result = {}
    for field, direction in zip(FIELDS, ("defense", "offense")):
        trajectory = _sector_flow_trajectory(as_of, direction=direction)
        sector = trajectory["sectors"][0]
        first = as_of.replace(hour=9, minute=31)
        sector["points"] = [
            {**sector["points"][-1], "provider_as_of": (first + timedelta(minutes=i)).isoformat(),
             "sampled_at": (first + timedelta(minutes=i)).isoformat()}
            for i in range(int((as_of - first).total_seconds() / 60) + 1)
        ]
        result[field] = trajectory
    return result


@pytest.fixture
def stores(tmp_path):
    path = tmp_path / "market.sqlite3"
    sector_path = tmp_path / "sector.sqlite3"
    factory = lambda: SectorFundFlowStore(sector_path)
    with factory() as curves:
        curves.record_target_identities(NOW.date(), [
            {"sector_key": key, "name": key, "taxonomy": "industry", "provider_sector_code": f"BK{index:04}"}
            for index, key in enumerate(("electric_power", "semiconductor"))
        ])
        curves.request_intraday_repair(NOW.date(), required_through=NOW, requested_at=NOW)
        curves.update_intraday_repair(NOW.date(), status="partial", observed_at=NOW,
                                      target_count=2, remaining_targets=0, last_error="等待发布")
    with MarketWatchHistoryStore(path, clock=lambda: NOW) as history, MarketWatchCollectionStore(path, clock=lambda: NOW) as ledger:
        yield history, ledger, factory


def seed(history, ledger, as_of):
    snapshot = _snapshot(as_of, snapshot_id=f"mw-{as_of.minute}")
    history.record(snapshot)
    ledger.reconcile_history_record(history.get_collection_records(as_of.date())[-1])
    return snapshot


def publisher(stores, loader=rotation):
    history, ledger, factory = stores
    return IntradayTrajectoryPublisher(history=history, ledger=ledger, rotation_loader=loader, sector_store_factory=factory)


def test_zero_downloads_publish_only_trajectories_and_skip_unchanged(stores):
    history, ledger, factory = stores
    original = seed(history, ledger, NOW)
    calls = []
    publish = publisher(stores, lambda target: calls.append(target) or rotation(target))
    result = publish(NOW)
    assert result["action"] == "updated"
    assert result["publication_status"] == "complete"
    with factory() as curves:
        repair = curves.read_intraday_repair(NOW.date())
        assert repair["status"] == "complete"
        assert repair["attempted_targets"] == 0
    payload = history.get_timeline(NOW.date())[-1]["payload"]
    baseline = original.model_dump(mode="json")
    assert {k: v for k, v in payload.items() if k not in FIELDS} == {k: v for k, v in baseline.items() if k not in FIELDS}
    assert len(payload[FIELDS[0]]["sectors"][0]["points"]) == 3
    assert publish(NOW)["action"] == "unchanged"
    assert calls == [NOW]


def test_earlier_valid_snapshot_remains_partial_then_new_snapshot_completes(stores):
    history, ledger, factory = stores
    seed(history, ledger, NOW - timedelta(minutes=1))
    publish = publisher(stores)
    first = publish(NOW)
    assert first["publication_status"] == "partial"
    assert first["published_through"] == (NOW - timedelta(minutes=1)).isoformat()
    with factory() as curves:
        assert "目标" in curves.read_intraday_repair(NOW.date())["last_error"]
    seed(history, ledger, NOW)
    assert publish(NOW)["publication_status"] == "complete"


def test_failed_ledger_binding_is_recovered_after_publisher_restart(stores, monkeypatch):
    history, ledger, factory = stores
    seed(history, ledger, NOW)
    original_bind = ledger.reconcile_history_record
    monkeypatch.setattr(ledger, "reconcile_history_record", lambda record: (_ for _ in ()).throw(RuntimeError("ledger unavailable")))
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        publisher(stores)(NOW)
    with factory() as curves:
        assert curves.read_intraday_repair(NOW.date())["status"] == "partial"
    monkeypatch.setattr(ledger, "reconcile_history_record", original_bind)
    assert publisher(stores)(NOW)["publication_status"] == "complete"


@pytest.mark.parametrize("fault", ["future", "missing_target", "remove_existing"])
def test_publication_rejects_future_points_and_preserves_required_scope(stores, fault):
    history, ledger, factory = stores
    original = seed(history, ledger, NOW)
    if fault == "remove_existing":
        payload = original.model_dump(mode="json")
        payload.update(rotation(NOW))
        history.record(MarketWatchSnapshotV1.model_validate(payload))
        ledger.reconcile_history_record(history.get_collection_records(NOW.date())[-1])
    def bad(target):
        result = rotation(target + timedelta(minutes=1) if fault == "future" else target)
        if fault == "missing_target":
            sector = result[FIELDS[1]]["sectors"][0]
            sector["sector_key"] = "other"
        if fault == "remove_existing":
            result[FIELDS[0]]["sectors"][0]["points"].pop(0)
        return result
    if fault == "missing_target":
        result = publisher(stores, bad)(NOW)
        assert result["publication_status"] == "partial"
        assert result["missing_target_keys"] == ["semiconductor"]
    else:
        with pytest.raises(ValueError, match="exceeds|remove"):
            publisher(stores, bad)(NOW)


def test_changed_base_revision_is_not_overwritten(stores):
    history, ledger, factory = stores
    seed(history, ledger, NOW)
    def changed(target):
        newer = _snapshot(target, snapshot_id="mw-newer")
        history.record(newer)
        ledger.reconcile_history_record(history.get_collection_records(NOW.date())[-1])
        return rotation(target)
    assert publisher(stores, changed)(NOW)["action"] == "conflict"
    assert ledger.get_envelope(as_of=NOW).latest_accepted_real.snapshot_id == "mw-newer"


def test_history_compare_and_swap_preserves_newer_unbound_revision(stores, monkeypatch):
    history, ledger, factory = stores
    seed(history, ledger, NOW)
    record = history.record
    def race(snapshot, **kwargs):
        record(_snapshot(NOW, snapshot_id="mw-newer-unbound"))
        return record(snapshot, **kwargs)
    monkeypatch.setattr(history, "record", race)
    assert publisher(stores)(NOW)["action"] == "conflict"
    assert history.get_timeline(NOW.date())[-1]["payload"]["snapshot_id"] == "mw-newer-unbound"


def test_collector_runs_publication_after_capture_and_retries_when_idle(stores):
    history, ledger, factory = stores
    clock = [NOW]
    calls = []
    def publish(observed):
        calls.append("publish")
        if calls.count("publish") == 1:
            raise RuntimeError("transient publication failure")
        return {"action": "updated"}
    collector = MarketWatchCollector(store=ledger,
        capture_current=lambda slot: calls.append("capture") or _snapshot(slot.minute_bucket, snapshot_id="mw-live"),
        repair_historical=lambda slot: pytest.fail("no provider repair"), persist_snapshot=history.record,
        publish_trajectories=publish, clock=lambda: clock[0])
    assert collector.run_once()["action"] == "accepted"
    assert calls == ["capture", "publish"]
    clock[0] += timedelta(seconds=31)
    assert collector.run_once()["trajectory_publication"]["action"] == "updated"
    assert calls == ["capture", "publish", "publish"]
