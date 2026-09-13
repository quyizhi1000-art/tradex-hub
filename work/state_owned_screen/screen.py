import json,pathlib,collections,unicodedata
p=pathlib.Path('work/state_owned_screen');master={x['ts_code']:x for x in json.loads((p/'stock_basic_extra.json').read_text(encoding='utf8'))['rows']};st={x['ts_code'] for x in json.loads((p/'stock_st_extra.json').read_text(encoding='utf8'))['rows']};u={};cur_names=collections.defaultdict(set)
for suffix in ['TI','DC','TDX']:
 for f in p.glob('*.'+suffix+'.json'):
  d=json.loads(f.read_text(encoding='utf8'));origin={'TI':'同花顺','DC':'东方财富','TDX':'通达信'}[suffix];dt=d['attrs']['provider_as_of'] if suffix=='TI' else '2026-09-10'
  for x in d['rows']:
   code=x['同花顺代码'] if suffix=='TI' else x['con_code'];u.setdefault(code,[]).append({'concept':d['concept'],'origin':origin,'date':dt,'source_file':f.name})
   if suffix=='TI':cur_names[code].add(x['名称'])
for code,x in master.items():
 if x.get('act_ent_type') in ['中央国企','地方国企']:u.setdefault(code,[]).append({'concept':x['act_ent_type'],'origin':'Tushare实控人性质','date':'2026-09-11','source_file':'stock_basic_extra.json'})
keep=[];exclude=[]
for code,evidence in sorted(u.items()):
 m=master.get(code);reason='证券主表无当前上市记录' if m is None else '非沪深主板A股' if m['market']!='主板' or not code.endswith(('.SH','.SZ')) else '当日ST风险警示名单' if code in st else '当前名称含ST或退市标记' if any('ST' in n.upper() or '退' in n for n in [m['name'],*cur_names[code]]) else ''
 row={'code':code,'master':m,'evidence':evidence,'reason':reason}
 (exclude if reason else keep).append(row)
print('result',len(keep),'exclusions',collections.Counter(x['reason'] for x in exclude),'markets',collections.Counter(x['code'][-2:] for x in keep))
print('controller',collections.Counter(x['master']['act_ent_type'] for x in keep))
print('prior_only',[(x['code'],x['master']['name'],[e['concept'] for e in x['evidence']]) for x in keep if all(e['date']=='2026-09-10' for e in x['evidence'])]);print('noncontrol',[(x['code'],x['master']['name'],[e['concept'] for e in x['evidence']]) for x in keep if x['master']['act_ent_type'] not in ['中央国企','地方国企']][:100])
(p/'screened.json').write_text(json.dumps({'included':keep,'excluded':exclude},ensure_ascii=False),encoding='utf8')
