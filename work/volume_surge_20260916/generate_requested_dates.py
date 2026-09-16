"""One-off operator run authorized for 2026-09-15/16 after market close."""
import hashlib
import json
import threading
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import tradex.stock_selection.service as selection_service
from tradex.analysis_jobs import AnalysisJobStore, DAILY_STOCK_SELECTION
from tradex.analysis_worker import AnalysisRuntime, exclusive_worker_lock


OUTPUT = Path(__file__).parent
DATES = (date(2026, 9, 15), date(2026, 9, 16))
assert datetime.now(ZoneInfo("Asia/Shanghai")).date() == DATES[-1]


def digest(model):
    return hashlib.sha256(model.model_dump_json().encode()).hexdigest()


with exclusive_worker_lock(), AnalysisJobStore() as jobs:
    runtime = AnalysisRuntime(jobs)
    stopped = threading.Event()

    def heartbeat():
        while not stopped.is_set():
            jobs.set_runtime_state("running", detail="authorized one-off selection generation")
            stopped.wait(15)

    beat = threading.Thread(target=heartbeat, daemon=True)
    beat.start()
    original_gate = selection_service.MANUAL_SELECTION_START
    try:
        # This process only: actual clock, trading calendar and closed-session
        # checks still apply. The automatic 18:30 setting is never modified.
        selection_service.MANUAL_SELECTION_START = time(15, 30)
        for target in DATES:
            before = {item.result_id: digest(item) for item in
                      runtime.selection_store.list_strategy_results(target)}
            legacy = runtime.selection_store.get_current(target)
            legacy_digest = digest(legacy) if legacy else None
            job = jobs.enqueue(DAILY_STOCK_SELECTION, trade_date=target,
                               trigger="manual-authorized-after-close-once")
            print(json.dumps({"submitted": job}, ensure_ascii=False), flush=True)
            result = runtime.execute_next_job()
            assert result and result["job_id"] == job["job_id"], result
            after = runtime.selection_store.list_strategy_results(target)
            after_digests = {item.result_id: digest(item) for item in after}
            assert all(after_digests.get(key) == value for key, value in before.items())
            if legacy_digest:
                assert digest(runtime.selection_store.get_current(target)) == legacy_digest
            receipt = {"job": result, "existing_archives_unchanged": True,
                       "results": [item.model_dump(mode="json") for item in after]}
            (OUTPUT / f"receipt-{target}.json").write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"completed": result, "strategy_count": len(after)},
                             ensure_ascii=False), flush=True)
    finally:
        selection_service.MANUAL_SELECTION_START = original_gate
        stopped.set()
        beat.join()
        runtime.close()
        jobs.set_runtime_state("stopped", detail="one-off selection generation finished")
