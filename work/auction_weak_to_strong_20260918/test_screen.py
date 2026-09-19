import copy
import json
from pathlib import Path

import pytest
from screen import classify, evaluate, read, ROOT, RULES
from research_gateway import dated_rows, TARGET


def example():
    return (
        dict(instrument_id='600000.SH',name='示例',industry='示例'),
        dict(open=10.3, high=10.4, low=9.9, close=10,previous_close=10.2,
             volume_shares=10000000,amount_cny=100000000),
        dict(price=10.2, previous_close=10, amount_cny=6000000, turnover_pct=.1),
        dict(amount_cny=3000000),
    )


def test_weak_reclaim_and_sibling_failed_reclaim():
    args=example()
    assert evaluate(*args,None,None)['passed']
    lower=copy.deepcopy(args)
    lower[2]['price']=10.1
    assert '未收复昨日回落的一半' in evaluate(*lower,None,None)['failed']


def test_strong_yesterday_is_not_weak_and_weak_board_is_separate():
    args=example()
    strong=dict(args[1],close=10.4,previous_close=10)
    assert classify(strong,None,None)[0] is None
    assert classify(strong,dict(open_count=1,last_seal_time='100000'),None)[0] is None
    assert classify(strong,dict(open_count=2,last_seal_time='100000'),None)[0]=='A'
    assert classify(strong,dict(open_count=0,last_seal_time='143000'),None)[0]=='A'


def test_amount_filter_and_missing_comparison_fail_closed():
    args=list(example())
    args[2]=dict(args[2],amount_cny=4999999)
    assert not evaluate(*args,None,None)['passed']
    args=list(example());args[3]=dict(amount_cny=0)
    assert not evaluate(*args,None,None)['passed']


def test_adjustment_mismatch_is_unresolved():
    args=list(example());args[2]=dict(args[2],previous_close=9.8)
    with pytest.raises(ValueError,match='prior close mismatch'):
        evaluate(*args,None,None)


def test_wrong_dates_and_duplicates_are_rejected():
    with pytest.raises(ValueError,match='wrong input date'):
        dated_rows([dict(ts_code='600000.SH',trade_date='20260917')],TARGET)
    with pytest.raises(ValueError,match='duplicate'):
        dated_rows([dict(ts_code='600000.SH',trade_date='20260918')]*2,TARGET)


def test_frozen_real_inputs_and_independent_arithmetic():
    r=json.loads((ROOT/'result.json').read_text(encoding='utf-8'))
    assert r['rules']==RULES
    st=set(read('st')['instrument_ids'])
    master={x['instrument_id']:x for x in read('master')['rows']}
    daily={x['instrument_id']:x for x in read('prior_session')['daily']}
    today={x['instrument_id']:x for x in read('auction_today')['rows']}
    yesterday={x['instrument_id']:x for x in read('auction_prior')['rows']}
    assert read('auction_today')['trade_date']=='2026-09-18'
    assert read('prior_session')['trade_date']=='2026-09-17'
    assert len({x['instrument_id'] for x in r['selected']})==len(r['selected'])
    for x in r['selected']+r['strong_opens']:
        c=x['instrument_id']; a=today[c]; b=daily[c]; p=yesterday[c]
        assert c not in st and master[c]['market']=='主板'
        assert 'ST' not in master[c]['name'].upper()
        assert a['price']>=a['previous_close']*1.01-1e-8
        assert a['amount_cny']>=5000000
        assert a['amount_cny']>=p['amount_cny']*1.5-1e-8
        assert abs(a['previous_close']-b['close'])<=.011
        if x in r['selected']:
            assert a['price']<=a['previous_close']*1.07+1e-8
        else:
            assert a['price']>a['previous_close']*1.07
    import hashlib
    for filename,digest in r['input_sha256'].items():
        assert hashlib.sha256((ROOT/filename).read_bytes()).hexdigest()==digest
