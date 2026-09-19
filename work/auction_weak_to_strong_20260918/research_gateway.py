"""Bounded research input adapter over Tradex's public, throttled router.

No direct provider transport, production cache, scheduler, or archive writes.
Provider fields terminate here; the screen consumes the normalized evidence.
"""
import json
import math
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tradex.data_sources import get_router, register_all_sources
from tradex.data_gateway.providers.auctions import map_opening_auction_frame
from tradex.data_gateway.providers.stock_selection import _unique_by_code
from tradex.data_gateway.providers.limit_sentiment import map_tushare_limit_sentiment
from tradex.data_gateway.providers.securities import canonical_instrument_id

ROOT = Path(__file__).resolve().parent
TARGET = date(2026, 9, 18)
PRIOR = date(2026, 9, 17)
BEFORE = date(2026, 9, 16)


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('nonfinite input')
    return result


def dated_rows(rows, day):
    if not isinstance(rows, list):
        raise ValueError('missing row list')
    seen = set()
    for row in rows:
        if row['trade_date'].replace('-', '') != day.strftime('%Y%m%d'):
            raise ValueError('wrong input date')
        code = canonical_instrument_id(row['ts_code'])
        if code in seen:
            raise ValueError('duplicate input instrument')
        seen.add(code)
    return rows


def auction_map(frame, provider, day):
    if provider != 'tushare':
        raise ValueError('unverified research provider')
    result, seen = [], set()
    for raw in frame.to_dict(orient='records'):
        code = canonical_instrument_id(raw['代码'])
        if code in seen:
            raise ValueError('duplicate auction instrument')
        seen.add(code)
        mapped = map_opening_auction_frame(frame.loc[frame['代码'] == raw['代码']],
                                         provider=provider, requested_symbol=code)
        if mapped['trading_date'] != day:
            raise ValueError('wrong auction date')
        for key in ('price', 'previous_close', 'volume_shares', 'amount_cny'):
            val = number(mapped[key])
            if val < 0:
                raise ValueError('negative auction input')
        if mapped['volume_shares'] > 0 and abs(
            mapped['price'] * mapped['volume_shares'] - mapped['amount_cny']
        ) > max(1, mapped['amount_cny'] * .005):
            raise ValueError('auction price/volume/amount unit mismatch')
        result.append(mapped)
    if not result:
        raise ValueError('empty auction table')
    return {'trade_date': day, 'rows': result}


def master_map(raw, provider):
    if provider != 'tushare' or raw['trade_date'] != TARGET.isoformat():
        raise ValueError('wrong master source or date')
    rows = _unique_by_code(raw['stock_basic'], name='research master',
                          allow_status_duplicates=True, skip_non_a_share=True)
    return {'trade_date': TARGET, 'rows': [dict(
        instrument_id=code, name=r['name'], market=r.get('market'),
        list_date=r.get('list_date'), delist_date=r.get('delist_date'),
        listing_status=r.get('list_status'), industry=r.get('industry'),
    ) for code, r in sorted(rows.items())]}


def st_map(raw, provider):
    if provider != 'tushare' or raw['trade_date'] != PRIOR.isoformat():
        raise ValueError('wrong ST bundle')
    rows = dated_rows(raw['special_treatment'], TARGET)
    if not rows:
        raise ValueError('empty ST list is not verified complete')
    return {'trade_date': TARGET, 'instrument_ids': sorted(r['ts_code'] for r in rows)}


def sentiment_map(raw, provider):
    if provider != 'tushare':
        raise ValueError('unverified sentiment provider')
    map_tushare_limit_sentiment(raw, requested_date=PRIOR, previous_trade_date=BEFORE)
    daily, rejected = [], []
    for r in dated_rows(raw['daily'], PRIOR):
        row = dict(instrument_id=r['ts_code'], trade_date=PRIOR,
                   **{k: number(r[k]) for k in ('open', 'high', 'low', 'close')},
                   previous_close=number(r['pre_close']),
                   amount_cny=number(r['amount']) * 1000,
                   volume_shares=number(r['vol']) * 100)
        if row['high'] < max(row['open'], row['close']) or row['low'] > min(row['open'], row['close']):
            rejected.append(dict(instrument_id=row['instrument_id'], reason='invalid daily OHLC', values=row))
            continue
        daily.append(row)
    pools = {}
    for key in ('limit_up', 'broken'):
        pools[key] = [dict(
            instrument_id=r['ts_code'], name=r.get('name'),
            open_count=None if r.get('open_num') in (None, '') else int(r['open_num']),
            last_seal_time=r.get('last_lu_time'),
        ) for r in dated_rows(raw[key], PRIOR)]
    return {'trade_date': PRIOR, 'daily': daily, 'rejected_daily_rows': rejected, **pools}


def acquire():
    register_all_sources()
    router = get_router()
    jobs = [
        ('auction_today', 'opening_auction_market', lambda r, p: auction_map(r, p, TARGET), {'trade_date': TARGET.isoformat()}),
        ('auction_prior', 'opening_auction_market', lambda r, p: auction_map(r, p, PRIOR), {'trade_date': PRIOR.isoformat()}),
        ('master', 'stock_selection_master', master_map, {'trade_date': TARGET.isoformat()}),
        ('st', 'stock_selection_technicals', st_map, {'trade_date': PRIOR.isoformat(), 'check_st': True, 'st_trade_date': TARGET.isoformat()}),
        ('prior_session', 'limit_sentiment_daily', sentiment_map, {'trade_date': PRIOR.isoformat(), 'previous_trade_date': BEFORE.isoformat()}),
    ]
    for name, route, mapper, params in jobs:
        path = ROOT / (name + '.json')
        if path.exists():
            print(name, 'evidence already recorded', flush=True)
            continue
        fetched = datetime.now(ZoneInfo('Asia/Shanghai'))
        value, provider = router.route_validated(route, mapper, **params,
                                                deadline_seconds=180, provider_deadline_seconds=180)
        evidence = dict(contract='auction_research_input.v1', provider=provider,
                        route=route, parameters=params, fetched_at=fetched,
                        source_timestamp_available=False, data=value)
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str, allow_nan=False), encoding='utf-8')
        print(name, 'recorded', {k: len(v) for k,v in value.items() if isinstance(v,list)}, flush=True)


if __name__ == '__main__':
    acquire()
