"""Provider payload mappings for dashboard leadership features."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

from ..contracts import BoardLeaderV1, LeaderQuoteV1, StockSectorProfileV1
from .market_overview import field, finite_number, parse_provider_time
from .securities import canonical_instrument_id


def _records(frame: Any, label: str) -> list[dict[str, Any]]:
    if frame is None or not hasattr(frame, "to_dict"):
        raise RuntimeError(f"{label} provider returned an unsupported payload")
    records = frame.to_dict(orient="records")
    if not records:
        raise RuntimeError(f"{label} provider returned no rows")
    return records


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        if bool(value != value):
            return None
    except (TypeError, ValueError):
        pass
    result = str(value).strip()
    if result.lower() in {"nan", "nat", "<na>"}:
        return None
    return result or None


def _required_text(value: Any, label: str) -> str:
    result = _optional_text(value)
    if result is None:
        raise ValueError(f"{label} is required")
    return result


def _record_code(row: dict[str, Any]) -> str:
    raw = _required_text(
        field(row, "代码", "证券代码", "code", "symbol", "ticker"),
        "instrument code",
    )
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", raw)
    if match is None:
        raise ValueError(f"invalid A-share instrument code: {raw!r}")
    return match.group(1)


def _row_provider_time(row: dict[str, Any], frame: Any) -> datetime | None:
    return parse_provider_time(
        field(row, "provider_as_of", "更新时间", "timestamp")
    ) or parse_provider_time(getattr(frame, "attrs", {}).get("provider_as_of"))


def payload_provider(
    frame: Any,
    route_provider: str,
    records: Iterable[dict[str, Any]],
) -> str:
    attrs_source = _optional_text(getattr(frame, "attrs", {}).get("source"))
    if attrs_source:
        return attrs_source
    for row in records:
        row_source = _optional_text(field(row, "source", "provider"))
        if row_source:
            return row_source
    return route_provider


def provider_watermark(items: Iterable[Any]) -> datetime | None:
    timestamps = [
        value
        for item in items
        if (value := getattr(item, "provider_as_of", None)) is not None
    ]
    return max(timestamps, default=None)


def map_leader_quote_frame(
    frame: Any,
    *,
    requested_codes: tuple[str, ...],
) -> tuple[LeaderQuoteV1, ...]:
    records = _records(frame, "leader quote")
    requested = set(requested_codes)
    by_code: dict[str, dict[str, Any]] = {}
    for row in records:
        code = _record_code(row)
        if code not in requested:
            raise ValueError(f"leader quote returned unrequested instrument {code}")
        if code in by_code:
            raise ValueError(f"leader quote returned duplicate instrument {code}")
        by_code[code] = row

    quotes: list[LeaderQuoteV1] = []
    for code in requested_codes:
        row = by_code.get(code)
        if row is None:
            continue
        quotes.append(
            LeaderQuoteV1(
                instrument_id=canonical_instrument_id(code),
                name=_required_text(field(row, "名称", "name"), "leader name"),
                last=finite_number(field(row, "最新价", "price", "last")),
                change_pct=finite_number(
                    field(row, "涨跌幅", "涨跌幅(%)", "change_pct", "pct_chg")
                ),
                provider_as_of=_row_provider_time(row, frame),
            )
        )
    return tuple(quotes)


def _concept_tags(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        candidates = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        try:
            if value != value:
                return ()
        except (TypeError, ValueError):
            pass
        raise ValueError("profile concept tags must be a string or sequence")
    result: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        tag = _optional_text(item)
        if tag and tag not in seen:
            seen.add(tag)
            result.append(tag)
    return tuple(result)


def map_stock_sector_profile_frame(
    frame: Any,
    *,
    requested_codes: tuple[str, ...],
    route_provider: str,
) -> tuple[StockSectorProfileV1, ...]:
    records = _records(frame, "stock sector profile")
    provider = payload_provider(frame, route_provider, records)
    by_code: dict[str, dict[str, Any]] = {}
    for row in records:
        code = _record_code(row)
        if code in by_code:
            raise ValueError(f"stock sector profile returned duplicate instrument {code}")
        by_code[code] = row
    if set(by_code) != set(requested_codes):
        missing = sorted(set(requested_codes) - set(by_code))
        unexpected = sorted(set(by_code) - set(requested_codes))
        raise ValueError(
            "stock sector profile code set does not match request: "
            f"missing={missing}, unexpected={unexpected}"
        )

    profiles: list[StockSectorProfileV1] = []
    for code in requested_codes:
        row = by_code[code]
        provider_as_of = _row_provider_time(row, frame)
        if provider_as_of is None:
            raise ValueError("股票行业 profile 存在缺失或错误交易日")
        profiles.append(
            StockSectorProfileV1(
                instrument_id=canonical_instrument_id(code),
                name=_required_text(field(row, "名称", "name"), "profile name"),
                industry=_required_text(
                    field(row, "行业", "industry"), "profile industry"
                ),
                region=_optional_text(field(row, "地域", "region")),
                concept_tags=_concept_tags(
                    field(row, "概念标签", "concept_tags", "concepts")
                ),
                provider_as_of=provider_as_of,
                provider_variant=_optional_text(field(row, "source", "provider"))
                or provider,
            )
        )
    return tuple(profiles)


def map_board_leader_frame(
    frame: Any,
    *,
    board_code: str,
    route_provider: str,
) -> tuple[BoardLeaderV1, ...]:
    records = _records(frame, "board leader")
    attrs_board = _optional_text(getattr(frame, "attrs", {}).get("board_code"))
    if attrs_board is not None and attrs_board.upper() != board_code:
        raise ValueError(
            f"board leader payload is for {attrs_board}, expected {board_code}"
        )
    provider = payload_provider(frame, route_provider, records)
    leaders = [
        BoardLeaderV1(
            instrument_id=canonical_instrument_id(_record_code(row)),
            name=_required_text(field(row, "name", "名称"), "board leader name"),
            price=finite_number(field(row, "price", "最新价")),
            change_pct=finite_number(field(row, "change_pct", "涨跌幅")),
            amount_cny=finite_number(field(row, "amount", "成交额")),
            turnover_pct=finite_number(field(row, "turnover", "换手率")),
            main_net_inflow_cny=finite_number(
                field(row, "flow_amount", "主力净流入")
            ),
            main_net_inflow_pct=finite_number(
                field(row, "flow_ratio", "主力净流入-占比")
            ),
            provider_as_of=_row_provider_time(row, frame),
            provider_variant=_optional_text(field(row, "source", "provider"))
            or provider,
        )
        for row in records
    ]
    return tuple(leaders)


__all__ = [
    "map_board_leader_frame",
    "map_leader_quote_frame",
    "map_stock_sector_profile_frame",
    "payload_provider",
    "provider_watermark",
]
