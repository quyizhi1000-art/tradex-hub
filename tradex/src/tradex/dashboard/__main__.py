"""tradex 行情看板 - 轻量 HTTP 服务。

启动：python -m tradex.dashboard
默认端口 8765（环境变量 TRADEX_DASHBOARD_PORT 可配置）

路由：
  GET /                → HTML 看板页面（单页应用，CSS/JS 全内联）
  GET /api/market      → 上证指数与深证成指实时行情 JSON
  GET /api/risk-appetite → 市场参与度与资金风格信号 JSON
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
logger = logging.getLogger(__name__)


def _get_html() -> str:
    """读取 index.html（与本文件同目录）。"""
    html_path = Path(__file__).parent / "index.html"
    return html_path.read_text(encoding="utf-8")


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
    """获取沪深两大指数快照，并做短时缓存避免频繁访问上游。"""
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


class DashboardHandler(BaseHTTPRequestHandler):
    """看板 HTTP 请求处理器。"""

    def do_GET(self):  # noqa: N802 - stdlib 接口命名
        request = urlsplit(self.path)
        if request.path == "/api/market":
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

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - stdlib 签名
        # 静默默认日志，避免刷屏
        pass


def _risk_sampler_loop(stop_event: threading.Event) -> None:
    """Produce one server-side risk snapshot per open-market minute."""

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
            except Exception as exc:  # noqa: BLE001 - next minute is the retry boundary
                logger.warning("risk appetite sampler failed: %s", exc)
        after = datetime.now(ZoneInfo("Asia/Shanghai"))
        delay = max(1.0, 60.0 - after.second - after.microsecond / 1_000_000)
        stop_event.wait(delay)


def main():
    port = int(os.environ.get("TRADEX_DASHBOARD_PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    sampler_stop = threading.Event()
    sampler = threading.Thread(
        target=_risk_sampler_loop,
        args=(sampler_stop,),
        name="risk-appetite-sampler",
        daemon=True,
    )
    sampler.start()
    print(f"tradex 数据源看板启动: http://127.0.0.1:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n看板已停止")
    finally:
        sampler_stop.set()
        sampler.join(timeout=2)
        from tradex.dashboard.risk_service import close_risk_trajectory_store

        close_risk_trajectory_store()
        server.server_close()


if __name__ == "__main__":
    main()
