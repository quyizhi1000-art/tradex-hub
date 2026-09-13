import os,json,pathlib,sys
os.environ['PYTHONIOENCODING']='utf-8'
from tradex.data_sources import get_router,register_all_sources
register_all_sources();r=get_router();out=pathlib.Path('work/state_owned_screen')
for typ,params in [('ths_index_catalog',{'tag':'cn_concept'}),('stock_selection_master',{'trade_date':'20260911'})]:
 try:
  value,source=r.route(typ,**params)
  if hasattr(value,'to_dict'): payload={'source':source,'attrs':value.attrs,'rows':value.to_dict('records')};print(typ,source,len(value),str(value.to_dict('records'))[:700])
  else:payload={'source':source,'data':value};print(typ,source,str(value)[:500])
  (out/(typ+'.json')).write_text(json.dumps(payload,ensure_ascii=False,default=str),encoding='utf8')
 except Exception as e: print('ERROR',typ,type(e).__name__,str(e)[:300])
