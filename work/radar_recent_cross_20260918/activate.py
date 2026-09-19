"""One bounded migration through the existing analysis runtime and collector."""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from tradex.analysis_jobs import AnalysisJobStore
from tradex.analysis_worker import AnalysisRuntime, exclusive_worker_lock
from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1
from tradex.stock_selection.intraday_macd_j import refresh_watch, read_watch

OUT = Path(__file__).parent
parser = argparse.ArgumentParser()
parser.add_argument('action', choices=['check', 'archive', 'scan'])
args = parser.parse_args()
jobs_path = Path.home()/'.tradex'/'analysis_jobs.sqlite3'
with sqlite3.connect(f'{jobs_path.as_uri()}?mode=ro',uri=True) as db:
    active = db.execute("SELECT capability,state FROM analysis_jobs WHERE state IN ('queued','running')").fetchall()
print(json.dumps({'active_jobs':active}),flush=True)
if args.action == 'check':
    raise SystemExit(1 if any(state == 'running' for _, state in active) else 0)
if args.action == 'archive':
    assert not any(state == 'running' for _, state in active), 'Running jobs must finish before migration'
    snapshot = DailyStockFactorSnapshotV1.model_validate_json(Path('work/radar_state_v2_20260918/baseline-input.json').read_text(encoding='utf-8'))
    def hashes(path):
        with sqlite3.connect(path) as db:
            return {table: dict(db.execute(f'SELECT {key}, payload_digest FROM {table}'))
                for table,key in [('daily_stock_selections','selection_id'),
                                  ('stock_selection_strategy_results','result_id'),
                                  ('daily_stock_selection_outcomes','selection_id'),
                                  ('stock_selection_strategy_outcomes','outcome_id')]}
    with exclusive_worker_lock(), AnalysisJobStore() as jobs:
        runtime = AnalysisRuntime(jobs)
        try:
            before = hashes(runtime.selection_store.db_path)
            with sqlite3.connect(runtime.selection_store.db_path) as source:
                with sqlite3.connect(OUT/'selection-before.sqlite3') as dest:
                    source.backup(dest)
            runtime.selection_service._factor_loader = lambda day: snapshot if day == snapshot.trade_date else None
            response = runtime.selection_service.generate(trade_date=snapshot.trade_date)
            baseline = runtime.selection_store.get_strategy_result(snapshot.trade_date,'macd-j-upturn-main-board',strategy_version='v3')
            assert baseline and baseline.payload.matched_count == 105
            after = hashes(runtime.selection_store.db_path)
            assert all(after[t].get(k)==v for t,rows in before.items() for k,v in rows.items())
            assert sum(len(after[t])-len(before[t]) for t in before) == 1
            published = runtime.materialize_selection_views(force=True)
            (OUT/'baseline-archive.json').write_text(baseline.model_dump_json(indent=2),encoding='utf-8')
            receipt = {'result_id': baseline.result_id,'matched':baseline.payload.matched_count,
                       'generated_at':baseline.generated_at.isoformat(),'published_views':published,
                       'old_archives_unchanged':True,'new_archive_count':1}
            (OUT/'archive-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(receipt,ensure_ascii=False),flush=True)
        finally:
            runtime.close()
if args.action == 'scan':
    root = Path.home()/'.tradex'/'intraday_macd_j'
    before = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('scan-*.json')}
    raw = refresh_watch(force=True)
    (OUT/'forced-raw.json').write_text(json.dumps(raw,ensure_ascii=False,indent=2),encoding='utf-8')
    displayed = read_watch()
    (OUT/'forced-display.json').write_text(json.dumps(displayed,ensure_ascii=False,indent=2),encoding='utf-8')
    assert raw.get('screen_version') == 'macd-j-upturn-main-board.v3'
    assert displayed.get('comparison',{}).get('status') == 'available', displayed.get('message')
    assert raw['evaluated_count'] > 0
    assert all(hashlib.sha256((root/n).read_bytes()).hexdigest()==sha for n,sha in before.items())
    receipt = {key:displayed.get(key) for key in ('generated_at','screen_version','status','requested_count','evaluated_count','excluded_counts','quote_provider_counts','comparison')}
    receipt['old_scan_archives_unchanged'] = True
    from collections import Counter
    receipt['raw_cross_ages'] = dict(Counter(c['macd_cross_age_sessions'] for c in raw['scan_candidates']))
    receipt['new_cross_ages'] = dict(Counter(c['macd_cross_age_sessions'] for c in displayed['scan_candidates']))
    assert all(0 <= c['macd_cross_age_sessions'] <= 2 for c in raw['scan_candidates'])
    (OUT/'scan-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(receipt,ensure_ascii=False,indent=2),flush=True)
