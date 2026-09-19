"""Apply explicit analyst judgments to traceable research; no inferred defaults."""
import hashlib,json,sys
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from tradex.smart_sector_library.catalog import MarketSectorDossierV2
from tradex.smart_sector_library.storage import write_json
from sector_source_guard import validate_source_sector
root=Path(__file__).parent
target=Path('tradex/src/tradex/smart_sector_library/market_membership_evidence.v2.json')
original=target.read_bytes()
payload=json.loads(original)
resolve_reviewed='--resolve-reviewed' in sys.argv[2:]
now=datetime.now(ZoneInfo('Asia/Shanghai'));today=now.date()
for judgment in json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')):
 key=judgment['id']
 existing=[e for e in payload['entries'] if e['instrument_id']==key]
 previous=max(existing,key=lambda e:e['effective_from']) if existing else None
 if previous and (not resolve_reviewed or previous['candidates']):continue
 if resolve_reviewed and not previous:
  raise ValueError(f'{key}: resolving requires an existing reviewed dossier')
 if not judgment.get('sector'):
  raise ValueError(f'{key}: review is unfinished until one evidenced existing primary sector is chosen')
 source_sector=validate_source_sector(root,judgment)
 if judgment.get('sector') and not judgment.get('stage'):
  raise ValueError(f'{key}: analyst must explicitly verify product/clinical/order/delivery/revenue stage')
 em_path=root/'full_market'/(key+'.json');em=json.loads(em_path.read_text(encoding='utf-8'))
 ths=None
 for suffix in ['.browser.json','.json','.search.json']:
  path=root/'ths_sequential'/(key+suffix)
  if path.exists():
   row=json.loads(path.read_text(encoding='utf-8'))
   if row.get('status')=='read':ths=row;break
 if not ths:raise ValueError(f'{key}: THS research missing')
 em_source=em['source'];fact=judgment.get('fact') or judgment['gap']
 ths_stamp=ths['retrieved_at'].replace(' UTC','+00:00').replace(' ','T')
 ths_fact=judgment.get('ths_fact') or ('该股概念解析材料已读取；具体业务与主题仍待判别' if judgment.get('gap') else fact)
 company_fact=judgment.get('company_fact') or judgment.get('fact') or ('公司资料列示业务：'+'、'.join(s['title'] for s in em_source['business_sections'] if s['kind']=='主营业务'))
 evidence=[dict(evidence_id=key+'-ths',source_kind=ths.get('evidence_source_kind','provider_membership'),publisher='同花顺',title=ths.get('title') or em['name']+'概念解析',source_url=ths['source_url'],published_at=None,retrieved_at=ths_stamp,assertions=[ths_fact],content_sha256=hashlib.sha256(ths['body'].encode()).hexdigest()),
 dict(evidence_id=key+'-company',source_kind='structured_provider',publisher='东方财富',title=em['name']+'公司业务资料',source_url=em_source['source_url'],published_at=None,retrieved_at=em_source['fetched_at'],assertions=[company_fact],content_sha256=hashlib.sha256(json.dumps(em_source,ensure_ascii=False,sort_keys=True).encode()).hexdigest())]
 for extra in judgment.get('extra_evidence',[]):
  evidence.append({**extra,'retrieved_at':now.isoformat()})
 if source_sector:
  publisher,source_name=source_sector
  source_ref=next(e for e in evidence if e['publisher']==publisher)
  source_ref['assertions'].append('已核对该股票的来源板块原名：'+source_name+'；主归属仅从该股已有行业或概念板块中选取。')
 candidates=[]
 if judgment.get('sector'):
  candidates=[dict(sector_key=judgment['key'],sector_name=judgment['sector'],chain=judgment['chain'],relation=judgment.get('relation','direct_business'),investment_reviewed=judgment.get('investment_reviewed',False),stage=judgment['stage'],market_role='primary',business_review_basis=judgment.get('basis','crosschecked_sources'),business_evidence_ids=[e['evidence_id'] for e in evidence],market_evidence_ids=[e['evidence_id'] for e in evidence if e['source_kind'] in ('provider_membership','web_secondary')],rationale=judgment['reason'])]
 entry=dict(instrument_id=key,name=em['name'],reviewed_on=str(today),effective_from=str(today),review_due=str(today+timedelta(days=30)),candidates=candidates,evidence=evidence,notes=judgment.get('review_notes',[]))
 MarketSectorDossierV2.model_validate(entry)
 if previous and previous['effective_from']==str(today):
  payload['entries'][payload['entries'].index(previous)]=entry
 else:payload['entries'].append(entry)
if resolve_reviewed:
 archive=root/('before-resolving-'+str(today)+'-'+hashlib.sha256(original).hexdigest()[:12]+'.json')
 if not archive.exists():archive.write_bytes(original)
 if archive.read_bytes()!=original:raise ValueError('resolution archive mismatch')
write_json(target,payload)
print('Reviewed dossiers:',len(payload['entries']))
