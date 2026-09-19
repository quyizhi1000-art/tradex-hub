from datetime import date, timedelta

import pytest

from test_daily_stock_selection import _candlestick_bar, _row, _snapshot
from tradex.data_gateway.stock_selection_contracts import DailyStockCandlestickHistoryV1
from tradex.stock_selection.volume_surge import screen_volume_surge


def snapshot(*, event=13, volume=200.0, close=10.2, limit=None, missing=None, **row_fields):
    dates = tuple(date(2026, 8, 3) + timedelta(days=i) for i in range(21)
                  if (date(2026, 8, 3) + timedelta(days=i)).weekday() < 5)
    bars = []
    for i, day in enumerate(dates):
        bar = _candlestick_bar(day, closed_limit_up=i == limit)
        updates = {"volume_shares": None if i == missing else volume if i == event else 100.0}
        if i == event and i != limit:
            updates["close"] = close
        bars.append(bar.model_copy(update=updates))
    row = _row(dates[-1], 0, **row_fields)
    return _snapshot(dates[-1], (row,), candlestick_window_trade_dates=dates,
                     candlestick_histories=(DailyStockCandlestickHistoryV1(
                         instrument_id=row.instrument_id, bars=tuple(bars)),))


@pytest.mark.parametrize("event,matched", [(7, 0), (8, 1), (13, 1), (14, 0)])
def test_exact_seven_session_window_and_two_times_threshold(event, matched):
    result = screen_volume_surge(snapshot(event=event))
    assert result.matched_count == matched
    if matched:
        evidence = result.candidates[0].evidence[0]
        assert evidence.volume_multiple == 2.0
        assert evidence.prior_5d_average_volume_shares == 100.0


@pytest.mark.parametrize("updates,reason", [
    ({"volume": 199.99}, "no_upward_volume_surge"),
    ({"close": 10.0}, "no_upward_volume_surge"),
    ({"close": 9.99}, "no_upward_volume_surge"),
    ({"limit": 8}, "limit_up_in_7_sessions"),
    ({"limit": 14}, "limit_up_in_7_sessions"),
    ({"missing": 3}, "missing_volume_window"),
    ({"missing": 14}, "missing_volume_window"),
    ({"market": "创业板"}, "not_main_board"),
    ({"name": "*ST样本"}, "special_treatment"),
])
def test_exclusions_fail_closed(updates, reason):
    result = screen_volume_surge(snapshot(**updates))
    assert result.matched_count == 0
    assert result.excluded_counts[reason] == 1


def test_old_limit_up_and_unused_missing_volume_do_not_exclude():
    result = screen_volume_surge(snapshot(limit=7, missing=2))
    assert result.matched_count == 1


def test_missing_calendar_session_is_not_replaced_with_older_bar():
    source = snapshot()
    history = source.candlestick_histories[0]
    source = source.model_copy(update={"candlestick_histories": (
        history.model_copy(update={"bars": history.bars[:9] + history.bars[10:]}),)})
    result = screen_volume_surge(source)
    assert result.quality == "unavailable"
    assert result.excluded_counts == {"incomplete_candlestick_window": 1}


def test_intraday_touch_is_not_a_closing_limit_up():
    source = snapshot()
    history = source.candlestick_histories[0]
    bars = list(history.bars)
    bars[-1] = bars[-1].model_copy(update={"high": 11.0})
    source = source.model_copy(update={"candlestick_histories": (
        history.model_copy(update={"bars": tuple(bars)}),)})
    assert screen_volume_surge(source).matched_count == 1


def test_volume_addition_preserves_existing_strategy_payloads_and_archive_roundtrip():
    from tradex.stock_selection.engine import select_daily_stocks
    from tradex.stock_selection.strategies import build_strategy_results
    from tradex.stock_selection.contracts import StockSelectionStrategyResultV1

    source = snapshot()
    history = source.candlestick_histories[0]
    legacy = source.model_copy(update={"candlestick_histories": (
        history.model_copy(update={"bars": tuple(
            bar.model_copy(update={"volume_shares": None}) for bar in history.bars)}),)})
    results = build_strategy_results(source, select_daily_stocks(source))
    old_results = build_strategy_results(legacy, select_daily_stocks(legacy))
    assert [item.payload for item in results[:3]] == [item.payload for item in old_results[:3]]
    surge = next(item for item in results if item.strategy_id == "upward-volume-surge-main-board")
    assert surge.strategy_id == "upward-volume-surge-main-board"
    assert surge.payload.matched_count == 1
    assert StockSelectionStrategyResultV1.model_validate_json(surge.model_dump_json()) == surge
    assert next(item for item in old_results if item.strategy_id == surge.strategy_id).quality == "unavailable"


def test_backfill_only_missing_snapshot_strategy_preserves_existing_archives(tmp_path):
    from contextlib import closing
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from tradex.stock_selection.engine import select_daily_stocks
    from tradex.stock_selection.strategies import build_strategy_results
    from tradex.stock_selection.service import DailyStockSelectionService
    from tradex.stock_selection.store import DailyStockSelectionStore

    source = snapshot()
    old = select_daily_stocks(source)
    existing_results = build_strategy_results(source, old)[:3]
    changed = source.model_copy(update={"factors": (source.factors[0].model_copy(
        update={"amount_cny": 0.0}),)})
    now = datetime(2026, 9, 16, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    with closing(DailyStockSelectionStore(tmp_path / 'selection.sqlite3')) as store:
        store.record(old)
        for result in existing_results:
            store.record_strategy_result(result)
        service = DailyStockSelectionService(store, factor_loader=lambda _: changed)
        result = service.generate(now=now, trade_date=source.trade_date)
        assert store.get_current(source.trade_date) == old
        archived = store.list_strategy_results(source.trade_date)
        assert archived[:3] == list(existing_results)
        assert len(result['strategy_results']) == 4
        surge = next(item for item in archived if item.strategy_id == "upward-volume-surge-main-board")
        assert surge.payload.matched_count == 1
        assert surge.generated_at == now
        assert surge.source_snapshot_revision != existing_results[0].source_snapshot_revision


def floor_snapshot(changes):
    source = snapshot(event=8, volume=1000)
    history = source.candlestick_histories[0]
    bars = list(history.bars)
    bars[8] = bars[8].model_copy(update={"low": 10.0})
    for index, update in changes.items():
        bars[index] = bars[index].model_copy(update=update)
    return source.model_copy(update={
        "factors": (source.factors[0].model_copy(update={"close": bars[-1].close}),),
        "candlestick_histories": (history.model_copy(update={"bars": tuple(bars)}),),
    })


def test_floor_allows_intraday_breach_and_equal_close():
    result = screen_volume_surge(floor_snapshot({9: {"low": 9.5, "close": 10.0}}))
    candidate = result.candidates[0]
    assert candidate.anchor_low == 10.0
    assert candidate.minimum_subsequent_close == 10.0
    assert len(candidate.evidence) == 1
    assert candidate.reset_count == 0
    assert result.screen_version.endswith(".v3")


@pytest.mark.parametrize("index", [9, 14])
def test_any_subsequent_close_below_floor_excludes_even_after_recovery(index):
    result = screen_volume_surge(floor_snapshot({index: {"low": 9.5, "close": 9.99}}))
    assert result.matched_count == 0
    assert result.excluded_counts["close_below_anchor_low"] == 1


@pytest.mark.parametrize("restart", [10, 12])
def test_breach_day_or_later_event_restarts_and_discards_old_hits(restart):
    changes = {10: {"close": 9.8, "low": 9.5}, restart: {
        "close": 9.8, "low": 9.5, "previous_close": 9.7, "volume_shares": 1000,
    }}
    result = screen_volume_surge(floor_snapshot(changes))
    candidate = result.candidates[0]
    assert candidate.anchor_low == 9.5
    assert candidate.anchor_trade_date == floor_snapshot(changes).candlestick_histories[0].bars[restart].trade_date
    assert len(candidate.evidence) == 1
    assert candidate.reset_count == 1


def test_intact_round_adds_hits_without_lowering_the_floor():
    event = {"close": 10.4, "low": 9.0, "high": 10.5, "volume_shares": 1000}
    result = screen_volume_surge(floor_snapshot({10: event}))
    assert len(result.candidates[0].evidence) == 2
    assert result.candidates[0].anchor_low == 10.0
    assert result.candidates[0].reset_count == 0
    broken = screen_volume_surge(floor_snapshot({10: event, 11: {"close": 9.9, "low": 9.8}}))
    assert broken.matched_count == 0


def test_new_event_on_final_day_waits_for_confirmation():
    result = screen_volume_surge(snapshot(event=14))
    assert result.matched_count == 0
    assert result.excluded_counts["pending_next_day_confirmation"] == 1


def test_all_three_archived_rule_versions_coexist_and_count_as_one_strategy(tmp_path):
    from contextlib import closing
    from tradex.stock_selection.contracts import VolumeSurgeScreenV1
    from tradex.stock_selection.engine import select_daily_stocks
    from tradex.stock_selection.strategies import build_strategy_results
    from tradex.stock_selection.store import DailyStockSelectionStore

    source = snapshot()
    current = next(item for item in build_strategy_results(source, select_daily_stocks(source))
                   if item.strategy_id == "upward-volume-surge-main-board")
    legacy = current.payload.model_dump(mode="json")
    legacy["screen_version"] = "upward-volume-surge-main-board.v1"
    for candidate in legacy["candidates"]:
        for key in ("anchor_trade_date", "anchor_low", "minimum_subsequent_close", "reset_count"):
            candidate.pop(key)
        for event in candidate["evidence"]:
            event.pop("low")
            for key in ("next_trade_date", "next_volume_shares", "next_volume_ratio"):
                event.pop(key)
    old = current.model_copy(update={"strategy_version": "v1",
        "result_id": "stock-selection-strategy-result:" + "a" * 24,
        "payload": VolumeSurgeScreenV1.model_validate(legacy)})
    with closing(DailyStockSelectionStore(tmp_path / "selection.sqlite3")) as store:
        store.record_strategy_result(old)
        v2_payload = current.payload.model_dump(mode="json")
        v2_payload["screen_version"] = "upward-volume-surge-main-board.v2"
        for candidate in v2_payload["candidates"]:
            for event in candidate["evidence"]:
                for key in ("next_trade_date", "next_volume_shares", "next_volume_ratio"):
                    event.pop(key)
        v2 = current.model_copy(update={"strategy_version": "v2",
            "result_id": "stock-selection-strategy-result:" + "b" * 24,
            "payload": VolumeSurgeScreenV1.model_validate(v2_payload)})
        store.record_strategy_result(v2)
        store.record_strategy_result(current)
        assert store.get_strategy_result(source.trade_date, current.strategy_id, strategy_version="v1") == old
        assert store.get_strategy_result(source.trade_date, current.strategy_id, strategy_version="v2") == v2
        assert store.get_strategy_result(source.trade_date, current.strategy_id, strategy_version="v3") == current
        assert store.list_strategy_dates()[0]["strategy_count"] == 1


@pytest.mark.parametrize("next_volume,matched", [(131.99, 1), (132, 1), (132.01, 0), (133, 0)])
def test_next_session_volume_exact_66_percent_boundary(next_volume, matched):
    source = snapshot()
    history = source.candlestick_histories[0]
    bars = list(history.bars)
    bars[-1] = bars[-1].model_copy(update={"volume_shares": next_volume})
    source = source.model_copy(update={"candlestick_histories": (history.model_copy(update={"bars": tuple(bars)}),)})
    result = screen_volume_surge(source)
    assert result.matched_count == matched
    if matched:
        event = result.candidates[0].evidence[0]
        assert event.next_trade_date == bars[-1].trade_date
        assert event.next_volume_shares == next_volume
        assert event.next_volume_ratio == next_volume / 200
    else:
        assert result.excluded_counts["next_day_volume_above_66pct"] == 1


def test_unconfirmed_event_does_not_displace_an_existing_confirmed_round():
    result = screen_volume_surge(floor_snapshot({14: {"volume_shares": 1000}}))
    assert len(result.candidates[0].evidence) == 1
    assert result.candidates[0].reset_count == 0


def test_rejected_first_event_cannot_set_the_price_floor():
    # First event fails next-day contraction; later confirmed event sets its own low.
    result = screen_volume_surge(floor_snapshot({
        9: {"volume_shares": 700}, 10: {"close": 9.8, "low": 9.5,
                                       "previous_close": 9.7, "volume_shares": 1000},
    }))
    assert result.candidates[0].anchor_low == 9.5
    assert result.candidates[0].reset_count == 0
    assert len(result.candidates[0].evidence) == 1


def test_next_session_skips_weekend_and_does_not_use_bars_after_archive_date():
    source = snapshot(event=9)
    event = screen_volume_surge(source).candidates[0].evidence[0]
    assert (event.next_trade_date - event.trade_date).days == 3
    source = snapshot(event=14)
    history = source.candlestick_histories[0]
    future = history.bars[-1].model_copy(update={"trade_date": source.trade_date + timedelta(days=3), "volume_shares": 1})
    source = source.model_copy(update={"candlestick_histories": (
        history.model_copy(update={"bars": (*history.bars, future)}),)})
    assert screen_volume_surge(source).matched_count == 0
