from datetime import date
from pathlib import Path
import json
from tradex.data_gateway.macd_price_history import sixty_sessions,fetch_macd_price_histories
from tradex.data_gateway.stock_technicals import fetch_stock_technical_window
from tradex.stock_selection.macd_rules import v4_signal,price_filter_evidence
from tradex.data_sources import register_all_sources,get_router
register_all_sources(); router=get_router()
day=date(2026,9,8);code='603650.SH'; dates=sixty_sessions(day)
t=fetch_stock_technical_window(dates[-6:]);pts=[next(p for p in d.points if p.instrument_id==code) for d in t.days]
h=fetch_macd_price_histories([code],day)[0]
raw,_=router.route_validated('stock_selection_daily_basic',lambda raw,p:raw,trade_date='20260908')
ratio=next(r for r in raw['daily_basic'] if r['ts_code']==code)['volume_ratio']
filters,reason=price_filter_evidence(h,dates=dates,price=pts[-1].close,volume_ratio=ratio)
r={'code':code,'date':str(day),'signal':v4_signal(pts),'price':pts[-1].close,'high60':max(b.high for b in h.bars),'ma5':sum(b.close for b in h.bars[-5:])/5,'volume_ratio':ratio,'price_filter_reason':reason,'filters':filters,'indicators':[dict(date=str(d.trade_date),dif=p.dif,dea=p.dea,j=p.j) for d,p in zip(t.days,pts)]}
Path('work/macd_j_v4_20260918/example-603650-20260908.json').write_text(json.dumps(r,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
print(json.dumps(r,ensure_ascii=False,default=str))
