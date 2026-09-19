"""Explain the frozen 229 displayed additions without changing runtime state."""
import json
from collections import Counter
from pathlib import Path

from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1
from tradex.stock_selection.macd_j import screen_macd_j

out = Path(__file__).parent
snapshot = DailyStockFactorSnapshotV1.model_validate_json((out/'baseline-input.json').read_text(encoding='utf-8'))
scan = json.loads((out/'forced-display.json').read_text(encoding='utf-8'))
maps = [{p.instrument_id:p for p in day.points} for day in snapshot.technicals.days]
factors = {r.instrument_id:r for r in snapshot.factors}
rows = []
for candidate in scan['scan_candidates']:
    instrument = candidate['instrument_id']
    previous = maps[-1][instrument]
    before = maps[-2][instrument]
    single = snapshot.model_copy(update={'factors':(factors[instrument],)})
    previous_reason = screen_macd_j(single).excluded_counts
    row = {
        'instrument_id':instrument, 'name':candidate['name'],
        'category':'fresh_macd_cross_today' if previous.dif <= previous.dea else 'already_bullish_yesterday',
        'previous_exclusions':previous_reason,
        'j_turn_date':candidate['j_turn_date'],
        'previous_dif':previous.dif, 'previous_dea':previous.dea,
        'current_dif':candidate['dif'], 'current_dea':candidate['dea'],
        'j_two_days_ago':before.j, 'j_yesterday':previous.j, 'j_now':candidate['j'],
        'previous_close':previous.close, 'price':candidate['price'],
        'change_pct':(candidate['price']/previous.close-1)*100,
    }
    rows.append(row)
counts = Counter(r['category'] for r in rows)
breakdown = Counter((r['category'],reason) for r in rows for reason in r['previous_exclusions'])
summary = {'scan_generated_at':scan['generated_at'],'new_count':len(rows),
    'categories':dict(counts),
    'previous_exclusion_breakdown':[{'category':a,'reason':b,'count':n} for (a,b),n in breakdown.items()],
    'j_turned_today':sum(r['j_turn_date']==scan['trade_date'] for r in rows),
    'flat_or_down_price_but_included':sum(r['change_pct']<=0 for r in rows)}
(out/'new-members-audit.json').write_text(json.dumps({'summary':summary,'rows':rows},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps([r for r in rows if r['category']=='already_bullish_yesterday'][:3],ensure_ascii=False,indent=2))
