import json,pathlib,collections
p=pathlib.Path('work/state_owned_screen');x=json.loads((p/'stock_basic_extra.json').read_text(encoding='utf8'))['rows'];allc={r['同花顺代码'] for f in p.glob('*.TI.json') for r in json.loads(f.read_text(encoding='utf8'))['rows']};st={r['ts_code'] for r in json.loads((p/'stock_st_extra.json').read_text(encoding='utf8'))['rows']}
new=[r for r in x if r['act_ent_type'] in ['地方国企','中央国企'] and r['ts_code'] not in allc and r['ts_code'] not in st and r['market']=='主板' and '退' not in r['name']]
print('controller supplemental',len(new)); print([(r['ts_code'],r['name'],r['act_ent_type'],r['act_name']) for r in new]);print('school',[(r['ts_code'],r['name'],r['act_name']) for r in x if r['act_ent_type']=='校办企业'])
