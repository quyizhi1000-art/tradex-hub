"""Provider-neutral gateway for daily A-share limit-up events."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractMetadata, LimitEventSeriesV1, QualityStatus
from .providers.limit_events import map_limit_event_frame
from .providers.securities import frame_request_id
from .quality import assess_limit_events


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


def _trade_date(value: str) -> date:
    try:
        result = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("trade_date must use YYYY-MM-DD") from exc
    return result


def fetch_limit_up_events(
    trade_date: str,
    *,
    router: Any | None = None,
    now: datetime | None = None,
) -> LimitEventSeriesV1:
    requested_date = _trade_date(trade_date)
    fetched_at = _now(now)

    def validate(frame: Any, route_provider: str) -> LimitEventSeriesV1:
        mapped = map_limit_event_frame(
            frame,
            route_provider=route_provider,
            requested_date=requested_date,
        )
        provider = mapped.pop("provider")
        provider_as_of = mapped.pop("provider_as_of")
        quality, flags = assess_limit_events(
            events=mapped["events"],
            trade_status=mapped["trade_status"],
            provider_as_of=provider_as_of,
        )
        return LimitEventSeriesV1(
            metadata=ContractMetadata(
                contract="limit_event.v1",
                provider=provider,
                provider_request_id=frame_request_id(frame),
                provider_as_of=provider_as_of,
                fetched_at=fetched_at,
                quality=quality,
                quality_flags=flags,
            ),
            **mapped,
        )

    series, _route_provider = _router(router).route_validated(
        "limit_events",
        validate,
        date=requested_date.strftime("%Y%m%d"),
    )
    return series


def _legacy_trade_status(series: LimitEventSeriesV1) -> dict[str, str]:
    return {
        "id": series.trade_status.code,
        "name": series.trade_status.label,
    }


def limit_event_series_to_legacy_records(
    series: LimitEventSeriesV1,
) -> list[dict[str, Any]]:
    data_date = series.trading_date.strftime("%Y%m%d")
    trade_status = _legacy_trade_status(series)
    return [
        {
            "代码": item.instrument_id[:6],
            "名称": item.name,
            "价格": item.price_cny,
            "涨幅%": item.change_pct,
            "涨停原因": item.reason,
            "板型": item.limit_up_type,
            "封板成功率": (
                item.seal_success_pct / 100
                if item.seal_success_pct is not None
                else None
            ),
            "炸板次数": item.open_count,
            "封单额": item.order_amount_cny,
            "连板": item.board_label,
            "首封时间": (
                item.first_sealed_at.isoformat()
                if item.first_sealed_at is not None
                else ""
            ),
            "是否回封": (
                int(item.resealed) if item.resealed is not None else None
            ),
            "数据日期": data_date,
            "交易状态": dict(trade_status),
        }
        for item in series.events
    ]


def limit_event_series_to_component_metadata(
    series: LimitEventSeriesV1,
) -> dict[str, Any]:
    metadata = series.metadata
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
        "data_date": series.trading_date.strftime("%Y%m%d"),
        "trade_status": _legacy_trade_status(series),
        "pool_total": series.pool_total,
        "unique_total": series.pool_total,
        "reason_coverage": series.reason_coverage,
        "board_count_coverage": series.board_count_coverage,
        "unknown_board_count": series.unknown_board_count,
        "valid_empty": series.valid_empty,
        "page_count": series.provider_page_count,
    }


__all__ = [
    "fetch_limit_up_events",
    "limit_event_series_to_component_metadata",
    "limit_event_series_to_legacy_records",
]
