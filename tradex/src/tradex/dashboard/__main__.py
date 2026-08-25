"""tradex 行情看板 - 轻量 HTTP 服务。

启动：python -m tradex.dashboard
默认端口 8765（环境变量 TRADEX_DASHBOARD_PORT 可配置）

路由：
  GET /                → HTML 盘面看板页面（构建免依赖、本地 CSS/JS）
  GET /api/market-watch → 统一盘面刹车器 market_watch.v1 JSON
  GET /api/market-watch/history → 按交易日返回分钟快照与提醒历史
  GET /api/market-watch/evaluation → 按交易日返回回放验收与噪声评估
  GET /api/post-market-review/history → 返回日复盘档案与次日回看
  POST /api/post-market-review → 20:30 后手动生成当日日复盘
  GET /api/market      → 兼容的指数实时行情 JSON
  GET /api/risk-appetite → 兼容的市场参与度与资金风格信号 JSON
  GET /api/dashboard   → 看板数据 JSON（与 MCP 工具 get_data_source_dashboard 结构一致）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
_MARKET_WATCH_HISTORY_ERROR: Exception | None = None
_POST_MARKET_REVIEW_STORE = None
_POST_MARKET_REVIEW_STORE_LOCK = threading.Lock()
_POST_MARKET_REVIEW_SERVICE = None
_POST_MARKET_REVIEW_SERVICE_LOCK = threading.Lock()
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
    """Return the process-wide owner of minute snapshots and alert history."""

    global _MARKET_WATCH_HISTORY_STORE
    if _MARKET_WATCH_HISTORY_STORE is None:
        with _MARKET_WATCH_HISTORY_STORE_LOCK:
            if _MARKET_WATCH_HISTORY_STORE is None:
                from tradex.market_watch.history import MarketWatchHistoryStore

                _MARKET_WATCH_HISTORY_STORE = MarketWatchHistoryStore()
    return _MARKET_WATCH_HISTORY_STORE


def _record_market_watch_snapshot(snapshot) -> None:
    """Persist a canonical snapshot without making live display depend on disk."""

    global _MARKET_WATCH_HISTORY_ERROR
    try:
        _get_market_watch_history_store().record(snapshot)
    except Exception as exc:  # noqa: BLE001 - history is secondary to the live brake
        _MARKET_WATCH_HISTORY_ERROR = exc
        logger.warning("market watch history record failed: %s", type(exc).__name__)
    else:
        _MARKET_WATCH_HISTORY_ERROR = None


def get_market_watch_data(force: bool = False) -> dict:
    """Return the unified JSON-safe desktop snapshot.

    ``force`` bypasses only the canonical snapshot cache (subject to its short
    double-click guard).  It deliberately does not bypass risk-component TTLs;
    manual refresh therefore rebuilds the view from the newest owned caches
    without starting an extra provider-wide fan-out.
    """

    snapshot = _get_market_watch_service().get(
        force=force,
        stale_while_revalidate=not force,
    )
    _record_market_watch_snapshot(snapshot)
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
        "samples": store.get_timeline(target_date, limit=limit),
        "alerts": store.get_alerts(target_date, limit=limit),
        "recording": {
            "status": "degraded" if _MARKET_WATCH_HISTORY_ERROR else "ready",
            "error": (
                type(_MARKET_WATCH_HISTORY_ERROR).__name__
                if _MARKET_WATCH_HISTORY_ERROR
                else None
            ),
        },
    }


def _evaluation_alerts(events: list[dict]) -> list[dict]:
    """Narrow persisted history envelopes to the evaluator's public input."""

    return [
        {
            "alert": item["alert"],
            "trade_date": item["trade_date"],
            "emitted_at": item["observed_at"],
            "snapshot_id": item["snapshot_id"],
        }
        for item in events
    ]


def get_market_watch_evaluation(
    *,
    trade_date: str | None = None,
    days: int | None = None,
) -> dict:
    """Evaluate one stored session or an explicitly bounded multi-day window."""

    if days is not None:
        if not 1 <= days <= 30:
            raise ValueError("days 必须介于 1 与 30 之间")
        if trade_date is not None:
            raise ValueError("trade_date 与 days 不能同时指定")
        store = _get_market_watch_history_store()
        selected_dates = [
            str(item["trade_date"])
            for item in reversed(store.list_dates(limit=days))
        ]
        samples = [
            item["payload"]
            for item_date in selected_dates
            for item in store.get_timeline(item_date, limit=1000)
        ]
        persisted_alerts = [
            item
            for item_date in selected_dates
            for item in store.get_alerts(item_date, limit=1000)
        ]
        if not samples:
            raise ValueError("尚无可用的多日盘面历史")
        from tradex.market_watch.evaluation import (
            EvaluationConfigV1,
            evaluate_market_watch_history,
        )

        return evaluate_market_watch_history(
            samples,
            alerts=_evaluation_alerts(persisted_alerts),
            config=EvaluationConfigV1(minimum_sessions_for_multi_day=days),
        ).model_dump(mode="json")

    history = get_market_watch_history(trade_date=trade_date, limit=1000)
    from tradex.market_watch.evaluation import evaluate_market_watch_session

    report = evaluate_market_watch_session(
        [item["payload"] for item in history["samples"]],
        alerts=_evaluation_alerts(history["alerts"]),
        trade_date=history["trade_date"],
    )
    return report.model_dump(mode="json")


def _get_post_market_review_store():
    """Return the process-wide immutable daily-review archive owner."""

    global _POST_MARKET_REVIEW_STORE
    if _POST_MARKET_REVIEW_STORE is None:
        with _POST_MARKET_REVIEW_STORE_LOCK:
            if _POST_MARKET_REVIEW_STORE is None:
                from tradex.market_watch.review_store import PostMarketReviewStore

                _POST_MARKET_REVIEW_STORE = PostMarketReviewStore()
    return _POST_MARKET_REVIEW_STORE


def _load_post_market_review_snapshot():
    """Refresh only the canonical view; its domain services retain upstream ownership."""

    return get_market_watch_data(force=True)


def _load_post_market_review_history(trade_date) -> list[dict]:
    """Read the persisted minute envelopes without starting provider refresh."""

    return _get_market_watch_history_store().get_timeline(
        trade_date.isoformat(),
        limit=1000,
    )


def _get_post_market_review_service():
    """Return the sole owner of manual generation and the 21:00 backstop."""

    global _POST_MARKET_REVIEW_SERVICE
    if _POST_MARKET_REVIEW_SERVICE is None:
        with _POST_MARKET_REVIEW_SERVICE_LOCK:
            if _POST_MARKET_REVIEW_SERVICE is None:
                from tradex.market_watch.review_service import PostMarketReviewService

                _POST_MARKET_REVIEW_SERVICE = PostMarketReviewService(
                    _load_post_market_review_snapshot,
                    _get_post_market_review_store(),
                    history_loader=_load_post_market_review_history,
                )
    return _POST_MARKET_REVIEW_SERVICE


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
    """Generate or read back today's one immutable manual review."""

    from tradex.market_watch.review import ReviewTrigger

    return _get_post_market_review_service().generate(trigger=ReviewTrigger.MANUAL)


def get_post_market_review_history(
    *,
    trade_date: str | None = None,
    limit: int = 90,
) -> dict:
    """Return the bounded review archive without starting a market refresh."""

    return _get_post_market_review_service().history(
        trade_date=trade_date,
        limit=limit,
    )


class DashboardHandler(BaseHTTPRequestHandler):
    """看板 HTTP 请求处理器。"""

    def do_GET(self):  # noqa: N802 - stdlib 接口命名
        request = urlsplit(self.path)
        if request.path == "/api/market-watch":
            query = parse_qs(request.query)
            self._handle_market_watch_api(force=query.get("refresh") == ["1"])
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

    def _handle_market_watch_api(self, force: bool = False):
        try:
            payload = get_market_watch_data(force=force)
            if not force and _get_market_watch_service().refreshing:
                self._send_json(
                    200,
                    payload,
                    headers={"X-Tradex-Refresh-State": "background"},
                )
            else:
                self._send_json(200, payload)
        except Exception as exc:
            from tradex.market_watch.service import MarketWatchUnavailableError

            if isinstance(exc, MarketWatchUnavailableError):
                logger.warning(
                    "market watch unavailable: %s",
                    type(exc.cause).__name__,
                )
                alerts = [
                    item.model_dump(mode="json")
                    if hasattr(item, "model_dump")
                    else dict(item)
                    for item in exc.alerts
                ]
                self._send_json(
                    503,
                    {
                        "contract": "market_watch.v1",
                        "schema_version": 1,
                        "error": "盘面快照暂不可用",
                        "alerts": alerts,
                    },
                )
                return
            logger.exception("market watch refresh failed")
            self._send_json(
                502,
                {
                    "contract": "market_watch.v1",
                    "schema_version": 1,
                    "error": "盘面快照刷新失败",
                },
            )

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
        except Exception:
            logger.exception("post-market review history read failed")
            self._send_json(502, {"error": "日复盘档案暂不可用"})

    def _handle_post_market_review_api(self):
        try:
            result = generate_post_market_review()
            status = 201 if result.get("action") == "inserted" else 200
            self._send_json(status, result)
        except Exception as exc:
            from tradex.market_watch.review_service import (
                PostMarketReviewError,
                ReviewSnapshotUnavailableError,
            )

            if isinstance(exc, ReviewSnapshotUnavailableError):
                self._send_json(503, {"error": exc.safe_message})
            elif isinstance(exc, PostMarketReviewError):
                self._send_json(409, {"error": exc.safe_message})
            elif isinstance(exc, ValueError):
                self._send_json(400, {"error": str(exc)})
            else:
                logger.exception("post-market review generation failed")
                self._send_json(502, {"error": "日复盘生成失败，请稍后重试"})

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

    def log_message(self, format, *args):  # noqa: A002 - stdlib 签名
        # 静默默认日志，避免刷屏
        pass


def _risk_sampler_loop(stop_event: threading.Event) -> None:
    """Produce one replayable market snapshot per open-market minute."""

    from tradex.dashboard.risk_service import get_risk_appetite_data

    while not stop_event.is_set():
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        if _market_state(now)["is_open"]:
            try:
                market_data = get_market_data(force=True)
                if _market_payload_is_current(market_data, now):
                    get_risk_appetite_data(
                        market_data,
                        force=True,
                        record_trajectory=True,
                    )
                    # MarketWatchService remains the sole alert-state owner.
                    # Its fetcher consumes the just-populated market/risk caches,
                    # then the public adapter stores the strict canonical result.
                    get_market_watch_data(force=True)
            except Exception as exc:  # noqa: BLE001 - next minute is the retry boundary
                logger.warning("market watch sampler failed: %s", exc)
        after = datetime.now(ZoneInfo("Asia/Shanghai"))
        delay = max(1.0, 60.0 - after.second - after.microsecond / 1_000_000)
        stop_event.wait(delay)


def _post_market_review_loop(stop_event: threading.Event) -> None:
    """Let the domain service own the 21:00 due check and bounded retries."""

    while not stop_event.is_set():
        try:
            result = _get_post_market_review_service().maybe_generate_automatic()
            if result.get("action") == "inserted":
                logger.info("automatic post-market review archived")
        except Exception as exc:  # noqa: BLE001 - service owns retry exhaustion/cooldown
            from tradex.market_watch.review_service import PostMarketReviewError

            if isinstance(exc, PostMarketReviewError):
                logger.warning(
                    "automatic post-market review unavailable: %s",
                    exc.safe_message,
                )
            else:
                logger.exception("automatic post-market review loop failed")
        stop_event.wait(30.0)


def main():
    port = int(os.environ.get("TRADEX_DASHBOARD_PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    sampler_stop = threading.Event()
    sampler = threading.Thread(
        target=_risk_sampler_loop,
        args=(sampler_stop,),
        name="market-watch-sampler",
        daemon=True,
    )
    sampler.start()
    review_stop = threading.Event()
    review_scheduler = threading.Thread(
        target=_post_market_review_loop,
        args=(review_stop,),
        name="post-market-review-scheduler",
        daemon=False,
    )
    review_scheduler.start()
    print(f"tradex 数据源看板启动: http://127.0.0.1:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n看板已停止")
    finally:
        sampler_stop.set()
        review_stop.set()
        sampler.join(timeout=2)
        # A review may be finishing a bounded provider read.  Wait for that
        # owner to leave the store before closing the SQLite connection.
        review_scheduler.join()
        from tradex.dashboard.risk_service import close_risk_trajectory_store

        close_risk_trajectory_store()
        if _MARKET_WATCH_HISTORY_STORE is not None:
            _MARKET_WATCH_HISTORY_STORE.close()
        if _POST_MARKET_REVIEW_STORE is not None:
            _POST_MARKET_REVIEW_STORE.close()
        server.server_close()


if __name__ == "__main__":
    main()
