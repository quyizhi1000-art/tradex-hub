"""Bounded fresh-quote batches for a known, provider-neutral instrument universe."""
from datetime import datetime, time as clock_time, timedelta
import logging
import time
from zoneinfo import ZoneInfo

from pydantic import Field

from .contracts import AShareUniverseQuoteV1, ContractMetadata, ContractModel, QualityStatus
from .providers.market_universe import map_a_share_universe_payload, require_valid_universe_payload

SHANGHAI = ZoneInfo("Asia/Shanghai")


def quote_time_is_current(stamp, now):
    """Shared freshness gate; lunch scans use the actual morning close."""
    local = now.astimezone(SHANGHAI)
    if stamp is None or stamp.tzinfo is None or stamp.astimezone(SHANGHAI).date() != local.date():
        return False
    cutoff = (local.replace(hour=11, minute=30, second=0, microsecond=0)
              if clock_time(11, 30) <= local.time().replace(tzinfo=None) < clock_time(13)
              else now)
    return cutoff - timedelta(seconds=90) <= stamp <= now


def _usable_quote(quote, now):
    return (quote_time_is_current(quote.observed_at, now) and quote.amount_cny > 0
            and all((quote.open, quote.high, quote.low, quote.previous_close))
            and 0 < quote.low <= quote.last <= quote.high)


class IntradayScanQuotesV1(ContractModel):
    metadata: ContractMetadata
    requested_count: int = Field(ge=0)
    missing_instrument_ids: tuple[str, ...]
    quotes: tuple[AShareUniverseQuoteV1, ...]
    quote_providers: dict[str, str] = Field(default_factory=dict)


def fetch_intraday_scan_quotes(instruments, *, now: datetime, router=None):
    if router is None:
        from tradex.data_sources import get_router, register_all_sources
        register_all_sources()
        router = get_router()
    requested = sorted(set(instruments))
    if len(requested) > 4000:
        raise ValueError("intraday main-board scan exceeds bounded universe")
    collected, quote_providers, failed = {}, {}, 0
    started = time.monotonic()
    observed = lambda: now + timedelta(seconds=time.monotonic() - started)
    requested_ids = set(requested)
    def validate_primary(raw, provider):
        require_valid_universe_payload(raw, provider)
        quotes, _, _, _ = map_a_share_universe_payload(raw, provider=provider)
        return tuple(q for q in quotes if q.instrument_id in requested_ids)
    if requested:
        try:
            quotes, provider = router.route_validated(
                "intraday_scan_universe", validate_primary, symbol="",
                deadline_seconds=20, provider_deadline_seconds=20,
            )
            checked_at = observed()
            for quote in quotes:
                if _usable_quote(quote, checked_at):
                    collected[quote.instrument_id] = quote
                    quote_providers[quote.instrument_id] = provider
        except Exception as exc:
            logging.getLogger(__name__).warning("intraday primary universe unavailable: %s", exc)
    pending = [instrument for instrument in requested if instrument not in collected]
    for offset in range(0, len(pending), 80):
        if time.monotonic() - started >= 45:
            break
        batch = pending[offset:offset+80]
        def validate(raw, provider, expected=tuple(batch)):
            quotes, _, _, _ = map_a_share_universe_payload(raw, provider=provider)
            if any(q.instrument_id not in expected for q in quotes):
                raise ValueError("unexpected scan instrument")
            return quotes
        try:
            quotes, provider = router.route_validated(
                "intraday_scan_quotes", validate, symbols=[i[:6] for i in batch],
                deadline_seconds=min(12, max(0.001, 45 - (time.monotonic() - started))),
                provider_deadline_seconds=12,
            )
            checked_at = observed()
            for quote in quotes:
                if _usable_quote(quote, checked_at):
                    collected[quote.instrument_id] = quote
                    quote_providers[quote.instrument_id] = provider
            failed = 0
        except Exception as exc:
            failed += 1
            logging.getLogger(__name__).warning(
                "intraday quote batch %s-%s failed: %s", batch[0], batch[-1], exc,
            )
            if failed >= 2:
                break
    missing = tuple(i for i in requested if i not in collected)
    providers = set(quote_providers.values())
    return IntradayScanQuotesV1(
        metadata=ContractMetadata(contract="intraday_scan_quotes.v1",
            provider="+".join(sorted(providers)) or "unavailable", fetched_at=now,
            provider_as_of=max((q.observed_at for q in collected.values() if q.observed_at), default=None),
            quality=QualityStatus.DEGRADED if missing else QualityStatus.ACCEPTED,
            quality_flags=("per_instrument_freshness_required", *(["partial_quote_coverage"] if missing else []))),
        requested_count=len(requested), missing_instrument_ids=missing,
        quotes=tuple(collected[i] for i in sorted(collected)),
        quote_providers=quote_providers,
    )
