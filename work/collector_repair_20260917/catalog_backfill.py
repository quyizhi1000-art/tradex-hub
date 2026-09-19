import json, logging, time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
from tradex.dashboard.collector_worker import exclusive_worker_lock
from tradex.market_watch.sector_catalog import SectorCatalogStore, expected_minutes
from tradex.data_gateway.sector_flow import fetch_sector_intraday_fund_flow
from tradex.data_gateway.sector_flow_store import SectorFundFlowStore
from tradex.dashboard.sector_catalog_collector import refresh_sector_catalog

logging.basicConfig(level=logging.ERROR)
clock=lambda: datetime.now(ZoneInfo('Asia/Shanghai'))
folder=Path(__file__).parent
log_path=folder/'catalog-backfill-20260917.jsonl'
stop_path=folder/'catalog-backfill-20260917.stop'
with exclusive_worker_lock(), SectorCatalogStore() as reader, SectorFundFlowStore() as store:
    catalog=reader.latest(); day=date.fromisoformat(catalog.trade_date)
    assert day==date(2026,9,17)
    required=set(expected_minutes(datetime(2026,9,17,15,0,tzinfo=ZoneInfo('Asia/Shanghai'))))
    existing=store.get_all_best(day)
    complete={key for key,s in existing.items() if required.issubset({p.provider_as_of.strftime('%H:%M') for p in s.points})}
    targets=sorted((e for e in catalog.entries if e.missing_minutes and e.curve_support=='same_source' and e.sector_key not in complete),key=lambda e:(e.hot_state=='none',e.name))
    baseline=store.get_targets(day)
    start=time.perf_counter();saved=failed=consecutive=processed=0;paused=False
    with log_path.open('a',encoding='utf-8',buffering=1) as log:
        def event(value):
            log.write(json.dumps(value,ensure_ascii=False,default=str)+'\n');log.flush()
        event({'event':'resumed','at':clock(),'remaining':len(targets),'already_complete':len(complete),'workers':2})
        print(json.dumps({'remaining':len(targets),'already_complete':len(complete),'workers':2}),flush=True)
        publication=refresh_sector_catalog(schedule_backfill=False)
        event({'event':'published','at':clock(),'result':publication})
        print(json.dumps({'published':publication},ensure_ascii=False),flush=True)
        def fetch(e):
            t=time.perf_counter()
            result=fetch_sector_intraday_fund_flow(sector_key=e.sector_key,name=e.name,taxonomy=e.taxonomy,
                provider_sector_code=e.provider_sector_code,trading_date=day,now=clock(),max_queue_wait=4,request_timeout=4)
            return result,round(time.perf_counter()-t,3)
        iterator=iter(targets)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending={}
            def add():
                e=next(iterator,None)
                if e is not None: pending[pool.submit(fetch,e)]=e
            add();add()
            while pending:
                done,_=wait(pending,return_when=FIRST_COMPLETED)
                for future in done:
                    e=pending.pop(future);processed+=1
                    try:
                        curve,elapsed=future.result()
                        assert curve.trading_date==day
                        receipt=store.record(curve)
                        restored=store.get_best(day,e.sector_key)
                        assert restored is not None and len(restored.points)>=len(curve.points)
                        saved+=1;consecutive=0
                        event({'event':'saved','key':e.sector_key,'name':e.name,'before_points':e.point_count,
                               'saved_points':len(restored.points),'last':restored.points[-1].provider_as_of,
                               'elapsed_seconds':elapsed,'receipt':receipt})
                    except Exception as error:
                        failed+=1;consecutive+=1
                        event({'event':'failed','key':e.sector_key,'name':e.name,'error':str(error)[:400]})
                    if consecutive>=3 or stop_path.exists() or time.perf_counter()-start>1200: paused=True
                    if not paused:add()
                if processed%25==0 or not pending:
                    print(json.dumps({'processed':processed,'remaining_at_start':len(targets),'saved':saved,'failed':failed,
                                      'elapsed_seconds':round(time.perf_counter()-start),'paused':paused}),flush=True)
                if processed%25==0 or not pending:
                    publication=refresh_sector_catalog(schedule_backfill=False)
                    event({'event':'published','at':clock(),'result':publication})
                    print(json.dumps({'published':publication},ensure_ascii=False),flush=True)
        unchanged=store.get_targets(day)==baseline
        event({'event':'finished','at':clock(),'processed':processed,'saved':saved,'failed':failed,
               'unattempted':len(targets)-processed,'legacy_targets_unchanged':unchanged,'paused':paused})
        assert unchanged
