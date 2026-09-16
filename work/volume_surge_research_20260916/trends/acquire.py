"""Read canonical minute curves and dated limit events for the 46 winners."""
import csv
import json
from pathlib import Path
from tradex.data_gateway import fetch_intraday_minute_series_batch_partial, fetch_limit_up_events

OUT=Path(__file__).parent
BASE=OUT.parent
rows=list(csv.DictReader((BASE/'limit_up_successes.csv').open(encoding='utf-8-sig')))
winners={}
for row in rows:
    winners.setdefault(row['instrument_id'],row)
today_ids=[i for i,r in winners.items() if r['first_limit_date']=='2026-09-16']
manifest={'winner_count':len(winners),'minute_target_date':'2026-09-16','minute_requested':today_ids,'errors':[]}
try:
    curves=fetch_intraday_minute_series_batch_partial(today_ids)
    for instrument,curve in curves.items():
        assert curve.trading_date.isoformat()=='2026-09-16'
        (OUT/f'minutes-{instrument}-2026-09-16.json').write_text(curve.model_dump_json(),encoding='utf-8')
        print(json.dumps({'minute':instrument,'count':len(curve.points),'quality':curve.metadata.quality.value}),flush=True)
    manifest['minute_returned']=list(curves)
except Exception as exc:
    manifest['errors'].append({'operation':'minute_batch','error':str(exc)})
    print(json.dumps(manifest['errors'][-1]),flush=True)
for day in sorted({r['first_limit_date'] for r in winners.values()}):
    try:
        path=OUT/f'limit-events-{day}.json'
        if path.exists():continue
        events=fetch_limit_up_events(day)
        assert events.trading_date.isoformat()==day
        path.write_text(events.model_dump_json(),encoding='utf-8')
        print(json.dumps({'events':day,'count':len(events.events),'provider':events.metadata.provider,'quality':events.metadata.quality.value}),flush=True)
    except Exception as exc:
        manifest['errors'].append({'operation':'limit_events','day':day,'error':str(exc)})
        print(json.dumps(manifest['errors'][-1]),flush=True)
(OUT/'acquisition.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
