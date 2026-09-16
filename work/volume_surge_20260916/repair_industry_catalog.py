"""Authorized catalog refresh; never regenerate or rewrite selection archives."""
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tradex.analysis_jobs import AnalysisJobStore
from tradex.analysis_worker import AnalysisRuntime, exclusive_worker_lock
from tradex.instrument_taxonomy.service import InstrumentTaxonomyService
from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader, default_db_path


OUT = Path(__file__).parent
NOW = datetime.now(ZoneInfo("Asia/Shanghai"))


def archives(runtime):
    results = [item.model_dump(mode="json") for day in ("2026-09-15", "2026-09-16")
               for item in runtime.selection_store.list_strategy_results(day)]
    return hashlib.sha256(json.dumps(results, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


with exclusive_worker_lock(), AnalysisJobStore() as jobs:
    runtime = AnalysisRuntime(jobs)
    try:
        before_archives = archives(runtime)
        with InstrumentTaxonomyReader() as reader:
            before = reader.status()
            before_profiles = reader.all_profiles()
        backup = OUT / f"taxonomy-before-repair-{NOW:%Y%m%d-%H%M%S}.sqlite3"
        with sqlite3.connect(f"file:{Path(default_db_path()).as_posix()}?mode=ro", uri=True) as original:
            with sqlite3.connect(backup) as destination:
                original.backup(destination)
        print(json.dumps({"stage": "refreshing", "backup": str(backup),
                          "prior_missing": sum(p.statistical_industry is None for p in before_profiles)}), flush=True)
        with InstrumentTaxonomyService() as service:
            after = service.refresh(as_of=NOW.date())
        with InstrumentTaxonomyReader() as reader:
            profiles = reader.all_profiles()
            examples = {key: reader.get(key).statistical_industry.model_dump(mode="json")
                        for key in ("603267.SH", "002378.SZ", "001280.SZ")}
        published = runtime.materialize_selection_views(force=True)
        after_archives = archives(runtime)
        assert before_archives == after_archives, "Selection archive changed"
        receipt = {"before": before.model_dump(mode="json"), "after": after.model_dump(mode="json"),
                   "before_missing": sum(p.statistical_industry is None for p in before_profiles),
                   "missing": [{"instrument_id": p.instrument_id, "name": p.name} for p in profiles
                               if p.statistical_industry is None], "examples": examples,
                   "published_views": published, "selection_archives_unchanged": True,
                   "selection_archive_digest": after_archives, "backup": str(backup)}
        (OUT / "industry-repair-receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(receipt, ensure_ascii=False), flush=True)
    finally:
        runtime.close()
