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


@pytest.fixture(autouse=True)
def _disable_review_announcement_network(monkeypatch):
    monkeypatch.setattr(
        collector_worker,
        "_generate_latest_review_announcements",
        lambda: {
            "source_revision": "9" * 64,
            "candidate_count": 0,
            "announcement_count": 0,
            "rematerialize_job_id": "fixture",
        },
    )
    monkeypatch.setattr(
        collector_worker,
        "_refresh_manual_portfolio_market",
        lambda: {
            "portfolio_revision": "7" * 64,
            "snapshot_revision": "8" * 64,
            "item_count": 0,
            "alert_count": 0,
        },
    )


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


def test_historical_worker_routes_0925_to_exact_auction_reconstructor() -> None:
    target = datetime(2026, 8, 26, 9, 25, tzinfo=SHANGHAI)
    observed = datetime(2026, 8, 27, 0, 5, tzinfo=SHANGHAI)
    expected = object()
    calls = []

    class Reconstructor:
        def reconstruct_opening_auction(self, received, progress):
            calls.append(received)
            progress(1, 1, "done", None)
            return expected

    assert collector_worker._repair_historical(
        SimpleNamespace(minute_bucket=target),
        reconstructor=Reconstructor(),
        observed=observed,
    ) is expected
    assert calls == [target]


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


def test_final_close_uses_exact_close_trajectory_instead_of_latest_live_cutoff(
    monkeypatch,
) -> None:
    from tradex.dashboard import __main__ as dashboard_app
    from tradex.dashboard import risk_service

    minute = datetime(2026, 8, 26, 15, 0, tzinfo=SHANGHAI)
    observed = minute + timedelta(minutes=20)
    slot = SimpleNamespace(minute_bucket=minute)
    exact_defense = {"as_of": minute.isoformat(), "sectors": [{"points": [1]}]}
    exact_offense = {"as_of": minute.isoformat(), "sectors": [{"points": [2]}]}
    captured = {}
    snapshot = SimpleNamespace(
        market_state=SimpleNamespace(phase=collector_worker.MarketPhase.CLOSED),
        freshness=SimpleNamespace(
            status=collector_worker.FreshnessStatus.FRESH,
            components=tuple(
                SimpleNamespace(
                    component=name,
                    status=collector_worker.FreshnessStatus.FRESH,
                )
                for name in ("indices", "breadth", "turnover", "rotation")
            ),
        ),
        indices=(SimpleNamespace(available=True), SimpleNamespace(available=True)),
        breadth=SimpleNamespace(available=True),
        turnover=SimpleNamespace(available=True),
        rotation=SimpleNamespace(sectors=(object(),)),
    )

    def exact_rotation(received):
        captured["as_of"] = received
        return {
            "sector_flow_trajectory": exact_defense,
            "offense_sector_flow_trajectory": exact_offense,
        }

    def build_snapshot(payload):
        captured["payload"] = payload
        return snapshot

    monkeypatch.setattr(dashboard_app, "get_market_data", lambda *, force: {"close": True})
    monkeypatch.setattr(
        collector_worker,
        "_market_payload_has_final_close",
        lambda _payload, _date: True,
    )
    monkeypatch.setattr(
        collector_worker,
        "_provider_verified_final_breadth",
        lambda _date, _observed: ({"available": True}, {"status": "fresh"}),
    )
    monkeypatch.setattr(
        risk_service,
        "get_risk_appetite_data",
        lambda *_args, **_kwargs: {
            "sector_flow_trajectory": {"as_of": "2026-08-26T14:43:00+08:00"},
            "offense_sector_flow_trajectory": {"as_of": "2026-08-26T14:43:00+08:00"},
            "components": {},
        },
    )
    monkeypatch.setattr(
        risk_service,
        "get_rotation_radar_as_of",
        exact_rotation,
    )
    monkeypatch.setattr(
        dashboard_app,
        "_build_market_watch_snapshot",
        build_snapshot,
    )
    monkeypatch.setattr(
        collector_worker.MarketWatchSnapshotV1,
        "model_validate",
        lambda _payload: snapshot,
    )

    assert collector_worker._capture_final_close(slot, observed) is snapshot
    assert captured["as_of"] == minute
    assert captured["payload"]["risk_data"]["sector_flow_trajectory"] == exact_defense
    assert (
        captured["payload"]["risk_data"]["offense_sector_flow_trajectory"]
        == exact_offense
    )


def test_recovery_rematerializes_final_close_and_rebinds_ledger(monkeypatch) -> None:
    trade_date = datetime(2026, 8, 26, tzinfo=SHANGHAI).date()
    observed = datetime(2026, 8, 26, 16, 5, tzinfo=SHANGHAI)
    snapshot = object()
    record = {
        "trade_date": trade_date.isoformat(),
        "minute_bucket": f"{trade_date.isoformat()}T15:00:00+08:00",
        "record_kind": "accepted_real",
        "snapshot_id": "mw-rematerialized-close",
        "payload_digest": "a" * 64,
        "updated_at": observed.isoformat(),
    }
    calls: list[tuple[str, object]] = []

    class History:
        def record(self, received):
            calls.append(("record", received))
            return {"action": "updated", "payload_digest": "a" * 64}

        def get_collection_records(self, received_date):
            calls.append(("records", received_date))
            return [record]

    class Ledger:
        def reconcile_history_record(self, received):
            calls.append(("reconcile", received))
            return {"action": "imported"}

    monkeypatch.setattr(
        collector_worker,
        "_capture_final_close",
        lambda slot, received_at: calls.append(
            ("capture", (slot.minute_bucket, received_at))
        )
        or snapshot,
    )

    result = collector_worker._rematerialize_final_close(
        trade_date,
        observed,
        history=History(),
        ledger=Ledger(),
    )

    assert calls[0] == (
        "capture",
        (datetime(2026, 8, 26, 15, 0, tzinfo=SHANGHAI), observed),
    )
    assert calls[1:] == [
        ("record", snapshot),
        ("records", trade_date),
        ("reconcile", record),
    ]
    assert result == {
        "action": "updated",
        "source_snapshot_revision": "a" * 64,
    }


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
        lambda *, reuse_existing: pool_calls.append(
            (observed.date(), reuse_existing)
        )
        or {"pool_revision": "d" * 64, "pool_total": 12},
    )

    collector_worker._run_post_close_resonance_loop(
        stop_event,
        clock=lambda: observed,
        check_interval_seconds=0,
    )

    assert calls == [observed.date()]
    assert pool_calls == [(observed.date(), False)]


def test_intraday_loop_generates_one_batch_for_the_five_minute_bucket(monkeypatch) -> None:
    stop_event = threading.Event()
    observed = datetime(2026, 8, 26, 10, 37, tzinfo=SHANGHAI)
    calls = []
    pool_calls = []
    portfolio_calls = []
    monkeypatch.setattr(
        collector_worker,
        "_refresh_manual_portfolio_market",
        lambda: portfolio_calls.append(observed)
        or {
            "portfolio_revision": "7" * 64,
            "snapshot_revision": "8" * 64,
            "item_count": 2,
            "alert_count": 0,
        },
    )

    def generate(*, reuse_existing):
        assert reuse_existing is True
        calls.append(observed)
        stop_event.set()
        return {"resonance_revision": "b" * 64, "entry_count": 8}

    monkeypatch.setattr(collector_worker, "_generate_latest_resonance", generate)
    monkeypatch.setattr(
        collector_worker,
        "_generate_latest_limit_up_pool",
        lambda *, reuse_existing: pool_calls.append(
            (observed, reuse_existing)
        )
        or {"pool_revision": "e" * 64, "pool_total": 12},
    )

    collector_worker._run_post_close_resonance_loop(
        stop_event,
        clock=lambda: observed,
        check_interval_seconds=0,
    )

    assert calls == [observed]
    assert pool_calls == [(observed, True)]
    assert portfolio_calls == [observed]


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
        lambda *, reuse_existing: pool_calls.append(
            (stop_event.index, reuse_existing)
        )
        or {"pool_revision": "f" * 64, "pool_total": 12},
    )

    collector_worker._run_post_close_resonance_loop(
        stop_event,
        clock=lambda: observations[min(stop_event.index, len(observations) - 1)],
        check_interval_seconds=0,
    )

    assert calls == [0, 2]
    assert pool_calls == [(0, True), (2, True)]


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

    def generate_pool(*, reuse_existing):
        pool_calls.append(reuse_existing)
        stop_event.set()
        return {"pool_revision": "1" * 64, "pool_total": 3}

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

    assert pool_calls == [True]
    assert resonance_calls == []


def test_midday_materializes_the_last_open_snapshot_once(monkeypatch) -> None:
    class TwoIterationStopEvent:
        index = 0

        def is_set(self):
            return self.index >= 2

        def wait(self, _seconds):
            self.index += 1
            return self.is_set()

    stop_event = TwoIterationStopEvent()
    observations = (
        datetime(2026, 8, 26, 11, 30, tzinfo=SHANGHAI),
        datetime(2026, 8, 26, 11, 31, tzinfo=SHANGHAI),
    )
    pool_calls = []

    monkeypatch.setattr(
        collector_worker,
        "_generate_latest_limit_up_pool",
        lambda *, reuse_existing: pool_calls.append(reuse_existing)
        or {
            "source_snapshot_revision": "a" * 64,
            "pool_revision": "b" * 64,
            "pool_total": 12,
        },
    )

    collector_worker._run_post_close_resonance_loop(
        stop_event,
        clock=lambda: observations[min(stop_event.index, 1)],
        check_interval_seconds=0,
    )

    assert pool_calls == [True]


def test_limit_sentiment_runs_once_only_after_1610(monkeypatch) -> None:
    class TwoIterationStopEvent:
        index = 0

        def is_set(self):
            return self.index >= 2

        def wait(self, _seconds):
            self.index += 1
            return self.is_set()

    observations = (
        datetime(2026, 8, 26, 16, 9, tzinfo=SHANGHAI),
        datetime(2026, 8, 26, 16, 10, tzinfo=SHANGHAI),
    )
    stop_event = TwoIterationStopEvent()
    sentiment_calls = []
    monkeypatch.setattr(
        collector_worker,
        "_generate_latest_resonance",
        lambda *, reuse_existing: {
            "resonance_revision": "a" * 64,
            "entry_count": 1,
        },
    )
    monkeypatch.setattr(
        collector_worker,
        "_generate_latest_limit_up_pool",
        lambda *, reuse_existing: {
            "pool_revision": "b" * 64,
            "pool_total": 1,
        },
    )
    monkeypatch.setattr(
        collector_worker,
        "_generate_latest_limit_sentiment",
        lambda: sentiment_calls.append(stop_event.index)
        or {
            "source_revision": "c" * 64,
            "limit_up_count": 81,
            "broken_count": 19,
        },
    )

    collector_worker._run_post_close_resonance_loop(
        stop_event,
        clock=lambda: observations[min(stop_event.index, 1)],
        check_interval_seconds=0,
    )

    assert sentiment_calls == [1]


def test_review_announcements_refresh_once_in_morning_and_evening(monkeypatch) -> None:
    class TwoIterationStopEvent:
        index = 0

        def is_set(self):
            return self.index >= 2

        def wait(self, _seconds):
            self.index += 1
            return self.is_set()

    observations = (
        datetime(2026, 8, 26, 8, 0, tzinfo=SHANGHAI),
        datetime(2026, 8, 26, 21, 10, tzinfo=SHANGHAI),
    )
    stop_event = TwoIterationStopEvent()
    announcement_calls = []
    monkeypatch.setattr(
        collector_worker,
        "_generate_latest_resonance",
        lambda *, reuse_existing: {"resonance_revision": "a" * 64, "entry_count": 1},
    )
    monkeypatch.setattr(
        collector_worker,
        "_generate_latest_limit_up_pool",
        lambda *, reuse_existing: {"pool_revision": "b" * 64, "pool_total": 1},
    )
    monkeypatch.setattr(
        collector_worker,
        "_generate_latest_limit_sentiment",
        lambda: {"source_revision": "c" * 64, "limit_up_count": 1, "broken_count": 0},
    )
    monkeypatch.setattr(
        collector_worker,
        "_generate_latest_review_announcements",
        lambda: announcement_calls.append(stop_event.index)
        or {
            "source_revision": "d" * 64,
            "candidate_count": 3,
            "announcement_count": 2,
            "rematerialize_job_id": "fixture",
        },
    )

    collector_worker._run_post_close_resonance_loop(
        stop_event,
        clock=lambda: observations[min(stop_event.index, 1)],
        check_interval_seconds=0,
    )

    assert announcement_calls == [0, 1]
