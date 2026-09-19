"""Compact source packets for individual analyst review, never automatic labels."""
import json,re,sys
from pathlib import Path
from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader
root=Path(__file__).parent
stocks=json.loads((root/'stocks.json').read_text(encoding='utf-8'))

def ths_rows(stock):
    for suffix in ['.browser.json','.json','.search.json']:
        p=root/'ths_sequential'/(stock['id']+suffix)
        if not p.exists():continue
        raw=json.loads(p.read_text(encoding='utf-8'))
        if raw.get('status')!='read':continue
        if raw.get('concepts'):return raw['concepts']
        body=raw.get('body','');parsed={}
        blocks=body.split('-'*80) if suffix=='.search.json' else [body]
        for block in blocks:
            if suffix=='.search.json' and f"https://basic.10jqka.com.cn/{stock['id'][:6]}/concept.html" not in block.splitlines()[0:2].__str__():continue
            current=None
            for line in block.splitlines():
                line=re.sub(r'^L\d+:\s*','',line)
                m=re.match(r'\d+\s*\|\s*([^|]+?)\s*\|.*',line)
                if m:
                    current={'name':m.group(1).strip(),'explanation':''}
                    parsed.setdefault(current['name'],current)
                elif current and line.strip() and '展开' not in line and not line.lstrip().startswith(('#','---','*','')):
                    current['explanation']+=' '+line.strip()
                    if len(current['explanation'])>len(parsed[current['name']]['explanation']):parsed[current['name']]=current
        return list(parsed.values())
    return []
start=int(sys.argv[1]);count=int(sys.argv[2]) if len(sys.argv)>2 else 10
with InstrumentTaxonomyReader() as reader:
    for stock in stocks[start:start+count]:
        p=reader.get(stock['id']);rows=ths_rows(stock)
        em=root/'full_market'/(stock['id']+'.json')
        company=json.loads(em.read_text(encoding='utf-8')).get('source',{}) if em.exists() else {}
        print(json.dumps({'id':stock['id'],'name':stock['name'],'background':p.business_summary,
              'company':[{'title':s['title'],'text':s['text'][:260]} for s in company.get('business_sections',[]) if s['kind']=='主营业务'],
              'concepts':[{'name':r['name'],'explanation':r['explanation'][:180]} for r in rows]},ensure_ascii=False))
