"""Deterministic 09:25 screen; no same-session continuous-trading inputs."""
import csv
import hashlib
import json
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RULES = {
    'version': 'auction-weak-to-strong-mainboard.research.v1',
    'trade_date': '2026-09-18', 'information_cutoff': '2026-09-18T09:25:00+08:00',
    'prior_date': '2026-09-17', 'min_gap_pct': 1, 'max_gap_pct': 7,
    'min_auction_amount_cny': 5000000, 'min_auction_amount_multiple': 1.5,
    'weak_board_min_open_count': 2, 'weak_board_late_seal': '14:30:00',
    'weak_decline_pct': -1, 'weak_pullback_pct': 3,
    'weak_close_range_max': .5, 'reclaim_fraction': .5,
    'latest_listing_date': '20260911',
    'ranking': 'auction amount multiple descending; amount descending; code ascending',
    'notes': ['Exploratory fixed thresholds, not backtested.',
              'Exclude first five listing sessions and ST as of target date.',
              'Limit-up / extreme high opens above 7 percent are kept in exclusions.',
              'Matched auction amount is not net inflow; provider volume_ratio is display only.',
              'After-the-fact replay; historical revisions and actual API publication latency are unverified.'],
}


def read(name):
    return json.loads((ROOT / (name + '.json')).read_text(encoding='utf-8'))['data']


def time_text(raw):
    if raw in (None, ''):
        return None
    s = str(raw).strip()
    if ':' in s:
        return s[-8:]
    s = s.zfill(6)
    if len(s) != 6 or not s.isdigit():
        raise ValueError('invalid seal time')
    return f'{s[:2]}:{s[2:4]}:{s[4:]}'


def classify(bar, limit, broken):
    close = bar['close']
    pct = (close / bar['previous_close'] - 1) * 100
    pullback = (bar['high'] / close - 1) * 100
    span = bar['high'] - bar['low']
    location = (close - bar['low']) / span if span else 1
    if broken:
        return 'A', '昨日炸板未封住'
    if limit:
        count, seal = limit['open_count'], time_text(limit['last_seal_time'])
        reasons = []
        if count is not None and count >= RULES['weak_board_min_open_count']:
            reasons.append(f'昨日开板{count}次')
        if seal is not None and seal >= RULES['weak_board_late_seal']:
            reasons.append('昨日最后封板' + seal)
        return ('A', '；'.join(reasons)) if reasons else (None, '昨日封板但不符合弱板定义')
    reasons = []
    if pct <= RULES['weak_decline_pct'] + 1e-8:
        reasons.append('昨日下跌至少1%')
    if pullback >= RULES['weak_pullback_pct'] - 1e-8 and location <= RULES['weak_close_range_max'] + 1e-8:
        reasons.append('昨日冲高回落且收于日内下半区')
    return ('B', '；'.join(reasons)) if reasons else (None, '昨日不符合弱势定义')


def evaluate(master, bar, auction, prior_auction, limit, broken):
    group, reason = classify(bar, limit, broken)
    if group is None:
        return None
    if min(bar['close'], bar['previous_close'], bar['volume_shares'], auction['previous_close'], auction['price']) <= 0:
        raise ValueError('inactive or invalid prices')
    if abs(auction['previous_close'] - bar['close']) > .011:
        raise ValueError('prior close mismatch, possible corporate action')
    gap = (auction['price'] / auction['previous_close'] - 1) * 100
    multiple = auction['amount_cny'] / prior_auction['amount_cny'] if prior_auction['amount_cny'] > 0 else None
    reclaim = (auction['price'] - bar['close']) / (bar['high'] - bar['close']) if bar['high'] > bar['close'] else None
    failed = []
    if gap < RULES['min_gap_pct'] - 1e-8:
        failed.append('竞价高开不足1%')
    if gap > RULES['max_gap_pct'] + 1e-8:
        failed.append('竞价高开超过7%（含竞价涨停）')
    if auction['amount_cny'] < RULES['min_auction_amount_cny']:
        failed.append('竞价额不足500万')
    if multiple is None or multiple < RULES['min_auction_amount_multiple'] - 1e-8:
        failed.append('竞价额未达到昨日1.5倍')
    if not limit and (reclaim is None or reclaim < RULES['reclaim_fraction'] - 1e-8):
        failed.append('未收复昨日回落的一半')
    return dict(
        instrument_id=master['instrument_id'], name=master['name'], group=group,
        reason=reason, industry=master['industry'],
        prior_change_pct=(bar['close']/bar['previous_close']-1)*100,
        prior_open=bar['open'], prior_high=bar['high'], prior_close=bar['close'],
        prior_open_count=limit['open_count'] if limit else None,
        prior_last_seal_time=time_text(limit['last_seal_time']) if limit else None,
        auction_price=auction['price'], gap_pct=gap,
        auction_amount_cny=auction['amount_cny'], prior_auction_amount_cny=prior_auction['amount_cny'],
        auction_amount_multiple=multiple, auction_turnover_pct=auction['turnover_pct'],
        auction_share_of_prior_daily_amount_pct=auction['amount_cny']/bar['amount_cny']*100,
        reclaim_fraction=reclaim, passed=not failed, failed=failed,
    )


def run():
    assert read('auction_today')['trade_date'] == RULES['trade_date']
    assert read('auction_prior')['trade_date'] == RULES['prior_date']
    assert read('prior_session')['trade_date'] == RULES['prior_date']
    assert read('st')['trade_date'] == RULES['trade_date']
    assert read('master')['trade_date'] == RULES['trade_date']
    assert json.loads((ROOT/'rules.json').read_text(encoding='utf-8')) == RULES
    master = read('master')['rows']
    st = set(read('st')['instrument_ids'])
    prior = read('prior_session')
    daily = {r['instrument_id']:r for r in prior['daily']}
    limits = {r['instrument_id']:r for r in prior['limit_up']}
    broken = {r['instrument_id']:r for r in prior['broken']}
    auctions = {r['instrument_id']:r for r in read('auction_today')['rows']}
    prior_auctions = {r['instrument_id']:r for r in read('auction_prior')['rows']}
    rows, universe_exclusions, eligible = [], [], []
    for m in master:
        code = m['instrument_id']
        why = None
        if m['market'] != '主板' or not (code.startswith(('600','601','603','605')) and code.endswith('.SH') or code.startswith(('000','001','002','003')) and code.endswith('.SZ')):
            why = '非沪深主板A股'
        elif m['listing_status'] != 'L' or not m['list_date'] or m['list_date'] > '20260918' or (m['delist_date'] and m['delist_date'] <= '20260918'):
            why = '非目标日已上市状态'
        elif code in st or 'ST' in m['name'].upper() or '退' in m['name']:
            why = '当日ST名单或名称风险标记'
        elif m['list_date'] > RULES['latest_listing_date']:
            why = '上市首五个交易日'
        else:
            eligible.append(code)
            if code not in daily:
                why = '缺昨日交易日线（停牌或数据缺失）'
            elif code not in auctions:
                why = '缺当日竞价（停牌或数据缺失）'
            elif code not in prior_auctions:
                why = '缺昨日竞价（停牌或数据缺失）'
            elif daily[code]['amount_cny'] <= 0 or auctions[code]['amount_cny'] <= 0:
                why = '无有效成交额'
        if why:
            universe_exclusions.append(dict(instrument_id=code,name=m['name'],reason=why))
            continue
        try:
            r = evaluate(m,daily[code],auctions[code],prior_auctions[code],limits.get(code),broken.get(code))
            if r:
                rows.append(r)
        except ValueError as e:
            universe_exclusions.append(dict(instrument_id=code,name=m['name'],reason=str(e)))
    rows.sort(key=lambda r:(-(r['auction_amount_multiple'] or 0),-r['auction_amount_cny'],r['instrument_id']))
    selected = [r for r in rows if r['passed']]
    strong_opens = [r for r in rows if r['failed'] == ['竞价高开超过7%（含竞价涨停）']]
    coverage = dict(master_count=len(master), auction_today_count=len(auctions),
                    auction_prior_count=len(prior_auctions), prior_daily_count=len(daily),
                    st_count=len(st), eligible_mainboard_nonst_count=len(eligible),
                    eligible_with_today_auction=sum(c in auctions for c in eligible),
                    eligible_with_prior_daily=sum(c in daily for c in eligible),
                    weak_group_counts=dict(Counter(r['group'] for r in rows)),
                    selected_group_counts=dict(Counter(r['group'] for r in selected)),
                    strong_open_group_counts=dict(Counter(r['group'] for r in strong_opens)),
                    rejected_daily_row_count=len(prior['rejected_daily_rows']),
                    exclusion_counts=dict(Counter(r['reason'] for r in universe_exclusions)))
    evidence = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.glob('*.json') if p.stem in ('master','st','auction_today','auction_prior','prior_session','rules')}
    result = dict(rules=RULES,coverage=coverage,selected=selected,strong_opens=strong_opens,weak_universe=rows,
                  universe_exclusions=universe_exclusions,input_sha256=evidence)
    (ROOT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    columns = {'group':'组别','instrument_id':'代码','name':'名称','reason':'昨日弱势依据',
               'prior_change_pct':'昨日涨跌幅%','prior_high':'昨日最高价','prior_close':'昨日收盘价',
               'auction_price':'竞价价格','gap_pct':'竞价涨幅%','auction_amount_cny':'竞价成交额元',
               'prior_auction_amount_cny':'昨日竞价成交额元','auction_amount_multiple':'竞价额较昨日倍数',
               'auction_turnover_pct':'竞价换手率%','reclaim_fraction':'昨日回落收复比例','failed':'未通过条件'}
    for filename, items in [('入选名单.csv',selected),('强高开补充名单.csv',strong_opens),('全部弱势股与排除原因.csv',rows)]:
        with (ROOT/filename).open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.writer(f);w.writerow(columns.values())
            for r in items:
                w.writerow([round(r[k],6) if isinstance(r[k],float) else '；'.join(r[k]) if isinstance(r[k],list) else r[k] for k in columns])
    print(json.dumps({'coverage':coverage,'selected_count':len(selected),'strong_open_count':len(strong_opens)},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    run()
