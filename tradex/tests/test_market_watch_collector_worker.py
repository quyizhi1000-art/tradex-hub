from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tradex.dashboard import collector_worker


SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_status_is_ledger_only_and_does_not_import_dashboard_provider_runtime(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    now = datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI)
    monkeypatch.setenv("TRADEX_MARKET_WATCH_DB", str(tmp_path / "status.sqlite3"))
    monkeypatch.setattr(collector_worker, "_now", lambda: now)

    assert collector_worker.main(["--status"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["contract"] == "market_watch_collector_envelope.v1"
    assert payload["latest_accepted_real"] is None
    assert payload["collection_completeness"]["expected_minute_buckets"] == 0
    assert payload["collection_completeness"]["pending"] == 0
    assert not (tmp_path / "status.sqlite3").exists()


def test_worker_lock_rejects_a_second_collector_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "collector.lock"

    with collector_worker.exclusive_worker_lock(lock_path):
        with pytest.raises(RuntimeError, match="already owns"):
            with collector_worker.exclusive_worker_lock(lock_path):
                raise AssertionError("second collector lock must not be acquired")


def test_historical_worker_seam_fails_closed_instead_of_backdating_current_data() -> None:
    with pytest.raises(
        collector_worker.HistoricalMarketWatchUnavailable,
        match="no exact historical",
    ):
        collector_worker._repair_historical(object())


def test_historical_worker_recovers_only_a_provider_verified_final_close(
    monkeypatch,
) -> None:
    target = datetime(2026, 8, 26, 15, 0, tzinfo=SHANGHAI)
    observed = datetime(2026, 8, 27, 0, 5, tzinfo=SHANGHAI)
    slot = SimpleNamespace(minute_bucket=target)
    final = object()
    calls = []
    monkeypatch.setattr(collector_worker, "_now", lambda: observed)
    monkeypatch.setattr(
        collector_worker,
        "_capture_final_close",
        lambda received_slot, received_at: calls.append(
            (received_slot, received_at)
        )
        or final,
    )

    assert collector_worker._repair_historical(slot) is final
    assert calls == [(slot, observed)]

    with pytest.raises(
        collector_worker.HistoricalMarketWatchUnavailable,
        match="no exact historical",
    ):
        collector_worker._repair_historical(
            SimpleNamespace(minute_bucket=target - timedelta(minutes=1))
        )


def test_final_close_requires_provider_date_and_1500_turnover_proof() -> None:
    trade_date = datetime(2026, 8, 26, tzinfo=SHANGHAI).date()
    payload = {
        "provider_as_of": "2026-08-26T15:00:45+08:00",
        "market_turnover": {
            "available": True,
            "today_date": "2026-08-26",
            "as_of": "15:00",
        },
    }

    assert collector_worker._market_payload_has_final_close(payload, trade_date)
    assert not collector_worker._market_payload_has_final_close(
        {**payload, "provider_as_of": "2026-08-27T09:30:00+08:00"},
        trade_date,
    )
    assert not collector_worker._market_payload_has_final_close(
        {
            **payload,
            "market_turnover": {**payload["market_turnover"], "as_of": "14:59"},
        },
        trade_date,
    )


def test_final_breadth_is_derived_from_provider_timestamped_full_universe(
    monkeypatch,
) -> None:
    from tradex import data_gateway

    observed = datetime(2026, 8, 27, 0, 10, tzinfo=SHANGHAI)
    provider_as_of = datetime(2026, 8, 26, 15, 0, 20, tzinfo=SHANGHAI)
    universe = SimpleNamespace(
        metadata=SimpleNamespace(
            provider_as_of=provider_as_of,
            quality=SimpleNamespace(value="accepted"),
            quality_flags=(),
        ),
        quotes=(
            SimpleNamespace(change_pct=1.0),
            SimpleNamespace(change_pct=-0.5),
            SimpleNamespace(change_pct=0.0),
        ),
        provider_row_count=4,
        excluded_row_count=1,
    )
    monkeypatch.setattr(
        data_gateway,
        "fetch_a_share_universe_snapshot",
        lambda **kwargs: universe,
    )
    monkeypatch.setattr(
        data_gateway,
        "metadata_to_component_status",
        lambda _metadata: {"provider_as_of": provider_as_of.isoformat()},
    )

    breadth, status = collector_worker._provider_verified_final_breadth(
        provider_as_of.date(),
        observed,
    )

    assert breadth["up_count"] == 1
    assert breadth["down_count"] == 1
    assert breadth["flat_count"] == 1
    assert breadth["unclassified_count"] == 1
    assert breadth["total_count"] == 4
    assert status["provider_as_of"] == provider_as_of.isoformat()


def test_current_capture_calls_canonical_owner_without_using_web_read_facade(
    monkeypatch,
) -> None:
    from tradex.dashboard import __main__ as dashboard_app
    from tradex.dashboard import risk_service

    now = datetime(2026, 8, 24, 10, 30, 5, tzinfo=SHANGHAI)
    minute = now.replace(second=0)
    canonical = object()
    calls: list[tuple[str, object]] = []

    class FakeService:
        def get(self, *, force, stale_while_revalidate):
            calls.append(("canonical", (force, stale_while_revalidate)))
            return canonical

    class FakeContract:
        @staticmethod
        def model_validate(value):
            assert value is canonical
            return value

    monkeypatch.setattr(collector_worker, "_now", lambda: now)
    monkeypatch.setattr(collector_worker, "MarketWatchSnapshotV1", FakeContract)
    monkeypatch.setattr(
        dashboard_app,
        "get_market_data",
        lambda *, force: calls.append(("market", force)) or {"market": "current"},
    )
    monkeypatch.setattr(
        dashboard_app,
        "_market_payload_is_current",
        lambda _payload, _observed: True,
    )
    monkeypatch.setattr(
        dashboard_app,
        "_get_market_watch_service",
        lambda: FakeService(),
    )
    monkeypatch.setattr(
        dashboard_app,
        "get_market_watch_data",
        lambda *_args, **_kwargs: pytest.fail("collector must not call the Web facade"),
    )
    monkeypatch.setattr(
        risk_service,
        "get_risk_appetite_data",
        lambda payload, *, force, record_trajectory: calls.append(
            ("risk", (payload, force, record_trajectory))
        ),
    )

    result = collector_worker._capture_current(SimpleNamespace(minute_bucket=minute))

    assert result is canonical
    assert calls[-1] == ("canonical", (True, False))
    assert ("market", True) in calls
    assert calls[1][0] == "risk"


def test_supervisor_reopens_runtime_after_transient_cycle_failure() -> None:
    stop_event = threading.Event()
    calls: list[int] = []

    def run_cycle(observed_stop_event, *, once):
        calls.append(len(calls) + 1)
        assert observed_stop_event is stop_event
        assert once is False
        if len(calls) == 1:
            raise RuntimeError("transient sqlite failure")
        stop_event.set()

    collector_worker._run_supervised(
        stop_event,
        run_cycle=run_cycle,
        retry_delay_seconds=0,
    )

    assert calls == [1, 2]


def test_supervised_reopen_restores_persisted_envelope_heartbeat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tradex.market_watch.collection_contracts import CollectorRuntimeState
    from tradex.market_watch.collection_store import MarketWatchCollectionStore

    db_path = tmp_path / "supervised-reopen.sqlite3"
    observed = datetime(2026, 8, 24, 10, 30, 5, tzinfo=SHANGHAI)
    stop_event = threading.Event()
    cycles = 0
    monkeypatch.setenv("TRADEX_MARKET_WATCH_DB", str(db_path))

    class FakeCollector:
        def __init__(self, ledger) -> None:
            self._ledger = ledger

        def run_forever(self, observed_stop_event) -> None:
            nonlocal cycles
            cycles += 1
            if cycles == 1:
                self._ledger.update_runtime(
                    CollectorRuntimeState.DEGRADED,
                    heartbeat_at=observed - timedelta(seconds=5),
                )
                raise RuntimeError("transient lifecycle failure")
            self._ledger.update_runtime(
                CollectorRuntimeState.RUNNING,
                heartbeat_at=observed,
            )
            observed_stop_event.set()

    monkeypatch.setattr(
        collector_worker,
        "build_collector",
        lambda *, ledger, history: FakeCollector(ledger),
    )

    collector_worker._run_supervised(
        stop_event,
        retry_delay_seconds=0,
    )

    with MarketWatchCollectionStore(db_path, read_only=True) as reader:
        envelope = reader.read_envelope(as_of=observed)
    assert cycles == 2
    assert envelope.collector_state is CollectorRuntimeState.RUNNING
    assert envelope.collector_heartbeat_at == observed


def test_post_close_loop_generates_one_batch_for_the_trading_day(monkeypatch) -> None:
    stop_event = threading.Event()
    observed = datetime(2026, 8, 26, 15, 5, tzinfo=SHANGHAI)
    calls = []
    pool_calls = []

    def generate(*, reuse_existing):
        assert reuse_existing is True
        calls.append(observed.date())
        stop_event.set()
        return {"resonance_revision": "a" * 64, "entry_count": 8}

    monkeypatch.setattr(collector_worker, "_generate_latest_resonance", generate)
    monkeypatch.setattr(
        collector_worker,
        "_generate_latest_limit_up_pool",
        lambda *, reuse_existing, analyze: pool_calls.append(
            (observed.date(), reuse_existing, analyze)
        )
        or {"attribution_revision": "d" * 64, "pool_total": 12},
    )

    collector_worker._run_post_close_resonance_loop(
        stop_event,
        clock=lambda: observed,
        check_interval_seconds=0,
    )

    assert calls == [observed.date()]
    assert pool_calls == [(observed.date(), False, True)]


def test_intraday_loop_generates_one_batch_for_the_five_minute_bucket(monkeypatch) -> None:
    stop_event = threading.Event()
    observed = datetime(2026, 8, 26, 10, 37, tzinfo=SHANGHAI)
    calls = []
    pool_calls = []

    def generate(*, reuse_existing):
        assert reuse_existing is True
        calls.append(observed)
        stop_event.set()
        return {"resonance_revision": "b" * 64, "entry_count": 8}

    monkeypatch.setattr(collector_worker, "_generate_latest_resonance", generate)
    monkeypatch.setattr(
        collector_worker,
        "_generate_latest_limit_up_pool",
        lambda *, reuse_existing, analyze: pool_calls.append(
            (observed, reuse_existing, analyze)
        )
        or {"attribution_revision": "e" * 64, "pool_total": 12},
    )

    collector_worker._run_post_close_resonance_loop(
        stop_event,
        clock=lambda: observed,
        check_interval_seconds=0,
    )

    assert calls == [observed]
    assert pool_calls == [(observed, True, False)]


def test_intraday_loop_rechecks_a_new_minute_instead_of_waiting_five_minutes(
    monkeypatch,
) -> None:
    observations = (
        datetime(2026, 8, 26, 13, 5, tzinfo=SHANGHAI),
        datetime(2026, 8, 26, 13, 5, 30, tzinfo=SHANGHAI),
        datetime(2026, 8, 26, 13, 6, tzinfo=SHANGHAI),
    )

    class StepStopEvent:
        index = 0

        def is_set(self):
            return self.index >= len(observations)

        def wait(self, _seconds):
            self.index += 1
            return self.is_set()

    stop_event = StepStopEvent()
    calls = []
    monkeypatch.setattr(
        collector_worker,
        "_generate_latest_resonance",
        lambda *, reuse_existing: calls.append(stop_event.index)
        or {"resonance_revision": "c" * 64, "entry_count": 8},
    )
    pool_calls = []
    monkeypatch.setattr(
        collector_worker,
        "_generate_latest_limit_up_pool",
        lambda *, reuse_existing, analyze: pool_calls.append(
            (stop_event.index, reuse_existing, analyze)
        )
        or {"attribution_revision": "f" * 64, "pool_total": 12},
    )

    collector_worker._run_post_close_resonance_loop(
        stop_event,
        clock=lambda: observations[min(stop_event.index, len(observations) - 1)],
        check_interval_seconds=0,
    )

    assert calls == [0, 2]
    assert pool_calls == [(0, True, False), (2, True, False)]


def test_opening_minutes_publish_limit_status_before_resonance(monkeypatch) -> None:
    stop_event = threading.Event()
    observed = datetime(2026, 8, 26, 9, 31, tzinfo=SHANGHAI)
    resonance_calls = []
    pool_calls = []
    monkeypatch.setattr(
        collector_worker,
        "_generate_latest_resonance",
        lambda *, reuse_existing: resonance_calls.append(reuse_existing),
    )

    def generate_pool(*, reuse_existing, analyze):
        pool_calls.append((reuse_existing, analyze))
        stop_event.set()
        return {"attribution_revision": "1" * 64, "pool_total": 3}

    monkeypatch.setattr(
        collector_worker,
        "_generate_latest_limit_up_pool",
        generate_pool,
    )

    collector_worker._run_post_close_resonance_loop(
        stop_event,
        clock=lambda: observed,
        check_interval_seconds=0,
    )

    assert pool_calls == [(True, False)]
    assert resonance_calls == []


def test_midday_runs_one_full_limit_up_attribution(monkeypatch) -> None:
    stop_event = threading.Event()
    observed = datetime(2026, 8, 26, 11, 35, tzinfo=SHANGHAI)
    pool_calls = []

    def generate_pool(*, reuse_existing, analyze):
        pool_calls.append((reuse_existing, analyze))
        stop_event.set()
        return {"attribution_revision": "2" * 64, "pool_total": 12}

    monkeypatch.setattr(
        collector_worker,
        "_generate_latest_limit_up_pool",
        generate_pool,
    )

    collector_worker._run_post_close_resonance_loop(
        stop_event,
        clock=lambda: observed,
        check_interval_seconds=0,
    )

    assert pool_calls == [(False, True)]
