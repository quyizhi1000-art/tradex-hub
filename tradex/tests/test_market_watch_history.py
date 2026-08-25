from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from tradex.market_calendar import CalendarDayStatus, calendar_day_status
from tradex.market_watch import AlertV1, MarketWatchSnapshotV1, build_market_watch_snapshot
from tradex.market_watch.history import (
    DEFAULT_CONFIG_VERSION,
    ENV_DB_PATH,
    HISTORY_CONTRACT,
    HISTORY_SCHEMA_VERSION,
    MarketWatchHistoryStore,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _snapshot(
    observed_at: datetime,
    *,
    snapshot_id: str,
    sequence: int,
    alert_code: str | None = None,
) -> MarketWatchSnapshotV1:
    specs = (
        ("broad_market", "000001.SH", "上证指数", 3400.0, 0.4),
        ("large_cap", "000300.SH", "沪深300", 4100.0, 0.5),
        ("small_cap", "000852.SH", "中证1000", 6800.0, 0.8),
        ("growth", "399006.SZ", "创业板指", 2250.0, 0.9),
    )
    market = {
        "timestamp": observed_at.isoformat(),
        "provider_as_of": observed_at.isoformat(),
        "market_state": {"phase": "trading", "is_open": True},
        "quality": "accepted",
        "indices": [
            {
                "role": role,
                "instrument_id": instrument_id,
                "name": name,
                "available": True,
                "level": level,
                "change_pct": change,
                "provider_as_of": observed_at.isoformat(),
                "quality": "accepted",
            }
            for role, instrument_id, name, level, change in specs
        ],
        "market_turnover": {
            "available": True,
            "today_date": observed_at.date().isoformat(),
            "previous_date": "2026-08-21",
            "as_of": observed_at.strftime("%H:%M"),
            "today_amount": 110_000_000_000.0,
            "previous_same_time_amount": 100_000_000_000.0,
        },
    }
    risk = {
        "timestamp": observed_at.isoformat(),
        "breadth": {
            "up_count": 3000,
            "down_count": 1800,
            "flat_count": 100,
            "unclassified_count": 100,
            "total_count": 5000,
            "provider_as_of": observed_at.isoformat(),
            "quality": "accepted",
        },
        "rotation": {
            "sectors": [
                {
                    "sector_key": "industry:securities",
                    "name": "证券",
                    "tags": ["attack"],
                    "change_pct": 1.5,
                    "breadth_ratio": 0.66,
                    "main_net_inflow_cny": 5_000_000_000.0,
                    "provider_as_of": observed_at.isoformat(),
                }
            ]
        },
    }
    built = build_market_watch_snapshot(
        market,
        risk,
        as_of=observed_at,
        sequence=sequence,
        snapshot_id=snapshot_id,
    )
    if alert_code is None:
        return built
    alert = AlertV1(
        code=alert_code,
        severity="stop" if alert_code.endswith("stop") else "caution",
        title=f"提醒 {alert_code}",
        message=f"事件 {alert_code} 已确认。",
        dedupe_key=f"market_watch:{alert_code}",
    )
    payload = built.model_dump(mode="python")
    payload["alerts"] = (alert,)
    return MarketWatchSnapshotV1.model_validate(payload)


def test_record_replays_the_complete_strict_payload_as_json_safe(tmp_path: Path) -> None:
    observed_at = datetime(2026, 8, 24, 10, 30, 8, tzinfo=SHANGHAI)
    snapshot = _snapshot(observed_at, snapshot_id="mw-1", sequence=1)
    store = MarketWatchHistoryStore(tmp_path / "history.sqlite3")
    try:
        result = store.record(snapshot)
        timeline = store.get_timeline("2026-08-24")

        assert result["action"] == "inserted"
        assert result["history_contract"] == HISTORY_CONTRACT
        assert result["history_schema_version"] == HISTORY_SCHEMA_VERSION
        assert result["config_version"] == DEFAULT_CONFIG_VERSION
        assert result["minute_bucket"] == "2026-08-24T10:30:00+08:00"
        assert result["payload_bytes"] > 0
        assert len(result["payload_digest"]) == 64
        assert len(timeline) == 1
        assert timeline[0]["payload"] == snapshot.model_dump(mode="json")
        assert timeline[0]["history_schema_version"] == HISTORY_SCHEMA_VERSION
        assert timeline[0]["config_version"] == DEFAULT_CONFIG_VERSION
        json.dumps(timeline, ensure_ascii=False, allow_nan=False)
    finally:
        store.close()


def test_same_minute_update_keeps_old_alerts_and_adds_new_events(tmp_path: Path) -> None:
    first = _snapshot(
        datetime(2026, 8, 24, 10, 30, 5, tzinfo=SHANGHAI),
        snapshot_id="mw-first",
        sequence=1,
        alert_code="breadth_warning",
    )
    second = _snapshot(
        datetime(2026, 8, 24, 10, 30, 50, tzinfo=SHANGHAI),
        snapshot_id="mw-second",
        sequence=2,
        alert_code="risk_stop",
    )
    store = MarketWatchHistoryStore(tmp_path / "upsert.sqlite3")
    try:
        assert store.record(first)["alerts_added"] == 1
        updated = store.record(second)
        repeated = store.record(second)

        assert updated["action"] == "updated"
        assert updated["alerts_added"] == 1
        assert repeated["action"] == "unchanged"
        assert repeated["alerts_added"] == 0

        timeline = store.get_timeline("2026-08-24")
        alerts = store.get_alerts("2026-08-24")
        assert len(timeline) == 1
        assert timeline[0]["payload"]["snapshot_id"] == "mw-second"
        assert [item["code"] for item in alerts] == [
            "breadth_warning",
            "risk_stop",
        ]
        assert alerts[0]["snapshot_id"] == "mw-first"
        assert alerts[1]["snapshot_id"] == "mw-second"
        assert alerts[0]["alert"]["dedupe_key"] == "market_watch:breadth_warning"
    finally:
        store.close()


def test_invalid_noncanonical_mapping_is_rejected_without_writing(tmp_path: Path) -> None:
    snapshot = _snapshot(
        datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI),
        snapshot_id="mw-valid",
        sequence=1,
    )
    invalid = {**snapshot.model_dump(mode="json"), "provider": "must-not-leak"}
    store = MarketWatchHistoryStore(tmp_path / "strict.sqlite3")
    try:
        with pytest.raises(ValidationError):
            store.record(invalid)
        assert store.list_dates() == []
    finally:
        store.close()


def test_lists_dates_and_limits_recent_rows_in_chronological_order(tmp_path: Path) -> None:
    store = MarketWatchHistoryStore(tmp_path / "dates.sqlite3")
    try:
        for day, hour, minute, sequence in (
            (24, 10, 30, 1),
            (24, 10, 31, 2),
            (24, 10, 32, 3),
            (25, 10, 30, 4),
        ):
            observed_at = datetime(2026, 8, day, hour, minute, tzinfo=SHANGHAI)
            store.record(
                _snapshot(
                    observed_at,
                    snapshot_id=f"mw-{sequence}",
                    sequence=sequence,
                )
            )

        dates = store.list_dates(limit=1)
        latest = store.get_timeline("2026-08-24", limit=2)
        assert [item["trade_date"] for item in dates] == ["2026-08-25"]
        assert dates[0]["snapshot_count"] == 1
        assert [item["payload"]["snapshot_id"] for item in latest] == [
            "mw-2",
            "mw-3",
        ]
        assert [item["minute_bucket"] for item in latest] == sorted(
            item["minute_bucket"] for item in latest
        )
        matched = store.find_latest_snapshot(
            lambda payload: payload["snapshot_id"] == "mw-2"
        )
        assert matched is not None
        assert matched["payload"]["snapshot_id"] == "mw-2"
    finally:
        store.close()


def test_retention_prunes_complete_old_dates_including_alerts(tmp_path: Path) -> None:
    store = MarketWatchHistoryStore(
        tmp_path / "retention.sqlite3",
        retention_trade_days=2,
    )
    try:
        for day in (24, 25, 26):
            observed_at = datetime(2026, 8, day, 10, 30, tzinfo=SHANGHAI)
            store.record(
                _snapshot(
                    observed_at,
                    snapshot_id=f"mw-{day}",
                    sequence=day,
                    alert_code=f"warning_{day}",
                )
            )

        assert [item["trade_date"] for item in store.list_dates()] == [
            "2026-08-26",
            "2026-08-25",
        ]
        assert store.get_timeline("2026-08-24") == []
        assert store.get_alerts("2026-08-24") == []
        assert len(store.get_alerts("2026-08-25")) == 1
    finally:
        store.close()


def test_env_path_persists_across_reopen_and_close_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "configured" / "market-watch.sqlite3"
    monkeypatch.setenv(ENV_DB_PATH, str(db_path))
    snapshot = _snapshot(
        datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI),
        snapshot_id="mw-persisted",
        sequence=1,
    )

    first = MarketWatchHistoryStore()
    assert Path(first.db_path) == db_path.resolve()
    first.record(snapshot)
    first.close()
    first.close()

    second = MarketWatchHistoryStore()
    try:
        assert second.get_timeline("2026-08-24")[0]["payload"]["snapshot_id"] == "mw-persisted"
    finally:
        second.close()

    with pytest.raises(RuntimeError, match="closed"):
        second.list_dates()
    with pytest.raises(RuntimeError, match="closed"):
        second.get_timeline("2026-02-16")
    with pytest.raises(RuntimeError, match="closed"):
        second.get_alerts("2026-02-16")
    with pytest.raises(RuntimeError, match="closed"):
        second.record(
            _snapshot(
                datetime(2026, 2, 16, 10, 0, tzinfo=SHANGHAI),
                snapshot_id="mw-closed-holiday",
                sequence=2,
            )
        )


def test_config_versions_have_independent_snapshots_and_alert_events(tmp_path: Path) -> None:
    db_path = tmp_path / "versions.sqlite3"
    snapshot = _snapshot(
        datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI),
        snapshot_id="mw-versioned",
        sequence=1,
        alert_code="versioned_warning",
    )
    first = MarketWatchHistoryStore(db_path, config_version="policy.v1")
    second = MarketWatchHistoryStore(db_path, config_version="policy.v2")
    try:
        assert first.record(snapshot)["alerts_added"] == 1
        assert second.record(snapshot)["alerts_added"] == 1
        assert len(first.get_timeline("2026-08-24")) == 1
        assert len(second.get_timeline("2026-08-24")) == 1
        assert first.get_alerts("2026-08-24")[0]["config_version"] == "policy.v1"
        assert second.get_alerts("2026-08-24")[0]["config_version"] == "policy.v2"
    finally:
        first.close()
        second.close()


def test_non_trading_day_snapshot_is_not_persisted(tmp_path: Path) -> None:
    holiday = _snapshot(
        datetime(2026, 2, 16, 10, 0, tzinfo=SHANGHAI),
        snapshot_id="mw-holiday",
        sequence=1,
        alert_code="holiday_warning",
    )
    assert holiday.market_state.phase.value == "non_trading"

    with MarketWatchHistoryStore(tmp_path / "holiday.sqlite3") as store:
        result = store.record(holiday)

        assert result["action"] == "skipped"
        assert result["reason"] == "non_trading_day"
        assert store.list_dates() == []
        assert store.get_timeline("2026-02-16") == []
        assert store.get_alerts("2026-02-16") == []


def test_legacy_non_trading_pollution_is_hidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tradex.market_watch.history as history_module

    holiday = _snapshot(
        datetime(2026, 2, 16, 10, 0, tzinfo=SHANGHAI),
        snapshot_id="mw-legacy-holiday",
        sequence=1,
        alert_code="legacy_holiday_warning",
    )
    with MarketWatchHistoryStore(tmp_path / "legacy-holiday.sqlite3") as store:
        monkeypatch.setattr(
            history_module,
            "calendar_day_status",
            lambda _value: CalendarDayStatus.VERIFIED_TRADING_DAY,
        )
        assert store.record(holiday)["action"] == "inserted"
        monkeypatch.setattr(history_module, "calendar_day_status", calendar_day_status)

        assert store.list_dates() == []
        assert store.get_timeline("2026-02-16") == []
        assert store.get_alerts("2026-02-16") == []


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_query_limits_must_be_positive_integers(tmp_path: Path, limit: object) -> None:
    store = MarketWatchHistoryStore(tmp_path / "limits.sqlite3")
    try:
        with pytest.raises(ValueError):
            store.get_timeline("2026-08-24", limit=limit)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            store.get_alerts("2026-08-24", limit=limit)  # type: ignore[arg-type]
    finally:
        store.close()
