from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from tradex.stock_selection.intraday_macd_j import (
    evaluate, merge_observations, prior_sessions, project_daily, read_watch, refresh_watch,
    scan_slot, archive_scan, read_scan_history,
    run_manual_close_scan,
)
from tradex.data_gateway.intraday_technical_seed import map_intraday_seed

NOW = datetime(2026, 9, 17, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture(autouse=True)
def isolated_close_archive(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADEX_DAILY_STOCK_SELECTION_DB", str(tmp_path / "selection.sqlite3"))


def fixture():
    closes = [10 + i * .1 for i in range(80)] + [18 - i * i * .015 for i in range(20)]
    e12 = e26 = closes[0]
    dea = 0
    k = d = 50
    points = []
    for i, close in enumerate(closes):
        e12 = e12 * 11/13 + close * 2/13
        e26 = e26 * 25/27 + close * 2/27
        dif = e12 - e26
        dea = dea * .8 + dif * .2
        high = max(closes[max(0, i-8):i+1]) + 1
        low = min(closes[max(0, i-8):i+1]) - 1
        rsv = (close-low)/(high-low)*100
        k = k * 2/3 + rsv/3
        d = d * 2/3 + k/3
        points.append(NS(instrument_id="600000.SH", close=close, adjusted_close=close,
            high=close+1, low=close-1, amount_cny=1e6, adjustment_factor=1,
            dif=dif, dea=dea, k=k, d=d, j=3*k-2*d))
    points = points[-9:]
    seeds = [NS(trade_date=day, points=[point], st_trade_date=NOW.date(), special_treatment_ids=[])
             for day, point in zip(prior_sessions(NOW.date()), points)]
    last = closes[-1] + 15
    q = NS(instrument_id="600000.SH", name="浦发银行", observed_at=NOW,
           last=last, high=last, low=closes[-1], open=closes[-1], previous_close=closes[-1], amount_cny=1e7)
    return seeds, NS(quotes=[q]), e12, e26


def test_projection_matches_full_history_ema_without_short_window_initialization():
    seeds, universe, e12, e26 = fixture()
    q = universe.quotes[0]
    dif, dea, *_ = project_daily([s.points[0] for s in seeds][-8:], q)
    expected = e12 * 11/13 + q.last * 2/13 - (e26 * 25/27 + q.last * 2/27)
    assert dif == pytest.approx(expected, abs=1e-12)
    assert dea == pytest.approx(seeds[-1].points[0].dea * .8 + expected * .2)


def test_signal_matches_existing_same_day_rule_and_rejects_sibling_failures():
    seeds, universe, *_ = fixture()
    candidates, coverage, checked = evaluate(seeds, universe, now=NOW)
    assert len(candidates) == 1
    assert candidates[0]["signal_group"] == "同日拐头"
    assert coverage["evaluated_count"] == 1
    assert checked == {"600000.SH"}
    universe.quotes[0].last = universe.quotes[0].previous_close
    assert not evaluate(seeds, universe, now=NOW)[0]


@pytest.mark.parametrize("failure", ["st", "growth", "stale", "future", "no_time", "exright", "bad_basis", "missing"])
def test_invalid_evidence_never_alerts(failure):
    seeds, universe, *_ = fixture()
    q = universe.quotes[0]
    if failure == "st": seeds[-1].special_treatment_ids = [q.instrument_id]
    if failure == "growth": q.instrument_id = "300001.SZ"
    if failure == "stale": q.observed_at = NOW - timedelta(minutes=3)
    if failure == "future": q.observed_at = NOW + timedelta(seconds=1)
    if failure == "no_time": q.observed_at = None
    if failure == "exright": q.previous_close -= 1
    if failure == "bad_basis": seeds[-1].points[0].dif += .5
    if failure == "missing": seeds[0].points = []
    assert not evaluate(seeds, universe, now=NOW)[0]


def test_requires_exact_calendar_and_current_st_list():
    seeds, universe, *_ = fixture()
    seeds[-1].st_trade_date -= timedelta(days=1)
    with pytest.raises(ValueError, match="ST"):
        evaluate(seeds, universe, now=NOW)
    with pytest.raises(ValueError, match="nine"):
        evaluate(seeds[1:], universe, now=NOW)


def test_scheduled_hit_alerts_immediately_with_daily_dedup_and_explicit_withdrawal():
    seeds, universe, *_ = fixture()
    candidates, _, checked = evaluate(seeds, universe, now=NOW)
    first = merge_observations([], candidates, now=NOW, evaluated_ids=checked)
    assert first[0]["alerted_at"] == NOW.isoformat()
    repeated = merge_observations(first, candidates, now=NOW, evaluated_ids=checked)
    assert repeated[0]["alerted_at"] == first[0]["alerted_at"]
    candidates[0]["observed_at"] = (NOW + timedelta(minutes=1)).isoformat()
    second = merge_observations(first, candidates, now=NOW+timedelta(minutes=1), evaluated_ids=checked)
    assert second[0]["alerted_at"]
    missing = merge_observations(second, [], now=NOW, evaluated_ids=set())
    assert missing[0]["unverified"]
    withdrawn = merge_observations(second, [], now=NOW, evaluated_ids=checked)
    assert not withdrawn[0]["active"] and not withdrawn[0]["unverified"]
    resumed = merge_observations(withdrawn, candidates, now=NOW, evaluated_ids=checked)
    assert resumed[0]["alerted_at"] == second[0]["alerted_at"]


def test_closed_hours_never_acquire_and_old_session_is_not_today(tmp_path):
    def fail(**kwargs): raise AssertionError("unexpected market acquisition")
    result = refresh_watch(now=NOW.replace(hour=18), root=tmp_path, quote_loader=fail, seed_loader=fail)
    assert result["status"] == "closed"
    assert result["records"] == []


def test_mapper_rejects_wrong_date_and_st_date():
    raw = {"trade_date": "2026-09-16", "indicators": [{"ts_code": "600000.SH", "trade_date": "20260916",
        "close": 10, "high": 11, "low": 9, "amount": 1000, "adj_factor": 1, "close_qfq": 10,
        "macd_dif": .1, "macd_dea": .2, "kdj_k": 30, "kdj_d": 40, "kdj_j": 10}],
        "special_treatment": [{"ts_code": "600001.SH", "trade_date": "20260917"}]}
    kwargs = dict(requested=NOW.date()-timedelta(days=1), st_date=NOW.date(), provider="fixture", now=NOW)
    seed = map_intraday_seed(raw, **kwargs)
    assert seed.points[0].amount_cny == 1e6
    raw["special_treatment"][0]["trade_date"] = "20260916"
    with pytest.raises(ValueError, match="ST"):
        map_intraday_seed(raw, **kwargs)


def test_existing_collector_loop_scans_once_per_minute(monkeypatch):
    from tradex.dashboard import collector_worker as worker
    class Stop:
        count = 0
        def is_set(self): return self.count >= 2
        def wait(self, _): self.count += 1
    for name in ("_refresh_sector_catalog", "_generate_latest_limit_up_pool",
                 "_generate_latest_resonance", "_refresh_manual_portfolio_market",
                 "_generate_latest_review_announcements"):
        monkeypatch.setattr(worker, name, lambda *args, **kwargs: {})
    calls = []
    monkeypatch.setattr("tradex.stock_selection.intraday_macd_j.refresh_watch",
                        lambda **kwargs: calls.append(kwargs["now"]))
    worker._run_post_close_resonance_loop(Stop(), clock=lambda: NOW, check_interval_seconds=0)
    assert calls == [NOW]


def test_page_api_only_reads_snapshot(monkeypatch):
    from tradex.dashboard import __main__ as dashboard
    # GET dispatch must not call refresh_watch or any provider.
    monkeypatch.setattr("tradex.stock_selection.intraday_macd_j.read_watch",
                        lambda: {"contract": "intraday_macd_j_watch.v1", "status": "closed"})
    monkeypatch.setattr("tradex.stock_selection.intraday_macd_j.refresh_watch",
                        lambda **kw: pytest.fail("GET acquired data"))
    handler = object.__new__(dashboard.DashboardHandler)
    handler.path = "/api/stock-selection/intraday-macd-j"
    replies = []
    handler._send_json = lambda status, payload: replies.append((status, payload))
    handler.do_GET()
    assert replies[0][0] == 200
    assert replies[0][1]["status"] == "closed"


@pytest.mark.parametrize("hour,minute,due", [(9,30,True),(9,31,True),(9,32,False),
    (9,45,True),(10,0,True),(10,5,False),(11,30,True),(11,45,True),
    (12,0,False),(12,15,False),(12,30,False),(12,45,False),(13,0,True),
    (14,45,True),(15,0,True),(15,15,False)])
def test_quarter_hour_schedule_and_single_lunch_slot(hour, minute, due):
    assert bool(scan_slot(NOW.replace(hour=hour,minute=minute))) is due


def test_lunch_scan_uses_1130_quotes_but_rejects_older_quotes():
    seeds, universe, *_ = fixture()
    lunch = NOW.replace(hour=11,minute=45)
    universe.quotes[0].observed_at = NOW.replace(hour=11,minute=30)
    assert evaluate(seeds,universe,now=lunch)[0]
    universe.quotes[0].observed_at = NOW.replace(hour=11,minute=27)
    assert not evaluate(seeds,universe,now=lunch)[0]


def test_each_scan_is_immutable_and_date_isolated(tmp_path):
    original = {"last_scan_slot":NOW.isoformat(),"trade_date":"2026-09-17","scan_candidates":[{"instrument_id":"600000.SH"}]}
    archive_scan(original,root=tmp_path)
    archive_scan({**original,"scan_candidates":[]},root=tmp_path)
    history = read_scan_history(trade_date="2026-09-17",root=tmp_path)
    assert history["scans"][0] == original
    assert not read_scan_history(trade_date="2026-09-16",root=tmp_path)["scans"]


def test_batch_quote_route_uses_timestamped_alternative_and_normalizes_units():
    import pandas as pd
    from tradex.data_gateway.intraday_scan_quotes import fetch_intraday_scan_quotes
    class Router:
        def route_validated(self, capability, validate, **kwargs):
            assert capability == "intraday_scan_quotes"
            assert kwargs["symbols"] == ["600000"]
            return validate(pd.DataFrame([{"代码":"600000","名称":"浦发银行","最新价":10,
                "涨跌幅":0,"成交额":123,"流通市值":2,"今开":10,"最高":11,"最低":9,
                "昨收":10,"更新时间":"20260917100000"}]),"tencent_http"),"tencent_http"
    result = fetch_intraday_scan_quotes(["600000.SH"],now=NOW,router=Router())
    assert result.quotes[0].observed_at == NOW
    assert result.quotes[0].amount_cny == 1230000
    assert result.quotes[0].float_market_cap_cny == 200000000
    assert not result.missing_instrument_ids


def test_manual_close_uses_daily_bars_without_forging_timestamps(tmp_path):
    from test_daily_stock_selection import _snapshot, _row
    from tradex.data_gateway.stock_selection_contracts import DailyStockCandlestickBarV1, DailyStockCandlestickHistoryV1
    seeds, universe, *_ = fixture()
    q = universe.quotes[0]
    bar = DailyStockCandlestickBarV1(trade_date=NOW.date(),open=q.open,close=q.last,
        high=q.high,low=q.low,previous_close=q.previous_close,amount_cny=q.amount_cny)
    snapshot = _snapshot(NOW.date(), rows=(_row(NOW.date(),0,open=q.open,close=q.last),),
        candlestick_window_trade_dates=tuple(NOW.date()-timedelta(days=i) for i in range(14,-1,-1)),
        candlestick_histories=(DailyStockCandlestickHistoryV1(instrument_id=q.instrument_id,bars=(bar,)),))
    for seed in seeds: seed.metadata = snapshot.metadata
    now = NOW.replace(hour=19)
    result = run_manual_close_scan(snapshot,seeds,now=now,root=tmp_path)
    assert result['status'] == 'close_scanned'
    assert result['last_scan_slot'] == now.isoformat()
    assert result['scan_candidates'][0]['observed_at'] is None
    assert result['scan_candidates'][0]['price_basis'] == 'daily_close'
    assert read_watch(now=now,root=tmp_path)['status'] == 'close_scanned'
    assert len(read_scan_history(now=now,root=tmp_path)['scans']) == 1
    with pytest.raises(ValueError,match='post-close'):
        run_manual_close_scan(snapshot,seeds,now=NOW,root=tmp_path)
    q.last += 1
    with pytest.raises(ValueError,match='does not match'):
        evaluate(seeds,universe,now=now,closing_snapshot=snapshot)
    # The normal live path continues to reject an old quote.
    assert not evaluate(seeds,universe,now=now)[0]


@pytest.mark.parametrize("kind", ["small_cross", "zero_matches", "unavailable"])
def test_close_uses_exact_archive_and_preserves_estimates(tmp_path, kind):
    from contextlib import closing
    from test_macd_j_selection import source
    from tradex.stock_selection.engine import select_daily_stocks
    from tradex.stock_selection.strategies import build_strategy_results
    from tradex.stock_selection.store import DailyStockSelectionStore
    data = source()
    days = list(data.technicals.days)
    # This published crossover is deliberately below the intraday .004 guard.
    point = days[-1].points[0].model_copy(update={"dif": 1.001 if kind == "small_cross" else .9})
    days[-1] = days[-1].model_copy(update={"points": (point,)})
    data = data.model_copy(update={"technicals": data.technicals.model_copy(update={"days": tuple(days)})})
    if kind == "unavailable":
        data = data.model_copy(update={"technicals": None})
    now = NOW.replace(year=data.trade_date.year, month=data.trade_date.month, day=data.trade_date.day, hour=19)
    result = build_strategy_results(data, select_daily_stocks(data))[-1].model_copy(update={"generated_at": now})
    original = {"last_scan_slot": now.replace(hour=15).isoformat(), "trade_date": str(data.trade_date),
                "scan_kind": "manual_close", "status": "close_scanned", "scan_candidates": []}
    archive_scan(original, root=tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.glob("scan-*.json")}
    with closing(DailyStockSelectionStore(tmp_path / "selection.sqlite3")) as store:
        store.record_strategy_result(result)
        payload = read_watch(now=now, root=tmp_path)
        history = read_scan_history(now=now, root=tmp_path)
        if kind == "unavailable":
            assert payload["status"] != "close_confirmed"
            assert history["scans"] == [original]
        else:
            assert payload["status"] == "close_confirmed"
            assert payload["source_result_id"] == result.result_id
            assert payload["matched_count"] == result.payload.matched_count == (kind == "small_cross")
            assert [c["instrument_id"] for c in payload["scan_candidates"]] == [c.instrument_id for c in result.payload.candidates]
            assert payload["evaluated_count"] == result.payload.evaluated_count
            if kind == "small_cross":
                assert payload["scan_candidates"][0]["dif"] == 1.001
                assert payload["scan_candidates"][0]["observed_at"] is None
            assert history["scans"] == [payload, original]
        assert read_watch(now=now.replace(hour=10), root=tmp_path)["status"] != "close_confirmed"
        assert read_watch(now=now+timedelta(days=1), root=tmp_path)["status"] != "close_confirmed"
        assert store.get_strategy_result(data.trade_date, result.strategy_id, strategy_version="v1") == result
    assert {p.name: p.read_bytes() for p in tmp_path.glob("scan-*.json")} == before


def test_no_close_archive_does_not_create_database(tmp_path):
    result = read_watch(now=NOW.replace(hour=19), root=tmp_path)
    assert result["status"] == "closed"
    assert not (tmp_path / "selection.sqlite3").exists()


def test_scan_industry_display_reads_existing_catalog_without_changing_archive(tmp_path, monkeypatch):
    from tradex.stock_selection.industry_display import SelectionIndustryDisplayV1
    original = {"last_scan_slot": NOW.isoformat(), "trade_date": "2026-09-17",
                "scan_candidates": [{"instrument_id": "600000.SH"}, {"instrument_id": "600001.SH"}]}
    archive_scan(original, root=tmp_path)
    def display(ids):
        assert set(ids) == {"600000.SH", "600001.SH"}
        return SelectionIndustryDisplayV1(quality="degraded", as_of=NOW.date(),
            names_by_instrument={"600000.SH": "银行"}, unclassified_instruments=("600001.SH",))
    monkeypatch.setattr("tradex.stock_selection.industry_display.load_selection_industry_display", display)
    result = read_scan_history(now=NOW, root=tmp_path)
    assert result["scans"] == [original]
    assert result["industry_display"]["names_by_instrument"] == {"600000.SH": "银行"}
    assert result["industry_display"]["unclassified_instruments"] == ["600001.SH"]
