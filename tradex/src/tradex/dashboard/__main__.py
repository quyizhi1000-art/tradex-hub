"""tradex 行情看板 - 轻量 HTTP 服务。

启动：python -m tradex.dashboard
默认端口 8765（环境变量 TRADEX_DASHBOARD_PORT 可配置）

路由：
  GET /                → HTML 盘面看板页面（构建免依赖、本地 CSS/JS）
  GET /api/market-watch/collection-status → 采集账本状态与最新可用真实快照指针
  POST /api/market-watch/daily-recovery → 排队一次收盘完整性检查与追补
  GET /api/market-watch/summary → 无轨迹点的精简盘面摘要
  GET /api/market-watch/trajectory → 按板块精确读取完整盘中轨迹
  GET /api/limit-up-pool → 按最新真实快照读取涨停池与真实股票归属
  GET /api/limit-up-pool/latest → 读取不晚于指定快照的同交易日上一版涨停池
  GET /api/stock-relationships → 读取统一证券关系目录状态或单股关系
  GET /api/market-watch/history → 按交易日返回分钟快照与提醒历史
  GET /api/market-watch/evaluation → 读取 Analysis Worker 预计算回放评估
  GET /api/post-market-review/history → 读取预计算日复盘展示档案
  GET /api/post-market-review/generation → 返回后台生成任务状态
  POST /api/post-market-review → 排队生成当日日复盘
  GET /api/daily-stock-selection/history → 读取预计算每日选股档案
  GET /api/daily-stock-selection/generation → 返回后台生成任务状态
  POST /api/daily-stock-selection → 排队生成当日候选池
  GET /api/stock-selection/strategies → 读取独立策略清单
  GET /api/stock-selection/results → 读取独立策略结果与评估
  GET /api/market      → 兼容的指数实时行情 JSON
  GET /api/risk-appetite → 兼容的市场参与度与资金风格信号 JSON
  GET /api/dashboard   → 看板数据 JSON（与 MCP 工具 get_data_source_dashboard 结构一致）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from tradex.data_gateway.market import (
    build_market_turnover as _build_canonical_market_turnover,
    fetch_market_overview,
    index_quotes_to_legacy,
    market_overview_to_legacy_payload,
    market_state as _canonical_market_state,
    market_turnover_to_legacy,
    participation_indices_to_legacy,
)
from tradex.data_gateway.providers.market_overview import (
    map_indices,
    map_participation_indices,
)

_TOOL_COUNT: int | None = None
_MARKET_CACHE: dict | None = None
_MARKET_CACHE_AT = 0.0
_MARKET_REFRESH_LOCK = threading.Lock()
_MARKET_CACHE_TTL = 10
_MARKET_MIN_FORCE_INTERVAL = 30
_MARKET_WATCH_SERVICE = None
_MARKET_WATCH_SERVICE_LOCK = threading.Lock()
_MARKET_WATCH_HISTORY_STORE = None
_MARKET_WATCH_HISTORY_STORE_LOCK = threading.Lock()
_MARKET_WATCH_COLLECTION_STORE = None
_MARKET_WATCH_COLLECTION_STORE_LOCK = threading.Lock()
_MARKET_WATCH_READ_FACADE = None
_MARKET_WATCH_READ_FACADE_LOCK = threading.Lock()
_MARKET_WATCH_WEB_API = None
_MARKET_WATCH_WEB_API_LOCK = threading.Lock()
_SECTOR_RESONANCE_STORE = None
_SECTOR_RESONANCE_STORE_LOCK = threading.Lock()
_ANALYSIS_JOB_STORE = None
_ANALYSIS_JOB_STORE_LOCK = threading.Lock()
_ANALYSIS_JOB_READER = None
_ANALYSIS_JOB_READER_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


def _get_html() -> str:
    """Read the desktop market-watch shell without caching it in-process."""
    html_path = Path(__file__).parent / "watch" / "index.html"
    return html_path.read_text(encoding="utf-8")


def _get_watch_asset(name: str) -> tuple[bytes, str]:
    """Return one allow-listed static asset for the build-free desktop page."""

    content_types = {
        "styles.css": "text/css; charset=utf-8",
        "app.js": "text/javascript; charset=utf-8",
    }
    if name not in content_types:
        raise FileNotFoundError(name)
    path = Path(__file__).parent / "watch" / name
    return path.read_bytes(), content_types[name]


def _get_tool_count() -> int:
    """获取 MCP 工具总数（首次调用后缓存，工具数启动后不变）。"""
    global _TOOL_COUNT
    if _TOOL_COUNT is None:
        try:
            from tradex.server import mcp

            _TOOL_COUNT = len(asyncio.run(mcp.list_tools()))
        except Exception:
            _TOOL_COUNT = 0
    return _TOOL_COUNT


def get_dashboard_data() -> dict:
    """获取看板数据（复用 diagnostics.build_dashboard_data 逻辑）。"""
    # 确保数据源已注册（幂等）
    try:
        from tradex.data_sources import register_all_sources

        register_all_sources()
    except Exception:
        pass
    from tradex.tools.diagnostics import build_dashboard_data

    return build_dashboard_data(tool_count=_get_tool_count())


def _normalise_indices(records: list[dict], source: str) -> list[dict]:
    """兼容入口：标准映射已迁移到 provider-neutral 数据网关。"""
    return index_quotes_to_legacy(map_indices(records, source))


def _normalise_participation_indices(records: list[dict]) -> list[dict]:
    """兼容入口：返回旧风险模型仍在消费的中文字段。"""
    return participation_indices_to_legacy(map_participation_indices(records))


def _market_state(now: datetime) -> dict:
    """返回中国 A 股常规交易时段状态（不推断节假日）。"""
    return _canonical_market_state(now).model_dump()


def _market_payload_is_current(payload: dict, now: datetime) -> bool:
    provider_as_of = payload.get("provider_as_of")
    if provider_as_of:
        try:
            return datetime.fromisoformat(str(provider_as_of)).date() == now.date()
        except ValueError:
            return False
    turnover = payload.get("market_turnover") or {}
    return bool(
        turnover.get("available")
        and turnover.get("today_date") == now.date().isoformat()
    )


def _build_market_turnover(series_by_code: dict[str, list[dict]], now: datetime) -> dict:
    """兼容入口：旧代码键转换后交由标准成交额契约计算。"""
    canonical_series = {
        {
            "sh000001": "000001.SH",
            "sz399001": "399001.SZ",
        }.get(code, code): series
        for code, series in series_by_code.items()
    }
    return market_turnover_to_legacy(
        _build_canonical_market_turnover(canonical_series, now)
    )


def get_market_data(force: bool = False) -> dict:
    """获取角色指数与兼容指数快照，并做短时缓存避免频繁访问上游。"""
    global _MARKET_CACHE, _MARKET_CACHE_AT
    now_monotonic = time.monotonic()
    if _MARKET_CACHE is not None:
        age = now_monotonic - _MARKET_CACHE_AT
        if (force and age < _MARKET_MIN_FORCE_INTERVAL) or (
            not force and age < _MARKET_CACHE_TTL
        ):
            return _MARKET_CACHE

    with _MARKET_REFRESH_LOCK:
        now_monotonic = time.monotonic()
        if _MARKET_CACHE is not None:
            age = now_monotonic - _MARKET_CACHE_AT
            if (force and age < _MARKET_MIN_FORCE_INTERVAL) or (
                not force and age < _MARKET_CACHE_TTL
            ):
                return _MARKET_CACHE

        return _refresh_market_data()


def _refresh_market_data() -> dict:
    """执行一次真实行情刷新；调用方负责持有刷新锁。"""
    global _MARKET_CACHE, _MARKET_CACHE_AT
    payload = market_overview_to_legacy_payload(fetch_market_overview())
    _MARKET_CACHE = payload
    _MARKET_CACHE_AT = time.monotonic()
    return payload


def _fetch_market_watch_inputs() -> dict:
    """Collect current provider-neutral views without forcing risk fan-out.

    The risk service's component TTLs and the minute sampler are the only
    owners of upstream risk refresh.  A stale market-watch snapshot may be
    rebuilt more often than those components, but rebuilding must only read
    their bounded cache rather than starting another expensive provider sweep.
    """

    from tradex.dashboard.risk_service import get_risk_appetite_data

    try:
        market_data = get_market_data(force=False)
    except Exception as exc:
        market_data = _last_good_market_watch_input(exc)
        risk_data = get_risk_appetite_data(market_data, force=False)
        risk_data = dict(risk_data)
        risk_data["freshness"] = market_data["freshness"]
        return {
            "market_data": market_data,
            "risk_data": risk_data,
        }
    return {
        "market_data": market_data,
        "risk_data": get_risk_appetite_data(market_data, force=False),
    }


def _last_good_market_watch_input(error: Exception) -> dict:
    """Reuse the strict bootstrap view when the market overview is unavailable.

    Rotation and leader refresh still remain owned by the risk service.  This
    adapter only supplies its last provider-neutral market input and marks every
    inherited component stale, so a failed overview cannot erase a newer
    persisted offense trajectory or retain an actionable conclusion.
    """

    service = _MARKET_WATCH_SERVICE
    previous = service.last_snapshot if service is not None else None
    if previous is None:
        raise error

    payload = previous.model_dump(mode="json")
    fallback_flag = f"market_overview_fallback:{type(error).__name__}"
    freshness = dict(payload.get("freshness") or {})
    freshness["status"] = "stale"
    freshness["flags"] = list(dict.fromkeys([
        *(freshness.get("flags") or []),
        fallback_flag,
    ]))
    components = []
    for raw in freshness.get("components") or []:
        component = dict(raw)
        component["status"] = "stale"
        component["flags"] = list(dict.fromkeys([
            *(component.get("flags") or []),
            fallback_flag,
        ]))
        components.append(component)
    freshness["components"] = components
    payload["freshness"] = freshness
    return payload


def _build_market_watch_snapshot(payload: dict):
    """Map one collected input bundle to the strict market_watch.v1 contract."""

    from tradex.market_watch.analysis import build_market_watch_snapshot

    return build_market_watch_snapshot(
        payload["market_data"],
        payload.get("risk_data"),
        as_of=payload.get("as_of"),
        sequence=int(payload.get("sequence") or 0),
        snapshot_id=payload.get("snapshot_id"),
    )


def _get_market_watch_service():
    """Return the process-wide owner of market-watch refresh and alert state."""

    global _MARKET_WATCH_SERVICE
    if _MARKET_WATCH_SERVICE is None:
        with _MARKET_WATCH_SERVICE_LOCK:
            if _MARKET_WATCH_SERVICE is None:
                from tradex.market_watch.service import MarketWatchService

                _MARKET_WATCH_SERVICE = MarketWatchService(
                    _fetch_market_watch_inputs,
                    _build_market_watch_snapshot,
                    initial_snapshot=_load_market_watch_bootstrap(),
                )
    return _MARKET_WATCH_SERVICE


def _load_market_watch_bootstrap():
    """Load the newest strict snapshot with a usable turnover component."""

    try:
        item = _get_market_watch_history_store().find_latest_snapshot(
            lambda payload: payload.get("turnover", {}).get("available") is True
        )
        if item is None:
            return None
        from tradex.market_watch.contracts import MarketWatchSnapshotV1

        return MarketWatchSnapshotV1.model_validate(item["payload"])
    except Exception as exc:  # noqa: BLE001 - live refresh remains authoritative
        logger.warning("market watch bootstrap skipped: %s", type(exc).__name__)
        return None


def _get_market_watch_history_store():
    """Return the process-wide zero-write history reader for the Dashboard."""

    global _MARKET_WATCH_HISTORY_STORE
    if _MARKET_WATCH_HISTORY_STORE is None:
        with _MARKET_WATCH_HISTORY_STORE_LOCK:
            if _MARKET_WATCH_HISTORY_STORE is None:
                from tradex.market_watch.history import MarketWatchHistoryStore

                _MARKET_WATCH_HISTORY_STORE = MarketWatchHistoryStore(read_only=True)
    return _MARKET_WATCH_HISTORY_STORE


def _get_market_watch_collection_store():
    """Return the process-wide zero-write collector-ledger reader."""

    global _MARKET_WATCH_COLLECTION_STORE
    if _MARKET_WATCH_COLLECTION_STORE is None:
        with _MARKET_WATCH_COLLECTION_STORE_LOCK:
            if _MARKET_WATCH_COLLECTION_STORE is None:
                from tradex.market_watch.collection_store import (
                    MarketWatchCollectionStore,
                )

                _MARKET_WATCH_COLLECTION_STORE = MarketWatchCollectionStore(
                    read_only=True
                )
    return _MARKET_WATCH_COLLECTION_STORE


def _get_market_watch_read_facade():
    """Return the sole Dashboard reader for collector-published snapshots."""

    global _MARKET_WATCH_READ_FACADE
    if _MARKET_WATCH_READ_FACADE is None:
        with _MARKET_WATCH_READ_FACADE_LOCK:
            if _MARKET_WATCH_READ_FACADE is None:
                from tradex.market_watch.read_facade import MarketWatchReadFacade

                _MARKET_WATCH_READ_FACADE = MarketWatchReadFacade(
                    collection_reader=_get_market_watch_collection_store(),
                    history_reader=_get_market_watch_history_store(),
                )
    return _MARKET_WATCH_READ_FACADE


def _get_market_watch_web_api():
    """Return the read-only summary/detail API with serialized-byte caches."""

    global _MARKET_WATCH_WEB_API
    if _MARKET_WATCH_WEB_API is None:
        with _MARKET_WATCH_WEB_API_LOCK:
            if _MARKET_WATCH_WEB_API is None:
                from tradex.market_watch.web_api import MarketWatchWebApi
                from tradex.market_watch.web_payload_service import (
                    MarketWatchWebPayloadService,
                )

                _MARKET_WATCH_WEB_API = MarketWatchWebApi(
                    MarketWatchWebPayloadService(
                        _get_market_watch_read_facade(),
                        resonance_reader=_get_sector_resonance_store(),
                    )
                )
    return _MARKET_WATCH_WEB_API


def _get_sector_resonance_store():
    """Return the Dashboard's zero-write independently versioned result reader."""

    global _SECTOR_RESONANCE_STORE
    if _SECTOR_RESONANCE_STORE is None:
        with _SECTOR_RESONANCE_STORE_LOCK:
            if _SECTOR_RESONANCE_STORE is None:
                from tradex.market_watch.sector_resonance_store import (
                    SectorResonanceStore,
                )

                _SECTOR_RESONANCE_STORE = SectorResonanceStore(read_only=True)
    return _SECTOR_RESONANCE_STORE


def get_market_watch_data(force: bool = False) -> dict:
    """Return the unified JSON-safe desktop snapshot.

    ``force`` bypasses only the canonical snapshot cache (subject to its short
    double-click guard).  It deliberately does not bypass risk-component TTLs;
    manual refresh therefore rebuilds the view from the newest owned caches
    without starting an extra provider-wide fan-out.

    Persistence is deliberately absent here.  Only the independent collector
    process may call this canonical service and write accepted snapshots.
    """

    snapshot = _get_market_watch_service().get(
        force=force,
        stale_while_revalidate=not force,
    )
    return snapshot.model_dump(mode="json")


def _history_trade_date(value: str | None, dates: list[dict]) -> str:
    if value:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("trade_date 必须是 YYYY-MM-DD") from exc
        return parsed.isoformat()
    if dates:
        return str(dates[0]["trade_date"])
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _history_limit(value: str | None, *, default: int = 300) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit 必须是正整数") from exc
    if not 1 <= parsed <= 1000:
        raise ValueError("limit 必须介于 1 与 1000 之间")
    return parsed


def _history_days(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("days 必须是正整数") from exc
    if not 1 <= parsed <= 30:
        raise ValueError("days 必须介于 1 与 30 之间")
    return parsed


def get_market_watch_history(
    *,
    trade_date: str | None = None,
    limit: int = 300,
) -> dict:
    """Return one replayable market-level session without provider details."""

    from tradex.market_watch.history import HISTORY_CONTRACT, HISTORY_SCHEMA_VERSION

    store = _get_market_watch_history_store()
    dates = store.list_dates()
    target_date = _history_trade_date(trade_date, dates)
    return {
        "contract": HISTORY_CONTRACT,
        "schema_version": HISTORY_SCHEMA_VERSION,
        "config_version": store.config_version,
        "trade_date": target_date,
        "dates": dates,
        "samples": store.get_replay_timeline(target_date, limit=limit),
        "alerts": store.get_alerts(target_date, limit=limit),
        "recording": {
            "status": "collector_owned",
            "error": None,
        },
    }


def get_market_watch_evaluation(
    *,
    trade_date: str | None = None,
    days: int | None = None,
) -> dict:
    """Read one Analysis Worker materialized evaluation without recomputing it."""

    if days is not None:
        if not 1 <= days <= 30:
            raise ValueError("days 必须介于 1 与 30 之间")
        if trade_date is not None:
            raise ValueError("trade_date 与 days 不能同时指定")
        scope_key = f"days:{days}"
    else:
        dates = _get_market_watch_history_store().list_dates()
        target_date = _history_trade_date(trade_date, dates)
        scope_key = f"date:{target_date}"
    from tradex.analysis_jobs import MARKET_WATCH_EVALUATION

    artifact = _get_analysis_job_reader().get_artifact(
        MARKET_WATCH_EVALUATION,
        scope_key=scope_key,
    )
    if artifact is None:
        raise LookupError("回放评估正在由后台准备")
    return artifact["payload"]


def _get_analysis_job_store():
    """Return the command writer used only by explicit Web POST actions."""

    global _ANALYSIS_JOB_STORE
    if _ANALYSIS_JOB_STORE is None:
        with _ANALYSIS_JOB_STORE_LOCK:
            if _ANALYSIS_JOB_STORE is None:
                from tradex.analysis_jobs import AnalysisJobCommandWriter

                _ANALYSIS_JOB_STORE = AnalysisJobCommandWriter()
    return _ANALYSIS_JOB_STORE


def _get_analysis_job_reader():
    """Return a strict read-only view of worker-owned status and artifacts."""

    global _ANALYSIS_JOB_READER
    if _ANALYSIS_JOB_READER is None:
        with _ANALYSIS_JOB_READER_LOCK:
            if _ANALYSIS_JOB_READER is None:
                from tradex.analysis_jobs import AnalysisJobReader

                _ANALYSIS_JOB_READER = AnalysisJobReader()
    return _ANALYSIS_JOB_READER


def _generation_payload(job: dict | None, *, contract: str) -> dict:
    """Project one internal analysis job to the stable feature status contract."""

    if job is None:
        return {
            "contract": contract,
            "schema_version": 1,
            "job_id": None,
            "trade_date": None,
            "trigger": None,
            "state": "idle",
            "phase": "idle",
            "started_at": None,
            "finished_at": None,
            "error": None,
            "failure_code": None,
            "failed_phase": None,
            "result": None,
        }
    return {
        "contract": contract,
        "schema_version": 1,
        "job_id": job["job_id"],
        "trade_date": job["trade_date"],
        "trigger": job["trigger"],
        "state": job["state"],
        "phase": job["phase"],
        "started_at": job["started_at"] or job["requested_at"],
        "finished_at": job["finished_at"],
        "error": job["error"],
        "failure_code": job["failure_code"],
        "failed_phase": job["failed_phase"],
        "result": job["result"],
    }


def _artifact_payload(capability: str, *, trade_date: str | None, limit: int) -> dict:
    scope_key = f"date:{trade_date}" if trade_date else "latest"
    artifact = _get_analysis_job_reader().get_artifact(
        capability,
        scope_key=scope_key,
    )
    if artifact is None:
        raise LookupError("后台展示结果正在准备")
    payload = dict(artifact["payload"])
    payload["dates"] = list(payload.get("dates") or [])[:limit]
    payload["artifact_revision"] = artifact["payload_digest"]
    payload["artifact_generated_at"] = artifact["generated_at"]
    return payload


def _review_history_limit(value: str | None, *, default: int = 90) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit 必须是正整数") from exc
    if not 1 <= parsed <= 365:
        raise ValueError("limit 必须介于 1 与 365 之间")
    return parsed


def generate_post_market_review() -> dict:
    """Queue today's manual review; Analysis Worker performs the generation."""

    from tradex.analysis_jobs import POST_MARKET_REVIEW

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    job = _get_analysis_job_store().enqueue(
        POST_MARKET_REVIEW,
        trade_date=now.date(),
        trigger="manual",
        requested_at=now,
    )
    return _generation_payload(job, contract="post_market_review_generation.v1")


def get_post_market_review_generation() -> dict:
    from tradex.analysis_jobs import POST_MARKET_REVIEW

    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    job = _get_analysis_job_reader().latest_job(
        POST_MARKET_REVIEW,
        trade_date=today,
    )
    return _generation_payload(job, contract="post_market_review_generation.v1")


def get_post_market_review_history(
    *,
    trade_date: str | None = None,
    limit: int = 90,
) -> dict:
    """Return one precomputed display archive without rebuilding presentation."""

    from tradex.analysis_jobs import POST_MARKET_REVIEW

    return _artifact_payload(
        POST_MARKET_REVIEW,
        trade_date=trade_date,
        limit=limit,
    )


def _selection_history_limit(value: str | None, *, default: int = 90) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit 必须是正整数") from exc
    if not 1 <= parsed <= 365:
        raise ValueError("limit 必须介于 1 与 365 之间")
    return parsed


def generate_daily_stock_selection() -> dict:
    """Queue today's selection; Analysis Worker performs provider acquisition."""

    from tradex.analysis_jobs import DAILY_STOCK_SELECTION

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    job = _get_analysis_job_store().enqueue(
        DAILY_STOCK_SELECTION,
        trade_date=now.date(),
        trigger="manual",
        requested_at=now,
    )
    return _generation_payload(job, contract="daily_stock_selection_generation.v1")


def get_daily_stock_selection_generation() -> dict:
    """Return today's durable worker-owned stock-selection job state."""

    from tradex.analysis_jobs import DAILY_STOCK_SELECTION

    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    job = _get_analysis_job_reader().latest_job(
        DAILY_STOCK_SELECTION,
        trade_date=today,
    )
    return _generation_payload(job, contract="daily_stock_selection_generation.v1")


def get_daily_stock_selection_history(
    *,
    trade_date: str | None = None,
    limit: int = 90,
) -> dict:
    """Return one Analysis Worker materialized stock-selection display."""

    from tradex.analysis_jobs import DAILY_STOCK_SELECTION

    return _artifact_payload(
        DAILY_STOCK_SELECTION,
        trade_date=trade_date,
        limit=limit,
    )


def _stock_selection_strategy_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not re.fullmatch(r"[a-z][a-z0-9-]*", normalized):
        raise ValueError("strategy_id 格式无效")
    return normalized


def get_stock_selection_strategy_results(
    *,
    trade_date: str | None = None,
    strategy_id: str | None = None,
    limit: int = 90,
) -> dict:
    from tradex.analysis_jobs import DAILY_STOCK_SELECTION

    legacy = _artifact_payload(
        DAILY_STOCK_SELECTION,
        trade_date=trade_date,
        limit=limit,
    )
    archive = dict(legacy.get("strategy_archive") or {})
    if archive.get("contract") != "stock_selection_strategy_archive.v1":
        archive = {
            "contract": "stock_selection_strategy_archive.v1",
            "schema_version": 1,
            "trade_date": legacy.get("trade_date"),
            "dates": list(legacy.get("dates") or []),
            "catalog": {
                "contract": "stock_selection_strategy_catalog.v1",
                "schema_version": 1,
                "strategies": [],
            },
            "results": [],
            "outcomes": [],
            "recent_outcomes": [],
            "schedule": dict(legacy.get("schedule") or {}),
        }
    archive["dates"] = list(archive.get("dates") or [])[:limit]
    normalized_strategy_id = _stock_selection_strategy_id(strategy_id)
    if normalized_strategy_id is not None:
        archive["results"] = [
            item
            for item in list(archive.get("results") or [])
            if item.get("strategy_id") == normalized_strategy_id
        ]
        result_ids = {
            item.get("result_id") for item in archive["results"] if item.get("result_id")
        }
        archive["outcomes"] = [
            item
            for item in list(archive.get("outcomes") or [])
            if item.get("result_id") in result_ids
        ]
        archive["recent_outcomes"] = [
            item
            for item in list(archive.get("recent_outcomes") or [])
            if item.get("strategy_id") == normalized_strategy_id
        ]
    archive["legacy_selection"] = legacy.get("selection")
    archive["legacy_outcome"] = legacy.get("outcome")
    archive["legacy_recent_outcomes"] = list(legacy.get("recent_outcomes") or [])
    archive["artifact_revision"] = legacy.get("artifact_revision")
    archive["artifact_generated_at"] = legacy.get("artifact_generated_at")
    return archive


def get_stock_selection_strategies(
    *,
    trade_date: str | None = None,
) -> dict:
    archive = get_stock_selection_strategy_results(
        trade_date=trade_date,
        limit=365,
    )
    return {
        "contract": "stock_selection_strategy_catalog_response.v1",
        "schema_version": 1,
        "trade_date": archive.get("trade_date"),
        "dates": archive.get("dates", []),
        "catalog": archive.get("catalog"),
        "artifact_revision": archive.get("artifact_revision"),
        "artifact_generated_at": archive.get("artifact_generated_at"),
    }


def request_market_watch_daily_recovery(*, now: datetime | None = None) -> dict:
    """Queue a manual sweep; the Dashboard never performs provider repair."""

    from tradex.market_calendar import a_share_session
    from tradex.market_watch.collection_contracts import DailyRecoveryTrigger
    from tradex.market_watch.collection_store import MarketWatchCollectionStore

    observed = (now or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(
        ZoneInfo("Asia/Shanghai")
    )
    session = a_share_session(observed)
    if not session.is_trading_day or session.phase.value != "closed":
        raise ValueError("收盘完整性检查仅在已核验交易日收盘后可执行")
    with MarketWatchCollectionStore() as store:
        result = store.request_daily_recovery(
            observed.date(),
            trigger=DailyRecoveryTrigger.MANUAL,
            requested_at=observed,
        )
    return {
        "action": result["action"],
        "recovery": result["recovery"].model_dump(mode="json"),
    }


def get_limit_up_pool(source_snapshot_revision: str | None) -> dict:
    """Read one exact Collector-owned catalog match without refreshing it."""

    revision = str(source_snapshot_revision or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", revision) is None:
        raise ValueError("source_snapshot_revision 必须是 64 位小写摘要")
    from tradex.market_watch.limit_up_pool_store import LimitUpPoolStore

    with LimitUpPoolStore(read_only=True) as store:
        pool = store.get_by_source_revision(revision)
    if pool is None:
        raise LookupError("涨停池归属匹配正在由采集进程准备，请稍后重试")
    return pool.model_dump(mode="json")


def get_latest_limit_up_pool(not_after: str | None) -> dict:
    """Read one same-day fallback without weakening the exact revision route."""

    raw = str(not_after or "").strip()
    try:
        cutoff = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("not_after 必须是带时区的 ISO 时间") from exc
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("not_after 必须是带时区的 ISO 时间")
    cutoff = cutoff.astimezone(ZoneInfo("Asia/Shanghai"))
    from tradex.market_watch.limit_up_pool_store import LimitUpPoolStore

    with LimitUpPoolStore(read_only=True) as store:
        pool = store.get_latest_for_trade_date(
            cutoff.date(),
            not_after=cutoff,
        )
    if pool is None:
        raise LookupError("当前交易日尚无可回看的涨停池")
    return pool.model_dump(mode="json")


def get_stock_relationships(symbol: str | None = None) -> dict:
    """Read one immutable catalog revision; this endpoint never refreshes data."""

    from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader

    with InstrumentTaxonomyReader() as reader:
        status = reader.status()
        if status is None:
            raise LookupError("证券关系目录尚未生成，请等待后台刷新")
        profile = None
        if symbol:
            raw = str(symbol).strip().upper()
            matched = re.search(r"\d{6}", raw)
            if matched is None:
                raise ValueError("symbol 必须包含 6 位股票代码")
            code = matched.group(0)
            instrument_id = (
                f"{code}.SH"
                if code.startswith("6")
                else f"{code}.BJ"
                if code.startswith(("4", "8"))
                else f"{code}.SZ"
            )
            profile = reader.get(instrument_id)
            if profile is None:
                raise LookupError(f"{instrument_id} 尚未进入证券关系目录")
    return {
        "contract": "stock_relationship_read.v1",
        "schema_version": 1,
        "catalog_status": status.model_dump(mode="json"),
        "profile": profile.model_dump(mode="json") if profile is not None else None,
    }


class DashboardHandler(BaseHTTPRequestHandler):
    """看板 HTTP 请求处理器。"""

    def do_GET(self):  # noqa: N802 - stdlib 接口命名
        request = urlsplit(self.path)
        if request.path == "/api/market-watch/collection-status":
            self._handle_market_watch_collection_status_api()
        elif request.path == "/api/market-watch/summary":
            query = parse_qs(request.query)
            self._handle_market_watch_summary_api(
                source_snapshot_revision=query.get(
                    "source_snapshot_revision", [None]
                )[0],
            )
        elif request.path == "/api/market-watch/trajectory":
            query = parse_qs(request.query)
            sector_keys = tuple(
                item.strip()
                for value in query.get("sector_keys", [])
                for item in value.split(",")
            )
            self._handle_market_watch_trajectory_api(
                direction=query.get("direction", [None])[0],
                sector_keys=sector_keys,
                source_snapshot_revision=query.get(
                    "source_snapshot_revision", [None]
                )[0],
                trajectory_revision=query.get("trajectory_revision", [None])[0],
            )
        elif request.path == "/api/limit-up-pool":
            query = parse_qs(request.query)
            self._handle_limit_up_pool_api(
                source_snapshot_revision=query.get(
                    "source_snapshot_revision", [None]
                )[0],
            )
        elif request.path == "/api/limit-up-pool/latest":
            query = parse_qs(request.query)
            self._handle_latest_limit_up_pool_api(
                not_after=query.get("not_after", [None])[0],
            )
        elif request.path == "/api/stock-relationships":
            query = parse_qs(request.query)
            self._handle_stock_relationships_api(
                symbol=query.get("symbol", [None])[0],
            )
        elif request.path == "/api/market-watch":
            self._handle_market_watch_legacy_api()
        elif request.path == "/api/market-watch/history":
            query = parse_qs(request.query)
            self._handle_market_watch_history_api(
                trade_date=query.get("trade_date", [None])[0],
                limit=query.get("limit", [None])[0],
            )
        elif request.path == "/api/market-watch/evaluation":
            query = parse_qs(request.query)
            self._handle_market_watch_evaluation_api(
                trade_date=query.get("trade_date", [None])[0],
                days=query.get("days", [None])[0],
            )
        elif request.path == "/api/post-market-review/history":
            query = parse_qs(request.query)
            self._handle_post_market_review_history_api(
                trade_date=query.get("trade_date", [None])[0],
                limit=query.get("limit", [None])[0],
            )
        elif request.path == "/api/post-market-review/generation":
            self._handle_post_market_review_generation_api()
        elif request.path == "/api/daily-stock-selection/history":
            query = parse_qs(request.query)
            self._handle_daily_stock_selection_history_api(
                trade_date=query.get("trade_date", [None])[0],
                limit=query.get("limit", [None])[0],
            )
        elif request.path == "/api/daily-stock-selection/generation":
            self._handle_daily_stock_selection_generation_api()
        elif request.path == "/api/stock-selection/strategies":
            query = parse_qs(request.query)
            self._handle_stock_selection_strategies_api(
                trade_date=query.get("trade_date", [None])[0],
            )
        elif request.path == "/api/stock-selection/results":
            query = parse_qs(request.query)
            self._handle_stock_selection_strategy_results_api(
                trade_date=query.get("trade_date", [None])[0],
                strategy_id=query.get("strategy_id", [None])[0],
                limit=query.get("limit", [None])[0],
            )
        elif request.path == "/api/market":
            query = parse_qs(request.query)
            self._handle_market_api(force=query.get("refresh") == ["1"])
        elif request.path == "/api/risk-appetite":
            query = parse_qs(request.query)
            self._handle_risk_appetite_api(force=query.get("refresh") == ["1"])
        elif request.path == "/api/risk-appetite/series":
            query = parse_qs(request.query)
            self._handle_risk_appetite_series(query.get("trade_date", [None])[0])
        elif request.path == "/api/dashboard":
            self._handle_api()
        elif request.path in ("/", "/index.html"):
            self._handle_html()
        elif request.path in ("/watch/styles.css", "/watch/app.js"):
            self._handle_watch_asset(request.path.rsplit("/", 1)[-1])
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802 - stdlib 接口命名
        request = urlsplit(self.path)
        if request.path == "/api/post-market-review":
            self._handle_post_market_review_api()
        elif request.path == "/api/market-watch/daily-recovery":
            self._handle_market_watch_daily_recovery_api()
        elif request.path == "/api/daily-stock-selection":
            self._handle_daily_stock_selection_api()
        else:
            self.send_error(404)

    def _handle_html(self):
        try:
            body = _get_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _handle_watch_asset(self, name: str):
        try:
            body, content_type = _get_watch_asset(name)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404)
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _handle_api(self):
        try:
            data = get_dashboard_data()
            self._send_json(200, data)
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _handle_market_api(self, force: bool = False):
        try:
            self._send_json(200, get_market_data(force=force))
        except Exception as e:
            self._send_json(502, {"error": str(e)})

    def _handle_market_watch_collection_status_api(self):
        try:
            response = _get_market_watch_web_api().get_collection_status(
                if_none_match=self.headers.get("If-None-Match"),
            )
            self._send_market_watch_response(response)
        except Exception:
            logger.exception("market watch collection status read failed")
            self._send_json(
                502,
                {
                    "contract": "market_watch_read_failed.v1",
                    "schema_version": 1,
                    "error": "盘面采集状态暂不可用",
                },
            )

    def _handle_market_watch_daily_recovery_api(self):
        try:
            result = request_market_watch_daily_recovery()
            self._send_json(202 if result.get("action") == "queued" else 200, result)
        except ValueError as exc:
            self._send_json(409, {"error": str(exc)})
        except Exception:
            logger.exception("market watch daily recovery request failed")
            self._send_json(502, {"error": "收盘完整性检查暂时无法排队"})

    def _handle_market_watch_summary_api(
        self,
        *,
        source_snapshot_revision: str | None,
    ):
        try:
            response = _get_market_watch_web_api().get_summary(
                source_snapshot_revision=source_snapshot_revision,
                if_none_match=self.headers.get("If-None-Match"),
            )
            self._send_market_watch_response(response)
        except Exception:
            logger.exception("market watch summary read failed")
            self._send_json(
                502,
                {
                    "contract": "market_watch_read_failed.v1",
                    "schema_version": 1,
                    "error": "盘面摘要暂不可用",
                },
            )

    def _handle_market_watch_trajectory_api(
        self,
        *,
        direction: str | None,
        sector_keys: tuple[str, ...],
        source_snapshot_revision: str | None,
        trajectory_revision: str | None,
    ):
        try:
            response = _get_market_watch_web_api().get_detail(
                direction=direction,
                sector_keys=sector_keys,
                source_snapshot_revision=source_snapshot_revision,
                trajectory_revision=trajectory_revision,
                if_none_match=self.headers.get("If-None-Match"),
            )
            self._send_market_watch_response(response)
        except Exception:
            logger.exception("market watch trajectory read failed")
            self._send_json(
                502,
                {
                    "contract": "market_watch_read_failed.v1",
                    "schema_version": 1,
                    "error": "盘面轨迹暂不可用",
                },
            )

    def _handle_market_watch_legacy_api(self):
        """Fail visibly instead of serving the retired refresh-owning contract."""

        self._send_json(
            410,
            {
                "contract": "market_watch_api_moved.v1",
                "schema_version": 1,
                "error": "请使用分包的盘面只读接口",
                "endpoints": {
                    "collection_status": "/api/market-watch/collection-status",
                    "summary": "/api/market-watch/summary",
                    "trajectory": "/api/market-watch/trajectory",
                },
            },
        )

    def _handle_limit_up_pool_api(
        self,
        *,
        source_snapshot_revision: str | None,
    ):
        try:
            self._send_json(
                200,
                get_limit_up_pool(source_snapshot_revision),
            )
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except LookupError as exc:
            self._send_json(503, {"error": str(exc)})
        except Exception:
            logger.exception("limit-up catalog pool read failed")
            self._send_json(502, {"error": "涨停池归属匹配暂不可用"})

    def _handle_latest_limit_up_pool_api(
        self,
        *,
        not_after: str | None,
    ):
        try:
            self._send_json(200, get_latest_limit_up_pool(not_after))
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except LookupError as exc:
            self._send_json(503, {"error": str(exc)})
        except Exception:
            logger.exception("latest available limit-up catalog pool read failed")
            self._send_json(502, {"error": "上一版涨停池暂不可用"})

    def _handle_stock_relationships_api(self, *, symbol: str | None):
        try:
            self._send_json(200, get_stock_relationships(symbol))
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except LookupError as exc:
            self._send_json(503, {"error": str(exc)})
        except Exception:
            logger.exception("stock relationship catalog read failed")
            self._send_json(502, {"error": "证券关系目录暂不可用"})

    def _handle_market_watch_history_api(
        self,
        *,
        trade_date: str | None,
        limit: str | None,
    ):
        try:
            self._send_json(
                200,
                get_market_watch_history(
                    trade_date=trade_date,
                    limit=_history_limit(limit),
                ),
            )
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception:
            logger.exception("market watch history read failed")
            self._send_json(502, {"error": "盘面历史暂不可用"})

    def _handle_market_watch_evaluation_api(
        self,
        *,
        trade_date: str | None,
        days: str | None,
    ):
        try:
            self._send_json(
                200,
                get_market_watch_evaluation(
                    trade_date=trade_date,
                    days=_history_days(days),
                ),
            )
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except LookupError as exc:
            self._send_json(503, {"error": str(exc)})
        except Exception:
            logger.exception("market watch evaluation failed")
            self._send_json(502, {"error": "盘面回放评估暂不可用"})

    def _handle_post_market_review_history_api(
        self,
        *,
        trade_date: str | None,
        limit: str | None,
    ):
        try:
            self._send_json(
                200,
                get_post_market_review_history(
                    trade_date=trade_date,
                    limit=_review_history_limit(limit),
                ),
            )
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except LookupError as exc:
            self._send_json(503, {"error": str(exc)})
        except Exception:
            logger.exception("post-market review history read failed")
            self._send_json(502, {"error": "日复盘档案暂不可用"})

    def _handle_post_market_review_api(self):
        try:
            result = generate_post_market_review()
            status = 202 if result.get("state") in {"queued", "running"} else 200
            self._send_json(status, result)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception:
            logger.exception("post-market review enqueue failed")
            self._send_json(502, {"error": "日复盘任务排队失败，请稍后重试"})

    def _handle_post_market_review_generation_api(self):
        try:
            self._send_json(200, get_post_market_review_generation())
        except LookupError as exc:
            self._send_json(503, {"error": str(exc)})
        except Exception:
            logger.exception("post-market review generation status read failed")
            self._send_json(502, {"error": "日复盘任务状态暂不可用"})

    def _handle_daily_stock_selection_history_api(
        self,
        *,
        trade_date: str | None,
        limit: str | None,
    ):
        try:
            self._send_json(
                200,
                get_daily_stock_selection_history(
                    trade_date=trade_date,
                    limit=_selection_history_limit(limit),
                ),
            )
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except LookupError as exc:
            self._send_json(503, {"error": str(exc)})
        except Exception:
            logger.exception("daily stock selection history read failed")
            self._send_json(502, {"error": "每日选股档案暂不可用"})

    def _handle_stock_selection_strategies_api(
        self,
        *,
        trade_date: str | None,
    ):
        try:
            self._send_json(
                200,
                get_stock_selection_strategies(trade_date=trade_date),
            )
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except LookupError as exc:
            self._send_json(503, {"error": str(exc)})
        except Exception:
            logger.exception("stock selection strategy catalog read failed")
            self._send_json(502, {"error": "选股策略清单暂不可用"})

    def _handle_stock_selection_strategy_results_api(
        self,
        *,
        trade_date: str | None,
        strategy_id: str | None,
        limit: str | None,
    ):
        try:
            self._send_json(
                200,
                get_stock_selection_strategy_results(
                    trade_date=trade_date,
                    strategy_id=strategy_id,
                    limit=_selection_history_limit(limit),
                ),
            )
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except LookupError as exc:
            self._send_json(503, {"error": str(exc)})
        except Exception:
            logger.exception("stock selection strategy results read failed")
            self._send_json(502, {"error": "选股策略结果暂不可用"})

    def _handle_daily_stock_selection_api(self):
        try:
            result = generate_daily_stock_selection()
            status = 202 if result.get("state") in {"queued", "running"} else 200
            self._send_json(status, result)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception:
            logger.exception("daily stock selection enqueue failed")
            self._send_json(502, {"error": "每日选股任务排队失败，请稍后重试"})

    def _handle_daily_stock_selection_generation_api(self):
        try:
            self._send_json(200, get_daily_stock_selection_generation())
        except LookupError as exc:
            self._send_json(503, {"error": str(exc)})
        except Exception:
            logger.exception("daily stock selection generation status read failed")
            self._send_json(502, {"error": "每日选股任务状态暂不可用"})

    def _handle_risk_appetite_api(self, force: bool = False):
        try:
            from tradex.dashboard.risk_service import get_risk_appetite_data

            try:
                market_data = get_market_data(force=False)
            except Exception:
                market_data = {}
            self._send_json(
                200,
                get_risk_appetite_data(market_data, force=force),
            )
        except Exception as e:
            self._send_json(502, {"error": str(e)})

    def _handle_risk_appetite_series(self, trade_date: str | None):
        try:
            from tradex.dashboard.risk_service import get_risk_appetite_series

            self._send_json(200, get_risk_appetite_series(trade_date=trade_date))
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            self._send_json(502, {"error": str(e)})

    def _send_json(
        self,
        status: int,
        payload: dict,
        *,
        headers: dict[str, str] | None = None,
    ):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_market_watch_response(self, response):
        """Write already-canonicalized market-watch bytes without reserialization."""

        self.send_response(response.status_code)
        for name, value in response.headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if response.body:
            self.wfile.write(response.body)

    def log_message(self, format, *args):  # noqa: A002 - stdlib 签名
        # 静默默认日志，避免刷屏
        pass


def main():
    port = int(os.environ.get("TRADEX_DASHBOARD_PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"tradex 数据源看板启动: http://127.0.0.1:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n看板已停止")
    finally:
        from tradex.dashboard.risk_service import close_risk_trajectory_store

        close_risk_trajectory_store()
        if _MARKET_WATCH_COLLECTION_STORE is not None:
            _MARKET_WATCH_COLLECTION_STORE.close()
        if _MARKET_WATCH_HISTORY_STORE is not None:
            _MARKET_WATCH_HISTORY_STORE.close()
        if _ANALYSIS_JOB_STORE is not None:
            _ANALYSIS_JOB_STORE.close()
        if _ANALYSIS_JOB_READER is not None:
            _ANALYSIS_JOB_READER.close()
        server.server_close()


if __name__ == "__main__":
    main()
