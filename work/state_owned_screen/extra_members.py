import json,pathlib
from datetime import datetime
from tradex.data_sources.tushare_client import request
p=pathlib.Path('work/state_owned_screen')
selected={'dc':['央国企改革','沪企改革','证金持股'],'tdx':['国开持股','证金汇金持股','活跃小盘国企','大基金持股']}
for typ,names in selected.items():
 cat=json.loads((p/(typ+'_index_20260910.json')).read_text(encoding='utf8'))['rows']
 for name in names:
  c=next(x for x in cat if x['name']==name);params={'trade_date':'20260910','ts_code':c['ts_code']}
  try:
   res=request(typ+'_member',params);rows=list(res.records)
   assert rows and len(rows)<(3000 if typ=='tdx' else 5000)
   assert len({x['con_code'] for x in rows})==len(rows)
   assert all(x['trade_date']=='20260910' and x['ts_code']==c['ts_code'] for x in rows)
   if typ=='tdx':assert len(rows)==c['idx_count'],(len(rows),c['idx_count'])
   payload={'source':'tushare','origin':typ,'concept':name,'api':typ+'_member','params':params,'fetched_at':datetime.now().astimezone().isoformat(),'rows':rows}
   (p/(c['ts_code']+'.json')).write_text(json.dumps(payload,ensure_ascii=False,default=str),encoding='utf8');print(typ,name,len(rows),flush=True)
  except Exception as e:print('ERROR',name,type(e).__name__,str(e)[:120],flush=True)
