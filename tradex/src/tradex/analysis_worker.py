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
from datetime import date, datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping
from zoneinfo import ZoneInfo

from tradex.analysis_jobs import (
    DAILY_STOCK_SELECTION,
    MANUAL_PORTFOLIO_INTRADAY_ANALYSIS,
    MANUAL_PORTFOLIO_OUTLOOK,
    MARKET_WATCH_EVALUATION,
    POST_MARKET_REVIEW,
    AnalysisJobStore,
)
from tradex.market_watch.collection_store import MarketWatchCollectionStore
from tradex.market_watch.history import MarketWatchHistoryStore
from tradex.market_watch.limit_sentiment_store import LimitSentimentStore
from tradex.market_watch.read_facade import MarketWatchReadFacade
from tradex.market_watch.review import ReviewTrigger
from tradex.market_watch.review_evidence import collect_daily_market_review_evidence
from tradex.market_watch.review_editorial_store import ReviewEditorialOverrideStore
from tradex.market_watch.review_announcement_store import (
    ReviewOfficialAnnouncementStore,
)
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
from tradex.manual_portfolio.intraday_analysis import (
    build_manual_portfolio_intraday_analysis,
)
from tradex.manual_portfolio.contracts import ManualPortfolioOutlookV1
from tradex.manual_portfolio.daily_review import build_manual_portfolio_daily_review
from tradex.manual_portfolio.outlook import build_manual_portfolio_outlook
from tradex.manual_portfolio.readiness import portfolio_snapshot_has_final_close
from tradex.manual_portfolio.store import ManualPortfolioReader


SHANGHAI = ZoneInfo("Asia/Shanghai")
# Preserve the existing bounded API (days=1..30), while the page currently
# exposes the common 3/10/20-day choices.
EVALUATION_SCOPES = tuple(range(1, 31))
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


def _portfolio_relationship_contexts(
    instrument_ids: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    """Project only the local relationship catalog; never use provider sector labels."""

    from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader

    contexts: dict[str, dict[str, Any]] = {}
    with InstrumentTaxonomyReader() as reader:
        status = reader.status()
        if status is None:
            return contexts
        for instrument_id in instrument_ids:
            profile = reader.get(instrument_id)
            if profile is None:
                continue
            industry = profile.regulatory_industry or profile.statistical_industry
            industry_path = ()
            industry_code = None
            industry_taxonomy = None
            if industry is not None and industry.taxonomy in {"capco", "sw"}:
                industry_taxonomy = industry.taxonomy
                industry_path = tuple(
                    dict.fromkeys(
                        value
                        for value in (
                            industry.level1_name,
                            industry.level2_name,
                            industry.level3_name,
                        )
                        if value
                    )
                )
                industry_code = (
                    industry.level3_code
                    or industry.level2_code
                    or industry.level1_code
                )
            business_path = tuple(
                dict.fromkeys(
                    value
                    for value in (
                        profile.business_domain_name,
                        profile.directory_category_name,
                        profile.primary_business_name,
                    )
                    if value
                )
            )
            contexts[instrument_id] = {
                "catalog_revision": status.catalog_revision,
                "catalog_as_of": status.as_of,
                "profile_as_of": profile.as_of,
                "profile_name": profile.name,
                "verification_status": profile.verification_status,
                "industry_taxonomy": industry_taxonomy,
                "industry_code": industry_code,
                "industry_path": industry_path,
                "business_path": business_path,
                "concept_names": tuple(
                    dict.fromkeys(item.name for item in profile.concept_memberships)
                ),
                "business_summary": profile.business_summary,
                "quality_flags": tuple(profile.flags),
            }
    return contexts


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
        limit_sentiment_store: LimitSentimentStore | None = None,
        official_announcement_store: ReviewOfficialAnnouncementStore | None = None,
        editorial_override_store: ReviewEditorialOverrideStore | None = None,
        selection_store: DailyStockSelectionStore | None = None,
        portfolio_reader: ManualPortfolioReader | None = None,
        enable_taxonomy_auto_refresh: bool = False,
    ) -> None:
        self.jobs = jobs
        self.history = history or MarketWatchHistoryStore(read_only=True)
        self.collection = collection or MarketWatchCollectionStore(read_only=True)
        self.review_store = review_store or PostMarketReviewStore()
        self.limit_sentiment_store = (
            limit_sentiment_store or LimitSentimentStore(read_only=True)
        )
        self.official_announcement_store = (
            official_announcement_store
            or ReviewOfficialAnnouncementStore(read_only=True)
        )
        self.editorial_override_store = (
            editorial_override_store or ReviewEditorialOverrideStore()
        )
        self.selection_store = selection_store or DailyStockSelectionStore()
        self.portfolio_reader = portfolio_reader or ManualPortfolioReader()
        self.read_facade = MarketWatchReadFacade(
            collection_reader=self.collection,
            history_reader=self.history,
        )
        self.review_service = PostMarketReviewService(
            self._load_accepted_snapshot,
            self.review_store,
            evidence_loader=self._load_review_evidence,
            history_loader=self._load_full_history,
            official_announcement_loader=self.official_announcement_store.get,
            editorial_override_loader=self.editorial_override_store.get,
        )
        self.selection_service = DailyStockSelectionService(self.selection_store)
        self._enable_taxonomy_auto_refresh = bool(enable_taxonomy_auto_refresh)
        self._taxonomy_refresh_lock = threading.Lock()
        self._taxonomy_refresh_thread: threading.Thread | None = None
        self._taxonomy_last_attempt_at: datetime | None = None

    def _load_accepted_snapshot(self):
        view = self.read_facade.read()
        if view.accepted is None:
            raise RuntimeError("no collector-accepted real market-watch snapshot")
        return view.accepted.source_payload

    def _load_full_history(self, trade_date) -> list[dict[str, Any]]:
        return self.history.get_timeline(trade_date.isoformat(), limit=1000)

    def _load_review_evidence(self, snapshot, now):
        trade_date = snapshot.market_state.trading_date
        return collect_daily_market_review_evidence(
            snapshot,
            history_samples=self._load_full_history(trade_date),
            collected_at=now,
            limit_sentiment=self.limit_sentiment_store.get(trade_date),
        )

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
        if capability in {
            MANUAL_PORTFOLIO_OUTLOOK,
            MANUAL_PORTFOLIO_INTRADAY_ANALYSIS,
        }:
            result_key = (
                "outlook"
                if capability == MANUAL_PORTFOLIO_OUTLOOK
                else "intraday_analysis"
            )
            analysis = result.get(result_key)
            analysis = analysis if isinstance(analysis, Mapping) else {}
            return {
                "action": result.get("action"),
                "portfolio_revision": analysis.get("portfolio_revision"),
                "source_snapshot_revision": analysis.get("source_snapshot_revision"),
                "item_count": len(analysis.get("items") or ()),
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
                result = self.selection_service.generate(
                    now=_now(),
                    trade_date=date.fromisoformat(str(job["trade_date"])),
                    automatic=False,
                )
            elif capability == MANUAL_PORTFOLIO_OUTLOOK:
                snapshot = self.portfolio_reader.latest_snapshot()
                if snapshot is None:
                    raise RuntimeError("no collector-owned manual portfolio snapshot")
                if not portfolio_snapshot_has_final_close(snapshot):
                    raise RuntimeError(
                        "manual portfolio outlook requires a verified final-close snapshot"
                    )
                expected_scopes = {f"portfolio:{snapshot.portfolio_revision}"}
                if snapshot.trading_date is not None:
                    expected_scopes.add(
                        f"date:{snapshot.trading_date}:portfolio:{snapshot.portfolio_revision}"
                    )
                if str(job["scope_key"]) not in expected_scopes:
                    raise RuntimeError(
                        "manual portfolio revision changed before analysis"
                    )
                market_context = None
                review_artifact = self.jobs.get_artifact(
                    POST_MARKET_REVIEW,
                    scope_key="latest",
                )
                if review_artifact is not None:
                    review_archive = review_artifact.get("payload") or {}
                    review = review_archive.get("review") or {}
                    next_day = review.get("next_day_outlook") or {}
                    if (
                        isinstance(review, Mapping)
                        and isinstance(next_day, Mapping)
                        and snapshot.trading_date is not None
                        and review.get("trade_date") == snapshot.trading_date.isoformat()
                    ):
                        market_context = {
                            "source_trade_date": review.get("trade_date"),
                            "bias": next_day.get("bias", "uncertain"),
                            "confidence": next_day.get("confidence", "abstain"),
                            "thesis": next_day.get("thesis", "盘面方向尚未形成。"),
                            "expected_shape": next_day.get(
                                "expected_shape",
                                "等待盘中价格、广度与资金形成同向确认。",
                            ),
                            "confirmation": next_day.get(
                                "confirmation",
                                "至少三项盘面证据连续同向。",
                            ),
                            "invalidation": next_day.get(
                                "invalidation",
                                "盘面证据持续背离时维持观望。",
                            ),
                            "risk_control": next_day.get(
                                "risk_control",
                                "等待条件确认，不追单点异动。",
                            ),
                            "supporting_evidence": next_day.get(
                                "supporting_evidence",
                                (),
                            ),
                            "counter_evidence": next_day.get(
                                "counter_evidence",
                                (),
                            ),
                            "limitations": review.get("limitations", ()),
                        }
                daily_review = None
                if snapshot.trading_date is not None:
                    archived_candidates = [
                        *self.jobs.list_artifacts(
                            MANUAL_PORTFOLIO_OUTLOOK,
                            scope_prefix="date:",
                            limit=366,
                        ),
                        *self.jobs.list_artifacts(
                            MANUAL_PORTFOLIO_OUTLOOK,
                            scope_prefix="portfolio:",
                            limit=366,
                        ),
                    ]
                    previous_candidates = []
                    seen_payloads = set()
                    for archived in archived_candidates:
                        payload_digest = archived.get("payload_digest")
                        if payload_digest in seen_payloads:
                            continue
                        seen_payloads.add(payload_digest)
                        try:
                            previous_outlook = ManualPortfolioOutlookV1.model_validate(
                                archived.get("payload") or {}
                            )
                        except (TypeError, ValueError):
                            continue
                        if (
                            previous_outlook.source_trading_date is not None
                            and previous_outlook.source_trading_date < snapshot.trading_date
                        ):
                            previous_candidates.append(previous_outlook)
                    if previous_candidates:
                        previous_outlook = max(
                            previous_candidates,
                            key=lambda item: (item.source_trading_date, item.generated_at),
                        )
                        daily_review = build_manual_portfolio_daily_review(
                            previous_outlook,
                            snapshot,
                            generated_at=current,
                        )
                relationship_loader = getattr(
                    self,
                    "portfolio_relationship_loader",
                    _portfolio_relationship_contexts,
                )
                relationship_contexts = relationship_loader(
                    tuple(item.instrument_id for item in snapshot.items)
                )
                outlook = build_manual_portfolio_outlook(
                    snapshot,
                    generated_at=current,
                    market_context=market_context,
                    daily_review=(
                        daily_review.model_dump(mode="json")
                        if daily_review is not None
                        else None
                    ),
                    relationship_contexts=relationship_contexts,
                )
                result = {
                    "action": "materialized",
                    "outlook": outlook.model_dump(mode="json"),
                }
            elif capability == MANUAL_PORTFOLIO_INTRADAY_ANALYSIS:
                snapshot = self.portfolio_reader.latest_snapshot()
                if snapshot is None:
                    raise RuntimeError("no collector-owned manual portfolio snapshot")
                expected_scope = f"snapshot:{snapshot.snapshot_revision}"
                if str(job["scope_key"]) != expected_scope:
                    raise RuntimeError(
                        "manual portfolio snapshot changed before intraday analysis"
                    )
                intraday_analysis = build_manual_portfolio_intraday_analysis(
                    snapshot,
                    generated_at=current,
                )
                result = {
                    "action": "materialized",
                    "intraday_analysis": intraday_analysis.model_dump(mode="json"),
                }
            else:
                raise ValueError(f"unsupported queued capability: {capability}")
            self.jobs.set_phase(job_id, "publishing")
            if capability == POST_MARKET_REVIEW:
                self.materialize_review_views(force=True)
            elif capability == DAILY_STOCK_SELECTION:
                self.materialize_selection_views(force=True)
            elif capability == MANUAL_PORTFOLIO_OUTLOOK:
                self.jobs.put_artifact(
                    MANUAL_PORTFOLIO_OUTLOOK,
                    scope_key=f"portfolio:{outlook.portfolio_revision}",
                    source_revision=outlook.source_snapshot_revision,
                    payload=outlook.model_dump(mode="json"),
                    generated_at=current,
                )
                self.jobs.put_artifact(
                    MANUAL_PORTFOLIO_OUTLOOK,
                    scope_key="latest",
                    source_revision=outlook.source_snapshot_revision,
                    payload=outlook.model_dump(mode="json"),
                    generated_at=current,
                )
                if outlook.source_trading_date is not None:
                    self.jobs.put_artifact(
                        MANUAL_PORTFOLIO_OUTLOOK,
                        scope_key=f"date:{outlook.source_trading_date}",
                        source_revision=outlook.source_snapshot_revision,
                        payload=outlook.model_dump(mode="json"),
                        generated_at=current,
                    )
            else:
                self.jobs.put_artifact(
                    MANUAL_PORTFOLIO_INTRADAY_ANALYSIS,
                    scope_key=f"snapshot:{intraday_analysis.source_snapshot_revision}",
                    source_revision=intraday_analysis.source_snapshot_revision,
                    payload=intraday_analysis.model_dump(mode="json"),
                    generated_at=current,
                )
                self.jobs.put_artifact(
                    MANUAL_PORTFOLIO_INTRADAY_ANALYSIS,
                    scope_key="latest",
                    source_revision=intraday_analysis.source_snapshot_revision,
                    payload=intraday_analysis.model_dump(mode="json"),
                    generated_at=current,
                )
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
        from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader

        dates = self.review_store.list_dates(limit=365)
        learning = self.review_store.learning_summary(limit=20).model_dump(mode="json")
        announcement_revisions = {
            str(item["trade_date"]): (
                archive.source_revision
                if (
                    archive := self.official_announcement_store.get(
                        str(item["trade_date"])
                    )
                )
                is not None
                else None
            )
            for item in dates
        }
        editorial_revisions = {
            str(item["trade_date"]): self.editorial_override_store.revision(
                str(item["trade_date"])
            )
            for item in dates
        }
        with InstrumentTaxonomyReader() as reader:
            relationship_status = reader.status()
        catalog_revision = _revision({
            "dates": dates,
            "learning": learning,
            "relationship_catalog_revision": (
                relationship_status.catalog_revision if relationship_status else None
            ),
            "official_announcement_revisions": announcement_revisions,
            "editorial_revisions": editorial_revisions,
        })
        published = 0
        latest_payload = None
        for item in dates:
            trade_date = str(item["trade_date"])
            scope = f"date:{trade_date}"
            source_revision = _revision(
                {
                    "catalog_revision": catalog_revision,
                    "entry": item,
                    "official_announcement_revision": announcement_revisions.get(
                        trade_date
                    ),
                    "editorial_revision": editorial_revisions.get(trade_date),
                }
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
        strategy_dates = self.selection_store.list_strategy_dates(limit=365)
        strategy_catalog = self.selection_service.strategy_history(limit=1)["catalog"]
        catalog_revision = _revision(
            {
                "legacy_dates": dates,
                "strategy_dates": strategy_dates,
                "strategy_catalog": strategy_catalog,
            }
        )
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
            latest_payload = self.selection_service.history(limit=365)
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

    def materialize_evaluations(self, *, force: bool = False) -> int:
        from tradex.market_watch.evaluation import (
            EvaluationConfigV1,
            evaluate_market_watch_history,
            evaluate_market_watch_session,
        )

        dates = [str(item["trade_date"]) for item in self.history.list_dates(limit=90)]
        published = 0
        material: dict[str, dict[str, Any]] = {}
        for trade_date in dates:
            scope = f"date:{trade_date}"
            samples = self.history.get_replay_timeline(trade_date, limit=1000)
            alerts = self.history.get_alerts(trade_date, limit=1000)
            source_revision = _revision({"samples": samples, "alerts": alerts})
            material[trade_date] = {
                "samples": samples,
                "alerts": alerts,
                "source_revision": source_revision,
            }
            existing = self.jobs.get_artifact(MARKET_WATCH_EVALUATION, scope_key=scope)
            if force or existing is None or existing["source_revision"] != source_revision:
                report = evaluate_market_watch_session(
                    [item["payload"] for item in samples],
                    alerts=_evaluation_alerts(alerts),
                    trade_date=trade_date,
                ).model_dump(mode="json")
                self.jobs.put_artifact(
                    MARKET_WATCH_EVALUATION,
                    scope_key=scope,
                    source_revision=source_revision,
                    payload=report,
                )
                published += 1
        for days in EVALUATION_SCOPES:
            selected = list(reversed(dates[:days]))
            selected_material = [material[item] for item in selected]
            if not any(item["samples"] for item in selected_material):
                continue
            source_revision = _revision(
                {
                    "days": days,
                    "dates": selected,
                    "date_revisions": [
                        item["source_revision"] for item in selected_material
                    ],
                }
            )
            scope = f"days:{days}"
            existing = self.jobs.get_artifact(MARKET_WATCH_EVALUATION, scope_key=scope)
            if force or existing is None or existing["source_revision"] != source_revision:
                samples = [
                    sample
                    for item in selected_material
                    for sample in item["samples"]
                ]
                alerts = [
                    alert
                    for item in selected_material
                    for alert in item["alerts"]
                ]
                report = evaluate_market_watch_history(
                    [item["payload"] for item in samples],
                    alerts=_evaluation_alerts(alerts),
                    config=EvaluationConfigV1(minimum_sessions_for_multi_day=days),
                ).model_dump(mode="json")
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
        self._maybe_refresh_taxonomy(observed)
        try:
            self.review_service.maybe_generate_automatic(now=observed)
        except PostMarketReviewError as exc:
            logger.debug("automatic review not generated: %s", exc.safe_message)
        except Exception:  # noqa: BLE001 - keep the independent runtime alive
            logger.exception("automatic review cycle failed")
        try:
            self.selection_service.maybe_generate_automatic(now=observed)
        except DailyStockSelectionError as exc:
            logger.debug("automatic selection not generated: %s", exc.safe_message)
        except Exception:  # noqa: BLE001 - keep the independent runtime alive
            logger.exception("automatic selection cycle failed")

    def _maybe_refresh_taxonomy(self, observed: datetime) -> None:
        if not self._enable_taxonomy_auto_refresh:
            return
        from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader

        with InstrumentTaxonomyReader() as reader:
            status = reader.status()
        if status is not None and status.as_of >= observed.date():
            return
        with self._taxonomy_refresh_lock:
            if (
                self._taxonomy_refresh_thread is not None
                and self._taxonomy_refresh_thread.is_alive()
            ):
                return
            if (
                self._taxonomy_last_attempt_at is not None
                and (observed - self._taxonomy_last_attempt_at).total_seconds() < 1800
            ):
                return
            self._taxonomy_last_attempt_at = observed
            thread = threading.Thread(
                target=self._refresh_taxonomy,
                args=(observed,),
                name="tradex-instrument-taxonomy-refresh",
                daemon=True,
            )
            self._taxonomy_refresh_thread = thread
            thread.start()

    @staticmethod
    def _refresh_taxonomy(observed: datetime) -> None:
        from tradex.instrument_taxonomy.service import InstrumentTaxonomyService

        try:
            with InstrumentTaxonomyService() as service:
                status = service.refresh(as_of=observed.date(), now=observed)
            logger.info(
                "instrument taxonomy refreshed: as_of=%s profiles=%s revision=%s",
                status.as_of,
                status.profile_total,
                status.catalog_revision,
            )
        except Exception:  # noqa: BLE001 - retain the last accepted complete catalog
            logger.exception("instrument taxonomy refresh failed; retained prior catalog")

    def materialize_all(self, *, force: bool = False) -> dict[str, int]:
        return {
            "review": self.materialize_review_views(force=force),
            "selection": self.materialize_selection_views(force=force),
            "evaluation": self.materialize_evaluations(force=force),
        }

    def close(self) -> None:
        if self._taxonomy_refresh_thread is not None:
            self._taxonomy_refresh_thread.join(timeout=5)
        self.selection_service.wait_for_generation()
        self.selection_store.close()
        self.portfolio_reader.close()
        self.review_store.close()
        self.limit_sentiment_store.close()
        self.official_announcement_store.close()
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
        runtime = AnalysisRuntime(jobs, enable_taxonomy_auto_refresh=True)
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
                    try:
                        runtime.materialize_all()
                    except Exception:  # noqa: BLE001 - retry the next bounded cycle
                        logger.exception("analysis artifact materialization failed")
                    finally:
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
