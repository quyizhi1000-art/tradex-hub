"""Map TuShare Pro tables to Tradex's existing provider-frame contracts."""

from __future__ import annotations

import math
import re
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
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
    "pe_ttm",
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
    "delist_date",
    "list_status",
)
_STOCK_COMPANY_FIELDS = (
    "ts_code",
    "chairman",
    "manager",
    "secretary",
    "reg_capital",
    "setup_date",
    "province",
    "city",
    "introduction",
    "website",
    "email",
    "office",
    "employees",
    "main_business",
    "business_scope",
)
_SW_MEMBER_ALL_FIELDS = (
    "l1_code",
    "l1_name",
    "l2_code",
    "l2_name",
    "l3_code",
    "l3_name",
    "ts_code",
    "name",
    "in_date",
    "out_date",
    "is_new",
)
_MAIN_BUSINESS_FIELDS = (
    "ts_code",
    "end_date",
    "bz_item",
    "bz_sales",
    "bz_profit",
    "bz_cost",
    "curr_type",
    "update_flag",
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
_TRADE_CAL_FIELDS = (
    "exchange",
    "cal_date",
    "is_open",
    "pretrade_date",
)
_FINANCIAL_INDICATOR_FIELDS = (
    "ts_code",
    "ann_date",
    "end_date",
    "roe",
    "roe_waa",
    "grossprofit_margin",
    "debt_to_assets",
    "or_yoy",
    "netprofit_yoy",
    "update_flag",
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

    raw_volume, multipliers = _turnover_volume_multiplier_candidates(
        item, context=context
    )
    if raw_volume in (None, 0):
        return raw_volume
    if len(multipliers) != 1:
        raise RuntimeError(f"TuShare {context}成交量单位无法唯一判定")
    return raw_volume * multipliers[0]


def _turnover_volume_multiplier_candidates(
    item: Mapping[str, Any], *, context: str
) -> tuple[float | int | None, tuple[int, ...]]:
    """Return the safe x1/x100 interpretations for one turnover row."""

    raw_volume = _number(item.get("vol"))
    amount = _number(item.get("amount"))
    if raw_volume is None:
        return None, ()
    if raw_volume < 0:
        raise RuntimeError(f"TuShare {context}成交量无效")
    if raw_volume == 0:
        if amount not in (None, 0):
            raise RuntimeError(f"TuShare {context}零成交量与成交额冲突")
        return 0, ()
    if amount is None or amount <= 0:
        raise RuntimeError(f"TuShare {context}缺少可校验的成交额")

    low = _number(item.get("low"))
    high = _number(item.get("high"))
    if low is None or high is None or low <= 0 or high + 1e-8 < low:
        raise RuntimeError(f"TuShare {context}缺少可校验的价格区间")

    candidates: list[int] = []
    for multiplier in (1, 100):
        normalized = raw_volume * multiplier
        implied_average = amount / normalized
        # Compatible realtime proxies can round the cumulative turnover and
        # volume independently, which is visible on lightly traded ETFs.  A
        # two-percent price tolerance still cleanly separates x1 from x100
        # while accepting that documented rounding noise.
        tolerance = max(0.01, float(high) * 0.02, 1.0 / normalized)
        if float(low) - tolerance <= implied_average <= float(high) + tolerance:
            candidates.append(multiplier)
    return raw_volume, tuple(candidates)


def _turnover_volume_shares_batch(
    items: Iterable[Mapping[str, Any]], *, context: str
) -> tuple[list[float | int | None], int]:
    """Normalize a cross-section without one rounded sparse row poisoning it.

    Every row first uses the strict amount/price-range check.  A row with no
    unique interpretation may borrow the dominant multiplier only when at
    least twenty other rows from the same provider request agree and at least
    95 percent of all resolved evidence supports that multiplier.
    """

    rows = list(items)
    resolved: list[float | int | None] = [None] * len(rows)
    candidates_by_index: dict[int, tuple[float | int, tuple[int, ...]]] = {}
    evidence = {1: 0, 100: 0}
    unresolved: list[int] = []
    for index, item in enumerate(rows):
        raw_volume, multipliers = _turnover_volume_multiplier_candidates(
            item, context=context
        )
        if raw_volume in (None, 0):
            resolved[index] = raw_volume
            continue
        candidates_by_index[index] = (raw_volume, multipliers)
        if len(multipliers) == 1:
            multiplier = multipliers[0]
            evidence[multiplier] += 1
            resolved[index] = raw_volume * multiplier
        else:
            unresolved.append(index)

    if not unresolved:
        return resolved, 0

    winner = max(evidence, key=evidence.get)
    evidence_total = sum(evidence.values())
    winner_count = evidence[winner]
    if (
        evidence_total < 20
        or winner_count / evidence_total < 0.95
    ):
        raise RuntimeError(f"TuShare {context}成交量单位无法唯一判定")
    for index in unresolved:
        raw_volume, _multipliers = candidates_by_index[index]
        resolved[index] = raw_volume * winner
    return resolved, len(unresolved)


def _minute_turnover_values(
    items: Iterable[Mapping[str, Any]],
) -> tuple[list[float | int], list[float | int | None], int, int]:
    """Normalize one stock's minute turnover without discarding valid prices.

    Unit ambiguity is resolved from the complete same-request series.  A
    provider amount that still conflicts with its own OHLC bar is omitted so
    the canonical series is explicitly degraded instead of rejecting the
    independently valid price curve.
    """

    rows = list(items)
    volumes: list[float | int | None] = [None] * len(rows)
    amounts = [_number(item.get("amount")) for item in rows]
    evidence = {1: 0, 100: 0}
    unresolved: list[tuple[int, float | int]] = []
    invalid_amount_rows: set[int] = set()
    for index, item in enumerate(rows):
        raw_volume = _number(item.get("vol"))
        if raw_volume is None or raw_volume < 0:
            raise RuntimeError("TuShare 实时分钟成交量无效")
        if raw_volume == 0:
            volumes[index] = 0
            if amounts[index] not in (None, 0):
                amounts[index] = None
                invalid_amount_rows.add(index)
            continue
        try:
            _, multipliers = _turnover_volume_multiplier_candidates(
                item,
                context="实时分钟",
            )
        except RuntimeError:
            multipliers = ()
        if len(multipliers) == 1:
            multiplier = multipliers[0]
            evidence[multiplier] += 1
            volumes[index] = raw_volume * multiplier
        else:
            unresolved.append((index, raw_volume))

    inferred_rows = 0
    if unresolved:
        winner = max(evidence, key=evidence.get)
        evidence_total = sum(evidence.values())
        if evidence_total < 20 or evidence[winner] / evidence_total < 0.95:
            raise RuntimeError("TuShare 实时分钟成交量单位无法唯一判定")
        for index, raw_volume in unresolved:
            volumes[index] = raw_volume * winner
        inferred_rows = len(unresolved)

    for index, (item, volume) in enumerate(zip(rows, volumes, strict=True)):
        assert volume is not None
        amount = amounts[index]
        if amount is None or amount < 0:
            amounts[index] = None
            invalid_amount_rows.add(index)
            continue
        if volume == 0:
            if amount != 0:
                amounts[index] = None
                invalid_amount_rows.add(index)
            continue
        low = _number(item.get("low"))
        high = _number(item.get("high"))
        if low is None or high is None:
            amounts[index] = None
            invalid_amount_rows.add(index)
            continue
        implied_average = amount / volume
        if not float(low) - 0.01 <= implied_average <= float(high) + 0.01:
            amounts[index] = None
            invalid_amount_rows.add(index)

    return (
        [value for value in volumes if value is not None],
        amounts,
        inferred_rows,
        len(invalid_amount_rows),
    )


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


def _paged_records(
    api_name: str,
    params: Mapping[str, Any],
    fields: Iterable[str],
    *,
    context: str,
    allow_empty: bool = False,
    page_size: int = 6000,
    max_pages: int = 3,
) -> tuple[list[dict[str, Any]], list[tuple[str, str | None]]]:
    """Read one bounded full-market table without silently truncating it."""

    rows: list[dict[str, Any]] = []
    request_ids: list[tuple[str, str | None]] = []
    for page in range(max_pages):
        offset = page * page_size
        payload = _request(
            api_name,
            {**dict(params), "limit": page_size, "offset": offset},
            fields=fields,
        )
        batch, request_id = _records(
            payload,
            context,
            allow_empty=True,
        )
        request_ids.append((f"{api_name}:{offset}", request_id))
        rows.extend(batch)
        if len(batch) < page_size:
            break
    else:
        raise RuntimeError(f"TuShare {context}超过受控分页上限")
    if not rows and not allow_empty:
        raise RuntimeError(f"TuShare {context}返回空数据")
    return rows, request_ids


def _quarter_periods(target: date, count: int = 8) -> tuple[str, ...]:
    candidates: list[date] = []
    for year in range(target.year, target.year - 4, -1):
        candidates.extend(
            date(year, month, day)
            for month, day in ((12, 31), (9, 30), (6, 30), (3, 31))
        )
    return tuple(
        item.strftime("%Y%m%d")
        for item in sorted((item for item in candidates if item <= target), reverse=True)[
            :count
        ]
    )


def _taxonomy_reporting_periods(target: date) -> tuple[str, ...]:
    """Bound the catalog refresh to recent annual and half-year disclosures."""

    candidates = (
        date(target.year, 6, 30),
        date(target.year - 1, 12, 31),
        date(target.year - 1, 6, 30),
        date(target.year - 2, 12, 31),
    )
    return tuple(item.strftime("%Y%m%d") for item in candidates if item <= target)


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


def _stock_selection_dates(
    compact: str,
    trading_date: date,
) -> tuple[str, str, tuple[str, ...], str | None]:
    calendar_start = (trading_date - timedelta(days=220)).strftime("%Y%m%d")
    payload = _request(
        "trade_cal",
        {"exchange": "SSE", "start_date": calendar_start, "end_date": compact},
        fields=_TRADE_CAL_FIELDS,
    )
    rows, request_id = _records(payload, "选股交易日历")
    open_dates = sorted(
        {
            _compact_date(row.get("cal_date"), "trade_cal.cal_date", allow_empty=False)
            for row in rows
            if int(_number(row.get("is_open")) or 0) == 1
        }
    )
    if compact not in open_dates:
        raise SourceCapabilityError("stock_selection_calendar requires an open trade date")
    target_index = open_dates.index(compact)
    if target_index < 60:
        raise RuntimeError("TuShare 选股交易日历不足 60 个历史交易日")
    candlestick_window = tuple(open_dates[target_index - 14 : target_index + 1])
    if len(candlestick_window) != 15:
        raise RuntimeError("TuShare 选股交易日历不足 15 个形态观察日")
    return (
        open_dates[target_index - 20],
        open_dates[target_index - 60],
        candlestick_window,
        request_id,
    )


def fetch_stock_selection_calendar(
    trade_date: str = "",
    code: str = "",
    symbol: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Resolve exact comparison sessions and the benchmark for one signal day."""
    del kwargs
    if code or symbol:
        raise SourceCapabilityError("stock_selection_calendar is a whole-market route")
    compact, trading_date = _daily_trade_date(trade_date, required=True)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="tushare-selection-calendar") as executor:
        dates_job = executor.submit(_stock_selection_dates, compact, trading_date)
        benchmark_job = executor.submit(
            _paged_records,
            "index_daily",
            {"ts_code": "000300.SH", "trade_date": compact},
            _BAR_FIELDS,
            context="选股基准",
            page_size=100,
            max_pages=1,
        )
        (
            prior_20d,
            prior_60d,
            candlestick_window,
            calendar_request_id,
        ) = dates_job.result()
        benchmark, benchmark_ids = benchmark_job.result()
    request_ids: list[tuple[str, str | None]] = [("trade_cal", calendar_request_id)]
    request_ids.extend((f"benchmark:{label}", value) for label, value in benchmark_ids)
    return {
        "trade_date": trading_date.isoformat(),
        "prior_20d_trade_date": datetime.strptime(prior_20d, "%Y%m%d").date().isoformat(),
        "prior_60d_trade_date": datetime.strptime(prior_60d, "%Y%m%d").date().isoformat(),
        "candlestick_window_trade_dates": [
            datetime.strptime(value, "%Y%m%d").date().isoformat()
            for value in candlestick_window
        ],
        "provider_as_of": _market_time(trading_date, time(18, 0)),
        "request_id": _request_id_bundle(*request_ids),
        "benchmark": benchmark,
    }


def fetch_stock_selection_daily(
    trade_date: str = "",
    session_date: str = "",
    code: str = "",
    symbol: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Fetch the exact current full-market daily table."""
    del kwargs
    if code or symbol:
        raise SourceCapabilityError("stock_selection_daily is a whole-market route")
    compact, trading_date = _daily_trade_date(trade_date, required=True)
    session_compact = _compact_date(
        session_date or compact, "session_date", allow_empty=False
    )
    if session_compact > compact:
        raise ValueError("stock-selection session_date cannot follow trade_date")
    daily, request_ids = _paged_records(
        "daily",
        {"trade_date": session_compact},
        _BAR_FIELDS,
        context="选股全市场日线",
        page_size=6000,
        max_pages=2,
    )
    return {
        "trade_date": trading_date.isoformat(),
        "session_date": datetime.strptime(session_compact, "%Y%m%d").date().isoformat(),
        "request_id": _request_id_bundle(
            *((f"daily:{label}", value) for label, value in request_ids)
        ),
        "daily": daily,
    }


def fetch_stock_selection_daily_basic(
    trade_date: str = "",
    code: str = "",
    symbol: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Fetch the exact current valuation and liquidity table."""
    del kwargs
    if code or symbol:
        raise SourceCapabilityError("stock_selection_daily_basic is a whole-market route")
    compact, trading_date = _daily_trade_date(trade_date, required=True)
    daily_basic, request_ids = _paged_records(
        "daily_basic",
        {"trade_date": compact},
        _DAILY_BASIC_FIELDS,
        context="选股每日指标",
        page_size=6000,
        max_pages=2,
    )
    return {
        "trade_date": trading_date.isoformat(),
        "request_id": _request_id_bundle(
            *((f"daily_basic:{label}", value) for label, value in request_ids)
        ),
        "daily_basic": daily_basic,
    }


_LIMIT_SENTIMENT_FIELDS = (
    "trade_date",
    "ts_code",
    "name",
    "pct_chg",
    "open_num",
    "lu_desc",
    "limit_type",
    "tag",
    "status",
    "first_lu_time",
    "last_lu_time",
    "limit_amount",
    "limit_up_suc_rate",
)


def fetch_limit_sentiment_daily(
    trade_date: str = "",
    previous_trade_date: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Fetch one complete same-provider limit-sentiment bundle.

    The gateway owns all calculations. This provider function only acquires
    bounded raw tables and preserves their individual request identifiers.
    """

    del kwargs
    compact, trading_date = _daily_trade_date(trade_date, required=True)
    previous_compact = _compact_date(
        previous_trade_date,
        "previous_trade_date",
        allow_empty=False,
    )
    previous_date = datetime.strptime(previous_compact, "%Y%m%d").date()
    if previous_date >= trading_date:
        raise ValueError("previous_trade_date must precede trade_date")

    request_ids: list[tuple[str, str | None]] = []

    def limit_rows(limit_type: str, label: str, *, previous: bool = False):
        rows, ids = _paged_records(
            "limit_list_ths",
            {
                "trade_date": previous_compact if previous else compact,
                "limit_type": limit_type,
            },
            _LIMIT_SENTIMENT_FIELDS,
            context=f"涨跌停情绪:{label}",
            allow_empty=True,
            page_size=4000,
            max_pages=2,
        )
        request_ids.extend((f"{label}:{name}", value) for name, value in ids)
        return rows

    limit_up = limit_rows("涨停池", "limit_up")
    broken = limit_rows("炸板池", "broken")
    previous_limit_up = limit_rows("涨停池", "previous_limit_up", previous=True)
    daily, daily_ids = _paged_records(
        "daily",
        {"trade_date": compact},
        _BAR_FIELDS,
        context="涨跌停情绪:次日反馈",
        allow_empty=False,
        page_size=6000,
        max_pages=2,
    )
    request_ids.extend((f"daily:{name}", value) for name, value in daily_ids)
    return {
        "trade_date": trading_date.isoformat(),
        "previous_trade_date": previous_date.isoformat(),
        "request_id": _request_id_bundle(*request_ids),
        "limit_up": limit_up,
        "broken": broken,
        "previous_limit_up": previous_limit_up,
        "daily": daily,
    }


def fetch_stock_selection_master(
    trade_date: str = "",
    code: str = "",
    symbol: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Fetch active, delisted and paused security master rows concurrently."""
    del kwargs
    if code or symbol:
        raise SourceCapabilityError("stock_selection_master is a whole-market route")
    _compact, trading_date = _daily_trade_date(trade_date, required=True)
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="tushare-selection-master") as executor:
        jobs = {
            status: executor.submit(
                _paged_records,
                "stock_basic",
                {"exchange": "", "list_status": status},
                _STOCK_BASIC_FIELDS,
                context=f"选股证券主数据:{status}",
                allow_empty=status != "L",
            )
            for status in ("L", "D", "P")
        }
        results = {status: future.result() for status, future in jobs.items()}
    request_ids: list[tuple[str, str | None]] = []
    stock_basic: list[dict[str, Any]] = []
    for status in ("L", "D", "P"):
        rows, ids = results[status]
        stock_basic.extend(rows)
        request_ids.extend((f"stock_basic:{status}:{label}", value) for label, value in ids)
    return {
        "trade_date": trading_date.isoformat(),
        "request_id": _request_id_bundle(*request_ids),
        "stock_basic": stock_basic,
    }


def fetch_stock_selection_financial_period(
    trade_date: str = "",
    period: str = "",
    code: str = "",
    symbol: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Fetch one exact full-market quarterly indicator table."""
    del kwargs
    if code or symbol:
        raise SourceCapabilityError(
            "stock_selection_financial_period is a whole-market route"
        )
    _compact, trading_date = _daily_trade_date(trade_date, required=True)
    compact_period = _compact_date(period, "period", allow_empty=False)
    if compact_period not in _quarter_periods(trading_date):
        raise ValueError("stock-selection period is outside the bounded quarter set")
    financials, request_ids = _paged_records(
        "fina_indicator_vip",
        {"period": compact_period},
        _FINANCIAL_INDICATOR_FIELDS,
        context=f"选股财务指标:{compact_period}",
        allow_empty=True,
    )
    return {
        "trade_date": trading_date.isoformat(),
        "period": compact_period,
        "request_id": _request_id_bundle(
            *((f"fina_indicator_vip:{compact_period}:{label}", value) for label, value in request_ids)
        ),
        "financials": financials,
    }


def fetch_instrument_taxonomy_source(
    as_of: str = "",
    code: str = "",
    symbol: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Fetch one low-frequency full-market relationship source bundle."""

    del kwargs
    if code or symbol:
        raise SourceCapabilityError("instrument_taxonomy is a whole-market route")
    if as_of:
        compact, target = _daily_trade_date(as_of, required=True)
    else:
        target = datetime.now(_SHANGHAI).date()
        compact = target.strftime("%Y%m%d")

    stocks, stock_ids = _paged_records(
        "stock_basic",
        {"exchange": "", "list_status": "L"},
        _STOCK_BASIC_FIELDS,
        context="证券关系库股票主数据",
        page_size=6000,
        max_pages=2,
    )

    with ThreadPoolExecutor(
        max_workers=3,
        thread_name_prefix="tushare-taxonomy-company",
    ) as executor:
        company_jobs = {
            exchange: executor.submit(
                _paged_records,
                "stock_company",
                {"exchange": exchange},
                _STOCK_COMPANY_FIELDS,
                context=f"证券关系库公司信息:{exchange}",
                allow_empty=exchange == "BSE",
                page_size=5000,
                max_pages=2,
            )
            for exchange in ("SSE", "SZSE", "BSE")
        }
        company_results = {
            exchange: future.result()
            for exchange, future in company_jobs.items()
        }
    companies: list[dict[str, Any]] = []
    company_ids: list[tuple[str, str | None]] = []
    for exchange in ("SSE", "SZSE", "BSE"):
        rows, ids = company_results[exchange]
        companies.extend(rows)
        company_ids.extend(
            (f"stock_company:{exchange}:{label}", value) for label, value in ids
        )

    sw_memberships, sw_ids = _paged_records(
        "index_member_all",
        {"is_new": "Y"},
        _SW_MEMBER_ALL_FIELDS,
        context="证券关系库申万行业成分",
        allow_empty=True,
        page_size=2000,
        max_pages=4,
    )

    periods = _taxonomy_reporting_periods(target)
    segment_rows: list[dict[str, Any]] = []
    segment_ids: list[tuple[str, str | None]] = []
    segment_flags: list[str] = []
    with ThreadPoolExecutor(
        max_workers=min(4, len(periods)),
        thread_name_prefix="tushare-taxonomy-mainbz",
    ) as executor:
        jobs = {
            period: executor.submit(
                _paged_records,
                "fina_mainbz_vip",
                {"period": period, "type": "P"},
                _MAIN_BUSINESS_FIELDS,
                context=f"证券关系库主营构成:{period}",
                allow_empty=True,
                page_size=10000,
                max_pages=6,
            )
            for period in periods
        }
        for period in periods:
            try:
                rows, ids = jobs[period].result()
            except Exception as exc:  # noqa: BLE001 - older accepted periods remain usable
                segment_flags.append(
                    f"business_segments_unavailable:{period}:{type(exc).__name__}"
                )
                continue
            segment_rows.extend(rows)
            segment_ids.extend(
                (f"fina_mainbz_vip:{period}:{label}", value)
                for label, value in ids
            )
    if not segment_rows:
        raise RuntimeError("TuShare 证券关系库主营构成全部不可用")

    request_ids = [
        *((f"stock_basic:{label}", value) for label, value in stock_ids),
        *company_ids,
        *((f"index_member_all:{label}", value) for label, value in sw_ids),
        *segment_ids,
    ]
    return {
        "contract": "instrument_taxonomy_source_bundle.v1",
        "schema_version": 1,
        "as_of": target.isoformat(),
        "request_id": _request_id_bundle(*request_ids),
        "source_providers": ["tushare"],
        "source_request_ids": [value for _label, value in request_ids if value],
        "stocks": stocks,
        "companies": companies,
        "sw_memberships": sw_memberships,
        "business_segments": segment_rows,
        "reporting_periods": list(periods),
        "flags": segment_flags,
        "requested_as_of": compact,
    }


def fetch_daily_stock_factors(
    trade_date: str = "",
    code: str = "",
    symbol: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Compatibility composition for the provider-neutral factor mapper."""
    if code or symbol:
        raise SourceCapabilityError("daily_stock_factors is a whole-market route")
    calendar = fetch_stock_selection_calendar(trade_date=trade_date, **kwargs)
    daily = fetch_stock_selection_daily(trade_date=trade_date, **kwargs)
    daily_basic = fetch_stock_selection_daily_basic(trade_date=trade_date, **kwargs)
    daily_20d = fetch_stock_selection_daily(
        trade_date=trade_date,
        session_date=calendar["prior_20d_trade_date"],
        **kwargs,
    )
    daily_60d = fetch_stock_selection_daily(
        trade_date=trade_date,
        session_date=calendar["prior_60d_trade_date"],
        **kwargs,
    )
    daily_window = [daily]
    for session_date in calendar["candlestick_window_trade_dates"]:
        if session_date == calendar["trade_date"]:
            continue
        daily_window.append(
            fetch_stock_selection_daily(
                trade_date=trade_date,
                session_date=session_date,
                **kwargs,
            )
        )
    daily_window.sort(key=lambda item: item["session_date"])
    master = fetch_stock_selection_master(trade_date=trade_date, **kwargs)
    periods = _quarter_periods(_daily_trade_date(trade_date, required=True)[1])
    with ThreadPoolExecutor(
        max_workers=4,
        thread_name_prefix="tushare-selection-financial-compose",
    ) as executor:
        jobs = {
            period: executor.submit(
                fetch_stock_selection_financial_period,
                trade_date=trade_date,
                period=period,
                **kwargs,
            )
            for period in periods
        }
        financial_slices = {period: jobs[period].result() for period in periods}
    financial_rows = [
        row
        for period in periods
        for row in financial_slices[period]["financials"]
    ]

    return {
        **calendar,
        "daily": daily["daily"],
        "daily_basic": daily_basic["daily_basic"],
        "daily_20d": daily_20d["daily"],
        "daily_60d": daily_60d["daily"],
        "daily_window": daily_window,
        "provider_as_of": calendar["provider_as_of"],
        "request_id": _request_id_bundle(
            ("calendar", calendar.get("request_id")),
            ("daily", daily.get("request_id")),
            ("daily_basic", daily_basic.get("request_id")),
            ("daily_20d", daily_20d.get("request_id")),
            ("daily_60d", daily_60d.get("request_id")),
            *(
                (f"daily_window:{item['session_date']}", item.get("request_id"))
                for item in daily_window
                if item["session_date"] != calendar["trade_date"]
            ),
            ("master", master.get("request_id")),
            *(
                (f"financials:{period}", financial_slices[period].get("request_id"))
                for period in periods
            ),
        ),
        "stock_basic": master["stock_basic"],
        "financials": financial_rows,
        "unit_contract": {
            "daily.amount": "thousand_CNY",
            "daily.vol": "lots",
            "daily_basic.*_mv": "ten_thousand_CNY",
            "percentage_fields": "percentage_points",
        },
    }


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


def _minute_frame_from_records(
    requested: str,
    records: Iterable[Mapping[str, Any]],
    *,
    request_id: str | None,
) -> pd.DataFrame:
    matches = _matching_records(records, requested, "实时分钟")
    (
        minute_volumes,
        minute_amounts,
        inferred_volume_rows,
        invalid_amount_rows,
    ) = _minute_turnover_values(matches)

    rows: list[dict[str, Any]] = []
    seen_times: set[datetime] = set()
    trading_dates: set[date] = set()
    for item, volume_shares, amount_cny in zip(
        matches,
        minute_volumes,
        minute_amounts,
        strict=True,
    ):
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
        volume_unit_inferred_rows=inferred_volume_rows,
        amount_invalid_rows=invalid_amount_rows,
    )


def fetch_minute_data(
    symbol: str = "", code: str = "", **kwargs: Any
) -> pd.DataFrame:
    """Fetch one complete current-session 1-minute series via ``rt_min``."""

    requested = _ts_code(symbol or code)
    payload = _request(
        "rt_min",
        {"ts_code": requested, "freq": "1MIN"},
        fields=_MINUTE_FIELDS,
    )
    records, request_id = _records(payload, "实时分钟")
    return _minute_frame_from_records(
        requested,
        records,
        request_id=request_id,
    )


def fetch_minute_data_batch(
    symbols: Iterable[str] | str = (),
    **kwargs: Any,
) -> pd.DataFrame:
    """Fetch up to 40 complete stock minute series in one ``rt_min`` call."""

    raw_symbols = (
        [item.strip() for item in symbols.split(",") if item.strip()]
        if isinstance(symbols, str)
        else list(symbols)
    )
    requested = tuple(dict.fromkeys(_ts_code(item) for item in raw_symbols))
    if not requested:
        raise ValueError("minute batch requires at least one A-share symbol")
    if len(requested) > 40:
        raise ValueError("minute batch supports at most 40 A-share symbols")
    payload = _request(
        "rt_min",
        {"ts_code": ",".join(requested), "freq": "1MIN"},
        fields=_MINUTE_FIELDS,
    )
    records, request_id = _records(payload, "实时分钟批量")
    frames = [
        _minute_frame_from_records(
            instrument_id,
            records,
            request_id=request_id,
        )
        for instrument_id in requested
    ]
    trading_dates = {str(frame.attrs["trading_date"]) for frame in frames}
    if len(trading_dates) != 1:
        raise RuntimeError("TuShare 实时分钟批量返回跨交易日数据")
    rows = [row for frame in frames for row in frame.to_dict("records")]
    rows.sort(key=lambda item: (item["代码"], item["时间"]))
    return _frame(
        rows,
        provider_as_of=max(str(frame.attrs["provider_as_of"]) for frame in frames),
        request_id=request_id,
        trading_date=trading_dates.pop(),
        frequency_minutes=1,
        volume_unit="shares",
        amount_unit="CNY",
        volume_unit_inferred_rows=sum(
            int(frame.attrs.get("volume_unit_inferred_rows", 0)) for frame in frames
        ),
        amount_invalid_rows=sum(
            int(frame.attrs.get("amount_invalid_rows", 0)) for frame in frames
        ),
    )


def fetch_minute_data_batch_partial(
    symbols: Iterable[str] | str = (),
    trade_date: date | str | None = None,
    **kwargs: Any,
) -> pd.DataFrame:
    """Return every exact series present in one bounded ``rt_min`` response."""

    raw_symbols = (
        [item.strip() for item in symbols.split(",") if item.strip()]
        if isinstance(symbols, str)
        else list(symbols)
    )
    requested = tuple(dict.fromkeys(_ts_code(item) for item in raw_symbols))
    if not requested:
        raise ValueError("partial minute batch requires at least one A-share symbol")
    if len(requested) > 40:
        raise ValueError("partial minute batch supports at most 40 A-share symbols")
    payload = _request(
        "rt_min",
        {"ts_code": ",".join(requested), "freq": "1MIN"},
        fields=_MINUTE_FIELDS,
    )
    records, request_id = _records(payload, "实时分钟批量")
    present = {
        _ts_code(str(item.get("ts_code") or ""))
        for item in records
        if str(item.get("ts_code") or "").strip()
    }
    frames = [
        _minute_frame_from_records(instrument_id, records, request_id=request_id)
        for instrument_id in requested
        if instrument_id in present
    ]
    if trade_date is not None:
        expected_date = (
            trade_date.isoformat()
            if isinstance(trade_date, date)
            else date.fromisoformat(str(trade_date)).isoformat()
        )
        frames = [
            frame
            for frame in frames
            if str(frame.attrs.get("trading_date")) == expected_date
        ]
    if not frames:
        raise RuntimeError("TuShare 实时分钟批量未返回任何请求证券")
    trading_dates = {str(frame.attrs["trading_date"]) for frame in frames}
    if len(trading_dates) != 1:
        raise RuntimeError("TuShare 实时分钟批量返回跨交易日数据")
    rows = [row for frame in frames for row in frame.to_dict("records")]
    rows.sort(key=lambda item: (item["代码"], item["时间"]))
    return _frame(
        rows,
        provider_as_of=max(str(frame.attrs["provider_as_of"]) for frame in frames),
        request_id=request_id,
        trading_date=trading_dates.pop(),
        frequency_minutes=1,
        volume_unit="shares",
        amount_unit="CNY",
        volume_unit_inferred_rows=sum(
            int(frame.attrs.get("volume_unit_inferred_rows", 0)) for frame in frames
        ),
        amount_invalid_rows=sum(
            int(frame.attrs.get("amount_invalid_rows", 0)) for frame in frames
        ),
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
    records: list[tuple[dict[str, Any], float | int | None]] = []
    volume_consensus_fallback_count = 0
    for label, params in requests:
        payload = _request("rt_etf_k", params, fields=_RT_FIELDS)
        batch, request_id = _records(payload, f"{label.upper()} ETF实时日线")
        batch_volumes, fallback_count = _turnover_volume_shares_batch(
            batch, context=f"{label.upper()} ETF实时日线"
        )
        records.extend(zip(batch, batch_volumes))
        volume_consensus_fallback_count += fallback_count
        request_ids.append((f"rt_etf_k_{label}", request_id))

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    provider_times: list[str] = []
    for item, volume_shares in records:
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
                "vol": volume_shares,
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
        volume_consensus_fallback_count=volume_consensus_fallback_count,
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


def fetch_opening_auction_market(
    trade_date: str = "",
    **kwargs: Any,
) -> pd.DataFrame:
    """Return every final 09:25 auction row for one exact trading day."""

    del kwargs
    requested = _compact_date(trade_date, "trade_date")
    if not requested:
        raise ValueError("trade_date is required for full-market opening auction")
    payload = _request(
        "stk_auction",
        {"trade_date": requested},
        fields=_AUCTION_FIELDS,
    )
    raw_records, request_id = _records(payload, "全市场集合竞价")
    target_date = _date_object(requested)
    rows: list[dict[str, Any]] = []
    for item in raw_records:
        item_date = _date_object(item.get("trade_date"))
        if item_date != target_date:
            raise RuntimeError("TuShare 全市场集合竞价返回了其他交易日")
        requested_code = _ts_code(str(item.get("ts_code") or ""))
        price = _number(item.get("price"))
        previous = _number(item.get("pre_close"))
        rows.append(
            {
                "代码": requested_code,
                "交易日期": item_date.strftime("%Y%m%d"),
                "开盘价": price,
                "开盘量": _number(item.get("vol")),
                "开盘额": _number(item.get("amount")),
                "开盘涨跌幅": (
                    price / previous * 100 - 100
                    if price is not None and previous
                    else None
                ),
                "昨收": previous,
                "换手率": _number(item.get("turnover_rate")),
                "量比": _number(item.get("volume_ratio")),
                "流通股本": _scaled(item.get("float_share"), 10_000),
            }
        )
    if not rows:
        raise RuntimeError("TuShare 全市场集合竞价未返回请求交易日")
    return _frame(
        rows,
        provider_as_of=_market_time(target_date, time(9, 25)),
        request_id=request_id,
    )


__all__ = [
    "fetch_auction_data",
    "fetch_opening_auction_market",
    "fetch_dragon_tiger_market_day",
    "fetch_etf_quotes",
    "fetch_historical_kline",
    "fetch_instrument_taxonomy_source",
    "fetch_market_universe",
    "fetch_minute_data",
    "fetch_minute_data_batch",
    "fetch_minute_data_batch_partial",
    "fetch_realtime_quote",
    "fetch_sector_quotes",
    "fetch_stock_fund_flow",
]
