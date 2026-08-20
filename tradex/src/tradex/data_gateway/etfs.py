"""Provider-neutral gateway for dashboard ETF quote context."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractMetadata, EtfQuoteSeriesV1
from .providers.etfs import (
    etf_payload_request_id,
    etf_provider_watermark,
    etf_units_verified,
    map_etf_quote_payload,
    require_valid_etf_payload,
)
from .quality import assess_etf_quotes


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


def fetch_etf_quotes(
    *,
    limit: int = 5000,
    router: Any | None = None,
    now: datetime | None = None,
) -> EtfQuoteSeriesV1:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
        raise ValueError("ETF quote limit must be an integer between 1 and 5000")
    fetched_at = _now(now)

    def validate(payload: Any, provider: str) -> EtfQuoteSeriesV1:
        require_valid_etf_payload(payload, provider)
        quotes = map_etf_quote_payload(payload, provider=provider, limit=limit)
        provider_as_of = etf_provider_watermark(quotes)
        quality, flags = assess_etf_quotes(
            quotes=quotes,
            provider_as_of=provider_as_of,
            provider_units_verified=etf_units_verified(provider),
        )
        return EtfQuoteSeriesV1(
            metadata=ContractMetadata(
                contract="etf_quote.v1",
                provider=provider,
                provider_request_id=etf_payload_request_id(payload),
                provider_as_of=provider_as_of,
                fetched_at=fetched_at,
                quality=quality,
                quality_flags=flags,
            ),
            requested_limit=limit,
            quotes=quotes,
        )

    series, _provider = _router(router).route_validated(
        "etf_data",
        validate,
        symbol="",
        top_n=limit,
        sort_by="成交额",
    )
    return series


def etf_quotes_to_legacy_records(series: EtfQuoteSeriesV1) -> list[dict[str, Any]]:
    return [
        {
            "etf_code": item.instrument_id[:6],
            "name": item.name,
            "price": item.last,
            "change_pct": item.change_pct,
            "amount": item.amount_cny,
            "provider_as_of": (
                item.provider_as_of.isoformat(timespec="seconds")
                if item.provider_as_of
                else None
            ),
            "source": item.provider_variant,
        }
        for item in series.quotes
    ]


__all__ = ["etf_quotes_to_legacy_records", "fetch_etf_quotes"]
