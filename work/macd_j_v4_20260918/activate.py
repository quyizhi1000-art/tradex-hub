import json,sqlite3,hashlib
from pathlib import Path
from tradex.analysis_jobs import AnalysisJobStore,DAILY_STOCK_SELECTION
from tradex.analysis_worker import AnalysisRuntime,exclusive_worker_lock
from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1
out=Path(__file__).parent
snapshot=DailyStockFactorSnapshotV1.model_validate_json((out/'snapshot-2026-09-17.json').read_text(encoding='utf-8'))
def hashes(path):
 with sqlite3.connect(path) as db:
  return {t:dict(db.execute(f'SELECT {key},payload_digest FROM {t}')) for t,key in [('daily_stock_selections','selection_id'),('stock_selection_strategy_results','result_id'),('daily_stock_selection_outcomes','selection_id'),('stock_selection_strategy_outcomes','outcome_id')]}
with exclusive_worker_lock(),AnalysisJobStore() as jobs:
 with sqlite3.connect(jobs.db_path) as db:
  assert not db.execute("select 1 from analysis_jobs where state='running'").fetchone()
 runtime=AnalysisRuntime(jobs)
 try:
  before=hashes(runtime.selection_store.db_path)
  with sqlite3.connect(runtime.selection_store.db_path) as db,sqlite3.connect(out/'selection-before.sqlite3') as backup:
   db.backup(backup)
  runtime.selection_service._factor_loader=lambda day:snapshot if day==snapshot.trade_date else None
  runtime.selection_service._price_history_loader=lambda *a:snapshot.technicals.price_histories
  runtime.selection_service.generate(trade_date=snapshot.trade_date)
  result=runtime.selection_store.get_strategy_result(snapshot.trade_date,'macd-j-upturn-main-board',strategy_version='v4')
  assert result and result.payload.matched_count==7 and result.payload.pending_count==39
  after=hashes(runtime.selection_store.db_path)
  assert all(after[t].get(k)==v for t,rs in before.items() for k,v in rs.items())
  assert sum(len(after[t])-len(before[t]) for t in before)==1
  published=runtime.materialize_selection_views(force=True)
  (out/'archive-2026-09-17.json').write_text(result.model_dump_json(indent=2),encoding='utf-8')
  receipt=dict(result_id=result.result_id,generated_at=result.generated_at.isoformat(),confirmed=7,pending=39,old_archives_unchanged=True,new_archive_count=1,published=published)
  (out/'activation.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
  print(json.dumps(receipt),flush=True)
  job=jobs.enqueue(DAILY_STOCK_SELECTION,trade_date='2026-09-18',trigger='user:macd-j-v4-verification')
  (out/'job-2026-09-18.json').write_text(json.dumps(job,ensure_ascii=False,default=str,indent=2),encoding='utf-8')
  print(json.dumps({'job_id':job['job_id'],'state':job['state']}),flush=True)
 finally:
  runtime.close()
