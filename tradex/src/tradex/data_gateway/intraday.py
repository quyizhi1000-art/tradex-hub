"""Provider-neutral gateway for the current A-share minute series."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime, time as minute_time
from threading import Condition
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractMetadata, IntradayMinuteSeriesV1
from .providers.intraday import intraday_units_verified, map_intraday_minute_frame
from .providers.securities import canonical_instrument_id
from .quality import assess_intraday_minute_series


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CACHE_TTL_SECONDS = 30.0
MAX_INTRADAY_BATCH_INSTRUMENTS = 40


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


def _map_intraday_series(
    frame: Any,
    *,
    provider: str,
    instrument_id: str,
    fetched_at: datetime,
) -> IntradayMinuteSeriesV1:
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


def fetch_intraday_minute_series(
    symbol: str,
    *,
    router: Any | None = None,
    now: datetime | None = None,
    cache: IntradayMinuteCache | None = None,
    use_cache: bool = True,
    expected_trading_date: date | None = None,
    required_minutes: tuple[minute_time, ...] = (),
) -> IntradayMinuteSeriesV1:
    """Fetch, normalize, quality-check, and cache one complete 1-minute curve."""

    instrument_id = canonical_instrument_id(symbol)
    fetched_at = _now(now)
    required = frozenset(required_minutes)

    def load() -> IntradayMinuteSeriesV1:
        def validate(frame: Any, provider: str) -> IntradayMinuteSeriesV1:
            series = _map_intraday_series(
                frame,
                provider=provider,
                instrument_id=instrument_id,
                fetched_at=fetched_at,
            )
            if (
                expected_trading_date is not None
                and series.trading_date != expected_trading_date
            ):
                raise RuntimeError(
                    f"intraday provider did not prove trading date {expected_trading_date}"
                )
            missing = required - {point.minute for point in series.points}
            if missing:
                raise RuntimeError(
                    "intraday provider omitted required exact minutes: "
                    + ", ".join(str(minute) for minute in sorted(missing)[:8])
                )
            return series

        series, _provider = _router(router).route_validated(
            "minute_data",
            validate,
            # Legacy providers accept a bare six-digit code; the canonical
            # identity remains inside the validator and returned contract.
            symbol=instrument_id[:6],
        )
        return series

    # The ordinary instrument cache does not encode exact-minute requirements.
    if not use_cache or required:
        return load()
    return (cache or _INTRADAY_CACHE).get_or_load(instrument_id, load)


def fetch_intraday_minute_series_batch(
    symbols: tuple[str, ...] | list[str],
    *,
    router: Any | None = None,
    now: datetime | None = None,
) -> dict[str, IntradayMinuteSeriesV1]:
    """Fetch and validate multiple complete minute curves in one provider call."""

    instrument_ids = tuple(sorted({canonical_instrument_id(item) for item in symbols}))
    if not instrument_ids:
        return {}
    if len(instrument_ids) > MAX_INTRADAY_BATCH_INSTRUMENTS:
        raise ValueError("intraday minute batch supports at most 40 instruments")
    fetched_at = _now(now)

    def validate(frame: Any, provider: str) -> dict[str, IntradayMinuteSeriesV1]:
        if "代码" not in frame.columns:
            raise RuntimeError("intraday minute batch is missing instrument identities")
        canonical_rows = frame["代码"].map(canonical_instrument_id)
        result: dict[str, IntradayMinuteSeriesV1] = {}
        for instrument_id in instrument_ids:
            subset = frame.loc[canonical_rows == instrument_id].copy()
            if subset.empty:
                raise RuntimeError(
                    f"intraday minute batch omitted requested instrument {instrument_id}"
                )
            subset.attrs.update(frame.attrs)
            result[instrument_id] = _map_intraday_series(
                subset,
                provider=provider,
                instrument_id=instrument_id,
                fetched_at=fetched_at,
            )
        return result

    result, _provider = _router(router).route_validated(
        "minute_data_batch",
        validate,
        symbols=instrument_ids,
    )
    return result


def fetch_intraday_minute_series_batch_partial(
    symbols: tuple[str, ...] | list[str],
    *,
    router: Any | None = None,
    now: datetime | None = None,
    allow_empty: bool = False,
) -> dict[str, IntradayMinuteSeriesV1]:
    """Fetch all exact curves present; omissions remain explicit for fallback."""

    instrument_ids = tuple(sorted({canonical_instrument_id(item) for item in symbols}))
    if not instrument_ids:
        return {}
    if len(instrument_ids) > MAX_INTRADAY_BATCH_INSTRUMENTS:
        raise ValueError("partial intraday minute batch supports at most 40 instruments")
    fetched_at = _now(now)

    def validate(frame: Any, provider: str) -> dict[str, IntradayMinuteSeriesV1]:
        if "代码" not in frame.columns:
            raise RuntimeError("partial intraday minute batch is missing identities")
        canonical_rows = frame["代码"].map(canonical_instrument_id)
        returned = tuple(dict.fromkeys(canonical_rows.tolist()))
        unexpected = set(returned) - set(instrument_ids)
        if unexpected:
            raise RuntimeError(
                "partial intraday minute batch returned unexpected instruments: "
                f"{sorted(unexpected)}"
            )
        result: dict[str, IntradayMinuteSeriesV1] = {}
        for instrument_id in returned:
            subset = frame.loc[canonical_rows == instrument_id].copy()
            subset.attrs.update(frame.attrs)
            result[instrument_id] = _map_intraday_series(
                subset,
                provider=provider,
                instrument_id=instrument_id,
                fetched_at=fetched_at,
            )
        if not result and not allow_empty:
            raise RuntimeError("partial intraday minute batch returned no exact curves")
        return result

    result, _provider = _router(router).route_validated(
        "minute_data_batch_partial",
        validate,
        symbols=instrument_ids,
        trade_date=fetched_at.astimezone(_SHANGHAI).date(),
        **({"allow_empty": True} if allow_empty else {}),
    )
    return result


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
    "MAX_INTRADAY_BATCH_INSTRUMENTS",
    "fetch_intraday_minute_series",
    "fetch_intraday_minute_series_batch",
    "fetch_intraday_minute_series_batch_partial",
    "intraday_minute_to_legacy_payload",
]
