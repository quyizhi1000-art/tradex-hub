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
from typing import BinaryIO, Iterator
from zoneinfo import ZoneInfo

from tradex.market_watch.collection_store import MarketWatchCollectionStore
from tradex.market_watch.collector import MarketWatchCollector
from tradex.market_watch.contracts import FreshnessStatus, MarketPhase, MarketWatchSnapshotV1
from tradex.market_watch.session_schedule import FINAL_CLOSE_TIME
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
    breadth = {
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "unclassified_count": universe.excluded_row_count,
        "total_count": universe.provider_row_count,
        "provider_as_of": provider_as_of.isoformat(),
        "quality": universe.metadata.quality.value,
        "quality_flags": list(universe.metadata.quality_flags),
    }
    return breadth, metadata_to_component_status(universe.metadata)


def _capture_final_close(slot, observed: datetime) -> MarketWatchSnapshotV1:
    """Build one close snapshot from provider-proven final values."""

    from tradex.dashboard import __main__ as dashboard_app
    from tradex.dashboard.risk_service import get_risk_appetite_data

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
    breadth, breadth_status = _provider_verified_final_breadth(
        minute.date(),
        observed,
    )
    risk_data = dict(risk_data)
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


def build_collector(
    *,
    ledger: MarketWatchCollectionStore,
    history: MarketWatchHistoryStore,
    clock=_now,
) -> MarketWatchCollector:
    from tradex.dashboard.risk_service import get_rotation_radar_as_of
    from tradex.market_watch.reconstruction import SameDayPostCloseReconstructor

    reconstructor = SameDayPostCloseReconstructor(
        history=history,
        target_minutes=ledger.list_recovery_gap_minutes,
        rotation_loader=get_rotation_radar_as_of,
        clock=clock,
    )

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
    analyze: bool = True,
) -> dict:
    """Persist live limit status or one scheduled whole-pool attribution."""

    from tradex.market_watch.limit_up_pool import build_limit_up_follow_pool
    from tradex.market_watch.limit_up_pool_store import LimitUpFollowPoolStore
    from tradex.market_watch.read_facade import MarketWatchReadFacade
    from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader, read_profiles

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
        with LimitUpFollowPoolStore() as store:
            existing = store.get_by_source_revision(
                accepted.pointer.source_snapshot_revision
            )
        existing_is_reusable = existing is not None and (
            not analyze
            or (
                "analysis_pending_midday_or_post_close"
                not in existing.quality_flags
                and existing.relationship_catalog_revision
                == (taxonomy_status.catalog_revision if taxonomy_status else None)
            )
        )
        if existing_is_reusable:
            return {
                "action": "existing",
                "source_snapshot_revision": existing.source_snapshot_revision,
                "attribution_revision": existing.attribution_revision,
                "pool_total": existing.pool_total,
            }
    pool = build_limit_up_follow_pool(
        accepted.source_payload,
        source_snapshot_revision=accepted.pointer.source_snapshot_revision,
        generated_at=_now(),
        relationship_loader=read_profiles,
        analyze=analyze,
    )
    with LimitUpFollowPoolStore() as store:
        return store.record(pool)


def _backfill_limit_up_pool() -> int:
    result = _generate_latest_limit_up_pool()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _run_post_close_resonance_loop(
    stop_event: threading.Event,
    *,
    clock=_now,
    check_interval_seconds: float = 30.0,
) -> None:
    """Run one bounded backfill per verified trading day after the close."""

    from tradex.market_calendar import TradingSessionPhase, a_share_session

    completed_dates = set()
    completed_midday_dates = set()
    completed_resonance_buckets = set()
    completed_limit_status_buckets = set()
    while not stop_event.is_set():
        observed = clock()
        session = a_share_session(observed)
        intraday_bucket = observed.replace(second=0, microsecond=0)
        local_time = observed.time().replace(tzinfo=None)
        limit_status_due = (
            session.is_open
            and intraday_bucket not in completed_limit_status_buckets
        )
        resonance_due = (
            session.is_open
            and local_time >= time(9, 35)
            and intraday_bucket not in completed_resonance_buckets
        )
        midday_due = (
            session.is_trading_day
            and time(11, 30) <= local_time < time(13, 0)
            and observed.date() not in completed_midday_dates
        )
        post_close_due = (
            session.is_trading_day
            and session.phase is TradingSessionPhase.CLOSED
            and local_time >= time(15, 0)
            and observed.date() not in completed_dates
        )
        if limit_status_due:
            try:
                result = _generate_latest_limit_up_pool(
                    reuse_existing=True,
                    analyze=False,
                )
                completed_limit_status_buckets.add(intraday_bucket)
                logger.info(
                    "live limit-up status ready revision=%s total=%s",
                    result.get("attribution_revision"),
                    result.get("pool_total"),
                )
            except Exception:
                logger.exception("live limit-up status refresh failed")
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
        if midday_due:
            try:
                result = _generate_latest_limit_up_pool(
                    reuse_existing=False,
                    analyze=True,
                )
                completed_midday_dates.add(observed.date())
                logger.info(
                    "midday limit-up attribution ready revision=%s total=%s",
                    result.get("attribution_revision"),
                    result.get("pool_total"),
                )
            except Exception:
                logger.exception("midday limit-up attribution failed")
        if post_close_due:
            succeeded = True
            resonance_result = {}
            limit_up_result = {}
            try:
                resonance_result = _generate_latest_resonance(reuse_existing=True)
            except Exception:
                logger.exception("post-close sector resonance backfill failed")
                succeeded = False
            try:
                limit_up_result = _generate_latest_limit_up_pool(
                    reuse_existing=False,
                    analyze=True,
                )
            except Exception:
                logger.exception("limit-up follow pool backfill failed")
                succeeded = False
            if succeeded:
                completed_dates.add(observed.date())
                logger.info(
                    "derived market-watch evidence ready resonance=%s entries=%s "
                    "limit_up_pool=%s total=%s phase=%s",
                    resonance_result.get("resonance_revision"),
                    resonance_result.get("entry_count"),
                    limit_up_result.get("attribution_revision"),
                    limit_up_result.get("pool_total"),
                    session.phase.value,
                )
        stop_event.wait(check_interval_seconds)


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
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return
        resonance_stop_event = threading.Event()
        resonance_thread = threading.Thread(
            target=_run_post_close_resonance_loop,
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
            return
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
    parser.add_argument(
        "--backfill-resonance",
        action="store_true",
        help="backtrack minute correlation for the latest accepted real snapshot",
    )
    parser.add_argument(
        "--backfill-limit-up-pool",
        action="store_true",
        help="attribute the latest accepted real limit-up pool at first seal time",
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
        if args.backfill_resonance:
            return _backfill_resonance()
        if args.backfill_limit_up_pool:
            return _backfill_limit_up_pool()
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
    "_run_post_close_resonance_loop",
]
