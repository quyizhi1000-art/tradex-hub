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
                        lambda target, **kw: scheduled.append(target))
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
    assert len(scheduled) == 1
    assert scheduled[0]["sector_key"] == target.sector_key
    before = path.stat().st_mtime_ns
    with SectorCatalogStore(path) as store:
        detail = store.detail(catalog.catalog_revision, (target.sector_key,))
    assert [p["provider_as_of"][11:16] for p in detail["sectors"][0]["points"]] == ["09:31", "09:32", "09:34"]
    from tradex.market_watch.integrity import build_sector_flow_series_integrity
    assert build_sector_flow_series_integrity(detail["sectors"][0]).points_revision == target.points_revision
    assert path.stat().st_mtime_ns == before
    assert refresh_sector_catalog(db_path=path, now=START + timedelta(minutes=4))["action"] == "existing"


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
    assert not scheduled


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
    assert not scheduled


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
