import json,pathlib
p=pathlib.Path('work/state_owned_screen')
d=json.loads((p/'ths_index_catalog.json').read_text(encoding='utf8'))
print([x for x in d['rows'] if any(t in x['名称'] for t in ['国企','国资','改革','持股'])])
m=json.loads((p/'stock_selection_master.json').read_text(encoding='utf8'))['data']; print('master keys',m.keys());print({k:len(v) for k,v in m.items() if isinstance(v,list)})
from tradex.data_sources import get_router,register_all_sources
register_all_sources();r=get_router()
for tag in ['tszs']:
 try:
  v,s=r.route('ths_index_catalog',tag=tag);(p/(tag+'.json')).write_text(json.dumps({'source':s,'attrs':v.attrs,'rows':v.to_dict('records')},ensure_ascii=False,default=str),encoding='utf8'); print(tag,len(v),[x for x in v.to_dict('records') if any(t in x['名称'] for t in ['国企','国资','改革','持股'])])
 except Exception as e: print(type(e).__name__,str(e)[:150])
