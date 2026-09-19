"""Bounded fresh-quote batches for a known, provider-neutral instrument universe."""
from datetime import datetime
import time

from pydantic import Field

from .contracts import AShareUniverseQuoteV1, ContractMetadata, ContractModel, QualityStatus
from .providers.market_universe import map_a_share_universe_payload


class IntradayScanQuotesV1(ContractModel):
    metadata: ContractMetadata
    requested_count: int = Field(ge=0)
    missing_instrument_ids: tuple[str, ...]
    quotes: tuple[AShareUniverseQuoteV1, ...]


def fetch_intraday_scan_quotes(instruments, *, now: datetime, router=None):
    if router is None:
        from tradex.data_sources import get_router, register_all_sources
        register_all_sources()
        router = get_router()
    requested = sorted(set(instruments))
    if len(requested) > 4000:
        raise ValueError("intraday main-board scan exceeds bounded universe")
    collected, providers, failed = {}, set(), 0
    started = time.monotonic()
    for offset in range(0, len(requested), 80):
        if time.monotonic() - started >= 45:
            break
        batch = requested[offset:offset+80]
        def validate(raw, provider):
            quotes, _, _, _ = map_a_share_universe_payload(raw, provider=provider)
            if any(q.instrument_id not in batch for q in quotes):
                raise ValueError("unexpected scan instrument")
            return quotes
        try:
            quotes, provider = router.route_validated(
                "intraday_scan_quotes", validate, symbols=[i[:6] for i in batch],
                deadline_seconds=12, provider_deadline_seconds=12,
            )
            collected.update({q.instrument_id: q for q in quotes})
            providers.add(provider)
        except Exception:
            failed += 1
            if failed >= 2:
                break
    missing = tuple(i for i in requested if i not in collected)
    return IntradayScanQuotesV1(
        metadata=ContractMetadata(contract="intraday_scan_quotes.v1",
            provider="+".join(sorted(providers)) or "unavailable", fetched_at=now,
            provider_as_of=max((q.observed_at for q in collected.values() if q.observed_at), default=None),
            quality=QualityStatus.DEGRADED if missing else QualityStatus.ACCEPTED,
            quality_flags=("per_instrument_freshness_required", *(["partial_quote_coverage"] if missing else []))),
        requested_count=len(requested), missing_instrument_ids=missing,
        quotes=tuple(collected[i] for i in sorted(collected)),
    )
