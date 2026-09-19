"""Read-only acceptance monitor; production Collector owns every repair."""
import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

folder = Path(__file__).resolve().parent
deadline = time.monotonic() + 3600
with (folder / "automatic_progress.jsonl").open("a", encoding="utf8", buffering=1) as log:
    while time.monotonic() < deadline:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/market-watch/sector-catalog", timeout=15) as response:
            catalog = json.load(response)
        selected = sorted((e for e in catalog["entries"] if e["hot_state"] == "active" and e["point_count"]),
                          key=lambda e:e["change_pct"] if e["change_pct"] is not None else float("-inf"), reverse=True)[:12]
        report = {"at":datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                  "revision":catalog["catalog_revision"], "counts":catalog["counts"],
                  "recovery":catalog.get("recovery"), "default_group_missing":{
                      e["name"]:len(e["missing_minutes"]) for e in selected if e["missing_minutes"]}}
        line = json.dumps(report, ensure_ascii=False)
        log.write(line + "\n")
        print(line, flush=True)
        if catalog["counts"]["history_missing"] == 0:
            (folder / "after.json").write_text(json.dumps(catalog,ensure_ascii=False), encoding="utf8")
            break
        time.sleep(40)
