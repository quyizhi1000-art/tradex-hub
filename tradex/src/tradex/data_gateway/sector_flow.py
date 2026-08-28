"""Provider-neutral sector intraday main-net-flow gateway."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime
from threading import Condition, Thread
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import SectorFundFlowIntradayV1
from .providers.sector_flow import map_sector_intraday_fund_flow
from .sector_flow_store import SectorFundFlowStore


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_PROVIDER_BOARD_CODE = re.compile(r"^BK\d+$")
_BackfillKey = tuple[date, str, str]
_BACKFILL_TARGETS_PER_SWEEP = 1


class SectorFundFlowBackfillRefresher:
    """Rotate bounded complete-curve refreshes on one daemon worker."""

    def __init__(
        self,
        refresh: Callable[..., dict[str, tuple[dict[str, Any], ...]]] | None = None,
    ) -> None:
        self._condition = Condition()
        self._refresh = refresh
        self._pending: tuple[tuple[dict[str, Any], ...], date] | None = None
        self._known_targets: dict[date, dict[str, dict[str, Any]]] = {}
        self._last_selected: dict[date, str] = {}
        self._thread: Thread | None = None

    def request(
        self,
        targets: Iterable[Mapping[str, Any]],
        *,
        trading_date: date | str,
    ) -> bool:
        normalized_targets = tuple(dict(target) for target in targets)
        if not normalized_targets:
            return False
        requested_date = (
            trading_date
            if isinstance(trading_date, date)
            else date.fromisoformat(str(trading_date))
        )
        with self._condition:
            known = self._known_targets.setdefault(requested_date, {})
            for target in normalized_targets:
                sector_key = str(target.get("sector_key") or "").strip()
                if sector_key:
                    known[sector_key] = target
            retained_dates = sorted(self._known_targets)[-2:]
            self._known_targets = {
                retained_date: self._known_targets[retained_date]
                for retained_date in retained_dates
            }
            self._last_selected = {
                retained_date: sector_key
                for retained_date, sector_key in self._last_selected.items()
                if retained_date in retained_dates
            }
            if self._thread is not None and self._thread.is_alive() and self._pending:
                self._condition.notify_all()
                return False
            ordered_keys = sorted(known)
            last_selected = self._last_selected.get(requested_date)
            start = (
                (ordered_keys.index(last_selected) + 1) % len(ordered_keys)
                if last_selected in ordered_keys
                else 0
            )
            selected_keys = tuple(
                ordered_keys[(start + offset) % len(ordered_keys)]
                for offset in range(
                    min(_BACKFILL_TARGETS_PER_SWEEP, len(ordered_keys))
                )
            )
            coalesced_targets = tuple(known[sector_key] for sector_key in selected_keys)
            if not coalesced_targets:
                return False
            self._last_selected[requested_date] = selected_keys[-1]
            self._pending = (coalesced_targets, requested_date)
            if self._thread is not None and self._thread.is_alive():
                self._condition.notify_all()
                return False
            self._thread = Thread(
                target=self._run,
                name="tradex-sector-flow-refresh",
                daemon=True,
            )
            self._thread.start()
            return True

    def _run(self) -> None:
        while True:
            with self._condition:
                work = self._pending
                self._pending = None
                if work is None:
                    self._thread = None
                    self._condition.notify_all()
                    return
            targets, trading_date = work
            refresh = self._refresh or fetch_sector_intraday_fund_flow_backfill
            try:
                refresh(
                    targets,
                    trading_date=trading_date,
                    load_missing=True,
                    refresh_existing=True,
                )
            except Exception:
                # The next Collector request retries the coalesced target set.
                continue

    def wait_for_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._thread is not None or self._pending is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


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

    def get_all_cached(
        self,
        trading_date: date,
    ) -> dict[str, SectorFundFlowIntradayV1]:
        with self._condition:
            cached = {
                key[1]: series
                for key, series in self._entries.items()
                if key[0] == trading_date
            }
        store = self._get_store()
        persisted = {} if store is None else store.get_all_best(trading_date)
        result = dict(persisted)
        for sector_key, series in cached.items():
            existing = result.get(sector_key)
            if existing is None or (
                len(series.points),
                series.points[-1].provider_as_of,
                series.metadata.fetched_at,
            ) > (
                len(existing.points),
                existing.points[-1].provider_as_of,
                existing.metadata.fetched_at,
            ):
                result[sector_key] = series
        return result

    def get_known_targets(
        self,
        trading_date: date,
    ) -> tuple[dict[str, str], ...]:
        store = self._get_store()
        return () if store is None else store.get_targets(trading_date)

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
            store.record_target(loaded, key[2])
            persisted = store.get_best(key[0], key[1])
            if persisted is not None:
                loaded = persisted
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


def _backfill_points(
    series: SectorFundFlowIntradayV1,
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "provider_as_of": point.provider_as_of,
            "cumulative_cny": point.cumulative_cny,
            "source_family": series.metadata.provider,
            "name": series.name,
            "taxonomy": series.taxonomy,
        }
        for point in series.points
    )


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
        result[sector_key] = _backfill_points(series)
    return result


def read_sector_intraday_fund_flow_backfill(
    *,
    trading_date: date | str,
    cache: SectorFundFlowBackfillCache | None = None,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Read every validated same-day curve without starting provider work."""

    requested_date = (
        trading_date
        if isinstance(trading_date, date)
        else date.fromisoformat(str(trading_date))
    )
    owner = cache or _SECTOR_FLOW_BACKFILL_CACHE
    return {
        sector_key: _backfill_points(series)
        for sector_key, series in owner.get_all_cached(requested_date).items()
    }


_SECTOR_FLOW_BACKFILL_REFRESHER = SectorFundFlowBackfillRefresher()


def schedule_sector_intraday_fund_flow_backfill(
    targets: Iterable[Mapping[str, Any]],
    *,
    trading_date: date | str,
) -> bool:
    """Request one bounded, non-blocking refresh across resolved curves."""

    requested_date = (
        trading_date
        if isinstance(trading_date, date)
        else date.fromisoformat(str(trading_date))
    )
    merged_targets = {
        str(target.get("sector_key") or "").strip(): dict(target)
        for target in _SECTOR_FLOW_BACKFILL_CACHE.get_known_targets(requested_date)
        if str(target.get("sector_key") or "").strip()
    }
    for target in targets:
        normalized = dict(target)
        sector_key = str(normalized.get("sector_key") or "").strip()
        if sector_key:
            merged_targets[sector_key] = normalized
    return _SECTOR_FLOW_BACKFILL_REFRESHER.request(
        merged_targets.values(),
        trading_date=requested_date,
    )


__all__ = [
    "SectorFundFlowBackfillCache",
    "SectorFundFlowBackfillRefresher",
    "SectorFundFlowStore",
    "fetch_sector_intraday_fund_flow",
    "fetch_sector_intraday_fund_flow_backfill",
    "read_sector_intraday_fund_flow_backfill",
    "schedule_sector_intraday_fund_flow_backfill",
]
