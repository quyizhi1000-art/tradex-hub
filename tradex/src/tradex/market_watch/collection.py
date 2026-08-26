"""Continuity helpers for the provider-neutral market-watch collector.

Slow provider work must not silently erase replay minutes.  When one owned
refresh crosses a minute boundary, these helpers derive explicit stale
heartbeats from the last strict snapshot.  A later real sample may replace the
same minute; the heartbeat never claims fresh provider data.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from tradex.market_calendar import a_share_session

from .contracts import (
    ConclusionStrength,
    FreshnessStatus,
    GuardrailSeverity,
    MarketPhase,
    MarketRegime,
    MarketWatchSnapshotV1,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
_HEARTBEAT_NAMESPACE = UUID("a67c743e-77ab-4f09-b99c-a8d1ae3b327f")
_GAP_FLAG = "collection_gap_backfilled"


def _aware_shanghai(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return value.astimezone(SHANGHAI)


def _continuous_session_minute(value: datetime) -> bool:
    minute = value.hour * 60 + value.minute
    return (
        9 * 60 + 30 <= minute < 11 * 60 + 30
        or 13 * 60 <= minute < 15 * 60
    )


def _append_flag(values: tuple[str, ...], flag: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*values, flag)))


def _heartbeat_snapshot(
    anchor: MarketWatchSnapshotV1,
    minute: datetime,
) -> MarketWatchSnapshotV1:
    components = tuple(
        item.model_copy(update={
            "status": FreshnessStatus.STALE,
            "flags": _append_flag(item.flags, _GAP_FLAG),
        })
        for item in anchor.freshness.components
    )
    freshness = anchor.freshness.model_copy(update={
        "status": FreshnessStatus.STALE,
        "components": components,
        "flags": _append_flag(anchor.freshness.flags, _GAP_FLAG),
    })
    guardrail = anchor.guardrail.model_copy(update={
        "regime": MarketRegime.UNCERTAIN,
        "severity": GuardrailSeverity.STOP,
        "conclusion_strength": ConclusionStrength.ABSTAIN,
        "current_state": "采集刷新跨分钟，当前仅保留上一次已验证盘面。",
        "supporting_evidence": (),
        "counter_evidence": ("该分钟没有新的完整供应商采样。",),
        "behavioral_constraint": "数据已标记为陈旧，请暂停追单并等待采集恢复。",
    })
    heartbeat_id = uuid5(
        _HEARTBEAT_NAMESPACE,
        f"{anchor.snapshot_id}|{minute.isoformat(timespec='minutes')}",
    )
    session = a_share_session(minute)
    market_state = anchor.market_state.model_copy(update={
        "phase": MarketPhase(session.phase.value),
        "is_open": session.is_open,
        "trading_date": session.trading_date,
    })
    candidate = anchor.model_copy(update={
        "snapshot_id": f"mw-heartbeat:{heartbeat_id}",
        "as_of": minute,
        "market_state": market_state,
        "freshness": freshness,
        "guardrail": guardrail,
        "scenarios": (),
        "alerts": (),
    })
    return MarketWatchSnapshotV1.model_validate(candidate)


def build_collection_gap_snapshots(
    snapshot: MarketWatchSnapshotV1,
    *,
    started_at: datetime,
    completed_at: datetime,
) -> tuple[MarketWatchSnapshotV1, ...]:
    """Return honest stale rows for session minutes crossed by one refresh.

    The refresh's start minute belongs to the real captured snapshot.  Every
    later continuous-auction minute through completion receives a heartbeat;
    lunch, close endpoints, other dates and non-trading-day anchors are not
    invented here.  Persistence decides whether an existing real row wins.
    """

    canonical = MarketWatchSnapshotV1.model_validate(snapshot)
    started = _aware_shanghai(started_at, name="started_at")
    completed = _aware_shanghai(completed_at, name="completed_at")
    if completed < started:
        raise ValueError("completed_at cannot precede started_at")

    minute = started.replace(second=0, microsecond=0) + timedelta(minutes=1)
    final_minute = completed.replace(second=0, microsecond=0)
    snapshots: list[MarketWatchSnapshotV1] = []
    while minute <= final_minute:
        if (
            minute.date() == canonical.market_state.trading_date
            and _continuous_session_minute(minute)
        ):
            snapshots.append(_heartbeat_snapshot(canonical, minute))
        minute += timedelta(minutes=1)
    return tuple(snapshots)


__all__ = ["build_collection_gap_snapshots"]
