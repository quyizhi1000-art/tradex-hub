"""Apply the gateway-validated THS read from this repair run, then publish views."""
import hashlib
import json
from pathlib import Path

from tradex.analysis_jobs import AnalysisJobStore
from tradex.analysis_worker import AnalysisRuntime, exclusive_worker_lock
from tradex.instrument_taxonomy.service import InstrumentTaxonomyService
from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader


OUT = Path(__file__).parent
source = json.loads((OUT / "ths-industry-source.json").read_text(encoding="utf-8"))


def preserved_profiles():
    with InstrumentTaxonomyReader() as reader:
        return [p.model_dump(mode="json", exclude={"market_industry"}) for p in reader.all_profiles()]


def archive_digest(runtime):
    rows = [item.model_dump(mode="json") for day in ("2026-09-15", "2026-09-16")
            for item in runtime.selection_store.list_strategy_results(day)]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


with exclusive_worker_lock(), AnalysisJobStore() as jobs:
    runtime = AnalysisRuntime(jobs)
    try:
        before = preserved_profiles()
        archive_before = archive_digest(runtime)
        with InstrumentTaxonomyService() as service:
            status = service.refresh_market_industries(source_loader=lambda _: source)
        assert preserved_profiles() == before
        assert archive_digest(runtime) == archive_before
        count = runtime.materialize_selection_views(force=True)
        receipt = {"status": status.model_dump(mode="json"), "published_views": count,
                   "non_market_profile_fields_unchanged": True,
                   "selection_archives_unchanged": True, "archive_digest": archive_before,
                   "mapped": len(source["memberships"]), "missing": source["missing_instruments"],
                   "conflicts": source["conflicting_instruments"]}
        (OUT / "market-industry-publish-receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"revision": status.catalog_revision, "views": count,
                          "preserved": True, "mapped": receipt["mapped"]}), flush=True)
    finally:
        runtime.close()
