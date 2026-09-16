"""Per-selection-date subsequent closing limit-up counts and first-hit lags."""
import csv
import hashlib
import json
import statistics
import urllib.request
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1
from tradex.stock_selection.volume_surge import screen_volume_surge

OUT = Path(__file__).parent
DAYS = [f'2026-09-{d:02d}' for d in (9, 10, 11, 14, 15, 16)]
STRATEGY = 'upward-volume-surge-main-board'

def load(name):
    return json.loads((OUT / name).read_text(encoding='utf-8'))

def dump(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')

def csv_out(name, rows):
    with (OUT / name).open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

snapshots = {d: DailyStockFactorSnapshotV1.model_validate(load(f'snapshot-{d}.json')) for d in DAYS}
members = {d: set(load(f'limit-membership-{d}.json')['instrument_ids']) for d in DAYS}
all_bars = {}
for snapshot in snapshots.values():
    for history in snapshot.candlestick_histories:
        for bar in history.bars:
            key = (history.instrument_id, bar.trade_date.isoformat())
            value = bar.model_dump(mode='json')
            assert key not in all_bars or value == all_bars[key], key
            all_bars[key] = value

# One-price sessions use the general OHLCV contract, which preserves them.
for path in OUT.glob('ohlcv-*.json'):
    evidence = json.loads(path.read_text(encoding='utf-8'))
    assert evidence['adjustment'] == 'none'
    assert evidence['metadata']['quality'] == 'accepted'
    for bar in evidence['bars']:
        assert bar['change'] is not None
        key = (evidence['instrument_id'], bar['trading_date'])
        assert key not in all_bars
        all_bars[key] = dict(close=bar['close'],
            previous_close=round(bar['close']-bar['change'],2))

records, validation, conflicts, missing = [], [], [], []
for day_index, day in enumerate(DAYS):
    api = json.load(urllib.request.urlopen(f'http://127.0.0.1:8765/api/stock-selection/results?trade_date={day}', timeout=20))
    dump(f'api-{day}.json', api)
    archived = next(r for r in api['results'] if r['strategy_id'] == STRATEGY)
    reproduced = screen_volume_surge(snapshots[day])
    actual = archived['payload']['candidates']
    assert [(c['instrument_id'],c['evidence']) for c in actual] == [(c.instrument_id,c.model_dump(mode='json')['evidence']) for c in reproduced.candidates], day
    validation.append(dict(trade_date=day, result_id=archived['result_id'], count=len(actual),
        quality=archived['quality'], snapshot_quality=snapshots[day].metadata.quality.value,
        flags=list(snapshots[day].metadata.quality_flags), artifact_revision=api['artifact_revision'],
        reproduced_candidates_and_evidence=True))
    for candidate in actual:
        instrument = candidate['instrument_id']
        assert instrument not in members[day], (day,instrument,'same-day closing limit conflicts with v1 exclusion')
        future_days = DAYS[day_index+1:]
        hit_days = [d for d in future_days if instrument in members[d]]
        first_hit = hit_days[0] if hit_days else None
        lag = DAYS.index(first_hit)-day_index if first_hit else None
        for future_day in future_days:
            bar = all_bars.get((instrument,future_day))
            if bar is None:
                missing.append((instrument,future_day))
                continue
            limit = float((Decimal(str(bar['previous_close']))*Decimal('1.10')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
            closed = abs(bar['close']-limit)<1e-8
            if closed != (instrument in members[future_day]):
                conflicts.append(dict(instrument_id=instrument,trade_date=future_day,
                    close=bar['close'],limit_price=limit,in_pool=instrument in members[future_day]))
        records.append(dict(signal_date=day,instrument_id=instrument,name=candidate['name'],
            observed_sessions=len(future_days),first_limit_date=first_hit,first_limit_lag=lag,
            all_limit_dates=';'.join(hit_days)))

csv_out('all_candidate_observations.csv', records)
successes = [r for r in records if r['first_limit_lag'] is not None]
csv_out('limit_up_successes.csv', successes)
daily = []
for day in DAYS:
    rows = [r for r in records if r['signal_date']==day]
    hits = [r for r in rows if r['first_limit_lag'] is not None]
    counts = Counter(r['first_limit_lag'] for r in hits)
    daily.append(dict(day=day,candidates=len(rows),observed_sessions=rows[0]['observed_sessions'],
        subsequent_limit_stocks=len(hits),rate_pct=round(len(hits)/len(rows)*100,2) if rows[0]['observed_sessions'] else None,
        first_hit_lags={str(h):counts.get(h,0) if rows[0]['observed_sessions']>=h else None for h in range(1,6)},
        conditional_mean_lag=statistics.mean(r['first_limit_lag'] for r in hits) if hits else None,
        conditional_median_lag=statistics.median(r['first_limit_lag'] for r in hits) if hits else None))
summary = dict(cutoff=DAYS[-1],strategy='upward-volume-surge-main-board.v1',daily=daily,
    unique_count=len({r['instrument_id'] for r in records}),unique_successes=len({r['instrument_id'] for r in successes}),
    price_membership_conflicts=list({(r['instrument_id'],r['trade_date']):r for r in conflicts}.values()),
    missing_pattern_bar_pairs=[dict(instrument_id=i,trade_date=d,in_limit_pool=i in members[d]) for i,d in sorted(set(missing))])
assert not summary['price_membership_conflicts'], summary['price_membership_conflicts']
assert not summary['missing_pattern_bar_pairs'], summary['missing_pattern_bar_pairs']
for row in daily:
    assert sum(n or 0 for n in row['first_hit_lags'].values()) == row['subsequent_limit_stocks']
dump('verification.json',validation)
dump('summary.json',summary)
dump('file-provenance.json',{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.glob('*.json') if p.name!='file-provenance.json'})
print(json.dumps(summary,ensure_ascii=False,indent=2))
