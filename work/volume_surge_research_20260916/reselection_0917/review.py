import json
from pathlib import Path
from tradex.data_gateway import fetch_intraday_minute_series_batch_partial

OUT=Path(__file__).parent
BASE=OUT.parent
rows=json.loads((BASE/'picks_0917/all166-evidence.json').read_text(encoding='utf-8'))
snapshot=json.loads((BASE/'snapshot-2026-09-16.json').read_text(encoding='utf-8'))
histories={h['instrument_id']:h['bars'] for h in snapshot['candlestick_histories']}
targets=[r for r in rows if r['name'] in ['征和工业','茶花股份','万润股份']]
curves=fetch_intraday_minute_series_batch_partial([r['instrument_id'] for r in targets])
metrics=[]
for r in targets:
    s=curves.get(r['instrument_id'])
    if s is None or str(s.trading_date)!='2026-09-16':
        print(r['name'],'missing correct dated minute curve',flush=True)
        continue
    ps=s.points
    assert len(ps)==241
    assert abs(ps[-1].price-r['close'])<.011
    assert abs(max(p.high for p in ps)-r['day_high'])<.011
    assert abs(min(p.low for p in ps)-r['day_low'])<.011
    b=histories[r['instrument_id']][-1]
    vr=sum(p.volume_shares for p in ps)/b['volume_shares']
    assert abs(vr-1)<.001
    p14=next(p for p in ps if p.minute.strftime('%H:%M')=='14:00')
    m={'name':r['name'],'instrument_id':r['instrument_id'],'date':str(s.trading_date),'points':len(ps),
       'last_hour_return':(r['close']/p14.price-1)*100,'close_below_high_pct':(r['close']/r['day_high']-1)*100,
       'vwap_available':sum(p.cumulative_average_price is not None for p in ps),
       'quality':s.metadata.quality.value,'flags':list(s.metadata.quality_flags),
       'minute_daily_volume_ratio':vr}
    (OUT/f"minutes-{r['instrument_id']}.json").write_text(s.model_dump_json(),encoding='utf-8')
    metrics.append(m)
    print(json.dumps(m,ensure_ascii=False),flush=True)
(OUT/'additional-minute-review.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2),encoding='utf-8')
