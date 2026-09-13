"""
HTTP 直连数据源 fetch_fn 包装器。

纯 HTTP 抓取的数据源（不依赖 akshare），主要是腾讯行情接口。
仅本文件允许直接发起 HTTP 请求获取行情数据。

全局行情（global_market_quote）通过腾讯 qt.gtimg.cn 批量获取，
支持美股/大宗/亚太指数/外汇等外围行情。
"""

from __future__ import annotations

import logging
import os
import re
import time
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from astock_signals.shared_rate_limit import (
    SharedRateLimitExceeded,
    SharedRateLimitUnavailable,
    reserve_shared_request_slot,
)
from astock_signals.smart_router import RequestValidationError, SourceBusyError

logger = logging.getLogger("tradex.http")
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_EASTMONEY_BOARD_CODE = re.compile(r"^BK\d+$")

# 全局直连 opener（绕过系统代理，避免代理失败）
_NO_PROXY_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(),
)


def _urlopen_no_proxy(url: str, timeout: int = 10) -> object:
    """Throttle the free Tencent endpoint across processes, then connect."""
    try:
        interval = float(os.getenv("TENCENT_RATE_LIMIT_INTERVAL", "0.5"))
        max_wait = float(os.getenv("TENCENT_MAX_QUEUE_WAIT", "4.0"))
    except ValueError:
        interval, max_wait = 0.5, 4.0
    try:
        wait = reserve_shared_request_slot(
            "free:tencent:qt",
            min_interval=max(0.0, interval),
            max_wait=max(0.0, max_wait),
        )
    except (SharedRateLimitExceeded, SharedRateLimitUnavailable):
        raise SourceBusyError("Tencent request queue is busy") from None
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    return _NO_PROXY_OPENER.open(req, timeout=timeout)


def _provider_time_iso(value) -> str | None:
    """Normalise a provider-supplied timestamp without inventing a local time."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw in {"-", "0"}:
        return None

    parsed = None
    if raw.isdigit() and len(raw) in {12, 14}:
        pattern = "%Y%m%d%H%M%S" if len(raw) == 14 else "%Y%m%d%H%M"
        try:
            parsed = datetime.strptime(raw, pattern)
        except ValueError:
            return None
    elif raw.isdigit():
        try:
            timestamp = float(raw)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            parsed = datetime.fromtimestamp(timestamp, _SHANGHAI_TZ)
        except (OSError, OverflowError, ValueError):
            return None
    else:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(raw, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI_TZ)
    else:
        parsed = parsed.astimezone(_SHANGHAI_TZ)
    return parsed.isoformat(timespec="seconds")


def _optional_float(value) -> float | None:
    """Convert an optional provider number without turning missing data into zero."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value in {"", "-", "--"}:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value) -> int | None:
    number = _optional_float(value)
    return None if number is None else int(number)


def _optional_text(value) -> str | None:
    """Return a trimmed provider string without inventing an empty value."""
    if value is None:
        return None
    result = str(value).strip()
    return result if result and result not in {"-", "--"} else None


def _eastmoney_secid(symbol: str, *, index: bool = False) -> tuple[str, str]:
    raw = str(symbol or "").strip().lower()
    if not raw:
        raise RequestValidationError("Eastmoney minute symbol is required")
    if raw.startswith("sh"):
        code, market = raw[2:].zfill(6), "1"
    elif raw.startswith("sz"):
        code, market = raw[2:].zfill(6), "0"
    elif "." in raw:
        code, suffix = raw.split(".", 1)
        market = "1" if suffix in {"sh", "1"} else "0"
        code = code.zfill(6)
    else:
        code = raw.zfill(6)
        market = "1" if (index and code in {"000001", "000300", "000852", "999999"}) or code.startswith("6") else "0"
    if not re.fullmatch(r"\d{6}", code):
        raise RequestValidationError("Eastmoney minute symbol must contain six digits")
    return f"{market}.{code}", code


def _fetch_eastmoney_trends(symbol: str, *, days: int, index: bool) -> tuple[list[list[str]], str, str | None]:
    from tradex.data_sources.em_client import em_get

    secid, code = _eastmoney_secid(symbol, index=index)
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "ndays": str(max(1, min(int(days), 5))),
        "iscr": "0",
        "secid": secid,
    }
    errors: list[str] = []
    for host in ("https://push2his.eastmoney.com", "https://push2delay.eastmoney.com"):
        source = "push2his" if "push2his" in host else "push2delay"
        try:
            response = em_get(
                f"{host}/api/qt/stock/trends2/get",
                params=params,
                timeout=10,
            )
            response.raise_for_status()
            trends = (response.json().get("data") or {}).get("trends") or []
            parsed = [str(item).split(",") for item in trends]
            parsed = [item for item in parsed if len(item) >= 7 and " " in item[0]]
            if parsed:
                return parsed, source, response.headers.get("x-request-id")
            errors.append(f"{source}: empty")
        except Exception as exc:  # noqa: BLE001 - bounded alternate host
            errors.append(f"{source}: {type(exc).__name__}")
    raise RuntimeError(f"Eastmoney exact minute series unavailable for {code}: {'; '.join(errors)}")


def fetch_minute_data_eastmoney(
    symbol: str = "",
    code: str = "",
    **_kwargs,
) -> pd.DataFrame:
    """Fetch one exact current-day stock minute series with bounded host fallback."""

    parsed, source, request_id = _fetch_eastmoney_trends(
        symbol or code,
        days=1,
        index=False,
    )
    _, canonical_code = _eastmoney_secid(symbol or code)
    rows = [
        {
            "代码": canonical_code,
            "时间": item[0],
            "开盘": _optional_float(item[1]),
            "收盘": _optional_float(item[2]),
            "最高": _optional_float(item[3]),
            "最低": _optional_float(item[4]),
            "成交量": _optional_float(item[5]),
            "成交额": _optional_float(item[6]),
        }
        for item in parsed
    ]
    frame = pd.DataFrame(rows)
    frame.attrs.update(
        provider_as_of=rows[-1]["时间"],
        provider_request_id=request_id,
        trading_date=rows[-1]["时间"][:10],
        frequency_minutes=1,
        volume_unit="lots",
        amount_unit="CNY",
        provider_transport=source,
    )
    return frame


def fetch_index_intraday_series_eastmoney(
    symbol: str = "",
    code: str = "",
    days: int = 5,
    **_kwargs,
) -> list[dict]:
    """Fetch exact index minute OHLC and amount values."""

    parsed, source, request_id = _fetch_eastmoney_trends(
        symbol or code,
        days=days,
        index=True,
    )
    return [
        {
            "datetime": item[0],
            "open": _optional_float(item[1]),
            "close": _optional_float(item[2]),
            "high": _optional_float(item[3]),
            "low": _optional_float(item[4]),
            "amount_cny": _optional_float(item[6]),
            "provider_transport": source,
            "provider_request_id": request_id,
        }
        for item in parsed
    ]


def fetch_index_intraday_amount_eastmoney(**kwargs) -> list[dict]:
    return [
        {
            "datetime": item["datetime"],
            "date": item["datetime"][:10],
            "time": item["datetime"][11:16],
            "amount": item["amount_cny"],
        }
        for item in fetch_index_intraday_series_eastmoney(**kwargs)
    ]


def fetch_sector_intraday_fund_flow_eastmoney(
    provider_sector_code: str = "",
    trade_date: date | str | None = None,
    max_queue_wait: float | None = None,
    request_timeout: int = 10,
    **_kwargs,
) -> pd.DataFrame:
    """Fetch the exact Eastmoney board minute main-net-flow curve.

    This is deliberately a distinct capability from daily board fund flow and
    from price/turnover minute data.  A successful response therefore always
    contains provider timestamps plus cumulative main-net amounts in yuan.
    """

    code = str(provider_sector_code).strip().upper()
    if not _EASTMONEY_BOARD_CODE.fullmatch(code):
        raise RequestValidationError("provider_sector_code must match BK plus digits")
    if trade_date is None:
        requested_date = datetime.now(_SHANGHAI_TZ).date()
    elif isinstance(trade_date, date):
        requested_date = trade_date
    else:
        try:
            requested_date = date.fromisoformat(str(trade_date))
        except ValueError as exc:
            raise RequestValidationError("trade_date must be ISO YYYY-MM-DD") from exc

    from tradex.data_sources.em_client import em_get

    params = {
        "secid": f"90.{code}",
        "klt": 1,
        "lmt": 500,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
    }
    headers = {
        "Referer": "https://data.eastmoney.com/",
        "User-Agent": "Mozilla/5.0",
    }
    rows: list[dict[str, object]] = []
    response = None
    provider_transport = None
    for transport, host in (
        ("push2", "https://push2.eastmoney.com"),
        ("push2delay", "https://push2delay.eastmoney.com"),
    ):
        try:
            candidate = em_get(
                f"{host}/api/qt/stock/fflow/kline/get",
                params=params,
                headers=headers,
                timeout=max(1, int(request_timeout)),
                max_queue_wait=max_queue_wait,
            )
            candidate.raise_for_status()
            payload = candidate.json()
        except SourceBusyError:
            raise
        except Exception as exc:  # noqa: BLE001 - mirror is the bounded fallback
            logger.warning(
                "sector minute fund flow %s failed for %s: %s",
                transport,
                code,
                exc,
            )
            continue

        candidate_rows: list[dict[str, object]] = []
        for raw in ((payload.get("data") or {}).get("klines") or []):
            parts = str(raw).split(",")
            if len(parts) < 2:
                continue
            try:
                provider_time = datetime.strptime(
                    parts[0], "%Y-%m-%d %H:%M"
                ).replace(tzinfo=_SHANGHAI_TZ)
                cumulative = float(parts[1])
            except (TypeError, ValueError):
                continue
            if provider_time.date() != requested_date:
                continue
            candidate_rows.append(
                {
                    "provider_as_of": provider_time.isoformat(timespec="seconds"),
                    "main_net_inflow_cny": cumulative,
                }
            )
        if not candidate_rows:
            logger.warning(
                "sector minute fund flow %s returned no rows for %s on %s",
                transport,
                code,
                requested_date,
            )
            continue
        rows = candidate_rows
        response = candidate
        provider_transport = transport
        break
    if not rows:
        raise RuntimeError(
            f"Eastmoney returned no sector minute fund flow for {code} on {requested_date}"
        )
    frame = pd.DataFrame(rows)
    frame.attrs["provider_sector_code"] = code
    frame.attrs["provider_request_id"] = response.headers.get("x-request-id")
    frame.attrs["provider_transport"] = provider_transport
    return frame


def _tencent_quote_vals(code: str) -> list:
    """从腾讯 qt.gtimg.cn 获取个股行情，返回 ~ 分隔的值列表。"""
    prefix = "sh" if code.startswith("6") else "sz"
    url = f"https://qt.gtimg.cn/q={prefix}{code}"
    resp = _urlopen_no_proxy(url, timeout=5)
    raw = resp.read().decode("gbk")
    if '"' not in raw:
        raise RuntimeError("tencent quote: no data")
    return raw.split('"')[1].split("~")


def fetch_realtime_quote_tencent(symbol: str = "", code: str = "", **kwargs):
    """实时行情（腾讯 qt.gtimg.cn）。返回单行 DataFrame，含'代码'列。

    作为 realtime_quote 的 priority=200 兜底源。
    兼容 symbol/code 两种参数名（SmartRouter 路由归一化）。
    """
    import pandas as pd
    sym = symbol or code
    if not sym:
        raise RuntimeError("stock code is required (symbol or code)")
    vals = _tencent_quote_vals(sym)
    if len(vals) < 50:
        raise RuntimeError("tencent returned insufficient data")
    return pd.DataFrame([{
        "代码": sym,
        "名称": vals[1] if len(vals) > 1 else "",
        "最新价": float(vals[3]) if vals[3] else 0,
        "涨跌幅": float(vals[32]) if len(vals) > 32 and vals[32] else 0,
        "成交量": float(vals[36]) if len(vals) > 36 and vals[36] else 0,
        "成交额": float(vals[37]) if len(vals) > 37 and vals[37] else 0,
        "最高": float(vals[33]) if len(vals) > 33 and vals[33] else 0,
        "最低": float(vals[34]) if len(vals) > 34 and vals[34] else 0,
        "今开": float(vals[5]) if len(vals) > 5 and vals[5] else 0,
        "昨收": float(vals[4]) if len(vals) > 4 and vals[4] else 0,
        "总市值": float(vals[45]) if len(vals) > 45 and vals[45] else 0,
        "流通市值": float(vals[44]) if len(vals) > 44 and vals[44] else 0,
        "市盈率": float(vals[39]) if len(vals) > 39 and vals[39] else 0,
    }])


def _tencent_a_share_prefix(code: str) -> str:
    """Return the Tencent market prefix for a six-digit A-share code."""
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"invalid A-share code: {code!r}")
    if code.startswith("6"):
        return "sh"
    if code.startswith(("0", "3")):
        return "sz"
    if code.startswith(("4", "8", "9")):
        return "bj"
    raise ValueError(f"unsupported A-share code: {code!r}")


def fetch_realtime_quotes_tencent(symbols) -> pd.DataFrame:
    """Batch-fetch fixed A-share representatives from Tencent in one request.

    Args:
        symbols: A comma-separated string or iterable of six-digit A-share codes.

    Returns:
        DataFrame columns: 代码 / 名称 / 最新价 / 涨跌幅 / 成交额 / 流通市值.
        Duplicate input codes are removed while preserving their first-seen order.
    """
    if isinstance(symbols, str):
        raw_symbols = symbols.split(",")
    else:
        try:
            raw_symbols = list(symbols)
        except TypeError as exc:
            raise ValueError("symbols must be a string or iterable of A-share codes") from exc

    codes = []
    seen = set()
    for value in raw_symbols:
        code = str(value).strip()
        prefix = _tencent_a_share_prefix(code)
        if code not in seen:
            codes.append((code, prefix))
            seen.add(code)
    if not codes:
        raise ValueError("at least one A-share code is required")

    url = "https://qt.gtimg.cn/q=" + ",".join(
        f"{prefix}{code}" for code, prefix in codes
    )
    resp = _urlopen_no_proxy(url, timeout=10)
    raw = resp.read().decode("gbk")

    parsed = {}
    for line in raw.strip().split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, raw_val = line.split("=", 1)
        quote_key = key.strip().removeprefix("v_")
        fields = raw_val.strip().strip('"').split("~")
        if len(fields) < 45:
            continue
        code = quote_key[2:]
        if code not in seen:
            continue
        parsed[code] = {
            "代码": code,
            "名称": fields[1] if len(fields) > 1 else "",
            "最新价": float(fields[3]) if fields[3] else 0.0,
            "涨跌幅": float(fields[32]) if fields[32] else 0.0,
            "成交额": float(fields[37]) if fields[37] else 0.0,
            "流通市值": float(fields[44]) if fields[44] else 0.0,
        }

    rows = [parsed[code] for code, _prefix in codes if code in parsed]
    if not rows:
        raise RuntimeError("tencent A-share batch returned empty")
    return pd.DataFrame(rows)


def fetch_profit_forecast_tencent(symbol: str = "", code: str = "", **kwargs) -> dict:
    """一致预期（腾讯行情兜底）。仅返回价格/PE，无 EPS 预测数据。

    作为 profit_forecast 的 priority=100 备源（当同花顺抓取失败时）。
    兼容 symbol/code 两种参数名（SmartRouter 路由归一化）。
    """
    sym = symbol or code
    if not sym:
        raise RuntimeError("stock code is required (symbol or code)")
    vals = _tencent_quote_vals(sym)
    if len(vals) < 50:
        raise RuntimeError("tencent returned insufficient data for profit_forecast")
    price = float(vals[3]) if vals[3] else 0
    pe_ttm = float(vals[39]) if len(vals) > 39 and vals[39] else 0
    return {
        "symbol": sym,
        "source": "tencent qt.gtimg.cn (price only)",
        "price": price,
        "pe_ttm": pe_ttm,
        "forecasts": [],
        "summary": "同花顺 EPS 抓取失败，仅返回腾讯实时价格/PE",
    }


# ============================================================
# 全局行情批量获取 — global_market_quote
# ============================================================

# 腾讯接口支持的全局行情代码表
# 格式: (code, 类别, 中文名称)
# 注意：腾讯接口不支持大宗商品(hf_*)、A50期货(int_fta50)、日经(int_nikkei)、KOSPI、外汇汇率(usUSDCNH)
GLOBAL_QUOTE_CODES = [
    # 美股指数
    ("usDJI", "美股指数", "道琼斯"),
    ("usIXIC", "美股指数", "纳斯达克"),
    ("usINX", "美股指数", "标普500"),
    # 热门美股
    ("usNVDA", "热门美股", "英伟达"),
    ("usTSLA", "热门美股", "特斯拉"),
    ("usAAPL", "热门美股", "苹果"),
    ("usMSFT", "热门美股", "微软"),
    ("usAMZN", "热门美股", "亚马逊"),
    ("usGOOGL", "热门美股", "谷歌"),
    ("usMETA", "热门美股", "Meta"),
    ("usMU", "热门美股", "美光科技"),
    ("usAMAT", "热门美股", "应用材料"),
    # 亚太指数
    ("hkHSI", "亚太指数", "恒生指数"),
    ("hkHSTECH", "亚太指数", "恒生科技"),
    # 韩股龙头
    ("kr005930", "韩股", "三星电子"),
    ("kr000660", "韩股", "SK海力士"),
    # 外汇
    ("whDINIW", "外汇", "美元指数"),
]


def _tencent_global_batch() -> pd.DataFrame:
    """批量获取腾讯全局行情（单次请求拉取所有预设代码）。

    Returns:
        DataFrame with columns: 代码, 名称, 类别, 最新价, 涨跌额, 涨跌幅, 昨收, 今开, 最高, 最低, 更新时间
    """
    codes = [c[0] for c in GLOBAL_QUOTE_CODES]
    url = "https://qt.gtimg.cn/q=" + ",".join(codes)
    resp = _urlopen_no_proxy(url, timeout=10)
    raw = resp.read().decode("gbk")

    rows = []
    for line in raw.strip().split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        parts = line.split("=", 1)
        key = parts[0].strip().replace("v_", "")
        raw_val = parts[1].strip().strip('"')
        fields = raw_val.split("~")

        # 查找代码对应的类别
        category = ""
        for c, cat, _ in GLOBAL_QUOTE_CODES:
            if c == key:
                category = cat
                break

        name = fields[1] if len(fields) > 1 else key
        price = float(fields[3]) if len(fields) > 3 and fields[3] else 0.0
        last_close = float(fields[4]) if len(fields) > 4 and fields[4] else 0.0
        open_px = float(fields[5]) if len(fields) > 5 and fields[5] else 0.0
        high = float(fields[33]) if len(fields) > 33 and fields[33] else 0.0
        low = float(fields[34]) if len(fields) > 34 and fields[34] else 0.0
        change = float(fields[31]) if len(fields) > 31 and fields[31] else 0.0
        change_pct = float(fields[32]) if len(fields) > 32 and fields[32] else 0.0
        ts = fields[30] if len(fields) > 30 else ""

        rows.append({
            "代码": key,
            "名称": name,
            "类别": category,
            "最新价": price,
            "涨跌额": change,
            "涨跌幅": change_pct,
            "昨收": last_close,
            "今开": open_px,
            "最高": high,
            "最低": low,
            "更新时间": ts,
        })

    return pd.DataFrame(rows)


def fetch_global_quote_tencent(**kwargs) -> pd.DataFrame:
    """全局行情批量获取（腾讯 qt.gtimg.cn）。

    一次性获取美股/大宗/亚太指数/热门股/外汇共 30+ 个品种的实时行情。
    作为 global_market_quote 数据类型的唯一源。

    Returns:
        DataFrame with columns: 代码, 名称, 类别, 最新价, 涨跌额, 涨跌幅, 昨收, 今开, 最高, 最低, 更新时间
    """
    try:
        df = _tencent_global_batch()
        if df is None or df.empty:
            raise RuntimeError("tencent global batch returned empty")
        return df
    except Exception as e:
        logger.warning("fetch_global_quote_tencent failed: %s", e)
        raise


def fetch_market_overview_tencent(**kwargs) -> pd.DataFrame:
    """A股主要指数行情（腾讯 qt.gtimg.cn 兜底）。

    作为 market_overview 的备用源，解决 akshare 代理失败问题。
    获取上证指数、深证成指、创业板指等主要指数行情。

    Returns:
        DataFrame with columns: 代码, 名称, 最新价, 涨跌幅, 涨跌额, 成交额, 成交量
    """
    # 腾讯A股指数代码：sh000001(上证), sz399001(深证), sz399006(创业板), sz399852(中证1000), sh000688(科创50), sh000300(沪深300), sh000905(中证500)
    index_codes = ["sh000001", "sz399001", "sz399006", "sh000688", "sh000300", "sh000905", "sz399852"]
    url = "https://qt.gtimg.cn/q=" + ",".join(index_codes)
    try:
        resp = _urlopen_no_proxy(url, timeout=10)
        raw = resp.read().decode("gbk")
        rows = []
        for line in raw.strip().split(";"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            parts = line.split("=", 1)
            raw_val = parts[1].strip().strip('"')
            fields = raw_val.split("~")
            name = fields[1] if len(fields) > 1 else ""
            price = float(fields[3]) if len(fields) > 3 and fields[3] else 0.0
            change = float(fields[31]) if len(fields) > 31 and fields[31] else 0.0
            change_pct = float(fields[32]) if len(fields) > 32 and fields[32] else 0.0
            volume = float(fields[36]) if len(fields) > 36 and fields[36] else 0.0
            amount = float(fields[37]) if len(fields) > 37 and fields[37] else 0.0
            provider_time = _provider_time_iso(fields[30] if len(fields) > 30 else None)
            rows.append({
                "指数名称": name,
                "最新点位": price,
                "涨跌额": change,
                "涨跌幅": change_pct,
                "成交量(手)": volume,
                "成交额(元)": amount,
                "更新时间": provider_time,
            })
        if not rows:
            raise RuntimeError("tencent index batch returned empty")
        result = pd.DataFrame(rows)
        result["更新时间"] = result["更新时间"].astype(object)
        result.loc[result["更新时间"].isna(), "更新时间"] = None
        return result
    except Exception as e:
        logger.warning("fetch_market_overview_tencent failed: %s", e)
        raise


def fetch_market_breadth(**kwargs) -> pd.DataFrame:
    """全市场实时涨跌家数/涨跌停（东财 push2ex 涨跌分布，直连实时）。

    Returns:
        DataFrame columns: 上涨 / 下跌 / 平盘 / 涨停 / 跌停
    """
    from tradex.data_sources.em_client import em_get

    url = "https://push2ex.eastmoney.com/getTopicZDFenBu"
    params = {"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt"}
    resp = em_get(url, params=params, timeout=15)
    resp.raise_for_status()
    fenbu = resp.json()["data"]["fenbu"]
    up = down = flat = limit_up = limit_down = 0
    for item in fenbu:
        for k, v in item.items():
            k = int(k)
            v = int(v)
            if k > 0:
                up += v
            elif k < 0:
                down += v
            else:
                flat += v
            if k >= 10:
                limit_up += v
            if k <= -10:
                limit_down += v
    return pd.DataFrame([{"上涨": up, "下跌": down, "平盘": flat, "涨停": limit_up, "跌停": limit_down}])


def fetch_industry_quotes(
    board_type: str = "industry",
    exact=None,
    **kwargs,
) -> pd.DataFrame:
    """行业或概念板块实时行情（东财 push2，失败降级至 delay 镜像）。

    Args:
        board_type: ``industry`` 对应东财行业板块，``concept`` 对应概念板块。
        exact: 可选的单个板块名称或名称 iterable，仅做完全相等过滤。

    Returns:
        DataFrame columns: 板块代码 / 最新点位 / 板块名称 / 涨跌幅 /
        成交额 / 主力净流入 / 主力净流入-占比 / 主力净流入排名 /
        上涨家数 / 下跌家数 / 领涨股代码 / 领涨股市场 / 领涨股票 /
        领涨股涨幅 / 更新时间 / source.
    """
    from tradex.data_sources.em_client import em_get

    board_type = str(board_type).strip().lower()
    type_code = {"industry": "2", "concept": "3"}.get(board_type)
    if type_code is None:
        raise ValueError("board_type must be 'industry' or 'concept'")

    if exact is None:
        exact_names = None
    elif isinstance(exact, str):
        exact_names = {exact}
    else:
        try:
            exact_names = {str(name) for name in exact}
        except TypeError as exc:
            raise ValueError("exact must be a string or iterable of names") from exc

    page_size = 100
    params = {
        "pz": str(page_size), "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2",
        "fid": "f3", "fs": f"m:90 t:{type_code} f:!50",
        "fields": (
            "f12,f14,f2,f3,f6,f62,f184,f204,f104,f105,"
            "f128,f140,f141,f136,f124"
        ),
    }
    columns = [
        "板块代码", "最新点位", "板块名称", "涨跌幅", "成交额",
        "主力净流入", "主力净流入-占比", "主力净流入排名", "上涨家数",
        "下跌家数", "领涨股代码", "领涨股市场", "领涨股票",
        "领涨股涨幅", "更新时间", "source",
    ]
    optional_columns = [
        "成交额", "主力净流入", "主力净流入-占比", "主力净流入排名",
        "领涨股代码", "领涨股市场", "领涨股票", "领涨股涨幅", "更新时间",
    ]
    # 主源 push2（实时、常封），备源 push2delay（稳定、延迟几分钟）
    for host in ("https://push2.eastmoney.com", "https://push2delay.eastmoney.com"):
        try:
            source = "push2delay" if "push2delay" in host else "push2"
            diff = []
            for page in range(1, 51):
                page_params = {**params, "pn": str(page)}
                resp = em_get(f"{host}/api/qt/clist/get", params=page_params, timeout=15)
                resp.raise_for_status()
                data = resp.json().get("data") or {}
                page_items = data.get("diff") or []
                diff.extend(page_items)
                total = int(data.get("total") or 0)
                if not page_items or len(page_items) < page_size or (total and len(diff) >= total):
                    break
            rows = []
            for item in diff:
                pct = item.get("f3")
                if pct is None or pct == "-":
                    continue
                name = item.get("f14", "")
                if exact_names is not None and name not in exact_names:
                    continue
                rows.append({
                    "板块代码": item.get("f12", ""),
                    "最新点位": float(item.get("f2", 0) or 0),
                    "板块名称": name,
                    "涨跌幅": float(pct),
                    "成交额": _optional_float(item.get("f6")),
                    "主力净流入": _optional_float(item.get("f62")),
                    "主力净流入-占比": _optional_float(item.get("f184")),
                    "主力净流入排名": _optional_int(item.get("f204")),
                    "上涨家数": int(item.get("f104", 0) or 0),
                    "下跌家数": int(item.get("f105", 0) or 0),
                    "领涨股代码": _optional_text(item.get("f140")),
                    "领涨股市场": _optional_int(item.get("f141")),
                    "领涨股票": _optional_text(item.get("f128")),
                    "领涨股涨幅": _optional_float(item.get("f136")),
                    "更新时间": _provider_time_iso(item.get("f124")),
                    "source": source,
                })
            rows.sort(key=lambda x: x["涨跌幅"], reverse=True)
            result = pd.DataFrame(rows, columns=columns)
            for column in optional_columns:
                result[column] = result[column].astype(object)
                result.loc[result[column].isna(), column] = None
            return result
        except SourceBusyError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("fetch_industry_quotes %s failed: %s", host, e)
    raise RuntimeError("fetch_industry_quotes all sources failed")


def fetch_board_leaders(
    board_code: str,
    limit: int = 3,
    source_hint: str | None = None,
    speed_order: str = "desc",
    **kwargs,
) -> pd.DataFrame:
    """Fetch a small, direction-aware speed-sorted constituent page.

    The provider's ``f22`` field is its current price-speed percentage-point
    metric. This remains a bounded display enrichment: it does not crawl the
    complete constituent universe and missing provider fields remain ``None``.
    """
    from tradex.data_sources.em_client import em_get

    code = str(board_code or "").strip().upper()
    if not (code.startswith("BK") and code[2:].isdigit()):
        raise ValueError("board_code must be an Eastmoney BK code")
    try:
        page_size = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer between 1 and 10") from exc
    if not 1 <= page_size <= 10:
        raise ValueError("limit must be an integer between 1 and 10")
    order = str(speed_order or "").strip().lower()
    if order not in {"desc", "asc"}:
        raise ValueError("speed_order must be desc or asc")

    params = {
        "pn": "1",
        "pz": str(page_size),
        "po": "1" if order == "desc" else "0",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f22",
        "fs": f"b:{code} f:!50",
        "fields": "f12,f14,f2,f3,f22,f6,f8,f62,f184,f124",
    }
    columns = [
        "code",
        "name",
        "price",
        "change_pct",
        "speed_pct",
        "amount",
        "turnover",
        "flow_amount",
        "flow_ratio",
        "provider_as_of",
        "source",
    ]
    source_key = str(source_hint or "").strip().lower()
    if "delay" in source_key:
        hosts = ("https://push2delay.eastmoney.com",)
    elif "push2" in source_key:
        hosts = ("https://push2.eastmoney.com",)
    else:
        hosts = ("https://push2.eastmoney.com", "https://push2delay.eastmoney.com")
    errors: list[str] = []
    for host in hosts:
        source = "push2delay" if "push2delay" in host else "push2"
        try:
            response = em_get(
                f"{host}/api/qt/clist/get",
                params=params,
                timeout=15,
                max_queue_wait=2.0,
            )
            response.raise_for_status()
            data = response.json().get("data") or {}
            diff = data.get("diff") or []
            items = list(diff.values()) if isinstance(diff, dict) else list(diff)
            rows = [
                {
                    "code": _optional_text(item.get("f12")),
                    "name": _optional_text(item.get("f14")),
                    "price": _optional_float(item.get("f2")),
                    "change_pct": _optional_float(item.get("f3")),
                    "speed_pct": _optional_float(item.get("f22")),
                    "amount": _optional_float(item.get("f6")),
                    "turnover": _optional_float(item.get("f8")),
                    "flow_amount": _optional_float(item.get("f62")),
                    "flow_ratio": _optional_float(item.get("f184")),
                    "provider_as_of": _provider_time_iso(item.get("f124")),
                    "source": source,
                }
                for item in items[:page_size]
            ]
            if not rows:
                errors.append(f"{source}: empty constituent page")
                continue
            result = pd.DataFrame(rows, columns=columns)
            for column in columns[:-1]:
                result[column] = result[column].astype(object)
                result.loc[result[column].isna(), column] = None
            result.attrs["source"] = source
            result.attrs["board_code"] = code
            return result
        except SourceBusyError:
            raise
        except Exception as exc:  # noqa: BLE001 - mirror fallback is intentional
            errors.append(f"{source}: {exc}")
            logger.warning("fetch_board_leaders %s %s failed: %s", code, host, exc)
    raise RuntimeError(
        f"fetch_board_leaders {code} all sources failed: {'; '.join(errors)}"
    )


def fetch_stock_sector_profiles(codes, **kwargs) -> pd.DataFrame:
    """Batch-fetch sector profile fields for a bounded A-share code set.

    Eastmoney's ``ulist.np`` endpoint accepts many ``secids`` in one request,
    so a complete limit-up pool can be classified without issuing one request
    per stock.  The response is all-or-nothing: missing, duplicate, or
    structurally incomplete profiles raise instead of becoming a partial data
    set that could bias downstream sector counts.
    """
    from tradex.data_sources.em_client import em_get

    if isinstance(codes, str):
        raw_codes = codes.split(",")
    else:
        try:
            raw_codes = list(codes)
        except TypeError as exc:
            raise ValueError("codes must be a string or iterable of A-share codes") from exc

    normalised_codes: list[str] = []
    seen: set[str] = set()
    secids: list[str] = []
    for value in raw_codes:
        code = str(value).strip()
        if len(code) != 6 or not code.isdigit():
            raise ValueError(f"invalid A-share code: {value!r}")
        if code.startswith("6"):
            market = "1"
        elif code.startswith(("0", "3", "4", "8", "9")):
            market = "0"
        else:
            raise ValueError(f"unsupported A-share code: {value!r}")
        if code in seen:
            continue
        seen.add(code)
        normalised_codes.append(code)
        secids.append(f"{market}.{code}")
    if not normalised_codes:
        raise ValueError("at least one A-share code is required")

    fields = "f12,f14,f100,f102,f103,f124"
    params = {
        "fltt": "2",
        "invt": "2",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fields": fields,
        "secids": ",".join(secids),
    }
    source = "push2delay"
    response = em_get(
        "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
        params=params,
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    response_code = payload.get("rc") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or isinstance(response_code, bool)
        or response_code != 0
    ):
        raise RuntimeError("stock sector profiles returned an invalid root payload")
    data = payload.get("data")
    if not isinstance(data, dict) or "diff" not in data:
        raise RuntimeError("stock sector profiles response is missing data.diff")
    diff = data.get("diff")
    if isinstance(diff, dict):
        items = list(diff.values())
    elif isinstance(diff, list):
        items = list(diff)
    else:
        raise RuntimeError("stock sector profiles data.diff must be a list or object")

    declared_total = data.get("total")
    if (
        isinstance(declared_total, bool)
        or not isinstance(declared_total, int)
        or declared_total != len(normalised_codes)
    ):
        raise RuntimeError(
            "stock sector profiles total does not match the requested code count"
        )

    profiles: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("stock sector profiles contains a non-object row")
        code = _optional_text(item.get("f12"))
        name = _optional_text(item.get("f14"))
        industry = _optional_text(item.get("f100"))
        if not code or not name or not industry:
            raise RuntimeError(
                "stock sector profiles requires non-empty code, name, and industry"
            )
        if code in profiles:
            raise RuntimeError(f"stock sector profiles returned duplicate code: {code}")

        raw_concepts = _optional_text(item.get("f103"))
        concepts: list[str] = []
        concept_seen: set[str] = set()
        if raw_concepts:
            for raw_tag in raw_concepts.replace("，", ",").split(","):
                tag = raw_tag.strip()
                if tag and tag not in concept_seen:
                    concept_seen.add(tag)
                    concepts.append(tag)
        profiles[code] = {
            "代码": code,
            "名称": name,
            "行业": industry,
            "地域": _optional_text(item.get("f102")),
            "概念标签": concepts,
            "provider_as_of": _provider_time_iso(item.get("f124")),
            "source": source,
        }

    returned_codes = set(profiles)
    requested_codes = set(normalised_codes)
    if returned_codes != requested_codes:
        missing = sorted(requested_codes - returned_codes)
        unexpected = sorted(returned_codes - requested_codes)
        raise RuntimeError(
            "stock sector profiles returned an incomplete code set: "
            f"missing={missing}, unexpected={unexpected}"
        )

    columns = [
        "代码",
        "名称",
        "行业",
        "地域",
        "概念标签",
        "provider_as_of",
        "source",
    ]
    result = pd.DataFrame(
        [profiles[code] for code in normalised_codes],
        columns=columns,
    )
    result.attrs["source"] = source
    result.attrs["profile_total"] = len(result)
    result.attrs["source_valid"] = True
    return result
