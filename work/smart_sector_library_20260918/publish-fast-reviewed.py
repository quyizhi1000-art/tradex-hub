"""Validate explicit analyst files, normalize existing keys, publish serially and read back."""
import hashlib,json,runpy,sys,urllib.request
from pathlib import Path
from sector_source_guard import validate_source_sector
from tradex.smart_sector_library.catalog import MarketSectorDossierV2
r=Path(__file__).parent
target=Path('tradex/src/tradex/smart_sector_library/market_membership_evidence.v2.json')
before=json.loads(target.read_text(encoding='utf-8'))
latest={}
for e in before['entries']:
 if e['instrument_id'] not in latest or e['effective_from']>latest[e['instrument_id']]['effective_from']:latest[e['instrument_id']]=e
names={c['sector_name']:c['sector_key'] for e in latest.values() for c in e['candidates']}
keys={v:k for k,v in names.items()}
rows=[];seen=set()
for filename in sys.argv[1:]:
 for row in json.loads(Path(filename).read_text(encoding='utf-8')):
  if row['id'] in latest:continue
  assert row['id'] not in seen,('duplicate input',row['id'])
  seen.add(row['id'])
  assert all(row.get(k) for k in ['key','sector','chain','stage','fact','ths_fact','company_fact','reason','source_sector_name','source_sector_provider']),row['id']
  validate_source_sector(r,row)
  if row['sector'] in names:row['key']=names[row['sector']]
  assert row['key'] not in keys or keys[row['key']]==row['sector'],('key collision',row['id'],row['key'])
  names[row['sector']]=row['key'];keys[row['key']]=row['sector'];rows.append(row)
if not rows:print('No unpublished judgments');sys.exit(0)
digest=hashlib.sha256(json.dumps(rows,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:12]
batch=r/f'fast-publish-{digest}.json'
batch.write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
sys.argv=['publish-review-batch.py',str(batch)]
runpy.run_path(str(r/'publish-review-batch.py'),run_name='__main__')
after=json.loads(target.read_text(encoding='utf-8'))
assert all(e in after['entries'] for e in before['entries']),'prior records changed'
for e in after['entries']:MarketSectorDossierV2.model_validate(e)
api=json.load(urllib.request.urlopen('http://127.0.0.1:8765/api/smart-sector-library',timeout=30))
actual={e['instrument_id']:e for e in api['items']}
assert all(actual[j['id']]['status']=='verified' and actual[j['id']]['primary_sector_name']==j['sector'] for j in rows),'published result differs'
receipt={k:api.get(k) for k in ['total','reviewed_total','verified_total','pending_total','revision']}
receipt.update(published=len(rows),instrument_ids=[j['id'] for j in rows],batch=str(batch))
(r/f'fast-receipt-{digest}.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(receipt,ensure_ascii=False))
