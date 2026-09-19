"""Publish collected research as candidates, never as accepted membership.

The audit operator owns publication. Consumers only read an atomic snapshot.
Raw extracts remain in the research workspace for later individual review.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

from .catalog import SHANGHAI
from .storage import write_json


def snapshot_path() -> Path:
    return Path(os.environ.get("TRADEX_SMART_SECTOR_RESEARCH") or Path.home() / ".tradex/smart_sector_research.v1.json")


def ths_concepts(row: dict) -> list[str]:
    """Only parse the identified stock's table; mixed search snippets abstain."""
    if row.get("status") != "read":
        return []
    code = str(row.get("instrument_id", ""))[:6]
    body = row.get("body", "")
    url = f"https://basic.10jqka.com.cn/{code}/concept.html"
    if row.get("acquisition") == "browser":
        if row.get("source_url") == url and f"({code})" in row.get("title", ""):
            return list(dict.fromkeys(c["name"] for c in row.get("concepts", []) if c.get("name")))
        return []
    if row.get("acquisition", "page") != "page":
        return []
    if row.get("source_url") != url or url not in body or not re.search(rf"L\d+: # {code}\b", body):
        return []
    return list(dict.fromkeys(re.findall(r"^L\d+: \d+\s*\|\s*([^|\n]+?)\s*\|", body, re.M)))


def publish(root: Path, output: Path | None = None) -> dict:
    rows = {}
    progress_path = root / "full_market/progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {}
    for instrument_id in progress.get("instrument_ids", []):
        path = root / "full_market" / f"{instrument_id}.json"
        evidence = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        source = evidence.get("source", {})
        rows[instrument_id] = {
            "instrument_id": instrument_id,
            "collection_status": evidence.get("collection_status", "pending"),
            "review_status": "pending",
            "candidate_concepts": [],
            "source_links": [],
            "review_gaps": ["尚未完成概念解析、公司业务资料与市场主线的逐股交叉核验"],
        }
        row = rows[instrument_id]
        if source:
            row["candidate_concepts"].extend({"name": name, "source": "东方财富", "verified": False} for name in source.get("memberships", []))
            row["source_links"].append({"publisher": "东方财富", "url": source["source_url"], "retrieved_at": source["fetched_at"]})
    ths_read = set()
    ths_attempted = set()
    for path in sorted((root / "ths_sequential").glob("*.json")):
        evidence = json.loads(path.read_text(encoding="utf-8"))
        instrument_id = evidence["instrument_id"]
        if instrument_id not in rows:
            continue
        ths_attempted.add(instrument_id)
        row = rows[instrument_id]
        if evidence.get("status") == "read":
            ths_read.add(instrument_id)
            row["candidate_concepts"].extend({"name": name, "source": "同花顺", "verified": False} for name in ths_concepts(evidence))
            row["source_links"].append({"publisher": "同花顺", "url": evidence["source_url"], "retrieved_at": evidence["retrieved_at"]})
    for key, row in rows.items():
        row["ths_status"] = "read" if key in ths_read else "unavailable" if key in ths_attempted else "pending"
        row["candidate_concepts"].sort(key=lambda c: (c["source"] != "同花顺", c["name"]))
    ths_progress_path = root / "ths-progress.json"
    ths_progress = json.loads(ths_progress_path.read_text(encoding="utf-8")) if ths_progress_path.exists() else {}
    source_states = {"ths": ths_progress.get("status", "unknown"), "business": progress.get("status", "unknown")}
    complete = all(state == "acquisition_complete_review_pending" for state in source_states.values())
    paused = any(state.startswith("paused") for state in source_states.values())
    stage = "acquisition_complete_review_pending" if complete else "acquisition_partial_source_paused" if paused else "acquisition_in_progress_review_pending"
    result = {"contract": "smart_sector_research.v1", "schema_version": 1,
              "published_at": datetime.now(SHANGHAI).isoformat(),
              "total": len(rows), "collected": sum(r["collection_status"] == "collected" for r in rows.values()),
              "ths_attempted": len(ths_attempted), "ths_read": len(ths_read),
              "stage": stage, "source_states": source_states, "items": rows}
    target = output or snapshot_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json(target, result)
    return {key: value for key, value in result.items() if key != "items"}


def read_snapshot() -> dict:
    try:
        raw = json.loads(snapshot_path().read_text(encoding="utf-8"))
        if raw.get("contract") == "smart_sector_research.v1" and isinstance(raw.get("items"), dict):
            return raw
    except (OSError, ValueError):
        pass
    return {"items": {}, "stage": "unavailable"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(publish(args.root), ensure_ascii=False))
