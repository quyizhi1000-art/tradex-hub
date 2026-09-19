"""Recompute yesterday from retained canonical evidence, without provider calls."""
import json
from pathlib import Path
from tradex.data_gateway.intraday_technical_seed import IntradayTechnicalSeedDayV1
from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1
from tradex.data_gateway.stock_technicals_contracts import StockTechnicalPointV1, StockTechnicalDayV1, StockTechnicalWindowV1
from tradex.stock_selection.macd_j import screen_macd_j
from tradex.stock_selection.store import read_archived_strategy_result

OUT = Path(__file__).parent
root = Path.home() / '.tradex' / 'intraday_macd_j'
snapshot = DailyStockFactorSnapshotV1.model_validate_json(
    Path('work/macd_j_20260917/snapshot-2026-09-17.json').read_text(encoding='utf-8'))
st = IntradayTechnicalSeedDayV1.model_validate_json((root/'seed-2026-09-16-st-2026-09-17.json').read_text(encoding='utf-8'))
assert st.st_trade_date == snapshot.trade_date
days = []
for day in snapshot.candlestick_window_trade_dates[-6:]:
    seed = IntradayTechnicalSeedDayV1.model_validate_json(
        (root/f'seed-{day}-st-{"2026-09-18" if day == snapshot.trade_date else None}.json').read_text(encoding='utf-8'))
    days.append(StockTechnicalDayV1(
        trade_date=day, metadata=seed.metadata.model_copy(update={'contract': 'stock_technical_day.v1'}),
        points=tuple(StockTechnicalPointV1.model_validate(p.model_dump(include=set(StockTechnicalPointV1.model_fields))) for p in seed.points),
        special_treatment_ids=st.special_treatment_ids if day==snapshot.trade_date else None))
snapshot = DailyStockFactorSnapshotV1.model_validate({**snapshot.model_dump(), 'technicals': StockTechnicalWindowV1(days=tuple(days))})
old = read_archived_strategy_result(snapshot.trade_date, 'macd-j-upturn-main-board', strategy_version='v1')
replay = screen_macd_j(snapshot, version='v1')
assert {c.instrument_id for c in replay.candidates} == {c.instrument_id for c in old.payload.candidates}
assert all(c.evidence == next(r.evidence for r in old.payload.candidates if r.instrument_id==c.instrument_id) for c in replay.candidates)
current = screen_macd_j(snapshot)
(OUT/'baseline-input.json').write_text(snapshot.model_dump_json(),encoding='utf-8')
(OUT/'baseline-preview.json').write_text(current.model_dump_json(indent=2),encoding='utf-8')
print(json.dumps({'trade_date': str(snapshot.trade_date),'v1_replay_matches_archive': True,
                  'v1_count': replay.matched_count, 'v2_count': current.matched_count,
                  'eligible': current.board_eligible_count, 'evaluated': current.evaluated_count,
                  'excluded': current.excluded_counts},ensure_ascii=False))
