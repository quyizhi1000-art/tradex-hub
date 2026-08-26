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
from datetime import datetime, time
from pathlib import Path
from typing import BinaryIO, Iterator
from zoneinfo import ZoneInfo

from tradex.market_watch.collection_store import MarketWatchCollectionStore
from tradex.market_watch.collector import MarketWatchCollector
from tradex.market_watch.contracts import MarketWatchSnapshotV1
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


def _repair_historical(_slot) -> MarketWatchSnapshotV1:
    """Fail closed until an exact full-snapshot historical source is available.

    Exact sector-flow curves are repaired independently by the sampler-owned
    gateway path and persisted across restarts.  The aggregate market-watch
    minute is never reconstructed with current indices or breadth.
    """

    raise HistoricalMarketWatchUnavailable(
        "no exact historical market-watch aggregate is available for this minute"
    )


def build_collector(
    *,
    ledger: MarketWatchCollectionStore,
    history: MarketWatchHistoryStore,
    clock=_now,
) -> MarketWatchCollector:
    def retained_history_records():
        # Reconcile every retained date before retrying gaps.  This prevents a
        # restarted ledger from refetching or overwriting a strict row that is
        # already durably present in history.
        for item in history.list_dates(limit=8):
            yield from history.get_collection_records(item["trade_date"])

    return MarketWatchCollector(
        store=ledger,
        capture_current=_capture_current,
        repair_historical=_repair_historical,
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


def _run_post_close_resonance_loop(
    stop_event: threading.Event,
    *,
    clock=_now,
    check_interval_seconds: float = 30.0,
) -> None:
    """Run one bounded backfill per verified trading day after the close."""

    from tradex.market_calendar import TradingSessionPhase, a_share_session

    completed_dates = set()
    completed_intraday_buckets = set()
    while not stop_event.is_set():
        observed = clock()
        session = a_share_session(observed)
        intraday_bucket = observed.replace(
            minute=(observed.minute // 5) * 5,
            second=0,
            microsecond=0,
        )
        intraday_due = (
            session.is_open
            and observed.time().replace(tzinfo=None) >= time(9, 35)
            and intraday_bucket not in completed_intraday_buckets
        )
        post_close_due = (
            session.is_trading_day
            and session.phase is TradingSessionPhase.CLOSED
            and observed.date() not in completed_dates
        )
        if intraday_due or post_close_due:
            try:
                result = _generate_latest_resonance(reuse_existing=True)
            except Exception:
                logger.exception("post-close sector resonance backfill failed")
            else:
                if intraday_due:
                    completed_intraday_buckets.add(intraday_bucket)
                if post_close_due:
                    completed_dates.add(observed.date())
                logger.info(
                    "sector resonance ready revision=%s entries=%s phase=%s",
                    result.get("resonance_revision"),
                    result.get("entry_count"),
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
    "_run_post_close_resonance_loop",
]
