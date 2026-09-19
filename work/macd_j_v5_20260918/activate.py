import json,sqlite3
from pathlib import Path
from tradex.analysis_jobs import AnalysisJobStore
from tradex.analysis_worker import AnalysisRuntime,exclusive_worker_lock
from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1
out=Path(__file__).parent
snapshots=[DailyStockFactorSnapshotV1.model_validate_json((out/f'snapshot-2026-09-{d}.json').read_text(encoding='utf-8')) for d in (17,18)]
def hashes(path):
 with sqlite3.connect(path) as db:
  return {t:dict(db.execute(f'SELECT {key},payload_digest FROM {t}')) for t,key in [('daily_stock_selections','selection_id'),('stock_selection_strategy_results','result_id'),('daily_stock_selection_outcomes','selection_id'),('stock_selection_strategy_outcomes','outcome_id')]}
with exclusive_worker_lock(),AnalysisJobStore() as jobs:
 with sqlite3.connect(jobs.db_path) as db:
  assert not db.execute("select 1 from analysis_jobs where state='running'").fetchone()
 runtime=AnalysisRuntime(jobs)
 try:
  before=hashes(runtime.selection_store.db_path)
  with sqlite3.connect(runtime.selection_store.db_path) as db,sqlite3.connect(out/'selection-before.sqlite3') as backup:db.backup(backup)
  receipts=[]
  for s in snapshots:
   runtime.selection_service._factor_loader=lambda day:s if day==s.trade_date else None
   runtime.selection_service._price_history_loader=lambda *a:s.technicals.price_histories
   runtime.selection_service.generate(trade_date=s.trade_date)
   r=runtime.selection_store.get_strategy_result(s.trade_date,'macd-j-upturn-main-board',strategy_version='v5')
   assert r
   expected=json.loads((out/f'screen-{s.trade_date}.json').read_text(encoding='utf-8'))
   assert r.payload.model_dump(mode='json')==expected
   old=runtime.selection_store.get_strategy_result(s.trade_date,'macd-j-upturn-main-board',strategy_version='v4')
   for field in ('candidates','pending_candidates'):
    assert {x.instrument_id for x in getattr(old.payload,field)} <= {x.instrument_id for x in getattr(r.payload,field)}
   (out/f'archive-{s.trade_date}.json').write_text(r.model_dump_json(indent=2),encoding='utf-8')
   receipts.append(dict(trade_date=str(s.trade_date),result_id=r.result_id,confirmed=r.payload.matched_count,pending=r.payload.pending_count,old_confirmed=old.payload.matched_count,old_pending=old.payload.pending_count))
  after=hashes(runtime.selection_store.db_path)
  assert all(after[t].get(k)==v for t,rs in before.items() for k,v in rs.items())
  assert sum(len(after[t])-len(before[t]) for t in before)==2
  published=runtime.materialize_selection_views(force=True)
  receipt=dict(days=receipts,old_archives_unchanged=True,new_archive_count=2,published=published)
  (out/'activation.json').write_text(json.dumps(receipt,indent=2,default=str),encoding='utf-8')
  print(json.dumps(receipt,default=str),flush=True)
 finally:runtime.close()
