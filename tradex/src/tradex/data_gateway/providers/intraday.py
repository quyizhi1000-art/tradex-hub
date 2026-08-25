"""Provider mappings for the canonical current-session minute series."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from ..contracts import IntradayMinutePointV1
from .market_overview import field, finite_number, parse_provider_time
from .securities import canonical_instrument_id, frame_request_id


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_VERIFIED_UNIT_PROVIDERS = {"eltdx", "tushare"}


def _record_code(row: dict[str, Any]) -> str | None:
    raw = str(field(row, "代码", "证券代码", "ts_code", "code") or "").strip()
    if not raw:
        return None
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", raw)
    return match.group(1) if match else ""


def _date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip().replace("/", "-")
    if len(raw) == 8 and raw.isdigit():
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"invalid intraday trading date: {value!r}") from exc


def _point_time(value: Any) -> tuple[date | None, time]:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        parsed = None
        try:
            candidate = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if "-" in raw[:10] or raw[:8].isdigit():
                parsed = candidate
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
            for fmt in ("%H:%M:%S", "%H:%M"):
                try:
                    minute = datetime.strptime(raw, fmt).time()
                    return None, minute.replace(second=0, microsecond=0)
                except ValueError:
                    continue
            raise ValueError(f"invalid intraday minute timestamp: {value!r}")

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_SHANGHAI)
    return parsed.date(), parsed.time().replace(tzinfo=None, second=0, microsecond=0)


def _volume_shares(
    row: dict[str, Any], *, provider: str, attrs: dict[str, Any]
) -> float | None:
    value = finite_number(field(row, "成交量", "volume", "vol"))
    if value is None:
        return None
    unit = str(attrs.get("volume_unit") or "").strip().lower()
    if unit == "lots" or (not unit and provider == "eltdx"):
        return value * 100
    if unit == "shares" or (not unit and provider == "tushare"):
        return value
    raise ValueError(f"unverified intraday volume unit for provider {provider}")


def map_intraday_minute_frame(
    frame: Any,
    *,
    provider: str,
    requested_symbol: str,
) -> dict[str, Any]:
    """Normalize one provider frame without inventing a missing trading date."""

    if frame is None or not hasattr(frame, "to_dict"):
        raise RuntimeError("intraday provider returned an unsupported payload")
    records = frame.to_dict(orient="records")
    if not records:
        raise RuntimeError("intraday provider returned no points")

    requested = canonical_instrument_id(requested_symbol)
    requested_code = requested[:6]
    record_codes = [_record_code(row) for row in records]
    if any(code == "" for code in record_codes):
        raise RuntimeError("intraday provider returned an invalid instrument code")
    explicit_codes = {code for code in record_codes if code is not None}
    if explicit_codes and explicit_codes != {requested_code}:
        raise RuntimeError(
            f"intraday provider did not return only requested symbol {requested_code}"
        )
    if provider == "tushare" and not explicit_codes:
        raise RuntimeError("TuShare intraday response omitted instrument identity")

    attrs = dict(getattr(frame, "attrs", {}))
    frequency = attrs.get("frequency_minutes")
    if frequency not in (None, 1, "1"):
        raise RuntimeError("intraday provider returned a non-1-minute frequency")

    raw_points: list[dict[str, Any]] = []
    row_dates: set[date] = set()
    for row in records:
        row_date, minute = _point_time(field(row, "时间", "time", "datetime"))
        if row_date is not None:
            row_dates.add(row_date)
        raw_points.append(
            {
                "minute": minute,
                "price": finite_number(field(row, "收盘", "价格", "close", "price")),
                "provider_average": finite_number(
                    field(row, "均价", "avg_price", "average_price")
                ),
                "volume_shares": _volume_shares(
                    row, provider=provider, attrs=attrs
                ),
                "amount_cny": finite_number(
                    field(row, "成交额", "amount", "amount_cny")
                ),
                "open": finite_number(field(row, "开盘", "open")),
                "high": finite_number(field(row, "最高", "high")),
                "low": finite_number(field(row, "最低", "low")),
            }
        )

    if len(row_dates) > 1:
        raise RuntimeError("intraday provider returned multiple trading dates")
    attrs_date = _date(attrs.get("trading_date"))
    row_date = next(iter(row_dates)) if row_dates else None
    if attrs_date is not None and row_date is not None and attrs_date != row_date:
        raise RuntimeError("intraday provider trading-date metadata conflicts with rows")
    trading_date = row_date or attrs_date

    raw_points.sort(key=lambda item: item["minute"])
    times = [item["minute"] for item in raw_points]
    if len(times) != len(set(times)):
        raise RuntimeError("intraday provider returned duplicate minute points")

    derive_cumulative_average = provider == "tushare"
    cumulative_amount = 0.0
    cumulative_volume = 0.0
    points: list[IntradayMinutePointV1] = []
    for item in raw_points:
        amount = item["amount_cny"]
        volume = item["volume_shares"]
        average = item["provider_average"]
        if derive_cumulative_average:
            if amount is None or volume is None:
                raise RuntimeError(
                    "TuShare intraday data cannot derive cumulative average price"
                )
            cumulative_amount += amount
            cumulative_volume += volume
            average = (
                cumulative_amount / cumulative_volume
                if cumulative_volume > 0
                else None
            )
        points.append(
            IntradayMinutePointV1(
                minute=item["minute"],
                price=item["price"],
                cumulative_average_price=average,
                volume_shares=volume,
                amount_cny=amount,
                open=item["open"],
                high=item["high"],
                low=item["low"],
            )
        )

    provider_as_of = parse_provider_time(attrs.get("provider_as_of"))
    if provider_as_of is None and trading_date is not None:
        provider_as_of = datetime.combine(
            trading_date, points[-1].minute, tzinfo=_SHANGHAI
        )
    return {
        "instrument_id": requested,
        "trading_date": trading_date,
        "points": tuple(points),
        "provider_as_of": provider_as_of,
        "provider_request_id": frame_request_id(frame),
    }


def intraday_units_verified(provider: str) -> bool:
    return provider in _VERIFIED_UNIT_PROVIDERS


__all__ = ["intraday_units_verified", "map_intraday_minute_frame"]
