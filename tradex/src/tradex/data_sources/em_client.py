"""东财统一请求客户端 —— 限流防封 + slist 板块归属。

借鉴 a-stock-data 的 em_get 防封机制：
  - 串行限流：最小间隔 ≥1s + 随机抖动（东财风控：>5次/秒触发封禁）
  - 会话复用 + 默认浏览器 UA + Referer
  - 所有 eastmoney.com 接口都应走 em_get，避免高频被封 IP。

东财风控阈值（社区实测）：
  - 每秒 >5 次 / 并发 ≥10 / 5分钟 ≥300 次 → 触发封禁
"""
from __future__ import annotations

import threading
import time

import pandas as pd
from curl_cffi import requests as _rq

from astock_signals import anti_ban_client as _shared_em_throttle

logger = __import__("logging").getLogger("tradex.em")

# Compatibility aliases keep the machine-local scheduler observable to
# existing diagnostics without creating a second mutable state owner.
_em_next_slot = _shared_em_throttle._em_next_slot
_EM_REQUEST_LOCK = _shared_em_throttle._lock
_EM_THREAD_LOCAL = threading.local()

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36")
_REFERER = "https://quote.eastmoney.com/"


def _get_session():
    """Return a worker-local curl_cffi session.

    curl_cffi/requests sessions carry mutable connection and cookie state and
    are not safe to share between the worker threads used by the MCP server.
    """
    session = getattr(_EM_THREAD_LOCAL, "session", None)
    if session is None:
        session = _rq.Session()
        _EM_THREAD_LOCAL.session = session
    return session


def _reserve_request_slot() -> float:
    """Reserve the shared cross-process Eastmoney/IP request slot."""
    return _shared_em_throttle.reserve_em_request_slot()


def em_get(url: str, params: dict | None = None, headers: dict | None = None,
           timeout: int = 15, **kwargs):
    """东财统一请求入口：有界排队、线程本地 session、默认 UA。

    只在锁内预定下一个 IP 请求时隙；等待和网络请求都在锁外完成。
    当预计等待超过预算时立即交给 SmartRouter 做本次请求的源切换，避免
    东财拥塞占满整个 MCP worker 池。
    """
    wait = _reserve_request_slot()
    if wait > 0:
        time.sleep(wait)
    h = {"User-Agent": _UA, "Referer": _REFERER}
    if headers:
        h.update(headers)
    return _get_session().get(
        url,
        params=params,
        headers=h,
        timeout=timeout,
        impersonate="chrome120",
        **kwargs,
    )


def fetch_stock_boards(code: str, **kwargs) -> pd.DataFrame:
    """个股所属板块/概念归属（东财 slist，一次请求拿全行业/概念/地域 + 龙头股）。

    Returns:
        DataFrame columns: 板块名称 / 板块代码(BK) / 涨跌幅 / 领涨股票
    """
    code = str(code).split(".")[0].split("_")[0]  # 归一纯 6 位
    market_code = 1 if code.startswith("6") else 0
    params = {
        "fltt": "2", "invt": "2",
        "secid": f"{market_code}.{code}",
        "spt": "3", "pi": "0", "pz": "200", "po": "1",
        "fields": "f12,f14,f3,f128",
    }
    r = em_get("https://push2.eastmoney.com/api/qt/slist/get", params=params, timeout=15)
    r.raise_for_status()
    diff = (r.json().get("data") or {}).get("diff") or {}
    items = diff.values() if isinstance(diff, dict) else diff
    rows = []
    for it in items:
        rows.append({
            "板块名称": it.get("f14", ""),
            "板块代码": it.get("f12", ""),
            "涨跌幅": it.get("f3", ""),
            "领涨股票": it.get("f128", ""),
        })
    return pd.DataFrame(rows)
