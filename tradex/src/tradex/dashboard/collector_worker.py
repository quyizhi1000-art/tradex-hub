"""Portless managed runtime for continuous market-watch collection."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, Iterator
from zoneinfo import ZoneInfo

from tradex.market_watch.collection_store import MarketWatchCollectionStore
from tradex.market_watch.collector import MarketWatchCollector
from tradex.market_watch.contracts import FreshnessStatus, MarketPhase, MarketWatchSnapshotV1
from tradex.market_watch.session_schedule import (
    FINAL_CLOSE_TIME,
    OPENING_AUCTION_RESULT_TIME,
)
from tradex.market_watch.history import MarketWatchHistoryStore


SHANGHAI = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)


class HistoricalMarketWatchUnavailable(RuntimeError):
    """Raised when no exact canonical snapshot exists for a past minute."""


def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _capture_current(slot) -> MarketWatchSnapshotV1:
    """Run the existing provider-neutral feature owners for one current minute."""

    from tradex.dashboard import __main__ as dashboard_app
    from tradex.dashboard.risk_service import get_risk_appetite_data

    observed = _now()
    if observed.replace(second=0, microsecond=0) != slot.minute_bucket:
        raise HistoricalMarketWatchUnavailable(
            "current capture seam cannot fabricate a historical market-watch minute"
        )
    market_data = dashboard_app.get_market_data(force=True)
    if not dashboard_app._market_payload_is_current(market_data, observed):
        raise RuntimeError("market overview is not current for collector minute")
    get_risk_appetite_data(
        market_data,
        force=True,
        record_trajectory=True,
    )
    # The Web facade is a persistence-only projection.  The collector calls
    # the canonical service owner directly so changing the HTTP read path can
    # never turn collection into a Web request or make it depend on a page.
    snapshot = dashboard_app._get_market_watch_service().get(
        force=True,
        stale_while_revalidate=False,
    )
    return MarketWatchSnapshotV1.model_validate(snapshot)


def _provider_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(SHANGHAI)


def _market_payload_has_final_close(payload: dict, trade_date: date) -> bool:
    provider_as_of = _provider_time(payload.get("provider_as_of"))
    turnover = payload.get("market_turnover") or {}
    try:
        turnover_as_of = time.fromisoformat(str(turnover.get("as_of") or ""))
    except ValueError:
        return False
    return bool(
        provider_as_of is not None
        and provider_as_of.date() == trade_date
        and provider_as_of.time().replace(tzinfo=None) >= FINAL_CLOSE_TIME
        and turnover.get("available")
        and turnover.get("today_date") == trade_date.isoformat()
        and turnover_as_of >= FINAL_CLOSE_TIME
    )


def _provider_verified_final_breadth(
    trade_date: date,
    observed: datetime,
) -> tuple[dict, dict]:
    from tradex.data_gateway import (
        fetch_a_share_universe_snapshot,
        metadata_to_component_status,
    )

    universe = fetch_a_share_universe_snapshot(
        now=observed,
        trade_date=trade_date,
    )
    provider_as_of = universe.metadata.provider_as_of
    if (
        provider_as_of is None
        or provider_as_of.astimezone(SHANGHAI).date() != trade_date
        or provider_as_of.astimezone(SHANGHAI).time().replace(tzinfo=None)
        < FINAL_CLOSE_TIME
    ):
        raise HistoricalMarketWatchUnavailable(
            "A-share universe does not prove the target 15:00 close"
        )
    up = sum(item.change_pct > 0 for item in universe.quotes)
    down = sum(item.change_pct < 0 for item in universe.quotes)
    flat = len(universe.quotes) - up - down
    from tradex.data_gateway.quality import assess_universe_breadth

    quality, flags = assess_universe_breadth(
        quality=universe.metadata.quality.value,
        quality_flags=universe.metadata.quality_flags,
        excluded_row_count=universe.excluded_row_count,
    )
    breadth = {
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "unclassified_count": universe.excluded_row_count,
        "total_count": universe.provider_row_count,
        "provider_as_of": provider_as_of.isoformat(),
        "quality": quality.value,
        "quality_flags": list(flags),
    }
    status = metadata_to_component_status(universe.metadata)
    status.update(quality=quality.value, quality_flags=list(flags), partial=quality.value == "degraded")
    return breadth, status


def _capture_final_close(slot, observed: datetime) -> MarketWatchSnapshotV1:
    """Build one close snapshot from provider-proven final values."""

    from tradex.dashboard import __main__ as dashboard_app
    from tradex.dashboard.risk_service import (
        get_risk_appetite_data,
        get_rotation_radar_as_of,
    )

    minute = slot.minute_bucket.astimezone(SHANGHAI).replace(second=0, microsecond=0)
    if (
        minute.time().replace(tzinfo=None) != FINAL_CLOSE_TIME
        or observed <= minute
    ):
        raise HistoricalMarketWatchUnavailable(
            "final close recovery is available only after the target trading day closes"
        )
    market_data = dashboard_app.get_market_data(force=True)
    if not _market_payload_has_final_close(market_data, minute.date()):
        raise HistoricalMarketWatchUnavailable(
            "provider has not published a verified 15:00 market close"
        )
    risk_data = get_risk_appetite_data(
        market_data,
        force=True,
        record_trajectory=minute.date() == observed.date(),
    )
    exact_rotation = get_rotation_radar_as_of(minute)
    risk_data = dict(risk_data)
    if exact_rotation.get("offense"):
        risk_data["offense"] = exact_rotation["offense"]
    for field in (
        "sector_flow_trajectory",
        "offense_sector_flow_trajectory",
    ):
        trajectory = dict(exact_rotation.get(field) or {})
        trajectory_as_of = _provider_time(trajectory.get("as_of"))
        if (
            trajectory_as_of is None
            or trajectory_as_of.astimezone(SHANGHAI) < minute
            or not trajectory.get("sectors")
        ):
            raise HistoricalMarketWatchUnavailable(
                f"final close {field} does not reach the exact close"
            )
        risk_data[field] = trajectory
    breadth, breadth_status = _provider_verified_final_breadth(
        minute.date(),
        observed,
    )
    risk_data["breadth"] = breadth
    components = {
        str(name): dict(status)
        for name, status in (risk_data.get("components") or {}).items()
    }
    components["breadth"] = dict(breadth_status)
    components["market_breadth"] = dict(breadth_status)
    risk_data["components"] = components
    close_as_of = min(observed, minute + timedelta(seconds=59, microseconds=999999))
    snapshot = MarketWatchSnapshotV1.model_validate(
        dashboard_app._build_market_watch_snapshot(
            {
                "market_data": market_data,
                "risk_data": risk_data,
                "as_of": close_as_of,
            }
        )
    )
    components = {item.component: item for item in snapshot.freshness.components}
    required = {"indices", "breadth", "turnover", "rotation"}
    if snapshot.market_state.phase is not MarketPhase.CLOSED:
        raise HistoricalMarketWatchUnavailable("final close snapshot is not closed")
    if snapshot.freshness.status in {
        FreshnessStatus.STALE,
        FreshnessStatus.UNAVAILABLE,
    }:
        raise HistoricalMarketWatchUnavailable("final close snapshot is stale")
    if required - components.keys() or any(
        components[name].status in {FreshnessStatus.STALE, FreshnessStatus.UNAVAILABLE}
        for name in required
    ):
        raise HistoricalMarketWatchUnavailable(
            "final close snapshot has unavailable required components"
        )
    if (
        sum(item.available for item in snapshot.indices) < 2
        or not snapshot.breadth.available
        or not snapshot.turnover.available
        or not snapshot.rotation.sectors
    ):
        raise HistoricalMarketWatchUnavailable(
            "final close snapshot does not contain all required market facts"
        )
    return snapshot


def _repair_historical(
    slot,
    *,
    reconstructor=None,
    progress=None,
    observed: datetime | None = None,
) -> MarketWatchSnapshotV1:
    """Recover a verified final close or an exact same-day post-close minute.

    Exact sector-flow curves are repaired independently by the sampler-owned
    gateway path and persisted across restarts.  The aggregate market-watch
    minute is never reconstructed with current values, except for the same-day
    15:00 final result whose provider timestamps prove the target date and closing
    boundary.
    """

    observed = (observed or _now()).astimezone(SHANGHAI)
    minute = getattr(slot, "minute_bucket", None)
    if isinstance(minute, datetime):
        local = minute.astimezone(SHANGHAI)
        if local.time().replace(tzinfo=None) == FINAL_CLOSE_TIME:
            return _capture_final_close(slot, observed)
        if (
            local.time().replace(tzinfo=None) == OPENING_AUCTION_RESULT_TIME
            and reconstructor is not None
        ):
            callback = progress or (lambda _done, _total, _stage, _message=None: None)
            return reconstructor.reconstruct_opening_auction(local, callback)
        if reconstructor is not None and local.date() == observed.date():
            callback = progress or (lambda _done, _total, _stage, _message=None: None)
            return reconstructor.reconstruct(local, callback)
        if local.date() != observed.date():
            raise HistoricalMarketWatchUnavailable(
                "no exact historical market-watch aggregate is available: "
                "cross-day non-close reconstruction requires a separately licensed historical minute source"
            )
    raise HistoricalMarketWatchUnavailable(
        "no exact historical market-watch aggregate is available for this minute"
    )


def _rematerialize_final_close(
    trade_date: date,
    observed: datetime,
    *,
    history: MarketWatchHistoryStore,
    ledger: MarketWatchCollectionStore,
) -> dict[str, str]:
    """Publish a new close revision only after its canonical snapshot persists."""

    close_minute = datetime.combine(trade_date, FINAL_CLOSE_TIME).replace(
        tzinfo=SHANGHAI
    )
    snapshot = _capture_final_close(
        SimpleNamespace(minute_bucket=close_minute),
        observed,
    )
    persistence = dict(history.record(snapshot))
    action = str(persistence.get("action") or "")
    digest = str(persistence.get("payload_digest") or "")
    if action not in {"inserted", "updated", "unchanged"} or len(digest) != 64:
        raise RuntimeError("final close rematerialization was not persisted")
    close_iso = close_minute.isoformat(timespec="seconds")
    record = next(
        (
            item
            for item in history.get_collection_records(trade_date)
            if item.get("minute_bucket") == close_iso
            and item.get("record_kind") == "accepted_real"
            and item.get("payload_digest") == digest
        ),
        None,
    )
    if record is None:
        raise RuntimeError("persisted final close revision cannot be read back")
    reconciliation = ledger.reconcile_history_record(record)
    if reconciliation.get("action") not in {"imported", "unchanged"}:
        raise RuntimeError("final close revision was not rebound to the collection ledger")
    return {
        "action": action,
        "source_snapshot_revision": digest,
    }


def build_collector(
    *,
    ledger: MarketWatchCollectionStore,
    history: MarketWatchHistoryStore,
    clock=_now,
) -> MarketWatchCollector:
    from tradex.data_gateway.sector_flow import (
        finalize_sector_intraday_fund_flow_backfill,
        prepare_sector_intraday_fund_flow_backfill,
    )
    from tradex.dashboard.risk_service import get_rotation_radar_as_of
    from tradex.market_watch.reconstruction import SameDayPostCloseReconstructor
    from tradex.market_watch.recovery_source_cache import RecoverySourceMatrixStore
    from tradex.market_watch.trajectory_publication import IntradayTrajectoryPublisher

    reconstructor = SameDayPostCloseReconstructor(
        history=history,
        target_minutes=ledger.list_recovery_gap_minutes,
        rotation_loader=get_rotation_radar_as_of,
        clock=clock,
        rotation_curve_preparer=prepare_sector_intraday_fund_flow_backfill,
        source_cache=RecoverySourceMatrixStore(),
    )

    def prepare_daily_recovery(trade_date, observed, heartbeat):
        gap_minutes = tuple(
            minute
            for minute in ledger.list_recovery_gap_minutes(trade_date)
            if OPENING_AUCTION_RESULT_TIME < minute.time().replace(tzinfo=None) < FINAL_CLOSE_TIME
        )
        close_minute = datetime.combine(trade_date, FINAL_CLOSE_TIME, SHANGHAI)
        if observed.astimezone(SHANGHAI).date() == trade_date and observed >= close_minute:
            # Accepted aggregate minutes do not prove that every sector curve
            # reaches close. Finalize the tails even when the ledger has no gaps.
            required_through = close_minute
        elif gap_minutes:
            required_through = max(gap_minutes)
        else:
            return {"action": "skipped", "reason": "no_intraday_curve_gaps"}
        finalization = finalize_sector_intraday_fund_flow_backfill(
            trading_date=trade_date,
            required_through=required_through,
            now=observed,
            progress=lambda _completed, _total, _sector_key: heartbeat(),
        )
        if not finalization.get("complete"):
            raise HistoricalMarketWatchUnavailable(
                "final close rematerialization requires complete persisted sector curves"
            )
        heartbeat()
        publication = _rematerialize_final_close(
            trade_date,
            observed,
            history=history,
            ledger=ledger,
        )
        heartbeat()
        return {
            **finalization,
            "close_snapshot": publication,
        }

    def repair(slot):
        return _repair_historical(
            slot,
            reconstructor=reconstructor,
            observed=clock(),
        )

    def repair_with_progress(slot, progress):
        return _repair_historical(
            slot,
            reconstructor=reconstructor,
            progress=progress,
            observed=clock(),
        )

    def retained_history_records():
        # Reconcile every retained date before retrying gaps.  This prevents a
        # restarted ledger from refetching or overwriting a strict row that is
        # already durably present in history.
        for item in history.list_dates(limit=8):
            yield from history.get_collection_records(item["trade_date"])

    return MarketWatchCollector(
        store=ledger,
        capture_current=_capture_current,
        repair_historical=repair,
        repair_historical_with_progress=repair_with_progress,
        persist_snapshot=history.record,
        history_records=retained_history_records,
        prepare_daily_recovery=prepare_daily_recovery,
        publish_trajectories=IntradayTrajectoryPublisher(
            history=history, ledger=ledger, rotation_loader=get_rotation_radar_as_of,
        ),
        clock=clock,
    )


def _lock_path() -> Path:
    configured = os.environ.get("TRADEX_MARKET_WATCH_COLLECTOR_LOCK")
    path = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".tradex" / "market_watch_collector.lock"
    )
    resolved = path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


@contextmanager
def exclusive_worker_lock(path: Path | None = None) -> Iterator[BinaryIO]:
    """Prevent two collector processes from becoming concurrent owners."""

    target = (path or _lock_path()).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = target.open("a+b")
    try:
        try:
            handle.seek(0)
            if handle.read(1) == b"":
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, PermissionError) as exc:
            raise RuntimeError("another market-watch collector already owns the lock") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        yield handle
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def stop(_signum, _frame) -> None:
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)


def _status() -> int:
    now = _now()
    with MarketWatchCollectionStore(read_only=True) as ledger:
        envelope = ledger.read_envelope(as_of=now)
    print(
        json.dumps(
            envelope.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _mark_stopped() -> int:
    now = _now()
    from tradex.market_watch.collection_contracts import CollectorRuntimeState

    with MarketWatchCollectionStore() as ledger:
        ledger.update_runtime(CollectorRuntimeState.STOPPED, heartbeat_at=now)
    return 0


def _generate_latest_resonance(*, reuse_existing: bool = False) -> dict:
    """Generate and persist one batch for the latest accepted real row."""

    from tradex.market_watch.read_facade import MarketWatchReadFacade
    from tradex.market_watch.sector_resonance import build_sector_resonance_batch
    from tradex.market_watch.sector_resonance_store import SectorResonanceStore

    with (
        MarketWatchCollectionStore(read_only=True) as ledger,
        MarketWatchHistoryStore(read_only=True) as history,
    ):
        view = MarketWatchReadFacade(
            collection_reader=ledger,
            history_reader=history,
        ).read()
    if view.accepted is None:
        raise HistoricalMarketWatchUnavailable(
            "no accepted real snapshot is available for resonance backfill"
        )
    accepted = view.accepted
    if reuse_existing:
        with SectorResonanceStore() as store:
            existing = store.get_by_source_revision(
                accepted.pointer.source_snapshot_revision
            )
        if existing is not None:
            return {
                "action": "existing",
                "source_snapshot_revision": existing.source_snapshot_revision,
                "resonance_revision": existing.resonance_revision,
                "entry_count": len(existing.entries),
            }
    batch = build_sector_resonance_batch(
        accepted.source_payload,
        source_snapshot_revision=accepted.pointer.source_snapshot_revision,
        generated_at=_now(),
    )
    with SectorResonanceStore() as store:
        result = store.record(batch)
    result["entries"] = [
        {
            "direction": item.direction,
            "sector_key": item.sector_key,
            "sector_name": item.sector_name,
            "status": item.leader_snapshot.status,
            "leaders": [
                {
                    "instrument_id": leader.instrument_id,
                    "name": leader.name,
                    "speed_pct": leader.speed_pct,
                    "resonance_correlation": leader.resonance_correlation,
                    "directional_agreement_ratio": (
                        leader.directional_agreement_ratio
                    ),
                }
                for leader in item.leader_snapshot.leaders
            ],
        }
        for item in batch.entries
    ]
    return result


def _backfill_resonance() -> int:
    """CLI wrapper for one latest accepted-real resonance backfill."""

    result = _generate_latest_resonance()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _generate_latest_limit_up_pool(
    *,
    reuse_existing: bool = False,
) -> dict:
    """Persist live limit status joined to one stock relationship revision."""

    from tradex.market_watch.limit_up_pool import build_limit_up_pool
    from tradex.market_watch.limit_up_pool_store import LimitUpPoolStore
    from tradex.market_watch.read_facade import MarketWatchReadFacade
    from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader

    with (
        MarketWatchCollectionStore(read_only=True) as ledger,
        MarketWatchHistoryStore(read_only=True) as history,
    ):
        view = MarketWatchReadFacade(
            collection_reader=ledger,
            history_reader=history,
        ).read()
    if view.accepted is None:
        raise HistoricalMarketWatchUnavailable(
            "no accepted real snapshot is available for limit-up attribution"
        )
    accepted = view.accepted
    if reuse_existing:
        with InstrumentTaxonomyReader() as taxonomy_reader:
            taxonomy_status = taxonomy_reader.status()
        relationship_revision = taxonomy_status.catalog_revision if taxonomy_status else None
        from tradex.smart_sector_library.catalog import SmartSectorCatalog
        from tradex.market_watch.integrity import stable_sha256
        from datetime import date
        pool_date = date.fromisoformat(str(accepted.source_payload["as_of"])[:10])
        if pool_date >= date(2026, 9, 18):
            with SmartSectorCatalog(as_of=pool_date) as sectors:
                relationship_revision = stable_sha256({"business": relationship_revision,
                                                      "market": sectors.revision})
        with LimitUpPoolStore() as store:
            existing = store.get_by_source_revision(
                accepted.pointer.source_snapshot_revision
            )
        existing_is_reusable = (
            existing is not None
            and existing.relationship_catalog_revision
            == relationship_revision
            and all(
                item.board_count_basis
                in {"daily_closed_limit_up_history", "unavailable"}
                for item in existing.items
            )
        )
        if existing_is_reusable:
            return {
                "action": "existing",
                "source_snapshot_revision": existing.source_snapshot_revision,
                "pool_revision": existing.pool_revision,
                "pool_total": existing.pool_total,
            }
    pool = build_limit_up_pool(
        accepted.source_payload,
        source_snapshot_revision=accepted.pointer.source_snapshot_revision,
        generated_at=_now(),
    )
    with LimitUpPoolStore() as store:
        return store.record(pool)


def _backfill_limit_up_pool() -> int:
    result = _generate_latest_limit_up_pool()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _previous_verified_trading_date(value: date) -> date:
    from tradex.market_calendar import CalendarDayStatus, calendar_day_status

    candidate = value - timedelta(days=1)
    for _ in range(10):
        status = calendar_day_status(candidate)
        if status is CalendarDayStatus.VERIFIED_TRADING_DAY:
            return candidate
        if status is CalendarDayStatus.UNVERIFIED:
            raise HistoricalMarketWatchUnavailable(
                "previous trading day is not verified for limit sentiment"
            )
        candidate -= timedelta(days=1)
    raise HistoricalMarketWatchUnavailable(
        "previous trading day is outside the bounded calendar lookup"
    )


def _generate_latest_limit_sentiment() -> dict:
    """Collect and persist one provider-consistent post-close sentiment day."""

    from tradex.data_gateway.limit_sentiment import fetch_limit_sentiment_daily
    from tradex.market_watch.limit_sentiment_store import LimitSentimentStore
    from tradex.market_watch.read_facade import MarketWatchReadFacade

    with (
        MarketWatchCollectionStore(read_only=True) as ledger,
        MarketWatchHistoryStore(read_only=True) as history,
    ):
        view = MarketWatchReadFacade(
            collection_reader=ledger,
            history_reader=history,
        ).read()
    if view.accepted is None:
        raise HistoricalMarketWatchUnavailable(
            "no accepted real snapshot is available for limit sentiment"
        )
    trade_date = view.accepted.pointer.trade_date
    previous = _previous_verified_trading_date(trade_date)
    sentiment = fetch_limit_sentiment_daily(
        trade_date,
        previous,
        now=_now(),
    )
    with LimitSentimentStore() as store:
        return store.record(sentiment)


def _backfill_limit_sentiment() -> int:
    result = _generate_latest_limit_sentiment()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _generate_latest_review_announcements() -> dict:
    """Archive official announcements for the current review watchlist."""

    from tradex.analysis_jobs import (
        POST_MARKET_REVIEW,
        AnalysisJobCommandWriter,
        AnalysisJobReader,
    )
    from tradex.data_gateway.review_announcements import (
        fetch_review_official_announcements,
    )
    from tradex.market_watch.review_announcement_candidates import (
        NoReviewAnnouncementCandidates,
        manifest_from_review_artifact,
    )
    from tradex.market_watch.review_announcement_store import (
        ReviewOfficialAnnouncementStore,
    )

    with AnalysisJobReader() as reader:
        artifact = reader.get_artifact(
            POST_MARKET_REVIEW,
            scope_key="latest",
        )
    if artifact is None:
        raise HistoricalMarketWatchUnavailable(
            "no materialized review is available for official announcements"
        )
    try:
        manifest = manifest_from_review_artifact(artifact)
    except NoReviewAnnouncementCandidates:
        return {
            "action": "skipped_no_candidates",
            "source_revision": None,
            "candidate_count": 0,
            "announcement_count": 0,
            "rematerialize_job_id": None,
        }
    observed = _now()
    window_end = min(observed.date(), manifest.trade_date + timedelta(days=7))
    archive = fetch_review_official_announcements(
        manifest,
        window_end=window_end,
        now=observed,
    )
    with ReviewOfficialAnnouncementStore() as store:
        result = store.record(archive)
    with AnalysisJobCommandWriter() as writer:
        job = writer.enqueue(
            POST_MARKET_REVIEW,
            trade_date=manifest.trade_date,
            trigger="official-announcements-rematerialize",
            requested_at=manifest.review_generated_at,
        )
    return {
        **result,
        "candidate_manifest_revision": manifest.manifest_revision,
        "rematerialize_job_id": job["job_id"],
        "rematerialize_state": job["state"],
    }


def _refresh_manual_portfolio_market() -> dict:
    """Refresh the manual portfolio inside the existing managed collector."""

    from tradex.analysis_jobs import (
        MANUAL_PORTFOLIO_INTRADAY_ANALYSIS,
        MANUAL_PORTFOLIO_OUTLOOK,
        AnalysisJobCommandWriter,
        AnalysisJobReader,
        AnalysisStateUnavailable,
    )
    from tradex.market_calendar import TradingSessionPhase, a_share_session
    from tradex.manual_portfolio.market import refresh_manual_portfolio_market
    from tradex.manual_portfolio.readiness import (
        portfolio_outlook_readiness,
        portfolio_snapshot_has_final_close,
    )
    from tradex.manual_portfolio.store import ManualPortfolioStore

    with ManualPortfolioStore() as store:
        snapshot = refresh_manual_portfolio_market(store, now=_now())
        request = store.outlook_request(snapshot.portfolio_revision)
        generation = None
        session = a_share_session(snapshot.generated_at)
        waiting_request = request is not None and request["state"] == "waiting_for_market"
        waiting_ready = bool(
            waiting_request
            and portfolio_outlook_readiness(
                portfolio_revision=snapshot.portfolio_revision,
                enabled_count=snapshot.item_count,
                snapshot=snapshot,
                now=snapshot.generated_at,
                automatic_generation_requested=True,
            ).state == "ready"
        )
        automatic_scope = (
            f"date:{snapshot.trading_date}:portfolio:{snapshot.portfolio_revision}"
            if snapshot.trading_date is not None
            else None
        )
        automatic_due = bool(
            automatic_scope
            and snapshot.item_count
            and session.is_trading_day
            and session.phase is TradingSessionPhase.CLOSED
            and portfolio_snapshot_has_final_close(snapshot)
        )
        daily_state = "checking" if automatic_due else "not_applicable"
        if automatic_due:
            try:
                with AnalysisJobReader() as reader:
                    archived = reader.get_artifact(
                        MANUAL_PORTFOLIO_OUTLOOK,
                        scope_key=f"date:{snapshot.trading_date}",
                    )
                    existing_job = reader.latest_job(
                        MANUAL_PORTFOLIO_OUTLOOK,
                        scope_key=automatic_scope,
                    )
                if archived is not None:
                    daily_state = "available"
                elif existing_job is not None and existing_job.get("state") in {
                    "queued", "running", "succeeded"
                }:
                    daily_state = "scheduled"
                automatic_due = archived is None and (
                    existing_job is None
                    or existing_job.get("state") not in {"queued", "running", "succeeded"}
                )
            except AnalysisStateUnavailable:
                automatic_due = False
                daily_state = "unavailable"
        if waiting_ready or automatic_due:
            dispatched_at = _now()
            scope_key = (
                f"portfolio:{snapshot.portfolio_revision}"
                if waiting_ready
                else automatic_scope
            )
            with AnalysisJobCommandWriter() as writer:
                job = writer.enqueue(
                    MANUAL_PORTFOLIO_OUTLOOK,
                    trade_date=snapshot.trading_date or dispatched_at.date(),
                    trigger=(
                        "manual-after-market-refresh"
                        if waiting_ready
                        else "automatic-after-close"
                    ),
                    scope_key=scope_key,
                    requested_at=dispatched_at,
                )
            if waiting_ready:
                store.mark_outlook_request_dispatched(
                    snapshot.portfolio_revision,
                    job_id=job["job_id"],
                    dispatched_at=dispatched_at,
                )
            generation = {
                "job_id": job["job_id"],
                "state": job["state"],
            }
            if session.phase is TradingSessionPhase.CLOSED:
                daily_state = "scheduled"
        intraday_generation = None
        if session.is_open or (
            session.is_trading_day and session.phase is TradingSessionPhase.CLOSED
            and snapshot.trading_date == session.trading_date
            and snapshot.generated_at.astimezone(SHANGHAI).hour >= 15
        ):
            with AnalysisJobCommandWriter() as writer:
                intraday_job = writer.enqueue(
                    MANUAL_PORTFOLIO_INTRADAY_ANALYSIS,
                    trade_date=snapshot.trading_date or snapshot.generated_at.date(),
                    trigger="collector-market-refresh",
                    scope_key=f"snapshot:{snapshot.snapshot_revision}",
                    requested_at=snapshot.generated_at,
                )
            intraday_generation = {
                "job_id": intraday_job["job_id"],
                "state": intraday_job["state"],
            }
    return {
        "portfolio_revision": snapshot.portfolio_revision,
        "snapshot_revision": snapshot.snapshot_revision,
        "item_count": snapshot.item_count,
        "alert_count": len(snapshot.alerts),
        "outlook_generation": generation,
        "daily_outlook_state": daily_state,
        "intraday_analysis_generation": intraday_generation,
    }


def _manual_portfolio_outlook_request_waiting() -> bool:
    """Read whether the current portfolio has a deferred next-session request."""

    from tradex.manual_portfolio.store import ManualPortfolioReader, portfolio_revision

    with ManualPortfolioReader() as reader:
        entries = reader.list_entries()
        revision = portfolio_revision(entries)
        request = reader.outlook_request(revision)
    return request is not None and request["state"] == "waiting_for_market"


def _backfill_review_announcements() -> int:
    result = _generate_latest_review_announcements()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _final_close_pointer_revision(pointer: dict | None, trade_date: date) -> str | None:
    if not pointer or pointer.get("trade_date") != trade_date.isoformat():
        return None
    minute = _provider_time(pointer.get("minute_bucket"))
    if minute is None or minute.date() != trade_date or minute.time().replace(tzinfo=None) != FINAL_CLOSE_TIME:
        return None
    return pointer.get("source_snapshot_revision")


def _latest_final_close_revision(trade_date: date) -> str | None:
    try:
        with MarketWatchCollectionStore(read_only=True) as ledger:
            envelope = ledger.read_envelope(as_of=_now()).model_dump(mode="json")
    except Exception:
        logger.exception("verified closing pointer temporarily unavailable")
        return None
    return _final_close_pointer_revision(envelope.get("latest_accepted_real"), trade_date)


def _refresh_sector_catalog(*, schedule_backfill=True, now=None):
    from .sector_catalog_collector import refresh_sector_catalog

    return refresh_sector_catalog(schedule_backfill=schedule_backfill, now=now)


def _run_post_close_resonance_loop(
    stop_event: threading.Event,
    *,
    clock=_now,
    check_interval_seconds: float = 30.0,
) -> None:
    """Run one bounded backfill per verified trading day after the close."""

    from tradex.market_calendar import TradingSessionPhase, a_share_session

    completed_close_revisions = {}
    completed_resonance_buckets = set()
    completed_limit_up_buckets = set()
    completed_midday_limit_up_dates = set()
    completed_sentiment_dates = set()
    completed_announcement_buckets = set()
    completed_portfolio_buckets = set()
    completed_portfolio_close_dates = set()
    completed_macd_j_bucket = None
    while not stop_event.is_set():
        observed = clock()
        catalog_backlog = False
        try:
            catalog_result = _refresh_sector_catalog()
            catalog_backlog = bool(catalog_result.get("history_missing"))
        except Exception:
            logger.exception("sector catalog materialization failed; prior revision retained")
        session = a_share_session(observed)
        intraday_bucket = observed.replace(second=0, microsecond=0)
        local_time = observed.time().replace(tzinfo=None)
        midday_limit_up_due = (
            session.is_trading_day
            and session.phase is TradingSessionPhase.MIDDAY_BREAK
            and observed.date() not in completed_midday_limit_up_dates
        )
        limit_up_due = (
            session.is_open
            and intraday_bucket not in completed_limit_up_buckets
        ) or midday_limit_up_due
        resonance_due = (
            session.is_open
            and local_time >= time(9, 35)
            and intraday_bucket not in completed_resonance_buckets
        )
        after_close = (
            session.is_trading_day
            and session.phase is TradingSessionPhase.CLOSED
            and local_time >= time(15, 0)
        )
        close_revision = _latest_final_close_revision(observed.date()) if after_close else None
        post_close_due = bool(close_revision) and (
            completed_close_revisions.get(observed.date()) != close_revision
        )
        sentiment_due = (
            session.is_trading_day
            and session.phase is TradingSessionPhase.CLOSED
            and local_time >= time(16, 10)
            and observed.date() not in completed_sentiment_dates
        )
        announcement_bucket = None
        if session.is_trading_day and local_time >= time(21, 10):
            announcement_bucket = (observed.date(), "evening")
        elif local_time >= time(8, 0):
            announcement_bucket = (observed.date(), "morning")
        announcement_due = (
            announcement_bucket is not None
            and announcement_bucket not in completed_announcement_buckets
        )
        post_close_portfolio_due = (
            session.is_trading_day
            and session.phase is TradingSessionPhase.CLOSED
            and (
                observed.date() not in completed_portfolio_close_dates
                or _manual_portfolio_outlook_request_waiting()
            )
        )
        portfolio_due = (
            session.is_open
            and intraday_bucket not in completed_portfolio_buckets
        ) or post_close_portfolio_due
        from tradex.stock_selection.intraday_macd_j import refresh_watch, scan_slot
        macd_observed = clock()
        macd_session = a_share_session(macd_observed)
        macd_time = macd_observed.time().replace(tzinfo=None)
        macd_bucket = macd_observed.replace(second=0, microsecond=0)
        macd_j_due = macd_session.is_trading_day and (
            macd_session.is_open or scan_slot(macd_observed) is not None or time(9, 20) <= macd_time < time(9, 30)
        ) and completed_macd_j_bucket != macd_bucket
        if macd_j_due:
            # Use the existing auxiliary owner; never acquire from a page read.
            try:
                refresh_watch(now=macd_observed)
            except Exception:
                logger.exception("intraday MACD J page alert refresh failed")
            completed_macd_j_bucket = macd_bucket
        if limit_up_due:
            try:
                result = _generate_latest_limit_up_pool(
                    reuse_existing=True,
                )
                completed_limit_up_buckets.add(intraday_bucket)
                if time(11, 30) <= local_time < time(13, 0):
                    completed_midday_limit_up_dates.add(observed.date())
                logger.info(
                    "live limit-up catalog pool ready revision=%s total=%s",
                    result.get("pool_revision"),
                    result.get("pool_total"),
                )
            except Exception:
                logger.exception("live limit-up catalog pool refresh failed")
        if resonance_due:
            try:
                result = _generate_latest_resonance(reuse_existing=True)
                completed_resonance_buckets.add(intraday_bucket)
                logger.info(
                    "intraday sector resonance ready revision=%s entries=%s",
                    result.get("resonance_revision"),
                    result.get("entry_count"),
                )
            except Exception:
                logger.exception("intraday sector resonance backfill failed")
        if portfolio_due:
            try:
                portfolio_result = _refresh_manual_portfolio_market()
                completed_portfolio_buckets.add(intraday_bucket)
                if (
                    session.phase is TradingSessionPhase.CLOSED
                    and portfolio_result.get("daily_outlook_state") != "unavailable"
                ):
                    completed_portfolio_close_dates.add(observed.date())
                logger.info(
                    "manual portfolio market ready revision=%s items=%s alerts=%s",
                    portfolio_result.get("snapshot_revision"),
                    portfolio_result.get("item_count"),
                    portfolio_result.get("alert_count"),
                )
            except Exception:
                logger.exception("manual portfolio market refresh failed")
        if post_close_due:
            succeeded = True
            resonance_result = {}
            limit_up_result = {}
            try:
                resonance_result = _generate_latest_resonance(reuse_existing=False)
            except Exception:
                logger.exception("post-close sector resonance backfill failed")
                succeeded = False
            try:
                limit_up_result = _generate_latest_limit_up_pool(
                    reuse_existing=False,
                )
            except Exception:
                logger.exception("limit-up catalog pool backfill failed")
                succeeded = False
            if succeeded:
                # Close work only for the exact source both producers consumed.
                if (
                    resonance_result.get("source_snapshot_revision") == close_revision
                    and limit_up_result.get("source_snapshot_revision") == close_revision
                ):
                    completed_close_revisions[observed.date()] = close_revision
                logger.info(
                    "derived market-watch evidence ready resonance=%s entries=%s "
                    "limit_up_pool=%s total=%s phase=%s",
                    resonance_result.get("resonance_revision"),
                    resonance_result.get("entry_count"),
                    limit_up_result.get("pool_revision"),
                    limit_up_result.get("pool_total"),
                    session.phase.value,
                )
        if sentiment_due:
            try:
                sentiment_result = _generate_latest_limit_sentiment()
                completed_sentiment_dates.add(observed.date())
                logger.info(
                    "post-close limit sentiment ready revision=%s "
                    "limit_up=%s broken=%s",
                    sentiment_result.get("source_revision"),
                    sentiment_result.get("limit_up_count"),
                    sentiment_result.get("broken_count"),
                )
            except Exception:
                logger.exception("post-close limit sentiment collection failed")
        if announcement_due:
            try:
                announcement_result = _generate_latest_review_announcements()
                completed_announcement_buckets.add(announcement_bucket)
                logger.info(
                    "review official announcements ready revision=%s "
                    "candidates=%s announcements=%s rematerialize=%s",
                    announcement_result.get("source_revision"),
                    announcement_result.get("candidate_count"),
                    announcement_result.get("announcement_count"),
                    announcement_result.get("rematerialize_job_id"),
                )
            except Exception:
                logger.exception("review official announcement collection failed")
        try:
            from tradex.data_gateway.sector_flow import (
                schedule_requested_sector_intraday_fund_flow_repair,
            )

            schedule_requested_sector_intraday_fund_flow_repair(
                observed_at=clock(),
            )
        except Exception:
            logger.exception("intraday sector trajectory repair scheduling failed")
        # In market breaks the persisted backlog should drain, not wait thirty
        # seconds between every small batch. Trading capture keeps its idle windows.
        idle_backfill = session.is_trading_day and (
            session.phase is TradingSessionPhase.MIDDAY_BREAK or after_close)
        stop_event.wait(min(check_interval_seconds, 1.0) if catalog_backlog and idle_backfill else check_interval_seconds)


def _run_supervised_auxiliary_loop(stop_event, *, run_loop=None, retry_delay_seconds=5.0):
    """Restart the existing auxiliary owner if an uncaught cycle error escapes."""
    run_loop = run_loop or _run_post_close_resonance_loop
    while not stop_event.is_set():
        try:
            run_loop(stop_event)
            if stop_event.is_set():
                return
            logger.error("collector auxiliary loop exited unexpectedly; restarting")
        except Exception:
            logger.exception("collector auxiliary loop failed; resuming persisted recovery")
        if stop_event.wait(retry_delay_seconds):
            return


def _run_collector_cycle(
    stop_event: threading.Event,
    *,
    once: bool,
) -> None:
    """Open fresh persistence owners and run one collector lifecycle."""

    with (
        MarketWatchCollectionStore() as ledger,
        MarketWatchHistoryStore() as history,
    ):
        collector = build_collector(ledger=ledger, history=history)
        if once:
            result = collector.run_once()
            try:
                result["manual_portfolio"] = _refresh_manual_portfolio_market()
            except Exception as exc:  # noqa: BLE001 - keep the primary collector result
                result["manual_portfolio"] = {
                    "status": "unavailable",
                    "failure_code": type(exc).__name__,
                }
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return
        # Establish the managed heartbeat before large directory projections
        # compete for CPU/SQLite. Startup reconciliation remains authoritative.
        collector.start()
        resonance_stop_event = threading.Event()
        resonance_thread = threading.Thread(
            target=_run_supervised_auxiliary_loop,
            args=(resonance_stop_event,),
            name="sector-resonance-post-close",
            daemon=True,
        )
        resonance_thread.start()
        try:
            collector.run_forever(stop_event)
        finally:
            resonance_stop_event.set()
            resonance_thread.join(timeout=5.0)


def _run_supervised(
    stop_event: threading.Event,
    *,
    run_cycle=_run_collector_cycle,
    retry_delay_seconds: float = 5.0,
) -> None:
    """Keep the portless owner alive across transient store/runtime failures.

    Provider and canonical capture failures are already audited by
    :class:`MarketWatchCollector`.  This outer boundary covers failures before
    an attempt can be claimed (for example a transient SQLite open/lock error),
    reopening both stores on every retry instead of leaving a live Dashboard
    backed by a silently dead collector.
    """

    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds must not be negative")
    while not stop_event.is_set():
        try:
            run_cycle(stop_event, once=False)
            if stop_event.is_set():
                return
            logger.error("collector runtime exited unexpectedly; reopening persistence owners")
        except Exception:  # noqa: BLE001 - process boundary must self-heal
            logger.exception("collector runtime failed; reopening persistence owners")
        if stop_event.wait(retry_delay_seconds):
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tradex market-watch collector")
    parser.add_argument("--status", action="store_true", help="read ledger status only")
    parser.add_argument(
        "--mark-stopped",
        action="store_true",
        help="record a managed stop after the worker process exits",
    )
    parser.add_argument("--once", action="store_true", help="run one owned collection step")
    parser.add_argument("--materialize-sector-catalog", action="store_true",
                        help="materialize full observed directory from stored real snapshots")
    parser.add_argument(
        "--backfill-resonance",
        action="store_true",
        help="backtrack minute correlation for the latest accepted real snapshot",
    )
    parser.add_argument(
        "--backfill-limit-up-pool",
        action="store_true",
        help="match the latest accepted real limit-up pool to the relationship catalog",
    )
    parser.add_argument(
        "--backfill-limit-sentiment",
        action="store_true",
        help="collect the latest accepted trade day's limit sentiment",
    )
    parser.add_argument(
        "--backfill-review-announcements",
        action="store_true",
        help="collect official announcements for the latest review candidates",
    )
    args = parser.parse_args(argv)
    if args.status:
        return _status()
    if args.mark_stopped:
        return _mark_stopped()

    logging.basicConfig(
        level=os.environ.get("TRADEX_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    try:
        if args.materialize_sector_catalog:
            with exclusive_worker_lock():
                print(json.dumps(_refresh_sector_catalog(schedule_backfill=False), ensure_ascii=False))
            return 0
        if args.backfill_resonance:
            return _backfill_resonance()
        if args.backfill_limit_up_pool:
            return _backfill_limit_up_pool()
        if args.backfill_limit_sentiment:
            return _backfill_limit_sentiment()
        if args.backfill_review_announcements:
            return _backfill_review_announcements()
        with exclusive_worker_lock():
            if args.once:
                _run_collector_cycle(stop_event, once=True)
            else:
                _run_supervised(stop_event)
    except RuntimeError as exc:
        logger.error("collector stopped: %s", exc)
        return 1
    finally:
        try:
            from tradex.dashboard.risk_service import close_risk_trajectory_store

            close_risk_trajectory_store()
        except Exception:
            logger.exception("collector trajectory store close failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "HistoricalMarketWatchUnavailable",
    "build_collector",
    "exclusive_worker_lock",
    "main",
    "_generate_latest_limit_up_pool",
    "_generate_latest_limit_sentiment",
    "_generate_latest_review_announcements",
    "_refresh_manual_portfolio_market",
    "_manual_portfolio_outlook_request_waiting",
    "_run_post_close_resonance_loop",
]
