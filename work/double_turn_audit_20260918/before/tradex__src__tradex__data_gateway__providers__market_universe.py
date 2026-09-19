"""Provider mappings for the all-A-share closing quote scan."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..contracts import AShareUniverseQuoteV1
from .market_overview import field, finite_number, parse_provider_time
from .securities import canonical_instrument_id


_VERIFIED_UNIT_PROVIDERS = {"akshare", "tushare", "tencent_http"}


def _records(payload: Any) -> list[dict[str, Any]]:
    value = payload.get("quotes") if isinstance(payload, dict) else payload
    if hasattr(value, "to_dict"):
        value = value.to_dict(orient="records")
    if not isinstance(value, (list, tuple)):
        raise RuntimeError("A-share universe provider returned an unsupported payload")
    records = [dict(item) for item in value if isinstance(item, dict)]
    if len(records) != len(value) or not records:
        raise RuntimeError("A-share universe provider returned no valid records")
    return records


def _instrument(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("A-share universe quote is missing its code")
    return canonical_instrument_id(raw)


def _name(value: Any) -> str:
    result = str(value or "").strip()
    if not result or result.lower() in {"nan", "none", "null"}:
        raise ValueError("A-share universe quote is missing its name")
    return result


def _optional_number(row: dict[str, Any], *aliases: str) -> float | None:
    return finite_number(field(row, *aliases))


def _provider_amount_cny(row: dict[str, Any], provider: str) -> float | None:
    if provider == "tencent_http":
        value = _optional_number(row, "成交额")
        return value * 10_000 if value is not None else None
    if provider == "tushare":
        return _optional_number(row, "amount")
    return _optional_number(
        row,
        "成交额",
        "成交额(元)",
        "amount_cny",
        "amount",
        "turnover",
    )


def _provider_market_cap_cny(
    row: dict[str, Any], provider: str, *, total: bool
) -> float | None:
    if provider == "tencent_http":
        value = _optional_number(row, "总市值" if total else "流通市值")
        return value * 100_000_000 if value is not None else None
    if provider == "tushare":
        value = _optional_number(row, "total_mv" if total else "circ_mv")
        return value * 10_000 if value is not None else None
    aliases = (
        ("总市值", "total_market_cap_cny", "total_market_cap")
        if total
        else ("流通市值", "float_market_cap_cny", "float_market_cap")
    )
    return _optional_number(row, *aliases)


def _amplitude_pct(row: dict[str, Any], provider: str) -> float | None:
    value = _optional_number(row, "振幅", "amplitude_pct", "amplitude")
    if value is not None or provider != "tushare":
        return value
    high = _optional_number(row, "high")
    low = _optional_number(row, "low")
    previous_close = _optional_number(row, "pre_close")
    if high is None or low is None or not previous_close:
        return None
    return (high - low) / previous_close * 100


def map_a_share_universe_payload(
    payload: Any,
    *,
    provider: str,
) -> tuple[tuple[AShareUniverseQuoteV1, ...], int, int, datetime | None]:
    """Normalize active quotes and count suspended/incomplete provider rows."""

    records = _records(payload)
    quotes: list[AShareUniverseQuoteV1] = []
    excluded = 0
    row_times: list[datetime] = []
    for row in records:
        provider_time = parse_provider_time(
            field(row, "provider_as_of", "更新时间", "数据时间", "timestamp")
        )
        if provider == "tencent_http" and provider_time is None:
            try:
                provider_time = datetime.strptime(str(field(row, "更新时间")), "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            except ValueError:
                pass
        if provider_time is not None:
            row_times.append(provider_time)
        last = _optional_number(
            row, "最新价", "现价", "price", "last", "trade", "close"
        )
        change_pct = _optional_number(
            row,
            "涨跌幅",
            "涨跌幅(%)",
            "change_pct",
            "changepercent",
            "pct_chg",
        )
        amount = _provider_amount_cny(row, provider)
        if last is None or last <= 0 or change_pct is None or amount is None or amount < 0:
            excluded += 1
            continue
        quotes.append(AShareUniverseQuoteV1(
            instrument_id=_instrument(
                field(
                    row,
                    "代码",
                    "证券代码",
                    "股票代码",
                    "code",
                    "symbol",
                    "ticker",
                    "ts_code",
                )
            ),
            name=_name(field(row, "名称", "股票名称", "name")),
            observed_at=provider_time,
            last=last,
            change_pct=change_pct,
            amount_cny=amount,
            open=_optional_number(row, "今开", "开盘", "open"),
            high=_optional_number(row, "最高", "最高价", "high"),
            low=_optional_number(row, "最低", "最低价", "low"),
            previous_close=_optional_number(
                row, "昨收", "前收盘", "pre_close", "previous_close", "settlement"
            ),
            turnover_pct=_optional_number(
                row, "换手率", "换手率(%)", "turnover_pct", "turnover_rate"
            ),
            amplitude_pct=_amplitude_pct(row, provider),
            total_market_cap_cny=_provider_market_cap_cny(
                row, provider, total=True
            ),
            float_market_cap_cny=_provider_market_cap_cny(
                row, provider, total=False
            ),
        ))
    if not quotes:
        raise RuntimeError("A-share universe provider returned no active quotes")
    quotes.sort(key=lambda item: item.instrument_id)
    ids = [item.instrument_id for item in quotes]
    if len(ids) != len(set(ids)):
        raise RuntimeError("A-share universe provider returned duplicate instruments")
    frame_time = parse_provider_time(
        (getattr(payload, "attrs", {}) or {}).get("provider_as_of")
    )
    provider_as_of = max(row_times, default=None) or frame_time
    return tuple(quotes), len(records), excluded, provider_as_of


def universe_provider_units_verified(provider: str) -> bool:
    return provider in _VERIFIED_UNIT_PROVIDERS


def universe_payload_request_id(payload: Any) -> str | None:
    attrs = getattr(payload, "attrs", {}) or {}
    value = attrs.get("request_id")
    if value in (None, "") and isinstance(payload, dict):
        value = payload.get("request_id")
    return str(value) if value not in (None, "") else None


def require_valid_universe_payload(payload: Any, provider: str) -> None:
    attrs = getattr(payload, "attrs", {}) or {}
    invalid = attrs.get("source_valid") is False
    if isinstance(payload, dict):
        invalid = invalid or payload.get("source_valid") is False
    if invalid:
        raise RuntimeError(
            f"{provider} explicitly marked its A-share universe payload invalid"
        )


__all__ = [
    "map_a_share_universe_payload",
    "require_valid_universe_payload",
    "universe_payload_request_id",
    "universe_provider_units_verified",
]
