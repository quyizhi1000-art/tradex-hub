"""Read-only, as-of September 16 shortlist preparation from the exact v2 archive."""
import csv,json,math,statistics,urllib.request
from collections import defaultdict
from pathlib import Path
from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1
from tradex.stock_selection.strategies import snapshot_revision
from tradex.stock_selection.volume_surge import screen_volume_surge

OUT=Path(__file__).parent;BASE=OUT.parent
def dump(n,v):(OUT/n).write_text(json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
api=json.load(urllib.request.urlopen('http://127.0.0.1:8765/api/stock-selection/results?trade_date=2026-09-16',timeout=20))
archive=next(r for r in api['results'] if r['strategy_id']=='upward-volume-surge-main-board' and r['strategy_version']=='v2')
snapshot=DailyStockFactorSnapshotV1.model_validate_json((BASE/'snapshot-2026-09-16.json').read_text(encoding='utf-8'))
assert snapshot_revision(snapshot)==archive['source_snapshot_revision']
screen=screen_volume_surge(snapshot)
assert screen.model_dump(mode='json')['candidates']==archive['payload']['candidates']
dump('archive-api.json',api)
histories={h.instrument_id:list(h.bars) for h in snapshot.candlestick_histories}
factors={f.instrument_id:f for f in snapshot.factors}
dates=list(snapshot.candlestick_window_trade_dates)
peers=defaultdict(list)
for f in snapshot.factors:
    bs=histories.get(f.instrument_id,[])
    if bs and bs[-1].trade_date==snapshot.trade_date:
        peers[f.industry].append(bs[-1].close/bs[-1].previous_close-1)

def clip(v):return max(0,min(1,v))
rows=[]
for c in screen.candidates:
    f=factors[c.instrument_id];bs=histories[c.instrument_id];b=bs[-1]
    prior=bs[-6:-1];last=c.evidence[-1];anchor=next(x for x in bs if x.trade_date==c.anchor_trade_date)
    after=[x for x in bs if x.trade_date>c.anchor_trade_date]
    recent_age=dates.index(snapshot.trade_date)-dates.index(last.trade_date)
    vol=b.volume_shares/statistics.mean(x.volume_shares for x in prior)
    cp=(b.close-b.low)/(b.high-b.low)
    ret=(b.close/b.previous_close-1)*100
    high=max(x.high for x in bs[:-1]);dist=(b.close/high-1)*100
    five=(math.prod(x.close/x.previous_close for x in bs[-5:])-1)*100
    industry=f.industry;sector=peers[industry]
    breadth=sum(x>0 for x in sector)/len(sector) if sector else 0
    cap=f.float_market_cap_cny/1e8 if f.float_market_cap_cny else None
    turnover=f.turnover_rate_pct
    # Fixed descriptive priorities; not optimized on previous winners and not a probability.
    components={
      'close_strength':15*cp,
      'breakout_proximity':15*clip((dist+8)/10),
      'recent_signal':10*max(0,1-recent_age/7),
      'price_momentum':10*clip((ret+1)/6),
      'active_volume':10*clip((vol-.5)/2),
      'industry_breadth':10*breadth,
      'turnover':10*(clip((turnover or 0)/3) if (turnover or 0)<=10 else clip((25-(turnover or 0))/15)),
      'float_cap':10*(clip((300-cap)/250) if cap else 0),
      'liquidity':5*clip(f.amount_cny/2e8),
      'controlled_5d_move':5*(1 if -3<=five<=15 else .5 if five<=25 else 0),
    }
    row=dict(instrument_id=c.instrument_id,name=c.name,industry=api['industry_display']['names_by_instrument'].get(c.instrument_id,industry),
        snapshot_industry=industry,close=b.close,change_pct=ret,close_position=cp,
        volume_multiple=vol,turnover_pct=turnover,float_cap_100m=cap,amount_100m=f.amount_cny/1e8,
        breakout_distance_pct=dist,prior14_high=high,day_high=b.high,day_low=b.low,
        five_day_return_pct=five,ma5=statistics.mean(x.close for x in bs[-5:]),
        last_event=str(last.trade_date),last_event_age=recent_age,last_event_multiple=last.volume_multiple,
        anchor_date=str(c.anchor_trade_date),anchor_low=c.anchor_low,
        anchor_margin_pct=(b.close/c.anchor_low-1)*100,events=len(c.evidence),reset_count=c.reset_count,
        min_post_anchor_close=c.minimum_subsequent_close,
        contraction_days=sum(x.volume_shares<anchor.volume_shares for x in after),
        current_vs_anchor_close_pct=(b.close/anchor.close-1)*100,
        sector_positive_ratio=breadth,sector_peer_count=len(sector),
        screen_rank=len(rows)+1,research_score=sum(components.values()),score_components=components)
    rows.append(row)
rows.sort(key=lambda r:(-r['research_score'],r['instrument_id']))
dump('all166-evidence.json',rows)
with (OUT/'all166-evidence.csv').open('w',encoding='utf-8-sig',newline='') as out:
    w=csv.DictWriter(out,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
dump('verification.json',{'trade_date':str(snapshot.trade_date),'result_id':archive['result_id'],
    'source_snapshot_revision':archive['source_snapshot_revision'],'archive_quality':archive['quality'],
    'snapshot_metadata':snapshot.metadata.model_dump(mode='json'),'reproduced_candidate_count':len(rows),
    'archive_and_recomputed_candidates_equal':True,'unavailable_existing_limit_tendency_candidates':len(next(r for r in api['results'] if r['strategy_id']=='next-session-limit-up-tendency-main-board')['payload']['candidates'])})
for n,r in enumerate(rows[:55],1):
    print(f"{n:2} {r['instrument_id']} {r['name']:6} {r['industry']} 分{r['research_score']:.1f} 涨{r['change_pct']:.2f}% 收位{r['close_position']:.2f} 量{r['volume_multiple']:.2f} 换{r['turnover_pct']} 额{r['amount_100m']:.2f} 流{r['float_cap_100m']:.1f} 距前高{r['breakout_distance_pct']:.2f}% 5日{r['five_day_return_pct']:.1f}% 锚{r['anchor_date']} 新{r['last_event_age']}次{r['events']}")
