"""Descriptive winner-only trends; never fits or changes the selection rule."""
import csv,json,statistics,math
from collections import Counter
from pathlib import Path

OUT=Path(__file__).parent
BASE=OUT.parent
def load(p):return json.loads(p.read_text(encoding='utf-8'))
def dump(name,v):(OUT/name).write_text(json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
def csvout(name,rows):
    with (OUT/name).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
def median(v):return statistics.median(v) if v else None

rows=list(csv.DictReader((BASE/'limit_up_successes.csv').open(encoding='utf-8-sig')))
winners={}
for r in rows:winners.setdefault(r['instrument_id'],r)
snapshots={p.stem.removeprefix('snapshot-'):load(p) for p in BASE.glob('snapshot-*.json')}
bars={}
for s in snapshots.values():
    for h in s['candlestick_histories']:
        for b in h['bars']:
            key=(h['instrument_id'],b['trade_date'])
            assert key not in bars or bars[key]==b
            bars[key]=b
for p in BASE.glob('ohlcv-*.json'):
    s=load(p)
    for b in s['bars']:
        key=(s['instrument_id'],b['trading_date'])
        bars.setdefault(key,dict(trade_date=b['trading_date'],open=b['open'],high=b['high'],low=b['low'],close=b['close'],
            previous_close=round(b['close']-b['change'],2),volume_shares=b['volume_shares'],amount_cny=b['amount_cny']))
dates=sorted({d for i,d in bars})
events={}
for p in OUT.glob('limit-events-*.json'):
    s=load(p)
    for e in s['events']:events[(e['instrument_id'],s['trading_date'])]=e
archives={d:next(r for r in load(BASE/f'api-{d}.json')['results'] if r['strategy_id']=='upward-volume-surge-main-board')['payload'] for d in snapshots}
daily=[];minute_rows=[];daily_plot_data={};minute_plot_data={}
for instrument,row in winners.items():
    selection_day=row['signal_date'];limit_day=row['first_limit_date']
    candidate=next(c for c in archives[selection_day]['candidates'] if c['instrument_id']==instrument)
    original_event=candidate['evidence'][-1];event_day=original_event['trade_date']
    idx=dates.index(limit_day);prior_dates=dates[idx-10:idx]
    prior=[bars[(instrument,d)] for d in prior_dates if (instrument,d) in bars]
    assert len(prior)==10,(instrument,prior_dates)
    limit_bar=bars[(instrument,limit_day)];event_bar=bars[(instrument,event_day)]
    entry_bar=bars[(instrument,selection_day)];before=prior[-1]
    between=[bars[(instrument,d)] for d in dates if event_day<d<limit_day and (instrument,d) in bars]
    consolidation=[b for b in between if b['volume_shares']<event_bar['volume_shares']]
    negative=[b for b in between if b['close']<b['previous_close']]
    low_volume_negative=[b for b in negative if b['volume_shares']<event_bar['volume_shares']]
    body_mid=(event_bar['open']+event_bar['close'])/2
    event_span=event_bar['high']-event_bar['low']
    record=dict(instrument_id=instrument,name=row['name'],first_selection=selection_day,first_limit=limit_day,
        selection_to_limit=int(row['first_limit_lag']),original_event=event_day,
        event_to_limit=dates.index(limit_day)-dates.index(event_day),
        event_multiple=original_event['volume_multiple'],event_change_pct=original_event['change_pct'],
        event_bullish_body=event_bar['close']>event_bar['open'],
        event_close_position=(event_bar['close']-event_bar['low'])/event_span,
        event_upper_shadow=(event_bar['high']-max(event_bar['open'],event_bar['close']))/event_span,
        signal_close_above_event_low=entry_bar['close']>=event_bar['low'],
        prior_close_above_ma5=before['close']>=statistics.mean(b['close'] for b in prior[-5:]),
        prior_close_above_ma10=before['close']>=statistics.mean(b['close'] for b in prior),
        prior_day_change_pct=(before['close']/before['previous_close']-1)*100,
        prior_day_volume_multiple=before['volume_shares']/statistics.mean(b['volume_shares'] for b in prior[-6:-1]),
        prior_5d_return_pct=(math.prod(b['close']/b['previous_close'] for b in prior[-5:])-1)*100,
        between_days=len(between),contraction_days=len(consolidation),negative_days=len(negative),low_volume_negative_days=len(low_volume_negative),
        min_close_since_event_pct=min([(b['close']/event_bar['close']-1)*100 for b in between],default=0),
        held_event_low_on_close=all(b['close']>=event_bar['low'] for b in between),
        held_event_body_mid_on_close=all(b['close']>=body_mid for b in between),
        prior_close_above_event_close=before['close']>=event_bar['close'],
        limit_open_gap_pct=(limit_bar['open']/limit_bar['previous_close']-1)*100,
        limit_day_low_pct=(limit_bar['low']/limit_bar['previous_close']-1)*100,
        limit_volume_multiple=limit_bar['volume_shares']/statistics.mean(b['volume_shares'] for b in prior[-5:]),
        limit_breakout_10d=limit_bar['close']>max(b['high'] for b in prior),
        price_adjustment_gaps=[d for d in dates[1:] if event_day<=d<=limit_day and (instrument,d) in bars and (instrument,dates[dates.index(d)-1]) in bars
            and abs(bars[(instrument,d)]['previous_close']-bars[(instrument,dates[dates.index(d)-1])]['close'])>0.011])
    ev=events[(instrument,limit_day)]
    record.update(first_sealed_at=ev['first_sealed_at'],open_count=ev['open_count'],limit_up_type=ev['limit_up_type'],board_count=ev['board_count'],reason=ev['reason'])
    daily.append(record)
    start=max(0,dates.index(event_day)-5)
    daily_plot_data[instrument]={'name':row['name'],'selection':selection_day,'event':event_day,'limit':limit_day,
        'bars':[bars[(instrument,d)] for d in dates[start:idx+1] if (instrument,d) in bars]}
    path=OUT/f'minutes-{instrument}-{limit_day}.json'
    if not path.exists():continue
    s=load(path);points=s['points']
    assert s['trading_date']==limit_day
    assert len(points)==241 and points[0]['minute']=='09:30:00' and points[-1]['minute']=='15:00:00'
    assert abs(points[-1]['price']-limit_bar['close'])<0.011
    assert abs(max(p['high'] for p in points)-limit_bar['high'])<0.011
    assert abs(min(p['low'] for p in points)-limit_bar['low'])<0.011
    daily_volume_ratio=sum(p['volume_shares'] for p in points)/limit_bar['volume_shares']
    first_close=next(i for i,p in enumerate(points) if abs(p['price']-limit_bar['close'])<0.0051)
    pre=points[:first_close]
    vwap_valid=[p for p in pre if p['cumulative_average_price'] is not None]
    last_below=max((i for i,p in enumerate(points) if p['price']<limit_bar['close']-0.0051),default=-1)
    pre_low=min((p['low'] for p in pre),default=limit_bar['open'])
    m=dict(instrument_id=instrument,name=row['name'],trade_date=limit_day,first_sealed_at=ev['first_sealed_at'],
        first_minute_close_at_limit=points[first_close]['minute'],last_below_limit_minute=points[last_below]['minute'] if last_below>=0 else None,
        open_gap_pct=record['limit_open_gap_pct'],pre_seal_min_from_open_pct=(pre_low/limit_bar['open']-1)*100,
        early30_return_pct=(points[30]['price']/limit_bar['previous_close']-1)*100,
        first30_volume_fraction=sum(p['volume_shares'] for p in points[:31])/sum(p['volume_shares'] for p in points),
        pre_seal_points=len(pre),vwap_available_points=len(vwap_valid),
        pre_seal_above_vwap_ratio=sum(p['price']>=p['cumulative_average_price']-0.005 for p in vwap_valid)/len(vwap_valid) if vwap_valid else None,
        minute_daily_volume_ratio=daily_volume_ratio,open_count=ev['open_count'],
        quality=s['metadata']['quality'],quality_flags=s['metadata']['quality_flags'])
    minute_rows.append(m)
    minute_plot_data[instrument]={'name':row['name'],'previous_close':limit_bar['previous_close'],'points':points,'event':ev}

csvout('daily_features.csv',daily);csvout('minute_features.csv',minute_rows)
summary={'sample_size':len(daily),'minute_sample_size':len(minute_rows),
 'bullish_event_body':sum(r['event_bullish_body'] for r in daily),
 'event_close_top20pct':sum(r['event_close_position']>=0.8 for r in daily),
 'event_close_top_half':sum(r['event_close_position']>=0.5 for r in daily),
 'event_upper_shadow_below20pct':sum(r['event_upper_shadow']<=0.2 for r in daily),
 'event_multiple_median':median([r['event_multiple'] for r in daily]),
 'event_multiple_2to3':sum(2<=r['event_multiple']<3 for r in daily),
 'event_multiple_3to5':sum(3<=r['event_multiple']<5 for r in daily),
 'event_multiple_ge5':sum(r['event_multiple']>=5 for r in daily),
 'prior_close_above_ma5':sum(r['prior_close_above_ma5'] for r in daily),
 'prior_close_above_ma10':sum(r['prior_close_above_ma10'] for r in daily),
 'prior_close_above_event_close':sum(r['prior_close_above_event_close'] for r in daily),
 'prior_day_up':sum(r['prior_day_change_pct']>0 for r in daily),
 'prior_day_volume_multiple_median':median([r['prior_day_volume_multiple'] for r in daily]),
 'prior_day_volume_ge2':sum(r['prior_day_volume_multiple']>=2 for r in daily),
 'prior_5d_return_median':median([r['prior_5d_return_pct'] for r in daily]),
 'between_observable':sum(r['between_days']>0 for r in daily),
 'has_contraction':sum(r['between_days']>0 and r['contraction_days']>0 for r in daily),
 'has_negative_day':sum(r['negative_days']>0 for r in daily),
 'has_low_volume_negative_day':sum(r['low_volume_negative_days']>0 for r in daily),
 'holds_event_low':sum(r['held_event_low_on_close'] for r in daily if r['between_days']>0),
 'holds_event_body_mid':sum(r['held_event_body_mid_on_close'] for r in daily if r['between_days']>0),
 'pullback_median_pct':median([r['min_close_since_event_pct'] for r in daily if r['between_days']>0]),
 'limit_volume_multiple_median':median([r['limit_volume_multiple'] for r in daily]),
 'limit_volume_ge2':sum(r['limit_volume_multiple']>=2 for r in daily),
 'limit_breakout_10d':sum(r['limit_breakout_10d'] for r in daily),
 'open_gap_median_pct':median([r['limit_open_gap_pct'] for r in daily]),
 'open_gap_distribution':dict(Counter('低开' if r['limit_open_gap_pct']<-.1 else '平开±0.1%' if r['limit_open_gap_pct']<=.1 else '高开0.1%-3%' if r['limit_open_gap_pct']<3 else '高开3%-7%' if r['limit_open_gap_pct']<7 else '高开>=7%' for r in daily)),
 'first_seal_buckets':dict(Counter('10:00前' if r['first_sealed_at']<'10:00:00' else '10:00-11:30' if r['first_sealed_at']<='11:30:00' else '午后' for r in daily)),
 'zero_open_count':sum(r['open_count']==0 for r in daily),
 'limit_types':dict(Counter(r['limit_up_type'] for r in daily)),
 'minute_preseal_dip_gt1pct':sum(r['pre_seal_min_from_open_pct']<=-1 for r in minute_rows),
 'minute_preseal_dip_median':median([r['pre_seal_min_from_open_pct'] for r in minute_rows]),
 'minute_first30_volume_median':median([r['first30_volume_fraction'] for r in minute_rows]),
 'minute_vwap_complete_preseal_count':sum(r['pre_seal_points']>0 and r['vwap_available_points']==r['pre_seal_points'] for r in minute_rows),
 'minute_vwap_median_for_complete_preseal':median([r['pre_seal_above_vwap_ratio'] for r in minute_rows if r['pre_seal_points']>0 and r['vwap_available_points']==r['pre_seal_points']]),
 'price_adjustment_gaps':[r['instrument_id'] for r in daily if r['price_adjustment_gaps']],
 'minute_daily_volume_ratio_range':[min(r['minute_daily_volume_ratio'] for r in minute_rows),max(r['minute_daily_volume_ratio'] for r in minute_rows)]}
dump('trend-summary.json',summary);dump('daily-plot-data.json',daily_plot_data);dump('minute-plot-data.json',minute_plot_data)
print(json.dumps(summary,ensure_ascii=False,indent=2))
print('EXAMPLES')
for i in ['002442.SZ','002913.SZ','600488.SH','600744.SH','603276.SH','002617.SZ','600616.SH','000670.SZ']:
    print(json.dumps(next(r for r in daily if r['instrument_id']==i),ensure_ascii=False))
