"""User-authorized recalculation of every existing volume-screen date, preserving v1."""
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
from tradex.stock_selection.strategies import snapshot_revision


BASE = Path('work/volume_surge_research_20260916')
OUT = Path(__file__).parent
MANIFEST = json.loads((BASE / 'file-provenance.json').read_text(encoding='utf-8'))
STRATEGY = 'upward-volume-surge-main-board'


def immutable_rows(store):
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
            jobs.set_runtime_state('running', detail='authorized volume-screen v2 recalculation')
            stopped.wait(15)

    beat = threading.Thread(target=heartbeat, daemon=True)
    beat.start()
    receipts = []
    try:
        before = immutable_rows(runtime.selection_store)
        dates = sorted({entry['trade_date'] for entry in runtime.selection_store.list_strategy_dates()
                        if runtime.selection_store.get_strategy_result(entry['trade_date'], STRATEGY,
                                                                       strategy_version='v1')})
        backup = OUT / f'selection-before-v2-{datetime.now():%Y%m%d-%H%M%S}.sqlite3'
        with sqlite3.connect(runtime.selection_store.db_path) as db, sqlite3.connect(backup) as saved:
            db.backup(saved)
        for day in dates:
            path = BASE / f'snapshot-{day}.json'
            data = path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            assert sha == MANIFEST[path.name], f'Historical evidence digest changed: {day}'
            snapshot = DailyStockFactorSnapshotV1.model_validate_json(data)
            assert snapshot.trade_date.isoformat() == day
            old = runtime.selection_store.get_strategy_result(day, STRATEGY, strategy_version='v1')
            # The 15/16 snapshots were reacquired during the prior research run.
            # Verify every archived hit against the actual price/volume evidence.
            histories = {item.instrument_id: {bar.trade_date: bar for bar in item.bars}
                         for item in snapshot.candlestick_histories}
            for candidate in old.payload.candidates:
                for event in candidate.evidence:
                    bar = histories[candidate.instrument_id][event.trade_date]
                    assert (event.close, event.previous_close, event.volume_shares) == (
                        bar.close, bar.previous_close, bar.volume_shares)
            runtime.selection_service._factor_loader = lambda _, snap=snapshot: snap
            runtime.selection_service.generate(
                now=datetime.now(ZoneInfo('Asia/Shanghai')), trade_date=snapshot.trade_date,
            )
            current = runtime.selection_store.get_strategy_result(day, STRATEGY, strategy_version='v2')
            assert current is not None
            assert {c.instrument_id for c in current.payload.candidates} <= {c.instrument_id for c in old.payload.candidates}
            after = immutable_rows(runtime.selection_store)
            assert all(after[table].get(key) == value for table, rows in before.items() for key, value in rows.items())
            receipt = {'date': day, 'before': old.payload.matched_count, 'after': current.payload.matched_count,
                       'reset_candidates': sum(c.reset_count > 0 for c in current.payload.candidates),
                       'old_result_id': old.result_id, 'new_result_id': current.result_id,
                       'snapshot_file_sha256': sha, 'snapshot_revision': snapshot_revision(snapshot),
                       'same_original_snapshot_revision': old.source_snapshot_revision == snapshot_revision(snapshot),
                       'archived_hit_prices_and_volumes_match': True, 'existing_archives_unchanged': True}
            receipts.append(receipt)
            print(json.dumps(receipt), flush=True)
        published = runtime.materialize_selection_views(force=True)
        (OUT / 'volume-v2-recalculation-receipt.json').write_text(json.dumps({
            'dates': receipts, 'published_views': published, 'backup': str(backup),
            'existing_archives_unchanged': True,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        stopped.set()
        beat.join()
        runtime.close()
        jobs.set_runtime_state('stopped', detail='authorized volume-screen v2 recalculation finished')
