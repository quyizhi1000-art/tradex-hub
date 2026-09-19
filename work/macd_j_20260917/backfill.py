"""One authorized latest-archive addition via the existing analysis runtime."""
import json
import sqlite3
from datetime import date
from pathlib import Path

from tradex.analysis_jobs import AnalysisJobStore, DAILY_STOCK_SELECTION
from tradex.analysis_worker import AnalysisRuntime, exclusive_worker_lock
from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1
from tradex.data_gateway.stock_technicals_contracts import StockTechnicalWindowV1

OUT = Path(__file__).parent
DAY = date(2026, 9, 16)
STRATEGY = 'macd-j-upturn-main-board'


def digests(store):
    with sqlite3.connect(store.db_path) as db:
        return {table: dict(db.execute(f'SELECT {key}, payload_digest FROM {table}'))
                for table, key in [('daily_stock_selections', 'selection_id'),
                                   ('stock_selection_strategy_results', 'result_id'),
                                   ('daily_stock_selection_outcomes', 'selection_id'),
                                   ('stock_selection_strategy_outcomes', 'outcome_id')]}


snapshot = DailyStockFactorSnapshotV1.model_validate_json(
    Path('work/volume_surge_research_20260916/snapshot-2026-09-16.json').read_text(encoding='utf-8'))
window = StockTechnicalWindowV1.model_validate_json((OUT/'technical_20260916.json').read_text(encoding='utf-8'))
snapshot = DailyStockFactorSnapshotV1.model_validate({**snapshot.model_dump(), 'technicals': window})
assert snapshot.trade_date == DAY

with exclusive_worker_lock(), AnalysisJobStore() as jobs:
    runtime = AnalysisRuntime(jobs)
    try:
        before = digests(runtime.selection_store)
        (OUT/'archive-before.json').write_text(json.dumps(before), encoding='utf-8')
        with sqlite3.connect(runtime.selection_store.db_path) as source:
            with sqlite3.connect(OUT/'selection-before.sqlite3') as destination:
                source.backup(destination)
        assert runtime.selection_store.get_current(DAY) is not None
        runtime.selection_service._factor_loader = lambda _: snapshot
        existing = runtime.selection_store.get_strategy_result(DAY, STRATEGY, strategy_version='v1')
        if existing is None:
            with sqlite3.connect(jobs.db_path) as db:
                assert db.execute("SELECT count(*) FROM analysis_jobs WHERE state IN ('queued','running')").fetchone()[0] == 0
            job = jobs.enqueue(DAILY_STOCK_SELECTION, trade_date=DAY, trigger='user-requested-macd-j-v1')
            result = runtime.execute_next_job()
            assert result and result['job_id'] == job['job_id'], result
        else:
            result = {'state': 'existing'}
        stored = runtime.selection_store.get_strategy_result(DAY, STRATEGY, strategy_version='v1')
        assert stored is not None, result
        after = digests(runtime.selection_store)
        assert all(after[t].get(k) == v for t, rows in before.items() for k, v in rows.items())
        runtime.materialize_selection_views(force=True)
        receipt = {'job': result, 'result': stored.model_dump(mode='json'), 'existing_archives_unchanged': True}
        (OUT/'acceptance.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'matched': stored.payload.matched_count, 'same_day': stored.payload.same_day_count,
            'prior_3_sessions': stored.payload.prior_3_sessions_count, 'existing_archives_unchanged': True}))
    finally:
        runtime.close()
