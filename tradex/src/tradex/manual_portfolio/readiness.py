"""Provider-neutral readiness for revision-bound portfolio outlooks."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from tradex.market_calendar import (
    CalendarDayStatus,
    TradingSessionPhase,
    a_share_session,
    calendar_day_status,
)

from .contracts import (
    ManualPortfolioMarketSnapshotV1,
    ManualPortfolioOutlookReadinessV1,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def portfolio_snapshot_has_final_close(
    snapshot: ManualPortfolioMarketSnapshotV1 | None,
) -> bool:
    if snapshot is None or snapshot.trading_date is None:
        return False
    final_items = 0
    for item in snapshot.items:
        if item.status == "unavailable":
            continue
        if not item.samples:
            return False
        latest = item.samples[-1].observed_at.astimezone(SHANGHAI)
        if latest.date() != snapshot.trading_date or latest.time().replace(
            tzinfo=None
        ) < time(15, 0):
            return False
        final_items += 1
    return final_items > 0


def _snapshot_is_ready_for_next_session(
    snapshot: ManualPortfolioMarketSnapshotV1 | None,
    *,
    now: datetime,
) -> bool:
    if not portfolio_snapshot_has_final_close(snapshot):
        return False
    observed = now.astimezone(SHANGHAI)
    session = a_share_session(observed)
    assert snapshot is not None and snapshot.trading_date is not None
    if session.phase is TradingSessionPhase.CLOSED:
        return snapshot.trading_date == observed.date()
    if session.phase in {
        TradingSessionPhase.PRE_OPEN,
        TradingSessionPhase.NON_TRADING,
    }:
        return snapshot.trading_date < observed.date()
    return False


def next_portfolio_collection_at(now: datetime) -> datetime | None:
    """Return the earliest verified session boundary when Collector may refresh."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("portfolio outlook readiness time must include a timezone")
    observed = now.astimezone(SHANGHAI)
    session = a_share_session(observed)
    if session.is_open:
        return observed
    if session.is_trading_day:
        if session.phase is TradingSessionPhase.CLOSED:
            # The completed session is already a valid source for next-session
            # analysis; Collector can service an explicit request immediately.
            return observed
        if session.phase is TradingSessionPhase.PRE_OPEN:
            return datetime.combine(observed.date(), time(9, 30), tzinfo=SHANGHAI)
        if session.phase is TradingSessionPhase.MIDDAY_BREAK:
            return datetime.combine(observed.date(), time(13, 0), tzinfo=SHANGHAI)
    candidate = observed.date() + timedelta(days=1)
    for _ in range(14):
        status = calendar_day_status(candidate)
        if status is CalendarDayStatus.VERIFIED_TRADING_DAY:
            return datetime.combine(candidate, time(9, 30), tzinfo=SHANGHAI)
        if status is CalendarDayStatus.UNVERIFIED:
            return None
        candidate += timedelta(days=1)
    return None


def portfolio_outlook_readiness(
    *,
    portfolio_revision: str,
    enabled_count: int,
    snapshot: ManualPortfolioMarketSnapshotV1 | None,
    now: datetime,
    automatic_generation_requested: bool = False,
) -> ManualPortfolioOutlookReadinessV1:
    matches = (
        snapshot is not None
        and snapshot.portfolio_revision == portfolio_revision
        and _snapshot_is_ready_for_next_session(snapshot, now=now)
    )
    state = "empty" if enabled_count == 0 else "ready" if matches else "waiting_for_market"
    return ManualPortfolioOutlookReadinessV1(
        state=state,
        portfolio_revision=portfolio_revision,
        market_portfolio_revision=snapshot.portfolio_revision if snapshot else None,
        market_generated_at=snapshot.generated_at if snapshot else None,
        next_collection_at=(
            next_portfolio_collection_at(now)
            if state == "waiting_for_market"
            else None
        ),
        automatic_generation_requested=automatic_generation_requested,
    )


__all__ = [
    "next_portfolio_collection_at",
    "portfolio_outlook_readiness",
    "portfolio_snapshot_has_final_close",
]
