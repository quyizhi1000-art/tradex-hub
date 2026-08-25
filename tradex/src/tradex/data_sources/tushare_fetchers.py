"""Map TuShare Pro tables to Tradex's existing provider-frame contracts."""

from __future__ import annotations

import math
import re
from bisect import bisect_right
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from astock_signals.smart_router import SourceCapabilityError

from ..utils.symbol import get_exchange, normalize_symbol
from .tushare_client import TushareResult, request as _request


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_PERIODS = {
    "d": ("daily", "daily"),
    "day": ("daily", "daily"),
    "daily": ("daily", "daily"),
    "1d": ("daily", "daily"),
    "w": ("weekly", "weekly"),
    "week": ("weekly", "weekly"),
    "weekly": ("weekly", "weekly"),
    "1w": ("weekly", "weekly"),
    "m": ("monthly", "monthly"),
    "month": ("monthly", "monthly"),
    "monthly": ("monthly", "monthly"),
    "1m": ("monthly", "monthly"),
}
_ADJUSTMENTS = {
    "": ("none", None),
    "n": ("none", None),
    "none": ("none", None),
    "qfq": ("forward", "qfq"),
    "f": ("forward", "qfq"),
    "forward": ("forward", "qfq"),
    "hfq": ("backward", "hfq"),
    "b": ("backward", "hfq"),
    "backward": ("backward", "hfq"),
}
_RT_FIELDS = (
    "ts_code",
    "name",
    "pre_close",
    "high",
    "open",
    "low",
    "close",
    "vol",
    "amount",
    "trade_time",
)
_SW_RT_FIELDS = (
    "ts_code",
    "name",
    "trade_time",
    "close",
    "pre_close",
    "high",
    "open",
    "low",
    "vol",
    "amount",
    "pct_change",
)
_BAR_FIELDS = (
    "ts_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "change",
    "pct_chg",
    "vol",
    "amount",
)
_FACTOR_FIELDS = ("ts_code", "trade_date", "adj_factor")
_AUCTION_FIELDS = (
    "ts_code",
    "trade_date",
    "vol",
    "price",
    "amount",
    "pre_close",
    "turnover_rate",
    "volume_ratio",
    "float_share",
)
_MINUTE_FIELDS = (
    "ts_code",
    "time",
    "open",
    "close",
    "high",
    "low",
    "vol",
    "amount",
)
_UNIVERSE_DAILY_FIELDS = _BAR_FIELDS
_DAILY_BASIC_FIELDS = (
    "ts_code",
    "trade_date",
    "turnover_rate",
    "volume_ratio",
    "pe",
    "pb",
    "ps",
    "dv_ratio",
    "total_share",
    "float_share",
    "free_share",
    "total_mv",
    "circ_mv",
)
_STOCK_BASIC_FIELDS = (
    "ts_code",
    "symbol",
    "name",
    "area",
    "industry",
    "market",
    "list_date",
    "list_status",
)
_FUND_DAILY_FIELDS = _BAR_FIELDS
_FUND_BASIC_FIELDS = (
    "ts_code",
    "name",
    "fund_type",
    "list_date",
    "delist_date",
    "status",
    "market",
)
_INDUSTRY_FLOW_FIELDS = (
    "trade_date",
    "ts_code",
    "industry",
    "lead_stock",
    "close",
    "pct_change",
    "company_num",
    "pct_change_stock",
    "close_price",
    "net_buy_amount",
    "net_sell_amount",
    "net_amount",
)
_CONCEPT_FLOW_FIELDS = (
    "trade_date",
    "ts_code",
    "name",
    "lead_stock",
    "close_price",
    "pct_change",
    "industry_index",
    "company_num",
    "pct_change_stock",
    "net_buy_amount",
    "net_sell_amount",
    "net_amount",
)
_STOCK_FLOW_FIELDS = (
    "ts_code",
    "trade_date",
    "buy_sm_vol",
    "buy_sm_amount",
    "sell_sm_vol",
    "sell_sm_amount",
    "buy_md_vol",
    "buy_md_amount",
    "sell_md_vol",
    "sell_md_amount",
    "buy_lg_vol",
    "buy_lg_amount",
    "sell_lg_vol",
    "sell_lg_amount",
    "buy_elg_vol",
    "buy_elg_amount",
    "sell_elg_vol",
    "sell_elg_amount",
    "net_mf_vol",
    "net_mf_amount",
)
_TOP_LIST_FIELDS = (
    "trade_date",
    "ts_code",
    "name",
    "close",
    "pct_change",
    "turnover_rate",
    "amount",
    "l_sell",
    "l_buy",
    "l_amount",
    "net_amount",
    "net_rate",
    "amount_rate",
    "float_values",
    "reason",
)


def _number(value: Any) -> float | int | None:
    if value in (None, "", "-"):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return int(result) if result.is_integer() else result


def _scaled(value: Any, multiplier: int) -> float | int | None:
    number = _number(value)
    return number * multiplier if number is not None else None


def _turnover_volume_shares(
    item: Mapping[str, Any], *, context: str
) -> float | int | None:
    """Resolve a documented-share field across compatible proxy variants.

    TuShare documents both ``rt_k.vol`` and realtime-minute ``vol`` as shares,
    but a compatible endpoint has returned lot-like ``rt_k`` values.  Accept
    only the unique x1/x100 interpretation whose turnover-implied price lies
    inside the provider's own price range.
    """

    raw_volume = _number(item.get("vol"))
    amount = _number(item.get("amount"))
    if raw_volume is None:
        return None
    if raw_volume < 0:
        raise RuntimeError(f"TuShare {context}成交量无效")
    if raw_volume == 0:
        if amount not in (None, 0):
            raise RuntimeError(f"TuShare {context}零成交量与成交额冲突")
        return 0
    if amount is None or amount <= 0:
        raise RuntimeError(f"TuShare {context}缺少可校验的成交额")

    low = _number(item.get("low"))
    high = _number(item.get("high"))
    if low is None or high is None or low <= 0 or high + 1e-8 < low:
        raise RuntimeError(f"TuShare {context}缺少可校验的价格区间")

    candidates: list[float | int] = []
    for multiplier in (1, 100):
        normalized = raw_volume * multiplier
        implied_average = amount / normalized
        # Compatible realtime proxies can round the cumulative turnover and
        # volume independently, which is visible on lightly traded ETFs.  A
        # two-percent price tolerance still cleanly separates x1 from x100
        # while accepting that documented rounding noise.
        tolerance = max(0.01, float(high) * 0.02, 1.0 / normalized)
        if float(low) - tolerance <= implied_average <= float(high) + tolerance:
            candidates.append(normalized)
    if len(candidates) != 1:
        raise RuntimeError(f"TuShare {context}成交量单位无法唯一判定")
    return candidates[0]


def _realtime_volume_shares(item: Mapping[str, Any]) -> float | int | None:
    return _turnover_volume_shares(item, context="实时日线")


def _records(
    payload: Any,
    context: str,
    *,
    allow_empty: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    request_id: str | None = None
    if isinstance(payload, TushareResult):
        raw_records: Any = payload.records
        request_id = payload.request_id
    else:
        raw_records = payload
    if not isinstance(raw_records, (list, tuple)) or any(
        not isinstance(item, Mapping) for item in raw_records
    ):
        raise RuntimeError(f"TuShare {context}响应结构无效")
    records = [dict(item) for item in raw_records]
    if not records and not allow_empty:
        raise RuntimeError(f"TuShare {context}返回空数据")
    return records, request_id


def _ts_code(value: str) -> str:
    raw = str(value or "").strip().upper()
    suffix = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", raw)
    prefix = re.fullmatch(r"(SH|SZ|BJ)\.?([0-9]{6})", raw)
    if suffix:
        code, exchange = suffix.groups()
    elif prefix:
        exchange, code = prefix.groups()
    else:
        code = normalize_symbol(raw)
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError(f"无效 A 股代码: {value!r}")
        exchange = get_exchange(raw).upper()

    valid_exchange = (
        (exchange == "SH" and code.startswith("6"))
        or (exchange == "SZ" and code.startswith(("0", "1", "2", "3")))
        or (exchange == "BJ" and code.startswith(("4", "8", "920")))
    )
    if not valid_exchange:
        raise ValueError(f"无效 A 股代码: {value!r}")
    return f"{code}.{exchange}"


def _compact_date(value: Any, label: str, *, allow_empty: bool = True) -> str:
    if value in (None, ""):
        if allow_empty:
            return ""
        raise ValueError(f"{label} 不能为空")
    if isinstance(value, datetime):
        result = value.strftime("%Y%m%d")
    elif isinstance(value, date):
        result = value.strftime("%Y%m%d")
    else:
        result = str(value).strip().replace("-", "").replace("/", "")
    if not re.fullmatch(r"\d{8}", result):
        raise ValueError(f"{label} 必须是 YYYYMMDD 日期")
    try:
        datetime.strptime(result, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"{label} 不是有效日期") from exc
    return result


def _bound(primary: Any, alias: Any, label: str) -> str:
    primary_value = _compact_date(primary, label)
    alias_value = _compact_date(alias, label)
    if primary_value and alias_value and primary_value != alias_value:
        raise ValueError(f"{label} 参数冲突")
    return primary_value or alias_value


def _count(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("count 必须是正整数")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("count 必须是正整数") from exc
    if result <= 0:
        raise ValueError("count 必须是正整数")
    return result


def _date_object(value: Any, label: str = "trade_date") -> date:
    compact = _compact_date(value, label, allow_empty=False)
    return datetime.strptime(compact, "%Y%m%d").date()


def _iso_provider_time(value: Any) -> str | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass
    if parsed is None:
        for fmt in (
            "%Y%m%d %H:%M:%S",
            "%Y%m%d%H%M%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    else:
        parsed = parsed.astimezone(_SHANGHAI)
    return parsed.isoformat(timespec="seconds")


def _market_time(trading_date: date, at: time) -> str:
    return datetime.combine(trading_date, at, tzinfo=_SHANGHAI).isoformat(
        timespec="seconds"
    )


def _frame(
    rows: Iterable[dict[str, Any]],
    *,
    provider_as_of: str | None,
    request_id: str | None,
    **attrs: Any,
) -> pd.DataFrame:
    frame = pd.DataFrame(list(rows))
    frame.attrs.update(
        {
            "source_valid": True,
            **attrs,
            "provider_as_of": provider_as_of,
            "request_id": request_id,
        }
    )
    return frame


def _daily_trade_date(value: Any, *, required: bool = False) -> tuple[str, date]:
    compact = _compact_date(value, "trade_date", allow_empty=not required)
    if not compact:
        compact = datetime.now(_SHANGHAI).strftime("%Y%m%d")
    return compact, datetime.strptime(compact, "%Y%m%d").date()


def _request_id_bundle(*items: tuple[str, str | None]) -> str | None:
    parts = [f"{name}:{request_id}" for name, request_id in items if request_id]
    return "|".join(parts) or None


def _exact_day_records(
    records: Iterable[dict[str, Any]],
    *,
    expected: str,
    context: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in records:
        actual = _compact_date(
            item.get("trade_date"), f"{context}.trade_date", allow_empty=False
        )
        if actual != expected:
            raise RuntimeError(
                f"TuShare {context}返回错日数据: expected={expected}, actual={actual}"
            )
        result.append(item)
    return result


def _unique_by_code(
    records: Iterable[dict[str, Any]], *, context: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in records:
        code = _ts_code(str(item.get("ts_code") or ""))
        if code in result:
            raise RuntimeError(f"TuShare {context}返回重复证券 {code}")
        result[code] = item
    return result


def _matching_records(
    records: Iterable[dict[str, Any]], requested: str, context: str
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for item in records:
        try:
            record_code = _ts_code(
                str(item.get("ts_code") or item.get("code") or "")
            )
        except ValueError:
            continue
        if record_code == requested:
            matches.append(item)
    if not matches:
        raise RuntimeError(f"TuShare {context}未返回请求的证券 {requested}")
    return matches


def fetch_realtime_quote(
    symbol: str = "", code: str = "", **kwargs: Any
) -> pd.DataFrame:
    """Fetch one ``rt_k`` snapshot and normalize its volume to shares."""

    requested = _ts_code(symbol or code)
    payload = _request("rt_k", {"ts_code": requested}, fields=_RT_FIELDS)
    records, request_id = _records(payload, "实时日线")
    matches = _matching_records(records, requested, "实时日线")
    if len(matches) != 1:
        raise RuntimeError(f"TuShare 实时日线返回重复证券 {requested}")
    item = matches[0]
    last = _number(item.get("close"))
    previous = _number(item.get("pre_close"))
    change = last - previous if last is not None and previous is not None else None
    change_pct = change / previous * 100 if change is not None and previous else None
    provider_as_of = _iso_provider_time(item.get("trade_time"))
    row = {
        "代码": requested[:6],
        "名称": item.get("name"),
        "最新价": last,
        "涨跌额": change,
        "涨跌幅": change_pct,
        "今开": _number(item.get("open")),
        "最高": _number(item.get("high")),
        "最低": _number(item.get("low")),
        "昨收": previous,
        "成交量": _realtime_volume_shares(item),
        "成交额": _number(item.get("amount")),
        "更新时间": provider_as_of,
    }
    return _frame(
        [row], provider_as_of=provider_as_of, request_id=request_id
    )


def _minute_timestamp(value: Any) -> datetime:
    raw = str(value or "").strip()
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass
    if parsed is None:
        for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d%H%M%S"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise RuntimeError(f"TuShare 实时分钟时间无效: {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    else:
        parsed = parsed.astimezone(_SHANGHAI)
    minute_of_day = parsed.hour * 60 + parsed.minute
    if not (570 <= minute_of_day <= 690 or 780 <= minute_of_day <= 900):
        raise RuntimeError("TuShare 实时分钟包含非 A 股交易时段数据")
    return parsed.replace(microsecond=0)


def fetch_minute_data(
    symbol: str = "", code: str = "", **kwargs: Any
) -> pd.DataFrame:
    """Fetch the complete current-session 1-minute series via ``rt_min_daily``.

    The normalized provider frame uses shares and CNY.  The compatible endpoint
    is still checked bar by bar for the known x1/x100 volume ambiguity before
    the result can reach the canonical gateway.
    """

    requested = _ts_code(symbol or code)
    payload = _request(
        "rt_min_daily",
        {"ts_code": requested, "freq": "1MIN"},
        fields=_MINUTE_FIELDS,
    )
    records, request_id = _records(payload, "实时分钟")
    matches = _matching_records(records, requested, "实时分钟")

    rows: list[dict[str, Any]] = []
    seen_times: set[datetime] = set()
    trading_dates: set[date] = set()
    for item in matches:
        observed_at = _minute_timestamp(item.get("time"))
        if observed_at in seen_times:
            raise RuntimeError("TuShare 实时分钟返回重复时间点")
        seen_times.add(observed_at)
        trading_dates.add(observed_at.date())

        prices = {
            name: _number(item.get(name))
            for name in ("open", "close", "high", "low")
        }
        if any(value is None or value <= 0 for value in prices.values()):
            raise RuntimeError("TuShare 实时分钟 OHLC 无效")
        if prices["high"] + 1e-8 < max(prices.values()):
            raise RuntimeError("TuShare 实时分钟最高价与 OHLC 冲突")
        if prices["low"] - 1e-8 > min(prices.values()):
            raise RuntimeError("TuShare 实时分钟最低价与 OHLC 冲突")

        volume_shares = _turnover_volume_shares(item, context="实时分钟")
        amount_cny = _number(item.get("amount"))
        if amount_cny is None or amount_cny < 0:
            raise RuntimeError("TuShare 实时分钟成交额无效")
        rows.append(
            {
                "代码": requested,
                "时间": observed_at.isoformat(timespec="seconds"),
                "开盘": prices["open"],
                "收盘": prices["close"],
                "最高": prices["high"],
                "最低": prices["low"],
                "成交量": volume_shares,
                "成交额": amount_cny,
            }
        )

    if len(trading_dates) != 1:
        raise RuntimeError("TuShare 实时分钟返回跨交易日数据")
    rows.sort(key=lambda item: item["时间"])
    trading_date = next(iter(trading_dates))
    return _frame(
        rows,
        provider_as_of=rows[-1]["时间"],
        request_id=request_id,
        trading_date=trading_date.isoformat(),
        frequency_minutes=1,
        volume_unit="shares",
        amount_unit="CNY",
    )


def fetch_market_universe(
    symbol: str = "",
    code: str = "",
    trade_date: str = "",
    **kwargs: Any,
) -> pd.DataFrame:
    """Fetch the complete paid ``rt_k`` A-share realtime cross-section."""

    if symbol or code:
        raise SourceCapabilityError("tushare market_universe is a whole-market route")
    payload = _request(
        "rt_k",
        {"ts_code": "3*.SZ,6*.SH,0*.SZ,9*.BJ"},
        fields=_RT_FIELDS,
    )
    records, request_id = _records(payload, "A股实时全市场")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    provider_times: list[str] = []
    for item in records:
        instrument_id = _ts_code(str(item.get("ts_code") or ""))
        if instrument_id in seen:
            raise RuntimeError(f"TuShare A股实时全市场返回重复证券 {instrument_id}")
        seen.add(instrument_id)
        name = str(item.get("name") or "").strip()
        if not name:
            raise RuntimeError(f"TuShare A股实时全市场缺少证券名称 {instrument_id}")
        last = _number(item.get("close"))
        previous = _number(item.get("pre_close"))
        change_pct = (
            (last - previous) / previous * 100
            if last is not None and previous not in (None, 0)
            else None
        )
        provider_as_of = _iso_provider_time(item.get("trade_time"))
        if provider_as_of is not None:
            provider_times.append(provider_as_of)
        rows.append(
            {
                **item,
                "ts_code": instrument_id,
                "name": name,
                "pct_chg": change_pct,
                "vol": _turnover_volume_shares(item, context="A股实时全市场"),
                "amount": _number(item.get("amount")),
                "provider_as_of": provider_as_of,
            }
        )
    return _frame(
        rows,
        provider_as_of=max(provider_times, default=None),
        request_id=request_id,
        unit_contract={
            "vol": "shares",
            "amount": "CNY",
        },
    )


def _fund_code(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not re.fullmatch(r"\d{6}\.(SH|SZ)", raw):
        raise RuntimeError(f"TuShare 场内基金代码无效: {value!r}")
    return raw


def fetch_etf_quotes(
    symbol: str = "",
    code: str = "",
    trade_date: str = "",
    top_n: int = 5000,
    **kwargs: Any,
) -> pd.DataFrame:
    """Fetch all Shanghai and Shenzhen ETFs through paid ``rt_etf_k``."""

    if symbol or code:
        raise SourceCapabilityError("tushare etf_quotes is a whole-market route")
    requests = (
        ("sz", {"ts_code": "1*.SZ"}),
        ("sh", {"ts_code": "5*.SH", "topic": "HQ_FND_TICK"}),
    )
    request_ids: list[tuple[str, str | None]] = []
    records: list[dict[str, Any]] = []
    for label, params in requests:
        payload = _request("rt_etf_k", params, fields=_RT_FIELDS)
        batch, request_id = _records(payload, f"{label.upper()} ETF实时日线")
        records.extend(batch)
        request_ids.append((f"rt_etf_k_{label}", request_id))

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    provider_times: list[str] = []
    for item in records:
        instrument_id = _fund_code(item.get("ts_code"))
        if instrument_id in seen:
            raise RuntimeError(f"TuShare ETF实时日线返回重复证券 {instrument_id}")
        seen.add(instrument_id)
        name = str(item.get("name") or "").strip()
        if not name:
            raise RuntimeError(f"TuShare ETF实时日线缺少证券名称 {instrument_id}")
        last = _number(item.get("close"))
        previous = _number(item.get("pre_close"))
        change_pct = (
            (last - previous) / previous * 100
            if last is not None and previous not in (None, 0)
            else None
        )
        provider_as_of = _iso_provider_time(item.get("trade_time"))
        if provider_as_of is not None:
            provider_times.append(provider_as_of)
        rows.append(
            {
                **item,
                "ts_code": instrument_id,
                "name": name,
                "pct_chg": change_pct,
                "vol": _turnover_volume_shares(item, context="ETF实时日线"),
                "amount": _number(item.get("amount")),
                "provider_as_of": provider_as_of,
            }
        )
    if not rows:
        raise RuntimeError("TuShare ETF实时日线返回空数据")
    return _frame(
        rows,
        provider_as_of=max(provider_times, default=None),
        request_id=_request_id_bundle(*request_ids),
        unit_contract={"vol": "shares", "amount": "CNY"},
    )


def _exact_sector_values(value: Any) -> set[str] | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
    else:
        raise ValueError("exact must be a sector name/code or a sequence")
    result = {str(item).strip() for item in values if str(item).strip()}
    return result or None


def fetch_sector_quotes(
    board_type: str = "industry",
    exact: Any = None,
    trade_date: str = "",
    **kwargs: Any,
) -> pd.DataFrame:
    """Fetch the paid Shenwan realtime industry cross-section."""

    sector_type = str(board_type or "industry").strip().lower()
    if sector_type != "industry":
        raise SourceCapabilityError(
            "tushare rt_sw_k supports Shenwan industry quotes only"
        )
    payload = _request("rt_sw_k", {}, fields=_SW_RT_FIELDS)
    records, request_id = _records(payload, "申万行业实时行情")
    selected = _exact_sector_values(exact)
    if selected is not None:
        records = [
            item
            for item in records
            if str(item.get("name") or "").strip() in selected
            or str(item.get("ts_code") or "").strip() in selected
        ]
        if not records:
            raise RuntimeError("TuShare 申万行业实时行情未返回 exact 请求的板块")
    seen: set[str] = set()
    provider_times: list[str] = []
    for item in records:
        sector_code = str(item.get("ts_code") or "").strip().upper()
        if not re.fullmatch(r"\d{6}\.SI", sector_code) or sector_code in seen:
            raise RuntimeError("TuShare 申万行业实时行情包含无效或重复板块代码")
        seen.add(sector_code)
        item["ts_code"] = sector_code
        item["amount"] = _number(item.get("amount"))
        provider_as_of = _iso_provider_time(item.get("trade_time"))
        item["provider_as_of"] = provider_as_of
        if provider_as_of is not None:
            provider_times.append(provider_as_of)
    return _frame(
        records,
        provider_as_of=max(provider_times, default=None),
        request_id=request_id,
        sector_type=sector_type,
        unit_contract={"vol": "shares", "amount": "CNY"},
    )


def fetch_stock_fund_flow(
    trade_date: str = "",
    code: str = "",
    symbol: str = "",
    **kwargs: Any,
) -> pd.DataFrame:
    """Fetch one raw, exact-day all-stock ``moneyflow`` table."""

    if code or symbol:
        raise SourceCapabilityError("stock_fund_flow_day does not accept a stock code")
    compact, trading_date = _daily_trade_date(trade_date, required=True)
    payload = _request("moneyflow", {"trade_date": compact}, fields=_STOCK_FLOW_FIELDS)
    raw_records, request_id = _records(payload, "个股资金流")
    records = _exact_day_records(
        raw_records, expected=compact, context="个股资金流"
    )
    _unique_by_code(records, context="个股资金流")
    return _frame(
        records,
        provider_as_of=_market_time(trading_date, time(19, 0)),
        request_id=request_id,
        trade_date=trading_date.isoformat(),
        unit_contract={
            "*_vol": "lots",
            "*_amount": "ten_thousand_CNY",
        },
    )


def fetch_dragon_tiger_market_day(
    code: str = "",
    symbol: str = "",
    trade_date: str = "",
    board_type: str = "all",
    look_back_days: int = 1,
    **kwargs: Any,
) -> pd.DataFrame:
    """Fetch the raw exact-day ``top_list`` market table."""

    if code or symbol:
        raise SourceCapabilityError("market-day dragon tiger does not accept a stock code")
    if str(board_type or "all").strip().lower() != "all":
        raise SourceCapabilityError("tushare fallback supports only board_type='all'")
    if look_back_days != 1:
        raise SourceCapabilityError("market-day dragon tiger requires look_back_days=1")
    compact, trading_date = _daily_trade_date(trade_date, required=True)
    payload = _request("top_list", {"trade_date": compact}, fields=_TOP_LIST_FIELDS)
    raw_records, request_id = _records(
        payload, "龙虎榜每日明细", allow_empty=True
    )
    records = _exact_day_records(
        raw_records, expected=compact, context="龙虎榜每日明细"
    )
    return _frame(
        records,
        provider_as_of=_market_time(trading_date, time(20, 0)),
        request_id=request_id,
        trade_date=trading_date.isoformat(),
        board_type="all",
        valid_empty=not records,
        unit_contract={
            "amount": "CNY",
            "l_sell": "CNY",
            "l_buy": "CNY",
            "l_amount": "CNY",
            "net_amount": "CNY",
            "float_values": "CNY",
        },
    )


def _period(value: str) -> tuple[str, str]:
    key = str(value or "daily").strip().lower()
    result = _PERIODS.get(key)
    if result is None:
        if key in {"1", "5", "15", "30", "60", "minute", "year", "yearly"}:
            raise SourceCapabilityError(
                "tushare historical_kline supports only daily/weekly/monthly"
            )
        raise ValueError("period 必须是 daily、weekly 或 monthly")
    return result


def _adjustment(value: str | None) -> tuple[str, str | None]:
    key = str(value or "").strip().lower()
    result = _ADJUSTMENTS.get(key)
    if result is None:
        raise ValueError("adjust 必须是 none、qfq 或 hfq")
    return result


def _factor_for_date(
    factor_dates: list[date], factor_values: list[float], bar_date: date
) -> float:
    index = bisect_right(factor_dates, bar_date) - 1
    if index < 0:
        raise RuntimeError(f"TuShare 缺少 {bar_date.isoformat()} 的复权因子")
    return factor_values[index]


def _factor_series(
    records: list[dict[str, Any]], requested: str
) -> tuple[list[date], list[float]]:
    matches = _matching_records(records, requested, "复权因子")
    by_date: dict[date, float] = {}
    for item in matches:
        trading_date = _date_object(item.get("trade_date"), "adj_factor.trade_date")
        raw_factor = _number(item.get("adj_factor"))
        if raw_factor is None or raw_factor <= 0:
            raise RuntimeError("TuShare 复权因子无效")
        factor = float(raw_factor)
        previous = by_date.get(trading_date)
        if previous is not None and previous != factor:
            raise RuntimeError("TuShare 同一交易日返回冲突复权因子")
        by_date[trading_date] = factor
    factor_dates = sorted(by_date)
    return factor_dates, [by_date[item] for item in factor_dates]


def fetch_historical_kline(
    symbol: str = "",
    code: str = "",
    period: str = "daily",
    start_date: str = "",
    end_date: str = "",
    start: str = "",
    end: str = "",
    count: int | None = None,
    adjust: str | None = "qfq",
    **kwargs: Any,
) -> pd.DataFrame:
    """Fetch daily/weekly/monthly bars and normalize lots/thousand-CNY units."""

    requested = _ts_code(symbol or code)
    api_name, period_attr = _period(period)
    adjust_attr, adjustment = _adjustment(adjust)
    start_value = _bound(start_date, start, "start_date")
    end_value = _bound(end_date, end, "end_date")
    if start_value and end_value and start_value > end_value:
        raise ValueError("start_date 不能晚于 end_date")
    count_value = _count(count)

    params: dict[str, Any] = {"ts_code": requested}
    if start_value:
        params["start_date"] = start_value
    if end_value:
        params["end_date"] = end_value
    if count_value is not None:
        params["limit"] = count_value
    payload = _request(api_name, params, fields=_BAR_FIELDS)
    raw_bars, request_id = _records(payload, f"{period_attr} K线")
    bars = _matching_records(raw_bars, requested, f"{period_attr} K线")

    parsed_bars: list[tuple[date, dict[str, Any]]] = []
    seen_dates: set[date] = set()
    for item in bars:
        trading_date = _date_object(item.get("trade_date"))
        if trading_date in seen_dates:
            raise RuntimeError("TuShare K线返回重复交易日")
        seen_dates.add(trading_date)
        parsed_bars.append((trading_date, item))

    factor_dates: list[date] = []
    factor_values: list[float] = []
    denominator: float | None = None
    if adjustment is not None:
        actual_dates = [item[0] for item in parsed_bars]
        factor_lookback_days = {"daily": 0, "weekly": 7, "monthly": 40}[
            period_attr
        ]
        factor_params = {
            "ts_code": requested,
            "start_date": (
                min(actual_dates) - timedelta(days=factor_lookback_days)
            ).strftime("%Y%m%d"),
            "end_date": max(actual_dates).strftime("%Y%m%d"),
        }
        factor_payload = _request(
            "adj_factor", factor_params, fields=_FACTOR_FIELDS
        )
        factor_records, _factor_request_id = _records(
            factor_payload, "复权因子", allow_empty=True
        )
        if not factor_records:
            raise RuntimeError("TuShare 复权因子返回空数据")
        factor_dates, factor_values = _factor_series(factor_records, requested)
        denominator = _factor_for_date(
            factor_dates, factor_values, max(actual_dates)
        )

    rows: list[dict[str, Any]] = []
    for trading_date, item in parsed_bars:
        prices = {
            name: _number(item.get(name))
            for name in ("open", "high", "low", "close", "pre_close")
        }
        if adjustment is not None:
            factor = _factor_for_date(factor_dates, factor_values, trading_date)
            multiplier = factor if adjustment == "hfq" else factor / float(denominator)
            prices = {
                name: value * multiplier if value is not None else None
                for name, value in prices.items()
            }

        close = prices["close"]
        previous = prices["pre_close"]
        change = close - previous if close is not None and previous is not None else None
        change_pct = change / previous * 100 if change is not None and previous else None
        high = prices["high"]
        low = prices["low"]
        amplitude = (
            (high - low) / previous * 100
            if high is not None and low is not None and previous
            else None
        )
        rows.append(
            {
                "日期": trading_date.isoformat(),
                "开盘": prices["open"],
                "收盘": close,
                "最高": high,
                "最低": low,
                # TuShare historical vol is lots and amount is thousand CNY.
                "成交量": _scaled(item.get("vol"), 100),
                "成交额": _scaled(item.get("amount"), 1000),
                "振幅": amplitude,
                "涨跌幅": change_pct,
                "涨跌额": change,
            }
        )
    rows.sort(key=lambda item: item["日期"])
    latest_date = max(item[0] for item in parsed_bars)
    return _frame(
        rows,
        period=period_attr,
        adjust=adjust_attr,
        provider_as_of=_market_time(latest_date, time(15, 0)),
        request_id=request_id,
    )


def fetch_auction_data(
    code: str = "",
    symbol: str = "",
    trade_date: str = "",
    start_date: str = "",
    end_date: str = "",
    start: str = "",
    end: str = "",
    count: int | None = None,
    **kwargs: Any,
) -> pd.DataFrame:
    """Return the latest final 09:25 opening-auction summary for one stock."""

    requested = _ts_code(symbol or code)
    trade_date_value = _compact_date(trade_date, "trade_date")
    start_value = _bound(start_date, start, "start_date")
    end_value = _bound(end_date, end, "end_date")
    if start_value and end_value and start_value > end_value:
        raise ValueError("start_date 不能晚于 end_date")
    count_value = _count(count)

    params: dict[str, Any] = {"ts_code": requested}
    if trade_date_value:
        params["trade_date"] = trade_date_value
    if start_value:
        params["start_date"] = start_value
    if end_value:
        params["end_date"] = end_value
    if count_value is not None:
        params["limit"] = count_value
    elif not (trade_date_value or start_value or end_value):
        # A current single-symbol lookup must stay bounded even if a compatible
        # upstream implementation changes its default result window.
        params["limit"] = 1
    payload = _request("stk_auction", params, fields=_AUCTION_FIELDS)
    raw_records, request_id = _records(payload, "集合竞价")
    matches = _matching_records(raw_records, requested, "集合竞价")
    dated = [(_date_object(item.get("trade_date")), item) for item in matches]
    if trade_date_value:
        target_date = _date_object(trade_date_value)
        dated = [item for item in dated if item[0] == target_date]
        if not dated:
            raise RuntimeError("TuShare 集合竞价未返回请求交易日")
    latest_date = max(item[0] for item in dated)
    latest = [item for item_date, item in dated if item_date == latest_date]
    if len(latest) != 1:
        raise RuntimeError("TuShare 集合竞价返回重复最终记录")
    item = latest[0]
    price = _number(item.get("price"))
    previous = _number(item.get("pre_close"))
    change_pct = price / previous * 100 - 100 if price is not None and previous else None
    row = {
        "代码": requested,
        "交易日期": latest_date.strftime("%Y%m%d"),
        # stk_auction already reports shares and CNY; do not rescale.
        "开盘价": price,
        "开盘量": _number(item.get("vol")),
        "开盘额": _number(item.get("amount")),
        "开盘涨跌幅": change_pct,
        "昨收": previous,
        "换手率": _number(item.get("turnover_rate")),
        "量比": _number(item.get("volume_ratio")),
        # The provider's float_share unit is ten thousand shares.
        "流通股本": _scaled(item.get("float_share"), 10_000),
    }
    return _frame(
        [row],
        provider_as_of=_market_time(latest_date, time(9, 25)),
        request_id=request_id,
    )


__all__ = [
    "fetch_auction_data",
    "fetch_dragon_tiger_market_day",
    "fetch_etf_quotes",
    "fetch_historical_kline",
    "fetch_market_universe",
    "fetch_minute_data",
    "fetch_realtime_quote",
    "fetch_sector_quotes",
    "fetch_stock_fund_flow",
]
