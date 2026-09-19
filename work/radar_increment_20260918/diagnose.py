"""Read-only replay of archived candidates against a frozen quote snapshot."""
import json
from collections import Counter
from datetime import date
from pathlib import Path

from tradex.data_gateway.intraday_technical_seed import IntradayTechnicalSeedDayV1
from tradex.data_gateway.intraday_scan_quotes import IntradayScanQuotesV1
from tradex.stock_selection.intraday_macd_j import prior_sessions, project_daily, evaluate
from tradex.stock_selection.store import read_archived_strategy_result

root = Path.home() / '.tradex' / 'intraday_macd_j'
day = date(2026, 9, 18)
seeds = [IntradayTechnicalSeedDayV1.model_validate_json(
    (root / f'seed-{d}-st-{day if d == date(2026,9,17) else None}.json').read_text(encoding='utf-8'))
    for d in prior_sessions(day)]
maps = [{p.instrument_id: p for p in seed.points} for seed in seeds]
quote_path = Path('work/double_turn_audit_20260918/repair-quotes.json')
quotes = IntradayScanQuotesV1.model_validate_json(quote_path.read_text(encoding='utf-8'))
quote_map = {q.instrument_id: q for q in quotes.quotes}
now = max(q.observed_at for q in quotes.quotes if q.observed_at)
result = read_archived_strategy_result('2026-09-17', 'macd-j-upturn-main-board', strategy_version='v1')
candidates = result.payload.model_dump(mode='json')['candidates']
rows = []
for c in candidates:
    instrument = c['instrument_id']
    q = quote_map.get(instrument)
    row = {'instrument_id': instrument, 'name': c['name']}
    points = [m.get(instrument) for m in maps]
    if not q or any(p is None for p in points):
        row['unverified'] = 'missing_quote_or_seed'
        rows.append(row)
        continue
    previous = points[-1]
    archived = c['evidence'][-1]
    row['archive_seed_deltas'] = {f: getattr(previous,f)-archived[f] for f in ('dif','dea','k','d','j')}
    row['archive_six_day_max_delta'] = max(
        abs(getattr(p, f)-e[f]) for p,e in zip(points[-6:],c['evidence'])
        for f in ('dif','dea','k','d','j'))
    selected = quotes.model_copy(update={'quotes': (q,)})
    _, counts, verified = evaluate(seeds, selected, now=now)
    row['evaluated'] = instrument in verified
    row['exclusions'] = counts['excluded_counts']
    dif, dea, k, d, j = project_daily(points[-8:], q)
    row.update(previous_dif=previous.dif, previous_dea=previous.dea, previous_j=previous.j,
               dif=dif, dea=dea, j=j, price=q.last, observed_at=q.observed_at.isoformat(),
               previous_spread=previous.dif-previous.dea, spread=dif-dea,
               still_macd_above=dif>dea, j_still_rising=j>previous.j)
    js = [p.j for p in points[-5:]] + [j]
    row['has_recent_j_turn'] = any(js[i-2]>js[i-1] and js[i]>js[i-1] for i in range(2,6))
    rows.append(row)
verified = [r for r in rows if r.get('evaluated')]
summary = {
    'previous_archive_id': result.result_id,
    'previous_count': len(candidates),
    'quote_file': str(quote_path),
    'quote_observed_min': min(q.observed_at for q in quotes.quotes if q.observed_at).isoformat(),
    'quote_observed_max': now.isoformat(),
    'previous_stocks_verified': len(verified),
    'exclusions': dict(sum((Counter(r.get('exclusions',{})) for r in rows), Counter())),
    'archive_seed_mismatch_count': sum(any(abs(v)>1e-9 for v in r.get('archive_seed_deltas',{}).values()) for r in rows),
    'archive_six_day_mismatch_count': sum(r.get('archive_six_day_max_delta',0)>1e-9 for r in rows),
    'verified_still_macd_above': sum(r['still_macd_above'] for r in verified),
    'verified_macd_above_and_j_rising': sum(r['still_macd_above'] and r['j_still_rising'] for r in verified),
    'verified_current_state_and_recent_j_turn': sum(r['still_macd_above'] and r['j_still_rising'] and r['has_recent_j_turn'] for r in verified),
}
forced = json.loads(Path('work/paid_radar_20260918/forced-result.json').read_text(encoding='utf-8'))
active = [r for r in forced['records'] if r.get('active')]
summary['forced_scan_active_count'] = len(active)
summary['forced_scan_overlap_previous_archive'] = sorted({r['instrument_id'] for r in active} & {c['instrument_id'] for c in candidates})
summary['forced_scan_all_previous_dif_le_dea'] = all(maps[-1][r['instrument_id']].dif <= maps[-1][r['instrument_id']].dea for r in active)
output = {'summary': summary, 'rows': rows}
Path('work/radar_increment_20260918/diagnosis.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps([r for r in verified if r['still_macd_above'] and r['j_still_rising']][:5],ensure_ascii=False,indent=2))
