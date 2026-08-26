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
    select_daily_stocks,
)
from tradex.stock_selection.service import DailyStockSelectionService
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
) -> DailyStockCandlestickBarV1:
    if long_upper_shadow:
        return DailyStockCandlestickBarV1(
            trade_date=day,
            open=10.0,
            high=10.8,
            low=9.9,
            close=10.1,
            amount_cny=300_000_000.0,
        )
    return DailyStockCandlestickBarV1(
        trade_date=day,
        open=10.0,
        high=10.25,
        low=9.9,
        close=10.2,
        amount_cny=300_000_000.0,
    )


def _candlestick_history(
    instrument_id: str,
    dates: tuple[date, ...],
    event_indexes: set[int],
) -> DailyStockCandlestickHistoryV1:
    return DailyStockCandlestickHistoryV1(
        instrument_id=instrument_id,
        bars=tuple(
            _candlestick_bar(day, long_upper_shadow=index in event_indexes)
            for index, day in enumerate(dates)
        ),
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
        _candlestick_history("600100.SH", dates, {2, 12}),
        _candlestick_history("600101.SH", dates, {8}),
        _candlestick_history("600102.SH", dates, {2, 12}),
        _candlestick_history("600103.SH", dates, {2, 12}),
        _candlestick_history("600104.SH", dates[:-1], {2, 12}),
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
    assert screen.screen_version == "long-upper-shadow-main-board.v1"
    assert screen.quality == "degraded"
    assert screen.lookback_sessions == 15
    assert screen.minimum_occurrences == 2
    assert screen.matched_count == 1
    assert screen.candidates[0].instrument_id == "600100.SH"
    assert [item.trade_date for item in screen.candidates[0].evidence] == [
        dates[2],
        dates[12],
    ]
    assert screen.excluded_counts == {
        "incomplete_candlestick_window": 1,
        "insufficient_occurrences": 1,
        "not_main_board": 1,
        "special_treatment": 1,
    }


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
