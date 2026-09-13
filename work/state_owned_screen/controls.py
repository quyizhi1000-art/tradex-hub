import json,pathlib,collections
from datetime import datetime
from tradex.data_sources.tushare_client import request
p=pathlib.Path('work/state_owned_screen')
for api,params,fields in [('stock_basic',{'list_status':'L'},'ts_code,name,market,exchange,list_status,list_date,delist_date,area,industry,act_name,act_ent_type'),('stock_st',{'trade_date':'20260911'},'ts_code,name,trade_date,type,type_name')]:
 try:
  d=request(api,params,fields);rows=list(d.records);payload={'source':'tushare','api':api,'params':params,'fetched_at':datetime.now().astimezone().isoformat(),'request_id':d.request_id,'rows':rows}
  (p/(api+'_extra.json')).write_text(json.dumps(payload,ensure_ascii=False,default=str),encoding='utf8');print(api,len(rows),rows[:2],flush=True)
  if api=='stock_basic':print('controller_types',collections.Counter(x.get('act_ent_type') for x in rows),flush=True)
 except Exception as e: print('ERROR',api,type(e).__name__,str(e)[:200],flush=True)
