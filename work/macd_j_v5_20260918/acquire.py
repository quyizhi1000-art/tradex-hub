import json
from datetime import date
from pathlib import Path
from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1
from tradex.data_gateway.stock_technicals import fetch_stock_technical_window
from tradex.data_gateway.macd_price_history import fetch_macd_price_histories
from tradex.stock_selection.service import _load_factor_snapshot_with_relationships
from tradex.stock_selection.macd_j import price_history_requests,screen_macd_j
out=Path(__file__).parent
for day in (date(2026,9,17),date(2026,9,18)):
 print(json.dumps(dict(stage='loading',day=str(day))),flush=True)
 if day.day==17:
  s=DailyStockFactorSnapshotV1.model_validate_json((out.parent/'macd_j_v4_20260918'/'snapshot-2026-09-17.json').read_text(encoding='utf-8'))
 else:
  s=_load_factor_snapshot_with_relationships(day)
  if s.technicals is None:
   s=s.model_copy(update={'technicals':fetch_stock_technical_window(s.candlestick_window_trade_dates[-6:])})
 ids=price_history_requests(s)
 print(json.dumps(dict(stage='prices',day=str(day),requested=len(ids))),flush=True)
 histories=fetch_macd_price_histories(ids,day)
 s=s.model_copy(update={'technicals':s.technicals.model_copy(update={'price_histories':histories})})
 (out/f'snapshot-{day}.json').write_text(s.model_dump_json(),encoding='utf-8')
 r=screen_macd_j(s)
 (out/f'screen-{day}.json').write_text(r.model_dump_json(indent=2),encoding='utf-8')
 print(json.dumps(dict(stage='screened',day=str(day),histories=len(histories),confirmed=r.matched_count,pending=r.pending_count,excluded=r.excluded_counts,tongfu=[c.model_dump(mode='json') for c in r.candidates if c.instrument_id=='002156.SZ']),ensure_ascii=True),flush=True)
