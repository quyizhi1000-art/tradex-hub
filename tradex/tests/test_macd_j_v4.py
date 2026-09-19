from datetime import datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from test_macd_j_selection import source, with_macd_spreads
from tradex.data_gateway.contracts import ContractMetadata, OHLCVBarV1, OHLCVSeriesV1
from tradex.data_gateway.macd_price_history import sixty_sessions, fetch_macd_price_histories
from tradex.stock_selection.macd_rules import v4_signal, price_filter_evidence
from tradex.stock_selection.macd_j import screen_macd_j as current_screen

def screen_macd_j(snapshot):
    return current_screen(snapshot, version="v4")
from tradex.stock_selection.contracts import MacdJScreenV1


def enriched(data=None, *, high=20, ratio=1.5, price=None):
    data = data or source()
    price = price or data.factors[0].close
    dates = sixty_sessions(data.trade_date)
    bars = tuple(OHLCVBarV1(trading_date=d, open=price, close=price,
        high=max(high if i == 0 else price+1, price), low=price-1,
        volume_shares=1000, amount_cny=10000) for i, d in enumerate(dates))
    history = OHLCVSeriesV1(metadata=ContractMetadata(contract="ohlcv_bar.v1", provider="fixture",
        fetched_at=datetime(2026, 9, 18, 18, tzinfo=ZoneInfo("Asia/Shanghai")), quality="accepted"),
        instrument_id=data.factors[0].instrument_id, period="daily", adjustment="forward", bars=bars)
    data = data.model_copy(update={"factors": (data.factors[0].model_copy(update={"volume_ratio": ratio}),),
        "technicals": data.technicals.model_copy(update={"price_histories": (history,)})})
    return data


@pytest.mark.parametrize("age,expected", [(0, 1), (1, 0), (2, 0), (3, 0)])
def test_v4_requires_today_cross(age, expected):
    data = enriched(with_macd_spreads([-.1 if i < 5-age else .1 for i in range(6)]), high=100)
    result = screen_macd_j(data)
    assert result.matched_count == expected
    assert result.pending_count == 0
    assert result.screen_version.endswith(".v4")


@pytest.mark.parametrize("js,expected", [
    ((50,40,30,25,10,18), 1), ((50,40,30,10,18,20), 1),
    ((50,40,10,18,20,22), 0), ((50,40,30,10,18,17), 0),
    ((50,40,30,10,10,18), 1)])
def test_j_one_day_window_flat_turn_and_current_rising(js, expected):
    result = screen_macd_j(enriched(source(js), high=100))
    assert result.matched_count == expected


@pytest.mark.parametrize("offset", [-1, 1])
def test_both_zero_axis_zones_are_eligible_and_ma5_equality(offset):
    result = screen_macd_j(enriched(source(offset=offset), high=100))
    assert result.matched_count == 1
    c = result.candidates[0]
    assert c.reference_close == c.ma5
    assert c.volume_ratio == 1.5
    assert MacdJScreenV1.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("spreads,expected", [
    ([-.8,-.7,-.6,-.5,-.3,-.1],1), ([-.8,-.7,-.6,-.5,-.3,0],1),
    ([-.8,-.7,-.6,-.2,-.3,-.1],0), ([-.8,-.7,-.6,-.3,-.3,-.1],0),
    ([-.8,-.7,-.6,.2,-.3,-.1],0), ([-.8,-.7,-.6,-.5,-.3,.1],0)])
def test_pending_requires_two_days_convergence_and_is_not_counted_as_selected(spreads, expected):
    result = screen_macd_j(enriched(with_macd_spreads(spreads), high=100))
    assert result.pending_count == expected
    if expected:
        assert result.matched_count == 0
        assert result.pending_candidates[0].macd_cross_date is None
        assert MacdJScreenV1.model_validate_json(result.model_dump_json()) == result


def test_pending_requires_dif_itself_rising():
    points = [NS(dif=1,dea=1.5,j=20),NS(dif=.9,dea=1.2,j=15),NS(dif=.8,dea=.9,j=10),NS(dif=.7,dea=.75,j=18)]
    assert v4_signal(points) == (None,None)


@pytest.mark.parametrize("ratio", [None, 1.50001])
def test_missing_or_excess_volume_ratio_excluded(ratio):
    assert screen_macd_j(enriched(high=100, ratio=ratio)).matched_count == 0


def test_user_example_and_strict_drawdown_boundary():
    data = enriched(high=84.7, price=58.31)
    history = data.technicals.price_histories[0]
    dates = sixty_sessions(data.trade_date)
    values, reason = price_filter_evidence(history, dates=dates, price=58.31, volume_ratio=1)
    assert reason is None
    assert values['drawdown_60_pct'] == pytest.approx(-31.15702479338843)
    for price, matched in ((80,False),(79.999,True)):
        history = enriched(high=100, price=price).technicals.price_histories[0]
        values, reason = price_filter_evidence(history, dates=dates, price=price, volume_ratio=1.5)
        assert (values is not None) == matched


def test_incomplete_history_and_bad_basis_fail_closed():
    data = enriched(high=100)
    hist = data.technicals.price_histories[0]
    for changed in (hist.model_copy(update={"bars":hist.bars[1:]}), hist.model_copy(update={"adjustment":"none"})):
        bad = data.model_copy(update={"technicals":data.technicals.model_copy(update={"price_histories":(changed,)})})
        assert screen_macd_j(bad).matched_count == 0
    assert screen_macd_j(source()).excluded_counts == {"price_history_unavailable":1}


def test_candidate_price_evidence_tampering_rejected():
    raw = screen_macd_j(enriched(high=100)).model_dump(mode="json")
    for key, val in (("ma5",1000),("volume_ratio",2),("drawdown_60_pct",-19),("macd_cross_age_sessions",1)):
        import copy
        bad = copy.deepcopy(raw); bad['candidates'][0][key]=val
        with pytest.raises(ValueError): MacdJScreenV1.model_validate(bad)


def test_gateway_reuses_exact_cache_and_rejects_missing_date(tmp_path):
    data = enriched(high=100); hist = data.technicals.price_histories[0]
    calls=[]
    def loader(*args,**kwargs): calls.append(args); return hist
    for _ in range(2):
        assert fetch_macd_price_histories([hist.instrument_id],data.trade_date,root=tmp_path,loader=loader)==(hist,)
    assert len(calls)==1
    assert fetch_macd_price_histories([hist.instrument_id], data.trade_date, root=tmp_path/'missing',
        loader=lambda *a,**k: hist.model_copy(update={"bars":hist.bars[1:]})) == ()


def test_intraday_same_filters_and_volume_minutes_exclude_lunch():
    from test_intraday_macd_j import fixture, price_histories, NOW
    from tradex.stock_selection.intraday_macd_j import evaluate, intraday_volume_ratio
    seeds, universe, *_ = fixture()
    histories = price_histories(seeds, NOW.date())
    candidates, _, _ = evaluate(seeds, universe, now=NOW, price_histories=histories)
    assert len(candidates) == 1
    assert candidates[0]['signal_kind'] == 'confirmed'
    assert candidates[0]['macd_cross_age_sessions'] == 0
    q = universe.quotes[0]
    q.volume_ratio = 1.50001
    assert not evaluate(seeds, universe, now=NOW, price_histories=histories)[0]
    q.volume_shares = 50000
    for hour, minute in ((11,30),(12,0),(13,0)):
        q.observed_at = NOW.replace(hour=hour, minute=minute)
        assert intraday_volume_ratio(q,histories[0],NOW) == 1


def test_universe_volume_units_and_invalid_unit_do_not_pass_ratio_input():
    from tradex.data_gateway.providers.market_universe import map_a_share_universe_payload
    row = dict(ts_code="600000.SH", name="样本", close=10, pct_chg=0, amount=100000,
               vol=10000, open=10, high=11, low=9, pre_close=10, volume_ratio=1.5)
    quote = map_a_share_universe_payload([row],provider="tushare")[0][0]
    assert quote.volume_shares == 10000 and quote.volume_ratio == 1.5
    row['vol']=100
    assert map_a_share_universe_payload([row],provider="tushare")[0][0].volume_shares is None


def test_price_outage_keeps_current_strategy_retryable_without_touching_siblings(tmp_path):
    from contextlib import closing
    from tradex.stock_selection.service import DailyStockSelectionService
    from tradex.stock_selection.store import DailyStockSelectionStore
    data = enriched(high=100)
    original = data.model_copy(update={"technicals":data.technicals.model_copy(update={"price_histories":()})})
    with closing(DailyStockSelectionStore(tmp_path/'selection.db')) as store:
        service = DailyStockSelectionService(store, factor_loader=lambda _:original,
            price_history_loader=lambda *a: ())
        now = datetime(2026,9,18,18,tzinfo=ZoneInfo("Asia/Shanghai"))
        first = service.generate(now=now,trade_date=data.trade_date)
        assert first['missing_strategy_ids'] == ['macd-j-upturn-main-board']
        before = store.list_strategy_results(data.trade_date)
        assert len(before) == 4
        service._price_history_loader = lambda *a: data.technicals.price_histories
        second = service.generate(now=now,trade_date=data.trade_date)
        assert second['missing_strategy_ids'] == []
        assert store.list_strategy_results(data.trade_date)[:4] == before
