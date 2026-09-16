"""Minute-price corroboration for the first 30 descriptive shortlist candidates."""
import json,statistics
from pathlib import Path
from tradex.data_gateway import fetch_intraday_minute_series_batch_partial
from tradex.data_gateway.contracts import IntradayMinuteSeriesV1

OUT=Path(__file__).parent
rows=json.loads((OUT/'all166-evidence.json').read_text(encoding='utf-8'))
selected=rows[:30]
curves={r['instrument_id']:IntradayMinuteSeriesV1.model_validate_json((OUT/f"minutes-{r['instrument_id']}.json").read_text(encoding='utf-8'))
        for r in selected if (OUT/f"minutes-{r['instrument_id']}.json").exists()}
missing=[r['instrument_id'] for r in selected if r['instrument_id'] not in curves]
if missing:
    curves.update(fetch_intraday_minute_series_batch_partial(missing))
snapshot=json.loads((OUT.parent/'snapshot-2026-09-16.json').read_text(encoding='utf-8'))
result=[]
for r in selected:
    key=r['instrument_id'];s=curves.get(key)
    if s is None:
        result.append({'instrument_id':key,'name':r['name'],'state':'missing'})
        continue
    assert s.trading_date.isoformat()=='2026-09-16'
    (OUT/f'minutes-{key}.json').write_text(s.model_dump_json(),encoding='utf-8')
    ps=s.points
    assert len(ps)==241 and ps[-1].minute.strftime('%H:%M')=='15:00'
    assert abs(ps[-1].price-r['close'])<.011
    assert abs(max(p.high for p in ps)-r['day_high'])<.011
    assert abs(min(p.low for p in ps)-r['day_low'])<.011
    afternoon=[p for p in ps if p.minute.strftime('%H:%M')>='13:00']
    vwap=[p for p in ps if p.cumulative_average_price is not None]
    p1400=next(p for p in ps if p.minute.strftime('%H:%M')=='14:00')
    p1430=next(p for p in ps if p.minute.strftime('%H:%M')=='14:30')
    p1000=next(p for p in ps if p.minute.strftime('%H:%M')=='10:00')
    previous=r['close']/(1+r['change_pct']/100)
    pclose=(r['close']/max(p.high for p in ps)-1)*100
    final_average=ps[-1].cumulative_average_price
    m={'instrument_id':key,'name':r['name'],'state':'verified','quality':s.metadata.quality.value,
       'flags':list(s.metadata.quality_flags),'point_count':len(ps),
       'change_at1000':(p1000.price/previous-1)*100,'last_hour_return':(r['close']/p1400.price-1)*100,
       'last_halfhour_return':(r['close']/p1430.price-1)*100,
       'close_below_high_pct':pclose,'vwap_available':len(vwap),
       'above_vwap_fraction':sum(p.price>=p.cumulative_average_price-.005 for p in vwap)/len(vwap) if vwap else None,
       'close_vs_vwap_pct':(r['close']/final_average-1)*100 if final_average else None,
       'afternoon_low_change':(min(p.low for p in afternoon)/previous-1)*100,
       'morning_volume_fraction':sum(p.volume_shares for p in ps if p.minute.strftime('%H:%M')<='11:30')/sum(p.volume_shares for p in ps),
       'minute_daily_volume_ratio':None}
    # Read exact canonical daily volume rather than infer it from amount.
    history=next(h for h in snapshot['candlestick_histories'] if h['instrument_id']==key)
    daily=next(b for b in history['bars'] if b['trade_date']=='2026-09-16')
    m['minute_daily_volume_ratio']=sum(p.volume_shares for p in ps)/daily['volume_shares']
    result.append(m)
    print(f"{key} {r['name']:6} 10点涨{m['change_at1000']:.1f}% 尾1h{m['last_hour_return']:.2f}% 距最高{m['close_below_high_pct']:.2f}% 均线完整{m['vwap_available']} 在线比{m['above_vwap_fraction']} 收/均{m['close_vs_vwap_pct']} 午后最低涨{m['afternoon_low_change']:.1f}% 量核对{m['minute_daily_volume_ratio']:.4f}",flush=True)
(OUT/'minute-evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
