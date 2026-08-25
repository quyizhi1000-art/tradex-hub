"""Provider-neutral gateways for late post-close review evidence."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import (
    ContractMetadata,
    DragonTigerSeriesV1,
    StockFundFlowSeriesV1,
)
from .providers.daily_review import (
    daily_review_request_id,
    daily_review_units_verified,
    frame_provider_as_of,
    map_dragon_tiger_frame,
    map_stock_fund_flow_frame,
)
from .quality import assess_dragon_tiger_day, assess_stock_fund_flows


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _router(router: Any | None) -> Any:
    if router is not None:
        return router
    from tradex.data_sources import get_router, register_all_sources

    register_all_sources()
    return get_router()


def _now(value: datetime | None) -> datetime:
    result = value or datetime.now(SHANGHAI)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("gateway now must include a timezone")
    return result.astimezone(SHANGHAI)


def _date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("trade_date must be YYYY-MM-DD or YYYYMMDD") from exc


def fetch_stock_fund_flow_day(
    trade_date: date | str,
    *,
    router: Any | None = None,
    now: datetime | None = None,
) -> StockFundFlowSeriesV1:
    requested = _date(trade_date)
    fetched_at = _now(now)

    def validate(frame: Any, provider: str) -> StockFundFlowSeriesV1:
        flows = map_stock_fund_flow_frame(
            frame,
            provider=provider,
            requested_date=requested,
        )
        provider_as_of = frame_provider_as_of(frame)
        quality, flags = assess_stock_fund_flows(
            flows=flows,
            provider_as_of=provider_as_of,
            provider_units_verified=daily_review_units_verified(provider),
        )
        return StockFundFlowSeriesV1(
            metadata=ContractMetadata(
                contract="stock_fund_flow_day.v1",
                provider=provider,
                provider_request_id=daily_review_request_id(frame),
                provider_as_of=provider_as_of,
                fetched_at=fetched_at,
                quality=quality,
                quality_flags=flags,
            ),
            trade_date=requested,
            flows=flows,
        )

    series, _provider = _router(router).route_validated(
        "stock_fund_flow_day",
        validate,
        trade_date=requested.strftime("%Y%m%d"),
    )
    return series


def fetch_dragon_tiger_day(
    trade_date: date | str,
    *,
    router: Any | None = None,
    now: datetime | None = None,
) -> DragonTigerSeriesV1:
    requested = _date(trade_date)
    fetched_at = _now(now)

    def validate(frame: Any, provider: str) -> DragonTigerSeriesV1:
        trades, valid_empty = map_dragon_tiger_frame(
            frame,
            provider=provider,
            requested_date=requested,
        )
        provider_as_of = frame_provider_as_of(frame)
        quality, flags = assess_dragon_tiger_day(
            valid_empty=valid_empty,
            provider_as_of=provider_as_of,
            provider_units_verified=daily_review_units_verified(provider),
        )
        return DragonTigerSeriesV1(
            metadata=ContractMetadata(
                contract="dragon_tiger_market_day.v1",
                provider=provider,
                provider_request_id=daily_review_request_id(frame),
                provider_as_of=provider_as_of,
                fetched_at=fetched_at,
                quality=quality,
                quality_flags=flags,
            ),
            trade_date=requested,
            valid_empty=valid_empty,
            trades=trades,
        )

    series, _provider = _router(router).route_validated(
        "dragon_tiger_market_day",
        validate,
        trade_date=requested.strftime("%Y%m%d"),
    )
    return series


__all__ = ["fetch_dragon_tiger_day", "fetch_stock_fund_flow_day"]
