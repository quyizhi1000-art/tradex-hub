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
import math
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

_TOOL_COUNT: int | None = None
_MARKET_CACHE: dict | None = None
_MARKET_CACHE_AT = 0.0
_MARKET_REFRESH_LOCK = threading.Lock()
_MARKET_CACHE_TTL = 10
_MARKET_MIN_FORCE_INTERVAL = 30
logger = logging.getLogger(__name__)

_INDEX_SPECS = (
    {"code": "sh000001", "name": "上证指数", "aliases": ("上证指数", "上证综合指数")},
    {"code": "sz399001", "name": "深证成指", "aliases": ("深证成指", "深证指数")},
)


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


def _number(value) -> float | None:
    """将行情字段安全转换为有限浮点数。"""
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _field(row: dict, *names: str):
    """兼容不同数据源的同义字段名。"""
    normalised = {str(key).strip().lower().replace(" ", ""): value for key, value in row.items()}
    for name in names:
        key = name.strip().lower().replace(" ", "")
        if key in normalised:
            return normalised[key]
    return None


def _matches_index(row: dict, spec: dict) -> bool:
    raw_code = str(_field(row, "代码", "指数代码", "code", "symbol") or "").lower()
    compact_code = raw_code.replace(".", "").replace("_", "")
    expected = spec["code"]
    if compact_code in (expected, expected[2:], expected[2:] + expected[:2]):
        return True
    raw_name = str(_field(row, "指数名称", "名称", "name") or "")
    return any(alias in raw_name for alias in spec["aliases"])


def _normalise_indices(records: list[dict], source: str) -> list[dict]:
    """从行情源结果中提取并统一沪深两大指数字段。"""
    indices: list[dict] = []
    for spec in _INDEX_SPECS:
        row = next((item for item in records if _matches_index(item, spec)), None)
        if row is None:
            indices.append({"code": spec["code"], "name": spec["name"], "available": False})
            continue

        price = _number(_field(row, "最新点位", "最新价", "最新", "现价", "price", "close", "收盘"))
        change = _number(_field(row, "涨跌额", "涨跌", "change"))
        change_pct = _number(_field(row, "涨跌幅", "涨跌幅(%)", "change_pct", "pct_chg"))
        previous_close = _number(_field(row, "昨收", "昨日收盘", "前收盘", "previous_close", "pre_close"))
        if previous_close is None and price is not None and change is not None:
            previous_close = price - change

        amount = _number(_field(row, "成交额(元)", "成交额", "amount", "turnover"))
        # 腾讯 qt.gtimg.cn 的成交额字段单位为万元，内部 fetcher 保留了原值。
        if amount is not None and source == "tencent_http":
            amount *= 10_000

        indices.append({
            "code": spec["code"],
            "name": spec["name"],
            "available": price is not None,
            "price": price,
            "change": change,
            "change_pct": change_pct,
            "previous_close": previous_close,
            "open": _number(_field(row, "今开", "开盘", "open")),
            "high": _number(_field(row, "最高", "最高价", "high")),
            "low": _number(_field(row, "最低", "最低价", "low")),
            "amount": amount,
        })
    return indices


def _normalise_participation_indices(records: list[dict]) -> list[dict]:
    """保留市场参与度模型需要的全量指数名称、代码与涨跌幅。"""

    result = []
    for row in records:
        change_pct = _number(_field(row, "涨跌幅", "涨跌幅(%)", "change_pct", "pct_chg"))
        name = _field(row, "指数名称", "名称", "name")
        code = _field(row, "代码", "指数代码", "code", "symbol")
        if change_pct is None or (name is None and code is None):
            continue
        result.append({
            "名称": str(name or ""),
            "代码": str(code or ""),
            "涨跌幅": change_pct,
            "更新时间": _field(row, "更新时间", "provider_as_of"),
        })
    return result


def _market_state(now: datetime) -> dict:
    """返回中国 A 股常规交易时段状态（不推断节假日）。"""
    minute = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return {"label": "周末休市", "is_open": False}
    if minute < 9 * 60 + 15:
        return {"label": "等待开盘", "is_open": False}
    if minute < 9 * 60 + 30:
        return {"label": "集合竞价", "is_open": False}
    if minute <= 11 * 60 + 30 or 13 * 60 <= minute <= 15 * 60:
        return {"label": "盘中交易", "is_open": True}
    if minute < 13 * 60:
        return {"label": "午间休市", "is_open": False}
    return {"label": "今日收盘", "is_open": False}


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


def _sum_amount_until(series: list[dict], trading_date: str, as_of: str) -> float:
    """累计某交易日从开盘至指定分钟（含）的成交额。"""
    return sum(
        amount
        for item in series or []
        if str(item.get("date") or "") == trading_date
        and str(item.get("time") or "")[:5] <= as_of
        and (amount := _number(item.get("amount"))) is not None
    )


def _build_market_turnover(series_by_code: dict[str, list[dict]], now: datetime) -> dict:
    """计算大 A 全市今日此时与上一交易日同一时刻的累计成交额差。"""
    today_date = now.date().isoformat()
    required_codes = {spec["code"] for spec in _INDEX_SPECS}
    if set(series_by_code) != required_codes:
        return {"available": False, "reason": "沪深成交额数据不完整"}

    dates_by_code: dict[str, set[str]] = {}
    latest_times: list[str] = []
    for code, series in series_by_code.items():
        dates = {str(item.get("date") or "") for item in series if item.get("date")}
        dates_by_code[code] = dates
        today_times = [
            str(item.get("time") or "")[:5]
            for item in series
            if str(item.get("date") or "") == today_date and _number(item.get("amount")) is not None
        ]
        if not today_times:
            return {"available": False, "reason": "今日尚无成交额"}
        latest_times.append(max(today_times))

    # 采用沪深两市都已返回的最新分钟，保证比较时点完全一致。
    as_of = min(latest_times)
    common_dates = set.intersection(*(dates for dates in dates_by_code.values()))
    previous_dates = sorted(date for date in common_dates if date < today_date)
    if not previous_dates:
        return {"available": False, "reason": "上一交易日同期数据暂缺"}
    previous_date = previous_dates[-1]

    today_amount = sum(
        _sum_amount_until(series, today_date, as_of) for series in series_by_code.values()
    )
    previous_same_time_amount = sum(
        _sum_amount_until(series, previous_date, as_of) for series in series_by_code.values()
    )
    if today_amount <= 0 or previous_same_time_amount <= 0:
        return {"available": False, "reason": "同期成交额暂缺"}

    difference = today_amount - previous_same_time_amount
    if difference > 0:
        direction, label = "expand", "放量"
    elif difference < 0:
        direction, label = "shrink", "缩量"
    else:
        direction, label = "flat", "持平"

    return {
        "available": True,
        "scope": "all_a_shares",
        "metric": "amount",
        "as_of": as_of,
        "today_date": today_date,
        "previous_date": previous_date,
        "today_amount": today_amount,
        "previous_same_time_amount": previous_same_time_amount,
        "difference": difference,
        "direction": direction,
        "label": label,
    }


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

    from tradex.data_sources import get_router, register_all_sources

    register_all_sources()
    frame, source = get_router().route("market_overview")
    if frame is None or not hasattr(frame, "to_dict"):
        raise RuntimeError("行情源返回了无法识别的数据")

    records = frame.to_dict(orient="records")
    indices = _normalise_indices(records, source)
    if not any(item.get("available") for item in indices):
        raise RuntimeError("行情结果中未找到上证指数或深证成指")

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    try:
        from tradex.data_sources.akshare_fetchers import fetch_index_intraday_amount

        series_by_code = {
            spec["code"]: fetch_index_intraday_amount(symbol=spec["code"], days=5)
            for spec in _INDEX_SPECS
        }
        market_turnover = _build_market_turnover(series_by_code, now)
    except Exception:
        market_turnover = {"available": False, "reason": "全市场同期成交额暂不可用"}
    source_labels = {"akshare": "AKShare 行情", "tencent_http": "腾讯行情"}
    payload = {
        "timestamp": now.isoformat(timespec="seconds"),
        "provider_as_of": max(
            (
                str(value)
                for row in records
                if (value := _field(row, "更新时间", "provider_as_of"))
            ),
            default=None,
        ),
        "market_state": _market_state(now),
        "source": source,
        "source_label": source_labels.get(source, source),
        "indices": indices,
        "participation_indices": _normalise_participation_indices(records),
        "market_turnover": market_turnover,
    }
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
