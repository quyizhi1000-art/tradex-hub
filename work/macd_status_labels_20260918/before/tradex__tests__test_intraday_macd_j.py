from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from tradex.stock_selection.intraday_macd_j import (
    evaluate as real_evaluate, merge_observations, prior_sessions, project_daily, read_watch, refresh_watch as real_refresh_watch,
    scan_slot, archive_scan, read_scan_history,
    run_manual_close_scan,
)
from tradex.data_gateway.intraday_technical_seed import map_intraday_seed

NOW = datetime(2026, 9, 17, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def price_histories(seeds, day):
    from tradex.data_gateway.contracts import ContractMetadata, OHLCVBarV1, OHLCVSeriesV1
    from tradex.data_gateway.macd_price_history import sixty_sessions
    dates = sixty_sessions(day)[:-1]
    seedmap = {seed.trade_date: seed.points[0] for seed in seeds if seed.points}
    bars = []
    for index, d in enumerate(dates):
        p = seedmap.get(d)
        close = p.close if p else 10
        bars.append(OHLCVBarV1(trading_date=d, open=close, close=close,
            high=80 if index == 0 else p.high if p else close+1,
            low=p.low if p else close-1, volume_shares=100000, amount_cny=1000000))
    return (OHLCVSeriesV1(metadata=ContractMetadata(contract="ohlcv_bar.v1", provider="fixture",
        fetched_at=NOW, quality="accepted"), instrument_id="600000.SH", period="daily",
        adjustment="forward", bars=tuple(bars)),)


def evaluate(seeds, universe, **kwargs):
    kwargs.setdefault("price_histories", price_histories(seeds, kwargs["now"].date()))
    return real_evaluate(seeds, universe, **kwargs)


def refresh_watch(**kwargs):
    kwargs.setdefault("price_loader", lambda instruments, day, **kw: price_histories(fixture()[0], day))
    return real_refresh_watch(**kwargs)



@pytest.fixture(autouse=True)
def isolated_close_archive(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADEX_DAILY_STOCK_SELECTION_DB", str(tmp_path / "selection.sqlite3"))
    monkeypatch.setattr("tradex.stock_selection.intraday_macd_j.fetch_macd_price_histories", lambda instruments, day, **kw: price_histories(fixture()[0], day))


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
           last=last, high=last, low=closes[-1], open=closes[-1], previous_close=closes[-1], amount_cny=1e7, volume_ratio=1.0)
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


@pytest.mark.parametrize("age", [1, 2, 3])
def test_intraday_accepts_recent_cross_but_rejects_older_bullish_state(age):
    seeds, universe, *_ = fixture()
    q = universe.quotes[0]
    for _ in range(age):
        dif, dea, k, d, j = project_daily([s.points[0] for s in seeds][-8:], q)
        assert dif > dea
        previous = NS(instrument_id=q.instrument_id, close=q.last, adjusted_close=q.last,
                      high=q.high, low=q.low, amount_cny=q.amount_cny, adjustment_factor=1,
                      dif=dif, dea=dea, k=k, d=d, j=j)
        points = [s.points[0] for s in seeds][1:] + [previous]
        for seed, point in zip(seeds, points):
            seed.points = [point]
        q.previous_close = previous.close
        q.last += 3
        q.high = q.last
    candidates, coverage, _ = evaluate(seeds, universe, now=NOW)
    assert coverage["evaluated_count"] == 1
    assert candidates == []
    assert coverage['excluded_counts'] == {'no_v4_signal': 1}



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
            if capability == "intraday_scan_universe":
                raise RuntimeError("primary unavailable in fallback fixture")
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


def test_stock_index_code_collisions_do_not_abort_later_scan_batches():
    from tradex.data_gateway.intraday_scan_quotes import fetch_intraday_scan_quotes
    from tradex.utils.symbol import get_exchange
    instruments = [f"{n:06d}.SZ" for n in range(500, 980)] + ["600000.SH"]
    calls = []
    class Router:
        def route_validated(self, capability, validate, **kwargs):
            if capability == "intraday_scan_universe":
                raise RuntimeError("primary unavailable in fallback fixture")
            calls.append(kwargs["symbols"])
            rows = [{"code": code, "name": "stock", "last": 10, "change_pct": 0,
                     "成交额": 123, "open": 10, "high": 11, "low": 9,
                     "previous_close": 10, "更新时间": "20260917100000"}
                    for code in kwargs["symbols"]]
            return validate(rows, "tencent_http"), "tencent_http"
    result = fetch_intraday_scan_quotes(instruments, now=NOW, router=Router())
    assert len(result.quotes) == len(instruments)
    assert not result.missing_instrument_ids
    assert len(calls) == 7
    assert {q.instrument_id for q in result.quotes} == set(instruments)
    # The stock mapper must not change the separate index-query convention.
    assert get_exchange("sh000688") == get_exchange("000905") == "sh"


@pytest.mark.parametrize("requested,evaluated,status", [(3, 1, "partial"), (1, 1, "monitoring")])
def test_partial_live_and_history_readback_preserves_immutable_scan(tmp_path, monkeypatch, requested, evaluated, status):
    monkeypatch.setattr("tradex.stock_selection.intraday_macd_j.with_previous_close_difference", lambda value, **kw: value)
    import json
    original = {"trade_date": str(NOW.date()), "screen_version":"macd-j-upturn-main-board.v5", "status": "monitoring", "message": "scan",
                "last_scan_slot": NOW.isoformat(), "generated_at": NOW.isoformat(),
                "next_scan_at": (NOW + timedelta(minutes=15)).isoformat(),
                "requested_count": requested, "evaluated_count": evaluated,
                "scan_candidates": [{"instrument_id": "600000.SH"}], "records": []}
    archive_scan(original, root=tmp_path)
    path = tmp_path / f"watch-{NOW.date()}.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    live = read_watch(now=NOW, root=tmp_path)
    history = read_scan_history(now=NOW, root=tmp_path)["scans"][0]
    assert live["status"] == history["status"] == status
    if status == "partial":
        assert "扫描不完整" in live["message"]
        assert "不代表全量结果" in history["message"]
    assert history["scan_candidates"] == original["scan_candidates"]
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.json")} == before


@pytest.mark.parametrize("force", [False, True])
def test_scheduled_scan_persists_partial_quality_and_keeps_verified_hits(tmp_path, force):
    import json
    from tradex.data_gateway.contracts import ContractMetadata
    from tradex.data_gateway.intraday_technical_seed import IntradayTechnicalSeedDayV1
    seeds, universe, *_ = fixture()
    metadata = ContractMetadata(contract="fixture.v1", provider="fixture", fetched_at=NOW, quality="accepted")
    for index, seed in enumerate(seeds):
        model = IntradayTechnicalSeedDayV1(metadata=metadata, trade_date=seed.trade_date,
            points=[vars(p) for p in seed.points], rejected_count=0,
            st_trade_date=NOW.date() if index == 8 else None)
        name = f"seed-{seed.trade_date}-st-{NOW.date() if index == 8 else None}.json"
        (tmp_path / name).write_text(model.model_dump_json(), encoding="utf-8")
    universe.requested_count = 2
    universe.missing_instrument_ids = ("600001.SH",)
    universe.metadata = metadata
    now = NOW.replace(hour=12, minute=10) if force else NOW
    if force:
        universe.quotes[0].observed_at = NOW.replace(hour=11, minute=30)
    result = refresh_watch(now=now, root=tmp_path, quote_loader=lambda *a, **kw: universe, force=force)
    assert result["status"] == "partial"
    assert result["evaluated_count"] == 1
    assert result["missing_quote_count"] == 1
    assert len(result["scan_candidates"]) == 1
    assert result["records"][0]["alerted_at"]
    archive = next(tmp_path.glob("scan-*.json"))
    saved = json.loads(archive.read_text(encoding="utf-8"))
    assert saved == result
    if force:
        assert result["scan_kind"] == "manual_intraday"
        assert result["quote_session"] == "midday_close"
        assert "午盘收市行情" in result["message"]
        refresh_watch(now=now+timedelta(microseconds=1), root=tmp_path,
                      quote_loader=lambda *a, **kw: universe, force=True)
        assert json.loads(archive.read_text(encoding="utf-8")) == saved
        assert len(read_scan_history(now=now, root=tmp_path)["scans"]) == 2


@pytest.mark.parametrize("hour", [8, 16, 20])
def test_forced_scan_does_not_bypass_session_boundary(hour, tmp_path):
    with pytest.raises(ValueError, match="trading session"):
        refresh_watch(now=NOW.replace(hour=hour), root=tmp_path, force=True)


@pytest.mark.parametrize("failures,attempts,received", [({2, 4}, 6, 320), ({2, 3}, 3, 80)])
def test_batch_failure_budget_is_consecutive_and_missing_quotes_stay_explicit(failures, attempts, received):
    from tradex.data_gateway.intraday_scan_quotes import fetch_intraday_scan_quotes
    instruments = [f"{n:06d}.SZ" for n in range(100, 580)]
    class Router:
        calls = 0
        def route_validated(self, capability, validate, **kwargs):
            if capability == "intraday_scan_universe":
                raise RuntimeError("primary unavailable in fallback fixture")
            self.calls += 1
            if self.calls in failures:
                raise RuntimeError("batch unavailable")
            rows = [{"code": code, "name": "stock", "last": 10, "change_pct": 0,
                     "成交额": 123, "open": 10, "high": 11, "low": 9, "previous_close": 10,
                     "更新时间": "20260917100000"} for code in kwargs["symbols"]]
            return validate(rows, "tencent_http"), "tencent_http"
    router = Router()
    result = fetch_intraday_scan_quotes(instruments, now=NOW, router=router)
    assert router.calls == attempts
    assert len(result.quotes) == received
    assert len(result.missing_instrument_ids) == len(instruments) - received
    assert result.metadata.quality == "degraded"


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
    from test_macd_j_v4 import enriched
    from contextlib import closing
    from test_macd_j_selection import source
    from tradex.stock_selection.engine import select_daily_stocks
    from tradex.stock_selection.strategies import build_strategy_results
    from tradex.stock_selection.store import DailyStockSelectionStore
    data = enriched(high=100)
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
        assert store.get_strategy_result(data.trade_date, result.strategy_id, strategy_version="v5") == result
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
