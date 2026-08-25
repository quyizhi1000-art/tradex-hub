"""Provider adapters for quote and OHLCV canonical contracts."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from tradex.utils.symbol import get_exchange, normalize_symbol

from ..contracts import OHLCVBarV1
from .market_overview import field, finite_number, parse_provider_time


_KNOWN_PROVIDERS = {
    "akshare",
    "biying",
    "eltdx",
    "tencent_http",
    "ths_fuyao",
    "tushare",
}


def canonical_instrument_id(symbol: str) -> str:
    raw = str(symbol or "").strip().upper()
    suffix = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", raw)
    prefix = re.fullmatch(r"(SH|SZ|BJ)\.?([0-9]{6})", raw)
    if suffix:
        bare, exchange = suffix.groups()
    elif prefix:
        exchange, bare = prefix.groups()
    else:
        bare = normalize_symbol(raw)
        exchange = get_exchange(raw).upper()
    if not re.fullmatch(r"\d{6}", bare):
        raise ValueError(f"invalid A-share symbol: {symbol!r}")
    return f"{bare}.{exchange}"


def _record_code(row: dict[str, Any]) -> str | None:
    raw = str(
        field(row, "代码", "同花顺代码", "证券代码", "code", "symbol", "ticker")
        or ""
    ).strip()
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", raw)
    return match.group(1) if match else None


def _matched_field(row: dict[str, Any], *names: str) -> tuple[str | None, Any]:
    normalized = {
        str(key).strip().lower().replace(" ", ""): (str(key), value)
        for key, value in row.items()
    }
    for name in names:
        match = normalized.get(name.strip().lower().replace(" ", ""))
        if match is not None:
            return match
    return None, None


def _volume_shares(row: dict[str, Any], provider: str) -> float | None:
    column, raw = _matched_field(row, "成交量", "成交量(手)", "volume", "vol")
    value = finite_number(raw)
    if value is None:
        return None
    # Fuyao exposes shares. eltdx and Tencent explicitly expose lots. AKShare's
    # Eastmoney Chinese column is lots, while its Sina fallback `volume` is shares.
    if provider in {"eltdx", "tencent_http"}:
        return value * 100
    if provider == "akshare" and column in {"成交量", "成交量(手)"}:
        return value * 100
    return value


def _amount_cny(row: dict[str, Any], provider: str) -> float | None:
    value = finite_number(field(row, "成交额", "成交额(元)", "amount", "turnover"))
    if value is not None and provider == "tencent_http":
        return value * 10_000
    return value


def _market_cap_cny(row: dict[str, Any], provider: str, *names: str) -> float | None:
    column, raw = _matched_field(row, *names)
    value = finite_number(raw)
    if value is None:
        return None
    if provider == "tencent_http":
        return value * 100_000_000
    if provider == "akshare" and column in {"mktcap", "nmc"}:
        return value * 10_000
    return value


def provider_units_verified(provider: str) -> bool:
    return provider in _KNOWN_PROVIDERS


def map_quote_frame(
    frame: Any,
    *,
    provider: str,
    requested_symbol: str,
) -> dict[str, Any]:
    """Select the exact requested instrument and normalize its quote fields."""

    if frame is None or not hasattr(frame, "to_dict"):
        raise RuntimeError("quote provider returned an unsupported payload")
    records = frame.to_dict(orient="records")
    requested = canonical_instrument_id(requested_symbol)[:6]
    matches = [row for row in records if _record_code(row) == requested]
    if not matches:
        raise RuntimeError(f"quote provider did not return requested symbol {requested}")
    if len(matches) > 1:
        raise RuntimeError(f"quote provider returned duplicate symbol {requested}")
    row = matches[0]

    provider_as_of = parse_provider_time(
        field(row, "更新时间", "provider_as_of", "timestamp")
    ) or parse_provider_time(getattr(frame, "attrs", {}).get("provider_as_of"))
    name_value = field(row, "名称", "股票名称", "name")

    return {
        "instrument_id": canonical_instrument_id(requested_symbol),
        "name": str(name_value).strip() if name_value not in (None, "") else None,
        "last": finite_number(field(row, "最新价", "现价", "trade", "price", "last")),
        "change": finite_number(field(row, "涨跌额", "pricechange", "change")),
        "change_pct": finite_number(
            field(row, "涨跌幅", "涨跌幅(%)", "changepercent", "change_pct", "pct_chg")
        ),
        "previous_close": finite_number(
            field(row, "昨收", "前收盘", "settlement", "pre_close", "previous_close")
        ),
        "open": finite_number(field(row, "今开", "开盘", "open")),
        "high": finite_number(field(row, "最高", "最高价", "high")),
        "low": finite_number(field(row, "最低", "最低价", "low")),
        "volume_shares": _volume_shares(row, provider),
        "amount_cny": _amount_cny(row, provider),
        "turnover_pct": finite_number(
            field(row, "换手率", "换手率(%)", "turnoverratio", "turnover_rate")
        ),
        "pe_ttm": finite_number(
            field(row, "市盈率-动态", "市盈率", "per", "pe_ttm", "pe")
        ),
        "pb": finite_number(field(row, "市净率", "pb")),
        "total_market_cap_cny": _market_cap_cny(
            row, provider, "总市值", "total_market_cap", "mktcap"
        ),
        "float_market_cap_cny": _market_cap_cny(
            row, provider, "流通市值", "float_market_cap", "nmc"
        ),
        "provider_as_of": provider_as_of,
    }


def _trading_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if len(raw) >= 10:
        raw = raw[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"invalid OHLCV trading date: {value!r}") from exc


def map_ohlcv_frame(frame: Any, *, provider: str) -> tuple[OHLCVBarV1, ...]:
    if frame is None or not hasattr(frame, "to_dict"):
        raise RuntimeError("OHLCV provider returned an unsupported payload")
    records = frame.to_dict(orient="records")
    if not records:
        raise RuntimeError("OHLCV provider returned no bars")

    bars = [
        OHLCVBarV1(
            trading_date=_trading_date(field(row, "日期", "date", "time")),
            open=finite_number(field(row, "开盘", "open")),
            close=finite_number(field(row, "收盘", "close")),
            high=finite_number(field(row, "最高", "high")),
            low=finite_number(field(row, "最低", "low")),
            volume_shares=_volume_shares(row, provider),
            amount_cny=_amount_cny(row, provider),
            amplitude_pct=finite_number(field(row, "振幅", "amplitude")),
            change_pct=finite_number(
                field(row, "涨跌幅", "涨跌幅(%)", "change_pct", "pct_chg")
            ),
            change=finite_number(field(row, "涨跌额", "change")),
            turnover_pct=finite_number(
                field(row, "换手率", "换手率(%)", "turnover_rate")
            ),
        )
        for row in records
    ]
    bars.sort(key=lambda item: item.trading_date)
    dates = [item.trading_date for item in bars]
    if len(dates) != len(set(dates)):
        raise ValueError("OHLCV provider returned duplicate trading dates")
    return tuple(bars)


def frame_provider_time(frame: Any) -> datetime | None:
    return parse_provider_time(getattr(frame, "attrs", {}).get("provider_as_of"))


def frame_request_id(frame: Any) -> str | None:
    request_id = getattr(frame, "attrs", {}).get("request_id")
    return str(request_id) if request_id not in (None, "") else None


__all__ = [
    "canonical_instrument_id",
    "frame_provider_time",
    "frame_request_id",
    "map_ohlcv_frame",
    "map_quote_frame",
    "provider_units_verified",
]
