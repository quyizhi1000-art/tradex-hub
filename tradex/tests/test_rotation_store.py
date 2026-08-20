"""Rotation radar persistence tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tradex.dashboard.rotation_store import RotationRadarStore


SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = "2026-08-19"
START = datetime(2026, 8, 19, 10, 0, tzinfo=SHANGHAI)


def _board(
    code: str,
    name: str,
    minute: datetime,
    *,
    change: float,
    breadth: float = 0.5,
    flow: float = 0.0,
    provider: datetime | None = None,
) -> dict:
    up = round(100 * breadth)
    return {
        "板块代码": code,
        "板块名称": name,
        "涨跌幅": change,
        "上涨家数": up,
        "下跌家数": 100 - up,
        "主力净流入": flow * 100_000_000,
        "主力净流入-占比": flow,
        "主力净流入排名": int(50 - flow),
        "更新时间": (provider or minute).isoformat(timespec="seconds"),
    }


def _records(
    minute: datetime,
    *,
    target_change: float = -3.0,
    target_breadth: float = 0.3,
    target_flow: float = 0.0,
    count: int = 8,
) -> tuple[list[dict], list[dict]]:
    changes = [-2.0 + index * 0.75 for index in range(max(1, count - 1))]
    industry = [
        _board(f"I{index:04d}", f"行业背景{index}", minute, change=change)
        for index, change in enumerate(changes)
    ]
    industry.append(_board(
        "TARGET",
        "半导体材料",
        minute,
        change=target_change,
        breadth=target_breadth,
        flow=target_flow,
    ))
    concept = [
        _board(f"C{index:04d}", f"概念背景{index}", minute, change=change)
        for index, change in enumerate(changes)
    ]
    return industry, concept


def _record(
    store: RotationRadarStore,
    minute: datetime,
    *,
    target_change: float = -3.0,
    target_breadth: float = 0.3,
    target_flow: float = 0.0,
    trade_date: str = TRADE_DATE,
    phase: str = "trading",
    count: int = 8,
) -> dict:
    industry, concept = _records(
        minute,
        target_change=target_change,
        target_breadth=target_breadth,
        target_flow=target_flow,
        count=count,
    )
    return store.record_snapshot(
        trade_date=trade_date,
        minute_bucket=minute,
        industry_records=industry,
        concept_records=concept,
        sources={"industry": "push2", "concept": "push2"},
        market_phase=phase,
        received_at=minute + timedelta(seconds=5),
    )


def test_same_minute_and_same_provider_digest_are_not_inserted(tmp_path: Path):
    store = RotationRadarStore(tmp_path / "rotation.sqlite3")
    try:
        first = _record(store, START)
        same_minute = _record(store, START, target_change=5)

        industry, concept = _records(START)
        same_provider = store.record_snapshot(
            trade_date=TRADE_DATE,
            minute_bucket=START + timedelta(minutes=1),
            industry_records=industry,
            concept_records=concept,
            sources={"industry": "push2", "concept": "push2"},
        )

        assert first["inserted"] is True
        assert same_minute["reason"] == "duplicate_minute"
        assert same_provider["reason"] == "duplicate_provider_digest"
        assert len(store.get_snapshot_stats(TRADE_DATE)) == 1
    finally:
        store.close()


def test_restart_replays_confirmed_lifecycle(tmp_path: Path):
    path = tmp_path / "restart.sqlite3"
    store = RotationRadarStore(path)
    for offset in range(5):
        _record(store, START + timedelta(minutes=offset))
    _record(store, START + timedelta(minutes=5), target_change=1.5, target_breadth=0.52)
    _record(store, START + timedelta(minutes=6), target_change=1.6, target_breadth=0.53)
    _record(store, START + timedelta(minutes=7), target_change=5, target_breadth=0.8, target_flow=10)
    current = _record(store, START + timedelta(minutes=8), target_change=5.2, target_breadth=0.82, target_flow=11)["current"]
    assert current["attacking"][0]["board_code"] == "TARGET"
    store.close()

    resumed = RotationRadarStore(path)
    try:
        restored = resumed.get_current(TRADE_DATE)
        assert restored["attacking"][0]["board_code"] == "TARGET"
        assert restored["storage"]["replayed_points"] == 9
    finally:
        resumed.close()


def test_midday_does_not_store_or_advance(tmp_path: Path):
    store = RotationRadarStore(tmp_path / "lunch.sqlite3")
    try:
        _record(store, START)
        lunch = _record(
            store,
            datetime(2026, 8, 19, 11, 45, tzinfo=SHANGHAI),
            target_change=5,
            target_breadth=0.8,
            target_flow=10,
            phase="midday_break",
        )
        assert lunch["inserted"] is False
        assert lunch["reason"] == "paused"
        assert len(store.get_snapshot_stats(TRADE_DATE)) == 1
    finally:
        store.close()


def test_compression_is_small_and_metadata_is_auditable(tmp_path: Path):
    store = RotationRadarStore(tmp_path / "large.sqlite3")
    try:
        minute = START
        industry = [
            _board(f"I{index:04d}", f"行业板块{index}", minute, change=(index % 50) / 10)
            for index in range(500)
        ]
        concept = [
            _board(f"C{index:04d}", f"概念板块{index}", minute, change=(index % 50) / 10)
            for index in range(500)
        ]
        result = store.record_snapshot(
            trade_date=TRADE_DATE,
            minute_bucket=minute,
            industry_records=industry,
            concept_records=concept,
            sources={"industry": "push2", "concept": "push2"},
        )
        stats = store.get_snapshot_stats(TRADE_DATE)

        assert result["inserted"] is True
        assert stats[0]["board_count"] == 1000
        assert stats[0]["payload_bytes"] < 100_000
        assert stats[0]["schema_version"] == "rotation-radar-v1"
        assert len(stats[0]["provider_digest"]) == 64
    finally:
        store.close()


def test_per_day_cap_and_two_trade_day_retention(tmp_path: Path):
    store = RotationRadarStore(tmp_path / "cap.sqlite3", max_points=3, replay_steps=2)
    try:
        for day in (19, 20, 21):
            trade_date = f"2026-08-{day:02d}"
            base = datetime(2026, 8, day, 10, 0, tzinfo=SHANGHAI)
            for offset in range(5):
                _record(
                    store,
                    base + timedelta(minutes=offset),
                    trade_date=trade_date,
                    target_change=float(offset),
                )

        assert store.get_snapshot_stats("2026-08-19") == []
        assert len(store.get_snapshot_stats("2026-08-20")) == 3
        latest = store.get_snapshot_stats("2026-08-21")
        assert len(latest) == 3
        assert latest[0]["minute_bucket"].endswith("10:02:00+08:00")
        current = store.get_current("2026-08-21")
        assert current["storage"]["stored_points"] == 3
        assert current["storage"]["replayed_points"] == 2
    finally:
        store.close()


def test_opening_is_stored_but_does_not_confirm(tmp_path: Path):
    store = RotationRadarStore(tmp_path / "opening.sqlite3")
    try:
        for offset in range(3):
            minute = datetime(2026, 8, 19, 9, 30 + offset, tzinfo=SHANGHAI)
            _record(
                store,
                minute,
                target_change=5,
                target_breadth=0.8,
                target_flow=10,
                phase="opening_observation",
            )
        current = store.get_current(TRADE_DATE)
        assert current["status"] == "collecting"
        assert current["attacking"] == []
        assert current["events"] == []
        assert current["storage"]["stored_points"] == 3
    finally:
        store.close()
