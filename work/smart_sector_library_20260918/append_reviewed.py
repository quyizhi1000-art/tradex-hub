"""Persist two individually researched dossiers; sources were read in this task."""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

target=Path('tradex/src/tradex/smart_sector_library/market_membership_evidence.v2.json')
payload=json.loads(target.read_text(encoding='utf-8'))
stamp=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
def source(key,kind,publisher,title,url,published,assertions):
    return dict(evidence_id=key,source_kind=kind,publisher=publisher,title=title,
                source_url=url,published_at=published,retrieved_at=stamp,assertions=assertions)
def candidate(key,name,chain,role,business,market,rationale):
    return dict(sector_key=key,sector_name=name,chain=chain,relation='direct_business',stage='revenue',
                market_role=role,business_evidence_ids=business,market_evidence_ids=market,rationale=rationale)

midea=dict(instrument_id='000333.SZ',name='美的集团',reviewed_on='2026-09-18',effective_from='2026-09-18',review_due='2026-10-18',
 candidates=[candidate('home_appliances','家用电器',['白色家电','智能家居'],'primary',['midea-h1-2026'],['midea-market-20260828'],
 '公司披露与当前市场报道共同以家电龙头定位为基础；机器人是已有实际业务的次级方向，现有证据未显示其替代家电成为该股票的主认知。'),
 candidate('industrial_automation','工业自动化',['工业机器人','库卡'],'secondary',['midea-h1-2026'],['midea-market-20260828'],'机器人与自动化已形成实际收入，保留业务标签，但不直接推断为人形机器人主归属。')],
 evidence=[source('midea-h1-2026','official_filing','美的集团','2026年半年度报告',
 'https://disc.static.szse.cn/disc/disk03/finalpage/2026-08-29/df25443f-d67a-4cc5-8bd6-6c3e681a9575.PDF','2026-08-29',
 ['智能家居含空调、冰箱、洗衣机等家电产品','公司同时经营机器人与自动化等商业工业业务']),
 source('midea-market-20260828','web_secondary','同花顺转载新浪科技','美的集团2026年半年报报道',
 'https://news.10jqka.com.cn/field/20260828/679407303.shtml','2026-08-28',
 ['报道继续以白色家电领先地位描述美的，机器人业务作为增量方向','库卡中国在工业机器人市场已有实际销售'])],notes=[])
jinrui=dict(instrument_id='600714.SH',name='金瑞矿业',reviewed_on='2026-09-18',effective_from='2026-09-18',review_due='2026-10-18',
 candidates=[candidate('glass_substrate','玻璃基板',['液晶玻璃上游','电子级碳酸锶'],'primary',['jinrui-h1-2026'],['jinrui-ths-concept','jinrui-market-events'],
 '近期多个交易日的市场题材记录持续围绕玻璃基板上游；公司披露证实电子级碳酸锶用于液晶玻璃，支持该市场概念。细分限定为液晶显示材料，不据此声称公司已向半导体先进封装供应材料。'),
 candidate('small_metals','小金属',['锶盐','金属锶'],'secondary',['jinrui-h1-2026'],['jinrui-ths-concept'],'锶盐、金属锶与铝锶合金是实际产品；小金属保留为次级标签，不能用主营行业抹去近期玻璃基板认知。')],
 evidence=[source('jinrui-h1-2026','official_filing','金瑞矿业','2026年半年度报告',
 'https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12556036&stockid=600714','2026-08-28',
 ['电子级碳酸锶用于液晶玻璃生产，已有生产销售业务','金属锶、铝锶合金等属于公司实际产品','该材料用途说明不构成半导体先进封装供货证明']),
 source('jinrui-ths-concept','provider_membership','同花顺','金瑞矿业概念解析',
 'https://basic.10jqka.com.cn/600714/concept.html',None,
 ['概念页包括玻璃基板和小金属','玻璃基板解析引用2026年6月3日互动说明，产品用途为液晶玻璃基板']),
 source('jinrui-market-events','web_secondary','打板客','金瑞矿业题材事件记录',
 'https://dabanke.com/gupiao-600714.html',None,
 ['2026年8月21日、25日、28日及9月3日的题材记录多次出现玻璃基板上游与碳酸锶','事件记录用于市场认知证据，不作为涨跌原因的因果证明'])],
 notes=['拒绝把液晶显示玻璃基板直接等同于半导体封装玻璃基板；当前结论为市场概念归属，非供应链订单确认。'])
ids={e['instrument_id'] for e in payload['entries']}
for entry in [midea,jinrui]:
    if entry['instrument_id'] not in ids: payload['entries'].append(entry)
from tradex.smart_sector_library.catalog import MarketSectorDossierV2
for entry in payload['entries']: MarketSectorDossierV2.model_validate(entry)
target.write_bytes((json.dumps(payload,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))
print('Validated reviewed dossiers:',len(payload['entries']))
