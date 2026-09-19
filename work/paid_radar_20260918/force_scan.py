"""Explicit one-off operator scan. Stop the managed collector before running."""
import hashlib
import json
from pathlib import Path

from tradex.stock_selection.intraday_macd_j import refresh_watch

root = Path.home() / ".tradex" / "intraday_macd_j"
out = Path(__file__).parent
before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob("scan-*.json")}
(out / "before-scan-hashes.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
result = refresh_watch(force=True)
(out / "forced-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == sha for name, sha in before.items())
assert result.get("scan_kind") == "manual_intraday"
assert result.get("evaluated_count", 0) > 0, result
assert result.get("quote_provider_counts", {}).get("tushare", 0) > 0, result.get("source_metadata")
print(json.dumps({key: value for key, value in result.items()
                  if key not in {"records", "scan_candidates", "seed_metadata"}}, ensure_ascii=False, indent=2))
print("matched:", len(result.get("scan_candidates", [])))
