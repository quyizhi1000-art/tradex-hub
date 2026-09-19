"""Full-directory coverage, provenance, retention and read-only API contract."""

from datetime import datetime, timedelta
from pathlib import Path
import json
import sqlite3
import zlib
from zoneinfo import ZoneInfo

import pytest

from tradex.dashboard.sector_catalog_collector import refresh_sector_catalog
from tradex.dashboard.rotation_radar import (
    ROTATION_CONFIG_VERSION, ROTATION_SCHEMA_VERSION, analyze_sector_flow_snapshots,
    normalize_rotation_snapshot,
)
from tradex.market_watch.sector_catalog import SectorCatalogStore


START = datetime(2026, 9, 11, 9, 31, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def sources(tmp_path, monkeypatch):
    path = tmp_path / "rotation.sqlite3"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE rotation_snapshots(trade_date TEXT, minute_bucket TEXT PRIMARY KEY, "
                  "schema_version TEXT, config_version TEXT, market_phase TEXT, payload_blob BLOB)")
    scheduled = []
    monkeypatch.setattr("tradex.data_gateway.sector_flow.read_sector_intraday_fund_flow_backfill", lambda **kw: {})
    monkeypatch.setattr("tradex.data_gateway.sector_flow.refresh_optional_sector_intraday_fund_flow",
                        lambda target, **kw: scheduled.append(target) or {})
    monkeypatch.setattr("tradex.data_gateway.sector_flow.sector_intraday_repair_window_open", lambda observed: True)
    return path, scheduled


def put(path, moment, *, hot=True, rename=False, count=90, provider_time=None, source="eastmoney"):
    rows = []
    for i in range(count):
        name = "电力" if i == 0 else "新兴主题" if i == 89 else f"主题{i}"
        if rename and i == 89:
            name = "新兴主题改名"
        price = 4.0 if i == 89 and hot else -.5
        flow = 2e8 if i == 89 and hot else -1e7
        rows.append(["concept" if i else "industry", f"BK{90000+i}", name, price,
                     80.0, 20.0, flow, 10.0 if i == 89 and hot else -1.0, None,
                     (provider_time or moment).isoformat(), source])
    with sqlite3.connect(path) as c:
        c.execute("INSERT OR REPLACE INTO rotation_snapshots VALUES (?,?,?,?,?,?)",
                  (moment.date().isoformat(), moment.isoformat(), ROTATION_SCHEMA_VERSION,
                   ROTATION_CONFIG_VERSION, "trading", zlib.compress(json.dumps(rows).encode())))


def read(path):
    with SectorCatalogStore(path) as store:
        return store.latest()


def test_full_directory_over_64_hotspot_retention_and_real_gap(sources):
    path, scheduled = sources
    put(path, START)
    put(path, START + timedelta(minutes=1))
    put(path, START + timedelta(minutes=3), hot=False)
    result = refresh_sector_catalog(db_path=path, now=START + timedelta(minutes=3))
    catalog = read(path)
    assert result["total"] == 90
    assert catalog.counts["mapped"] == 1
    assert catalog.counts["unclassified"] == 89
    target = next(e for e in catalog.entries if e.name == "新兴主题")
    assert target.hot_state == "retained"
    assert target.missing_minutes == ("09:33",)
    assert target.point_count == 3
    assert len(scheduled) == 3
    assert scheduled[0]["sector_key"] == target.sector_key
    before = path.stat().st_mtime_ns
    with SectorCatalogStore(path) as store:
        detail = store.detail(catalog.catalog_revision, (target.sector_key,))
    assert [p["provider_as_of"][11:16] for p in detail["sectors"][0]["points"]] == ["09:31", "09:32", "09:34"]
    from tradex.market_watch.integrity import build_sector_flow_series_integrity
    assert build_sector_flow_series_integrity(detail["sectors"][0]).points_revision == target.points_revision
    assert path.stat().st_mtime_ns == before
    assert refresh_sector_catalog(db_path=path, now=START + timedelta(minutes=4))["action"] == "recorded"
    assert "09:35" in read(path).entries[0].missing_minutes


def test_post_close_repairs_non_hot_catalog_entries_with_bounded_requests(sources, monkeypatch):
    from tradex.dashboard.sector_catalog_collector import _repair_catalog_curves
    path, scheduled = sources
    put(path, START, hot=False)
    refresh_sector_catalog(db_path=path, now=START, schedule_backfill=False)
    catalog = read(path)
    close = START.replace(hour=15, minute=1)
    calls = []
    def repair(target, **kwargs):
        calls.append(target)
        return {target['sector_key']: ({'provider_as_of': '2026-09-11T09:32:00+08:00'},)}
    # Build real missing minutes without turning ordinary boards into hotspots.
    put(path, START + timedelta(minutes=2), hot=False)
    refresh_sector_catalog(db_path=path, now=START + timedelta(minutes=2), schedule_backfill=False)
    catalog = read(path)
    monkeypatch.setattr('tradex.data_gateway.sector_flow.refresh_optional_sector_intraday_fund_flow', repair)
    assert _repair_catalog_curves(catalog, close, db_path=path, max_targets=4)
    assert len(calls) == 4
    assert all(next(e for e in catalog.entries if e.sector_key == c['sector_key']).hot_state == 'none' for c in calls)
    calls.clear()
    assert _repair_catalog_curves(catalog, START + timedelta(minutes=3), db_path=path)
    assert len(calls) == 4
    calls.clear()
    monkeypatch.setattr('tradex.data_gateway.sector_flow.refresh_optional_sector_intraday_fund_flow',
                        lambda target, **kwargs: calls.append(target) or {})
    assert not _repair_catalog_curves(catalog, close, db_path=path)
    assert len(calls) == 3
    calls.clear()
    assert not _repair_catalog_curves(catalog, close + timedelta(days=1), db_path=path)
    assert calls == []


def test_successful_catalog_repair_is_published_in_the_same_collector_step(sources, monkeypatch):
    path, scheduled = sources
    put(path, START)
    put(path, START + timedelta(minutes=1))
    put(path, START + timedelta(minutes=3))
    supplements = {}
    monkeypatch.setattr('tradex.data_gateway.sector_flow.read_sector_intraday_fund_flow_backfill',
                        lambda **kwargs: supplements)
    def repair(target, **kwargs):
        points = ({'provider_as_of': START + timedelta(minutes=2),
                   'sampled_at': START + timedelta(minutes=2), 'cumulative_cny': 2e8,
                   'taxonomy': target['taxonomy'], 'source_family': 'eastmoney'},)
        supplements[target['sector_key']] = points
        return {target['sector_key']: points}
    monkeypatch.setattr('tradex.data_gateway.sector_flow.refresh_optional_sector_intraday_fund_flow', repair)
    result = refresh_sector_catalog(db_path=path, now=START + timedelta(minutes=3))
    catalog = read(path)
    entry = next(e for e in catalog.entries if e.name == '新兴主题')
    assert entry.missing_minutes == ()
    assert entry.point_count == 4
    assert result['catalog_revision'] == catalog.catalog_revision


def test_rename_preserves_identity_and_new_day_does_not_copy_points(sources):
    path, _ = sources
    put(path, START)
    refresh_sector_catalog(db_path=path, now=START)
    first = read(path)
    key = next(e.sector_key for e in first.entries if e.name == "新兴主题")
    put(path, START + timedelta(minutes=1), rename=True)
    refresh_sector_catalog(db_path=path, now=START + timedelta(minutes=1))
    updated = read(path)
    entry = next(e for e in updated.entries if e.sector_key == key)
    assert entry.change == "renamed" and "新兴主题" in entry.aliases
    tomorrow = START + timedelta(days=3)
    put(path, tomorrow, count=89)
    refresh_sector_catalog(db_path=path, now=tomorrow)
    missing = next(e for e in read(path).entries if e.sector_key == key)
    assert not missing.observed_today and not missing.present_latest
    assert missing.point_count == 0 and missing.hot_state == "none"
    assert missing.change == "missing"


def test_repeated_or_stale_provider_times_do_not_confirm_hotspot(sources):
    path, scheduled = sources
    for offset in (0, 1, 20):
        put(path, START + timedelta(minutes=offset), provider_time=START)
    refresh_sector_catalog(db_path=path, now=START + timedelta(minutes=20))
    assert read(path).counts["hot"] == 0
    assert len(scheduled) == 3  # Ordinary boards are also eligible for history repair.


def test_provider_switch_is_separate_identity_and_does_not_blend_curves(sources):
    path, scheduled = sources
    put(path, START)
    put(path, START + timedelta(minutes=1), source="paid_source")
    refresh_sector_catalog(db_path=path, now=START + timedelta(minutes=1))
    catalog = read(path)
    targets = [e for e in catalog.entries if e.name == "新兴主题"]
    assert len(targets) == 2 and len({e.sector_key for e in targets}) == 2
    assert {e.curve_support for e in targets} == {"same_source", "unverified"}
    assert all(e.point_count == 1 for e in targets)
    assert scheduled and all(t["source_family"] == "eastmoney" for t in scheduled)


def test_revision_race_and_corrupt_curve_fail_closed(sources):
    path, _ = sources
    put(path, START)
    refresh_sector_catalog(db_path=path, now=START)
    catalog = read(path)
    key = catalog.entries[0].sector_key
    with SectorCatalogStore(path) as store:
        with pytest.raises(RuntimeError, match="revision_changed"):
            store.detail("0" * 64, (key,))
        with pytest.raises(ValueError):
            store.detail(catalog.catalog_revision, (key, key))
    with sqlite3.connect(path) as c:
        row = c.execute("SELECT payload FROM sector_catalog_curves WHERE sector_key=?", (key,)).fetchone()
        raw = json.loads(zlib.decompress(row[0]))
        raw["points"][0]["cumulative_cny"] += 1
        raw["latest"]["cumulative_cny"] += 1
        c.execute("UPDATE sector_catalog_curves SET payload=? WHERE sector_key=?", (zlib.compress(json.dumps(raw).encode()), key))
    with SectorCatalogStore(path) as store:
        with pytest.raises(RuntimeError, match="integrity_failed"):
            store.detail(catalog.catalog_revision, (key,))


def test_missing_read_store_creates_nothing(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with SectorCatalogStore(path) as store:
        assert store.latest() is None
    assert not path.exists()


def test_verified_close_curve_extends_flow_watermark_without_extending_quotes(sources, monkeypatch):
    path, _ = sources
    put(path, START)
    put(path, START + timedelta(minutes=1))
    refresh_sector_catalog(db_path=path, now=START, schedule_backfill=False)
    key = next(e.sector_key for e in read(path).entries if e.name == "新兴主题")
    close = START.replace(hour=15, minute=0)
    monkeypatch.setattr("tradex.data_gateway.sector_flow.read_sector_intraday_fund_flow_backfill",
        lambda **kw: {key: ({"provider_as_of": close, "cumulative_cny": 3e8,
                            "source_family": "eastmoney", "taxonomy": "concept", "name": "新兴主题"},)})
    refresh_sector_catalog(db_path=path, now=close, schedule_backfill=False)
    catalog = read(path)
    entry = next(e for e in catalog.entries if e.sector_key == key)
    assert catalog.as_of == close
    assert catalog.quote_as_of == START + timedelta(minutes=1)
    assert entry.last_provider_as_of == close and entry.change_pct == 4.0
    assert entry.quote_as_of == START + timedelta(minutes=1)
    assert entry.point_count == 3
    assert "09:33" in entry.missing_minutes


def test_old_catalog_digest_survives_additive_quote_watermark(sources):
    from tradex.market_watch.integrity import stable_sha256
    path, _ = sources
    put(path, START)
    refresh_sector_catalog(db_path=path, now=START, schedule_backfill=False)
    raw = read(path).model_dump(mode="json")
    raw.pop("quote_as_of")
    for e in raw["entries"]:
        e.pop("quote_as_of")
    raw["catalog_revision"] = stable_sha256({k: v for k, v in raw.items() if k != "catalog_revision"})
    with sqlite3.connect(path) as c:
        c.execute("UPDATE sector_catalog_days SET manifest=?,revision=?",
                  (zlib.compress(json.dumps(raw).encode()), raw["catalog_revision"]))
    assert read(path).catalog_revision == raw["catalog_revision"]


def test_catalog_http_routes_are_read_only_and_reject_stale_revision(sources, monkeypatch):
    from tradex.dashboard.__main__ import DashboardHandler

    path, _ = sources
    put(path, START)
    refresh_sector_catalog(db_path=path, now=START)
    monkeypatch.setenv("TRADEX_ROTATION_DB", str(path))
    handler = DashboardHandler.__new__(DashboardHandler)
    replies = []
    handler._send_json = lambda status, payload: replies.append((status, payload))
    before = path.stat().st_mtime_ns
    handler.path = "/api/market-watch/sector-catalog"
    handler.do_GET()
    assert replies[-1][0] == 200
    catalog = replies[-1][1]
    assert catalog["recovery"]["catalog_revision"] == catalog["catalog_revision"]
    key = catalog["entries"][0]["sector_key"]
    handler.path = f"/api/market-watch/sector-catalog/trajectory?catalog_revision={catalog['catalog_revision']}&sector_keys={key}"
    handler.do_GET()
    assert replies[-1][0] == 200 and replies[-1][1]["catalog_revision"] == catalog["catalog_revision"]
    handler.path = f"/api/market-watch/sector-catalog/trajectory?catalog_revision={'0'*64}&sector_keys={key}"
    handler.do_GET()
    assert replies[-1][0] == 409
    handler.path = "/api/market-watch/sector-catalog/trajectory?catalog_revision=invalid"
    handler.do_GET()
    assert replies[-1][0] == 400
    assert path.stat().st_mtime_ns == before


def test_legacy_direction_lists_and_alias_matching_remain_intact():
    snapshot = normalize_rotation_snapshot(
        [{"board_code": "BK1283", "name": "银行", "change_pct": 1.0,
          "up_count": 8, "down_count": 2, "flow_amount": 1e8, "flow_ratio": 1,
          "provider_as_of": START.isoformat(), "source": "eastmoney"}], [], minute_bucket=START)
    defense = analyze_sector_flow_snapshots([snapshot])
    offense = analyze_sector_flow_snapshots([snapshot], direction="offense")
    assert len(defense["sectors"]) == 40 and len(offense["sectors"]) == 48
    bank = next(e for e in defense["sectors"] if e["sector_key"] == "bank")
    assert bank["latest"]["cumulative_cny"] == 1e8
    assert not bank["follow_eligible"]


def test_stopped_source_still_discovers_missing_close_tail(sources):
    path, _ = sources
    put(path, START, count=3)
    refresh_sector_catalog(db_path=path, now=START, schedule_backfill=False)
    close = START.replace(hour=15, minute=1)
    refresh_sector_catalog(db_path=path, now=close, schedule_backfill=False)
    catalog = read(path)
    assert catalog.as_of == START  # Actual observations do not advance with the clock.
    assert catalog.coverage_as_of == close.replace(minute=0)
    assert all(e.expected_point_count == 240 and "15:00" in e.missing_minutes for e in catalog.entries)


def test_automatic_recovery_backoff_survives_reopen_and_finishes_without_manual_targets(sources, monkeypatch):
    from tradex.market_watch.sector_catalog import expected_minutes
    path, scheduled = sources
    put(path, START, count=3)
    close = START.replace(hour=15, minute=1)
    refresh_sector_catalog(db_path=path, now=close)
    assert len(scheduled) == 3
    with SectorCatalogStore(path) as store:
        status = store.recovery(START.date().isoformat())
        assert status["state"] == "backoff" and status["missing_series"] == 3
        assert status["attempt_count"] == 3
    # Fresh store instances must respect durable retry deadlines.
    refresh_sector_catalog(db_path=path, now=close + timedelta(seconds=1))
    assert len(scheduled) == 3
    supplements = {}
    monkeypatch.setattr("tradex.data_gateway.sector_flow.read_sector_intraday_fund_flow_backfill", lambda **kw: supplements)
    def recovered_source(target, **kw):
        scheduled.append(target)
        supplements[target["sector_key"]] = tuple(dict(
            provider_as_of=datetime.fromisoformat(f"2026-09-11T{minute}:00+08:00"),
            cumulative_cny=1e8, source_family="eastmoney", taxonomy=target["taxonomy"])
            for minute in expected_minutes(close))
        return {target["sector_key"]: supplements[target["sector_key"]]}
    monkeypatch.setattr("tradex.data_gateway.sector_flow.refresh_optional_sector_intraday_fund_flow", recovered_source)
    refresh_sector_catalog(db_path=path, now=close + timedelta(seconds=31))
    assert len(scheduled) == 6 and read(path).counts["history_missing"] == 0
    with SectorCatalogStore(path) as store:
        status = store.recovery(START.date().isoformat())
        assert status["state"] == "complete" and status["attempt_count"] == 6
        assert status["catalog_revision"] == store.latest().catalog_revision
    refresh_sector_catalog(db_path=path, now=close + timedelta(seconds=61))
    assert len(scheduled) == 6  # Completion never redownloads successful targets.


def full_source_curve(target, close):
    from tradex.market_watch.sector_catalog import expected_minutes
    return tuple(dict(provider_as_of=datetime.fromisoformat(f"2026-09-11T{minute}:00+08:00"),
                      cumulative_cny=1e8, source_family="eastmoney", taxonomy=target["taxonomy"])
                 for minute in expected_minutes(close))


def test_interrupted_request_is_reclaimed_from_persisted_lease(sources, monkeypatch):
    class ProcessInterrupted(BaseException):
        pass
    path, _ = sources
    put(path, START, count=1)
    close = START.replace(hour=15, minute=1)
    supplements, calls = {}, []
    def source(target, **kwargs):
        calls.append(target)
        if len(calls) == 1:
            raise ProcessInterrupted()
        supplements[target["sector_key"]] = full_source_curve(target, close)
        return supplements
    monkeypatch.setattr("tradex.data_gateway.sector_flow.refresh_optional_sector_intraday_fund_flow", source)
    monkeypatch.setattr("tradex.data_gateway.sector_flow.read_sector_intraday_fund_flow_backfill", lambda **kw: supplements)
    with pytest.raises(ProcessInterrupted):
        refresh_sector_catalog(db_path=path, now=close)
    with SectorCatalogStore(path) as reopened:
        assert next(iter(reopened.repair_attempts("2026-09-11").values()))["outcome"] == "running"
    refresh_sector_catalog(db_path=path, now=close + timedelta(seconds=1))
    assert len(calls) == 1
    refresh_sector_catalog(db_path=path, now=close + timedelta(seconds=31))
    assert len(calls) == 2 and read(path).counts["history_missing"] == 0


def test_publication_failure_retries_cached_truth_without_redownload(sources, monkeypatch):
    path, _ = sources
    put(path, START, count=1)
    close = START.replace(hour=15, minute=1)
    supplements, calls = {}, []
    def source(target, **kwargs):
        calls.append(target)
        supplements[target["sector_key"]] = full_source_curve(target, close)
        return supplements
    monkeypatch.setattr("tradex.data_gateway.sector_flow.refresh_optional_sector_intraday_fund_flow", source)
    monkeypatch.setattr("tradex.data_gateway.sector_flow.read_sector_intraday_fund_flow_backfill", lambda **kw: supplements)
    original_record = SectorCatalogStore.record
    def unavailable_publication(self, catalog, curves):
        if not catalog.counts["history_missing"]:
            raise RuntimeError("injected publication failure")
        return original_record(self, catalog, curves)
    monkeypatch.setattr(SectorCatalogStore, "record", unavailable_publication)
    with pytest.raises(RuntimeError, match="injected publication"):
        refresh_sector_catalog(db_path=path, now=close)
    with SectorCatalogStore(path) as reader:
        status = reader.recovery("2026-09-11")
        assert status["state"] == "failed" and status["missing_series"] == 1
        assert reader.latest().entries[0].point_count == 1
    monkeypatch.setattr(SectorCatalogStore, "record", original_record)
    refresh_sector_catalog(db_path=path, now=close + timedelta(seconds=31))
    assert len(calls) == 1
    with SectorCatalogStore(path) as reader:
        assert reader.recovery("2026-09-11")["state"] == "complete"
        assert reader.latest().entries[0].point_count == 240


def test_expired_history_gap_stays_visible_without_wrong_day_requests(sources):
    path, scheduled = sources
    put(path, START, count=1)
    refresh_sector_catalog(db_path=path, now=START + timedelta(days=3))
    with SectorCatalogStore(path) as reader:
        status = reader.recovery("2026-09-11")
        assert status["state"] == "unavailable" and status["missing_series"] == 1
        assert status["repairable_series"] == 0
        assert reader.latest().as_of == START
    assert scheduled == []


def test_repeated_failures_back_off_to_a_bounded_persisted_deadline(sources):
    path, _ = sources
    put(path, START, count=1)
    close = START.replace(hour=15, minute=1)
    refresh_sector_catalog(db_path=path, now=close, schedule_backfill=False)
    catalog = read(path)
    key = catalog.entries[0].sector_key
    for count, seconds in enumerate((30, 60, 120, 240, 480, 960, 1800, 1800), start=1):
        with SectorCatalogStore(path, read_only=False) as journal:
            journal.begin_repair(catalog.trade_date, key, close)
            journal.finish_repair(catalog.trade_date, key, close, gained=False, error="TimeoutError")
        with SectorCatalogStore(path) as reopened:
            attempt = reopened.repair_attempts(catalog.trade_date)[key]
            assert attempt["attempt_count"] == count and attempt["failures"] == count
            assert datetime.fromisoformat(attempt["next_retry_at"]) == close + timedelta(seconds=seconds)
        close += timedelta(seconds=seconds)


def test_closed_admission_window_does_not_create_failed_attempt_or_backoff(sources, monkeypatch):
    path, scheduled = sources
    put(path, START, count=3)
    later = START + timedelta(minutes=10)
    monkeypatch.setattr("tradex.data_gateway.sector_flow.sector_intraday_repair_window_open", lambda observed: False)
    refresh_sector_catalog(db_path=path, now=later)
    with SectorCatalogStore(path) as store:
        assert store.repair_attempts("2026-09-11") == {}
        assert store.recovery("2026-09-11")["attempt_count"] == 0
    assert not scheduled
    monkeypatch.setattr("tradex.data_gateway.sector_flow.sector_intraday_repair_window_open", lambda observed: True)
    refresh_sector_catalog(db_path=path, now=later + timedelta(seconds=22))
    assert len(scheduled) == 3


def test_lunch_drains_multiple_non_hot_targets_and_stops_at_live_window_boundary(sources, monkeypatch):
    from tradex.dashboard.sector_catalog_collector import _repair_catalog_curves
    path, _ = sources
    put(path, START, count=10, hot=False)
    lunch = START.replace(hour=11, minute=31)
    refresh_sector_catalog(db_path=path, now=lunch, schedule_backfill=False)
    calls = []
    def repair(target, **kwargs):
        calls.append(target)
        return {target["sector_key"]: ({"provider_as_of":START + timedelta(minutes=1)},)}
    monkeypatch.setattr("tradex.data_gateway.sector_flow.refresh_optional_sector_intraday_fund_flow", repair)
    assert _repair_catalog_curves(read(path), lunch, db_path=path, max_targets=8)
    assert len(calls) == 8
    calls.clear()
    moments = iter([lunch, lunch.replace(hour=13, minute=0)])
    monkeypatch.setattr("tradex.data_gateway.sector_flow.sector_intraday_repair_window_open", lambda stamp: stamp.hour != 13)
    assert not _repair_catalog_curves(read(path), lunch, db_path=path, clock=lambda: next(moments))
    assert calls == []


def test_old_closed_window_backoff_is_released_but_real_source_backoff_is_kept(sources, monkeypatch):
    from tradex.dashboard.sector_catalog_collector import _repair_catalog_curves
    path, scheduled = sources
    put(path, START, count=2)
    current = START.replace(hour=10, minute=0, second=30)
    refresh_sector_catalog(db_path=path, now=current, schedule_backfill=False)
    catalog = read(path)
    keys = [e.sector_key for e in catalog.entries]
    with SectorCatalogStore(path, read_only=False) as journal:
        for key, second in zip(keys, [0, 25]):
            stamp = current.replace(second=second)
            journal.begin_repair(catalog.trade_date, key, stamp)
            journal.finish_repair(catalog.trade_date, key, stamp, gained=False, error="no_missing_minutes_returned")
    monkeypatch.setattr("tradex.data_gateway.sector_flow.sector_intraday_repair_window_open", lambda stamp:22 <= stamp.second <= 46)
    _repair_catalog_curves(catalog, current, db_path=path)
    assert [t["sector_key"] for t in scheduled] == [keys[0]]
    with SectorCatalogStore(path) as journal:
        assert journal.repair_attempts(catalog.trade_date)[keys[1]]["attempt_count"] == 1


def test_source_filled_by_another_gateway_path_clears_pending_failure_count(sources, monkeypatch):
    path, _ = sources
    put(path, START, count=1)
    close = START.replace(hour=15, minute=1)
    refresh_sector_catalog(db_path=path, now=close)
    entry = read(path).entries[0]
    points = full_source_curve({"taxonomy":entry.taxonomy}, close)
    monkeypatch.setattr("tradex.data_gateway.sector_flow.read_sector_intraday_fund_flow_backfill",
                        lambda **kw:{entry.sector_key:points})
    refresh_sector_catalog(db_path=path, now=close+timedelta(seconds=31))
    with SectorCatalogStore(path) as store:
        status = store.recovery("2026-09-11")
        assert status["state"] == "complete" and status["failed_targets"] == 0
        assert store.repair_attempts("2026-09-11")[entry.sector_key]["failures"] == 1  # Keep past audit evidence.
