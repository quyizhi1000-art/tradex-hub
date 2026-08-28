"""Data aggregation and caching for the dashboard risk-appetite panel."""

from __future__ import annotations

import copy
import logging
import math
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime, time as clock_time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from tradex.data_gateway.leadership import (
    board_leader_snapshot_to_legacy_payload,
    fetch_board_leader_snapshot,
    fetch_leader_quotes,
    fetch_stock_sector_profiles,
    leader_quotes_to_legacy_records,
    stock_sector_profiles_to_legacy_records,
)
from tradex.data_gateway.etfs import etf_quotes_to_legacy_records, fetch_etf_quotes
from tradex.data_gateway.limit_events import (
    fetch_limit_up_events,
    limit_event_series_to_component_metadata,
    limit_event_series_to_legacy_records,
)
from tradex.data_gateway.market_structure import (
    fetch_market_breadth_snapshot,
    fetch_sector_quotes,
    market_breadth_to_legacy_records,
    metadata_to_component_status,
    sector_quotes_to_legacy_records,
)
from tradex.data_gateway.sector_flow import (
    read_sector_intraday_fund_flow_backfill,
    schedule_sector_intraday_fund_flow_backfill,
)

from .risk_appetite import (
    CONFIG_VERSION,
    DEFENSE_FOCUS_VERSION,
    LEVEL_LABELS,
    SECTOR_DEFINITIONS,
    build_risk_appetite_snapshot,
)
from .risk_trajectory import RiskTrajectoryStore, STATE_LABELS
from .sector_attribution import attribute_board_names
from .rotation_radar import (
    ROTATION_CONFIG_VERSION,
    analyze_rotation_snapshots,
    analyze_sector_flow_snapshots,
    normalize_rotation_snapshot,
    sector_flow_backfill_targets,
)
from .rotation_store import RotationRadarStore

logger = logging.getLogger(__name__)

_SNAPSHOT_TTL = 60
_MIN_FORCE_INTERVAL = 30
_BOARD_LEADER_TTL = 90
_BOARD_LEADER_MAX_STALE = 300
_BOARD_LEADER_RETRY_INTERVAL = 30
_BOARD_LEADER_MAX_TARGETS = 14
_BOARD_LEADER_GROUP_TARGETS = 3
_BOARD_LEADER_FLOW_TARGETS = 8
_SECTOR_RESONANCE_MAX_SKEW_SECONDS = 120
_DEFENSE_FOCUS_KEYS = (
    "agriculture",
    "ports",
    "electric_power",
    "oil_gas",
    "precious_metals",
)
_DEFENSE_FOCUS_LIMIT = 3
_COMPONENT_TTLS = {
    "market_breadth": 30,
    "industry_quotes": 60,
    "concept_quotes": 60,
    "leadership_pool": 60,
    "etfs": 300,
    "leaders": 300,
}
_COMPONENT_MAX_STALE = {
    "market_breadth": 90,
    "industry_quotes": 90,
    "concept_quotes": 90,
    "leadership_pool": 120,
    "etfs": 900,
    "leaders": 900,
}
_FAST_COMPONENTS = (
    "market_breadth",
    "industry_quotes",
    "concept_quotes",
    "leadership_pool",
)
_CONTEXT_COMPONENTS = ("etfs", "leaders")

_snapshot_cache: dict[str, Any] | None = None
_snapshot_cached_at = 0.0
_snapshot_trade_date: str | None = None
_component_cache: dict[str, dict[str, Any]] = {}
_component_errors: dict[str, str] = {}
_component_attempted_at: dict[str, float] = {}
_cache_lock = threading.RLock()
_refresh_lock = threading.Lock()
_context_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="risk-context")
_context_futures: dict[str, Future] = {}
_board_leader_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="risk-board-leaders",
)
_board_leader_cache: dict[tuple[str, str], dict[str, Any]] = {}
_board_leader_futures: dict[tuple[str, str], Future] = {}
_board_leader_generation = 0
_board_leader_trade_date: str | None = None
_published_headline: dict[str, Any] | None = None
_pending_headline_key: tuple[str, str] | None = None
_pending_headline_count = 0
_trajectory_store: RiskTrajectoryStore | None = None
_trajectory_store_lock = threading.Lock()
_rotation_store: RotationRadarStore | None = None
_rotation_store_lock = threading.Lock()


def _fetch_market_breadth() -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    snapshot = fetch_market_breadth_snapshot()
    return (
        market_breadth_to_legacy_records(snapshot),
        snapshot.metadata.provider,
        metadata_to_component_status(snapshot.metadata),
    )


def _fetch_board_quotes(
    board_type: str,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    series = fetch_sector_quotes(board_type)
    return (
        sector_quotes_to_legacy_records(series),
        series.metadata.provider,
        metadata_to_component_status(series.metadata),
    )


def _fetch_etfs() -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    series = fetch_etf_quotes()
    return (
        etf_quotes_to_legacy_records(series),
        series.metadata.provider,
        metadata_to_component_status(series.metadata),
    )


def _fetch_leaders() -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    codes = list(dict.fromkeys(
        leader.code
        for definition in SECTOR_DEFINITIONS
        for leader in definition.leaders
    ))
    series = fetch_leader_quotes(codes)
    return (
        leader_quotes_to_legacy_records(series),
        series.metadata.provider,
        metadata_to_component_status(series.metadata),
    )


def _fetch_leadership_pool(
    trade_date: str,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader, read_profiles

    series = fetch_limit_up_events(trade_date)
    records = limit_event_series_to_legacy_records(series)
    source = series.metadata.provider
    metadata = limit_event_series_to_component_metadata(series)
    codes = [str(row.get("代码") or row.get("code") or "").strip() for row in records]

    profile_status: dict[str, Any] = {
        "status": "ready" if not records else "degraded",
        "source": None,
        "source_valid": not records,
        "eligible_for_attribution": not records,
        "requested_total": len(records),
        "returned_total": 0,
        "coverage": 1.0 if not records else 0.0,
        "provider_as_of": None,
        "error": None,
    }
    enriched_records = [dict(record) for record in records]
    if records:
        try:
            requested_ids = tuple(
                f"{code}.{'SH' if code.startswith('6') else 'BJ' if code.startswith(('4', '8')) else 'SZ'}"
                for code in codes
            )
            relationships = read_profiles(requested_ids)
            relationships_by_code = {
                instrument_id[:6]: profile
                for instrument_id, profile in relationships.items()
            }
            with InstrumentTaxonomyReader() as taxonomy_reader:
                taxonomy_status = taxonomy_reader.status()
            profile_series = fetch_stock_sector_profiles(
                codes,
                trade_date=trade_date,
            )
            profile_records = stock_sector_profiles_to_legacy_records(profile_series)
            profile_codes = [profile["代码"] for profile in profile_records]
            profiles_by_code = dict(zip(profile_codes, profile_records, strict=True))
            for record in enriched_records:
                code = str(record.get("代码") or record.get("code") or "").strip()
                profile = profiles_by_code[code]
                relationship = relationships_by_code.get(code)
                record["sector_profile"] = {
                    "industry": profile.get("行业"),
                    "region": profile.get("地域"),
                    # Concepts are retained for audit context, but only the
                    # stable f100 industry contributes attribution.  A broad
                    # concept membership alone does not prove today's theme.
                    "concept_tags": copy.deepcopy(profile.get("概念标签") or []),
                    "source": profile.get("source") or profile_series.metadata.provider,
                    "provider_as_of": profile.get("provider_as_of"),
                    "fetched_at": profile_series.metadata.fetched_at.isoformat(
                        timespec="seconds"
                    ),
                }
                if relationship is not None:
                    record["stock_relationship"] = {
                        "primary_business": relationship.primary_business_name,
                        "business_tags": list(relationship.business_tags),
                        "statistical_industry": (
                            relationship.statistical_industry.level3_name
                            if relationship.statistical_industry
                            else None
                        ),
                        "business_verification_status": relationship.verification_status,
                        "relationship_catalog_revision": (
                            taxonomy_status.catalog_revision if taxonomy_status else None
                        ),
                    }
            profile_provider_as_of = (
                profile_series.metadata.provider_as_of.isoformat(timespec="seconds")
                if profile_series.metadata.provider_as_of
                else None
            )
            profile_status = {
                "status": "ready",
                "source": profile_series.metadata.provider,
                "source_valid": True,
                "eligible_for_attribution": True,
                "requested_total": len(codes),
                "returned_total": len(profile_records),
                "coverage": 1.0,
                "provider_as_of": profile_provider_as_of,
                "fetched_at": profile_series.metadata.fetched_at.isoformat(
                    timespec="seconds"
                ),
                "error": None,
                "relationship_catalog_revision": (
                    taxonomy_status.catalog_revision if taxonomy_status else None
                ),
                "relationship_coverage": (
                    len(relationships) / len(requested_ids) if requested_ids else 1.0
                ),
            }
        except Exception as exc:  # noqa: BLE001 - reason attribution remains usable
            logger.warning("stock sector profile enrichment failed: %s", exc)
            profile_status["error"] = str(exc)
    metadata["industry_profile_status"] = profile_status
    return enriched_records, source, metadata


def _fetch_board_leader_snapshot(
    board_code: str,
    source_hint: str | None = None,
) -> dict[str, Any]:
    return board_leader_snapshot_to_legacy_payload(
        fetch_board_leader_snapshot(
            board_code,
            limit=10,
            source_hint=source_hint,
        )
    )


def _provider_as_of(records: list[dict[str, Any]]) -> str | None:
    values = [
        str(value).strip()
        for record in records
        if (value := _first_present(record, "provider_as_of", "更新时间"))
    ]
    return max(values) if values else None


def _component_status(
    entry: dict[str, Any],
    *,
    now_mono: float,
    stale: bool,
    error: str | None = None,
    expired: bool = False,
    refreshing: bool = False,
    closed_session_fallback: bool = False,
) -> dict[str, Any]:
    return {
        "source": entry.get("source"),
        "fetched_at": entry.get("fetched_at"),
        "as_of": entry.get("fetched_at"),
        "provider_as_of": entry.get("provider_as_of"),
        "provider_request_id": entry.get("provider_request_id"),
        "contract": entry.get("contract"),
        "schema_version": entry.get("schema_version"),
        "quality": entry.get("quality"),
        "quality_flags": copy.deepcopy(entry.get("quality_flags") or []),
        "partial": bool(entry.get("partial", False)),
        "cache_identity": entry.get("cache_identity"),
        "source_valid": entry.get("source_valid", True),
        "data_date": entry.get("data_date"),
        "trade_status": copy.deepcopy(entry.get("trade_status")),
        "pool_total": entry.get("pool_total"),
        "unique_total": entry.get("unique_total"),
        "reason_coverage": entry.get("reason_coverage"),
        "board_count_coverage": entry.get("board_count_coverage"),
        "unknown_board_count": entry.get("unknown_board_count"),
        "industry_profile_status": copy.deepcopy(entry.get("industry_profile_status")),
        "valid_empty": entry.get("valid_empty", False),
        "page_count": entry.get("page_count"),
        "age_seconds": max(0.0, round(now_mono - entry["cached_at"], 1)),
        "stale": stale,
        "expired": expired,
        "refreshing": refreshing,
        "closed_session_fallback": closed_session_fallback,
        "error": error if error is not None else entry.get("last_error"),
    }


def _component(
    name: str,
    fetcher: Callable[
        [],
        tuple[list[dict[str, Any]], str]
        | tuple[list[dict[str, Any]], str, dict[str, Any]],
    ],
    *,
    force: bool,
    cache_identity: str | None = None,
    closed_trade_date: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read one independently cached component with stale-on-error fallback."""

    now_mono = time.monotonic()
    ttl = _COMPONENT_TTLS[name]
    with _cache_lock:
        candidate = _component_cache.get(name)
        cached = (
            candidate
            if candidate and candidate.get("cache_identity") == cache_identity
            else None
        )
        if cached and (not force or now_mono - cached["cached_at"] < _MIN_FORCE_INTERVAL):
            if now_mono - cached["cached_at"] < ttl:
                return copy.deepcopy(cached["records"]), _component_status(
                    cached,
                    now_mono=now_mono,
                    stale=cached.get("last_error") is not None,
                )

    try:
        with _cache_lock:
            _component_attempted_at[name] = time.monotonic()
        fetched = fetcher()
        if len(fetched) == 3:
            records, source, metadata = fetched
        else:
            records, source = fetched
            metadata = {}
        fetched_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
        entry = {
            "records": records,
            "source": source,
            "fetched_at": fetched_at,
            "provider_as_of": _provider_as_of(records),
            "cached_at": time.monotonic(),
            "last_error": None,
            "cache_identity": cache_identity,
            **metadata,
        }
        with _cache_lock:
            _component_cache[name] = entry
            _component_errors.pop(name, None)
        return copy.deepcopy(records), _component_status(
            entry,
            now_mono=entry["cached_at"],
            stale=False,
        )
    except Exception as exc:  # noqa: BLE001 - partial responses are intentional
        logger.warning("risk appetite component %s failed: %s", name, exc)
        with _cache_lock:
            candidate = _component_cache.get(name)
            cached = (
                candidate
                if candidate and candidate.get("cache_identity") == cache_identity
                else None
            )
            _component_errors[name] = str(exc)
        if cached:
            with _cache_lock:
                cached["last_error"] = str(exc)
            age = time.monotonic() - cached["cached_at"]
            hard_expired = age > _COMPONENT_MAX_STALE[name]
            closed_session_fallback = bool(
                hard_expired
                and closed_trade_date
                and name in {"industry_quotes", "concept_quotes"}
                and _provider_matches_date(
                    cached.get("provider_as_of"),
                    closed_trade_date,
                )
                and bool(cached.get("records"))
                and all(
                    _provider_matches_date(
                        _first_present(record, "provider_as_of", "更新时间"),
                        closed_trade_date,
                    )
                    for record in cached["records"]
                )
            )
            expired = hard_expired and not closed_session_fallback
            return ([] if expired else copy.deepcopy(cached["records"])), _component_status(
                cached,
                now_mono=time.monotonic(),
                stale=True,
                expired=expired,
                error=str(exc),
                closed_session_fallback=closed_session_fallback,
            )
        return [], {
            "source": None,
            "fetched_at": None,
            "as_of": None,
            "provider_as_of": None,
            "provider_request_id": None,
            "contract": None,
            "schema_version": None,
            "quality": None,
            "quality_flags": [],
            "partial": False,
            "cache_identity": cache_identity,
            "source_valid": False,
            "data_date": None,
            "trade_status": None,
            "pool_total": None,
            "unique_total": None,
            "reason_coverage": None,
            "board_count_coverage": None,
            "unknown_board_count": None,
            "industry_profile_status": None,
            "valid_empty": False,
            "page_count": None,
            "age_seconds": None,
            "stale": False,
            "expired": False,
            "refreshing": False,
            "closed_session_fallback": False,
            "error": str(exc),
        }


def _context_jobs() -> dict[str, Callable[[], tuple[Any, ...]]]:
    return {
        "etfs": _fetch_etfs,
        "leaders": _fetch_leaders,
    }


def _derive_sector_flow_components(
    values: dict[str, list[dict[str, Any]]],
    statuses: dict[str, dict[str, Any]],
) -> None:
    """Expose quote-embedded fund flow through the legacy component names."""

    for quote_name, flow_name in (
        ("industry_quotes", "industry_flow"),
        ("concept_quotes", "concept_flow"),
    ):
        values[flow_name] = copy.deepcopy(values.get(quote_name, []))
        status = copy.deepcopy(statuses.get(quote_name, {}))
        status["derived_from"] = quote_name
        statuses[flow_name] = status


def _clear_context_future(name: str, future: Future) -> None:
    try:
        future.result()
    except Exception:  # noqa: BLE001 - _component normally converts failures
        logger.exception("risk appetite context task %s crashed", name)
    finally:
        with _cache_lock:
            if _context_futures.get(name) is future:
                _context_futures.pop(name, None)


def _refresh_context_async(*, force: bool) -> dict[str, Future]:
    """Start eligible context refreshes and return the in-flight work.

    Ordinary dashboard callers intentionally ignore the returned futures and
    keep the existing stale-while-refreshing behaviour.  The independent lake
    collector can wait for this exact set before observing the calculation,
    which avoids both a second network sweep and a cold-process empty capture.
    """

    in_flight: dict[str, Future] = {}
    for name, fetcher in _context_jobs().items():
        with _cache_lock:
            future = _context_futures.get(name)
            if future is not None and not future.done():
                in_flight[name] = future
                continue
            cached = _component_cache.get(name)
            age = time.monotonic() - cached["cached_at"] if cached else None
            attempted_at = _component_attempted_at.get(name)
            attempt_age = time.monotonic() - attempted_at if attempted_at is not None else None
            if cached and age is not None:
                if force and age < _MIN_FORCE_INTERVAL:
                    continue
                if not force and age < _COMPONENT_TTLS[name]:
                    continue
            elif attempt_age is not None:
                if force and attempt_age < _MIN_FORCE_INTERVAL:
                    continue
                if not force and attempt_age < _COMPONENT_TTLS[name]:
                    continue
            future = _context_executor.submit(_component, name, fetcher, force=force)
            _context_futures[name] = future
            in_flight[name] = future
            future.add_done_callback(
                lambda completed, component_name=name: _clear_context_future(
                    component_name,
                    completed,
                )
            )
    return in_flight


def _wait_for_context_futures(futures: dict[str, Future]) -> None:
    """Wait for already-submitted context work without holding cache locks."""

    for name, future in futures.items():
        try:
            future.result()
        except Exception:  # noqa: BLE001 - component failures remain partial data
            logger.exception("risk appetite context task %s crashed", name)
        finally:
            # The done callback normally removes this entry.  ``result()`` may
            # return before that callback has acquired the lock, so remove the
            # same future here as well to make the captured status final rather
            # than spuriously ``refreshing``.
            with _cache_lock:
                if _context_futures.get(name) is future and future.done():
                    _context_futures.pop(name, None)


def _cached_component(name: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    now_mono = time.monotonic()
    with _cache_lock:
        cached = _component_cache.get(name)
        refreshing = name in _context_futures
        if cached is None:
            return [], {
                "source": None,
                "fetched_at": None,
                "as_of": None,
                "provider_as_of": None,
                "age_seconds": None,
                "stale": False,
                "expired": False,
                "refreshing": refreshing,
                "error": _component_errors.get(name),
            }
        age = now_mono - cached["cached_at"]
        expired = age > _COMPONENT_MAX_STALE[name]
        stale = age >= _COMPONENT_TTLS[name] or cached.get("last_error") is not None
        records = [] if expired else copy.deepcopy(cached["records"])
        status = _component_status(
            cached,
            now_mono=now_mono,
            stale=stale,
            expired=expired,
            refreshing=refreshing,
        )
    return records, status


def _level_summary(level: str) -> dict[str, str]:
    tone = {"strong": "positive", "weak": "negative"}.get(level, "neutral")
    return {"key": level, "label": LEVEL_LABELS.get(level, "数据不足"), "tone": tone}


def _first_present(record: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    return None


def _resource_summary(groups: dict[str, dict[str, Any]]) -> dict[str, str]:
    levels = [
        groups["event_hedge"]["level"],
        groups["cyclical_resource"]["level"],
    ]
    if "strong" in levels:
        return _level_summary("strong")
    if levels and all(level == "weak" for level in levels):
        return _level_summary("weak")
    if all(level == "unknown" for level in levels):
        return _level_summary("unknown")
    return _level_summary("medium")


def _structure_reason(structure: str) -> str:
    return {
        "金融进攻": "证券与互联网金融形成进攻共振。",
        "银行护盘": "银行走强，但金融进攻核心尚未同步。",
        "防御升温": "稳态防御或事件避险方向相对占优。",
        "资源通胀": "周期资源走强，更接近资源或通胀交易。",
        "消费修复": "白酒与零售方向出现消费修复共振。",
        "其他主线": "市场参与度较强，主线不在当前观察篮子。",
        "普遍退潮": "市场参与度转弱，防守方向也未形成承接。",
        "分化观察": "当前证据存在分化，暂不输出单一方向。",
    }.get(structure, "当前证据不足。")


def _market_reason(level: str) -> str:
    return {
        "strong": "市场广度、指数参与和同期量能证据整体偏强。",
        "weak": "市场广度、指数参与和同期量能证据整体偏弱。",
        "medium": "全市场参与度处于中性或内部不一致。",
        "unknown": "全市场参与度数据尚不完整。",
    }[level]


def _merge_leader_quotes(snapshot: dict[str, Any], leader_records: list[dict[str, Any]]) -> None:
    by_code = {str(row.get("代码", "")).zfill(6): row for row in leader_records}
    for sector in snapshot["sectors"].values():
        merged = []
        for leader in sector["leaders"]:
            quote = by_code.get(leader["code"], {})
            merged.append({
                **leader,
                "change_pct": quote.get("涨跌幅"),
                "price": quote.get("最新价"),
            })
        sector["leaders"] = merged


def _membership_from_sector_profile(
    profile: Any,
    relationship: Any = None,
) -> dict[str, Any]:
    if not isinstance(profile, dict) or not str(profile.get("industry") or "").strip():
        return {
            "status": "unavailable",
            "source": None,
            "provider_as_of": None,
            "fetched_at": None,
            "derived_from": "stock_sector_profile.v1",
            "reason": "sector_profile_unavailable",
        }

    raw_concepts = profile.get("concept_tags")
    if isinstance(raw_concepts, str):
        concepts = [item.strip() for item in raw_concepts.split(",") if item.strip()]
    elif isinstance(raw_concepts, (list, tuple, set)):
        concepts = [str(item).strip() for item in raw_concepts if str(item).strip()]
    else:
        concepts = []
    relationship = relationship if isinstance(relationship, dict) else {}
    raw_business_tags = relationship.get("business_tags")
    business_tags = (
        [str(item).strip() for item in raw_business_tags if str(item).strip()]
        if isinstance(raw_business_tags, (list, tuple, set))
        else []
    )
    board_names = list(dict.fromkeys(
        value
        for raw in (
            profile.get("industry"),
            relationship.get("statistical_industry"),
            profile.get("region"),
            *concepts,
            *business_tags,
        )
        if (value := str(raw or "").strip())
    ))
    attribution = attribute_board_names(board_names)
    result = {
        "status": "ready",
        "source": profile.get("source"),
        "provider_as_of": profile.get("provider_as_of"),
        "fetched_at": profile.get("fetched_at"),
        "derived_from": "stock_sector_profile.v1",
        "board_names": board_names,
        "parsed_tags": attribution.get("parsed_tags", []),
        "matched_tags": attribution.get("matched_tags", []),
        "attributions": attribution.get("attributions", []),
        "unmapped_tags": attribution.get("unmapped_tags", []),
        "taxonomy_version": attribution.get("taxonomy_version"),
    }
    if relationship:
        result.update({
            "primary_business": relationship.get("primary_business"),
            "business_tags": business_tags,
            "statistical_industry": relationship.get("statistical_industry"),
            "business_verification_status": relationship.get(
                "business_verification_status"
            ),
            "relationship_catalog_revision": relationship.get(
                "relationship_catalog_revision"
            ),
        })
    return result


def _attach_static_memberships(snapshot: dict[str, Any]) -> None:
    """Derive display-only membership from the already fetched batch profile."""

    for sector in snapshot.get("sectors", {}).values():
        for leader in sector.get("leadership", {}).get("limit_up_leaders", []):
            leader["static_membership"] = _membership_from_sector_profile(
                leader.get("sector_profile"),
                leader.get("stock_relationship"),
            )


def _normalise_board_code(value: Any) -> str | None:
    code = str(value or "").strip().upper()
    return code if code.startswith("BK") and code[2:].isdigit() else None


def _candidate_board_code(candidate: dict[str, Any]) -> str | None:
    representative = candidate.get("representative_board")
    if isinstance(representative, dict):
        code = _normalise_board_code(
            _first_present(representative, "board_code", "code", "板块代码")
        )
        if code:
            return code
    for field in ("leader_board_code", "representative_board_code", "board_code"):
        code = _normalise_board_code(candidate.get(field))
        if code:
            return code
    key = str(candidate.get("key") or candidate.get("id") or "")
    return _normalise_board_code(key.rsplit(":", 1)[-1])


def _candidate_board_name(candidate: dict[str, Any]) -> str | None:
    representative = candidate.get("representative_board")
    if isinstance(representative, dict):
        value = _first_present(representative, "name", "label", "板块名称")
        if value:
            return str(value).strip() or None
    value = _first_present(candidate, "matched_name", "label", "name")
    return str(value).strip() if value else None


def _candidate_taxonomy(candidate: dict[str, Any]) -> str | None:
    representative = candidate.get("representative_board")
    if isinstance(representative, dict) and representative.get("taxonomy"):
        return str(representative["taxonomy"])
    value = candidate.get("taxonomy")
    return str(value) if value else None


def _core_offense_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    core = (result.get("offense") or {}).get("core") or {}
    items = core.get("items") if isinstance(core, dict) else None
    if isinstance(items, dict):
        return [item for item in items.values() if isinstance(item, dict)]
    return [item for item in (items or []) if isinstance(item, dict)]


def _radar_offense_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    offense = result.get("offense") or {}
    lists = offense.get("lists") or {}
    items: list[dict[str, Any]] = []
    for key in ("attacking", "rotating", "unclassified", "cooling"):
        items.extend(
            item for item in (lists.get(key) or []) if isinstance(item, dict)
        )
    return items


def _summary_offense_leaders(result: dict[str, Any]) -> list[dict[str, Any]]:
    values = (
        (result.get("offense") or {})
        .get("summary", {})
        .get("current_leaders", [])
    )
    return [item for item in values if isinstance(item, dict)]


def _all_offense_leader_objects(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *_core_offense_items(result),
        *_radar_offense_items(result),
        *_summary_offense_leaders(result),
    ]


def _sector_flow_leader_objects(result: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    # Prioritise offense concepts for constituent enrichment while still
    # applying the provider snapshot fallback to every directional series.
    for field in (
        "offense_sector_flow_trajectory",
        "sector_flow_trajectory",
    ):
        trajectory = result.get(field) or {}
        if not isinstance(trajectory, dict):
            continue
        values.extend(
            item
            for item in trajectory.get("sectors") or []
            if isinstance(item, dict)
        )
    return values


def _provider_leader_snapshot(
    candidate: dict[str, Any],
    board_by_code: dict[str, dict[str, Any]],
    board_by_name: dict[str, dict[str, Any]],
    statuses: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    code = _candidate_board_code(candidate)
    name = _candidate_board_name(candidate)
    row = board_by_code.get(code or "") or board_by_name.get(name or "") or {}
    taxonomy = _candidate_taxonomy(candidate)
    status = statuses.get(f"{taxonomy}_quotes", {}) if taxonomy else {}
    source = row.get("source") or status.get("source")
    provider_as_of = _first_present(row, "provider_as_of", "更新时间")
    leader_name = _first_present(row, "leader_name", "领涨股票")
    leader_code = _first_present(row, "leader_code", "领涨股代码")
    leader_market = _first_present(row, "leader_market", "领涨股市场")
    leader_instrument_id = _first_present(
        row,
        "leader_instrument_id",
        "instrument_id",
    )
    leader_change = _first_present(row, "leader_change_pct", "领涨股涨幅")
    items = []
    if leader_name or leader_code:
        items.append({
            "instrument_id": leader_instrument_id,
            "code": str(leader_code).strip() if leader_code else None,
            "name": str(leader_name).strip() if leader_name else None,
            "price": None,
            "change_pct": leader_change,
            "amount": None,
            "turnover": None,
            "flow_amount": None,
            "flow_ratio": None,
            "provider_as_of": provider_as_of,
            "source": source,
            "role": "price_leader",
            "market": leader_market,
        })
    return {
        "status": "fallback" if items else "unavailable",
        "status_label": "板块快照领涨" if items else "领涨数据暂缺",
        "source": source,
        "provider_as_of": provider_as_of,
        "fetched_at": status.get("fetched_at"),
        "stale": bool(status.get("stale") or status.get("expired")),
        "method": "provider_leader",
        "items": items,
    }


def _finite_market_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _aware_market_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else None


def _sector_flow_leader_snapshot(
    snapshot: dict[str, Any],
    latest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Select one high-resonance stock from speed-ranked board constituents.

    A match requires the board's five-minute fund-flow increment, the stock's
    current main inflow, and the stock's current price speed to all be positive.
    Provider timestamps must also align closely enough to support simultaneity.
    """

    raw_items = snapshot.get("leaders") or snapshot.get("items") or []
    raw_status = str(snapshot.get("status") or "unavailable")
    refreshing = bool(snapshot.get("refreshing"))
    stale = bool(snapshot.get("stale"))
    latest = latest if isinstance(latest, dict) else {}
    sector_flow_delta = _finite_market_number(latest.get("delta_5m_cny"))
    sector_as_of = _aware_market_time(latest.get("provider_as_of"))
    leaders: list[dict[str, Any]] = []
    comparable_items = 0
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        instrument_id = str(
            item.get("instrument_id") or item.get("leader_instrument_id") or ""
        ).strip().upper()
        name = str(item.get("name") or "").strip()
        if (
            len(instrument_id) != 9
            or instrument_id[6] != "."
            or not instrument_id[:6].isdigit()
            or instrument_id[7:] not in {"SH", "SZ", "BJ"}
            or not name
        ):
            continue
        speed_pct = _finite_market_number(item.get("speed_pct"))
        main_net_inflow_cny = _finite_market_number(item.get("flow_amount"))
        stock_as_of = _aware_market_time(item.get("provider_as_of"))
        if speed_pct is None or main_net_inflow_cny is None or stock_as_of is None:
            continue
        comparable_items += 1
        timestamps_align = (
            sector_as_of is not None
            and abs((stock_as_of - sector_as_of).total_seconds())
            <= _SECTOR_RESONANCE_MAX_SKEW_SECONDS
        )
        if (
            sector_flow_delta is None
            or sector_flow_delta <= 0
            or speed_pct <= 0
            or main_net_inflow_cny <= 0
            or not timestamps_align
        ):
            continue
        leaders.append({
            "instrument_id": instrument_id,
            "name": name,
            "change_pct": item.get("change_pct"),
            "speed_pct": speed_pct,
            "main_net_inflow_cny": main_net_inflow_cny,
            "resonance_strength": "high",
            "price": item.get("price"),
            "provider_as_of": item.get("provider_as_of"),
        })
    leaders.sort(
        key=lambda item: (
            -float(item["speed_pct"]),
            -float(item["main_net_inflow_cny"]),
            str(item["instrument_id"]),
        )
    )
    leaders = leaders[:1]
    if leaders:
        status = (
            "stale" if stale or raw_status == "stale"
            else "fallback" if raw_status == "fallback"
            else "full"
        )
    elif sector_flow_delta is not None and sector_flow_delta <= 0:
        status = "no_match"
    elif raw_status in {"ready", "full", "stale"} and comparable_items:
        status = "no_match"
    else:
        status = "loading" if refreshing or raw_status == "loading" else (
            "error" if raw_status == "error" else "unavailable"
        )
    default_label = {
        "full": "高共振股已更新",
        "fallback": "当前高共振股",
        "loading": "共振股加载中",
        "stale": "高共振股数据延迟",
        "no_match": "暂无同时拉升的高共振股",
        "error": "共振股暂不可用",
        "unavailable": "共振条件暂缺",
    }[status]
    return {
        "status": status,
        "status_label": default_label,
        "selection_method": "sector_fund_flow_stock_speed.v1",
        "marginal_window_minutes": 5,
        "source": str(snapshot.get("source")) if snapshot.get("source") else None,
        "provider_as_of": snapshot.get("provider_as_of"),
        "stale": stale,
        "refreshing": refreshing,
        "leaders": leaders,
    }


def _attach_provider_leader_fallbacks(
    result: dict[str, Any],
    board_records: list[dict[str, Any]],
    statuses: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    board_by_code = {
        code: row
        for row in board_records
        if (code := _normalise_board_code(
            _first_present(row, "board_code", "板块代码", "code")
        ))
    }
    board_by_name = {
        str(name).strip(): row
        for row in board_records
        if (name := _first_present(row, "name", "板块名称", "板块"))
        and str(name).strip()
    }
    for candidate in _all_offense_leader_objects(result):
        candidate["leader_snapshot"] = _provider_leader_snapshot(
            candidate,
            board_by_code,
            board_by_name,
            statuses,
        )
    for candidate in _sector_flow_leader_objects(result):
        candidate["leader_snapshot"] = _sector_flow_leader_snapshot(
            _provider_leader_snapshot(
                candidate,
                board_by_code,
                board_by_name,
                statuses,
            ),
            candidate.get("latest"),
        )
    return result


def _prepare_board_leader_trade_date(trade_date: str) -> None:
    global _board_leader_generation, _board_leader_trade_date

    with _cache_lock:
        if _board_leader_trade_date == trade_date:
            return
        _board_leader_generation += 1
        for future in _board_leader_futures.values():
            future.cancel()
        _board_leader_futures.clear()
        _board_leader_cache.clear()
        _board_leader_trade_date = trade_date


def _complete_board_leader_fetch(
    cache_key: tuple[str, str],
    generation: int,
    future: Future,
) -> None:
    now_mono = time.monotonic()
    try:
        result = future.result()
        error = None
    except Exception as exc:  # noqa: BLE001 - enrichment is display-only
        logger.warning("board leader enrichment %s failed: %s", cache_key[1], exc)
        result = None
        error = str(exc)

    with _cache_lock:
        if generation != _board_leader_generation:
            return
        if _board_leader_futures.get(cache_key) is future:
            _board_leader_futures.pop(cache_key, None)
        if result is not None:
            _board_leader_cache[cache_key] = {
                **result,
                "cached_at": now_mono,
                "last_attempt_at": now_mono,
                "last_error": None,
            }
            return
        cached = _board_leader_cache.get(cache_key)
        if cached and cached.get("items"):
            cached["last_attempt_at"] = now_mono
            cached["last_error"] = error
        else:
            _board_leader_cache[cache_key] = {
                "status": "error",
                "source": None,
                "provider_as_of": None,
                "fetched_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(
                    timespec="seconds"
                ),
                "stale": False,
                "method": "board_constituents",
                "items": [],
                "cached_at": now_mono,
                "last_attempt_at": now_mono,
                "last_error": error,
            }


def _schedule_board_leader_fetch(
    trade_date: str,
    board_code: str,
    now_mono: float,
    *,
    refresh_existing: bool = True,
    source_hint: str | None = None,
) -> bool:
    cache_key = (trade_date, board_code)
    with _cache_lock:
        if cache_key in _board_leader_futures:
            return True
        cached = _board_leader_cache.get(cache_key)
        if cached and not refresh_existing:
            return False
        if cached and cached.get("items"):
            due = now_mono - float(cached.get("cached_at", 0.0)) >= _BOARD_LEADER_TTL
        elif cached:
            due = (
                now_mono - float(cached.get("last_attempt_at", 0.0))
                >= _BOARD_LEADER_RETRY_INTERVAL
            )
        else:
            due = True
        if not due:
            return False
        if len(_board_leader_futures) >= _BOARD_LEADER_MAX_TARGETS:
            return False
        generation = _board_leader_generation
        future = _board_leader_executor.submit(
            _fetch_board_leader_snapshot,
            board_code,
            source_hint,
        )
        _board_leader_futures[cache_key] = future
        future.add_done_callback(
            lambda completed, key=cache_key, current_generation=generation: (
                _complete_board_leader_fetch(key, current_generation, completed)
            )
        )
        return True


def _leader_snapshot_from_cache(
    trade_date: str,
    board_code: str,
    now_mono: float,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    cache_key = (trade_date, board_code)
    with _cache_lock:
        completed = _board_leader_futures.get(cache_key)
        generation = _board_leader_generation
    # Future.result() may wake its caller just before registered callbacks run.
    # Finalise a completed task synchronously so the response cannot regress
    # from a provider fallback back to a spurious "pending" state.
    if completed is not None and completed.done():
        _complete_board_leader_fetch(cache_key, generation, completed)
    with _cache_lock:
        cached = copy.deepcopy(_board_leader_cache.get(cache_key))
        pending = cache_key in _board_leader_futures
    if not cached:
        return None, None, pending
    error = cached.get("last_error")
    if not cached.get("items"):
        return None, error, pending
    age = max(0.0, now_mono - float(cached.get("cached_at", 0.0)))
    wall_now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    market_open = _board_leader_market_open(wall_now)
    if market_open and age > _BOARD_LEADER_MAX_STALE:
        return None, error or "board leader cache expired", pending
    provider_stale = _board_leader_provider_stale(
        cached.get("provider_as_of"),
        trade_date,
        wall_now,
    )
    stale = (market_open and age >= _BOARD_LEADER_TTL) or provider_stale or bool(error)
    snapshot = {
        key: copy.deepcopy(cached.get(key))
        for key in (
            "source",
            "provider_as_of",
            "fetched_at",
            "method",
            "items",
        )
    }
    snapshot.update({
        "status": "stale" if stale else "full",
        "status_label": "领涨数据延迟" if stale else "成分股已更新",
        "stale": stale,
    })
    if error:
        snapshot["error"] = error
    if pending:
        snapshot["refreshing"] = True
    return snapshot, error, pending


def _unique_board_codes(items: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for item in items:
        code = _candidate_board_code(item)
        if code and code not in result:
            result.append(code)
    return result


def _board_leader_market_open(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    current = now.time()
    return (
        clock_time(9, 30) <= current <= clock_time(11, 30)
        or clock_time(13, 0) <= current <= clock_time(15, 0)
    )


def _board_leader_provider_stale(
    provider_as_of: Any,
    trade_date: str,
    now: datetime,
) -> bool:
    if not provider_as_of:
        return _board_leader_market_open(now)
    try:
        provider = datetime.fromisoformat(str(provider_as_of))
    except (TypeError, ValueError):
        return True
    if provider.tzinfo is None:
        provider = provider.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    provider = provider.astimezone(ZoneInfo("Asia/Shanghai"))
    if provider.date().isoformat() != trade_date:
        return True
    if not _board_leader_market_open(now):
        return False
    lag = (now - provider).total_seconds()
    return lag > _BOARD_LEADER_MAX_STALE or lag < -120


def _core_leader_priority(item: dict[str, Any]) -> tuple[int, int, int, str]:
    level = str(item.get("level") or "unknown")
    direction = _rotation_direction(item.get("direction"))
    level_rank = {"strong": 0, "medium": 1, "weak": 2}.get(level, 3)
    direction_rank = {
        "strengthening": 0,
        "flat": 1,
        "weakening": 2,
        "unknown": 3,
    }.get(direction, 3)
    priority = 0 if level == "strong" else 1 if direction == "strengthening" else 2
    return (priority, level_rank, direction_rank, str(item.get("key") or ""))


def _board_leader_source_hints(result: dict[str, Any]) -> dict[str, str]:
    hints: dict[str, str] = {}
    for item in (
        *_sector_flow_leader_objects(result),
        *_all_offense_leader_objects(result),
    ):
        code = _candidate_board_code(item)
        snapshot = item.get("leader_snapshot")
        source = snapshot.get("source") if isinstance(snapshot, dict) else None
        if code and source and code not in hints:
            hints[code] = str(source)
    return hints


def _board_leader_targets(result: dict[str, Any]) -> list[str]:
    core_items = sorted(_core_offense_items(result), key=_core_leader_priority)
    core_codes = _unique_board_codes(core_items)
    radar_items = _radar_offense_items(result)
    radar_codes = _unique_board_codes(radar_items)
    for code in _unique_board_codes(_summary_offense_leaders(result)):
        if code not in radar_codes:
            radar_codes.append(code)

    flow_items = [
        item
        for item in _sector_flow_leader_objects(result)
        if isinstance((item.get("latest") or {}).get("change_pct"), (int, float))
        and (item.get("latest") or {}).get("change_pct") > 0
    ]
    flow_codes = _unique_board_codes(flow_items)[:_BOARD_LEADER_FLOW_TARGETS]

    selected: list[str] = list(flow_codes)
    for code in core_codes[:_BOARD_LEADER_GROUP_TARGETS]:
        if code not in selected:
            selected.append(code)
    radar_count = 0
    for code in radar_codes:
        if code in selected:
            continue
        selected.append(code)
        radar_count += 1
        if radar_count >= _BOARD_LEADER_GROUP_TARGETS:
            break
    for code in (*core_codes, *radar_codes):
        if len(selected) >= _BOARD_LEADER_MAX_TARGETS:
            break
        if code not in selected:
            selected.append(code)
    return selected[:_BOARD_LEADER_MAX_TARGETS]


def _overlay_board_leaders(
    result: dict[str, Any],
    trade_date: str,
) -> dict[str, Any]:
    """Overlay asynchronous leader data on every response, including cache hits."""

    _prepare_board_leader_trade_date(trade_date)
    now_mono = time.monotonic()
    wall_now = datetime.now(ZoneInfo("Asia/Shanghai"))
    market_open = _board_leader_market_open(wall_now)
    targets = _board_leader_targets(result)
    source_hints = _board_leader_source_hints(result)
    for code in targets:
        _schedule_board_leader_fetch(
            trade_date,
            code,
            now_mono,
            refresh_existing=market_open,
            source_hint=source_hints.get(code),
        )

    snapshots: dict[str, tuple[dict[str, Any] | None, str | None, bool]] = {
        code: _leader_snapshot_from_cache(
            trade_date,
            code,
            now_mono,
            now=wall_now,
        )
        for code in targets
    }
    for candidate in _all_offense_leader_objects(result):
        code = _candidate_board_code(candidate)
        if code not in snapshots:
            continue
        enriched, error, pending = snapshots[code]
        if enriched is not None:
            candidate["leader_snapshot"] = copy.deepcopy(enriched)
            continue
        fallback = copy.deepcopy(candidate.get("leader_snapshot") or {
            "status": "unavailable",
            "source": None,
            "provider_as_of": None,
            "fetched_at": None,
            "stale": False,
            "method": "provider_leader",
            "items": [],
        })
        if pending:
            fallback["status"] = "fallback" if fallback.get("items") else "loading"
            fallback["status_label"] = (
                "板块快照领涨，成分股加载中"
                if fallback.get("items")
                else "成分股加载中"
            )
            fallback["refreshing"] = True
        elif error:
            fallback["status"] = "fallback" if fallback.get("items") else "error"
            fallback["status_label"] = (
                "板块快照领涨，成分股补全失败"
                if fallback.get("items")
                else "领涨数据暂缺"
            )
            fallback["error"] = error
        candidate["leader_snapshot"] = fallback
    for candidate in _sector_flow_leader_objects(result):
        code = _candidate_board_code(candidate)
        if code not in snapshots:
            continue
        enriched, error, pending = snapshots[code]
        if enriched is not None:
            candidate["leader_snapshot"] = _sector_flow_leader_snapshot(
                enriched,
                candidate.get("latest"),
            )
            continue
        fallback = copy.deepcopy(candidate.get("leader_snapshot") or {
            "status": "unavailable",
            "status_label": "领涨股暂缺",
            "source": None,
            "provider_as_of": None,
            "stale": False,
            "refreshing": False,
            "leaders": [],
        })
        leaders = list(fallback.get("leaders") or [])
        if pending:
            fallback["status"] = "fallback" if leaders else "loading"
            fallback["status_label"] = (
                "当前领涨股，成分股补全中"
                if leaders else "领涨股加载中"
            )
            fallback["refreshing"] = True
        elif error:
            fallback["status"] = "fallback" if leaders else "error"
            fallback["status_label"] = (
                "当前领涨股，成分股补全失败"
                if leaders else "领涨股暂不可用"
            )
            fallback["refreshing"] = False
        candidate["leader_snapshot"] = fallback
    return result


def _item_view(sector: dict[str, Any]) -> dict[str, Any]:
    metrics = sector["metrics"]
    etf = sector.get("etf")
    etf_view = None
    if etf:
        etf_view = {
            "code": _first_present(etf, "etf_code", "代码", "code"),
            "name": _first_present(etf, "name", "名称"),
            "change_pct": _first_present(etf, "change_pct", "涨跌幅"),
            "amount": _first_present(etf, "amount", "成交额"),
        }
    return {
        "key": sector["key"],
        "label": sector["label"],
        "state": _level_summary(sector["level"]),
        "taxonomy": sector.get("taxonomy"),
        "matched_name": sector.get("matched_alias"),
        "board_code": sector.get("board_code"),
        "index_level": metrics.get("index_level"),
        "change_pct": metrics.get("change_pct"),
        "price_percentile": metrics.get("price_percentile"),
        "breadth_ratio": metrics.get("breadth_ratio"),
        "flow_pct": metrics.get("main_net_ratio"),
        "flow_percentile": metrics.get("fund_flow_percentile"),
        "available_evidence": sector.get("available_evidence", 0),
        "core_evidence_available": sector.get("core_evidence_available", 0),
        "evidence": copy.deepcopy(sector.get("evidence", {})),
        "core_state": _level_summary(sector.get("core_level", "unknown")),
        "strength_profile": sector.get("strength_profile", "mixed"),
        "leadership": copy.deepcopy(sector.get("leadership", {})),
        "etf": etf_view,
        "leaders": sector.get("leaders", []),
        "current_leader": None,
    }


def _defense_focus_view(
    snapshot: dict[str, Any],
    component_status: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    sectors = {
        **snapshot.get("sectors", {}),
        **snapshot.get("defense_focus_sectors", {}),
    }
    configured = [
        sectors[key]
        for key in _DEFENSE_FOCUS_KEYS
        if isinstance(sectors.get(key), dict)
    ]
    candidates = [sector for sector in configured if sector.get("level") == "strong"]
    key_order = {key: position for position, key in enumerate(_DEFENSE_FOCUS_KEYS)}

    def sort_key(sector: dict[str, Any]) -> tuple[Any, ...]:
        evidence = sector.get("evidence", {})
        metrics = sector.get("metrics", {})
        leadership_strong = evidence.get("leadership") == "strong"
        price_percentile = metrics.get("price_percentile")
        breadth_ratio = metrics.get("breadth_ratio")
        flow_percentile = metrics.get("fund_flow_percentile")
        return (
            -int(leadership_strong),
            -(float(price_percentile) if isinstance(price_percentile, (int, float)) else -1.0),
            -(float(breadth_ratio) if isinstance(breadth_ratio, (int, float)) else -1.0),
            -(float(flow_percentile) if isinstance(flow_percentile, (int, float)) else -1.0),
            key_order.get(str(sector.get("key")), len(key_order)),
        )

    candidates.sort(key=sort_key)
    stale = any(
        component_status.get(name, {}).get("stale")
        or component_status.get(name, {}).get("expired")
        for name in _FAST_COMPONENTS
    )
    usable_count = sum(sector.get("level") != "unknown" for sector in configured)
    if candidates:
        status = "stale" if stale else "ready"
        status_label = "最近有效强势" if stale else "当前强势"
    elif usable_count:
        status = "empty"
        status_label = "暂无强势防御细分"
    else:
        status = "unavailable"
        status_label = "防御细分数据暂不可用"

    visible = candidates[:_DEFENSE_FOCUS_LIMIT]
    return {
        "schema_version": snapshot.get("defense_focus_version", DEFENSE_FOCUS_VERSION),
        "mode": "current_state",
        "status": status,
        "status_label": status_label,
        "total_candidates": len(candidates),
        "truncated_count": max(0, len(candidates) - len(visible)),
        "items": [
            {
                "key": sector.get("key"),
                "label": sector.get("label"),
                "state": _level_summary(str(sector.get("level") or "unknown")),
                "matched_name": sector.get("matched_alias"),
                "basis": (
                    "短线梯队强"
                    if sector.get("evidence", {}).get("leadership") == "strong"
                    else "板块三项强"
                    if sector.get("core_level") == "strong"
                    else "综合信号强"
                ),
            }
            for sector in visible
        ],
    }


def _view_model(
    snapshot: dict[str, Any],
    component_status: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    market_level = snapshot["market_participation"]["level"]
    opening = snapshot["opening_observation"]
    emotion_label = "开盘观察" if opening else snapshot["emotion"]
    structure_label = "信号形成中" if opening else snapshot["structure"]
    groups = snapshot["groups"]
    sectors = snapshot["sectors"]

    group_specs = (
        ("financial", "金融板块", ("bank", "securities", "internet_finance")),
        ("stable_defense", "稳态防御", ("electric_power",)),
        ("event_hedge", "事件与通胀", ("precious_metals", "oil_gas", "agriculture")),
        ("cyclical_resource", "周期资源", ("nonferrous", "rare_earth")),
        ("consumer", "消费风格", ("baijiu", "retail")),
    )
    view_groups = []
    for key, label, sector_keys in group_specs:
        items = [_item_view(sectors[sector_key]) for sector_key in sector_keys]
        if key == "financial":
            group_level = groups["financial_core"]["level"]
        elif key == "stable_defense":
            group_level = groups["cashflow_defense"]["level"]
        elif key == "consumer":
            group_level = groups["consumer_stability"]["level"]
        else:
            group_level = groups[key]["level"]
        state = _level_summary(group_level)
        view_groups.append({
            "key": key,
            "label": label,
            "state": state,
            "tone": state["tone"],
            "items": items,
        })

    available_sectors = sum(
        sector.get("core_evidence_available", 0) >= 2 for sector in sectors.values()
    )
    total_sectors = len(sectors)
    stale = any(
        component_status.get(name, {}).get("stale")
        or component_status.get(name, {}).get("expired")
        for name in _FAST_COMPONENTS
    )
    missing_components = [
        name for name, status in component_status.items()
        if status.get("expired")
        or (status.get("error") and not status.get("fetched_at"))
        or (name == "leadership_pool" and not status.get("eligible_for_vote"))
    ]
    industry_profile_status = component_status.get("leadership_pool", {}).get(
        "industry_profile_status"
    )
    if (
        isinstance(industry_profile_status, dict)
        and not industry_profile_status.get("eligible_for_attribution")
    ):
        missing_components.append("industry_profile")
    quality = snapshot["data_quality"]
    quality_partial = quality["level"] != "high" or bool(missing_components) or stale
    effective_quality_level = (
        "medium" if quality["level"] == "high" and quality_partial else quality["level"]
    )
    return {
        "version": snapshot["version"],
        "attribution_version": snapshot.get("attribution_version"),
        "trade_date": snapshot.get("trade_date"),
        "timestamp": snapshot["as_of"],
        "phase": snapshot["phase"],
        "opening_observation": opening,
        "stale": stale,
        "emotion": {
            "key": "opening" if opening else market_level,
            "label": emotion_label,
            "tone": "neutral" if opening else _level_summary(market_level)["tone"],
        },
        "structure": {
            "key": snapshot["structure"],
            "label": structure_label,
            "tone": "neutral" if opening else (
                "positive" if snapshot["structure"] in {"金融进攻", "其他主线", "消费修复"}
                else "negative" if snapshot["structure"] in {"防御升温", "普遍退潮"}
                else "neutral"
            ),
            "reasons": [
                _market_reason(market_level),
                _structure_reason(snapshot["structure"]),
            ],
        },
        "data_quality": {
            "key": effective_quality_level,
            "label": {"high": "数据完整", "medium": "部分数据", "low": "数据不足"}[
                effective_quality_level
            ],
            "partial": quality_partial,
            "stale": stale,
            "available": available_sectors,
            "total": total_sectors,
            "evidence_available": quality["available_evidence"],
            "evidence_total": quality["total_evidence"],
            "missing_components": missing_components,
        },
        "summary": {
            "financial_core": _level_summary(groups["financial_core"]["level"]),
            "bank_support": _level_summary(groups["bank_support"]["level"]),
            "stable_defense": _level_summary(groups["cashflow_defense"]["level"]),
            "resource": _resource_summary(groups),
        },
        "defense_focus": _defense_focus_view(snapshot, component_status),
        "market_participation": snapshot["market_participation"],
        "leadership_pool": copy.deepcopy(snapshot.get("leadership_pool", {})),
        "groups": view_groups,
        "components": component_status,
    }


def _stabilise_headline(result: dict[str, Any]) -> dict[str, Any]:
    """Require two same-session snapshots before changing the published headline."""

    global _published_headline, _pending_headline_key, _pending_headline_count

    if result.get("opening_observation"):
        return result
    current_key = (
        str(result.get("emotion", {}).get("key")),
        str(result.get("structure", {}).get("key")),
    )
    headline_context = (
        str(result.get("version") or CONFIG_VERSION),
        str(result.get("trade_date") or ""),
    )
    if current_key[0] == "unknown":
        return result

    with _cache_lock:
        if (
            _published_headline is None
            or _published_headline.get("context") != headline_context
        ):
            _published_headline = {
                "context": headline_context,
                "key": current_key,
                "emotion": copy.deepcopy(result["emotion"]),
                "structure": copy.deepcopy(result["structure"]),
            }
            _pending_headline_key = None
            _pending_headline_count = 0
            return result
        if current_key == _published_headline["key"]:
            _pending_headline_key = None
            _pending_headline_count = 0
            return result
        if current_key == _pending_headline_key:
            _pending_headline_count += 1
        else:
            _pending_headline_key = current_key
            _pending_headline_count = 1
        if _pending_headline_count >= 2:
            _published_headline = {
                "context": headline_context,
                "key": current_key,
                "emotion": copy.deepcopy(result["emotion"]),
                "structure": copy.deepcopy(result["structure"]),
            }
            _pending_headline_key = None
            _pending_headline_count = 0
            return result

        result["pending_headline"] = {
            "emotion": copy.deepcopy(result["emotion"]),
            "structure": copy.deepcopy(result["structure"]),
            "samples": _pending_headline_count,
            "required_samples": 2,
        }
        result["emotion"] = copy.deepcopy(_published_headline["emotion"])
        result["structure"] = copy.deepcopy(_published_headline["structure"])
        return result


def _get_trajectory_store() -> RiskTrajectoryStore:
    global _trajectory_store
    if _trajectory_store is None:
        with _trajectory_store_lock:
            if _trajectory_store is None:
                _trajectory_store = RiskTrajectoryStore()
    return _trajectory_store


def _get_rotation_store() -> RotationRadarStore:
    global _rotation_store
    if _rotation_store is None:
        with _rotation_store_lock:
            if _rotation_store is None:
                _rotation_store = RotationRadarStore()
    return _rotation_store


def _market_phase(now: datetime, market_data: dict[str, Any]) -> str:
    minute = now.hour * 60 + now.minute
    label = str(market_data.get("market_state", {}).get("label") or "")
    provider_as_of = market_data.get("provider_as_of")
    if provider_as_of and not _provider_matches_date(provider_as_of, now.date().isoformat()):
        return "closed"
    if now.weekday() >= 5 or label in {"周末休市", "今日收盘"}:
        return "closed"
    if minute < 9 * 60 + 30:
        return "pre_open"
    if minute < 9 * 60 + 45:
        return "opening_observation"
    if minute <= 11 * 60 + 30:
        return "trading"
    if minute < 13 * 60:
        return "midday_break"
    if minute <= 15 * 60:
        return "trading"
    return "closed"


def _provider_matches_date(value: Any, trade_date: str) -> bool:
    if value is None:
        return False
    try:
        return datetime.fromisoformat(str(value)).date().isoformat() == trade_date
    except ValueError:
        return False


def _normalise_trade_date(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if len(raw) == 8 and raw.isdigit():
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    try:
        return datetime.fromisoformat(raw).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError:
            return None


def _effective_trade_date(market_data: dict[str, Any], now: datetime) -> str:
    """Choose the provider's current session rather than the wall-clock date."""

    provider_date = _normalise_trade_date(market_data.get("provider_as_of"))
    if provider_date:
        return provider_date
    turnover_date = _normalise_trade_date(
        (market_data.get("market_turnover") or {}).get("today_date")
    )
    return turnover_date or now.date().isoformat()


def _leadership_trade_status_structured(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("id") is not None
        and str(value.get("id")).strip()
        and value.get("name") is not None
        and str(value.get("name")).strip()
    )


def _leadership_trade_status_eligible(value: Any) -> bool:
    """Only score a pool after continuous trading has begun or the day closed."""

    if not _leadership_trade_status_structured(value):
        return False
    status_id = str(value["id"]).strip().lower().replace("-", "_")
    status_name = str(value["name"]).strip().lower()
    disallowed = (
        "盘前",
        "未开盘",
        "集合竞价",
        "休市",
        "停盘",
        "未交易",
        "pre_open",
        "preopen",
        "auction",
        "not_started",
        "closed_day",
    )
    combined = f"{status_id} {status_name}"
    if any(token in combined for token in disallowed):
        return False
    if status_id in {"3", "open", "trade", "trading", "closed", "close"}:
        return True
    return any(
        token in status_name
        for token in ("交易中", "连续交易", "已收盘", "交易结束", "盘后")
    )


def _leadership_contract_valid(status: dict[str, Any]) -> bool:
    """Accept only payloads already validated at the canonical gateway boundary."""

    return bool(
        status.get("contract") == "limit_event.v1"
        and status.get("schema_version") == 1
        and status.get("quality") in {"accepted", "degraded"}
        and status.get("source_valid")
        and _leadership_trade_status_structured(status.get("trade_status"))
    )


def _component_is_fresh(
    name: str,
    statuses: dict[str, dict[str, Any]],
    trade_date: str,
    *,
    require_provider_time: bool,
) -> bool:
    status = statuses.get(name, {})
    if not status.get("fetched_at") or status.get("stale") or status.get("expired"):
        return False
    if require_provider_time:
        return _provider_matches_date(status.get("provider_as_of"), trade_date)
    return True


def _trajectory_inputs(
    snapshot: dict[str, Any],
    statuses: dict[str, dict[str, Any]],
    market_data: dict[str, Any],
    trade_date: str,
) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
    industry_fresh = _component_is_fresh(
        "industry_quotes", statuses, trade_date, require_provider_time=True
    )
    concept_fresh = _component_is_fresh(
        "concept_quotes", statuses, trade_date, require_provider_time=True
    )
    breadth_fresh = _component_is_fresh(
        "market_breadth", statuses, trade_date, require_provider_time=False
    )
    market_provider_fresh = _provider_matches_date(
        market_data.get("provider_as_of"), trade_date
    )
    leadership_status = statuses.get("leadership_pool", {})
    leadership_fresh = (
        _component_is_fresh(
            "leadership_pool", statuses, trade_date, require_provider_time=False
        )
        and _normalise_trade_date(leadership_status.get("data_date")) == trade_date
    )

    sector_fresh: dict[str, bool] = {}
    raw_axes: dict[str, str] = {}
    metrics: dict[str, Any] = {
        "market_participation": snapshot["market_participation"].get("metrics", {}),
    }
    for key, sector in snapshot["sectors"].items():
        taxonomy = sector.get("taxonomy")
        quote_fresh = (
            industry_fresh
            if taxonomy == "industry"
            else concept_fresh
            if taxonomy == "concept"
            else False
        )
        leadership_affected_state = (
            sector.get("evidence", {}).get("leadership") is not None
            and sector.get("level") != sector.get("core_level")
        )
        fresh = quote_fresh and (
            leadership_fresh if leadership_affected_state else True
        )
        sector_fresh[key] = fresh
        raw_axes[f"sector:{key}"] = sector["level"] if fresh else "unknown"
        leadership = sector.get("leadership", {})
        metrics[f"sector:{key}"] = {
            **sector.get("metrics", {}),
            "leadership": {
                "matched_count": leadership.get("matched_count"),
                "max_board_count": leadership.get("max_board_count"),
                "vote": leadership.get("vote"),
                "rank": leadership.get("leadership_rank"),
            },
        }

    market_fresh = (
        breadth_fresh
        and market_provider_fresh
        and snapshot["market_participation"].get("available_evidence", 0) >= 2
    )
    raw_axes["market_participation"] = (
        snapshot["market_participation"]["level"] if market_fresh else "unknown"
    )

    for group_key, group in snapshot["groups"].items():
        dependencies = group.get("sector_keys", [])
        group_fresh = bool(dependencies) and all(sector_fresh.get(key, False) for key in dependencies)
        raw_axes[group_key] = group["level"] if group_fresh else "unknown"

    raw_axes["stable_defense"] = raw_axes.get("cashflow_defense", "unknown")
    resource_levels = [
        raw_axes.get("event_hedge", "unknown"),
        raw_axes.get("cyclical_resource", "unknown"),
    ]
    if all(level == "unknown" for level in resource_levels):
        raw_axes["resource"] = "unknown"
    elif "strong" in resource_levels:
        raw_axes["resource"] = "strong"
    elif all(level == "weak" for level in resource_levels if level != "unknown"):
        raw_axes["resource"] = "weak"
    else:
        raw_axes["resource"] = "medium"

    provider_as_of = {
        "market": market_data.get("provider_as_of"),
        "market_breadth": statuses.get("market_breadth", {}).get("fetched_at"),
        "industry_quotes": statuses.get("industry_quotes", {}).get("provider_as_of"),
        "concept_quotes": statuses.get("concept_quotes", {}).get("provider_as_of"),
        "industry_flow": statuses.get("industry_flow", {}).get("provider_as_of"),
        "concept_flow": statuses.get("concept_flow", {}).get("provider_as_of"),
        "leadership_pool": statuses.get("leadership_pool", {}).get("data_date"),
    }
    return raw_axes, metrics, provider_as_of


def _axis_path(
    series: list[dict[str, Any]],
    axis: str,
    last_sample_at: str | None,
) -> list[dict[str, str]]:
    if not last_sample_at:
        return []
    cutoff = datetime.fromisoformat(last_sample_at) - timedelta(minutes=10)
    path: list[dict[str, str]] = []
    for point in series:
        at = point.get("minute_bucket")
        if not at or datetime.fromisoformat(at) < cutoff:
            continue
        state = point.get("confirmed_axes", {}).get(axis, "unknown")
        if state == "unknown":
            continue
        if not path or path[-1]["state"] != state:
            path.append({"state": state, "label": STATE_LABELS[state], "at": at})
    return path


def _axis_view(
    current: dict[str, Any],
    axis: str,
) -> dict[str, Any]:
    value = current.get("axes", {}).get(axis, {})
    direction = value.get("direction", "insufficient")
    direction_labels = {
        "strengthening": "相对增强",
        "weakening": "相对转弱",
        "flat": "相对持平",
        "mixed": "区间反复",
        "insufficient": "趋势待积累",
    }
    pending = value.get("pending") or {}
    return {
        "current_state": value.get("raw", "unknown"),
        "confirmed_state": value.get("confirmed", "unknown"),
        "direction": direction,
        "direction_label": direction_labels.get(direction, "趋势待积累"),
        "summary": value.get("summary", "近10分钟有效确认样本不足。"),
        "path": _axis_path(
            current.get("series", []),
            axis,
            current.get("last_sample_at"),
        ),
        "pending_state": pending.get("candidate"),
    }


def _dynamic_copy(axis: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in axis.items()}


def _dynamic_headline(status: str, axes: dict[str, dict[str, Any]]) -> tuple[str, str]:
    status_labels = {
        "collecting": "动态样本积累中",
        "paused": "午休，动态暂停",
        "stale": "动态数据延迟",
        "reset": "历史重新积累中",
        "closed": "收盘信号已定格",
    }
    if status != "ready":
        label = status_labels.get(status, "动态样本积累中")
        return label, "当前状态不推进新的动态结论。"

    market = axes.get("market_participation", {})
    financial = axes.get("financial_core", {})
    bank = axes.get("bank_support", {})
    defense = axes.get("stable_defense", {})
    resource = axes.get("resource", {})
    if bank.get("direction") == "strengthening" and financial.get("direction") != "strengthening":
        return "银行承接相对增强", "银行已改善，券商与互联网金融尚未形成同步增强。"
    if financial.get("direction") == "strengthening" and market.get("direction") != "weakening":
        return "金融进攻核心相对增强", "证券与互联网金融的确认状态正在改善。"
    if (
        defense.get("direction") == "strengthening"
        and market.get("direction") == "weakening"
    ):
        return "防御情绪相对升温", "稳态防御增强，同时市场参与度正在减弱。"
    if resource.get("direction") == "strengthening":
        return "资源方向相对增强", "更接近资源或通胀轮动，不直接解释为避险。"
    if (
        financial.get("direction") == "weakening"
        and market.get("direction") == "weakening"
    ):
        return "风险偏好正在降温", "金融核心与市场参与度同步减弱。"
    if any(axis.get("pending_state") for axis in axes.values()):
        return "板块状态出现变化，等待确认", "单个分钟候选不会直接改变动态结论。"
    return "主要结构相对稳定", "尚未出现两个连续新鲜分钟共同确认的结构变化。"


def _attach_trajectory(
    result: dict[str, Any],
    snapshot: dict[str, Any],
    statuses: dict[str, dict[str, Any]],
    market_data: dict[str, Any],
    now: datetime,
    *,
    record: bool,
) -> dict[str, Any]:
    trade_date = _effective_trade_date(market_data, now)
    phase = _market_phase(now, market_data)
    raw_axes, metrics, provider_as_of = _trajectory_inputs(
        snapshot,
        statuses,
        market_data,
        trade_date,
    )
    fresh = any(state != "unknown" for state in raw_axes.values())
    segment = "am" if now.time() <= clock_time(11, 30) else "pm"
    store = _get_trajectory_store()
    if record:
        current = store.record_snapshot(
            trade_date=trade_date,
            minute_bucket=now,
            config_version=CONFIG_VERSION,
            raw_axes=raw_axes,
            metrics=metrics,
            provider_as_of=provider_as_of,
            market_phase=phase,
            received_at=now,
            fresh=fresh,
            session_segment=segment,
            headline={
                "emotion": result.get("emotion", {}).get("label"),
                "structure": result.get("structure", {}).get("label"),
            },
        )["current"]
    else:
        current = store.get_current(
            trade_date,
            CONFIG_VERSION,
            market_phase=phase,
        )
    axis_names = {
        "market_participation",
        "financial_core",
        "bank_support",
        "stable_defense",
        "resource",
        *(f"sector:{definition.key}" for definition in SECTOR_DEFINITIONS),
    }
    axes = {name: _axis_view(current, name) for name in axis_names}
    headline, detail = _dynamic_headline(current["dynamics"], axes)
    status_labels = {
        "collecting": "动态样本积累中",
        "ready": "动态已确认",
        "paused": "午休，动态暂停",
        "stale": "动态数据延迟",
        "reset": "历史重新积累中",
        "closed": "收盘信号已定格",
    }
    result["dynamics"] = {
        "schema_version": current["schema_version"],
        "status": current["dynamics"],
        "status_label": status_labels.get(current["dynamics"], "动态样本积累中"),
        "headline": headline,
        "detail": detail,
        "window_minutes": 10,
        "sampled_at": current.get("last_sample_at"),
        "last_confirmed_at": current.get("last_confirmed_at"),
        "sample_count": len(current.get("series", [])),
        "axes": {
            key: _dynamic_copy(value)
            for key, value in axes.items()
            if not key.startswith("sector:")
        },
    }
    for key, summary in result.get("summary", {}).items():
        axis = axes.get(key)
        if axis:
            summary["trend"] = _dynamic_copy(axis)
    for group in result.get("groups", []):
        for item in group.get("items", []):
            item["trend"] = _dynamic_copy(axes.get(f"sector:{item['key']}", {}))
    return result


def _rotation_direction(value: Any) -> str:
    key = str(value or "unknown").lower()
    return "flat" if key == "stable" else key if key in {
        "strengthening", "weakening", "flat"
    } else "unknown"


def _rotation_direction_label(value: Any) -> str:
    return {
        "strengthening": "相对增强",
        "weakening": "相对转弱",
        "flat": "相对持平",
    }.get(_rotation_direction(value), "方向待积累")


def _rotation_reasons(metrics: dict[str, Any], flags: list[Any]) -> list[str]:
    reasons: list[str] = []
    rank_delta = metrics.get("rank_delta_5m")
    if isinstance(rank_delta, (int, float)):
        reasons.append(f"5分钟相对排名{rank_delta * 100:+.0f}pp")
    elif isinstance(metrics.get("rank_delta_10m"), (int, float)):
        reasons.append(
            f"10分钟相对排名{metrics['rank_delta_10m'] * 100:+.0f}pp"
        )
    breadth = metrics.get("breadth_ratio")
    if isinstance(breadth, (int, float)):
        reasons.append(f"内部广度{breadth * 100:.0f}%")
    flow_percentile = metrics.get("flow_percentile")
    if isinstance(flow_percentile, (int, float)):
        reasons.append(f"资金位置{flow_percentile * 100:.0f}%分位")
    reasons.extend(str(flag) for flag in flags if flag)
    return reasons[:4]


def _rotation_candidate_view(candidate: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(candidate.get("metrics") or {})
    family = candidate.get("family")
    role = candidate.get("role")
    flags = list(candidate.get("flags") or [])
    fund_state = str(metrics.get("fund_state") or "unknown")
    fund_direction = _rotation_direction(metrics.get("fund_direction"))
    flow_amount = metrics.get("flow_amount")
    previous_as_of = metrics.get("previous_provider_as_of")
    if flow_amount is None:
        fund_status = "unavailable"
        fund_status_label = "资金数据暂缺"
    elif previous_as_of is None:
        fund_status = "collecting"
        fund_status_label = "资金变化待积累"
    else:
        fund_status = "ready"
        fund_status_label = "同源资金快照已更新"
    return {
        "key": candidate.get("id"),
        "label": candidate.get("name"),
        "taxonomy": candidate.get("taxonomy"),
        "board_code": candidate.get("board_code"),
        "role": role.get("label") if isinstance(role, dict) else role,
        "family": family.get("label") if isinstance(family, dict) else family,
        "lifecycle": candidate.get("state", "pending"),
        "lifecycle_label": candidate.get("state_label", "观察中"),
        "direction": _rotation_direction(candidate.get("direction")),
        "direction_label": _rotation_direction_label(candidate.get("direction")),
        "divergent": bool(flags),
        "price": {
            "change_pct": metrics.get("change_pct"),
            "percentile": metrics.get("price_percentile"),
            "delta_5m": metrics.get("rank_delta_5m"),
            "delta_10m": metrics.get("rank_delta_10m"),
        },
        "breadth": {
            "ratio": metrics.get("breadth_ratio"),
            "delta_5m": metrics.get("breadth_delta_5m"),
        },
        "funds": {
            "state": fund_state,
            "label": LEVEL_LABELS.get(fund_state, "数据不足"),
            "direction": fund_direction,
            "direction_label": _rotation_direction_label(fund_direction),
            "current_amount": flow_amount,
            "current_ratio": metrics.get("flow_ratio"),
            "rank": metrics.get("flow_rank"),
            "delta_amount": metrics.get("fund_delta"),
            "previous_as_of": previous_as_of,
            "as_of": candidate.get("provider_as_of"),
            "source": candidate.get("source"),
            "status": fund_status,
            "status_label": fund_status_label,
        },
        "entered_at": candidate.get("state_since"),
        "reasons": _rotation_reasons(metrics, flags),
    }


def _rotation_leader_view(candidate: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(candidate.get("metrics") or {})
    return {
        "key": candidate.get("id"),
        "label": candidate.get("name"),
        "taxonomy": candidate.get("taxonomy"),
        "board_code": candidate.get("board_code"),
        "strength": {
            "key": candidate.get("strength", "unknown"),
            "label": LEVEL_LABELS.get(candidate.get("strength"), "观察"),
        },
        "direction": {
            "key": _rotation_direction(candidate.get("direction")),
            "label": _rotation_direction_label(candidate.get("direction")),
        },
        "change_pct": metrics.get("change_pct"),
        "price": {
            "change_pct": metrics.get("change_pct"),
            "percentile": metrics.get("price_percentile"),
            "delta_5m": metrics.get("rank_delta_5m"),
            "delta_10m": metrics.get("rank_delta_10m"),
        },
        "breadth": {
            "ratio": metrics.get("breadth_ratio"),
            "delta_5m": metrics.get("breadth_delta_5m"),
        },
        "metrics": {
            "change_pct": metrics.get("change_pct"),
            "price_percentile": metrics.get("price_percentile"),
            "breadth_ratio": metrics.get("breadth_ratio"),
            "flow_percentile": metrics.get("flow_percentile"),
            "rank_delta_5m": metrics.get("rank_delta_5m"),
            "rank_delta_10m": metrics.get("rank_delta_10m"),
        },
    }


def _rotation_frontend_view(
    current: dict[str, Any],
    *,
    phase: str,
    market_direction: str,
) -> dict[str, Any]:
    summary = copy.deepcopy(current.get("summary") or {})
    core = copy.deepcopy(current.get("core") or {})
    raw_lists = {
        key: list(current.get(key) or [])
        for key in ("attacking", "rotating", "cooling", "unclassified")
    }
    attacking = [_rotation_candidate_view(item) for item in raw_lists["attacking"]]
    rotating = [_rotation_candidate_view(item) for item in raw_lists["rotating"]]
    true_cooling = [_rotation_candidate_view(item) for item in raw_lists["cooling"]]
    divergent = [
        item for item in (*attacking, *rotating)
        if item["divergent"]
    ]
    cooling_by_key = {
        item.get("key"): item for item in (*divergent, *true_cooling)
    }

    status = str(current.get("status") or "collecting")
    status_label = str(current.get("status_label") or "轮动样本积累中")
    if phase in {"pre_open", "midday_break"}:
        status = "collecting" if phase == "pre_open" else "partial"
        status_label = "开盘前，轮动样本待积累" if phase == "pre_open" else "午休，轮动观察暂停"
    elif phase == "opening_observation" and status not in {"partial", "stale"}:
        status = "collecting"
        status_label = "开盘观察，轮动样本积累中"
    elif phase == "closed":
        status = "closed"
        status_label = "今日轮动已定格"
    elif status == "ready":
        counts = summary.get("counts") or {}
        attack_count = int(counts.get("attacking") or 0)
        rotation_count = int(counts.get("rotating") or 0)
        if attack_count or rotation_count:
            status_label = f"{attack_count}个方向正在进攻，{rotation_count}个新近增强"
        else:
            status_label = "尚无确认方向，继续扫描"

    if core:
        if phase == "pre_open":
            core["status"] = "collecting"
            core["status_label"] = "开盘前，常规进攻待观察"
        elif phase == "midday_break":
            core["status"] = "partial"
            core["status_label"] = "午休，常规进攻信号定格"
        elif phase == "opening_observation" and core.get("status") not in {
            "partial", "stale"
        }:
            core["status"] = "collecting"
            core["status_label"] = "开盘观察，常规进攻样本积累中"
        elif phase == "closed":
            core["status"] = "closed"
            core["status_label"] = "今日常规进攻已定格"

    strength = dict(summary.get("strength") or {})
    strength["tone"] = {
        "strong": "positive", "weak": "negative"
    }.get(strength.get("key"), "neutral")
    structure = dict(summary.get("structure") or {})
    if int(current.get("sample_count") or 0) < 2 and structure.get("key") == "observing":
        structure = {
            "key": "observing",
            "label": "历史不足，暂不判断扩散",
            "tone": "neutral",
        }
    elif structure.get("key") == "broadening" and market_direction == "weakening":
        structure = {
            "key": "countertrend",
            "label": "多方向逆势轮动",
            "tone": "warning",
        }
    elif structure.get("key") == "broadening" and market_direction not in {
        "strengthening", "flat"
    }:
        structure = {
            "key": "observing",
            "label": "多方向轮动，市场待确认",
            "tone": "warning",
        }
    else:
        structure["tone"] = {
            "broadening": "positive",
            "cooling": "negative",
            "observing": "neutral",
        }.get(structure.get("key"), "neutral")

    core_counts = dict(summary.get("counts") or {})
    counts = {
        "attacking": int(core_counts.get("attacking") or 0),
        "rotating": int(core_counts.get("rotating") or 0),
        "divergent": len(divergent),
        "cooling": int(core_counts.get("cooling") or 0),
    }
    coverage_parts = current.get("coverage") or {}
    industry = dict(coverage_parts.get("industry") or {})
    concept = dict(coverage_parts.get("concept") or {})
    available = int(industry.get("effective") or 0) + int(concept.get("effective") or 0)
    total = int(industry.get("total") or 0) + int(concept.get("total") or 0)
    coverage = {
        "available": available,
        "total": total,
        "partial": status == "partial" or bool(industry.get("frozen") or concept.get("frozen")),
        "stale": status == "stale",
        "label": (
            f"行业 {int(industry.get('effective') or 0)}/{int(industry.get('total') or 0)}"
            f" · 概念 {int(concept.get('effective') or 0)}/{int(concept.get('total') or 0)}"
        ),
        "industry": industry,
        "concept": concept,
    }
    return {
        "schema_version": current.get("schema_version"),
        "config_version": current.get("config_version"),
        "status": status,
        "status_label": status_label,
        "as_of": current.get("as_of"),
        "sample_count": current.get("sample_count", 0),
        "coverage": coverage,
        "core": core,
        "summary": {
            "strength": strength,
            "direction": {
                "key": _rotation_direction((summary.get("direction") or {}).get("key")),
                "label": _rotation_direction_label((summary.get("direction") or {}).get("key")),
            },
            "structure": structure,
            "counts": counts,
            "current_leaders": [
                _rotation_leader_view(item)
                for item in list(summary.get("current_leaders") or [])[:3]
            ],
        },
        "lists": {
            "attacking": attacking,
            "rotating": rotating,
            "cooling": list(cooling_by_key.values())[:8],
            "unclassified": [
                _rotation_candidate_view(item) for item in raw_lists["unclassified"]
            ],
        },
        "events": [
            {
                **dict(event),
                "label": " · ".join(
                    part for part in (
                        str(event.get("name") or "方向"),
                        str(event.get("label") or "状态变化"),
                    ) if part
                ),
            }
            for event in list(current.get("events") or [])[-12:]
        ],
    }


def _attach_rotation_radar(
    result: dict[str, Any],
    values: dict[str, list[dict[str, Any]]],
    statuses: dict[str, dict[str, Any]],
    market_data: dict[str, Any],
    now: datetime,
    *,
    record: bool,
    trade_date: str | None = None,
) -> dict[str, Any]:
    phase = _market_phase(now, market_data)
    effective_trade_date = trade_date or _effective_trade_date(market_data, now)
    store = _get_rotation_store()
    targets = sector_flow_backfill_targets(
        values.get("industry_quotes", []),
        values.get("concept_quotes", []),
        minute_bucket=now,
        sources={
            "industry": statuses.get("industry_quotes", {}).get("source") or "unknown",
            "concept": statuses.get("concept_quotes", {}).get("source") or "unknown",
        },
    )
    if record and phase == "trading":
        # The provider-neutral gateway owns one coalesced daemon sweep.  Exact
        # curves may be numerous and remain rate-limited, so Collector minute
        # capture must never wait for their serial provider requests.
        schedule_sector_intraday_fund_flow_backfill(
            targets,
            trading_date=effective_trade_date,
        )
        supplemental_points = read_sector_intraday_fund_flow_backfill(
            trading_date=effective_trade_date,
        )
    else:
        supplemental_points = read_sector_intraday_fund_flow_backfill(
            trading_date=effective_trade_date,
        )
    if record:
        current = store.record_snapshot(
            trade_date=effective_trade_date,
            minute_bucket=now,
            industry_records=values.get("industry_quotes", []),
            concept_records=values.get("concept_quotes", []),
            sources={
                "industry": statuses.get("industry_quotes", {}).get("source") or "unknown",
                "concept": statuses.get("concept_quotes", {}).get("source") or "unknown",
            },
            market_phase=phase,
            received_at=now,
            config_version=ROTATION_CONFIG_VERSION,
            supplemental_points=supplemental_points,
        )["current"]
    else:
        current = store.get_current(
            effective_trade_date,
            ROTATION_CONFIG_VERSION,
            supplemental_points=supplemental_points,
        )
        if int(current.get("sample_count") or 0) == 0:
            # A dashboard first opened after the session still gets a static
            # closing cross-section. It is never persisted or treated as a
            # confirmed trajectory sample.
            transient = normalize_rotation_snapshot(
                values.get("industry_quotes", []),
                values.get("concept_quotes", []),
                minute_bucket=market_data.get("provider_as_of") or now,
                sources={
                    "industry": statuses.get("industry_quotes", {}).get("source") or "unknown",
                    "concept": statuses.get("concept_quotes", {}).get("source") or "unknown",
                },
                market_phase=phase,
            )
            current = analyze_rotation_snapshots(
                [transient],
                config_version=ROTATION_CONFIG_VERSION,
            )
            current["sector_flow_trajectory"] = analyze_sector_flow_snapshots(
                [transient],
                supplemental_points,
            )
            current["offense_sector_flow_trajectory"] = analyze_sector_flow_snapshots(
                [transient],
                supplemental_points,
                direction="offense",
            )
    market_direction = (
        result.get("dynamics", {})
        .get("axes", {})
        .get("market_participation", {})
        .get("direction", "unknown")
    )
    result["offense"] = _rotation_frontend_view(
        current,
        phase=phase,
        market_direction=market_direction,
    )
    for field, direction in (
        ("sector_flow_trajectory", "defense"),
        ("offense_sector_flow_trajectory", "offense"),
    ):
        sector_flow = copy.deepcopy(current.get(field) or {})
        if sector_flow:
            sector_flow["direction"] = direction
            sector_flow["market_phase"] = phase
        else:
            sector_flow = {
                "contract": "sector_flow_trajectory.v1",
                "schema_version": 1,
                "direction": direction,
                "status": "unavailable",
                "trade_date": None,
                "as_of": None,
                "market_phase": phase,
                "trajectory_scope": "trading_session_to_as_of",
                "marginal_window_minutes": 5,
                "sectors": [],
                "flags": ["rotation_flow_view_unavailable"],
                "reason": "rotation_flow_view_unavailable",
            }
        result[field] = sector_flow
    return result


def get_rotation_radar_as_of(target: datetime) -> dict[str, Any]:
    """Read the persisted rotation owner at one historical minute without I/O."""

    if target.tzinfo is None or target.utcoffset() is None:
        raise ValueError("rotation as-of target must include a timezone")
    local = target.astimezone(ZoneInfo("Asia/Shanghai")).replace(second=0, microsecond=0)
    current = _get_rotation_store().get_as_of(
        local.date(),
        local,
        ROTATION_CONFIG_VERSION,
    )
    if int(current.get("sample_count") or 0) == 0:
        raise RuntimeError("no persisted rotation snapshot exists at the target minute")
    result: dict[str, Any] = {
        "offense": _rotation_frontend_view(
            current,
            phase="trading",
            market_direction="unknown",
        )
    }
    for field, direction in (
        ("sector_flow_trajectory", "defense"),
        ("offense_sector_flow_trajectory", "offense"),
    ):
        trajectory = copy.deepcopy(current.get(field) or {})
        if trajectory:
            trajectory["direction"] = direction
            trajectory["market_phase"] = "trading"
        result[field] = trajectory
    return result


def get_risk_appetite_data(
    market_data: dict[str, Any],
    *,
    force: bool = False,
    record_trajectory: bool = False,
    capture_observer: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Fetch, cache and assemble one partial-tolerant dashboard snapshot.

    ``capture_observer`` is an opt-in persistence seam for the independent data
    lake collector.  It receives deep copies of the exact normalized inputs and
    component statuses used for this calculation.  On a cold process it waits
    for the context futures submitted by this refresh, while ordinary dashboard
    reads keep their asynchronous stale-while-refreshing behaviour.
    """

    global _snapshot_cache, _snapshot_cached_at, _snapshot_trade_date

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    trade_date = _effective_trade_date(market_data, now)
    market_phase = _market_phase(now, market_data)
    now_mono = time.monotonic()
    with _cache_lock:
        if (
            _snapshot_cache is not None
            and _snapshot_trade_date == trade_date
            and not record_trajectory
        ):
            age = now_mono - _snapshot_cached_at
            if (force and age < _MIN_FORCE_INTERVAL) or (
                not force and age < _SNAPSHOT_TTL
            ):
                return _overlay_board_leaders(
                    copy.deepcopy(_snapshot_cache),
                    trade_date,
                )

    with _refresh_lock:
        now_mono = time.monotonic()
        with _cache_lock:
            if (
                _snapshot_cache is not None
                and _snapshot_trade_date == trade_date
                and not record_trajectory
            ):
                age = now_mono - _snapshot_cached_at
                if (force and age < _MIN_FORCE_INTERVAL) or (
                    not force and age < _SNAPSHOT_TTL
                ):
                    return _overlay_board_leaders(
                        copy.deepcopy(_snapshot_cache),
                        trade_date,
                    )
        values: dict[str, list[dict[str, Any]]] = {}
        statuses: dict[str, dict[str, Any]] = {}
        # Breadth and both sector quote fetchers can all fall back to the same
        # machine-local Eastmoney admission timeline. Submitting them together
        # makes the third request exceed the bounded queue wait by design. Keep
        # that provider-sensitive group ordered while the independent THS
        # leadership request runs in parallel.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="risk-leadership") as executor:
            leadership_future = executor.submit(
                _component,
                "leadership_pool",
                lambda: _fetch_leadership_pool(trade_date),
                force=force,
                cache_identity=trade_date,
            )
            for name, fetcher in (
                ("market_breadth", _fetch_market_breadth),
                ("industry_quotes", lambda: _fetch_board_quotes("industry")),
                ("concept_quotes", lambda: _fetch_board_quotes("concept")),
            ):
                values[name], statuses[name] = _component(
                    name,
                    fetcher,
                    force=force,
                    closed_trade_date=(
                        trade_date
                        if market_phase == "closed"
                        and name in {"industry_quotes", "concept_quotes"}
                        else None
                    ),
                )
            values["leadership_pool"], statuses["leadership_pool"] = (
                leadership_future.result()
            )

        _derive_sector_flow_components(values, statuses)
        context_futures = _refresh_context_async(
            force=force and not record_trajectory
        )
        if capture_observer is not None:
            _wait_for_context_futures(context_futures)
        for name in _CONTEXT_COMPONENTS:
            values[name], statuses[name] = _cached_component(name)

        leadership_status = statuses["leadership_pool"]
        raw_profile_status = leadership_status.get("industry_profile_status")
        profile_status = (
            copy.deepcopy(raw_profile_status)
            if isinstance(raw_profile_status, dict)
            else None
        )
        pool_total = leadership_status.get("pool_total")
        if profile_status is None and pool_total == 0:
            profile_status = {
                "status": "ready",
                "source": None,
                "source_valid": True,
                "eligible_for_attribution": True,
                "requested_total": 0,
                "returned_total": 0,
                "coverage": 1.0,
                "provider_as_of": None,
                "error": None,
            }
        elif profile_status is None:
            profile_status = {
                "status": "degraded",
                "source": None,
                "source_valid": False,
                "eligible_for_attribution": False,
                "requested_total": pool_total,
                "returned_total": 0,
                "coverage": 0.0,
                "provider_as_of": None,
                "error": "industry_profile_missing",
            }
        profile_eligible = bool(
            profile_status.get("status") == "ready"
            and profile_status.get("source_valid")
            and profile_status.get("eligible_for_attribution")
            and profile_status.get("requested_total") == pool_total
            and profile_status.get("returned_total") == pool_total
            and profile_status.get("coverage") == 1.0
            and (
                pool_total == 0
                or _provider_matches_date(
                    profile_status.get("provider_as_of"),
                    trade_date,
                )
            )
        )
        profile_status["eligible_for_attribution"] = profile_eligible
        leadership_status["industry_profile_status"] = profile_status
        leadership_status["attribution_mode"] = (
            "reason_plus_industry" if profile_eligible else "reason_only"
        )
        leadership_status["attribution_degraded"] = not profile_eligible
        leadership_date_matches = (
            _normalise_trade_date(leadership_status.get("data_date")) == trade_date
        )
        leadership_contract_valid = _leadership_contract_valid(leadership_status)
        leadership_source_valid = bool(
            leadership_status.get("fetched_at")
            and leadership_status.get("source_valid")
            and leadership_date_matches
            and leadership_contract_valid
            and not leadership_status.get("expired")
        )
        trade_status_eligible = _leadership_trade_status_eligible(
            leadership_status.get("trade_status")
        )
        leadership_eligible = bool(
            leadership_source_valid
            and not leadership_status.get("stale")
            and not leadership_status.get("error")
            and trade_status_eligible
        )
        leadership_status["source_valid"] = leadership_source_valid
        leadership_status["eligible_for_vote"] = leadership_eligible
        if not leadership_source_valid:
            leadership_status["reason"] = (
                "wrong_trade_date"
                if leadership_status.get("data_date") and not leadership_date_matches
                else "source_invalid"
            )
        elif not leadership_eligible:
            leadership_status["reason"] = (
                "stale_display_only"
                if leadership_status.get("stale") or leadership_status.get("error")
                else "trade_status_not_eligible"
            )
        else:
            leadership_status["reason"] = "fresh"

        if capture_observer is not None:
            capture_observer({
                "trade_date": trade_date,
                "observed_at": now,
                "minute_bucket": now.replace(second=0, microsecond=0),
                "market_phase": market_phase,
                "market_data": copy.deepcopy(market_data),
                "values": copy.deepcopy(values),
                "statuses": copy.deepcopy(statuses),
            })

        leadership_records = copy.deepcopy(values["leadership_pool"])
        if not profile_eligible:
            for record in leadership_records:
                record.pop("sector_profile", None)

        snapshot = build_risk_appetite_snapshot(
            values["industry_quotes"],
            values["concept_quotes"],
            industry_flow_records=values["industry_flow"],
            concept_flow_records=values["concept_flow"],
            index_records=(
                market_data.get("participation_indices")
                or market_data.get("indices")
                or []
            ),
            market_breadth=(values["market_breadth"] or [None])[0],
            market_turnover=market_data.get("market_turnover"),
            etf_records=values["etfs"],
            leadership_records=leadership_records,
            leadership_source_status=leadership_status,
            trade_date=trade_date,
            as_of=now,
        )
        _merge_leader_quotes(snapshot, values["leaders"])
        _attach_static_memberships(snapshot)

        # Preserve the provider's current leader as context. It never affects votes.
        board_records = values["industry_quotes"] + values["concept_quotes"]
        board_by_name = {
            str(row.get("板块名称", "")).strip(): row for row in board_records
        }
        for group in snapshot["sectors"].values():
            matched = board_by_name.get(group.get("matched_alias") or "", {})
            group["current_leader"] = {
                "code": matched.get("领涨股代码"),
                "name": matched.get("领涨股票"),
                "market": matched.get("领涨股市场"),
                "change_pct": matched.get("领涨股涨幅"),
                "provider_as_of": matched.get("更新时间"),
                "source": matched.get("source"),
            } if matched.get("领涨股票") else None

        result = _view_model(snapshot, statuses)
        result["phase_label"] = (
            market_data.get("market_state", {}).get("label")
            or ("开盘观察" if snapshot["opening_observation"] else "盘中确认")
        )
        # Carry current leaders into the flattened view after view-model creation.
        sector_by_key = snapshot["sectors"]
        for group in result["groups"]:
            for item in group["items"]:
                item["current_leader"] = sector_by_key[item["key"]].get("current_leader")

        result = _stabilise_headline(result)
        try:
            result = _attach_trajectory(
                result,
                snapshot,
                statuses,
                market_data,
                now,
                record=record_trajectory,
            )
        except Exception as exc:  # noqa: BLE001 - static snapshot must remain usable
            logger.exception("risk trajectory update failed")
            result["dynamics"] = {
                "schema_version": "risk-trajectory-v1",
                "status": "stale",
                "status_label": "动态存储暂不可用",
                "headline": "动态数据暂不可用",
                "detail": str(exc),
                "window_minutes": 10,
                "sampled_at": None,
                "last_confirmed_at": None,
                "sample_count": 0,
                "axes": {},
            }
        try:
            result = _attach_rotation_radar(
                result,
                values,
                statuses,
                market_data,
                now,
                record=record_trajectory,
            )
        except Exception as exc:  # noqa: BLE001 - existing sentiment must remain usable
            logger.exception("rotation radar update failed")
            empty_core = copy.deepcopy(analyze_rotation_snapshots([]).get("core") or {})
            empty_core.update({
                "status": "error",
                "status_label": "常规进攻暂不可用",
            })
            result["offense"] = {
                "schema_version": "rotation-radar-v1",
                "config_version": ROTATION_CONFIG_VERSION,
                "status": "error",
                "status_label": "进攻雷达暂不可用",
                "as_of": None,
                "sample_count": 0,
                "coverage": {"available": 0, "total": 0, "error": True},
                "core": empty_core,
                "summary": {
                    "strength": {"key": "unknown", "label": "观察"},
                    "direction": {"key": "unknown", "label": "方向待积累"},
                    "structure": {"key": "observing", "label": "暂不可用"},
                    "counts": {"attacking": 0, "rotating": 0, "divergent": 0, "cooling": 0},
                    "current_leaders": [],
                },
                "lists": {
                    "attacking": [], "rotating": [], "cooling": [], "unclassified": []
                },
                "events": [],
                "error": str(exc),
            }
            for field, direction in (
                ("sector_flow_trajectory", "defense"),
                ("offense_sector_flow_trajectory", "offense"),
            ):
                result[field] = {
                    "contract": "sector_flow_trajectory.v1",
                    "schema_version": 1,
                    "direction": direction,
                    "status": "unavailable",
                    "trade_date": None,
                    "as_of": None,
                    "market_phase": market_phase,
                    "trajectory_scope": "trading_session_to_as_of",
                    "marginal_window_minutes": 5,
                    "sectors": [],
                    "flags": [f"rotation_flow_error:{type(exc).__name__}"],
                    "reason": "rotation_flow_view_unavailable",
                }
        result = _attach_provider_leader_fallbacks(
            result,
            board_records,
            statuses,
        )
        with _cache_lock:
            _snapshot_cache = copy.deepcopy(result)
            _snapshot_cached_at = time.monotonic()
            _snapshot_trade_date = trade_date
        return _overlay_board_leaders(
            result,
            trade_date,
        )


def reset_risk_appetite_cache() -> None:
    """Clear dashboard caches for deterministic tests."""

    global _snapshot_cache, _snapshot_cached_at, _snapshot_trade_date
    global _published_headline, _pending_headline_key, _pending_headline_count
    global _board_leader_generation, _board_leader_trade_date
    with _cache_lock:
        _snapshot_cache = None
        _snapshot_cached_at = 0.0
        _snapshot_trade_date = None
        _component_cache.clear()
        _component_errors.clear()
        _component_attempted_at.clear()
        for future in _context_futures.values():
            future.cancel()
        _context_futures.clear()
        _board_leader_generation += 1
        for future in _board_leader_futures.values():
            future.cancel()
        _board_leader_futures.clear()
        _board_leader_cache.clear()
        _board_leader_trade_date = None
        _published_headline = None
        _pending_headline_key = None
        _pending_headline_count = 0


def get_risk_appetite_series(*, trade_date: str | None = None) -> dict[str, Any]:
    target_date = date.fromisoformat(trade_date) if trade_date else datetime.now(
        ZoneInfo("Asia/Shanghai")
    ).date()
    store = _get_trajectory_store()
    points = store.get_series(target_date, CONFIG_VERSION)
    return {
        "schema_version": "risk-trajectory-v1",
        "trade_date": target_date.isoformat(),
        "config_version": CONFIG_VERSION,
        "count": len(points),
        "points": points,
    }


def close_risk_trajectory_store() -> None:
    global _trajectory_store, _rotation_store
    with _trajectory_store_lock:
        if _trajectory_store is not None:
            _trajectory_store.close()
            _trajectory_store = None
    with _rotation_store_lock:
        if _rotation_store is not None:
            _rotation_store.close()
            _rotation_store = None


__all__ = [
    "close_risk_trajectory_store",
    "get_risk_appetite_data",
    "get_risk_appetite_series",
    "reset_risk_appetite_cache",
]
