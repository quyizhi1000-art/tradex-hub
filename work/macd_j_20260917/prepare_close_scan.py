from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
from tradex.data_gateway.intraday_technical_seed import fetch_intraday_seed_day
from tradex.stock_selection.intraday_macd_j import prior_sessions, _root, _write

target = date(2026, 9, 17)
days = prior_sessions(target)
for index, day in enumerate(days):
    st_date = target if day == days[-1] else None
    path = _root() / f'seed-{day}-st-{st_date}.json'
    if path.exists():
        print(f'{index+1}/9 {day} cached', flush=True)
        continue
    result = fetch_intraday_seed_day(day, st_date=st_date, now=datetime.now(ZoneInfo('Asia/Shanghai')))
    _write(path, result.model_dump(mode='json'))
    print(f'{index+1}/9 {day} valid={len(result.points)}', flush=True)
