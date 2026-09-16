from contextlib import closing
from datetime import date, datetime
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import pytest

from test_daily_stock_selection import _snapshot
from tradex.stock_selection.service import (
    DailyStockSelectionService,
    SelectionCalendarError,
    SelectionDataUnavailableError,
    SelectionTooEarlyError,
)
from tradex.stock_selection.store import DailyStockSelectionStore


SHANGHAI = ZoneInfo("Asia/Shanghai")
FRIDAY = date(2026, 9, 4)
MONDAY = datetime(2026, 9, 7, 15, 0, tzinfo=SHANGHAI)


@pytest.mark.parametrize("now", [MONDAY, datetime(2026, 9, 6, 10, tzinfo=SHANGHAI)])
def test_restart_recovers_friday_with_actual_generation_time(tmp_path, now):
    calls = []

    def load(target):
        calls.append(target)
        return _snapshot(target)

    with closing(DailyStockSelectionStore(tmp_path / "selection.sqlite3")) as store:
        service = DailyStockSelectionService(store, factor_loader=load, clock=lambda: now)
        status = service.maybe_generate_automatic()
        assert status["trade_date"] == FRIDAY.isoformat()
        assert service.wait_for_generation(timeout=5)
        assert service.generation_status()["state"] == "succeeded"
        selected = store.get_current(FRIDAY)
        assert selected.generated_at == now
        assert len(store.list_strategy_results(FRIDAY)) == 4
        service.maybe_generate_automatic()
        assert calls == [FRIDAY]


def test_backfill_preserves_date_and_manual_time_gates(tmp_path):
    with closing(DailyStockSelectionStore(tmp_path / "selection.sqlite3")) as store:
        service = DailyStockSelectionService(store, factor_loader=_snapshot, clock=lambda: MONDAY)
        with pytest.raises(SelectionTooEarlyError):
            service.generate()
        with pytest.raises(SelectionTooEarlyError):
            service.generate(trade_date=date(2026, 9, 8))
        with pytest.raises(SelectionCalendarError):
            service.generate(trade_date=date(2026, 9, 5))
        result = service.generate(trade_date=FRIDAY)
        assert result["selection"]["trade_date"] == FRIDAY.isoformat()
        assert store.get_current(MONDAY.date()) is None


def test_backfill_rejects_current_snapshot(tmp_path):
    with closing(DailyStockSelectionStore(tmp_path / "selection.sqlite3")) as store:
        service = DailyStockSelectionService(
            store, factor_loader=lambda _: _snapshot(MONDAY.date()), clock=lambda: MONDAY
        )
        with pytest.raises(SelectionDataUnavailableError):
            service.generate(trade_date=FRIDAY)
        assert store.get_current(FRIDAY) is None


def test_worker_uses_job_trade_date_and_actual_execution_time(tmp_path, monkeypatch):
    from tradex.analysis_jobs import AnalysisJobStore, DAILY_STOCK_SELECTION
    from tradex.analysis_worker import AnalysisRuntime

    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        return {"action": "inserted", "selection": {"trade_date": FRIDAY.isoformat()}}

    monkeypatch.setattr("tradex.analysis_worker._now", lambda: MONDAY)
    with AnalysisJobStore(tmp_path / "jobs.sqlite3") as jobs:
        jobs.enqueue(
            DAILY_STOCK_SELECTION,
            trade_date=FRIDAY,
            trigger="manual-backfill",
            requested_at=MONDAY,
        )
        runtime = AnalysisRuntime.__new__(AnalysisRuntime)
        runtime.jobs = jobs
        runtime.selection_service = SimpleNamespace(generate=generate)
        runtime.materialize_selection_views = lambda **_: 0
        runtime.execute_next_job()
        assert jobs.latest_job(DAILY_STOCK_SELECTION)["state"] == "succeeded"
        assert calls == [{"now": MONDAY, "trade_date": FRIDAY, "automatic": False}]
