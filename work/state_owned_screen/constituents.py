import json,pathlib,time
from tradex.data_sources import get_router,register_all_sources
p=pathlib.Path('work/state_owned_screen');register_all_sources();r=get_router()
cat=json.loads((p/'ths_index_catalog.json').read_text(encoding='utf8'))['rows']
names=['国企改革','央企国企改革','上海国企改革','深圳国企改革','国家大基金持股','证金持股']
for name in names:
 row=next(x for x in cat if x['名称']==name)
 try:
  v,s=r.route('ths_index_constituents',index_code=row['同花顺指数代码'])
  d={'concept':name,'index_code':row['同花顺指数代码'],'source':s,'attrs':v.attrs,'rows':v.to_dict('records')}
  (p/(row['同花顺指数代码']+'.json')).write_text(json.dumps(d,ensure_ascii=False,default=str),encoding='utf8')
  print(name,len(v),v.attrs,flush=True)
 except Exception as e:print('FAILED',name,type(e).__name__,str(e)[:200],flush=True)
