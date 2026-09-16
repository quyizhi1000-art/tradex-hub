"""Read canonical gateway evidence; write only research artifacts."""
import json
from pathlib import Path
from tradex.data_gateway import fetch_daily_limit_up_membership, fetch_daily_stock_factor_snapshot

OUT = Path(__file__).parent
for day in (9, 10, 11, 14, 15, 16):
    target = f'2026-09-{day:02d}'
    path = OUT / f'limit-membership-{target}.json'
    if not path.exists():
        result = fetch_daily_limit_up_membership(target)
        assert result.trading_date.isoformat() == target
        path.write_text(result.model_dump_json(indent=2), encoding='utf-8')
        print(json.dumps({'membership': target, 'count': len(result.instrument_ids),
                          'quality': result.metadata.quality.value}), flush=True)
for day in (15, 16):
    target = f'2026-09-{day:02d}'
    path = OUT / f'snapshot-{target}.json'
    if not path.exists():
        result = fetch_daily_stock_factor_snapshot(target, apply_relationship_catalog=True)
        path.write_text(result.model_dump_json(), encoding='utf-8')
        print(json.dumps({'snapshot': target, 'count': len(result.factors),
                          'quality': result.metadata.quality.value}), flush=True)
