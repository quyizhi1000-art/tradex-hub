"""Portless runtime owner for replay evaluation and post-close analysis.

This process deliberately sits beside, not inside, the Dashboard and the
market-data Collector.  It consumes accepted provider-neutral data, executes
bounded domain jobs, and publishes display-ready artifacts for Web reads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping
from zoneinfo import ZoneInfo

from tradex.analysis_jobs import (
    DAILY_STOCK_SELECTION,
    MARKET_WATCH_EVALUATION,
    POST_MARKET_REVIEW,
    AnalysisJobStore,
)
from tradex.market_watch.collection_store import MarketWatchCollectionStore
from tradex.market_watch.history import MarketWatchHistoryStore
from tradex.market_watch.read_facade import MarketWatchReadFacade
from tradex.market_watch.review import ReviewTrigger
from tradex.market_watch.review_service import (
    PostMarketReviewError,
    PostMarketReviewService,
)
from tradex.market_watch.review_store import PostMarketReviewStore
from tradex.stock_selection.service import (
    DailyStockSelectionError,
    DailyStockSelectionService,
)
from tradex.stock_selection.store import DailyStockSelectionStore


SHANGHAI = ZoneInfo("Asia/Shanghai")
EVALUATION_SCOPES = (3, 10, 20)
logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _revision(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _requested_at(job: Mapping[str, Any]) -> datetime:
    parsed = datetime.fromisoformat(str(job["requested_at"]))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("analysis job requested_at must include a timezone")
    return parsed.astimezone(SHANGHAI)


def _evaluation_alerts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "alert": item["alert"],
            "trade_date": item["trade_date"],
            "emitted_at": item["observed_at"],
            "snapshot_id": item["snapshot_id"],
        }
        for item in events
    ]


def _compact_review_history(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep every visible review field while excluding the raw evidence bundle."""

    view = dict(payload)
    review = payload.get("review")
    if not isinstance(review, Mapping):
        return view
    evidence = review.get("evidence")
    evidence_components = (
        list(evidence.get("components") or ()) if isinstance(evidence, Mapping) else []
    )
    visible_keys = (
        "contract",
        "schema_version",
        "config_version",
        "review_id",
        "trade_date",
        "generated_at",
        "trigger",
        "quality",
        "source_snapshot_id",
        "source_snapshot_as_of",
        "source_freshness",
        "recap",
        "next_day_outlook",
        "opportunity_sectors",
        "learning",
        "limitations",
    )
    compact = {key: review[key] for key in visible_keys if key in review}
    compact["evidence"] = {"components": evidence_components}
    view["review"] = compact
    return view


class AnalysisRuntime:
    """Own domain services and materialized artifacts inside one process."""

    def __init__(
        self,
        jobs: AnalysisJobStore,
        *,
        history: MarketWatchHistoryStore | None = None,
        collection: MarketWatchCollectionStore | None = None,
        review_store: PostMarketReviewStore | None = None,
        selection_store: DailyStockSelectionStore | None = None,
    ) -> None:
        self.jobs = jobs
        self.history = history or MarketWatchHistoryStore(read_only=True)
        self.collection = collection or MarketWatchCollectionStore(read_only=True)
        self.review_store = review_store or PostMarketReviewStore()
        self.selection_store = selection_store or DailyStockSelectionStore()
        self.read_facade = MarketWatchReadFacade(
            collection_reader=self.collection,
            history_reader=self.history,
        )
        self.review_service = PostMarketReviewService(
            self._load_accepted_snapshot,
            self.review_store,
            history_loader=self._load_full_history,
        )
        self.selection_service = DailyStockSelectionService(self.selection_store)

    def _load_accepted_snapshot(self):
        view = self.read_facade.read()
        if view.accepted is None:
            raise RuntimeError("no collector-accepted real market-watch snapshot")
        return view.accepted.source_payload

    def _load_full_history(self, trade_date) -> list[dict[str, Any]]:
        return self.history.get_timeline(trade_date.isoformat(), limit=1000)

    def _job_result_summary(
        self,
        capability: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        if capability == POST_MARKET_REVIEW:
            review = result.get("review")
            review = review if isinstance(review, Mapping) else {}
            return {
                "action": result.get("action"),
                "trade_date": review.get("trade_date"),
                "review_id": review.get("review_id"),
            }
        selection = result.get("selection")
        selection = selection if isinstance(selection, Mapping) else {}
        return {
            "action": result.get("action"),
            "trade_date": selection.get("trade_date"),
            "selection_id": selection.get("selection_id"),
        }

    def execute_next_job(self) -> dict[str, Any] | None:
        job = self.jobs.claim_next()
        if job is None:
            return None
        job_id = str(job["job_id"])
        capability = str(job["capability"])
        try:
            current = _requested_at(job)
            self.jobs.set_phase(job_id, "generating")
            if capability == POST_MARKET_REVIEW:
                result = self.review_service.generate(
                    trigger=ReviewTrigger.MANUAL,
                    now=current,
                )
            elif capability == DAILY_STOCK_SELECTION:
                result = self.selection_service.generate(now=current, automatic=False)
            else:
                raise ValueError(f"unsupported queued capability: {capability}")
            self.jobs.set_phase(job_id, "publishing")
            if capability == POST_MARKET_REVIEW:
                self.materialize_review_views(force=True)
            else:
                self.materialize_selection_views(force=True)
            summary = self._job_result_summary(capability, result)
            self.jobs.succeed(job_id, result=summary)
        except (PostMarketReviewError, DailyStockSelectionError) as exc:
            cause = getattr(exc, "cause", None) or exc
            self.jobs.fail(
                job_id,
                error=exc.safe_message,
                failure_code=type(cause).__name__,
            )
            logger.warning(
                "analysis job failed safely: capability=%s job_id=%s failure=%s",
                capability,
                job_id,
                type(cause).__name__,
            )
        except Exception as exc:  # noqa: BLE001 - durable public failure boundary
            self.jobs.fail(
                job_id,
                error="后台分析失败，请稍后重试。",
                failure_code=type(exc).__name__,
            )
            logger.exception(
                "analysis job crashed: capability=%s job_id=%s",
                capability,
                job_id,
            )
        return self.jobs.latest_job(capability, scope_key=str(job["scope_key"]))

    def materialize_review_views(self, *, force: bool = False) -> int:
        dates = self.review_store.list_dates(limit=365)
        learning = self.review_store.learning_summary(limit=20).model_dump(mode="json")
        catalog_revision = _revision({"dates": dates, "learning": learning})
        published = 0
        latest_payload = None
        for item in dates:
            trade_date = str(item["trade_date"])
            scope = f"date:{trade_date}"
            source_revision = _revision(
                {"catalog_revision": catalog_revision, "entry": item}
            )
            existing = self.jobs.get_artifact(POST_MARKET_REVIEW, scope_key=scope)
            if force or existing is None or existing["source_revision"] != source_revision:
                payload = _compact_review_history(
                    self.review_service.history(trade_date=trade_date, limit=365)
                )
                self.jobs.put_artifact(
                    POST_MARKET_REVIEW,
                    scope_key=scope,
                    source_revision=source_revision,
                    payload=payload,
                )
                published += 1
            else:
                payload = existing["payload"]
            if latest_payload is None:
                latest_payload = payload
        if latest_payload is None:
            latest_payload = {
                "contract": "post_market_review_archive.v1",
                "schema_version": 1,
                "trade_date": None,
                "dates": [],
                "review": None,
                "presentation": None,
                "outcome": None,
                "learning": learning,
                "schedule": {
                    "manual_after": "17:30",
                    "automatic_if_missing_after": "21:00",
                    "timezone": "Asia/Shanghai",
                },
            }
        latest = self.jobs.get_artifact(POST_MARKET_REVIEW, scope_key="latest")
        if force or latest is None or latest["source_revision"] != catalog_revision:
            self.jobs.put_artifact(
                POST_MARKET_REVIEW,
                scope_key="latest",
                source_revision=catalog_revision,
                payload=latest_payload,
            )
            published += 1
        return published

    def materialize_selection_views(self, *, force: bool = False) -> int:
        dates = self.selection_store.list_dates(limit=365)
        catalog_revision = _revision(dates)
        published = 0
        latest_payload = None
        for item in dates:
            trade_date = str(item["trade_date"])
            scope = f"date:{trade_date}"
            source_revision = _revision(
                {"catalog_revision": catalog_revision, "entry": item}
            )
            existing = self.jobs.get_artifact(DAILY_STOCK_SELECTION, scope_key=scope)
            if force or existing is None or existing["source_revision"] != source_revision:
                payload = self.selection_service.history(
                    trade_date=trade_date,
                    limit=365,
                )
                self.jobs.put_artifact(
                    DAILY_STOCK_SELECTION,
                    scope_key=scope,
                    source_revision=source_revision,
                    payload=payload,
                )
                published += 1
            else:
                payload = existing["payload"]
            if latest_payload is None:
                latest_payload = payload
        if latest_payload is None:
            latest_payload = {
                "contract": "daily_stock_selection_archive.v1",
                "schema_version": 1,
                "trade_date": None,
                "dates": [],
                "selection": None,
                "outcome": None,
                "recent_outcomes": [],
                "schedule": {
                    "manual_after": "18:00",
                    "automatic_if_missing_after": "18:30",
                    "timezone": "Asia/Shanghai",
                },
            }
        latest = self.jobs.get_artifact(DAILY_STOCK_SELECTION, scope_key="latest")
        if force or latest is None or latest["source_revision"] != catalog_revision:
            self.jobs.put_artifact(
                DAILY_STOCK_SELECTION,
                scope_key="latest",
                source_revision=catalog_revision,
                payload=latest_payload,
            )
            published += 1
        return published

    def _session_evaluation(self, trade_date: str) -> tuple[str, dict[str, Any]]:
        from tradex.market_watch.evaluation import evaluate_market_watch_session

        samples = self.history.get_replay_timeline(trade_date, limit=1000)
        alerts = self.history.get_alerts(trade_date, limit=1000)
        source_revision = _revision({"samples": samples, "alerts": alerts})
        report = evaluate_market_watch_session(
            [item["payload"] for item in samples],
            alerts=_evaluation_alerts(alerts),
            trade_date=trade_date,
        ).model_dump(mode="json")
        return source_revision, report

    def _multi_day_evaluation(
        self,
        dates: list[str],
        days: int,
    ) -> tuple[str, dict[str, Any]] | None:
        from tradex.market_watch.evaluation import (
            EvaluationConfigV1,
            evaluate_market_watch_history,
        )

        selected = list(reversed(dates[:days]))
        samples = [
            item
            for trade_date in selected
            for item in self.history.get_replay_timeline(trade_date, limit=1000)
        ]
        alerts = [
            item
            for trade_date in selected
            for item in self.history.get_alerts(trade_date, limit=1000)
        ]
        if not samples:
            return None
        source_revision = _revision(
            {"days": days, "dates": selected, "samples": samples, "alerts": alerts}
        )
        report = evaluate_market_watch_history(
            [item["payload"] for item in samples],
            alerts=_evaluation_alerts(alerts),
            config=EvaluationConfigV1(minimum_sessions_for_multi_day=days),
        ).model_dump(mode="json")
        return source_revision, report

    def materialize_evaluations(self, *, force: bool = False) -> int:
        dates = [str(item["trade_date"]) for item in self.history.list_dates(limit=90)]
        published = 0
        for trade_date in dates:
            scope = f"date:{trade_date}"
            source_revision, report = self._session_evaluation(trade_date)
            existing = self.jobs.get_artifact(MARKET_WATCH_EVALUATION, scope_key=scope)
            if force or existing is None or existing["source_revision"] != source_revision:
                self.jobs.put_artifact(
                    MARKET_WATCH_EVALUATION,
                    scope_key=scope,
                    source_revision=source_revision,
                    payload=report,
                )
                published += 1
        for days in EVALUATION_SCOPES:
            materialized = self._multi_day_evaluation(dates, days)
            if materialized is None:
                continue
            source_revision, report = materialized
            scope = f"days:{days}"
            existing = self.jobs.get_artifact(MARKET_WATCH_EVALUATION, scope_key=scope)
            if force or existing is None or existing["source_revision"] != source_revision:
                self.jobs.put_artifact(
                    MARKET_WATCH_EVALUATION,
                    scope_key=scope,
                    source_revision=source_revision,
                    payload=report,
                )
                published += 1
        return published

    def run_automatic(self, *, now: datetime | None = None) -> None:
        observed = (now or _now()).astimezone(SHANGHAI)
        try:
            self.review_service.maybe_generate_automatic(now=observed)
        except PostMarketReviewError as exc:
            logger.debug("automatic review not generated: %s", exc.safe_message)
        try:
            self.selection_service.maybe_generate_automatic(now=observed)
        except DailyStockSelectionError as exc:
            logger.debug("automatic selection not generated: %s", exc.safe_message)

    def materialize_all(self, *, force: bool = False) -> dict[str, int]:
        return {
            "review": self.materialize_review_views(force=force),
            "selection": self.materialize_selection_views(force=force),
            "evaluation": self.materialize_evaluations(force=force),
        }

    def close(self) -> None:
        self.selection_service.wait_for_generation()
        self.selection_store.close()
        self.review_store.close()
        self.collection.close()
        self.history.close()


def _lock_path() -> Path:
    configured = os.environ.get("TRADEX_ANALYSIS_WORKER_LOCK")
    path = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".tradex" / "analysis_worker.lock"
    )
    resolved = path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


@contextmanager
def exclusive_worker_lock(path: Path | None = None) -> Iterator[BinaryIO]:
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
            raise RuntimeError("another analysis worker already owns the lock") from exc
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
    def request_stop(_signum, _frame):
        stop_event.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signal_name):
            signal.signal(getattr(signal, signal_name), request_stop)


def _status() -> int:
    with AnalysisJobStore() as jobs:
        print(json.dumps(jobs.runtime_status(), ensure_ascii=False, sort_keys=True))
    return 0


def _mark_stopped() -> int:
    with AnalysisJobStore() as jobs:
        jobs.set_runtime_state("stopped", detail="managed stop")
    return 0


def _run(stop_event: threading.Event, *, once: bool = False) -> None:
    with AnalysisJobStore() as jobs:
        jobs.set_runtime_state("starting")
        recovered = jobs.recover_interrupted()
        runtime = AnalysisRuntime(jobs)
        try:
            initial = runtime.materialize_all()
            jobs.set_runtime_state(
                "running",
                detail=f"ready recovered={recovered} published={sum(initial.values())}",
            )
            if once:
                runtime.execute_next_job()
                runtime.run_automatic()
                runtime.materialize_all()
                return
            last_materialized = _now()
            while not stop_event.is_set():
                runtime.execute_next_job()
                observed = _now()
                runtime.run_automatic(now=observed)
                if (observed - last_materialized).total_seconds() >= 30:
                    runtime.materialize_all()
                    last_materialized = observed
                jobs.set_runtime_state("running")
                stop_event.wait(2.0)
        finally:
            runtime.close()
            jobs.set_runtime_state("stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tradex analysis worker")
    parser.add_argument("--status", action="store_true", help="read worker status only")
    parser.add_argument("--mark-stopped", action="store_true")
    parser.add_argument("--once", action="store_true", help="run one bounded cycle")
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
        with exclusive_worker_lock():
            _run(stop_event, once=args.once)
    except RuntimeError as exc:
        logger.error("analysis worker stopped: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "AnalysisRuntime",
    "exclusive_worker_lock",
    "main",
    "_compact_review_history",
]
