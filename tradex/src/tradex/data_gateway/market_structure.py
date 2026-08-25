"""Gateway for whole-market breadth and industry/concept quotes."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import (
    ContractMetadata,
    MarketBreadthV1,
    QualityStatus,
    SectorQuoteSeriesV1,
)
from .providers.market_structure import (
    map_market_breadth_frame,
    map_sector_quote_frame,
    sector_provider_watermark,
    sector_units_verified,
)
from .providers.securities import frame_request_id
from .quality import assess_market_breadth, assess_sector_quotes


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _router(router: Any | None) -> Any:
    if router is not None:
        return router
    from tradex.data_sources import get_router, register_all_sources

    register_all_sources()
    return get_router()


def _now(value: datetime | None) -> datetime:
    result = value or datetime.now(_SHANGHAI)
    if result.tzinfo is None:
        raise ValueError("gateway now must include a timezone")
    return result


def _require_valid_source(frame: Any, provider: str) -> None:
    if getattr(frame, "attrs", {}).get("source_valid") is False:
        raise RuntimeError(f"{provider} explicitly marked its payload invalid")


def fetch_market_breadth_snapshot(
    *,
    router: Any | None = None,
    now: datetime | None = None,
) -> MarketBreadthV1:
    fetched_at = _now(now)

    def validate(frame: Any, provider: str) -> MarketBreadthV1:
        _require_valid_source(frame, provider)
        mapped = map_market_breadth_frame(frame, provider=provider)
        provider_as_of = mapped.pop("provider_as_of")
        quality, flags = assess_market_breadth(
            provider_as_of=provider_as_of,
            unclassified_count=mapped["unclassified_count"],
            universe_verified=provider == "ths_fuyao",
        )
        return MarketBreadthV1(
            metadata=ContractMetadata(
                contract="market_breadth.v1",
                provider=provider,
                provider_request_id=frame_request_id(frame),
                provider_as_of=provider_as_of,
                fetched_at=fetched_at,
                quality=quality,
                quality_flags=flags,
            ),
            **mapped,
        )

    snapshot, _provider = _router(router).route_validated(
        "market_breadth", validate
    )
    return snapshot


def fetch_sector_quotes(
    sector_type: str,
    *,
    exact: Any = None,
    router: Any | None = None,
    now: datetime | None = None,
) -> SectorQuoteSeriesV1:
    fetched_at = _now(now)

    def validate(frame: Any, provider: str) -> SectorQuoteSeriesV1:
        _require_valid_source(frame, provider)
        quotes = map_sector_quote_frame(
            frame,
            provider=provider,
            sector_type=sector_type,
        )
        provider_as_of = sector_provider_watermark(quotes)
        quality, flags = assess_sector_quotes(
            quotes=quotes,
            provider_as_of=provider_as_of,
            provider_units_verified=sector_units_verified(provider),
        )
        return SectorQuoteSeriesV1(
            metadata=ContractMetadata(
                contract="sector_quote.v1",
                provider=provider,
                provider_request_id=frame_request_id(frame),
                provider_as_of=provider_as_of,
                fetched_at=fetched_at,
                quality=quality,
                quality_flags=flags,
            ),
            sector_type=sector_type,
            quotes=quotes,
        )

    series, _provider = _router(router).route_validated(
        "industry_quotes",
        validate,
        board_type=sector_type,
        exact=exact,
    )
    return series


def market_breadth_to_legacy_records(snapshot: MarketBreadthV1) -> list[dict[str, Any]]:
    return [
        {
            "上涨": snapshot.up_count,
            "下跌": snapshot.down_count,
            "平盘": snapshot.flat_count,
            "未分类": snapshot.unclassified_count,
            "涨停": snapshot.limit_up_count,
            "跌停": snapshot.limit_down_count,
        }
    ]


def sector_quotes_to_legacy_records(
    series: SectorQuoteSeriesV1,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in series.quotes:
        leader_code = (
            item.leader_instrument_id[:6] if item.leader_instrument_id else None
        )
        leader_market = (
            item.leader_instrument_id.split(".", 1)[1]
            if item.leader_instrument_id
            else None
        )
        records.append(
            {
                "板块代码": item.provider_sector_code,
                "最新点位": item.value,
                "板块名称": item.name,
                "涨跌幅": item.change_pct,
                "成交额": item.amount_cny,
                "主力净流入": item.main_net_inflow_cny,
                "主力净流入-占比": item.main_net_inflow_pct,
                "主力净流入排名": item.main_net_inflow_rank,
                "上涨家数": item.up_count,
                "下跌家数": item.down_count,
                "领涨股代码": leader_code,
                "领涨股市场": leader_market,
                "leader_instrument_id": item.leader_instrument_id,
                "领涨股票": item.leader_name,
                "领涨股涨幅": item.leader_change_pct,
                "更新时间": (
                    item.provider_as_of.isoformat(timespec="seconds")
                    if item.provider_as_of
                    else None
                ),
                "source": item.provider_variant or series.metadata.provider,
            }
        )
    return records


def metadata_to_component_status(metadata: ContractMetadata) -> dict[str, Any]:
    """Expose contract quality through the existing dashboard cache status."""

    return {
        "contract": metadata.contract,
        "schema_version": metadata.schema_version,
        "provider_request_id": metadata.provider_request_id,
        "provider_as_of": (
            metadata.provider_as_of.isoformat(timespec="seconds")
            if metadata.provider_as_of
            else None
        ),
        "quality": metadata.quality.value,
        "quality_flags": list(metadata.quality_flags),
        "partial": metadata.quality is QualityStatus.DEGRADED,
        "source_valid": metadata.quality is not QualityStatus.REJECTED,
    }


__all__ = [
    "fetch_market_breadth_snapshot",
    "fetch_sector_quotes",
    "market_breadth_to_legacy_records",
    "metadata_to_component_status",
    "sector_quotes_to_legacy_records",
]
