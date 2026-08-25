"""Provider adapters for canonical sector intraday fund-flow curves."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..contracts import (
    ContractMetadata,
    QualityStatus,
    SectorFundFlowIntradayV1,
    SectorFundFlowMinuteV1,
)


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def map_sector_intraday_fund_flow(
    payload: Any,
    provider: str,
    *,
    sector_key: str,
    name: str,
    taxonomy: str,
    trading_date: date,
    fetched_at: datetime,
) -> SectorFundFlowIntradayV1:
    """Normalize an exact provider curve without accepting proxy metrics."""

    if fetched_at.tzinfo is None:
        raise ValueError("sector fund-flow fetched_at must include a timezone")
    if not hasattr(payload, "to_dict"):
        raise ValueError("sector fund-flow provider payload must be tabular")
    rows = payload.to_dict(orient="records")
    points: list[SectorFundFlowMinuteV1] = []
    for row in rows:
        raw_time = row.get("provider_as_of")
        raw_amount = row.get("main_net_inflow_cny")
        if raw_time is None or raw_amount is None:
            raise ValueError("sector fund-flow row lacks exact time or main-net amount")
        parsed = datetime.fromisoformat(str(raw_time))
        if parsed.tzinfo is None:
            raise ValueError("sector fund-flow provider time must include a timezone")
        points.append(
            SectorFundFlowMinuteV1(
                provider_as_of=parsed.astimezone(_SHANGHAI),
                cumulative_cny=float(raw_amount),
            )
        )
    points.sort(key=lambda item: item.provider_as_of)
    if not points:
        raise ValueError("sector fund-flow curve is empty")
    attrs = getattr(payload, "attrs", {})
    request_id = attrs.get("provider_request_id")
    return SectorFundFlowIntradayV1(
        metadata=ContractMetadata(
            contract="sector_intraday_fund_flow.v1",
            schema_version=1,
            provider=provider,
            provider_request_id=str(request_id) if request_id else None,
            provider_as_of=points[-1].provider_as_of,
            fetched_at=fetched_at,
            quality=QualityStatus.ACCEPTED,
            quality_flags=("exact_main_net_flow", "provider_timestamped"),
        ),
        sector_key=sector_key,
        name=name,
        taxonomy=taxonomy,
        trading_date=trading_date,
        points=tuple(points),
    )


__all__ = ["map_sector_intraday_fund_flow"]
