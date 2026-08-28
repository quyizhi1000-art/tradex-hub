"""Provider mappings for the canonical opening-auction snapshot."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from .market_overview import field, finite_number, parse_provider_time
from .securities import canonical_instrument_id, frame_request_id


_VERIFIED_UNIT_PROVIDERS = {"eltdx", "tushare"}


def _record_code(row: dict[str, Any]) -> str | None:
    raw = str(field(row, "代码", "证券代码", "ts_code", "code") or "").strip()
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", raw)
    return match.group(1) if match else None


def _trading_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip().replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError(f"invalid opening-auction trading date: {value!r}")
    return date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))


def map_opening_auction_frame(
    frame: Any,
    *,
    provider: str,
    requested_symbol: str,
) -> dict[str, Any]:
    if frame is None or not hasattr(frame, "to_dict"):
        raise RuntimeError("opening-auction provider returned an unsupported payload")
    records = frame.to_dict(orient="records")
    requested = canonical_instrument_id(requested_symbol)[:6]
    matches = [row for row in records if _record_code(row) == requested]
    if not matches:
        raise RuntimeError(
            f"opening-auction provider did not return requested symbol {requested}"
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"opening-auction provider returned duplicate symbol {requested}"
        )
    row = matches[0]
    price = finite_number(field(row, "开盘价", "price"))
    previous_close = finite_number(field(row, "昨收", "pre_close"))
    change_pct = finite_number(field(row, "开盘涨跌幅", "change_pct"))
    if change_pct is None and price is not None and previous_close not in (None, 0):
        change_pct = (price / previous_close - 1.0) * 100.0

    volume_shares = finite_number(field(row, "开盘量", "vol", "volume"))
    # eltdx helpers.auction_data exposes the 09:25 matched volume in lots;
    # its open_amount is already CNY (price * lots * 100).
    if volume_shares is not None and provider == "eltdx":
        volume_shares *= 100

    attrs = getattr(frame, "attrs", {})
    return {
        "instrument_id": canonical_instrument_id(requested_symbol),
        "trading_date": _trading_date(field(row, "交易日期", "trade_date")),
        "price": price,
        "volume_shares": volume_shares,
        "amount_cny": finite_number(field(row, "开盘额", "amount")),
        "previous_close": previous_close,
        "change_pct": change_pct,
        "turnover_pct": finite_number(field(row, "换手率", "turnover_rate")),
        "volume_ratio": finite_number(field(row, "量比", "volume_ratio")),
        "float_shares": finite_number(field(row, "流通股本", "float_share")),
        "provider_as_of": parse_provider_time(
            field(row, "更新时间", "provider_as_of")
        )
        or parse_provider_time(attrs.get("provider_as_of")),
        "provider_request_id": frame_request_id(frame),
    }


def opening_auction_units_verified(provider: str) -> bool:
    return provider in _VERIFIED_UNIT_PROVIDERS


def map_opening_auction_market_frame(
    frame: Any,
    *,
    provider: str,
    requested_date: date,
) -> dict[str, Any]:
    """Normalize and aggregate one exact full-market 09:25 provider frame."""

    if frame is None or not hasattr(frame, "to_dict"):
        raise RuntimeError("opening-auction market provider returned an unsupported payload")
    records = frame.to_dict(orient="records")
    if not records:
        raise RuntimeError("opening-auction market provider returned no rows")

    seen: set[str] = set()
    up = down = flat = excluded = 0
    total_volume = 0.0
    total_amount = 0.0
    for row in records:
        row_date = _trading_date(field(row, "交易日期", "trade_date"))
        if row_date != requested_date:
            raise RuntimeError("opening-auction market provider returned another date")
        code = _record_code(row)
        if code is None:
            excluded += 1
            continue
        instrument_id = canonical_instrument_id(code)
        if instrument_id in seen:
            raise RuntimeError("opening-auction market provider returned duplicate symbols")
        seen.add(instrument_id)
        price = finite_number(field(row, "开盘价", "price"))
        previous = finite_number(field(row, "昨收", "pre_close"))
        volume = finite_number(field(row, "开盘量", "vol", "volume"))
        amount = finite_number(field(row, "开盘额", "amount"))
        if (
            price is None
            or price <= 0
            or previous is None
            or previous <= 0
            or volume is None
            or volume < 0
            or amount is None
            or amount < 0
        ):
            excluded += 1
            continue
        if price > previous:
            up += 1
        elif price < previous:
            down += 1
        else:
            flat += 1
        total_volume += volume
        total_amount += amount

    accepted = up + down + flat
    if accepted == 0:
        raise RuntimeError("opening-auction market provider has no valid rows")
    attrs = getattr(frame, "attrs", {})
    return {
        "trading_date": requested_date,
        "instrument_count": accepted,
        "excluded_row_count": excluded,
        "provider_row_count": len(records),
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "total_volume_shares": total_volume,
        "total_amount_cny": total_amount,
        "provider_as_of": parse_provider_time(attrs.get("provider_as_of")),
        "provider_request_id": frame_request_id(frame),
    }


__all__ = [
    "map_opening_auction_frame",
    "map_opening_auction_market_frame",
    "opening_auction_units_verified",
]
