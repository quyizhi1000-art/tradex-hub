import json
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1
from tradex.data_gateway.intraday_technical_seed import IntradayTechnicalSeedDayV1
from tradex.stock_selection.intraday_macd_j import prior_sessions, _root, run_manual_close_scan

target = date(2026, 9, 17)
snapshot = DailyStockFactorSnapshotV1.model_validate_json(
    Path('work/macd_j_20260917/snapshot-2026-09-17.json').read_text(encoding='utf-8'))
days = prior_sessions(target)
seeds = []
for day in days:
    st_date = target if day == days[-1] else None
    seeds.append(IntradayTechnicalSeedDayV1.model_validate_json(
        (_root() / f'seed-{day}-st-{st_date}.json').read_text(encoding='utf-8')))
result = run_manual_close_scan(snapshot, seeds, now=datetime.now(ZoneInfo('Asia/Shanghai')))
Path('work/macd_j_20260917/manual-close-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:result[k] for k in ['trade_date','last_scan_slot','requested_count','eligible_count','evaluated_count','excluded_counts']}))
print('matched',len(result['scan_candidates']))
