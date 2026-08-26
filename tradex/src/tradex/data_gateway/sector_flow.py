"""Provider-neutral sector intraday main-net-flow gateway."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime
from threading import Condition
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import SectorFundFlowIntradayV1
from .providers.sector_flow import map_sector_intraday_fund_flow
from .sector_flow_store import SectorFundFlowStore


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_PROVIDER_BOARD_CODE = re.compile(r"^BK\d+$")
_BackfillKey = tuple[date, str, str]


class SectorFundFlowBackfillCache:
    """Own success-only, per-session history and its single-flight loading."""

    def __init__(
        self,
        *,
        store: SectorFundFlowStore | None = None,
        persistent: bool = False,
        refresh_min_interval_seconds: float = 300.0,
    ) -> None:
        if refresh_min_interval_seconds < 0:
            raise ValueError("refresh_min_interval_seconds cannot be negative")
        self._condition = Condition()
        self._entries: dict[_BackfillKey, SectorFundFlowIntradayV1] = {}
        self._loading: set[_BackfillKey] = set()
        self._last_loaded_at: dict[_BackfillKey, datetime] = {}
        self._store = store
        self._persistent = bool(persistent)
        self.refresh_min_interval_seconds = float(refresh_min_interval_seconds)

    def _get_store(self) -> SectorFundFlowStore | None:
        with self._condition:
            if self._store is None and self._persistent:
                self._store = SectorFundFlowStore()
            return self._store

    def get_cached(self, key: _BackfillKey) -> SectorFundFlowIntradayV1 | None:
        with self._condition:
            cached = self._entries.get(key)
        if cached is not None:
            return cached
        store = self._get_store()
        persisted = None if store is None else store.get_best(key[0], key[1])
        if persisted is None:
            return None
        with self._condition:
            self._entries[key] = persisted
        return persisted

    def get_or_load(
        self,
        key: _BackfillKey,
        loader: Callable[[], SectorFundFlowIntradayV1],
        *,
        refresh_existing: bool = False,
        refreshed_at: datetime | None = None,
    ) -> SectorFundFlowIntradayV1:
        effective_refresh_at = refreshed_at or datetime.now(_SHANGHAI)
        if effective_refresh_at.tzinfo is None:
            raise ValueError("sector backfill refreshed_at must include a timezone")
        effective_refresh_at = effective_refresh_at.astimezone(_SHANGHAI)
        stale_success: SectorFundFlowIntradayV1 | None = None
        with self._condition:
            while key in self._loading:
                self._condition.wait()
            cached = self._entries.get(key)
            if cached is None:
                store = self._get_store()
                cached = None if store is None else store.get_best(key[0], key[1])
                if cached is not None:
                    self._entries[key] = cached
            if cached is not None and not refresh_existing:
                return cached
            last_loaded = self._last_loaded_at.get(key)
            if (
                cached is not None
                and last_loaded is not None
                and (
                    effective_refresh_at - last_loaded
                ).total_seconds() < self.refresh_min_interval_seconds
            ):
                return cached
            stale_success = cached
            self._loading.add(key)

        try:
            loaded = loader()
            if (
                loaded.trading_date != key[0]
                or loaded.sector_key != key[1]
            ):
                raise ValueError("sector fund-flow backfill cache key mismatch")
        except Exception:
            with self._condition:
                self._loading.discard(key)
                self._condition.notify_all()
            if stale_success is not None:
                return stale_success
            raise

        store = self._get_store()
        if store is not None:
            store.record(loaded)
        with self._condition:
            self._entries[key] = loaded
            self._last_loaded_at[key] = effective_refresh_at
            retained_dates = sorted({entry_key[0] for entry_key in self._entries})[-2:]
            self._entries = {
                entry_key: entry
                for entry_key, entry in self._entries.items()
                if entry_key[0] in retained_dates
            }
            self._loading.discard(key)
            self._condition.notify_all()
            return loaded


_SECTOR_FLOW_BACKFILL_CACHE = SectorFundFlowBackfillCache(persistent=True)


def fetch_sector_intraday_fund_flow(
    *,
    sector_key: str,
    name: str,
    taxonomy: str,
    provider_sector_code: str,
    trading_date: date | str,
    now: datetime | None = None,
    router: Any | None = None,
) -> SectorFundFlowIntradayV1:
    """Fetch one exact curve through the paid-first capability route.

    Providers are registered only when they expose this exact minute-level
    board-flow contract.  Daily fund-flow endpoints and minute price/turnover
    feeds are intentionally ineligible for fallback.
    """

    requested_date = (
        trading_date
        if isinstance(trading_date, date)
        else date.fromisoformat(str(trading_date))
    )
    fetched_at = now or datetime.now(_SHANGHAI)
    if fetched_at.tzinfo is None:
        raise ValueError("sector fund-flow gateway now must include a timezone")
    code = str(provider_sector_code).strip().upper()
    if not _PROVIDER_BOARD_CODE.fullmatch(code):
        raise ValueError("provider_sector_code must match BK plus digits")
    if taxonomy not in {"industry", "concept"}:
        raise ValueError("taxonomy must be industry or concept")

    if router is None:
        from tradex.data_sources import get_router, register_all_sources

        register_all_sources()
        router = get_router()
    series, _provider = router.route_validated(
        "sector_intraday_fund_flow",
        lambda payload, source: map_sector_intraday_fund_flow(
            payload,
            source,
            sector_key=sector_key,
            name=name,
            taxonomy=taxonomy,
            trading_date=requested_date,
            fetched_at=fetched_at,
        ),
        provider_sector_code=code,
        trade_date=requested_date,
    )
    return series


def fetch_sector_intraday_fund_flow_backfill(
    targets: Iterable[Mapping[str, Any]],
    *,
    trading_date: date | str,
    now: datetime | None = None,
    router: Any | None = None,
    cache: SectorFundFlowBackfillCache | None = None,
    load_missing: bool = False,
    refresh_existing: bool = False,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Return cached exact curves; only the minute sampler may fill misses.

    A failed sector is omitted so its live rotation samples remain usable. The
    cache stores successes only, allowing the next sampler tick to retry.
    """

    requested_date = (
        trading_date
        if isinstance(trading_date, date)
        else date.fromisoformat(str(trading_date))
    )
    owner = cache or _SECTOR_FLOW_BACKFILL_CACHE
    result: dict[str, tuple[dict[str, Any], ...]] = {}
    seen_keys: set[str] = set()
    for raw_target in targets:
        sector_key = str(raw_target.get("sector_key") or "").strip()
        name = str(raw_target.get("name") or "").strip()
        taxonomy = str(raw_target.get("taxonomy") or "").strip()
        provider_sector_code = str(
            raw_target.get("provider_sector_code") or ""
        ).strip().upper()
        if not sector_key or not name:
            raise ValueError("sector fund-flow backfill target lacks identity")
        if sector_key in seen_keys:
            raise ValueError("sector fund-flow backfill targets must be unique")
        if taxonomy not in {"industry", "concept"}:
            raise ValueError("sector fund-flow backfill taxonomy is invalid")
        if not _PROVIDER_BOARD_CODE.fullmatch(provider_sector_code):
            raise ValueError("sector fund-flow backfill provider code is invalid")
        seen_keys.add(sector_key)
        cache_key = (requested_date, sector_key, provider_sector_code)

        try:
            series = owner.get_cached(cache_key)
            if (series is None and load_missing) or (
                series is not None and refresh_existing
            ):
                series = owner.get_or_load(
                    cache_key,
                    lambda: fetch_sector_intraday_fund_flow(
                        sector_key=sector_key,
                        name=name,
                        taxonomy=taxonomy,
                        provider_sector_code=provider_sector_code,
                        trading_date=requested_date,
                        now=now,
                        router=router,
                    ),
                    refresh_existing=series is not None and refresh_existing,
                    refreshed_at=now,
                )
        except Exception:
            continue
        if series is None:
            continue
        result[sector_key] = tuple(
            {
                "provider_as_of": point.provider_as_of,
                "cumulative_cny": point.cumulative_cny,
                "source_family": series.metadata.provider,
            }
            for point in series.points
        )
    return result


__all__ = [
    "SectorFundFlowBackfillCache",
    "SectorFundFlowStore",
    "fetch_sector_intraday_fund_flow",
    "fetch_sector_intraday_fund_flow_backfill",
]
