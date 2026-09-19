import json
from pathlib import Path
from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1
from tradex.data_gateway.macd_price_history import fetch_macd_price_histories
from tradex.stock_selection.macd_j import price_history_requests, screen_macd_j
p=Path('work/radar_state_v2_20260918/baseline-input.json')
s=DailyStockFactorSnapshotV1.model_validate_json(p.read_text(encoding='utf-8'))
ids=price_history_requests(s)
print(json.dumps({'stage':'acquiring','count':len(ids)}),flush=True)
histories=fetch_macd_price_histories(ids,s.trade_date)
s=s.model_copy(update={'technicals':s.technicals.model_copy(update={'price_histories':histories})})
out=Path('work/macd_j_v4_20260918')
(out/'snapshot-2026-09-17.json').write_text(s.model_dump_json(),encoding='utf-8')
r=screen_macd_j(s)
(out/'screen-2026-09-17.json').write_text(r.model_dump_json(indent=2),encoding='utf-8')
print(json.dumps({'stage':'screened','histories':len(histories),'confirmed':r.matched_count,'pending':r.pending_count,'excluded':r.excluded_counts},ensure_ascii=True),flush=True)
