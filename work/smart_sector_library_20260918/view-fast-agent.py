import sys,runpy,json
start=int(sys.argv[1]); end=int(sys.argv[2]); sys.argv=['x','0','0']
h=runpy.run_path('work/smart_sector_library_20260918/review_packet.py'); p=h['root']
for i,s in enumerate(h['stocks'][start:end],start):
 d=json.loads((p/'full_market'/(s['id']+'.json')).read_text(encoding='utf-8'))['source']
 print('\n',i,s['id'],s['name'])
 print('BUS','\n'.join(x['text'] for x in d['business_sections'] if x['kind']=='主营业务'))
 print('EM',','.join(d['memberships']))
 print('THS',','.join(x['name'] for x in h['ths_rows'](s)))
