"""Provider-neutral gateway for one full A-share closing quote scan."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import AShareUniverseSnapshotV1, ContractMetadata
from .providers.market_universe import (
    map_a_share_universe_payload,
    require_valid_universe_payload,
    universe_payload_request_id,
    universe_provider_units_verified,
)
from .quality import DataQualityError, assess_a_share_universe


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


def fetch_a_share_universe_snapshot(
    *,
    require_market_cap: bool = False,
    router: Any | None = None,
    now: datetime | None = None,
) -> AShareUniverseSnapshotV1:
    """Route, normalize, and validate the provider's active A-share universe."""

    fetched_at = _now(now)

    def validate(payload: Any, provider: str) -> AShareUniverseSnapshotV1:
        require_valid_universe_payload(payload, provider)
        quotes, row_count, excluded_count, provider_as_of = (
            map_a_share_universe_payload(payload, provider=provider)
        )
        if require_market_cap and any(
            item.total_market_cap_cny is None for item in quotes
        ):
            raise DataQualityError("A 股全市场市值筛选要求完整的总市值字段")
        if (
            provider_as_of is not None
            and provider_as_of.astimezone(SHANGHAI).date() != fetched_at.date()
        ):
            raise DataQualityError("A 股全市场行情不是当日快照")
        quality, flags = assess_a_share_universe(
            quotes=quotes,
            provider_row_count=row_count,
            excluded_row_count=excluded_count,
            provider_as_of=provider_as_of,
            provider_units_verified=universe_provider_units_verified(provider),
        )
        return AShareUniverseSnapshotV1(
            metadata=ContractMetadata(
                contract="a_share_universe_quote.v1",
                provider=provider,
                provider_request_id=universe_payload_request_id(payload),
                provider_as_of=provider_as_of,
                fetched_at=fetched_at,
                quality=quality,
                quality_flags=flags,
            ),
            provider_row_count=row_count,
            active_quote_count=len(quotes),
            excluded_row_count=excluded_count,
            quotes=quotes,
        )

    snapshot, _provider = _router(router).route_validated(
        "market_universe",
        validate,
        symbol="",
    )
    return snapshot


def a_share_universe_to_legacy_records(
    snapshot: AShareUniverseSnapshotV1,
) -> list[dict[str, Any]]:
    return [
        {
            "代码": item.instrument_id[:6],
            "名称": item.name,
            "最新价": item.last,
            "涨跌幅": item.change_pct,
            "成交额": item.amount_cny,
            "今开": item.open,
            "最高": item.high,
            "最低": item.low,
            "昨收": item.previous_close,
            "换手率": item.turnover_pct,
            "振幅": item.amplitude_pct,
            "总市值": item.total_market_cap_cny,
            "流通市值": item.float_market_cap_cny,
        }
        for item in snapshot.quotes
    ]


__all__ = [
    "a_share_universe_to_legacy_records",
    "fetch_a_share_universe_snapshot",
]
