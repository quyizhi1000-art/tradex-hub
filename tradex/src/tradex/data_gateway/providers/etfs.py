"""Provider mappings for dashboard ETF quote context."""

from __future__ import annotations

import re
from typing import Any

from ..contracts import EtfQuoteV1
from .market_overview import field, finite_number, parse_provider_time


_VERIFIED_UNIT_PROVIDERS = {"astock_signals", "tushare"}


def _records(payload: Any) -> list[dict[str, Any]]:
    value = payload.get("etfs") if isinstance(payload, dict) else payload
    if hasattr(value, "to_dict"):
        value = value.to_dict(orient="records")
    if not isinstance(value, (list, tuple)):
        raise RuntimeError("ETF provider returned an unsupported payload")
    records = [dict(item) for item in value if isinstance(item, dict)]
    if len(records) != len(value) or not records:
        raise RuntimeError("ETF provider returned no valid records")
    return records


def _text(value: Any, *, field_name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"ETF quote is missing {field_name}")
    return result


def _instrument_id(value: Any) -> str:
    raw = _text(value, field_name="instrument_id").upper()
    compact = re.sub(r"[.\-_]", "", raw)
    match = re.fullmatch(r"(?:(SH|SZ))?(\d{6})(?:(SH|SZ))?", compact)
    if match is None or (match.group(1) and match.group(3)):
        raise ValueError(f"invalid ETF instrument identifier: {value!r}")
    prefix, code, suffix = match.groups()
    if not code.startswith(("1", "5")):
        raise ValueError(f"unsupported mainland ETF code: {code}")
    expected_exchange = "SZ" if code.startswith("1") else "SH"
    explicit_exchange = prefix or suffix
    if explicit_exchange and explicit_exchange != expected_exchange:
        raise ValueError(f"ETF exchange does not match its code: {value!r}")
    return f"{code}.{expected_exchange}"


def _required_number(row: dict[str, Any], field_name: str, *aliases: str) -> float:
    value = finite_number(field(row, *aliases))
    if value is None:
        raise ValueError(f"ETF quote is missing or has invalid {field_name}")
    return value


def _amount_cny(row: dict[str, Any], provider: str) -> float:
    if provider == "tushare":
        return _required_number(row, "amount", "amount")
    return _required_number(
        row,
        "amount_cny",
        "amount_cny",
        "amount",
        "成交额",
        "成交额(元)",
    )


def map_etf_quote_payload(
    payload: Any,
    *,
    provider: str,
    limit: int,
) -> tuple[EtfQuoteV1, ...]:
    quotes: list[EtfQuoteV1] = []
    frame_as_of = parse_provider_time(
        (getattr(payload, "attrs", {}) or {}).get("provider_as_of")
    )
    for row in _records(payload):
        provider_as_of = parse_provider_time(
            field(row, "provider_as_of", "更新时间", "数据时间", "trade_time")
        ) or frame_as_of
        quotes.append(
            EtfQuoteV1(
                instrument_id=_instrument_id(
                    field(
                        row,
                        "instrument_id",
                        "etf_code",
                        "代码",
                        "code",
                        "symbol",
                        "ts_code",
                    )
                ),
                name=_text(field(row, "name", "名称"), field_name="name"),
                last=_required_number(
                    row,
                    "last",
                    "price",
                    "最新价",
                    "最新",
                    "last",
                    "close",
                ),
                change_pct=_required_number(
                    row,
                    "change_pct",
                    "change_pct",
                    "涨跌幅",
                    "涨跌幅(%)",
                    "pct_chg",
                ),
                amount_cny=_amount_cny(row, provider),
                provider_as_of=provider_as_of,
                provider_variant=(
                    str(field(row, "source", "数据源") or provider).strip() or provider
                ),
            )
        )
    quotes.sort(key=lambda item: item.amount_cny, reverse=True)
    return tuple(quotes[:limit])


def etf_provider_watermark(quotes: tuple[EtfQuoteV1, ...]):
    return max(
        (item.provider_as_of for item in quotes if item.provider_as_of is not None),
        default=None,
    )


def etf_units_verified(provider: str) -> bool:
    return provider in _VERIFIED_UNIT_PROVIDERS


def etf_payload_request_id(payload: Any) -> str | None:
    attrs = getattr(payload, "attrs", {}) or {}
    value = attrs.get("request_id")
    if value in (None, "") and isinstance(payload, dict):
        value = payload.get("request_id")
    return str(value) if value not in (None, "") else None


def require_valid_etf_payload(payload: Any, provider: str) -> None:
    attrs = getattr(payload, "attrs", {}) or {}
    invalid = attrs.get("source_valid") is False
    if isinstance(payload, dict):
        invalid = invalid or payload.get("source_valid") is False
    if invalid:
        raise RuntimeError(f"{provider} explicitly marked its ETF payload invalid")


__all__ = [
    "etf_payload_request_id",
    "etf_provider_watermark",
    "etf_units_verified",
    "map_etf_quote_payload",
    "require_valid_etf_payload",
]
