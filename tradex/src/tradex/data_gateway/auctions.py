"""Provider-neutral gateway for final opening-auction snapshots."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import (
    ContractMetadata,
    OpeningAuctionMarketV1,
    OpeningAuctionSnapshotV1,
    QualityStatus,
)
from .providers.auctions import (
    map_opening_auction_frame,
    map_opening_auction_market_frame,
    opening_auction_units_verified,
)
from .quality import assess_opening_auction_snapshot


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


def fetch_opening_auction_snapshot(
    symbol: str,
    *,
    router: Any | None = None,
    now: datetime | None = None,
) -> OpeningAuctionSnapshotV1:
    fetched_at = _now(now)

    def validate(frame: Any, provider: str) -> OpeningAuctionSnapshotV1:
        mapped = map_opening_auction_frame(
            frame,
            provider=provider,
            requested_symbol=symbol,
        )
        provider_as_of = mapped.pop("provider_as_of")
        provider_request_id = mapped.pop("provider_request_id")
        quality, flags = assess_opening_auction_snapshot(
            trading_date_missing=mapped["trading_date"] is None,
            turnover_pct=mapped["turnover_pct"],
            volume_ratio=mapped["volume_ratio"],
            provider_as_of=provider_as_of,
            provider_units_verified=opening_auction_units_verified(provider),
        )
        return OpeningAuctionSnapshotV1(
            metadata=ContractMetadata(
                contract="opening_auction_snapshot.v1",
                provider=provider,
                provider_request_id=provider_request_id,
                provider_as_of=provider_as_of,
                fetched_at=fetched_at,
                quality=quality,
                quality_flags=flags,
            ),
            **mapped,
        )

    snapshot, _provider = _router(router).route_validated(
        "auction_data",
        validate,
        code=symbol,
    )
    return snapshot


def fetch_opening_auction_market(
    trading_date: date | str,
    *,
    router: Any | None = None,
    now: datetime | None = None,
) -> OpeningAuctionMarketV1:
    """Fetch one exact full-market 09:25 auction aggregate."""

    requested_date = (
        trading_date
        if isinstance(trading_date, date)
        else date.fromisoformat(str(trading_date))
    )
    fetched_at = _now(now)

    def validate(frame: Any, provider: str) -> OpeningAuctionMarketV1:
        mapped = map_opening_auction_market_frame(
            frame,
            provider=provider,
            requested_date=requested_date,
        )
        provider_as_of = mapped.pop("provider_as_of") or datetime.combine(
            requested_date,
            time(9, 25),
            tzinfo=_SHANGHAI,
        )
        provider_request_id = mapped.pop("provider_request_id")
        excluded = int(mapped["excluded_row_count"])
        return OpeningAuctionMarketV1(
            metadata=ContractMetadata(
                contract="opening_auction_market.v1",
                provider=provider,
                provider_request_id=provider_request_id,
                provider_as_of=provider_as_of,
                fetched_at=fetched_at,
                quality=(
                    QualityStatus.ACCEPTED
                    if excluded == 0
                    else QualityStatus.DEGRADED
                ),
                quality_flags=(
                    () if excluded == 0 else ("auction_rows_excluded",)
                ),
            ),
            **mapped,
        )

    snapshot, _provider = _router(router).route_validated(
        "opening_auction_market",
        validate,
        trade_date=requested_date.isoformat(),
    )
    return snapshot


def opening_auction_to_legacy_payload(
    snapshot: OpeningAuctionSnapshotV1,
) -> dict[str, Any]:
    return {
        "code": snapshot.instrument_id[:6],
        "trading_date": (
            snapshot.trading_date.isoformat() if snapshot.trading_date else None
        ),
        "open_price": snapshot.price,
        "open_volume": snapshot.volume_shares,
        "open_amount": snapshot.amount_cny,
        "open_change_pct": snapshot.change_pct,
        "pre_close": snapshot.previous_close,
        "turnover_pct": snapshot.turnover_pct,
        "volume_ratio": snapshot.volume_ratio,
        "float_shares": snapshot.float_shares,
        "provider": snapshot.metadata.provider,
        "quality": snapshot.metadata.quality.value,
        "quality_flags": list(snapshot.metadata.quality_flags),
    }


__all__ = [
    "fetch_opening_auction_market",
    "fetch_opening_auction_snapshot",
    "opening_auction_to_legacy_payload",
]
