"""Isolated real-source probe of the Collector's normal recovery entrypoint.

Run prepare/fail and recover in separate processes; all writes stay in this
probe's two databases. No production minute or canonical curve is removed.
"""
import argparse
import json
import os
import sqlite3
import zlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

folder = Path(__file__).resolve().parent / "automatic_recovery_probe"
rotation = folder / "rotation.sqlite3"
flows = folder / "sector_flow.sqlite3"
parser = argparse.ArgumentParser()
parser.add_argument("stage", choices=["fail", "recover"])
args = parser.parse_args()
now = datetime.now(ZoneInfo("Asia/Shanghai"))
day = now.date().isoformat()
if args.stage == "fail":
    folder.mkdir(exist_ok=True)
    assert not rotation.exists() and not flows.exists(), "Probe must start with isolated empty stores"
    source_path = Path.home() / ".tradex" / "rotation_radar.sqlite3"
    with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        row = source.execute("SELECT * FROM rotation_snapshots WHERE trade_date=? ORDER BY minute_bucket DESC LIMIT 1", (day,)).fetchone()
        schema = source.execute("SELECT sql FROM sqlite_master WHERE name='rotation_snapshots'").fetchone()[0]
        raw = json.loads(zlib.decompress(row["payload_blob"]))
        subset = [item for item in raw if str(item[1]).startswith("BK") and item[6] is not None][:3]
        assert len(subset) == 3
        projected = dict(row)
        projected["payload_blob"] = zlib.compress(json.dumps(subset, ensure_ascii=False).encode("utf8"))
    with sqlite3.connect(rotation) as target:
        target.execute(schema)
        fields = list(projected)
        target.execute(f"INSERT INTO rotation_snapshots ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", tuple(projected.values()))
    (folder / "fixture.json").write_text(json.dumps({"basis":"three unmodified board observations projected from a real raw snapshot", "day":day, "minute_bucket":row["minute_bucket"], "board_codes":[item[1] for item in subset]}, ensure_ascii=False, indent=2), encoding="utf8")

os.environ["TRADEX_ROTATION_DB"] = str(rotation)
os.environ["TRADEX_SECTOR_FLOW_DB"] = str(flows)
from tradex.dashboard.collector_worker import _refresh_sector_catalog
from tradex.market_watch.sector_catalog import SectorCatalogStore
from tradex.data_gateway import sector_flow

if args.stage == "fail":
    def outage(*args, **kwargs):
        raise TimeoutError("isolated provider outage injection")
    sector_flow.refresh_optional_sector_intraday_fund_flow = outage
else:
    with SectorCatalogStore(rotation) as reader:
        before = reader.recovery(day)
    assert (before["state"], before["attempt_count"]) in {("backoff", 3), ("complete", 6)}
    if before["next_retry_at"]:
        assert datetime.fromisoformat(before["next_retry_at"]) <= now, "Wait for the real retry deadline"

result = _refresh_sector_catalog(now=now)
with SectorCatalogStore(rotation) as reader:
    catalog = reader.latest()
    status = reader.recovery(day)
    details = reader.detail(catalog.catalog_revision, tuple(e.sector_key for e in catalog.entries))
    attempts = reader.repair_attempts(day)
if args.stage == "fail":
    assert status["state"] == "backoff" and status["missing_series"] == 3
    assert status["attempt_count"] == 3
else:
    assert status["state"] == "complete" and status["attempt_count"] == 6
    assert status["missing_series"] == 0
    assert all(e.expected_point_count == 240 and not e.missing_minutes for e in catalog.entries)
    with sector_flow.SectorFundFlowStore() as store:
        assert store.get_targets(now.date()) == () or not store.get_targets(now.date())
        source_curves = store.get_all_best(now.date())
        assert len(source_curves) == 3 and all(len(c.points) == 240 for c in source_curves.values())
    from tradex.dashboard.rotation_store import _expand_payload
    from tradex.dashboard.sector_catalog_collector import _identity
    with sqlite3.connect(rotation) as raw_store:
        row = raw_store.execute("SELECT payload_blob,minute_bucket,market_phase FROM rotation_snapshots").fetchone()
    raw_boards = _expand_payload(*row)["boards"]
    raw_values = {_identity(b)[0]:(b["provider_as_of"], b["flow_amount"]) for b in raw_boards}
    for series in details["sectors"]:
        canonical = {p.provider_as_of.isoformat():p.cumulative_cny for p in source_curves[series["sector_key"]].points}
        assert all(canonical.get(p["provider_as_of"]) == p["cumulative_cny"] or
                   raw_values[series["sector_key"]] == (p["provider_as_of"],p["cumulative_cny"])
                   for p in series["points"])
report = {"stage":args.stage, "process_id":os.getpid(), "result":result, "recovery":status, "attempts":attempts}
(folder / (args.stage + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
print(json.dumps(report, ensure_ascii=False))
