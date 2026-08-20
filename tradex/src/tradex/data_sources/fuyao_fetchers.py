"""同花顺扶摇 API 的 SmartRouter fetch_fn 适配器。

行情快照和历史 K 线适配现有通用数据类型；估值快照、同花顺指数、连板
天梯与个股异动使用独立 data_type 暴露。扶摇历史接口仅提供日线，本模块
在本地按真实交易日聚合周线/月线，保持 ``historical_kline`` 的既有契约。
"""

from __future__ import annotations

import math
import re
from datetime import date as Date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from astock_signals.smart_router import SourceCapabilityError

from .fuyao_client import FuyaoAPIError, request as _request
from ..utils.symbol import get_exchange, normalize_symbol


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SNAPSHOT_PAGE_SIZE = 10_000
_EARLIEST_DEFAULT_DATE = Date(1990, 1, 1)

_SNAPSHOT_COLUMNS = [
    "代码",
    "同花顺代码",
    "最新价",
    "涨跌额",
    "涨跌幅",
    "今开",
    "最高",
    "最低",
    "昨收",
    "成交量",
    "成交额",
    "更新时间",
]
_KLINE_COLUMNS = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]
_VALUATION_COLUMNS = [
    "同花顺代码",
    "代码",
    "名称",
    "市盈率TTM",
    "市盈率MRQ",
    "市净率MRQ",
    "市销率TTM",
    "市现率TTM",
    "更新时间",
]
_INDEX_CATALOG_COLUMNS = ["同花顺指数代码", "名称", "标签", "更新时间"]
_INDEX_CONSTITUENT_COLUMNS = [
    "指数代码",
    "同花顺代码",
    "代码",
    "名称",
    "更新时间",
]
_ANOMALY_COLUMNS = [
    "代码",
    "同花顺代码",
    "名称",
    "异动标签",
    "异动解读",
    "关键词",
    "更新时间",
]
_DRAGON_TIGER_COLUMNS = [
    "代码",
    "名称",
    "上榜日",
    "解读",
    "涨跌幅",
    "龙虎榜买入额",
    "龙虎榜卖出额",
    "龙虎榜净买额",
    "净买额占总成交比",
    "机构净买额",
    "游资净买额",
    "热度排名",
    "上榜天数",
    "概念列表",
    "数据源",
]

_INDEX_THSCODE_PATTERN = re.compile(r"^\d{6}\.(?:SH|SZ|TI)$", re.IGNORECASE)
_LADDER_BOARD_KEYS = (
    "two_board",
    "three_board",
    "four_board",
    "five_board",
    "six_board",
    "seven_over",
)

_THSCODE_PATTERN = re.compile(
    r"^(?:(SH|SZ|BJ)[._-]?)?(\d{6})(?:[._-]?(SH|SZ|BJ))?$",
    re.IGNORECASE,
)
_FULL_THSCODE_PATTERN = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")

_POOL_PAGE_SIZE = 200
_POOL_PATHS = {
    "zt": "/api/a-share/special-data/limit-up-pool",
    "dt": "/api/a-share/special-data/limit-down-pool",
    "zb": "/api/a-share/special-data/limit-break-pool",
}
_POOL_REQUIRED_FIELDS = {
    "zt": {
        "thscode",
        "ticker",
        "name",
        "is_st",
        "is_new",
        "last_price",
        "price_change_ratio_pct",
        "limit_up_time",
        "limit_up_reason",
        "continue_day_text",
        "continue_day_cnt",
        "seal_money",
        "max_seal_money",
    },
    "dt": {
        "thscode",
        "ticker",
        "name",
        "last_price",
        "price_change_ratio_pct",
        "first_limit_time",
        "last_limit_time",
        "turnover_ratio_pct",
    },
    "zb": {
        "thscode",
        "ticker",
        "name",
        "last_price",
        "price_change_ratio_pct",
        "open_times",
        "turnover_ratio_pct",
        "turnover",
    },
}


def _to_thscode(value: str) -> str:
    raw = str(value or "").strip().upper()
    match = _THSCODE_PATTERN.fullmatch(raw)
    if match:
        prefix, ticker, suffix = match.groups()
        if prefix and suffix and prefix.upper() != suffix.upper():
            raise ValueError(f"股票代码交易所前后缀冲突: {value!r}")
        exchange = (prefix or suffix or get_exchange(ticker)).upper()
        return f"{ticker}.{exchange}"

    ticker = normalize_symbol(raw)
    if len(ticker) == 6 and ticker.isdigit():
        return f"{ticker}.{get_exchange(ticker).upper()}"
    raise ValueError(f"无效的 A 股代码: {value!r}")


def _to_thscodes(values: Any, *, max_tokens: int, field: str) -> list[str]:
    if isinstance(values, (list, tuple, set)):
        raw_tokens = [str(value).strip() for value in values]
    else:
        raw = str(values or "").strip()
        raw_tokens = [token.strip() for token in raw.split(",")]
    if not raw_tokens or any(not token for token in raw_tokens):
        raise ValueError(f"{field} 必须包含至少一个有效股票代码，且不能有空项")
    if len(raw_tokens) > max_tokens:
        raise ValueError(f"{field} 最多接受 {max_tokens} 个代码")

    normalised: list[str] = []
    seen: set[str] = set()
    for token in raw_tokens:
        thscode = _to_thscode(token)
        if thscode not in seen:
            seen.add(thscode)
            normalised.append(thscode)
    return normalised


def _to_index_thscode(value: str) -> str:
    raw = str(value or "").strip().upper()
    if not raw or "," in raw or not _INDEX_THSCODE_PATTERN.fullmatch(raw):
        raise ValueError(
            "index_code 必须是单个完整指数代码，如 886042.TI 或 000300.SH"
        )
    return raw


def _items(
    data: dict[str, Any],
    *,
    context: str,
    allow_empty: bool = False,
) -> list[dict[str, Any]]:
    raw_items = data.get("item")
    if not isinstance(raw_items, list):
        raise RuntimeError(f"同花顺扶摇 {context} 响应缺少有效 item")
    if not raw_items and not allow_empty:
        raise RuntimeError(f"同花顺扶摇 {context} 返回空数据")
    if any(not isinstance(item, dict) for item in raw_items):
        raise RuntimeError(f"同花顺扶摇 {context} item 包含非对象记录")
    return raw_items


def _required_int(value: Any, *, context: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise RuntimeError(f"同花顺扶摇 {context} 必须是 >= {minimum} 的整数")
    return value


def _optional_int(value: Any, *, context: str, minimum: int = 0) -> int | None:
    if value is None:
        return None
    return _required_int(value, context=context, minimum=minimum)


def _required_number(value: Any, *, context: str) -> int | float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise RuntimeError(f"同花顺扶摇 {context} 必须是有限数值")
    return value


def _optional_number(value: Any, *, context: str) -> int | float | None:
    if value is None or bool(pd.isna(value)):
        return None
    return _required_number(value, context=context)


def _concept_names(value: Any, *, context: str) -> list[str]:
    """Normalise both documented strings and real ``{"name": ...}`` items."""

    if not isinstance(value, list):
        raise RuntimeError(f"同花顺扶摇 {context} 必须是数组")
    names: list[str] = []
    for index, concept in enumerate(value):
        if isinstance(concept, str):
            name = _required_text(
                concept, context=f"{context}[{index}]"
            )
        elif isinstance(concept, dict):
            name = _required_text(
                concept.get("name"), context=f"{context}[{index}].name"
            )
        else:
            raise RuntimeError(
                f"同花顺扶摇 {context}[{index}] 必须是字符串或含 name 的对象"
            )
        names.append(name)
    return names


def _required_text(
    value: Any,
    *,
    context: str,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        suffix = "字符串" if allow_empty else "非空字符串"
        raise RuntimeError(f"同花顺扶摇 {context} 必须是{suffix}")
    return value.strip()


def _stock_identity(item: dict[str, Any], *, context: str) -> tuple[str, str, str]:
    thscode = _required_text(item.get("thscode"), context=f"{context}.thscode").upper()
    ticker = _required_text(item.get("ticker"), context=f"{context}.ticker")
    name = _required_text(item.get("name"), context=f"{context}.name")
    if (
        not _FULL_THSCODE_PATTERN.fullmatch(thscode)
        or not re.fullmatch(r"\d{6}", ticker)
        or thscode.split(".", 1)[0] != ticker
    ):
        raise RuntimeError(f"同花顺扶摇 {context} 标的代码无效")
    return thscode, ticker, name


def _timestamp_iso(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        milliseconds = int(value)
        return datetime.fromtimestamp(
            milliseconds / 1000, tz=_SHANGHAI
        ).isoformat(timespec="seconds")
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _snapshot_rows(
    items: list[dict[str, Any]],
    provider_timestamp: Any,
) -> list[dict[str, Any]]:
    provider_as_of = _timestamp_iso(provider_timestamp)
    rows: list[dict[str, Any]] = []
    required = {
        "thscode",
        "ticker",
        "last_price",
        "price_change",
        "price_change_ratio_pct",
        "open_price",
        "high_price",
        "low_price",
        "prev_price",
        "volume",
        "turnover",
    }
    for index, item in enumerate(items):
        missing = required.difference(item)
        if missing:
            raise RuntimeError(
                "同花顺扶摇行情记录缺少字段: " + ",".join(sorted(missing))
            )
        ticker = str(item.get("ticker") or "").strip()
        thscode = str(item.get("thscode") or "").strip().upper()
        if (
            not re.fullmatch(r"\d{6}", ticker)
            or not _THSCODE_PATTERN.fullmatch(thscode)
            or thscode.split(".", 1)[0] != ticker
        ):
            raise RuntimeError(
                f"同花顺扶摇行情 item[{index}] 缺少有效 ticker/thscode"
            )
        rows.append(
            {
                "代码": ticker,
                "同花顺代码": thscode,
                "最新价": item.get("last_price"),
                "涨跌额": item.get("price_change"),
                "涨跌幅": item.get("price_change_ratio_pct"),
                "今开": item.get("open_price"),
                "最高": item.get("high_price"),
                "最低": item.get("low_price"),
                "昨收": item.get("prev_price"),
                "成交量": item.get("volume"),
                "成交额": item.get("turnover"),
                "更新时间": provider_as_of,
            }
        )
    return rows


def fetch_realtime_quote(
    symbol: str = "",
    code: str = "",
    **kwargs: Any,
) -> pd.DataFrame:
    """获取单只或全市场 A 股行情快照，返回现有中文列契约。

    ``symbol`` / ``code`` 均为空时使用服务端最大页长并在需要时继续分页，
    以兼容 ``get_stock_list`` 对 ``realtime_quote(symbol='')`` 的调用方式。
    """
    requested = str(symbol or code or "").strip()
    if requested:
        thscode = _to_thscode(requested)
        data = _request(
            "/api/a-share/prices/snapshot",
            params={"thscodes": thscode},
        )
        response_items = _items(data, context="行情快照")
        rows = _snapshot_rows(response_items, data.get("timestamp"))
        if len(rows) != 1 or rows[0]["同花顺代码"] != thscode:
            raise RuntimeError(
                f"同花顺扶摇行情未精确返回请求标的 {thscode}"
            )
        frame = pd.DataFrame(rows, columns=_SNAPSHOT_COLUMNS)
        frame.attrs.update(
            {
                "provider_as_of": rows[0]["更新时间"],
                "total": 1,
                "source_valid": True,
            }
        )
        return frame

    offset = 0
    reported_total: int | None = None
    all_rows: list[dict[str, Any]] = []
    latest_timestamp: int | None = None
    while True:
        data = _request(
            "/api/a-share/prices/snapshot",
            params={"limit": _SNAPSHOT_PAGE_SIZE, "offset": offset},
        )
        page_items = _items(data, context="全市场行情", allow_empty=True)
        total = data.get("total")
        if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
            raise RuntimeError("同花顺扶摇全市场行情 total 无效")
        if reported_total is None:
            reported_total = total
        elif total != reported_total:
            raise RuntimeError("同花顺扶摇全市场行情分页 total 不一致")
        if len(page_items) > _SNAPSHOT_PAGE_SIZE:
            raise RuntimeError("同花顺扶摇全市场行情单页超过请求 limit")
        if offset == 0 and not page_items:
            raise RuntimeError("同花顺扶摇全市场行情首屏为空")

        timestamp = data.get("timestamp")
        if isinstance(timestamp, int) and not isinstance(timestamp, bool):
            latest_timestamp = max(latest_timestamp or timestamp, timestamp)
        all_rows.extend(_snapshot_rows(page_items, timestamp))

        offset += len(page_items)
        # Official contract ends pagination on a short page. ``total`` is an
        # estimate for page planning and is not a strict row-count invariant.
        if len(page_items) < _SNAPSHOT_PAGE_SIZE:
            break
    tickers = [row["代码"] for row in all_rows]
    if len(set(tickers)) != len(tickers):
        raise RuntimeError("同花顺扶摇全市场行情包含重复股票代码")

    frame = pd.DataFrame(all_rows, columns=_SNAPSHOT_COLUMNS)
    frame.attrs.update(
        {
            "provider_as_of": _timestamp_iso(latest_timestamp),
            "total": len(all_rows),
            "reported_total": reported_total,
            "source_valid": True,
        }
    )
    return frame


def _parse_date(value: Any, *, field: str) -> Date:
    if isinstance(value, datetime):
        return value.astimezone(_SHANGHAI).date() if value.tzinfo else value.date()
    if isinstance(value, Date):
        return value
    raw = str(value or "").strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{field} 日期格式无效: {value!r}")


def _shift_year(value: Date, years: int) -> Date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        # 2 月 29 日移到非闰年时采用 2 月 28 日。
        return value.replace(year=value.year + years, day=28)


def _date_ms(value: Date) -> int:
    return int(datetime.combine(value, datetime.min.time(), tzinfo=_SHANGHAI).timestamp() * 1000)


def _historical_windows(start: Date, end: Date) -> list[tuple[Date, Date]]:
    windows: list[tuple[Date, Date]] = []
    cursor = start
    while cursor <= end:
        window_end = min(end, _shift_year(cursor, 10) - timedelta(days=1))
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def _date_from_ms(value: Any, *, index: int) -> str:
    if value is None or isinstance(value, bool):
        raise RuntimeError(f"同花顺扶摇历史 K 线 item[{index}].date_ms 无效")
    try:
        return datetime.fromtimestamp(
            int(value) / 1000, tz=_SHANGHAI
        ).date().isoformat()
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"同花顺扶摇历史 K 线 item[{index}].date_ms 无效"
        ) from exc


def _daily_frame(items: list[dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    required = {
        "date_ms",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
        "turnover",
    }
    for index, item in enumerate(items):
        missing = required.difference(item)
        if missing:
            raise RuntimeError(
                "同花顺扶摇历史 K 线记录缺少字段: " + ",".join(sorted(missing))
            )
        rows.append(
            {
                "日期": _date_from_ms(item.get("date_ms"), index=index),
                "开盘": item.get("open_price"),
                "收盘": item.get("close_price"),
                "最高": item.get("high_price"),
                "最低": item.get("low_price"),
                "成交量": item.get("volume"),
                "成交额": item.get("turnover"),
            }
        )
    frame = pd.DataFrame(rows, columns=_KLINE_COLUMNS)
    frame = frame.sort_values("日期", kind="stable").reset_index(drop=True)
    if frame["日期"].duplicated().any():
        raise RuntimeError("同花顺扶摇历史 K 线包含重复交易日")
    return frame


def _aggregate_kline(daily: pd.DataFrame, period: str) -> pd.DataFrame:
    if period == "daily":
        return daily

    work = daily.copy()
    work["__date"] = pd.to_datetime(work["日期"], format="%Y-%m-%d")
    frequency = "W-FRI" if period == "weekly" else "M"
    work["__period"] = work["__date"].dt.to_period(frequency)

    grouped = work.groupby("__period", sort=True, observed=True)
    result = grouped.agg(
        日期=("__date", "max"),
        开盘=("开盘", lambda values: values.iloc[0]),
        收盘=("收盘", lambda values: values.iloc[-1]),
        最高=("最高", "max"),
        最低=("最低", "min"),
        成交量=("成交量", lambda values: values.sum(min_count=1)),
        成交额=("成交额", lambda values: values.sum(min_count=1)),
    ).reset_index(drop=True)
    result["日期"] = result["日期"].dt.strftime("%Y-%m-%d")
    return result[_KLINE_COLUMNS]


def fetch_historical_kline(
    symbol: str = "",
    code: str = "",
    period: str = "daily",
    start_date: str = "",
    end_date: str = "",
    adjust: str = "qfq",
    **kwargs: Any,
) -> pd.DataFrame:
    """获取日/周/月 K 线；周/月由扶摇日线按真实交易日聚合。"""
    requested = str(symbol or code or "").strip()
    if not requested:
        raise ValueError("同花顺扶摇历史 K 线需要 symbol 或 code")
    thscode = _to_thscode(requested)

    period_key = str(period or "daily").strip().lower()
    period_map = {
        "1d": "daily",
        "day": "daily",
        "daily": "daily",
        "1w": "weekly",
        "week": "weekly",
        "weekly": "weekly",
        "1m": "monthly",
        "month": "monthly",
        "monthly": "monthly",
    }
    if period_key not in period_map:
        raise ValueError(f"同花顺扶摇不支持 K 线周期: {period!r}")
    output_period = period_map[period_key]

    adjust_key = str(adjust or "").strip().lower()
    adjust_map = {
        "": "none",
        "none": "none",
        "qfq": "forward",
        "forward": "forward",
        "hfq": "backward",
        "backward": "backward",
    }
    if adjust_key not in adjust_map:
        raise ValueError(f"同花顺扶摇不支持复权方式: {adjust!r}")
    api_adjust = adjust_map[adjust_key]

    today = datetime.now(_SHANGHAI).date()
    end = _parse_date(end_date, field="end_date") if end_date else today
    # 工具层最多序列化 500 行；默认取近十年既满足接口窗口，又覆盖其输出。
    default_start = max(_EARLIEST_DEFAULT_DATE, _shift_year(end, -10) + timedelta(days=1))
    start = _parse_date(start_date, field="start_date") if start_date else default_start
    if start > end:
        raise ValueError("start_date 不能晚于 end_date")

    all_items: list[dict[str, Any]] = []
    latest_timestamp: int | None = None
    for window_start, window_end in _historical_windows(start, end):
        data = _request(
            "/api/a-share/prices/historical",
            params={
                "thscode": thscode,
                "interval": "1d",
                "start": _date_ms(window_start),
                "end": _date_ms(window_end),
                "adjust": api_adjust,
                "offset": 0,
            },
        )
        all_items.extend(_items(data, context="历史 K 线", allow_empty=True))
        timestamp = data.get("timestamp")
        if isinstance(timestamp, int) and not isinstance(timestamp, bool):
            latest_timestamp = max(latest_timestamp or timestamp, timestamp)

    if not all_items:
        raise RuntimeError(f"同花顺扶摇历史 K 线返回空数据 ({thscode})")
    daily = _daily_frame(all_items)
    result = _aggregate_kline(daily, output_period)
    result.attrs.update(
        {
            "provider_as_of": _timestamp_iso(latest_timestamp),
            "thscode": thscode,
            "period": output_period,
            "adjust": api_adjust,
            "source_valid": True,
        }
    )
    return result


def fetch_valuation_snapshot(
    symbols: Any = "",
    thscodes: Any = "",
    **kwargs: Any,
) -> pd.DataFrame:
    """批量获取 A 股当前 PE/PB/PS/PCF 估值快照。"""
    requested = _to_thscodes(
        symbols or thscodes,
        max_tokens=100,
        field="symbols",
    )
    data = _request(
        "/api/a-share/valuations/snapshot",
        params={"thscodes": ",".join(requested)},
    )
    response_items = _items(data, context="估值快照", allow_empty=True)
    total = data.get("total")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise RuntimeError("同花顺扶摇估值快照 total 无效")
    if total != len(response_items):
        raise RuntimeError("同花顺扶摇估值快照记录数与 total 不一致")

    requested_set = set(requested)
    provider_as_of = _timestamp_iso(data.get("timestamp"))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    metric_fields = ("pe_ttm", "pe_mrq", "pb_mrq", "ps_ttm", "pcf_ttm")
    for index, item in enumerate(response_items):
        thscode = str(item.get("thscode") or "").strip().upper()
        ticker = str(item.get("ticker") or "").strip()
        if (
            thscode not in requested_set
            or not re.fullmatch(r"\d{6}", ticker)
            or thscode.split(".", 1)[0] != ticker
        ):
            raise RuntimeError(f"同花顺扶摇估值 item[{index}] 标的无效")
        if thscode in seen:
            raise RuntimeError("同花顺扶摇估值快照包含重复标的")
        missing = [field for field in metric_fields if field not in item]
        if missing:
            raise RuntimeError(
                "同花顺扶摇估值快照缺少固定指标字段: " + ",".join(missing)
            )
        seen.add(thscode)
        rows.append(
            {
                "同花顺代码": thscode,
                "代码": ticker,
                "名称": item.get("name"),
                "市盈率TTM": item.get("pe_ttm"),
                "市盈率MRQ": item.get("pe_mrq"),
                "市净率MRQ": item.get("pb_mrq"),
                "市销率TTM": item.get("ps_ttm"),
                "市现率TTM": item.get("pcf_ttm"),
                "更新时间": provider_as_of,
            }
        )
    frame = pd.DataFrame(rows, columns=_VALUATION_COLUMNS)
    frame.attrs.update(
        {
            "provider_as_of": provider_as_of,
            "total": total,
            "requested_total": len(requested),
            "source_valid": True,
        }
    )
    return frame


def fetch_ths_index_catalog(
    tag: str = "cn_concept",
    **kwargs: Any,
) -> pd.DataFrame:
    """按标签获取完整的同花顺指数目录。"""
    normalised_tag = str(tag or "cn_concept").strip().lower()
    allowed_tags = {"cn_concept", "region", "tszs", "industry"}
    if normalised_tag not in allowed_tags:
        raise ValueError(
            "tag 必须是 cn_concept、region、tszs 或 industry"
        )
    data = _request(
        "/api/a-share-index/catalog/ths-index-list",
        params={"tag": normalised_tag},
    )
    response_items = _items(data, context="同花顺指数目录", allow_empty=True)
    provider_as_of = _timestamp_iso(data.get("timestamp"))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(response_items):
        thscode = str(item.get("thscode") or "").strip().upper()
        name = str(item.get("name") or "").strip()
        if not _INDEX_THSCODE_PATTERN.fullmatch(thscode) or not name:
            raise RuntimeError(f"同花顺指数目录 item[{index}] 缺少代码或名称")
        if thscode in seen:
            raise RuntimeError("同花顺指数目录包含重复代码")
        seen.add(thscode)
        rows.append(
            {
                "同花顺指数代码": thscode,
                "名称": name,
                "标签": normalised_tag,
                "更新时间": provider_as_of,
            }
        )
    frame = pd.DataFrame(rows, columns=_INDEX_CATALOG_COLUMNS)
    frame.attrs.update(
        {
            "provider_as_of": provider_as_of,
            "tag": normalised_tag,
            "total": len(rows),
            "source_valid": True,
        }
    )
    return frame


def fetch_ths_index_constituents(
    index_code: str = "",
    thscode: str = "",
    **kwargs: Any,
) -> pd.DataFrame:
    """获取单个同花顺/标准 A 股指数的当前成分股。"""
    requested = _to_index_thscode(index_code or thscode)
    data = _request(
        "/api/a-share-index/constituents/ths-stock-list",
        params={"thscode": requested},
    )
    response_items = _items(data, context="同花顺指数成分股", allow_empty=True)
    provider_as_of = _timestamp_iso(data.get("timestamp"))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(response_items):
        stock_thscode = str(item.get("thscode") or "").strip().upper()
        ticker = str(item.get("ticker") or "").strip()
        name = str(item.get("name") or "").strip()
        if (
            not _THSCODE_PATTERN.fullmatch(stock_thscode)
            or not re.fullmatch(r"\d{6}", ticker)
            or stock_thscode.split(".", 1)[0] != ticker
            or not name
        ):
            raise RuntimeError(f"同花顺指数成分股 item[{index}] 字段无效")
        if stock_thscode in seen:
            raise RuntimeError("同花顺指数成分股包含重复股票")
        seen.add(stock_thscode)
        rows.append(
            {
                "指数代码": requested,
                "同花顺代码": stock_thscode,
                "代码": ticker,
                "名称": name,
                "更新时间": provider_as_of,
            }
        )
    frame = pd.DataFrame(rows, columns=_INDEX_CONSTITUENT_COLUMNS)
    frame.attrs.update(
        {
            "provider_as_of": provider_as_of,
            "index_code": requested,
            "total": len(rows),
            "source_valid": True,
        }
    )
    return frame


def fetch_limit_up_ladder(**kwargs: Any) -> dict[str, Any]:
    """获取近 30 个交易日的六档连板天梯矩阵。"""
    data = _request("/api/a-share/special-data/limit-up-ladder")
    window = data.get("window")
    response_items = data.get("item")
    if not isinstance(window, dict) or not isinstance(response_items, list):
        raise RuntimeError("同花顺扶摇连板天梯响应结构无效")
    board_caps = window.get("board_caps")
    if (
        not isinstance(window.get("length"), int)
        or isinstance(window.get("length"), bool)
        or not isinstance(window.get("date_list"), list)
        or not isinstance(board_caps, dict)
        or any(key not in board_caps for key in _LADDER_BOARD_KEYS)
    ):
        raise RuntimeError("同花顺扶摇连板天梯 window 无效")

    for row_index, row in enumerate(response_items):
        if not isinstance(row, dict) or not str(row.get("date") or "").strip():
            raise RuntimeError(f"同花顺扶摇连板天梯 item[{row_index}] 无效")
        boards = row.get("boards")
        if not isinstance(boards, dict):
            raise RuntimeError(f"同花顺扶摇连板天梯 item[{row_index}].boards 无效")
        for board_key in _LADDER_BOARD_KEYS:
            board_items = boards.get(board_key)
            if not isinstance(board_items, list):
                raise RuntimeError(
                    f"同花顺扶摇连板天梯缺少固定板位 {board_key}"
                )
            for stock in board_items:
                if not isinstance(stock, dict) or not all(
                    key in stock
                    for key in (
                        "thscode",
                        "ticker",
                        "name",
                        "board_num",
                        "seal_nextday",
                        "sign_level",
                    )
                ):
                    raise RuntimeError(
                        f"同花顺扶摇连板天梯 {board_key} 股票记录无效"
                    )
    return data


def _validate_pool_item(
    board_type: str,
    item: dict[str, Any],
    *,
    index: int,
) -> dict[str, Any]:
    context = f"{board_type} 池 item[{index}]"
    missing = _POOL_REQUIRED_FIELDS[board_type].difference(item)
    if missing:
        raise RuntimeError(
            f"同花顺扶摇 {context} 缺少字段: " + ",".join(sorted(missing))
        )
    thscode, ticker, name = _stock_identity(item, context=context)
    price = _required_number(item["last_price"], context=f"{context}.last_price")
    change_pct = _required_number(
        item["price_change_ratio_pct"],
        context=f"{context}.price_change_ratio_pct",
    )
    row: dict[str, Any] = {
        "code": ticker,
        "name": name,
        "price": price,
        "change_pct": change_pct,
        "volume": None,
        "amount": None,
        "turnover": None,
        "high_pct": None,
        "low_pct": None,
        "open_pct": None,
        "amplitude": None,
        "industry": None,
        "thscode": thscode,
    }

    if board_type == "zt":
        for field in ("is_st", "is_new"):
            if not isinstance(item[field], bool):
                raise RuntimeError(f"同花顺扶摇 {context}.{field} 必须是布尔值")
        continue_day_cnt = _required_int(
            item["continue_day_cnt"],
            context=f"{context}.continue_day_cnt",
            minimum=1,
        )
        row.update(
            {
                "is_st": item["is_st"],
                "is_new": item["is_new"],
                "limit_up_time": _required_text(
                    item["limit_up_time"],
                    context=f"{context}.limit_up_time",
                    allow_empty=True,
                ),
                "limit_up_reason": _required_text(
                    item["limit_up_reason"],
                    context=f"{context}.limit_up_reason",
                    allow_empty=True,
                ),
                "continue_day_text": _required_text(
                    item["continue_day_text"],
                    context=f"{context}.continue_day_text",
                    allow_empty=True,
                ),
                "continue_day_cnt": continue_day_cnt,
                "seal_money": _required_number(
                    item["seal_money"], context=f"{context}.seal_money"
                ),
                "max_seal_money": _required_number(
                    item["max_seal_money"], context=f"{context}.max_seal_money"
                ),
            }
        )
    elif board_type == "dt":
        turnover_ratio = _required_number(
            item["turnover_ratio_pct"],
            context=f"{context}.turnover_ratio_pct",
        )
        row.update(
            {
                "turnover": turnover_ratio,
                "first_limit_time": _required_text(
                    item["first_limit_time"],
                    context=f"{context}.first_limit_time",
                    allow_empty=True,
                ),
                "last_limit_time": _required_text(
                    item["last_limit_time"],
                    context=f"{context}.last_limit_time",
                    allow_empty=True,
                ),
            }
        )
    else:
        turnover_ratio = _required_number(
            item["turnover_ratio_pct"],
            context=f"{context}.turnover_ratio_pct",
        )
        amount = _required_number(
            item["turnover"], context=f"{context}.turnover"
        )
        row.update(
            {
                "turnover": turnover_ratio,
                "amount": amount,
                "open_times": _required_int(
                    item["open_times"],
                    context=f"{context}.open_times",
                    minimum=0,
                ),
            }
        )
    return row


def _fetch_pool_records(
    board_type: str,
    *,
    date_ms: int | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    path = _POOL_PATHS[board_type]
    base_params: dict[str, Any] = {"size": _POOL_PAGE_SIZE}
    if date_ms is not None:
        base_params["date_ms"] = _required_int(
            date_ms, context="股票池 date_ms", minimum=0
        )

    expected_total: int | None = None
    expected_pages: int | None = None
    expected_size: int | None = None
    provider_timestamp: int | None = None
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 1

    while True:
        params = {**base_params, "page": page}
        data = _request(path, params=params)
        timestamp = _required_int(
            data.get("timestamp"), context=f"{board_type} 池 timestamp", minimum=0
        )
        pagination = data.get("pagination")
        if not isinstance(pagination, dict):
            raise RuntimeError(f"同花顺扶摇 {board_type} 池缺少有效 pagination")
        total = _required_int(
            pagination.get("total"), context=f"{board_type} 池 total", minimum=0
        )
        pages = _required_int(
            pagination.get("pages"), context=f"{board_type} 池 pages", minimum=0
        )
        size = _required_int(
            pagination.get("size"), context=f"{board_type} 池 size", minimum=1
        )
        response_page = _required_int(
            pagination.get("page"), context=f"{board_type} 池 page", minimum=0
        )
        page_items = _items(data, context=f"{board_type} 池", allow_empty=True)

        if size != _POOL_PAGE_SIZE or len(page_items) > size:
            raise RuntimeError(f"同花顺扶摇 {board_type} 池分页 size 不一致")
        if total == 0:
            if pages not in (0, 1) or response_page not in (0, 1) or page_items:
                raise RuntimeError(f"同花顺扶摇 {board_type} 池空集分页无效")
        else:
            calculated_pages = (total + size - 1) // size
            if pages != calculated_pages or response_page != page or not page_items:
                raise RuntimeError(f"同花顺扶摇 {board_type} 池分页元数据无效")

        if expected_total is None:
            expected_total = total
            expected_pages = pages
            expected_size = size
            provider_timestamp = timestamp
        elif (
            total != expected_total
            or pages != expected_pages
            or size != expected_size
            or timestamp != provider_timestamp
        ):
            raise RuntimeError(f"同花顺扶摇 {board_type} 池跨页元数据不一致")

        for item in page_items:
            row = _validate_pool_item(board_type, item, index=len(records))
            if row["thscode"] in seen:
                raise RuntimeError(f"同花顺扶摇 {board_type} 池包含重复股票")
            seen.add(row["thscode"])
            records.append(row)

        if total == 0 or page >= pages:
            break
        page += 1

    if expected_total is None or len(records) != expected_total:
        raise RuntimeError(
            f"同花顺扶摇 {board_type} 池记录数与 total 不一致"
        )
    return records, _timestamp_iso(provider_timestamp)


def fetch_limit_up_board(
    board_type: str = "zt",
    date_ms: int | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """适配扶摇涨停/炸板/跌停池到 Tradex 既有外层契约。"""
    normalised = str(board_type or "zt").strip().lower()
    if normalised in {"sentiment", "prev_zt"}:
        raise SourceCapabilityError(
            f"ths_fuyao does not support limit_up_board type {normalised!r}"
        )
    if normalised not in _POOL_PATHS:
        raise ValueError("board_type 必须是 zt、zb、dt、prev_zt 或 sentiment")
    records, _provider_as_of = _fetch_pool_records(
        normalised, date_ms=date_ms
    )
    return {
        "type": normalised,
        "data": records,
        "count": len(records),
        "source": "ths_fuyao",
    }


def fetch_market_breadth(**kwargs: Any) -> pd.DataFrame:
    """用全市场快照与官方涨跌停池生成严格的市场宽度口径。"""
    snapshot = fetch_realtime_quote()
    changes: list[int | float] = []
    unclassified_count = 0
    for index, value in enumerate(snapshot["涨跌幅"].tolist()):
        change = _optional_number(
            value,
            context=f"全市场行情 item[{index}].涨跌幅",
        )
        if change is None:
            unclassified_count += 1
        else:
            changes.append(change)
    up_records, up_as_of = _fetch_pool_records("zt")
    down_records, down_as_of = _fetch_pool_records("dt")

    component_dates = {
        value[:10]
        for value in (
            snapshot.attrs.get("provider_as_of"),
            up_as_of,
            down_as_of,
        )
        if isinstance(value, str) and len(value) >= 10
    }
    if len(component_dates) > 1:
        raise RuntimeError("同花顺扶摇市场宽度组件交易日不一致")

    result = pd.DataFrame(
        [
            {
                "上涨": sum(value > 0 for value in changes),
                "下跌": sum(value < 0 for value in changes),
                "平盘": sum(value == 0 for value in changes),
                "未分类": unclassified_count,
                "涨停": len(up_records),
                "跌停": len(down_records),
            }
        ]
    )
    result.attrs.update(
        {
            "provider_as_of": snapshot.attrs.get("provider_as_of"),
            "source": "ths_fuyao",
            "source_valid": True,
        }
    )
    return result


def _validate_hot_money_items(items: Any) -> None:
    if not isinstance(items, list):
        raise RuntimeError("同花顺扶摇龙虎榜 hot_money_items 不是数组")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise RuntimeError(
                f"同花顺扶摇龙虎榜 hot_money_items[{index}] 不是对象"
            )
        missing = {"name", "buying", "rows"}.difference(item)
        if missing:
            raise RuntimeError(
                "同花顺扶摇龙虎榜游资记录缺少字段: "
                + ",".join(sorted(missing))
            )
        _required_text(item["name"], context=f"龙虎榜游资 item[{index}].name")
        _required_number(
            item["buying"], context=f"龙虎榜游资 item[{index}].buying"
        )
        if not isinstance(item["rows"], list) or any(
            not isinstance(row, dict) for row in item["rows"]
        ):
            raise RuntimeError(
                f"同花顺扶摇龙虎榜游资 item[{index}].rows 无效"
            )


def fetch_dragon_tiger(
    code: str = "",
    symbol: str = "",
    trade_date: str = "",
    board_type: str = "all",
    look_back_days: int = 1,
    **kwargs: Any,
) -> pd.DataFrame:
    """获取单交易日市场龙虎榜；不冒充个股多日席位明细。"""
    requested = str(code or symbol or "").strip()
    if requested:
        raise SourceCapabilityError(
            "ths_fuyao dragon-tiger-list does not provide per-stock TOP5 seats"
        )
    if (
        not isinstance(look_back_days, int)
        or isinstance(look_back_days, bool)
        or look_back_days < 1
    ):
        raise ValueError("look_back_days 必须是 >= 1 的整数")
    if look_back_days > 1:
        raise SourceCapabilityError(
            "ths_fuyao dragon-tiger-list only provides one trading day"
        )
    normalised_board_type = str(board_type or "all").strip().lower()
    if normalised_board_type not in {"all", "org", "hot_money"}:
        raise ValueError("board_type 必须是 all、org 或 hot_money")
    if normalised_board_type != "all":
        raise SourceCapabilityError(
            "ths_fuyao adapter currently exposes only board_type='all'"
        )

    params = {"board_type": normalised_board_type}
    if trade_date:
        try:
            parsed_trade_date = datetime.strptime(
                str(trade_date).strip(), "%Y-%m-%d"
            ).strftime("%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("trade_date 必须是 YYYY-MM-DD") from exc
        params["date"] = parsed_trade_date

    data = _request(
        "/api/a-share/special-data/dragon-tiger-list",
        params=params,
    )
    provider_as_of = _timestamp_iso(
        _required_int(data.get("timestamp"), context="龙虎榜 timestamp", minimum=0)
    )
    returned_board_type = _required_text(
        data.get("board_type"), context="龙虎榜 board_type"
    ).lower()
    returned_trade_date = _required_text(
        data.get("trade_date"), context="龙虎榜 trade_date"
    )
    try:
        datetime.strptime(returned_trade_date, "%Y-%m-%d")
    except ValueError as exc:
        raise RuntimeError("同花顺扶摇龙虎榜 trade_date 格式无效") from exc
    if returned_board_type != normalised_board_type:
        raise RuntimeError("同花顺扶摇龙虎榜 board_type 与请求不一致")
    if trade_date and returned_trade_date != params["date"]:
        raise RuntimeError("同花顺扶摇龙虎榜 trade_date 与请求不一致")

    record_count = _required_int(
        data.get("count"), context="龙虎榜 count", minimum=0
    )
    stock_count = _required_int(
        data.get("stock_count"), context="龙虎榜 stock_count", minimum=0
    )
    stock_items = data.get("stock_items")
    if not isinstance(stock_items, list) or any(
        not isinstance(item, dict) for item in stock_items
    ):
        raise RuntimeError("同花顺扶摇龙虎榜 stock_items 无效")
    if len(stock_items) != record_count:
        raise RuntimeError("同花顺扶摇龙虎榜 stock_items 与 count 不一致")
    _validate_hot_money_items(data.get("hot_money_items"))

    required = {
        "thscode",
        "ticker",
        "name",
        "concept_list",
        "change",
        "buy_value",
        "sell_value",
        "net_value",
        "net_rate",
        "range_days",
    }
    rows: list[dict[str, Any]] = []
    unique_stocks: set[str] = set()
    for index, item in enumerate(stock_items):
        missing = required.difference(item)
        if missing:
            raise RuntimeError(
                "同花顺扶摇龙虎榜个股记录缺少字段: "
                + ",".join(sorted(missing))
            )
        thscode, ticker, name = _stock_identity(
            item, context=f"龙虎榜 stock_items[{index}]"
        )
        unique_stocks.add(thscode)
        concepts = _concept_names(
            item["concept_list"],
            context=f"龙虎榜 stock_items[{index}].concept_list",
        )
        numeric_fields = (
            "change",
            "buy_value",
            "sell_value",
            "net_value",
            "net_rate",
        )
        values = {
            field: _required_number(
                item[field], context=f"龙虎榜 stock_items[{index}].{field}"
            )
            for field in numeric_fields
        }
        rows.append(
            {
                "代码": ticker,
                "名称": name,
                "上榜日": returned_trade_date,
                "解读": (
                    _required_text(
                        item.get("limit_reason"),
                        context=f"龙虎榜 stock_items[{index}].limit_reason",
                        allow_empty=True,
                    )
                    if item.get("limit_reason") is not None
                    else ""
                ),
                # The official endpoint returns decimal ratios (0.1 == 10%).
                "涨跌幅": values["change"] * 100,
                "龙虎榜买入额": values["buy_value"],
                "龙虎榜卖出额": values["sell_value"],
                "龙虎榜净买额": values["net_value"],
                "净买额占总成交比": values["net_rate"] * 100,
                "机构净买额": _optional_number(
                    item.get("org_net_value"),
                    context=f"龙虎榜 stock_items[{index}].org_net_value",
                ),
                "游资净买额": _optional_number(
                    item.get("hot_money_net_value"),
                    context=f"龙虎榜 stock_items[{index}].hot_money_net_value",
                ),
                "热度排名": _optional_int(
                    item.get("hot_rank"),
                    context=f"龙虎榜 stock_items[{index}].hot_rank",
                    minimum=0,
                ),
                "上榜天数": _required_int(
                    item["range_days"],
                    context=f"龙虎榜 stock_items[{index}].range_days",
                    minimum=0,
                ),
                "概念列表": concepts,
                "数据源": "ths_fuyao",
            }
        )
    if len(unique_stocks) != stock_count:
        raise RuntimeError("同花顺扶摇龙虎榜唯一股票数与 stock_count 不一致")

    frame = pd.DataFrame(rows, columns=_DRAGON_TIGER_COLUMNS)
    frame.attrs.update(
        {
            "provider_as_of": provider_as_of,
            "trade_date": returned_trade_date,
            "board_type": returned_board_type,
            "total": record_count,
            "stock_count": stock_count,
            "source": "ths_fuyao",
            "source_valid": True,
            "valid_empty": record_count == 0,
        }
    )
    return frame


def fetch_stock_anomaly_analysis(
    symbols: Any = "",
    thscodes: Any = "",
    **kwargs: Any,
) -> pd.DataFrame:
    """批量查询当日个股异动原因；无异动是合法空集。"""
    requested = _to_thscodes(
        symbols or thscodes,
        max_tokens=50,
        field="symbols",
    )
    data = _request(
        "/api/a-share/special-data/anomaly-analysis-stock",
        params={"thscodes": ",".join(requested)},
    )
    response_items = _items(data, context="个股异动原因", allow_empty=True)
    provider_as_of = _timestamp_iso(data.get("timestamp"))
    requested_set = set(requested)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    required = {
        "stock_name",
        "analysis_content",
        "keyword_list",
        "thscode",
        "tag_name",
    }
    for index, item in enumerate(response_items):
        missing = required.difference(item)
        if missing:
            raise RuntimeError(
                "同花顺扶摇个股异动记录缺少字段: "
                + ",".join(sorted(missing))
            )
        thscode = str(item.get("thscode") or "").strip().upper()
        keywords = item.get("keyword_list")
        if (
            thscode not in requested_set
            or not _THSCODE_PATTERN.fullmatch(thscode)
            or not isinstance(keywords, list)
            or thscode in seen
        ):
            raise RuntimeError(f"同花顺扶摇个股异动 item[{index}] 字段无效")
        if any(not isinstance(keyword, str) for keyword in keywords):
            raise RuntimeError(f"同花顺扶摇个股异动 item[{index}] 关键词无效")
        seen.add(thscode)
        rows.append(
            {
                "代码": thscode.split(".", 1)[0],
                "同花顺代码": thscode,
                "名称": item.get("stock_name"),
                "异动标签": item.get("tag_name"),
                "异动解读": item.get("analysis_content"),
                "关键词": keywords,
                "更新时间": provider_as_of,
            }
        )
    frame = pd.DataFrame(rows, columns=_ANOMALY_COLUMNS)
    frame.attrs.update(
        {
            "provider_as_of": provider_as_of,
            "total": len(rows),
            "requested_total": len(requested),
            "valid_empty": not rows,
            "source_valid": True,
        }
    )
    return frame


__all__ = [
    "FuyaoAPIError",
    "fetch_dragon_tiger",
    "fetch_historical_kline",
    "fetch_limit_up_board",
    "fetch_limit_up_ladder",
    "fetch_market_breadth",
    "fetch_realtime_quote",
    "fetch_stock_anomaly_analysis",
    "fetch_ths_index_catalog",
    "fetch_ths_index_constituents",
    "fetch_valuation_snapshot",
]
