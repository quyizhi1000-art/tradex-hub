from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from test_volume_surge_selection import snapshot
from tradex.data_gateway.contracts import ContractMetadata
from tradex.data_gateway.stock_technicals_contracts import (
    StockTechnicalPointV1, StockTechnicalDayV1, StockTechnicalWindowV1,
)
from tradex.stock_selection.macd_j import screen_macd_j


def source(js=(50, 40, 30, 25, 10, 18), *, offset=1, **fields):
    base = snapshot(**fields)
    days = []
    for i, (day, j) in enumerate(zip(base.candlestick_window_trade_dates[-6:], js)):
        days.append(StockTechnicalDayV1(
            metadata=ContractMetadata(contract="stock_technical_day.v1", provider="fixture", quality="accepted",
                fetched_at=datetime(2026, 9, 17, 18, tzinfo=ZoneInfo("Asia/Shanghai"))),
            trade_date=day,
            points=(StockTechnicalPointV1(instrument_id=base.factors[0].instrument_id,
                close=base.factors[0].close, amount_cny=1e8,
                dif=offset + (0.1 if i == 5 else -0.1), dea=offset,
                k=(j + 100) / 3, d=50, j=j),),
            special_treatment_ids=() if i == 5 else None,
        ))
    return base.model_copy(update={"technicals": StockTechnicalWindowV1(days=tuple(days))})


@pytest.mark.parametrize("offset,zone", [(1, "above_zero"), (-1, "below_zero"), (-.05, "crossing_zero")])
def test_fresh_cross_both_sides_and_zero_straddle(offset, zone):
    item = screen_macd_j(source(offset=offset)).candidates[0]
    assert item.zero_axis_zone == zone
    assert item.signal_group == "same_day"
    assert item.gap_sessions == 0
    assert item.j_trough == 10
    assert item.low_j_tags == ("J<20",)


@pytest.mark.parametrize("js,gap,tags", [
    ((60, 55, 50, 45, 40, 48), 0, ()),
    ((30, 20, 10, 15, 18, 19), 2, ("J<20",)),
    ((20, -10, -5, 0, 2, 3), 3, ("J<20", "J<0")),
    ((50, 40, 30, 20, 25, 30), 1, ()),
])
def test_turns_and_labels_do_not_require_threshold_crossing(js, gap, tags):
    item = screen_macd_j(source(js)).candidates[0]
    assert item.gap_sessions == gap
    assert item.low_j_tags == tags


@pytest.mark.parametrize("js,reason", [
    ((1, 2, 3, 4, 5, 6), "no_recent_j_turn"),
    ((30, 20, 20, 20, 20, 25), "no_recent_j_turn"),
    ((50, 40, 30, 10, 18, 17), "j_not_rising"),
    ((50, 40, 30, 10, 18, 18), "j_not_rising"),
])
def test_no_fake_turns(js, reason):
    result = screen_macd_j(source(js))
    assert result.matched_count == 0
    assert result.excluded_counts == {reason: 1}


@pytest.mark.parametrize("fields,reason", [
    ({"market": "创业板"}, "not_main_board"),
    ({"market": "科创板"}, "not_main_board"),
    ({"name": "*ST样本"}, "special_treatment"),
    ({"name": "退市样本"}, "special_treatment"),
    ({"amount_cny": 0}, "inactive_session"),
])
def test_board_st_and_inactive_exclusions(fields, reason):
    assert screen_macd_j(source(**fields)).excluded_counts == {reason: 1}


def test_dated_st_list_overrides_normal_name():
    data = source()
    days = list(data.technicals.days)
    days[-1] = days[-1].model_copy(update={"special_treatment_ids": (data.factors[0].instrument_id,)})
    data = data.model_copy(update={"technicals": StockTechnicalWindowV1(days=tuple(days))})
    assert screen_macd_j(data).excluded_counts == {"special_treatment": 1}


def test_missing_data_and_old_cross_are_not_candidates():
    for change, reason in [({"points": ()}, "incomplete_indicator_window"),
                           ({"dif": 1.1}, "no_fresh_macd_cross")]:
        data = source()
        days = list(data.technicals.days)
        if "points" in change:
            days[0] = days[0].model_copy(update=change)
        else:
            days[-2] = days[-2].model_copy(update={"points": (
                days[-2].points[0].model_copy(update=change),)})
        data = data.model_copy(update={"technicals": StockTechnicalWindowV1(days=tuple(days))})
        result = screen_macd_j(data, version="v1")
        assert result.excluded_counts == {reason: 1}
    assert screen_macd_j(snapshot()).quality == "unavailable"


def test_new_evidence_preserves_four_existing_results_and_roundtrips(tmp_path):
    from tradex.stock_selection.engine import select_daily_stocks
    from tradex.stock_selection.strategies import build_strategy_results
    from tradex.stock_selection.store import DailyStockSelectionStore
    data = source()
    old = data.model_copy(update={"technicals": None})
    selection = select_daily_stocks(old)
    results = build_strategy_results(data, selection)
    assert results[:4] == build_strategy_results(old, selection)[:4]
    with closing(DailyStockSelectionStore(tmp_path / "selection.sqlite3")) as store:
        store.record_strategy_result(results[-1])
        assert store.get_strategy_result(data.trade_date, "macd-j-upturn-main-board") == results[-1]


def test_v2_retains_existing_bullish_state_and_v1_keeps_fresh_cross_rule():
    from tradex.stock_selection.contracts import MacdJScreenV1
    data = source()
    days = list(data.technicals.days)
    days[-2] = days[-2].model_copy(update={"points": (
        days[-2].points[0].model_copy(update={"dif": 1.1}),)})
    data = data.model_copy(update={"technicals": StockTechnicalWindowV1(days=tuple(days))})
    current = screen_macd_j(data, version="v2")
    assert current.screen_version == "macd-j-upturn-main-board.v2"
    assert current.matched_count == 1
    assert current.candidates[0].signal_rule == "bullish_state"
    assert MacdJScreenV1.model_validate_json(current.model_dump_json()) == current
    old = screen_macd_j(data, version="v1")
    assert screen_macd_j(data) == old
    assert old.matched_count == 0
    assert old.excluded_counts == {"no_fresh_macd_cross": 1}
    # A falling J or DIF at/below DEA still fails the current-state rule.
    for fields in ({"j": 9, "k": 109/3}, {"dif": 1.0}):
        changed = list(days)
        changed[-1] = changed[-1].model_copy(update={"points": (
            changed[-1].points[0].model_copy(update=fields),)})
        sibling = data.model_copy(update={"technicals": StockTechnicalWindowV1(days=tuple(changed))})
        assert screen_macd_j(sibling).matched_count == 0


def test_legacy_archive_without_rule_field_still_validates_and_versions_cannot_mix():
    from tradex.stock_selection.contracts import MacdJScreenV1
    old = screen_macd_j(source(), version="v1").model_dump(mode="json")
    for candidate in old['candidates']:
        candidate.pop('signal_rule')
    assert MacdJScreenV1.model_validate(old).candidates[0].signal_rule == 'fresh_cross'
    old['screen_version'] = 'macd-j-upturn-main-board.v2'
    with pytest.raises(ValueError, match='rule must match'):
        MacdJScreenV1.model_validate(old)


def raw_day():
    return {"trade_date": "2026-09-16", "request_id": "fixture", "indicators": [{
        "ts_code": "600000.SH", "trade_date": "20260916", "close": 10, "amount": 100,
        "macd_dif": -.1, "macd_dea": -.2,
        "kdj_k": 30, "kdj_d": 50, "kdj_j": -10,
    }], "special_treatment": [{"ts_code": "600001.SH", "trade_date": "20260916"}]}


def mapped(raw):
    from datetime import date
    from tradex.data_gateway.providers.stock_technicals import map_stock_technical_day
    return map_stock_technical_day(raw, requested=date(2026, 9, 16), provider="fixture",
        fetched_at=datetime(2026, 9, 17, 18, tzinfo=ZoneInfo("Asia/Shanghai")), check_st=True)


def test_mapper_units_adjustment_and_missing_timestamp():
    result = mapped(raw_day())
    assert result.points[0].amount_cny == 100000
    assert result.adjustment == "forward"
    assert result.metadata.provider_as_of is None
    assert result.special_treatment_ids == ("600001.SH",)


@pytest.mark.parametrize("kind", ["wrong_date", "duplicate", "bad_j", "wrong_st_date", "missing_st"])
def test_mapper_rejects_unsafe_provider_payloads(kind):
    raw = raw_day()
    if kind == "wrong_date": raw["indicators"][0]["trade_date"] = "20260915"
    if kind == "duplicate": raw["indicators"] *= 2
    if kind == "bad_j": raw["indicators"][0]["kdj_j"] = 50
    if kind == "wrong_st_date": raw["special_treatment"][0]["trade_date"] = "20260915"
    if kind == "missing_st": raw["special_treatment"] = None
    with pytest.raises(ValueError): mapped(raw)


def test_mapper_marks_partial_invalid_rows_without_inventing_values():
    raw = raw_day()
    raw["indicators"].append({**raw["indicators"][0], "ts_code": "600002.SH", "kdj_j": None})
    result = mapped(raw)
    assert len(result.points) == 1
    assert result.rejected_instrument_ids == ("600002.SH",)


def test_fetcher_routes_named_fields_and_signal_day_st(monkeypatch):
    from tradex.data_sources import tushare_fetchers as ts
    from tradex.data_sources.tushare_client import TushareResult
    calls = []
    def request(api_name, params, fields):
        calls.append((api_name, params, fields))
        rows = raw_day()["indicators" if api_name == "stk_factor" else "special_treatment"]
        return TushareResult(records=tuple(rows), request_id="fixture")
    monkeypatch.setattr(ts, "_request", request)
    assert mapped(ts.fetch_stock_selection_technicals("20260916", check_st=True)).points
    assert [call[0] for call in calls] == ["stk_factor", "stock_st"]
    assert "macd_dif" in calls[0][2]
    assert calls[1][1]["trade_date"] == "20260916"


def test_indicator_outage_preserves_old_strategies_and_missing_result_can_retry(tmp_path):
    from tradex.stock_selection.service import DailyStockSelectionService
    from tradex.stock_selection.store import DailyStockSelectionStore
    data = source()
    now = datetime(2026, 9, 17, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
    def unavailable(_dates):
        raise RuntimeError("fixture outage")
    with closing(DailyStockSelectionStore(tmp_path / "partial.sqlite3")) as store:
        service = DailyStockSelectionService(store,
            factor_loader=lambda _: data.model_copy(update={"technicals": None}),
            technical_loader=unavailable)
        first = service.generate(now=now, trade_date=data.trade_date)
        assert first["missing_strategy_ids"] == ["macd-j-upturn-main-board"]
        before = store.list_strategy_results(data.trade_date)
        assert len(before) == 4
        service._technical_loader = lambda _: data.technicals
        second = service.generate(now=now, trade_date=data.trade_date)
        assert second["missing_strategy_ids"] == []
        after = store.list_strategy_results(data.trade_date)
        assert after[:4] == before
        assert after[-1].payload.matched_count == 1
