"""Compare the user's 12 picks with the previously frozen 10, as of 9/16."""
import csv,json,statistics,hashlib
from pathlib import Path
from tradex.data_gateway import fetch_intraday_minute_series_batch_partial
from tradex.data_gateway.contracts import IntradayMinuteSeriesV1

OUT=Path(__file__).parent;BASE=OUT.parent;PREV=BASE/'picks_0917'
def load(p):return json.loads(p.read_text(encoding='utf-8'))
def dump(n,v):(OUT/n).write_text(json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
names=['长裕集团','夏厦精密','三祥新材','西昌电力','鸿远电子','百达精工','赛伍技术','八方股份','深南电路','华阳股份','宝新能源','吉大正元']
allrows=load(PREV/'all166-evidence.json');by_name={r['name']:r for r in allrows}
user=[by_name[n] for n in names]
frozen=load(PREV/'frozen-top10.json');assistant=frozen['selected']
snapshot=load(BASE/'snapshot-2026-09-16.json')
histories={h['instrument_id']:h['bars'] for h in snapshot['candlestick_histories']}
curves={}
for r in user:
    key=r['instrument_id']
    for parent in (OUT,PREV):
        p=parent/f'minutes-{key}.json'
        if p.exists():
            curves[key]=IntradayMinuteSeriesV1.model_validate_json(p.read_text(encoding='utf-8'))
            break
missing=[r['instrument_id'] for r in user if r['instrument_id'] not in curves]
if missing:
    curves.update(fetch_intraday_minute_series_batch_partial(missing))
minute_evidence=[]
for r in user:
    key=r['instrument_id'];s=curves[key];ps=s.points
    assert s.trading_date.isoformat()=='2026-09-16'
    assert len(ps)==241 and ps[-1].minute.strftime('%H:%M')=='15:00'
    assert abs(ps[-1].price-r['close'])<.011
    assert abs(max(p.high for p in ps)-r['day_high'])<.011
    assert abs(min(p.low for p in ps)-r['day_low'])<.011
    day=next(b for b in histories[key] if b['trade_date']=='2026-09-16')
    volume_ratio=sum(p.volume_shares for p in ps)/day['volume_shares']
    assert abs(volume_ratio-1)<.001
    (OUT/f'minutes-{key}.json').write_text(s.model_dump_json(),encoding='utf-8')
    valid=[p for p in ps if p.cumulative_average_price is not None]
    p1400=next(p for p in ps if p.minute.strftime('%H:%M')=='14:00')
    p1000=next(p for p in ps if p.minute.strftime('%H:%M')=='10:00')
    m=dict(instrument_id=key,name=r['name'],points=len(ps),date=str(s.trading_date),
        quality=s.metadata.quality.value,flags=list(s.metadata.quality_flags),
        close_below_high_pct=(r['close']/r['day_high']-1)*100,
        last_hour_return=(r['close']/p1400.price-1)*100,
        return_at1000=(p1000.price/day['previous_close']-1)*100,
        vwap_available=len(valid),above_vwap_fraction=sum(p.price>=p.cumulative_average_price-.005 for p in valid)/len(valid) if valid else None,
        close_vs_vwap_pct=(r['close']/ps[-1].cumulative_average_price-1)*100 if ps[-1].cumulative_average_price else None,
        minute_daily_volume_ratio=volume_ratio)
    minute_evidence.append(m)
    print(json.dumps(m,ensure_ascii=False),flush=True)

def summarize(rs):
    keys=['change_pct','volume_multiple','last_event_age','anchor_margin_pct','close_position','turnover_pct','float_cap_100m','five_day_return_pct','breakout_distance_pct']
    return dict(n=len(rs),medians={k:statistics.median(r[k] for r in rs) for k in keys},
        new_event_today=sum(r['last_event_age']==0 for r in rs),
        volume_below_5d_average=sum(r['volume_multiple']<1 for r in rs),
        near_anchor_within_5pct=sum(r['anchor_margin_pct']<5 for r in rs),
        close_above_prior14_high=sum(r['breakout_distance_pct']>0 for r in rs),
        signal_age_at_least4=sum(r['last_event_age']>=4 for r in rs),
        close_below_anchor_close=sum(r['current_vs_anchor_close_pct']<0 for r in rs),
        already_has_subsequent_days=sum(r['min_post_anchor_close'] is not None for r in rs))
summary=dict(cutoff='2026-09-16',name_assumptions={'鸿源电子':'鸿远电子 603267','百大精工':'百达精工 603331'},
    user=summarize(user),assistant=summarize(assistant),overlap=set(r['instrument_id'] for r in user)&set(r['instrument_id'] for r in assistant),
    anchor_signal_expiring_tomorrow_if_no_new_event=[r['name'] for r in user if r['last_event_age']==6],
    assistant_frozen_file_sha256=hashlib.sha256((PREV/'frozen-top10.json').read_bytes()).hexdigest())
summary['overlap']=sorted(summary['overlap'])
dump('comparison-summary.json',summary);dump('user12-daily.json',user);dump('user12-minute.json',minute_evidence)
flat=[]
for r in user:
    m=next(m for m in minute_evidence if m['instrument_id']==r['instrument_id'])
    flat.append({k:v for k,v in {**r,**m}.items() if k!='score_components'})
with (OUT/'用户12只证据明细.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=list(flat[0]));w.writeheader();w.writerows(flat)
print('SUMMARY',json.dumps(summary,ensure_ascii=False))
