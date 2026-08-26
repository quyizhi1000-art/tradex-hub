"""Alert confirmation tests use only deterministic in-memory snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from tradex.market_watch.alerts import MarketAlertEngine, StructuredAlert


@dataclass(frozen=True)
class _Freshness:
    status: str


@dataclass(frozen=True)
class _Snapshot:
    freshness: _Freshness
    alerts: tuple[StructuredAlert, ...] = ()


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _candidate(*, code: str = "risk_off", key: str = "market:regime") -> StructuredAlert:
    return StructuredAlert(
        code=code,
        severity="stop" if code == "risk_off" else "caution",
        title="盘面状态变化",
        message="暂停追单，重新核对盘面。",
        dedupe_key=key,
    )


def _snapshot(status: str = "fresh", *alerts: StructuredAlert) -> _Snapshot:
    return _Snapshot(freshness=_Freshness(status=status), alerts=tuple(alerts))


def test_market_alert_requires_two_consecutive_fresh_samples() -> None:
    engine = MarketAlertEngine()
    candidate = _candidate()

    assert engine.evaluate(_snapshot("fresh", candidate)) == ()
    assert engine.evaluate(_snapshot("fresh", candidate)) == (candidate,)
    assert engine.evaluate(_snapshot("fresh", candidate)) == ()


def test_stale_sample_interrupts_candidate_confirmation() -> None:
    engine = MarketAlertEngine()
    candidate = _candidate()

    assert engine.evaluate(_snapshot("fresh", candidate)) == ()
    stale_alerts = engine.evaluate(_snapshot("stale"))
    assert [item.code for item in stale_alerts] == ["data_stale"]
    assert engine.evaluate(_snapshot("fresh", candidate)) == ()
    assert engine.evaluate(_snapshot("fresh", candidate)) == (candidate,)


def test_stale_sample_clears_an_established_market_confirmation() -> None:
    engine = MarketAlertEngine(cooldown_seconds=0)
    candidate = _candidate()

    assert engine.evaluate(_snapshot("fresh", candidate)) == ()
    assert engine.evaluate(_snapshot("fresh", candidate)) == (candidate,)
    assert [item.code for item in engine.evaluate(_snapshot("stale"))] == [
        "data_stale"
    ]
    assert engine.evaluate(_snapshot("fresh", candidate)) == ()
    assert engine.evaluate(_snapshot("fresh", candidate)) == (candidate,)


def test_stable_key_deduplicates_and_cooldown_blocks_fast_state_flap() -> None:
    clock = _Clock()
    engine = MarketAlertEngine(clock=clock, cooldown_seconds=300)
    risk_off = _candidate(code="risk_off")
    mixed = _candidate(code="mixed")

    assert engine.evaluate(_snapshot("fresh", risk_off)) == ()
    assert engine.evaluate(_snapshot("fresh", risk_off)) == (risk_off,)
    assert engine.evaluate(_snapshot("fresh", risk_off)) == ()

    assert engine.evaluate(_snapshot("fresh", mixed)) == ()
    assert engine.evaluate(_snapshot("fresh", mixed)) == ()

    clock.advance(301)
    risk_off_again = _candidate(code="risk_off")
    assert engine.evaluate(_snapshot("fresh", risk_off_again)) == ()
    assert engine.evaluate(_snapshot("fresh", risk_off_again)) == (
        risk_off_again,
    )


def test_global_mute_suppresses_alerts_without_deferring_old_state() -> None:
    engine = MarketAlertEngine(muted=True)
    candidate = _candidate()

    assert engine.evaluate(_snapshot("fresh", candidate)) == ()
    assert engine.evaluate(_snapshot("fresh", candidate)) == ()
    engine.set_muted(False)
    assert engine.evaluate(_snapshot("fresh", candidate)) == ()


def test_data_risk_alert_is_immediate_and_uses_same_cooldown() -> None:
    clock = _Clock()
    engine = MarketAlertEngine(clock=clock, cooldown_seconds=300)

    first = engine.evaluate(_snapshot("unavailable"))
    assert [item.code for item in first] == ["data_unavailable"]
    assert engine.evaluate(_snapshot("unavailable")) == ()

    clock.advance(301)
    repeated = engine.evaluate(_snapshot("unavailable"))
    assert [item.code for item in repeated] == ["data_unavailable"]


def test_degraded_sample_emits_degraded_risk_and_clears_pending_candidate() -> None:
    engine = MarketAlertEngine()
    candidate = _candidate()

    assert engine.evaluate(_snapshot("fresh", candidate)) == ()
    alerts = engine.evaluate(_snapshot("degraded"))
    assert [item.code for item in alerts] == ["data_degraded"]
    assert engine.evaluate(_snapshot("fresh", candidate)) == ()


def test_degraded_global_snapshot_still_confirms_sector_move_candidates() -> None:
    engine = MarketAlertEngine()
    candidate = StructuredAlert(
        code="sector_move_up",
        severity="caution",
        title="半导体突然增强",
        message="5分钟板块涨幅变化+0.60个百分点。",
        dedupe_key="market_watch:sector_move:offense:semiconductor:strengthening:0_5",
        kind="sector_move",
    )

    first = engine.evaluate(_snapshot("degraded", candidate))
    assert [item.code for item in first] == ["data_degraded"]
    second = engine.evaluate(_snapshot("degraded", candidate))
    assert second == (candidate,)
