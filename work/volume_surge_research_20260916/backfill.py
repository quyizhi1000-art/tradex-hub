"""Authorized historical strategy backfill with immutable evidence receipts."""
import hashlib
import json
import sqlite3
import threading
from datetime import date
from pathlib import Path

from tradex.analysis_jobs import AnalysisJobStore, DAILY_STOCK_SELECTION
from tradex.analysis_worker import AnalysisRuntime, exclusive_worker_lock
from tradex.market_calendar import CalendarDayStatus, calendar_day_status

OUT = Path(__file__).parent
DATES = tuple(date(2026, 9, d) for d in (9, 10, 11, 14))
STRATEGY = 'upward-volume-surge-main-board'

def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

def evidence(store):
    with sqlite3.connect(store.db_path) as db:
        return {table: dict(db.execute(f'SELECT {key}, payload_digest FROM {table}'))
                for table, key in [('daily_stock_selections', 'selection_id'),
                                   ('stock_selection_strategy_results', 'result_id'),
                                   ('daily_stock_selection_outcomes', 'selection_id'),
                                   ('stock_selection_strategy_outcomes', 'outcome_id')]}

with exclusive_worker_lock(), AnalysisJobStore() as jobs:
    runtime = AnalysisRuntime(jobs)
    stopped = threading.Event()
    def heartbeat():
        while not stopped.is_set():
            jobs.set_runtime_state('running', detail='authorized September 9-14 volume strategy backfill')
            stopped.wait(15)
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        before_path = OUT / 'archive-before.json'
        before = json.loads(before_path.read_text(encoding='utf-8')) if before_path.exists() else evidence(runtime.selection_store)
        if not before_path.exists():
            dump(before_path, before)
        if not (OUT / 'selection-before.sqlite3').exists():
            with sqlite3.connect(runtime.selection_store.db_path) as source:
                with sqlite3.connect(OUT / 'selection-before.sqlite3') as destination:
                    source.backup(destination)
        loader = runtime.selection_service._factor_loader
        def capture(target):
            snapshot = loader(target)
            (OUT / f'snapshot-{target}.json').write_text(snapshot.model_dump_json(), encoding='utf-8')
            print(json.dumps({'snapshot': str(target), 'factors': len(snapshot.factors),
                              'quality': snapshot.metadata.quality.value}), flush=True)
            return snapshot
        runtime.selection_service._factor_loader = capture
        for target in DATES:
            assert calendar_day_status(target) is CalendarDayStatus.VERIFIED_TRADING_DAY
            existing = runtime.selection_store.list_strategy_results(target)
            assert runtime.selection_store.get_current(target) is not None
            missing = [s.strategy_id for s in __import__('tradex.stock_selection.strategies', fromlist=['REGISTERED_STOCK_SELECTION_STRATEGIES']).REGISTERED_STOCK_SELECTION_STRATEGIES
                       if s.strategy_id not in {r.strategy_id for r in existing}]
            assert missing in ([STRATEGY], []), missing
            if missing:
                job = jobs.enqueue(DAILY_STOCK_SELECTION, trade_date=target, trigger='user-requested-historical-volume-backfill')
                print(json.dumps({'submitted': str(target), 'job_id': job['job_id']}), flush=True)
                result = runtime.execute_next_job()
                assert result and result['job_id'] == job['job_id'], result
            else:
                result = {'state': 'existing'}
            stored = runtime.selection_store.get_strategy_result(target, STRATEGY, strategy_version='v1')
            assert stored is not None, result
            dump(OUT / f'backfill-{target}.json', {'job': result, 'result': stored.model_dump(mode='json')})
            after = evidence(runtime.selection_store)
            assert all(after[table].get(key) == value for table, rows in before.items() for key, value in rows.items())
            print(json.dumps({'completed': str(target), 'matched': stored.payload.matched_count,
                              'existing_archives_unchanged': True}), flush=True)
        runtime.materialize_selection_views(force=True)
        dump(OUT / 'archive-after.json', evidence(runtime.selection_store))
    finally:
        stopped.set()
        thread.join()
        runtime.close()
        jobs.set_runtime_state('stopped', detail='authorized historical backfill finished')
