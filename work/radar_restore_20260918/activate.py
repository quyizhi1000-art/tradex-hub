"""Restore original rule using existing archive, refresh owner and managed runtime."""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from tradex.analysis_jobs import AnalysisJobStore
from tradex.analysis_worker import AnalysisRuntime, exclusive_worker_lock
from tradex.stock_selection.intraday_macd_j import refresh_watch, read_watch
from tradex.stock_selection.store import read_archived_strategy_result

OUT=Path(__file__).parent
parser=argparse.ArgumentParser()
parser.add_argument('action',choices=['check','publish','scan'])
action=parser.parse_args().action
jobs_path=Path.home()/'.tradex'/'analysis_jobs.sqlite3'
with sqlite3.connect(f'{jobs_path.as_uri()}?mode=ro',uri=True) as db:
    active=db.execute("SELECT capability,state FROM analysis_jobs WHERE state IN ('queued','running')").fetchall()
print(json.dumps({'active_jobs':active}),flush=True)
if action=='check':
    raise SystemExit(1 if any(state=='running' for _,state in active) else 0)
baseline=read_archived_strategy_result('2026-09-17','macd-j-upturn-main-board',strategy_version='v1')
assert baseline and baseline.payload.matched_count==56
(OUT/'baseline-original.json').write_text(baseline.model_dump_json(indent=2),encoding='utf-8')
if action=='publish':
    assert not any(state=='running' for _,state in active)
    with exclusive_worker_lock(), AnalysisJobStore() as jobs:
        runtime=AnalysisRuntime(jobs)
        try:
            def hashes():
                with sqlite3.connect(runtime.selection_store.db_path) as db:
                    return dict(db.execute('SELECT result_id,payload_digest FROM stock_selection_strategy_results'))
            before=hashes()
            published=runtime.materialize_selection_views(force=True)
            assert hashes()==before
            receipt={'published_views':published,'strategy_archives_unchanged':True,'active_strategy':'v1'}
            (OUT/'publish-receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
            print(json.dumps(receipt),flush=True)
        finally:
            runtime.close()
if action=='scan':
    root=Path.home()/'.tradex'/'intraday_macd_j'
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('scan-*.json')}
    raw=refresh_watch(force=True)
    (OUT/'forced-raw.json').write_text(json.dumps(raw,ensure_ascii=False,indent=2),encoding='utf-8')
    displayed=read_watch()
    (OUT/'forced-display.json').write_text(json.dumps(displayed,ensure_ascii=False,indent=2),encoding='utf-8')
    assert raw.get('screen_version')=='macd-j-upturn-main-board.v1'
    assert raw.get('evaluated_count',0)>0
    assert displayed.get('comparison',{}).get('baseline',{}).get('result_id')==baseline.result_id
    assert all(hashlib.sha256((root/name).read_bytes()).hexdigest()==sha for name,sha in before.items())
    seed=json.loads((root/'seed-2026-09-17-st-2026-09-18.json').read_text(encoding='utf-8'))
    points={p['instrument_id']:p for p in seed['points']}
    assert all(points[c['instrument_id']]['dif']<=points[c['instrument_id']]['dea'] for c in raw['scan_candidates'])
    receipt={k:displayed.get(k) for k in ('generated_at','screen_version','status','requested_count','evaluated_count','quote_provider_counts','comparison')}
    receipt.update(old_scans_unchanged=True,all_candidates_pass_original_fresh_cross=True)
    (OUT/'scan-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(receipt,ensure_ascii=False,indent=2),flush=True)
