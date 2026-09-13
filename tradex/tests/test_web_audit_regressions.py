"""Behavioral regressions found by the desktop web audit."""

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tradex.analysis_jobs import AnalysisJobReader, AnalysisJobStore, MANUAL_PORTFOLIO_INTRADAY_ANALYSIS
from tradex.market_watch import analysis
from tradex.dashboard import collector_worker
from tradex.data_gateway import sector_flow

SH = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize("reader_type", [AnalysisJobStore, AnalysisJobReader])
def test_session_history_filters_before_limit_and_orders_time(tmp_path, reader_type):
    path = tmp_path / "jobs.sqlite3"
    with AnalysisJobStore(path) as store:
        for scope, day, revision, hour in [
            ("ff", "2026-09-04", "current", 15),
            ("ee", "2026-09-07", "old", 15),
            ("dd", "2026-09-07", "current", 9),
            ("01", "2026-09-07", "current", 14),
            ("02", "2026-09-07", "current", 13),
        ]:
            store.put_artifact(
                MANUAL_PORTFOLIO_INTRADAY_ANALYSIS,
                scope_key=f"snapshot:{scope}", source_revision="a" * 64,
                payload={"contract": "manual_portfolio_intraday_analysis.v1",
                         "source_trading_date": day, "portfolio_revision": revision},
                generated_at=datetime.fromisoformat(f"{day}T{hour:02}:00:00+08:00"),
            )
    with reader_type(path) as reader:
        records = reader.list_artifacts(
            MANUAL_PORTFOLIO_INTRADAY_ANALYSIS, scope_prefix="snapshot:", limit=2,
            source_trading_date="2026-09-07", portfolio_revision="current",
            order_by_generated_at=True,
        )
        assert [x["scope_key"] for x in records] == ["snapshot:01", "snapshot:02"]
        # Existing date/portfolio callers keep their lexical scope ordering.
        assert reader.list_artifacts(MANUAL_PORTFOLIO_INTRADAY_ANALYSIS,
                                     scope_prefix="snapshot:", limit=1)[0]["scope_key"] == "snapshot:ff"


def test_rotation_uses_trajectory_direction_without_overriding_explicit_tags():
    risk = {
        "rotation": {"sectors": [
            {"sector_key": "concept:cpo", "name": "CPO概念", "change_pct": 5, "breadth_ratio": .9},
            {"sector_key": "concept:retail", "name": "新零售", "change_pct": 2, "breadth_ratio": .8},
            {"sector_key": "explicit", "name": "黄金", "tags": ["safe_haven"]},
        ]},
        "offense_sector_flow_trajectory": {"sectors": [{"sector_key": "cpo", "name": "CPO概念"}]},
        "sector_flow_trajectory": {"sectors": [{"sector_key": "retail", "name": "新零售"}]},
    }
    rows = analysis._rotation_inputs(risk)
    assert analysis._sector_tags(rows[0]) == (analysis.SectorTag.ATTACK,)
    assert analysis._sector_tags(rows[1]) == (analysis.SectorTag.DEFENSE,)
    assert analysis._sector_tags(rows[2]) == (analysis.SectorTag.SAFE_HAVEN,)
    assert analysis.classify_rotation(rows).regime == analysis.MarketRegime.MIXED


def test_repair_can_finish_after_close_but_not_before_open(monkeypatch):
    from tradex import market_calendar
    phase = market_calendar.TradingSessionPhase
    monkeypatch.setattr(market_calendar, "a_share_session", lambda now: SimpleNamespace(
        is_trading_day=True, phase=phase.CLOSED))
    assert sector_flow._intraday_repair_window_open(datetime(2026, 9, 7, 15, 5, tzinfo=SH))
    assert not sector_flow._intraday_repair_window_open(datetime(2026, 9, 7, 8, 5, tzinfo=SH))


def test_final_pointer_requires_same_day_verified_close():
    pointer = {"trade_date": "2026-09-07", "minute_bucket": "2026-09-07T14:56:00+08:00",
               "source_snapshot_revision": "a" * 64}
    assert collector_worker._final_close_pointer_revision(pointer, date(2026, 9, 7)) is None
    pointer["minute_bucket"] = "2026-09-07T15:00:00+08:00"
    assert collector_worker._final_close_pointer_revision(pointer, date(2026, 9, 7)) == "a" * 64
    assert collector_worker._final_close_pointer_revision(pointer, date(2026, 9, 8)) is None


def test_unreadable_close_pointer_does_not_stop_other_collector_work(monkeypatch):
    def unavailable(**kwargs):
        raise OSError("database temporarily unreadable")
    monkeypatch.setattr(collector_worker, "MarketWatchCollectionStore", unavailable)
    assert collector_worker._latest_final_close_revision(date(2026, 9, 7)) is None


@pytest.mark.parametrize("flags,excluded,expected", [
    (("turnover_partial",), 0, "accepted"),
    (("turnover_partial", "provider_timestamp_missing"), 0, "degraded"),
    (("turnover_partial",), 1, "degraded"),
    ((), 0, "degraded"),
])
def test_breadth_projection_keeps_relevant_and_unknown_quality_warnings(flags, excluded, expected):
    from tradex.data_gateway.quality import assess_universe_breadth
    quality, projected = assess_universe_breadth(
        quality="degraded", quality_flags=flags, excluded_row_count=excluded)
    assert quality.value == expected
    assert "turnover_partial" not in projected
    if "provider_timestamp_missing" in flags:
        assert "provider_timestamp_missing" in projected


def test_close_derivation_waits_for_close_then_follows_revision_changes(monkeypatch):
    from tradex import market_calendar
    observed = datetime(2026, 9, 7, 15, 5, tzinfo=SH)
    revisions = [None, "a" * 64, "a" * 64, "b" * 64, "b" * 64]

    class Steps:
        index = 0
        def is_set(self):
            return self.index == len(revisions)
        def wait(self, seconds):
            self.index += 1
    steps = Steps()
    monkeypatch.setattr(market_calendar, "a_share_session", lambda now: SimpleNamespace(
        is_trading_day=True, is_open=False, trading_date=observed.date(),
        phase=market_calendar.TradingSessionPhase.CLOSED))
    monkeypatch.setattr(collector_worker, "_latest_final_close_revision", lambda day: revisions[steps.index])
    calls = []
    def derive(**kwargs):
        calls.append(revisions[steps.index])
        return {"source_snapshot_revision": revisions[steps.index]}
    monkeypatch.setattr(collector_worker, "_generate_latest_resonance", derive)
    monkeypatch.setattr(collector_worker, "_generate_latest_limit_up_pool", derive)
    monkeypatch.setattr(collector_worker, "_refresh_manual_portfolio_market", lambda: {})
    monkeypatch.setattr(collector_worker, "_manual_portfolio_outlook_request_waiting", lambda: False)
    monkeypatch.setattr(collector_worker, "_generate_latest_review_announcements", lambda: {})
    monkeypatch.setattr(sector_flow, "schedule_requested_sector_intraday_fund_flow_repair", lambda **kwargs: None)
    collector_worker._run_post_close_resonance_loop(steps, clock=lambda: observed, check_interval_seconds=0)
    assert calls == ["a" * 64, "a" * 64, "b" * 64, "b" * 64]


@pytest.mark.parametrize("custom,workers", [(False, 1), (True, 4)])
def test_default_resonance_respects_shared_queue_while_custom_fetchers_stay_parallel(monkeypatch, custom, workers):
    from tradex.market_watch import sector_resonance
    from tradex.market_watch.integrity import stable_sha256
    now = datetime(2026, 9, 7, 10, 0, tzinfo=SH)
    snapshot = analysis.build_market_watch_snapshot({"market_data": {}, "risk_data": {}, "as_of": now})
    budgets = []
    def entries(jobs, **kwargs):
        budgets.append(kwargs["max_workers"])
        return ()
    monkeypatch.setattr(sector_resonance, "_build_sector_resonance_entries_batched", entries)
    sector_resonance.build_sector_resonance_batch(
        snapshot, source_snapshot_revision=stable_sha256(snapshot.model_dump(mode="json")),
        generated_at=now, board_fetcher=(lambda *a, **k: None) if custom else None,
        minute_batch_fetcher=lambda *a, **k: {}, max_workers=4,
    )
    assert budgets == [workers]
