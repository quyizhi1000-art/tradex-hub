"""Focused tests for the sole market-watch refresh/cache owner."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Lock
from zoneinfo import ZoneInfo

import pytest

from tradex.market_watch.alerts import StructuredAlert
from tradex.market_watch.analysis import build_market_watch_snapshot
from tradex.market_watch.contracts import (
    BreadthSnapshotV1,
    ChangeSummaryV1,
    ComponentQuality,
    ConclusionStrength,
    FreshnessComponentV1,
    FreshnessStatus,
    FreshnessV1,
    GuardrailSeverity,
    GuardrailV1,
    IndexRole,
    IndexSnapshotV1,
    MarketPhase,
    MarketRegime,
    MarketStateV1,
    MarketWatchSnapshotV1,
    RotationSnapshotV1,
    TurnoverSnapshotV1,
)
from tradex.market_watch.service import (
    MarketWatchService,
    MarketWatchUnavailableError,
)


@dataclass(frozen=True)
class _FreshnessComponent:
    component: str = "overview"
    status: str = "fresh"
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Freshness:
    status: str = "fresh"
    components: tuple[_FreshnessComponent, ...] = (_FreshnessComponent(),)
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Guardrail:
    regime: str = "attack"
    severity: str = "caution"
    conclusion_strength: str = "moderate"
    current_state: str = "进攻方向占优。"
    supporting_evidence: tuple[str, ...] = ("进攻板块扩散。",)
    counter_evidence: tuple[str, ...] = ()
    behavioral_constraint: str = "不追高。"


@dataclass(frozen=True)
class _Snapshot:
    snapshot_id: str
    sequence: int
    as_of: datetime
    freshness: _Freshness = _Freshness()
    guardrail: _Guardrail = _Guardrail()
    scenarios: tuple[str, ...] = ("若量价继续配合，则维持当前结构。",)
    alerts: tuple[StructuredAlert, ...] = ()


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)
        self.tick = 100.0

    def wall(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.tick

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.tick += seconds


def _builder(payload):
    return _Snapshot(
        snapshot_id=payload["snapshot_id"],
        sequence=payload["sequence"],
        as_of=payload["as_of"],
        freshness=payload.get("freshness", _Freshness()),
        alerts=tuple(payload.get("alerts", ())),
    )


def _service(fetcher, clock: _Clock, **kwargs) -> MarketWatchService:
    return MarketWatchService(
        fetcher,
        _builder,
        clock=clock.wall,
        monotonic=clock.monotonic,
        snapshot_id_factory=lambda sequence: f"snapshot-{sequence}",
        market_open=lambda _now: True,
        **kwargs,
    )


def test_trading_ttl_hit_avoids_duplicate_upstream_call() -> None:
    clock = _Clock()
    calls = 0

    def fetcher():
        nonlocal calls
        calls += 1
        return {}

    service = _service(fetcher, clock)
    first = service.get()
    same = service.get()
    assert same is first
    assert calls == 1

    clock.advance(15.1)
    refreshed = service.get()
    assert calls == 2
    assert refreshed.sequence == 2
    assert refreshed.snapshot_id == "snapshot-2"


def test_off_session_uses_sixty_second_default_ttl() -> None:
    clock = _Clock()
    calls = 0

    def fetcher():
        nonlocal calls
        calls += 1
        return {}

    service = MarketWatchService(
        fetcher,
        _builder,
        clock=clock.wall,
        monotonic=clock.monotonic,
        snapshot_id_factory=lambda sequence: f"snapshot-{sequence}",
        market_open=lambda _now: False,
    )
    service.get()
    clock.advance(30)
    service.get()
    assert calls == 1
    clock.advance(30.1)
    service.get()
    assert calls == 2


def test_successful_stale_snapshot_still_uses_the_session_ttl() -> None:
    """Domain staleness is not the same thing as a failed refresh attempt."""

    clock = _Clock()
    calls = 0

    def fetcher():
        nonlocal calls
        calls += 1
        return {"freshness": _Freshness(status="stale")}

    service = MarketWatchService(
        fetcher,
        _builder,
        clock=clock.wall,
        monotonic=clock.monotonic,
        snapshot_id_factory=lambda sequence: f"snapshot-{sequence}",
        market_open=lambda _now: False,
    )

    first = service.get()
    clock.advance(5.1)
    assert service.get() is first
    assert calls == 1

    clock.advance(55)
    assert service.get().sequence == 2
    assert calls == 2


def test_concurrent_cache_miss_is_single_flight() -> None:
    clock = _Clock()
    started = Event()
    release = Event()
    count_lock = Lock()
    calls = 0

    def fetcher():
        nonlocal calls
        with count_lock:
            calls += 1
        started.set()
        assert release.wait(timeout=5)
        return {}

    service = _service(fetcher, clock)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(service.get) for _ in range(6)]
        assert started.wait(timeout=5)
        release.set()
        snapshots = [future.result(timeout=5) for future in futures]

    assert calls == 1
    assert {item.snapshot_id for item in snapshots} == {"snapshot-1"}
    assert {item.sequence for item in snapshots} == {1}


def test_stale_while_revalidate_returns_cached_snapshot_and_keeps_single_flight() -> None:
    clock = _Clock()
    started = Event()
    release = Event()
    calls = 0

    def fetcher():
        nonlocal calls
        calls += 1
        if calls == 2:
            started.set()
            assert release.wait(timeout=5)
        return {}

    service = _service(fetcher, clock)
    first = service.get()
    clock.advance(15.1)

    immediate = service.get(stale_while_revalidate=True)
    assert immediate is first
    assert started.wait(timeout=5)
    assert service.refreshing is True
    assert service.get(stale_while_revalidate=True) is first
    assert calls == 2

    release.set()
    refreshed = service.get()
    assert refreshed.sequence == 2
    assert service.refreshing is False
    assert calls == 2


def test_refresh_error_serves_traceable_stale_snapshot_without_strong_conclusion() -> None:
    clock = _Clock()
    calls = 0

    def fetcher():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("fixture timeout")
        return {}

    service = _service(fetcher, clock)
    original = service.get()
    clock.advance(15.1)
    stale = service.get()

    assert stale.snapshot_id == "snapshot-2"
    assert stale.sequence == 2
    assert stale.as_of == original.as_of
    assert stale.freshness.status == "stale"
    assert "refresh_error:TimeoutError" in stale.freshness.flags
    assert stale.freshness.components[0].status == "stale"
    assert stale.guardrail.regime == "uncertain"
    assert stale.guardrail.severity == "stop"
    assert stale.guardrail.conclusion_strength == "abstain"
    assert stale.scenarios == ()
    assert [alert.code for alert in stale.alerts] == ["data_stale"]

    # The failed refresh has its own short retry window instead of hammering
    # the provider on every desktop poll.
    assert service.get() is stale
    assert calls == 2


def test_forced_refresh_respects_minimum_interval() -> None:
    clock = _Clock()
    calls = 0

    def fetcher():
        nonlocal calls
        calls += 1
        return {}

    service = _service(fetcher, clock, force_min_interval_seconds=2)
    first = service.get()
    assert service.refresh() is first
    assert calls == 1
    clock.advance(2.1)
    assert service.refresh().sequence == 2
    assert calls == 2


def test_service_exposes_only_confirmed_alert_candidates() -> None:
    clock = _Clock()
    candidate = StructuredAlert(
        code="defense_takeover",
        severity="caution",
        title="防御方向占优",
        message="暂停追单，重新核对大盘与板块扩散。",
        dedupe_key="market_watch:regime",
    )

    service = _service(lambda: {"alerts": (candidate,)}, clock)
    first = service.get()
    assert first.alerts == ()
    clock.advance(2.1)
    second = service.refresh()
    assert second.alerts == (candidate,)
    clock.advance(2.1)
    third = service.refresh()
    assert third.alerts == ()


def test_initial_failure_raises_unavailable_with_structured_data_alert() -> None:
    clock = _Clock()

    def fetcher():
        raise ConnectionError("fixture offline")

    service = _service(fetcher, clock)
    with pytest.raises(MarketWatchUnavailableError) as captured:
        service.get()

    assert isinstance(captured.value.cause, ConnectionError)
    assert [alert.code for alert in captured.value.alerts] == ["data_unavailable"]
    assert service.last_snapshot is None


def test_stale_fallback_revalidates_the_real_market_watch_v1_contract() -> None:
    clock = _Clock()
    attempts = 0

    def fetcher():
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise TimeoutError("fixture timeout")
        return {}

    def canonical_builder(payload):
        as_of = payload["as_of"]
        specs = (
            (IndexRole.BROAD_MARKET, "000001.SH", "上证指数", 3400.0),
            (IndexRole.LARGE_CAP, "000300.SH", "沪深300", 4100.0),
            (IndexRole.SMALL_CAP, "000852.SH", "中证1000", 6800.0),
            (IndexRole.GROWTH, "399006.SZ", "创业板指", 2250.0),
        )
        return MarketWatchSnapshotV1(
            snapshot_id=payload["snapshot_id"],
            sequence=payload["sequence"],
            as_of=as_of,
            market_state=MarketStateV1(
                phase=MarketPhase.TRADING,
                is_open=True,
                trading_date=as_of.date(),
            ),
            freshness=FreshnessV1(
                status=FreshnessStatus.FRESH,
                components=tuple(
                    FreshnessComponentV1(
                        component=component,
                        status=FreshnessStatus.FRESH,
                        quality=ComponentQuality.ACCEPTED,
                        provider_as_of=as_of,
                        fetched_at=as_of,
                    )
                    for component in ("indices", "breadth", "turnover", "rotation")
                ),
            ),
            guardrail=GuardrailV1(
                regime=MarketRegime.MIXED,
                severity=GuardrailSeverity.CAUTION,
                conclusion_strength=ConclusionStrength.WEAK,
                current_state="指数与内部结构存在分化。",
                supporting_evidence=("大盘指数仍有支撑。",),
                counter_evidence=("小盘与成长偏弱。",),
                behavioral_constraint="暂停追单，重新核对盘面。",
            ),
            indices=tuple(
                IndexSnapshotV1(
                    role=role,
                    instrument_id=instrument_id,
                    name=name,
                    available=True,
                    level=level,
                    change_pct=0.1,
                    provider_as_of=as_of,
                    quality=ComponentQuality.ACCEPTED,
                )
                for role, instrument_id, name, level in specs
            ),
            breadth=BreadthSnapshotV1(
                available=False,
                quality=ComponentQuality.UNAVAILABLE,
                reason="fixture_not_supplied",
            ),
            turnover=TurnoverSnapshotV1(
                available=False,
                reason="fixture_not_supplied",
            ),
            rotation=RotationSnapshotV1(
                regime=MarketRegime.UNCERTAIN,
                sectors=(),
                summary="轮动数据未提供。",
            ),
            scenarios=(),
            change=ChangeSummaryV1(
                available=False,
                reason="previous_snapshot_unavailable",
            ),
        )

    service = MarketWatchService(
        fetcher,
        canonical_builder,
        clock=clock.wall,
        monotonic=clock.monotonic,
        market_open=lambda _now: True,
        snapshot_id_factory=lambda sequence: f"canonical-{sequence}",
    )
    service.get()
    clock.advance(15.1)
    stale = service.get()

    assert isinstance(stale, MarketWatchSnapshotV1)
    assert stale.freshness.status is FreshnessStatus.STALE
    assert stale.guardrail.regime is MarketRegime.UNCERTAIN
    assert stale.guardrail.severity is GuardrailSeverity.STOP
    assert stale.guardrail.conclusion_strength is ConclusionStrength.ABSTAIN
    assert [item.code for item in stale.alerts] == ["data_stale"]


def test_cold_start_recovers_latest_completed_turnover_as_stale() -> None:
    shanghai = ZoneInfo("Asia/Shanghai")
    previous_at = datetime(2026, 8, 24, 21, 37, tzinfo=shanghai)
    current_at = datetime(2026, 8, 25, 0, 4, tzinfo=shanghai)

    def snapshot(observed_at: datetime, *, turnover_available: bool, sequence: int):
        provider_at = previous_at if observed_at.date() > previous_at.date() else observed_at
        market = {
            "timestamp": observed_at.isoformat(),
            "provider_as_of": provider_at.isoformat(),
            "market_state": {"phase": "pre_open", "is_open": False},
            "indices": [
                {
                    "role": role,
                    "instrument_id": instrument_id,
                    "name": name,
                    "available": True,
                    "level": level,
                    "change_pct": 0.1,
                    "provider_as_of": provider_at.isoformat(),
                    "quality": "accepted",
                }
                for role, instrument_id, name, level in (
                    ("broad_market", "000001.SH", "上证指数", 3400.0),
                    ("large_cap", "000300.SH", "沪深300", 4100.0),
                    ("small_cap", "000852.SH", "中证1000", 6800.0),
                    ("growth", "399006.SZ", "创业板指", 2250.0),
                )
            ],
            "market_turnover": (
                {
                    "available": True,
                    "today_date": "2026-08-24",
                    "previous_date": "2026-08-21",
                    "as_of": "16:14",
                    "today_amount": 200.0,
                    "previous_same_time_amount": 180.0,
                }
                if turnover_available
                else {"available": False, "reason": "fixture unavailable"}
            ),
        }
        risk = {
            "timestamp": observed_at.isoformat(),
            "breadth": {
                "up_count": 2500,
                "down_count": 2300,
                "flat_count": 100,
                "unclassified_count": 0,
                "total_count": 4900,
            },
            "rotation": {"sectors": []},
        }
        return build_market_watch_snapshot(
            market,
            risk,
            as_of=observed_at,
            sequence=sequence,
            snapshot_id=f"canonical-{sequence}",
        )

    previous = snapshot(previous_at, turnover_available=True, sequence=41)
    candidate = snapshot(current_at, turnover_available=False, sequence=1)
    service = MarketWatchService(
        lambda: {},
        lambda _payload: candidate,
        initial_snapshot=previous,
        clock=lambda: current_at,
        monotonic=lambda: 100.0,
        market_open=lambda _now: False,
        snapshot_id_factory=lambda sequence: f"canonical-{sequence}",
    )

    recovered = service.get()

    assert recovered.sequence == 42
    assert recovered.turnover == previous.turnover.model_copy(update={"as_of": "15:00"})
    assert recovered.turnover.as_of == "15:00"
    turnover_freshness = next(
        item for item in recovered.freshness.components if item.component == "turnover"
    )
    assert turnover_freshness.status is FreshnessStatus.STALE
    assert turnover_freshness.quality is ComponentQuality.DEGRADED
    assert "last_good_turnover_recovered" in turnover_freshness.flags
    assert recovered.freshness.status is FreshnessStatus.STALE
    assert "turnover:stale" in recovered.guardrail.counter_evidence
    assert recovered.guardrail.regime is MarketRegime.UNCERTAIN
    assert recovered.guardrail.severity is GuardrailSeverity.STOP
    assert recovered.guardrail.conclusion_strength is ConclusionStrength.ABSTAIN
    assert "仅供回看" in recovered.guardrail.current_state
    assert recovered.guardrail.supporting_evidence == ()


def test_post_close_recovers_complete_same_session_turnover_as_degraded() -> None:
    shanghai = ZoneInfo("Asia/Shanghai")
    previous_at = datetime(2026, 8, 25, 20, 50, tzinfo=shanghai)
    current_at = datetime(2026, 8, 25, 21, 0, tzinfo=shanghai)
    provider_at = datetime(2026, 8, 25, 15, 0, tzinfo=shanghai)

    def snapshot(observed_at: datetime, *, turnover_available: bool, sequence: int):
        market = {
            "timestamp": observed_at.isoformat(),
            "provider_as_of": provider_at.isoformat(),
            "market_state": {"phase": "closed", "is_open": False},
            "indices": [
                {
                    "role": role,
                    "instrument_id": instrument_id,
                    "name": name,
                    "available": True,
                    "level": level,
                    "change_pct": 0.1,
                    "provider_as_of": provider_at.isoformat(),
                    "quality": "accepted",
                }
                for role, instrument_id, name, level in (
                    ("broad_market", "000001.SH", "上证指数", 3400.0),
                    ("large_cap", "000300.SH", "沪深300", 4100.0),
                    ("small_cap", "000852.SH", "中证1000", 6800.0),
                    ("growth", "399006.SZ", "创业板指", 2250.0),
                )
            ],
            "market_turnover": (
                {
                    "available": True,
                    "today_date": "2026-08-25",
                    "previous_date": "2026-08-24",
                    "as_of": "15:00",
                    "today_amount": 180.0,
                    "previous_same_time_amount": 200.0,
                }
                if turnover_available
                else {"available": False, "reason": "fixture unavailable"}
            ),
        }
        risk = {
            "timestamp": observed_at.isoformat(),
            "breadth": {
                "up_count": 3000,
                "down_count": 1800,
                "flat_count": 100,
                "unclassified_count": 0,
                "total_count": 4900,
            },
            "rotation": {"sectors": []},
        }
        return build_market_watch_snapshot(
            market,
            risk,
            as_of=observed_at,
            sequence=sequence,
            snapshot_id=f"canonical-{sequence}",
        )

    previous = snapshot(previous_at, turnover_available=True, sequence=41)
    candidate = snapshot(current_at, turnover_available=False, sequence=1)
    service = MarketWatchService(
        lambda: {},
        lambda _payload: candidate,
        initial_snapshot=previous,
        clock=lambda: current_at,
        monotonic=lambda: 100.0,
        market_open=lambda _now: False,
        snapshot_id_factory=lambda sequence: f"canonical-{sequence}",
    )

    recovered = service.get()

    assert recovered.turnover == previous.turnover
    turnover_freshness = next(
        item for item in recovered.freshness.components if item.component == "turnover"
    )
    assert turnover_freshness.status is FreshnessStatus.DEGRADED
    assert turnover_freshness.quality is ComponentQuality.DEGRADED
    assert "same_session_closed_turnover_recovered" in turnover_freshness.flags
    assert recovered.freshness.status is FreshnessStatus.DEGRADED
    assert recovered.guardrail.severity is not GuardrailSeverity.STOP
    assert recovered.guardrail.conclusion_strength is not ConclusionStrength.ABSTAIN
