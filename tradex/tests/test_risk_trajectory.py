"""SQLite trajectory persistence and confirmation tests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tradex.dashboard.risk_trajectory import RiskTrajectoryStore


TRADE_DATE = "2026-08-19"
VERSION = "risk-appetite-v1.0"


@pytest.fixture
def store(tmp_path: Path):
    trajectory = RiskTrajectoryStore(tmp_path / "trajectory.sqlite3")
    yield trajectory
    trajectory.close()


def _record(
    store: RiskTrajectoryStore,
    minute: str,
    state: str,
    *,
    trade_date: str = TRADE_DATE,
    version: str = VERSION,
    payload_hash: str | None = None,
    watermark: str | None = None,
    phase: str = "trading",
    fresh: bool = True,
    segment: str | None = None,
) -> dict:
    return store.record_snapshot(
        trade_date=trade_date,
        minute_bucket=f"{trade_date}T{minute}:00+08:00",
        config_version=version,
        raw_axes={"market_participation": state},
        metrics={"breadth_ratio": {"weak": 0.3, "medium": 0.5, "strong": 0.7}[state]},
        provider_as_of={"market": watermark or f"{trade_date}T{minute}:00+08:00"},
        payload_hash=payload_hash or f"hash-{trade_date}-{version}-{minute}-{state}",
        market_phase=phase,
        received_at=f"{trade_date}T{minute}:05+08:00",
        fresh=fresh,
        session_segment=segment,
        headline={"emotion": state, "structure": "测试结构", "reasons": ["仅描述当前状态。"]},
    )


def test_same_minute_is_inserted_once_and_never_advances_pending(store):
    first = _record(store, "10:00", "strong", payload_hash="first")
    duplicate = _record(store, "10:00", "strong", payload_hash="changed")

    assert first["inserted"] is True
    assert first["current"]["axes"]["market_participation"]["pending"]["samples"] == 1
    assert duplicate["inserted"] is False
    assert duplicate["reason"] == "duplicate_minute"
    assert len(store.get_series(TRADE_DATE, VERSION)) == 1
    assert duplicate["current"]["axes"]["market_participation"]["pending"]["samples"] == 1


def test_duplicate_payload_and_provider_watermark_do_not_confirm(store):
    _record(store, "10:00", "strong", payload_hash="same", watermark="w0")
    duplicate_hash = _record(store, "10:01", "strong", payload_hash="same", watermark="w1")
    accepted = _record(store, "10:02", "medium", payload_hash="new", watermark="w1")
    duplicate_watermark = _record(store, "10:03", "strong", payload_hash="newer", watermark="w1")

    assert duplicate_hash["reason"] == "duplicate_payload"
    assert duplicate_hash["inserted"] is False
    assert accepted["inserted"] is True
    assert duplicate_watermark["reason"] == "duplicate_watermark"
    assert duplicate_watermark["inserted"] is False
    assert duplicate_watermark["current"]["axes"]["market_participation"]["confirmed"] == "unknown"

    restarted_pending = _record(store, "10:03", "strong", payload_hash="h3", watermark="w3")
    confirmed = _record(store, "10:04", "strong", payload_hash="h4", watermark="w4")
    assert restarted_pending["current"]["axes"]["market_participation"]["pending"]["samples"] == 1
    assert confirmed["current"]["axes"]["market_participation"]["confirmed"] == "strong"


def test_two_fresh_different_minutes_confirm_the_same_candidate(store):
    first = _record(store, "10:00", "medium")
    second = _record(store, "10:01", "medium")

    assert first["current"]["dynamics"] == "collecting"
    assert first["current"]["axes"]["market_participation"]["confirmed"] == "unknown"
    assert second["current"]["dynamics"] == "ready"
    assert second["current"]["last_confirmed_at"].endswith("10:01:00+08:00")
    assert second["current"]["axes"]["market_participation"] == {
        "raw": "medium",
        "confirmed": "medium",
        "value": 0,
        "direction": "flat",
        "path": ["medium"],
        "pending": None,
        "summary": "近10分钟维持中。",
    }


def test_confirmed_state_can_jump_directly_from_weak_to_strong(store):
    _record(store, "10:00", "weak")
    _record(store, "10:01", "weak")
    _record(store, "10:02", "strong")
    result = _record(store, "10:03", "strong")

    axis = result["current"]["axes"]["market_participation"]
    assert axis["confirmed"] == "strong"
    assert axis["path"] == ["weak", "strong"]
    assert axis["direction"] == "strengthening"
    assert axis["summary"] == "近10分钟由弱转为强。"


def test_gap_over_120_seconds_and_session_change_clear_pending(store):
    _record(store, "10:00", "strong")
    after_gap = _record(store, "10:03", "strong")
    assert after_gap["current"]["axes"]["market_participation"]["confirmed"] == "unknown"
    assert after_gap["current"]["axes"]["market_participation"]["pending"]["samples"] == 1

    confirmed = _record(store, "10:04", "strong")
    assert confirmed["current"]["axes"]["market_participation"]["confirmed"] == "strong"

    _record(store, "11:29", "weak", segment="am")
    afternoon = _record(store, "13:00", "weak", segment="pm")
    assert afternoon["current"]["axes"]["market_participation"]["confirmed"] == "strong"
    assert afternoon["current"]["axes"]["market_participation"]["pending"]["samples"] == 1


def test_opening_is_stored_but_lunch_and_close_are_not(store):
    opening = _record(store, "09:40", "strong", phase="opening_observation")
    assert opening["inserted"] is True
    assert opening["reason"] == "opening_observation"
    assert opening["current"]["axes"]["market_participation"]["pending"] is None

    first_regular = _record(store, "09:45", "strong")
    second_regular = _record(store, "09:46", "strong")
    assert first_regular["current"]["axes"]["market_participation"]["confirmed"] == "unknown"
    assert second_regular["current"]["axes"]["market_participation"]["confirmed"] == "strong"

    before_pause = len(store.get_series(TRADE_DATE, VERSION))
    lunch = _record(store, "11:45", "strong", phase="midday_break")
    closed = _record(store, "15:01", "strong", phase="closed")
    assert lunch == {
        "inserted": False,
        "reason": "paused",
        "current": lunch["current"],
    }
    assert lunch["current"]["dynamics"] == "paused"
    assert closed["inserted"] is False
    assert closed["reason"] == "closed"
    assert closed["current"]["dynamics"] == "closed"
    assert len(store.get_series(TRADE_DATE, VERSION)) == before_pause


def test_stale_sample_is_not_recorded_and_clears_pending(store):
    _record(store, "10:00", "strong")
    stale = _record(store, "10:01", "strong", fresh=False)
    after_stale = _record(store, "10:02", "strong")

    assert stale["inserted"] is False
    assert stale["reason"] == "stale"
    assert stale["current"]["dynamics"] == "stale"
    assert len(stale["current"]["series"]) == 1
    assert after_stale["current"]["axes"]["market_participation"]["confirmed"] == "unknown"
    assert after_stale["current"]["axes"]["market_participation"]["pending"]["samples"] == 1


def test_restart_restores_confirmed_state_but_not_pending(tmp_path: Path):
    db_path = tmp_path / "restart.sqlite3"
    first_store = RiskTrajectoryStore(db_path)
    _record(first_store, "10:00", "strong")
    _record(first_store, "10:01", "strong")
    _record(first_store, "10:02", "weak")
    first_store.close()

    resumed = RiskTrajectoryStore(db_path)
    try:
        current = resumed.get_current(TRADE_DATE, VERSION)
        assert current["dynamics"] == "ready"
        assert current["axes"]["market_participation"]["confirmed"] == "strong"
        assert current["axes"]["market_participation"]["pending"] is None

        one_after_restart = _record(resumed, "10:03", "weak")
        assert one_after_restart["current"]["axes"]["market_participation"]["confirmed"] == "strong"
        confirmed = _record(resumed, "10:04", "weak")
        assert confirmed["current"]["axes"]["market_participation"]["confirmed"] == "weak"
    finally:
        resumed.close()


def test_trade_date_and_configuration_are_isolated_and_report_reset(store):
    _record(store, "10:00", "strong")
    _record(store, "10:01", "strong")

    next_day = _record(
        store,
        "10:00",
        "weak",
        trade_date="2026-08-20",
    )
    assert next_day["current"]["dynamics"] == "reset"
    assert next_day["current"]["axes"]["market_participation"]["confirmed"] == "unknown"
    assert len(store.get_series(TRADE_DATE, VERSION)) == 2

    new_config = _record(
        store,
        "10:01",
        "weak",
        trade_date="2026-08-20",
        version="risk-appetite-v2.0",
    )
    assert new_config["current"]["dynamics"] == "reset"
    assert new_config["current"]["config_version"] == "risk-appetite-v2.0"
    assert new_config["current"]["axes"]["market_participation"]["confirmed"] == "unknown"


def test_near_ten_minute_path_direction_pending_and_summary(store):
    sequence = ["weak", "weak", "medium", "medium", "strong", "strong"]
    for offset, state in enumerate(sequence):
        _record(store, f"10:0{offset}", state)

    current = store.get_current(TRADE_DATE, VERSION)
    axis = current["axes"]["market_participation"]
    assert axis["path"] == ["weak", "medium", "strong"]
    assert axis["direction"] == "strengthening"
    assert axis["pending"] is None
    assert axis["summary"] == "近10分钟由弱转为强。"
    assert current["headline_raw"] == {
        "emotion": "strong",
        "structure": "测试结构",
        "reasons": ["仅描述当前状态。"],
    }


def test_direction_follows_latest_confirmed_transition_not_window_net_change(store):
    sequence = ["weak", "weak", "strong", "strong", "medium", "medium"]
    for offset, state in enumerate(sequence):
        _record(store, f"10:0{offset}", state)

    axis = store.get_current(TRADE_DATE, VERSION)["axes"]["market_participation"]
    assert axis["path"] == ["weak", "strong", "medium"]
    assert axis["direction"] == "weakening"
    assert axis["summary"] == "近10分钟出现往返，最近由强转为中。"


def test_series_stores_audit_fields_and_is_capped_at_256(tmp_path: Path):
    trajectory = RiskTrajectoryStore(tmp_path / "cap.sqlite3", max_points=256)
    try:
        start = datetime.fromisoformat(f"{TRADE_DATE}T09:00:00+08:00")
        for offset in range(260):
            point = start + timedelta(minutes=offset)
            trajectory.record_snapshot(
                trade_date=TRADE_DATE,
                minute_bucket=point,
                config_version=VERSION,
                raw_axes={"market_participation": "medium"},
                metrics={"sequence": offset},
                provider_as_of={"market": point.isoformat()},
                payload_hash=f"hash-{offset}",
                market_phase="trading",
                received_at=point + timedelta(seconds=5),
                headline={"sequence": offset},
            )

        series = trajectory.get_series(TRADE_DATE, VERSION)
        assert len(series) == 256
        assert series[0]["metrics"] == {"sequence": 4}
        assert series[-1]["metrics"] == {"sequence": 259}
        assert series[-1]["provider_as_of"] == {"market": (start + timedelta(minutes=259)).isoformat()}
        assert series[-1]["payload_hash"] == "hash-259"
        assert series[-1]["market_phase"] == "trading"
        assert series[-1]["received_at"].endswith("13:19:05+08:00")
    finally:
        trajectory.close()


def test_thread_safety_keeps_concurrent_same_minute_unique(store):
    def write(index: int) -> bool:
        result = _record(store, "10:00", "strong", payload_hash=f"parallel-{index}")
        return result["inserted"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        inserted = list(executor.map(write, range(20)))

    assert inserted.count(True) == 1
    assert len(store.get_series(TRADE_DATE, VERSION)) == 1
