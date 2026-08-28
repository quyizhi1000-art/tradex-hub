from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tradex.data_gateway.contracts import ContractMetadata, QualityStatus
from tradex.data_gateway.stock_selection_contracts import (
    DailyStockCandlestickBarV1,
    DailyStockCandlestickHistoryV1,
    DailyStockFactorSnapshotV1,
    DailyStockFactorV1,
    StockFactorCoverageV1,
)
from tradex.stock_selection.backtest import walk_forward_backtest
from tradex.stock_selection.engine import (
    SelectionConfigV1,
    screen_long_upper_shadow_trials,
    screen_next_session_limit_up_tendency,
    select_daily_stocks,
)
from tradex.stock_selection.service import DailyStockSelectionService
from tradex.stock_selection.strategies import (
    REGISTERED_STOCK_SELECTION_STRATEGIES,
    RegisteredStockSelectionStrategy,
    build_strategy_results,
    evaluate_strategy_result,
)
from tradex.stock_selection.store import DailyStockSelectionStore


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _row(day: date, index: int, **updates) -> DailyStockFactorV1:
    payload = {
        "instrument_id": f"{600000 + index:06d}.SH",
        "name": f"样本{index}",
        "industry": "软件" if index % 2 else "银行",
        "market": "主板",
        "list_date": day - timedelta(days=800),
        "trade_date": day,
        "open": 10.0 + index,
        "close": 10.5 + index,
        "amount_cny": 300_000_000.0 + index * 10_000_000,
        "total_market_cap_cny": 10_000_000_000.0 + index * 1_000_000_000,
        "float_market_cap_cny": 8_000_000_000.0,
        "turnover_rate_pct": 2.0,
        "volume_ratio": 1.1,
        "pe_ttm": 18.0 + index * 0.2,
        "pb": 1.8 + index * 0.02,
        "dividend_yield_pct": 1.0 + index * 0.1,
        "momentum_20d_pct": -2.0 + index,
        "momentum_60d_pct": -4.0 + index * 1.2,
        "roe_pct": 8.0 + index,
        "gross_margin_pct": 20.0 + index,
        "debt_to_assets_pct": 60.0 - index,
        "revenue_yoy_pct": 5.0 + index * 2,
        "net_profit_yoy_pct": 4.0 + index * 2.2,
        "financial_report_date": day - timedelta(days=60),
        "financial_announcement_date": day - timedelta(days=20),
    }
    payload.update(updates)
    return DailyStockFactorV1(**payload)


def _snapshot(
    day: date,
    rows: tuple[DailyStockFactorV1, ...] | None = None,
    *,
    candlestick_window_trade_dates: tuple[date, ...] = (),
    candlestick_histories: tuple[DailyStockCandlestickHistoryV1, ...] = (),
):
    factors = rows or tuple(_row(day, index) for index in range(12))
    return DailyStockFactorSnapshotV1(
        metadata=ContractMetadata(
            contract="daily_stock_factor_snapshot.v1",
            provider="fixture",
            provider_as_of=datetime.combine(day, datetime.min.time(), tzinfo=SHANGHAI)
            + timedelta(hours=18),
            fetched_at=datetime.combine(day, datetime.min.time(), tzinfo=SHANGHAI)
            + timedelta(hours=18, minutes=1),
            quality=QualityStatus.ACCEPTED,
        ),
        trade_date=day,
        prior_20d_trade_date=day - timedelta(days=28),
        prior_60d_trade_date=day - timedelta(days=90),
        benchmark_instrument_id="000300.SH",
        benchmark_open=100.0,
        benchmark_close=101.0,
        coverage=StockFactorCoverageV1(
            universe_count=len(factors),
            master_count=len(factors),
            daily_basic_count=len(factors),
            momentum_20d_count=len(factors),
            momentum_60d_count=len(factors),
            financial_count=len(factors),
            candlestick_history_count=sum(
                len(item.bars) == len(candlestick_window_trade_dates)
                for item in candlestick_histories
            ),
        ),
        factors=tuple(sorted(factors, key=lambda item: item.instrument_id)),
        candlestick_window_trade_dates=candlestick_window_trade_dates,
        candlestick_histories=tuple(
            sorted(candlestick_histories, key=lambda item: item.instrument_id)
        ),
    )


def _candlestick_bar(
    day: date,
    *,
    long_upper_shadow: bool = False,
    closed_limit_up: bool = False,
) -> DailyStockCandlestickBarV1:
    if closed_limit_up:
        return DailyStockCandlestickBarV1(
            trade_date=day,
            open=10.5,
            high=11.06,
            low=10.4,
            close=11.06,
            previous_close=10.05,
            amount_cny=300_000_000.0,
        )
    if long_upper_shadow:
        return DailyStockCandlestickBarV1(
            trade_date=day,
            open=10.0,
            high=10.8,
            low=9.9,
            close=10.1,
            previous_close=10.0,
            amount_cny=300_000_000.0,
        )
    return DailyStockCandlestickBarV1(
        trade_date=day,
        open=10.0,
        high=10.25,
        low=9.9,
        close=10.2,
        previous_close=10.0,
        amount_cny=300_000_000.0,
    )


def _candlestick_history(
    instrument_id: str,
    dates: tuple[date, ...],
    event_indexes: set[int],
    limit_up_indexes: set[int] | None = None,
) -> DailyStockCandlestickHistoryV1:
    limit_up_indexes = limit_up_indexes or set()
    return DailyStockCandlestickHistoryV1(
        instrument_id=instrument_id,
        bars=tuple(
            _candlestick_bar(
                day,
                long_upper_shadow=index in event_indexes,
                closed_limit_up=index in limit_up_indexes,
            )
            for index, day in enumerate(dates)
        ),
    )


def _tendency_history(
    instrument_id: str,
    dates: tuple[date, ...],
    *,
    final_return_pct: float,
    final_amount_multiple: float,
    closed_limit_up: bool = False,
) -> DailyStockCandlestickHistoryV1:
    bars = []
    previous_close = 10.0
    for index, trade_date in enumerate(dates):
        is_final = index == len(dates) - 1
        return_pct = final_return_pct if is_final else 0.4
        close = previous_close * (1.0 + return_pct / 100.0)
        if is_final and closed_limit_up:
            close = round(previous_close * 1.10 + 1e-12, 2)
        open_price = previous_close * (1.01 if is_final else 1.001)
        amount = 300_000_000.0 * (final_amount_multiple if is_final else 1.0)
        bars.append(
            DailyStockCandlestickBarV1(
                trade_date=trade_date,
                open=open_price,
                high=max(open_price, close) * 1.002,
                low=min(previous_close, open_price, close) * 0.998,
                close=close,
                previous_close=previous_close,
                amount_cny=amount,
            )
        )
        previous_close = close
    return DailyStockCandlestickHistoryV1(
        instrument_id=instrument_id,
        bars=tuple(bars),
    )


def test_selector_uses_hard_gates_missing_penalty_and_deterministic_ranking():
    day = date(2026, 8, 26)
    complete = _row(day, 20, instrument_id="600020.SH")
    missing_debt = _row(
        day,
        20,
        instrument_id="600021.SH",
        debt_to_assets_pct=None,
    )
    st_row = _row(day, 22, instrument_id="600022.SH", name="*ST样本")
    sparse = _row(
        day,
        23,
        instrument_id="600023.SH",
        roe_pct=None,
        gross_margin_pct=None,
        debt_to_assets_pct=None,
        revenue_yoy_pct=None,
        net_profit_yoy_pct=None,
    )
    suspended = _row(
        day,
        24,
        instrument_id="600024.SH",
        open=0,
        amount_cny=0,
    )
    rows = tuple(_row(day, index) for index in range(12)) + (
        complete,
        missing_debt,
        st_row,
        sparse,
        suspended,
    )
    snapshot = _snapshot(day, rows)
    config = SelectionConfigV1(top_n=30, industry_group_minimum=3)

    first = select_daily_stocks(snapshot, config=config)
    second = select_daily_stocks(snapshot, config=config)

    assert first.selection_id == second.selection_id
    assert [item.instrument_id for item in first.candidates] == [
        item.instrument_id for item in second.candidates
    ]
    assert "600022.SH" not in {item.instrument_id for item in first.candidates}
    assert "600023.SH" not in {item.instrument_id for item in first.candidates}
    assert first.excluded_counts["special_treatment"] == 1
    assert first.excluded_counts["insufficient_factor_coverage"] == 1
    assert first.excluded_counts["low_liquidity"] == 1
    scores = {item.instrument_id: item.score for item in first.candidates}
    assert scores["600020.SH"] > scores["600021.SH"]


def test_long_upper_shadow_screen_is_complete_main_board_and_non_st_only():
    day = date(2026, 8, 26)
    dates = tuple(day - timedelta(days=offset) for offset in range(14, -1, -1))
    rows = (
        _row(day, 100, instrument_id="600100.SH", market="主板", name="形态命中"),
        _row(day, 101, instrument_id="600101.SH", market="主板", name="只有一次"),
        _row(day, 102, instrument_id="600102.SH", market="创业板", name="非主板"),
        _row(day, 103, instrument_id="600103.SH", market="主板", name="ST样本"),
        _row(day, 104, instrument_id="600104.SH", market="主板", name="历史不完整"),
    )
    histories = (
        _candlestick_history("600100.SH", dates, {5, 12}),
        _candlestick_history("600101.SH", dates, {8}),
        _candlestick_history("600102.SH", dates, {5, 12}),
        _candlestick_history("600103.SH", dates, {5, 12}),
        _candlestick_history("600104.SH", dates[:-1], {5, 12}),
    )

    screen = screen_long_upper_shadow_trials(
        _snapshot(
            day,
            rows,
            candlestick_window_trade_dates=dates,
            candlestick_histories=histories,
        )
    )

    assert screen.contract == "stock_pattern_screen.v1"
    assert screen.screen_version == "long-upper-shadow-main-board.v3"
    assert screen.quality == "degraded"
    assert screen.lookback_sessions == 10
    assert screen.minimum_occurrences == 2
    assert screen.limit_up_exclusion_lookback_sessions == 10
    assert screen.matched_count == 1
    assert screen.candidates[0].instrument_id == "600100.SH"
    assert [item.trade_date for item in screen.candidates[0].evidence] == [
        dates[5],
        dates[12],
    ]
    assert screen.excluded_counts == {
        "incomplete_candlestick_window": 1,
        "insufficient_occurrences": 1,
        "not_main_board": 1,
        "special_treatment": 1,
    }
    legacy_payload = screen.model_dump(mode="json")
    legacy_payload["screen_version"] = "long-upper-shadow-main-board.v1"
    legacy_payload["lookback_sessions"] = 15
    legacy_payload.pop("limit_up_exclusion_lookback_sessions")
    legacy_screen = type(screen).model_validate(legacy_payload)
    assert legacy_screen.screen_version == "long-upper-shadow-main-board.v1"
    assert legacy_screen.limit_up_exclusion_lookback_sessions is None


def test_long_upper_shadow_screen_excludes_a_limit_up_close_in_the_latest_ten_sessions():
    day = date(2026, 8, 26)
    dates = tuple(day - timedelta(days=offset) for offset in range(14, -1, -1))
    rows = (
        _row(day, 100, instrument_id="600100.SH", market="主板", name="近十日涨停"),
        _row(day, 101, instrument_id="600101.SH", market="主板", name="十日前涨停"),
    )
    histories = (
        _candlestick_history(
            "600100.SH",
            dates,
            {5, 12},
            limit_up_indexes={10},
        ),
        _candlestick_history(
            "600101.SH",
            dates,
            {5, 12},
            limit_up_indexes={4},
        ),
    )

    screen = screen_long_upper_shadow_trials(
        _snapshot(
            day,
            rows,
            candlestick_window_trade_dates=dates,
            candlestick_histories=histories,
        )
    )

    assert screen.matched_count == 1
    assert screen.candidates[0].instrument_id == "600101.SH"
    assert screen.excluded_counts["recent_limit_up"] == 1


def test_long_upper_shadow_screen_counts_occurrences_only_in_the_latest_ten_sessions():
    day = date(2026, 8, 26)
    dates = tuple(day - timedelta(days=offset) for offset in range(14, -1, -1))
    row = _row(day, 105, instrument_id="600105.SH", market="主板", name="窗口外长上影")
    history = _candlestick_history("600105.SH", dates, {4, 12})

    screen = screen_long_upper_shadow_trials(
        _snapshot(
            day,
            (row,),
            candlestick_window_trade_dates=dates,
            candlestick_histories=(history,),
        )
    )

    assert screen.matched_count == 0
    assert screen.excluded_counts["insufficient_occurrences"] == 1


def test_next_session_limit_up_tendency_returns_twenty_ranked_main_board_candidates():
    day = date(2026, 8, 27)
    dates = tuple(day - timedelta(days=offset) for offset in range(14, -1, -1))
    rows = []
    histories = []
    for index in range(25):
        instrument_id = f"{600200 + index:06d}.SH"
        history = _tendency_history(
            instrument_id,
            dates,
            final_return_pct=2.0 + index * 0.25,
            final_amount_multiple=1.0 + index * 0.08,
            closed_limit_up=index == 24,
        )
        latest = history.bars[-1]
        histories.append(history)
        rows.append(
            _row(
                day,
                200 + index,
                instrument_id=instrument_id,
                open=latest.open,
                close=latest.close,
                amount_cny=latest.amount_cny,
                total_market_cap_cny=30_000_000_000.0 - index * 500_000_000.0,
                float_market_cap_cny=20_000_000_000.0 - index * 400_000_000.0,
                turnover_rate_pct=3.0 + index * 0.4,
                volume_ratio=1.0 + index * 0.06,
            )
        )

    snapshot = _snapshot(
        day,
        tuple(rows),
        candlestick_window_trade_dates=dates,
        candlestick_histories=tuple(histories),
    )
    screen = screen_next_session_limit_up_tendency(snapshot)
    selection = select_daily_stocks(snapshot)

    assert screen.contract == "stock_limit_up_tendency_screen.v1"
    assert screen.screen_version == "next-session-limit-up-tendency-main-board.v2"
    assert screen.quality == "accepted"
    assert screen.evaluated_count == 25
    assert screen.selected_count == 20
    assert [item.rank for item in screen.candidates] == list(range(1, 21))
    assert all(item.reasons[0].startswith("机会结构：") for item in screen.candidates)
    assert len({item.reasons for item in screen.candidates}) > 1
    continuation = next(item for item in screen.candidates if item.closed_at_limit_up)
    assert continuation.opportunity_stage == "limit_up_continuation"
    assert len(continuation.reasons) >= 4
    assert any("T+1" in risk and "第三个交易日" in risk for risk in continuation.risks)
    assert any("首次封板时间" in risk for risk in continuation.risks)
    assert any(
        item.opportunity_stage == "pre_limit_up" and not item.closed_at_limit_up
        for item in screen.candidates
    )
    disclosure = "".join(screen.methodology + screen.limitations)
    assert "不代表可校准涨停概率" in disclosure
    assert "预计涨停概率" not in disclosure
    assert selection.limit_up_tendency_screens == (screen,)
    assert selection.pattern_screens[0].screen_version == "long-upper-shadow-main-board.v3"


def test_strategy_result_identity_is_unchanged_when_an_unrelated_strategy_is_registered():
    day = date(2026, 8, 27)
    snapshot = _snapshot(day)
    selection = select_daily_stocks(snapshot)

    baseline = build_strategy_results(snapshot, selection)
    extra = RegisteredStockSelectionStrategy(
        strategy_id="fixture-copy-long-upper-shadow",
        strategy_version="v1",
        title="测试策略",
        result_contract="stock_pattern_screen.v1",
        evaluation_policy="not_defined",
        display_order=99,
        execute=lambda _snapshot_value, selection_value: (
            selection_value.pattern_screens[0]
        ),
    )
    expanded = build_strategy_results(
        snapshot,
        selection,
        strategies=REGISTERED_STOCK_SELECTION_STRATEGIES + (extra,),
    )

    assert {
        item.strategy_id: item.result_id for item in baseline
    } == {
        item.strategy_id: item.result_id
        for item in expanded
        if item.strategy_id != extra.strategy_id
    }


def test_store_archives_each_strategy_independently_for_the_same_trade_date(tmp_path):
    day = date(2026, 8, 27)
    snapshot = _snapshot(day)
    selection = select_daily_stocks(snapshot)
    results = build_strategy_results(snapshot, selection)
    store = DailyStockSelectionStore(tmp_path / "selection.sqlite3")
    try:
        actions = [store.record_strategy_result(item)[0] for item in results]
        stored = store.list_strategy_results(day)
    finally:
        store.close()

    assert actions == ["inserted"] * len(results)
    assert [item.strategy_id for item in stored] == [
        item.strategy_id for item in results
    ]
    assert len({item.result_id for item in stored}) == len(results)


def test_limit_up_strategy_outcome_counts_next_session_touch_and_close():
    signal_day = date(2026, 8, 27)
    evaluation_day = date(2026, 8, 28)
    dates = tuple(signal_day - timedelta(days=offset) for offset in range(14, -1, -1))
    signal_rows = []
    signal_histories = []
    for index in range(2):
        instrument_id = f"{600300 + index:06d}.SH"
        history = _tendency_history(
            instrument_id,
            dates,
            final_return_pct=4.0 + index,
            final_amount_multiple=2.0,
        )
        latest = history.bars[-1]
        signal_histories.append(history)
        signal_rows.append(
            _row(
                signal_day,
                300 + index,
                instrument_id=instrument_id,
                open=latest.open,
                close=latest.close,
                amount_cny=latest.amount_cny,
                turnover_rate_pct=8.0,
                volume_ratio=2.0,
            )
        )
    signal_snapshot = _snapshot(
        signal_day,
        tuple(signal_rows),
        candlestick_window_trade_dates=dates,
        candlestick_histories=tuple(signal_histories),
    )
    tendency_result = next(
        item
        for item in build_strategy_results(
            signal_snapshot,
            select_daily_stocks(signal_snapshot),
        )
        if item.strategy_id == "next-session-limit-up-tendency-main-board"
    )
    candidate_ids = [item.instrument_id for item in tendency_result.payload.candidates]
    evaluation_dates = dates[1:] + (evaluation_day,)
    evaluation_histories = []
    evaluation_rows = []
    for index, instrument_id in enumerate(candidate_ids):
        previous_close = tendency_result.payload.candidates[index].reference_close
        limit_price = round(previous_close * 1.10 + 1e-12, 2)
        close = limit_price if index == 0 else previous_close * 1.04
        high = limit_price if index < 2 else previous_close * 1.05
        bars = list(signal_histories[index].bars[1:])
        bars.append(
            DailyStockCandlestickBarV1(
                trade_date=evaluation_day,
                open=previous_close * 1.02,
                high=high,
                low=previous_close * 1.01,
                close=close,
                previous_close=previous_close,
                amount_cny=400_000_000.0,
            )
        )
        evaluation_histories.append(
            DailyStockCandlestickHistoryV1(
                instrument_id=instrument_id,
                bars=tuple(bars),
            )
        )
        evaluation_rows.append(
            _row(
                evaluation_day,
                400 + index,
                instrument_id=instrument_id,
                open=previous_close * 1.02,
                close=close,
            )
        )
    evaluation_snapshot = _snapshot(
        evaluation_day,
        tuple(evaluation_rows),
        candlestick_window_trade_dates=evaluation_dates,
        candlestick_histories=tuple(evaluation_histories),
    )

    outcome = evaluate_strategy_result(tendency_result, evaluation_snapshot)

    assert outcome.evaluation_policy == "next_session_limit_up"
    assert outcome.evaluated_count == len(candidate_ids)
    assert outcome.touched_limit_up_count == min(2, len(candidate_ids))
    assert outcome.closed_limit_up_count == 1
    assert outcome.touched_limit_up_rate == pytest.approx(
        min(2, len(candidate_ids)) / len(candidate_ids)
    )
    assert outcome.closed_limit_up_rate == pytest.approx(1 / len(candidate_ids))


def test_walk_forward_enters_at_next_session_open_and_charges_costs():
    signal_day = date(2026, 8, 24)
    signal = _snapshot(signal_day)
    config = SelectionConfigV1(top_n=1, industry_group_minimum=3)
    selected_id = select_daily_stocks(signal, config=config).candidates[0].instrument_id
    entry_day = date(2026, 8, 25)
    entry_rows = tuple(
        _row(
            entry_day,
            index,
            open=20.0 if f"{600000 + index:06d}.SH" == selected_id else 10.0 + index,
            close=22.0 if f"{600000 + index:06d}.SH" == selected_id else 10.5 + index,
        )
        for index in range(12)
    )
    entry = _snapshot(entry_day, entry_rows)

    report = walk_forward_backtest(
        [signal, entry],
        config=config,
        horizon_sessions=1,
        round_trip_cost_pct=0.15,
    )

    assert report.signal_count == 1
    assert report.trades[0].entry_trade_date == entry_day
    assert report.trades[0].portfolio_return_pct == pytest.approx(9.85)
    assert report.trades[0].benchmark_return_pct == pytest.approx(1.0)


def test_service_generates_once_and_reads_immutable_archive(tmp_path):
    day = date(2026, 8, 26)
    now = datetime(2026, 8, 26, 18, 31, tzinfo=SHANGHAI)
    calls = []
    store = DailyStockSelectionStore(tmp_path / "selection.sqlite3")
    service = DailyStockSelectionService(
        store,
        factor_loader=lambda requested: calls.append(requested) or _snapshot(requested),
        clock=lambda: now,
    )
    try:
        first = service.generate()
        second = service.generate()
        history = service.history()
    finally:
        store.close()

    assert first["action"] == "inserted"
    assert second["action"] == "existing"
    assert calls == [day]
    assert history["contract"] == "daily_stock_selection_archive.v1"
    assert history["selection"]["selection_id"] == first["selection"]["selection_id"]
    assert len(first["strategy_results"]) == 3
    assert len(second["strategy_results"]) == 3
    assert history["strategy_archive"]["contract"] == (
        "stock_selection_strategy_archive.v1"
    )
    assert len(history["strategy_archive"]["results"]) == 3
    assert len(
        {item["source_snapshot_revision"] for item in first["strategy_results"]}
    ) == 1
    assert history["schedule"]["automatic_if_missing_after"] == "18:30"


def test_service_background_generation_is_single_flight_and_observable(tmp_path):
    day = date(2026, 8, 26)
    now = datetime(2026, 8, 26, 18, 31, tzinfo=SHANGHAI)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def load(requested):
        calls.append(requested)
        entered.set()
        assert release.wait(2.0)
        return _snapshot(requested)

    store = DailyStockSelectionStore(tmp_path / "selection.sqlite3")
    service = DailyStockSelectionService(store, factor_loader=load, clock=lambda: now)
    try:
        first = service.start_generation()
        assert first["contract"] == "daily_stock_selection_generation.v1"
        assert first["state"] == "running"
        assert entered.wait(1.0)

        second = service.start_generation()
        assert second["state"] == "running"
        assert second["job_id"] == first["job_id"]
        assert service.generation_status()["phase"] == "acquiring"

        release.set()
        assert service.wait_for_generation(timeout=2.0)
        completed = service.generation_status()
    finally:
        release.set()
        service.wait_for_generation(timeout=2.0)
        store.close()

    assert completed["state"] == "succeeded"
    assert completed["phase"] == "completed"
    assert completed["result"]["contract"] == "daily_stock_selection_result.v1"
    assert completed["result"]["action"] == "inserted"
    assert calls == [day]


def test_service_background_generation_preserves_safe_failure_state(tmp_path):
    now = datetime(2026, 8, 26, 18, 31, tzinfo=SHANGHAI)
    store = DailyStockSelectionStore(tmp_path / "selection.sqlite3")
    service = DailyStockSelectionService(
        store,
        factor_loader=lambda _requested: (_ for _ in ()).throw(
            RuntimeError("fixture provider failure")
        ),
        clock=lambda: now,
    )
    try:
        started = service.start_generation()
        assert started["state"] == "running"
        assert service.wait_for_generation(timeout=2.0)
        failed = service.generation_status()
    finally:
        service.wait_for_generation(timeout=2.0)
        store.close()

    assert failed["state"] == "failed"
    assert failed["phase"] == "failed"
    assert failed["failed_phase"] == "acquiring"
    assert failed["error"] == "Tushare 每日选股数据暂不可用，未生成候选池。"
    assert failed["failure_code"] == "RuntimeError"
    assert failed["result"] is None


def test_service_automatic_background_generation_keeps_bounded_retries(tmp_path):
    calls = []

    def fail(_requested):
        calls.append(True)
        raise RuntimeError("fixture provider failure")

    store = DailyStockSelectionStore(tmp_path / "selection.sqlite3")
    service = DailyStockSelectionService(store, factor_loader=fail)
    try:
        first = service.maybe_generate_automatic(
            now=datetime(2026, 8, 26, 18, 30, tzinfo=SHANGHAI)
        )
        assert first["state"] == "running"
        assert service.wait_for_generation(timeout=2.0)
        assert service.maybe_generate_automatic(
            now=datetime(2026, 8, 26, 18, 35, tzinfo=SHANGHAI)
        ) == {"action": "retry_cooldown"}

        for minute in (40, 50):
            retried = service.maybe_generate_automatic(
                now=datetime(2026, 8, 26, 18, minute, tzinfo=SHANGHAI)
            )
            assert retried["state"] == "running"
            assert service.wait_for_generation(timeout=2.0)

        assert service.maybe_generate_automatic(
            now=datetime(2026, 8, 26, 19, 0, tzinfo=SHANGHAI)
        ) == {"action": "retry_exhausted"}
    finally:
        service.wait_for_generation(timeout=2.0)
        store.close()

    assert len(calls) == 3


def test_store_prefers_current_screening_archive_without_hiding_legacy(tmp_path):
    day = date(2026, 8, 26)
    generated = datetime(2026, 8, 26, 18, 31, tzinfo=SHANGHAI)
    current = select_daily_stocks(_snapshot(day), generated_at=generated)
    legacy_payload = current.model_dump(mode="json")
    legacy_payload.update(
        {
            "config_version": "daily-stock-selection-balanced.v1",
            "selection_id": f"daily-stock-selection:{'1' * 24}",
            "pattern_screens": [],
        }
    )
    store = DailyStockSelectionStore(tmp_path / "selection.sqlite3")
    try:
        store.record(legacy_payload)
        assert store.get_current(day) is None
        assert store.get(day).config_version == "daily-stock-selection-balanced.v1"

        store.record(current)
        assert store.get_current(day).selection_id == current.selection_id
        assert store.get(day).selection_id == current.selection_id
        assert store.list_dates() == [
            {
                "trade_date": day.isoformat(),
                "selection_id": current.selection_id,
                "config_version": current.config_version,
                "generated_at": generated.isoformat(),
                "source_quality": current.source_quality,
                "selected_count": current.selected_count,
                "verdict": None,
                "excess_return_pct": None,
                "evaluation_trade_date": None,
            }
        ]
    finally:
        store.close()
