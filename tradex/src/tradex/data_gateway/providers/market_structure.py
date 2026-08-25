"""Provider mappings for whole-market breadth and sector quotes."""

from __future__ import annotations

from numbers import Integral
from typing import Any

from ..contracts import SectorQuoteV1
from .market_overview import field, finite_number, parse_provider_time
from .securities import canonical_instrument_id


_SECTOR_UNIT_PROVIDERS = {"biying", "em_push2", "tushare"}


def _count(value: Any, *, field_name: str, required: bool) -> int | None:
    if value is None or value == "":
        if required:
            raise ValueError(f"{field_name} is required")
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-negative integer")
    if isinstance(value, Integral):
        result = int(value)
    else:
        number = finite_number(value)
        if number is None or not number.is_integer():
            raise ValueError(f"{field_name} must be a non-negative integer")
        result = int(number)
    if result < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return result


def map_market_breadth_frame(frame: Any, *, provider: str) -> dict[str, Any]:
    if frame is None or not hasattr(frame, "to_dict"):
        raise RuntimeError("market breadth provider returned an unsupported payload")
    records = frame.to_dict(orient="records")
    if len(records) != 1:
        raise RuntimeError("market breadth provider must return exactly one record")
    row = records[0]
    up_count = _count(
        field(row, "上涨", "上涨家数", "上涨股数", "up_count"),
        field_name="up_count",
        required=True,
    )
    down_count = _count(
        field(row, "下跌", "下跌家数", "下跌股数", "down_count"),
        field_name="down_count",
        required=True,
    )
    flat_count = _count(
        field(row, "平盘", "平盘家数", "平盘股数", "flat_count"),
        field_name="flat_count",
        required=True,
    )
    raw_unclassified = field(
        row,
        "未分类",
        "未分类家数",
        "unclassified_count",
    )
    unclassified_count = _count(
        0 if raw_unclassified is None and provider == "em_push2ex" else raw_unclassified,
        field_name="unclassified_count",
        required=True,
    )
    limit_up_count = _count(
        field(row, "涨停", "涨停家数", "limit_up_count"),
        field_name="limit_up_count",
        required=True,
    )
    limit_down_count = _count(
        field(row, "跌停", "跌停家数", "limit_down_count"),
        field_name="limit_down_count",
        required=True,
    )
    provider_as_of = parse_provider_time(
        field(row, "更新时间", "provider_as_of")
    ) or parse_provider_time(getattr(frame, "attrs", {}).get("provider_as_of"))
    return {
        "up_count": up_count,
        "down_count": down_count,
        "flat_count": flat_count,
        "unclassified_count": unclassified_count,
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
        "total_count": up_count + down_count + flat_count + unclassified_count,
        "provider_as_of": provider_as_of,
    }


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _leader_instrument_id(row: dict[str, Any]) -> str | None:
    code = _optional_text(
        field(row, "领涨股代码", "leader_code", "leading_stock_code")
    )
    if code is None:
        return None
    try:
        return canonical_instrument_id(code)
    except ValueError:
        return None


def _sector_net_inflow_cny(row: dict[str, Any], provider: str) -> float | None:
    if provider == "tushare":
        value = finite_number(field(row, "net_amount"))
        return value * 100_000_000 if value is not None else None
    return finite_number(
        field(row, "主力净流入", "主力净流入额", "main_net_inflow")
    )


def map_sector_quote_frame(
    frame: Any,
    *,
    provider: str,
    sector_type: str,
) -> tuple[SectorQuoteV1, ...]:
    if sector_type not in {"industry", "concept"}:
        raise ValueError("sector_type must be 'industry' or 'concept'")
    if frame is None or not hasattr(frame, "to_dict"):
        raise RuntimeError("sector quote provider returned an unsupported payload")
    records = frame.to_dict(orient="records")
    if not records:
        raise RuntimeError("sector quote provider returned no records")
    frame_as_of = parse_provider_time(
        getattr(frame, "attrs", {}).get("provider_as_of")
    )

    quotes_by_key: dict[str, SectorQuoteV1] = {}
    for row in records:
        name = _optional_text(
            field(
                row,
                "板块名称",
                "板块",
                "行业名称",
                "名称",
                "name",
                "industry",
            )
        )
        if name is None:
            raise ValueError("sector quote is missing its name")
        provider_as_of = parse_provider_time(
            field(row, "更新时间", "provider_as_of", "trade_time")
        ) or frame_as_of
        quote = SectorQuoteV1(
                sector_key=f"{sector_type}:{name}",
                sector_type=sector_type,
                name=name,
                provider_sector_code=_optional_text(
                    field(
                        row,
                        "板块代码",
                        "代码",
                        "sector_code",
                        "code",
                        "ts_code",
                    )
                ),
                provider_variant=_optional_text(field(row, "source", "数据源")),
                value=finite_number(
                    field(
                        row,
                        "最新点位",
                        "最新价",
                        "value",
                        "price",
                        "close",
                        "industry_index",
                    )
                ),
                change_pct=finite_number(
                    field(
                        row,
                        "涨跌幅",
                        "涨跌幅(%)",
                        "change_pct",
                        "pct_chg",
                        "pct_change",
                    )
                ),
                amount_cny=finite_number(
                    field(row, "成交额", "成交额(元)", "amount", "turnover")
                ),
                main_net_inflow_cny=_sector_net_inflow_cny(row, provider),
                main_net_inflow_pct=finite_number(
                    field(
                        row,
                        "主力净流入-占比",
                        "主力净流入占比",
                        "main_net_inflow_pct",
                    )
                ),
                main_net_inflow_rank=_count(
                    field(row, "主力净流入排名", "main_net_inflow_rank"),
                    field_name="main_net_inflow_rank",
                    required=False,
                ),
                up_count=_count(
                    field(row, "上涨家数", "上涨股数", "up_count"),
                    field_name="up_count",
                    required=False,
                ),
                down_count=_count(
                    field(row, "下跌家数", "下跌股数", "down_count"),
                    field_name="down_count",
                    required=False,
                ),
                leader_instrument_id=_leader_instrument_id(row),
                leader_name=_optional_text(
                    field(row, "领涨股票", "领涨股", "leader_name", "lead_stock")
                ),
                leader_change_pct=finite_number(
                    field(
                        row,
                        "领涨股涨幅",
                        "leader_change_pct",
                        "pct_change_stock",
                    )
                ),
                provider_as_of=provider_as_of,
            )
        previous = quotes_by_key.get(quote.sector_key)
        if previous is None:
            quotes_by_key[quote.sector_key] = quote
            continue

        # Eastmoney's moving, sorted pagination can repeat the boundary row.
        # It is safe to collapse only an identical provider identity. The same
        # canonical name mapped to another (or missing) code stays ambiguous and
        # is rejected later by the canonical contract.
        if (
            quote.provider_sector_code is None
            or previous.provider_sector_code is None
            or quote.provider_sector_code != previous.provider_sector_code
        ):
            # Preserve the duplicate so SectorQuoteSeriesV1 reports the
            # canonical-key violation with its established error.
            quotes_by_key[f"{quote.sector_key}\0{len(quotes_by_key)}"] = quote
            continue
        if (
            quote.provider_as_of is not None
            and (
                previous.provider_as_of is None
                or quote.provider_as_of > previous.provider_as_of
            )
        ):
            quotes_by_key[quote.sector_key] = quote

    quotes = list(quotes_by_key.values())
    quotes.sort(key=lambda item: item.change_pct, reverse=True)
    return tuple(quotes)


def sector_provider_watermark(quotes: tuple[SectorQuoteV1, ...]):
    return max(
        (item.provider_as_of for item in quotes if item.provider_as_of is not None),
        default=None,
    )


def sector_units_verified(provider: str) -> bool:
    return provider in _SECTOR_UNIT_PROVIDERS


__all__ = [
    "map_market_breadth_frame",
    "map_sector_quote_frame",
    "sector_provider_watermark",
    "sector_units_verified",
]
