import json,pathlib,collections,importlib.util
p=pathlib.Path('work/state_owned_screen');m=json.loads((p/'stock_selection_master.json').read_text(encoding='utf8'))['data']['stock_basic'];master={x['ts_code']:x for x in m}
print('master statuses',collections.Counter(x['list_status'] for x in m),'pywencai',bool(importlib.util.find_spec('pywencai')),'openpyxl',bool(importlib.util.find_spec('openpyxl')))
union={};conflicts=[]
for f in p.glob('*.TI.json'):
 d=json.loads(f.read_text(encoding='utf8'))
 for x in d['rows']:
  code=x['同花顺代码']; union.setdefault(code,{'names':set(),'concepts':set()});union[code]['names'].add(x['名称']);union[code]['concepts'].add(d['concept'])
counts=collections.Counter();included=[];excluded=[]
for code,x in union.items():
 t=master.get(code)
 reason='missing_master' if t is None else 'not_listed' if t['list_status']!='L' else 'not_mainboard' if t['market']!='主板' or not code.endswith(('.SH','.SZ')) else 'ST_or_delist' if any('ST' in n.upper() or '退' in n for n in [t['name'],*x['names']]) else 'included'
 counts[reason]+=1
 if reason=='included':included.append((code,t['name'],';'.join(sorted(x['concepts']))))
 else:excluded.append((code,t and t['name'],reason))
 if t and t['name'] not in x['names']:conflicts.append((code,t['name'],list(x['names'])))
print('counts',counts,'name_conflicts',conflicts[:25]);print('exclusions', [x for x in excluded if x[2]!='not_mainboard'][:80]);print('sample',sorted(included)[:20]);print('main markets',collections.Counter(c.split('.')[1] for c,n,t in included));print('extras',[x for x in included if not any(y in x[2] for y in ['国企改革'])])
