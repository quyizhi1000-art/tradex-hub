import json,pathlib
from datetime import datetime
from tradex.data_sources.tushare_client import request
p=pathlib.Path('work/state_owned_screen')
for api in ['dc_index','tdx_index']:
 try:
  d=request(api,{'trade_date':'20260910'});rows=list(d.records);(p/(api+'_20260910.json')).write_text(json.dumps({'source':'tushare','api':api,'params':{'trade_date':'20260910'},'fetched_at':datetime.now().astimezone().isoformat(),'rows':rows},ensure_ascii=False,default=str),encoding='utf8');print(api,len(rows),rows[:1]);print([x for x in rows if any(t in str(x.get('name') or x.get('name_index') or x.get('index_name') or '') for t in ['国资','国企','改革','持股'])],flush=True)
 except Exception as e: print('ERROR',api,type(e).__name__,str(e)[:100],flush=True)

