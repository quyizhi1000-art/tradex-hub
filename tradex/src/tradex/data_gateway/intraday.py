"""Provider-neutral gateway for the current A-share minute series."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from threading import Condition
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractMetadata, IntradayMinuteSeriesV1
from .providers.intraday import intraday_units_verified, map_intraday_minute_frame
from .providers.securities import canonical_instrument_id
from .quality import assess_intraday_minute_series


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CACHE_TTL_SECONDS = 30.0


class IntradayMinuteCache:
    """Success-only TTL cache with one in-flight load per instrument."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._entries: dict[str, tuple[IntradayMinuteSeriesV1, float]] = {}
        self._loading: set[str] = set()

    def clear(self) -> None:
        with self._condition:
            self._entries.clear()

    def get_or_load(
        self,
        key: str,
        loader: Callable[[], IntradayMinuteSeriesV1],
        *,
        ttl_seconds: float = _CACHE_TTL_SECONDS,
    ) -> IntradayMinuteSeriesV1:
        if ttl_seconds <= 0:
            raise ValueError("intraday cache TTL must be positive")
        with self._condition:
            while key in self._loading:
                self._condition.wait()
            entry = self._entries.get(key)
            now = time.monotonic()
            if entry is not None and entry[1] > now:
                return entry[0]
            self._entries.pop(key, None)
            self._loading.add(key)

        try:
            loaded = loader()
            if loaded.instrument_id != key:
                raise ValueError("intraday cache key does not match loaded instrument")
        except Exception:
            with self._condition:
                self._loading.discard(key)
                self._condition.notify_all()
            raise

        with self._condition:
            self._entries[key] = (loaded, time.monotonic() + ttl_seconds)
            self._loading.discard(key)
            self._condition.notify_all()
            return loaded


_INTRADAY_CACHE = IntradayMinuteCache()


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


def fetch_intraday_minute_series(
    symbol: str,
    *,
    router: Any | None = None,
    now: datetime | None = None,
    cache: IntradayMinuteCache | None = None,
    use_cache: bool = True,
) -> IntradayMinuteSeriesV1:
    """Fetch, normalize, quality-check, and cache one complete 1-minute curve."""

    instrument_id = canonical_instrument_id(symbol)
    fetched_at = _now(now)

    def load() -> IntradayMinuteSeriesV1:
        def validate(frame: Any, provider: str) -> IntradayMinuteSeriesV1:
            mapped = map_intraday_minute_frame(
                frame,
                provider=provider,
                requested_symbol=instrument_id,
            )
            provider_as_of = mapped.pop("provider_as_of")
            provider_request_id = mapped.pop("provider_request_id")
            quality, flags = assess_intraday_minute_series(
                points=mapped["points"],
                trading_date_missing=mapped["trading_date"] is None,
                provider_as_of=provider_as_of,
                provider_units_verified=intraday_units_verified(provider),
            )
            return IntradayMinuteSeriesV1(
                metadata=ContractMetadata(
                    contract="intraday_minute_series.v1",
                    provider=provider,
                    provider_request_id=provider_request_id,
                    provider_as_of=provider_as_of,
                    fetched_at=fetched_at,
                    quality=quality,
                    quality_flags=flags,
                ),
                **mapped,
            )

        series, _provider = _router(router).route_validated(
            "minute_data",
            validate,
            # Legacy providers accept a bare six-digit code; the canonical
            # identity remains inside the validator and returned contract.
            symbol=instrument_id[:6],
        )
        return series

    if not use_cache:
        return load()
    return (cache or _INTRADAY_CACHE).get_or_load(instrument_id, load)


def _legacy_number(value: float) -> float | int:
    return int(value) if float(value).is_integer() else value


def intraday_minute_to_legacy_payload(
    series: IntradayMinuteSeriesV1,
) -> dict[str, Any]:
    """Preserve the existing MCP shape while sourcing canonical values."""

    return {
        "code": series.instrument_id[:6],
        "point_count": len(series.points),
        "points": [
            {
                "time": point.minute.strftime("%H:%M"),
                "price": point.price,
                "avg_price": point.cumulative_average_price or 0.0,
                "volume": _legacy_number(point.volume_shares),
            }
            for point in series.points
        ],
    }


__all__ = [
    "IntradayMinuteCache",
    "fetch_intraday_minute_series",
    "intraday_minute_to_legacy_payload",
]
