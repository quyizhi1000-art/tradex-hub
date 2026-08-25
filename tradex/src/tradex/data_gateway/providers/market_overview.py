"""Provider mappings for the canonical market-overview contract."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from ..contracts import IndexQuoteV1, ParticipationIndexV1


_INDEX_SPECS = (
    {
        "provider_code": "sh000001",
        "instrument_id": "000001.SH",
        "name": "上证指数",
        "aliases": ("上证指数", "上证综合指数"),
    },
    {
        "provider_code": "sz399001",
        "instrument_id": "399001.SZ",
        "name": "深证成指",
        "aliases": ("深证成指", "深证指数"),
    },
    {
        "provider_code": "sh000300",
        "instrument_id": "000300.SH",
        "name": "沪深300",
        "aliases": ("沪深300", "沪深300指数"),
    },
    {
        # The same CSI 1000 series is distributed as 000852/399852 by
        # different quote vendors.  Canonical consumers use 000852.SH while
        # the mapper accepts either provider code through the name alias.
        "provider_code": "sz399852",
        "provider_codes": ("sz399852", "sh000852"),
        "instrument_id": "000852.SH",
        "name": "中证1000",
        "aliases": ("中证1000", "中证1000指数"),
    },
    {
        "provider_code": "sz399006",
        "instrument_id": "399006.SZ",
        "name": "创业板指",
        "aliases": ("创业板指", "创业板指数"),
    },
)


def finite_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def field(row: dict[str, Any], *names: str) -> Any:
    normalised = {
        str(key).strip().lower().replace(" ", ""): value
        for key, value in row.items()
    }
    for name in names:
        key = name.strip().lower().replace(" ", "")
        if key in normalised:
            return normalised[key]
    return None


def parse_provider_time(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _matches_index(row: dict[str, Any], spec: dict[str, Any]) -> bool:
    raw_code = str(field(row, "代码", "指数代码", "code", "symbol") or "").lower()
    compact_code = raw_code.replace(".", "").replace("_", "")
    expected_codes = spec.get("provider_codes", (spec["provider_code"],))
    code_aliases = {
        alias
        for expected in expected_codes
        for alias in (expected, expected[2:], expected[2:] + expected[:2])
    }
    if compact_code in code_aliases:
        return True
    raw_name = str(field(row, "指数名称", "名称", "name") or "")
    return any(alias in raw_name for alias in spec["aliases"])


def map_indices(records: list[dict[str, Any]], provider: str) -> tuple[IndexQuoteV1, ...]:
    indices: list[IndexQuoteV1] = []
    for spec in _INDEX_SPECS:
        row = next((item for item in records if _matches_index(item, spec)), None)
        if row is None:
            indices.append(
                IndexQuoteV1(
                    instrument_id=spec["instrument_id"],
                    name=spec["name"],
                    available=False,
                )
            )
            continue

        value = finite_number(
            field(row, "最新点位", "最新价", "最新", "现价", "price", "close", "收盘")
        )
        change = finite_number(field(row, "涨跌额", "涨跌", "change"))
        previous_close = finite_number(
            field(row, "昨收", "昨日收盘", "前收盘", "previous_close", "pre_close")
        )
        if previous_close is None and value is not None and change is not None:
            previous_close = value - change

        amount = finite_number(field(row, "成交额(元)", "成交额", "amount", "turnover"))
        if amount is not None and provider == "tencent_http":
            amount *= 10_000

        indices.append(
            IndexQuoteV1(
                instrument_id=spec["instrument_id"],
                name=spec["name"],
                available=value is not None,
                value=value,
                change=change,
                change_pct=finite_number(
                    field(row, "涨跌幅", "涨跌幅(%)", "change_pct", "pct_chg")
                ),
                previous_close=previous_close,
                open=finite_number(field(row, "今开", "开盘", "open")),
                high=finite_number(field(row, "最高", "最高价", "high")),
                low=finite_number(field(row, "最低", "最低价", "low")),
                amount_cny=amount,
                provider_as_of=parse_provider_time(
                    field(row, "更新时间", "provider_as_of")
                ),
            )
        )
    return tuple(indices)


def _canonical_index_id(row: dict[str, Any]) -> str | None:
    raw = str(field(row, "代码", "指数代码", "code", "symbol") or "").strip().upper()
    compact = raw.replace(".", "").replace("_", "")
    if compact.startswith("SH") and len(compact) == 8:
        return f"{compact[2:]}.SH"
    if compact.startswith("SZ") and len(compact) == 8:
        return f"{compact[2:]}.SZ"
    if compact.endswith("SH") and len(compact) == 8:
        return f"{compact[:6]}.SH"
    if compact.endswith("SZ") and len(compact) == 8:
        return f"{compact[:6]}.SZ"
    return None


def map_participation_indices(
    records: list[dict[str, Any]],
) -> tuple[ParticipationIndexV1, ...]:
    result: list[ParticipationIndexV1] = []
    for row in records:
        change_pct = finite_number(
            field(row, "涨跌幅", "涨跌幅(%)", "change_pct", "pct_chg")
        )
        name = field(row, "指数名称", "名称", "name")
        instrument_id = _canonical_index_id(row)
        if change_pct is None or name is None or instrument_id is None:
            continue
        result.append(
            ParticipationIndexV1(
                instrument_id=instrument_id,
                name=str(name),
                change_pct=change_pct,
                provider_as_of=parse_provider_time(
                    field(row, "更新时间", "provider_as_of")
                ),
            )
        )
    return tuple(result)


def provider_watermark(records: list[dict[str, Any]]) -> datetime | None:
    timestamps = [
        parsed
        for row in records
        if (parsed := parse_provider_time(field(row, "更新时间", "provider_as_of")))
        is not None
    ]
    return max(timestamps, default=None)


__all__ = [
    "field",
    "finite_number",
    "map_indices",
    "map_participation_indices",
    "parse_provider_time",
    "provider_watermark",
]
