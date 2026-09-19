"""Resumable, bounded all-market evidence acquisition and gap accounting.

Run under the existing analysis task or explicitly via python -m. Supplier
membership/digests remain candidate evidence; fetching is never verification.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from tradex.data_gateway.sector_evidence import fetch_stock_sector_evidence
from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader
from .catalog import SHANGHAI
from .core import infer_current_market_categories
from .storage import write_json


def _write(path, payload):
    write_json(path, payload)


def run(output: Path, *, limit: int | None = None):
    output.mkdir(parents=True, exist_ok=True)
    with InstrumentTaxonomyReader() as reader:
        status = reader.status()
        profiles = reader.all_profiles()
    if not profiles or not status:
        raise RuntimeError("stock master unavailable")
    if limit is not None:
        profiles = profiles[:limit]
    manifest = {"contract": "smart_sector_audit.v1", "taxonomy_revision": status.catalog_revision,
                "started_at": datetime.now(SHANGHAI).isoformat(), "total": len(profiles),
                "instrument_ids": [p.instrument_id for p in profiles],
                "processed": 0, "collected": 0, "failed": 0, "status": "running"}
    _write(output / "progress.json", manifest)
    def one(profile):
        path = output / f"{profile.instrument_id}.json"
        if path.exists():
            prior = json.loads(path.read_text(encoding="utf-8"))
            if prior.get("taxonomy_revision") == status.catalog_revision and prior.get("collection_status") == "collected":
                return prior
        try:
            source = fetch_stock_sector_evidence(profile.instrument_id)
            context = "+".join(source.memberships)
            business = [s.text for s in source.business_sections]
            candidates = infer_current_market_categories(context, business)
            row = {"instrument_id": profile.instrument_id, "name": profile.name,
                   "taxonomy_revision": status.catalog_revision, "collection_status": "collected",
                   "source": source.model_dump(mode="json"),
                   "candidate_sectors": list(dict.fromkeys(m.category_name for m in candidates)),
                   "review_status": "pending", "flags": ["market_primary_not_verified",
                       "official_disclosure_crosscheck_required"],
                   "business_background": profile.primary_business_name}
        except Exception as exc:
            row = {"instrument_id": profile.instrument_id, "name": profile.name,
                   "taxonomy_revision": status.catalog_revision, "collection_status": "failed",
                   "review_status": "pending", "error": type(exc).__name__,
                   "flags": ["evidence_acquisition_failed"]}
        _write(path, row)
        time.sleep(0.2)
        return row
    with ThreadPoolExecutor(max_workers=2) as executor:
        for row in executor.map(one, profiles):
            manifest["processed"] += 1
            manifest["collected" if row["collection_status"] == "collected" else "failed"] += 1
            manifest["updated_at"] = datetime.now(SHANGHAI).isoformat()
            _write(output / "progress.json", manifest)
            if manifest["processed"] % 100 == 0:
                print(json.dumps({k: v for k, v in manifest.items() if k != "instrument_ids"}), flush=True)
    manifest["status"] = "acquisition_complete_review_pending"
    _write(output / "progress.json", manifest)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    result = run(args.output, limit=args.limit)
    print(json.dumps({k: v for k, v in result.items() if k != "instrument_ids"}))
