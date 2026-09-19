"""Recalculate only the authorized volume strategy, from identical archived evidence."""
import hashlib
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tradex.analysis_jobs import AnalysisJobStore
from tradex.analysis_worker import AnalysisRuntime, exclusive_worker_lock
from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1
from tradex.stock_selection.strategies import (
    REGISTERED_STOCK_SELECTION_STRATEGIES, build_strategy_results, snapshot_revision,
)


OUT = Path(__file__).parent
STRATEGY = next(item for item in REGISTERED_STOCK_SELECTION_STRATEGIES
                if item.strategy_id == 'upward-volume-surge-main-board')
assert STRATEGY.strategy_version == 'v3' and not STRATEGY.requires_legacy_selection


def records(store):
    with sqlite3.connect(store.db_path) as db:
        return {table: dict(db.execute(f'SELECT {key}, payload_json FROM {table}')) for table, key in (
            ('daily_stock_selections', 'selection_id'),
            ('stock_selection_strategy_results', 'result_id'),
            ('daily_stock_selection_outcomes', 'selection_id'),
            ('stock_selection_strategy_outcomes', 'outcome_id'),
        )}


with exclusive_worker_lock(), AnalysisJobStore() as jobs:
    runtime = AnalysisRuntime(jobs)
    stopped = threading.Event()

    def heartbeat():
        while not stopped.is_set():
            jobs.set_runtime_state('running', detail='authorized volume-screen v3 recalculation')
            stopped.wait(15)

    beat = threading.Thread(target=heartbeat, daemon=True)
    beat.start()
    try:
        before = records(runtime.selection_store)
        dates = sorted(entry['trade_date'] for entry in runtime.selection_store.list_strategy_dates()
                       if runtime.selection_store.get_strategy_result(entry['trade_date'], STRATEGY.strategy_id))
        backup = OUT / f'selection-before-v3-{datetime.now():%Y%m%d-%H%M%S}.sqlite3'
        with sqlite3.connect(runtime.selection_store.db_path) as db, sqlite3.connect(backup) as saved:
            db.backup(saved)
        receipts = []
        for day in dates:
            old = runtime.selection_store.get_strategy_result(day, STRATEGY.strategy_id, strategy_version='v2')
            assert old is not None, day
            base = Path('work/macd_j_20260917' if day == '2026-09-17' else 'work/volume_surge_research_20260916')
            path = base / f'snapshot-{day}.json'
            raw = path.read_bytes()
            snapshot = DailyStockFactorSnapshotV1.model_validate_json(raw)
            assert snapshot.trade_date.isoformat() == day
            assert snapshot_revision(snapshot) == old.source_snapshot_revision, f'Evidence drift: {day}'
            context = runtime.selection_store.get(day)
            assert context is not None
            context = context.model_copy(update={
                'generated_at': datetime.now(ZoneInfo('Asia/Shanghai')),
                'source_quality': snapshot.metadata.quality.value,
                'source_provider_as_of': snapshot.metadata.provider_as_of,
            })
            current = runtime.selection_store.get_strategy_result(day, STRATEGY.strategy_id, strategy_version='v3')
            if current is None:
                result, = build_strategy_results(snapshot, context, strategies=(STRATEGY,))
                _, current = runtime.selection_store.record_strategy_result(result)
            assert current.source_snapshot_revision == old.source_snapshot_revision
            receipts.append({'date': day, 'old_count': old.payload.matched_count,
                             'new_count': current.payload.matched_count, 'old_result_id': old.result_id,
                             'new_result_id': current.result_id, 'same_source_revision': True,
                             'snapshot_path': str(path), 'snapshot_sha256': hashlib.sha256(raw).hexdigest()})
            print(json.dumps(receipts[-1]), flush=True)
        after = records(runtime.selection_store)
        assert all(after[table].get(key) == value for table, rows in before.items() for key, value in rows.items())
        assert len(after['stock_selection_strategy_results']) - len(before['stock_selection_strategy_results']) <= len(dates)
        published = runtime.materialize_selection_views(force=True)
        (OUT / 'receipt.json').write_text(json.dumps({
            'dates': receipts, 'existing_archives_unchanged': True,
            'published_views': published, 'backup': str(backup),
        }, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        stopped.set()
        beat.join()
        runtime.close()
        jobs.set_runtime_state('stopped', detail='authorized volume-screen v3 recalculation finished')
