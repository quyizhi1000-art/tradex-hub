"""Provider-neutral sector intraday main-net-flow gateway."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, time as datetime_time, timedelta
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
        self._target_fingerprints: dict[date, tuple[tuple[str, ...], ...]] = {}
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

    def remember_targets(
        self,
        trading_date: date,
        targets: Iterable[Mapping[str, Any]],
    ) -> int:
        """Persist every resolved identity without starting provider work."""

        normalized = tuple(
            sorted(
                (
                    str(target.get("sector_key") or "").strip(),
                    str(target.get("name") or "").strip(),
                    str(target.get("taxonomy") or "").strip(),
                    str(target.get("provider_sector_code") or "").strip().upper(),
                    str(target.get("source_family") or "eastmoney").strip(),
                )
                for target in targets
            )
        )
        if not normalized:
            return 0
        store = self._get_store()
        if store is None:
            return 0
        with self._condition:
            if self._target_fingerprints.get(trading_date) == normalized:
                return len(normalized)
        remembered = store.record_target_identities(
            trading_date,
            (
                {
                    "sector_key": item[0],
                    "name": item[1],
                    "taxonomy": item[2],
                    "provider_sector_code": item[3],
                    "source_family": item[4],
                }
                for item in normalized
            ),
        )
        with self._condition:
            self._target_fingerprints[trading_date] = normalized
            retained_dates = sorted(self._target_fingerprints)[-2:]
            self._target_fingerprints = {
                key: value
                for key, value in self._target_fingerprints.items()
                if key in retained_dates
            }
        return remembered

    def get_or_load(
        self,
        key: _BackfillKey,
        loader: Callable[[], SectorFundFlowIntradayV1],
        *,
        refresh_existing: bool = False,
        force_refresh: bool = False,
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
                and not force_refresh
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
    max_queue_wait: float | None = None,
    request_timeout: int = 10,
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
    route_kwargs: dict[str, Any] = {
        "provider_sector_code": code,
        "trade_date": requested_date,
    }
    if max_queue_wait is not None:
        route_kwargs["max_queue_wait"] = max_queue_wait
    if request_timeout != 10:
        route_kwargs["request_timeout"] = request_timeout
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
        **route_kwargs,
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
    force_refresh: bool = False,
    max_queue_wait: float | None = None,
    request_timeout: int = 10,
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
                        max_queue_wait=max_queue_wait,
                        request_timeout=request_timeout,
                    ),
                    refresh_existing=series is not None and refresh_existing,
                    force_refresh=force_refresh,
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


def prepare_sector_intraday_fund_flow_backfill(
    *,
    trading_date: date | str,
    required_minutes: Iterable[datetime],
    now: datetime | None = None,
    cache: SectorFundFlowBackfillCache | None = None,
    router: Any | None = None,
    progress: Callable[[int, int, str, str | None], None] | None = None,
) -> dict[str, Any]:
    """Refresh only exact sector curves that omit a required recovery minute."""

    requested_date = (
        trading_date
        if isinstance(trading_date, date)
        else date.fromisoformat(str(trading_date))
    )
    effective_now = now or datetime.now(_SHANGHAI)
    if effective_now.tzinfo is None or effective_now.utcoffset() is None:
        raise ValueError("sector backfill preparation now must include a timezone")
    effective_now = effective_now.astimezone(_SHANGHAI)
    required = set()
    for minute in required_minutes:
        if minute.tzinfo is None or minute.utcoffset() is None:
            raise ValueError("required sector backfill minutes must include a timezone")
        local = minute.astimezone(_SHANGHAI).replace(second=0, microsecond=0)
        if local.date() != requested_date:
            raise ValueError("required sector backfill minute belongs to another date")
        required.add(local)
    if not required:
        raise ValueError("required sector backfill minutes must not be empty")

    owner = cache or _SECTOR_FLOW_BACKFILL_CACHE
    targets = tuple(owner.get_known_targets(requested_date))

    def covered(curves: Mapping[str, Iterable[Mapping[str, Any]]], key: str) -> bool:
        exact_minutes: set[datetime] = set()
        for point in curves.get(key) or ():
            raw = point.get("provider_as_of")
            try:
                parsed = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
            except (TypeError, ValueError):
                continue
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                continue
            exact_minutes.add(
                parsed.astimezone(_SHANGHAI).replace(second=0, microsecond=0)
            )
        return required.issubset(exact_minutes)

    curves = read_sector_intraday_fund_flow_backfill(
        trading_date=requested_date,
        cache=owner,
    )
    missing = tuple(
        target
        for target in targets
        if not covered(curves, str(target.get("sector_key") or "").strip())
    )
    unavailable_minutes = tuple(
        sorted(
            minute
            for minute in required
            if minute.time() in {datetime_time(9, 30), datetime_time(13, 0)}
        )
    )
    if missing and unavailable_minutes:
        missing_keys = tuple(
            str(target.get("sector_key") or "").strip() for target in missing
        )
        missing_key_set = set(missing_keys)
        return {
            "complete": False,
            "known_targets": len(targets),
            "ready_targets": len(targets) - len(missing_keys),
            "ready_target_keys": tuple(
                str(target.get("sector_key") or "").strip()
                for target in targets
                if str(target.get("sector_key") or "").strip()
                not in missing_key_set
            ),
            "refreshed_targets": 0,
            "missing_targets": missing_keys,
            "unavailable_minutes": unavailable_minutes,
        }
    total = len(missing)
    for completed, target in enumerate(missing, start=1):
        fetch_sector_intraday_fund_flow_backfill(
            (target,),
            trading_date=requested_date,
            now=effective_now,
            router=router,
            cache=owner,
            load_missing=True,
            refresh_existing=True,
        )
        if progress is not None:
            progress(
                completed,
                total,
                "rotation_curves",
                f"已刷新精确板块曲线 {completed}/{total}",
            )

    curves = read_sector_intraday_fund_flow_backfill(
        trading_date=requested_date,
        cache=owner,
    )
    missing_keys = tuple(
        str(target.get("sector_key") or "").strip()
        for target in targets
        if not covered(curves, str(target.get("sector_key") or "").strip())
    )
    ready_targets = len(targets) - len(missing_keys)
    return {
        "complete": bool(targets) and not missing_keys,
        "known_targets": len(targets),
        "ready_targets": ready_targets,
        "ready_target_keys": tuple(
            str(target.get("sector_key") or "").strip()
            for target in targets
            if str(target.get("sector_key") or "").strip() not in missing_keys
        ),
        "refreshed_targets": total,
        "missing_targets": missing_keys,
    }


def finalize_sector_intraday_fund_flow_backfill(
    *,
    trading_date: date | str,
    required_through: datetime,
    now: datetime | None = None,
    cache: SectorFundFlowBackfillCache | None = None,
    router: Any | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Refresh every persisted target once before replaying close gaps.

    This is deliberately separate from the intraday rotating refresher, whose
    one-target request budget remains unchanged.  The report is an optimization
    preflight only; exact requested minutes are still checked by
    ``prepare_sector_intraday_fund_flow_backfill`` before a snapshot is accepted.
    """

    requested_date = (
        trading_date
        if isinstance(trading_date, date)
        else date.fromisoformat(str(trading_date))
    )
    if required_through.tzinfo is None or required_through.utcoffset() is None:
        raise ValueError("sector-flow finalization cutoff must include a timezone")
    cutoff = required_through.astimezone(_SHANGHAI).replace(second=0, microsecond=0)
    if cutoff.date() != requested_date:
        raise ValueError("sector-flow finalization cutoff belongs to another date")
    effective_now = now or datetime.now(_SHANGHAI)
    if effective_now.tzinfo is None or effective_now.utcoffset() is None:
        raise ValueError("sector-flow finalization now must include a timezone")
    effective_now = effective_now.astimezone(_SHANGHAI)
    if effective_now.date() != requested_date or effective_now.time() < cutoff.time():
        raise ValueError("sector-flow finalization now must reach the requested cutoff")
    owner = cache or _SECTOR_FLOW_BACKFILL_CACHE
    targets = tuple(
        sorted(
            owner.get_known_targets(requested_date),
            key=lambda item: str(item.get("sector_key") or ""),
        )
    )
    total = len(targets)
    existing_curves = read_sector_intraday_fund_flow_backfill(
        trading_date=requested_date,
        cache=owner,
    )

    def curve_is_complete(points: Iterable[Mapping[str, Any]]) -> bool:
        provider_times: list[datetime] = []
        for point in points:
            try:
                provider = datetime.fromisoformat(str(point.get("provider_as_of") or ""))
            except ValueError:
                return False
            if provider.tzinfo is None:
                return False
            provider_times.append(provider.astimezone(_SHANGHAI))
        if not provider_times or provider_times[-1] < cutoff:
            return False
        for previous, current in zip(provider_times, provider_times[1:]):
            if current <= previous:
                return False
            same_segment = (
                previous.time() <= datetime_time(11, 30)
                and current.time() <= datetime_time(11, 30)
            ) or (
                previous.time() >= datetime_time(13, 0)
                and current.time() >= datetime_time(13, 0)
            )
            if same_segment and (current - previous).total_seconds() > 5 * 60:
                return False
        return True

    refreshed_count = 0
    for completed, target in enumerate(targets, start=1):
        key = str(target.get("sector_key") or "").strip()
        if not curve_is_complete(existing_curves.get(key) or ()):
            fetch_sector_intraday_fund_flow_backfill(
                (target,),
                trading_date=requested_date,
                now=effective_now,
                router=router,
                cache=owner,
                load_missing=True,
                refresh_existing=True,
                force_refresh=True,
                # Closing finalization shares the queue with derived evidence;
                # allow a bounded wait for those already-reserved requests.
                max_queue_wait=6.0,
                request_timeout=4,
            )
            refreshed_count += 1
        if progress is not None:
            progress(completed, total, key)

    curves = read_sector_intraday_fund_flow_backfill(
        trading_date=requested_date,
        cache=owner,
    )
    point_counts: list[int] = []
    complete_keys: list[str] = []
    for target in targets:
        key = str(target.get("sector_key") or "").strip()
        points = tuple(curves.get(key) or ())
        point_counts.append(len(points))
        if curve_is_complete(points):
            complete_keys.append(key)
    return {
        "contract": "sector_intraday_fund_flow_finalization.v1",
        "schema_version": 1,
        "trade_date": requested_date.isoformat(),
        "required_through": cutoff.isoformat(timespec="seconds"),
        "target_count": total,
        "refreshed_target_count": refreshed_count,
        "complete_count": len(complete_keys),
        "complete": bool(total) and len(complete_keys) == total,
        "complete_target_keys": tuple(complete_keys),
        "missing_target_keys": tuple(
            str(target.get("sector_key") or "").strip()
            for target in targets
            if str(target.get("sector_key") or "").strip() not in complete_keys
        ),
        "min_point_count": min(point_counts, default=0),
        "max_point_count": max(point_counts, default=0),
    }


_SECTOR_FLOW_BACKFILL_REFRESHER = SectorFundFlowBackfillRefresher()


def _expected_intraday_minutes(cutoff: datetime) -> set[datetime]:
    local = cutoff.astimezone(_SHANGHAI).replace(second=0, microsecond=0)
    morning_start = local.replace(hour=9, minute=31)
    morning_end = min(local, local.replace(hour=11, minute=30))
    expected: set[datetime] = set()
    if morning_end >= morning_start:
        cursor = morning_start
        while cursor <= morning_end:
            expected.add(cursor)
            cursor += timedelta(minutes=1)
    afternoon_start = local.replace(hour=13, minute=1)
    if local >= afternoon_start:
        cursor = afternoon_start
        afternoon_end = min(local, local.replace(hour=15, minute=0))
        while cursor <= afternoon_end:
            expected.add(cursor)
            cursor += timedelta(minutes=1)
    return expected


def _curve_missing_count(
    series: SectorFundFlowIntradayV1 | None,
    cutoff: datetime,
) -> int:
    expected = _expected_intraday_minutes(cutoff)
    if series is None:
        return len(expected)
    actual = {
        point.provider_as_of.astimezone(_SHANGHAI).replace(second=0, microsecond=0)
        for point in series.points
        if point.provider_as_of.astimezone(_SHANGHAI) <= cutoff
    }
    return len(expected - actual)


def _intraday_repair_window_open(observed: datetime) -> bool:
    from tradex.market_calendar import TradingSessionPhase, a_share_session

    local = observed.astimezone(_SHANGHAI)
    session = a_share_session(local)
    if not session.is_trading_day:
        return False
    if session.phase is TradingSessionPhase.MIDDAY_BREAK:
        return True
    if session.phase is TradingSessionPhase.CLOSED and local.hour >= 15:
        # The same-day provider can still supply real minute history after close.
        # There is no current-minute collection competing for the queue then.
        return True
    if session.phase in {
        TradingSessionPhase.OPENING_OBSERVATION,
        TradingSessionPhase.TRADING,
    }:
        # Current-minute acquisition normally finishes around second 20.  Stop
        # early enough that a slow mirror attempt cannot cross the next minute.
        return 22 <= local.second <= 46
    return False


class SectorFundFlowIntradayRepairWorker:
    """Run one persisted manual repair round only in Collector slack windows."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(_SHANGHAI))
        self._condition = Condition()
        self._thread: Thread | None = None

    def request(self, trading_date: date) -> bool:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._thread = Thread(
                target=self._run,
                args=(trading_date,),
                name="tradex-sector-flow-intraday-repair",
                daemon=True,
            )
            self._thread.start()
            return True

    def _run(self, trading_date: date) -> None:
        try:
            with SectorFundFlowStore() as store:
                repair = store.read_intraday_repair(trading_date)
                if repair is None or repair["status"] not in {"pending", "running"}:
                    return
                cutoff = datetime.fromisoformat(str(repair["required_through"]))
                targets = tuple(store.get_targets(trading_date))
                curves = _SECTOR_FLOW_BACKFILL_CACHE.get_all_cached(trading_date)
                ordered = tuple(
                    sorted(
                        targets,
                        key=lambda item: (
                            -_curve_missing_count(
                                curves.get(str(item.get("sector_key") or "")),
                                cutoff,
                            ),
                            str(item.get("sector_key") or ""),
                        ),
                    )
                )
                remaining = sum(
                    _curve_missing_count(
                        curves.get(str(item.get("sector_key") or "")),
                        cutoff,
                    )
                    > 0
                    for item in ordered
                )
                store.update_intraday_repair(
                    trading_date,
                    status="running",
                    observed_at=self._clock(),
                    target_count=len(ordered),
                    remaining_targets=remaining,
                )
                if not ordered:
                    store.update_intraday_repair(
                        trading_date,
                        status="partial",
                        observed_at=self._clock(),
                        target_count=0,
                        remaining_targets=0,
                        last_error="尚未记录可追补的板块标识",
                    )
                    return
                for target in ordered:
                    sector_key = str(target.get("sector_key") or "")
                    before = _SECTOR_FLOW_BACKFILL_CACHE.get_cached(
                        (
                            trading_date,
                            sector_key,
                            str(target.get("provider_sector_code") or ""),
                        )
                    )
                    before_missing = _curve_missing_count(before, cutoff)
                    if before_missing == 0:
                        continue
                    observed = self._clock()
                    if not _intraday_repair_window_open(observed):
                        store.update_intraday_repair(
                            trading_date,
                            status="pending",
                            observed_at=observed,
                            remaining_targets=remaining,
                            last_error=None,
                        )
                        return
                    fetch_sector_intraday_fund_flow_backfill(
                        (target,),
                        trading_date=trading_date,
                        now=observed,
                        cache=_SECTOR_FLOW_BACKFILL_CACHE,
                        load_missing=True,
                        refresh_existing=True,
                        force_refresh=True,
                        max_queue_wait=2.0,
                        request_timeout=4,
                    )
                    after = _SECTOR_FLOW_BACKFILL_CACHE.get_cached(
                        (
                            trading_date,
                            sector_key,
                            str(target.get("provider_sector_code") or ""),
                        )
                    )
                    after_missing = _curve_missing_count(after, cutoff)
                    improved = after_missing < before_missing
                    failed = (
                        after is None
                        or (
                            not improved
                            and (
                                before is None
                                or after.metadata.fetched_at == before.metadata.fetched_at
                            )
                        )
                    )
                    if after_missing == 0:
                        remaining = max(0, remaining - 1)
                    store.update_intraday_repair(
                        trading_date,
                        status="running",
                        observed_at=self._clock(),
                        attempted_delta=1,
                        improved_delta=int(improved),
                        failed_delta=int(failed),
                        remaining_targets=remaining,
                        last_sector_key=sector_key,
                        last_error=("上游繁忙或本轮未返回更完整曲线" if failed else None),
                    )
                    if failed:
                        store.update_intraday_repair(
                            trading_date,
                            status="pending",
                            observed_at=self._clock(),
                            remaining_targets=remaining,
                            last_sector_key=sector_key,
                            last_error="上游繁忙；已暂停并等待下一采集空档",
                        )
                        return
                final_curves = _SECTOR_FLOW_BACKFILL_CACHE.get_all_cached(trading_date)
                remaining = sum(
                    _curve_missing_count(
                        final_curves.get(str(item.get("sector_key") or "")),
                        cutoff,
                    )
                    > 0
                    for item in ordered
                )
                store.update_intraday_repair(
                    trading_date,
                    status="complete" if remaining == 0 else "partial",
                    observed_at=self._clock(),
                    remaining_targets=remaining,
                    last_error=(None if remaining == 0 else "部分板块的精确分钟仍未由上游返回"),
                )
        except Exception as error:
            try:
                with SectorFundFlowStore() as store:
                    current = store.read_intraday_repair(trading_date)
                    if current is not None and current["status"] in {"pending", "running"}:
                        store.update_intraday_repair(
                            trading_date,
                            status="partial",
                            observed_at=self._clock(),
                            remaining_targets=current["remaining_targets"],
                            last_error=f"{type(error).__name__}: {error}"[:300],
                        )
            except Exception:
                pass
        finally:
            with self._condition:
                self._thread = None
                self._condition.notify_all()

    def wait_for_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._thread is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


_SECTOR_FLOW_INTRADAY_REPAIR_WORKER = SectorFundFlowIntradayRepairWorker()


def request_sector_intraday_fund_flow_repair(
    *,
    trading_date: date,
    required_through: datetime,
    requested_at: datetime,
) -> dict[str, Any]:
    with SectorFundFlowStore() as store:
        return store.request_intraday_repair(
            trading_date,
            required_through=required_through,
            requested_at=requested_at,
        )


def read_sector_intraday_fund_flow_repair(
    *,
    trading_date: date,
) -> dict[str, Any] | None:
    with SectorFundFlowStore() as store:
        return store.read_intraday_repair(trading_date)


def schedule_requested_sector_intraday_fund_flow_repair(
    *,
    observed_at: datetime,
) -> bool:
    """Let the managed Collector poll a Web-queued repair without provider work."""

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("intraday repair observed_at must include a timezone")
    local = observed_at.astimezone(_SHANGHAI)
    with SectorFundFlowStore() as store:
        repair = store.read_intraday_repair(local.date())
    if repair is None or repair["status"] not in {"pending", "running"}:
        return False
    return _SECTOR_FLOW_INTRADAY_REPAIR_WORKER.request(local.date())


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
    current_targets = tuple(dict(target) for target in targets)
    _SECTOR_FLOW_BACKFILL_CACHE.remember_targets(
        requested_date,
        current_targets,
    )
    merged_targets = {
        str(target.get("sector_key") or "").strip(): dict(target)
        for target in _SECTOR_FLOW_BACKFILL_CACHE.get_known_targets(requested_date)
        if str(target.get("sector_key") or "").strip()
    }
    for normalized in current_targets:
        sector_key = str(normalized.get("sector_key") or "").strip()
        if sector_key:
            merged_targets[sector_key] = normalized
    with SectorFundFlowStore() as store:
        repair = store.read_intraday_repair(requested_date)
    if repair is not None and repair["status"] in {"pending", "running"}:
        return _SECTOR_FLOW_INTRADAY_REPAIR_WORKER.request(requested_date)
    return _SECTOR_FLOW_BACKFILL_REFRESHER.request(
        merged_targets.values(),
        trading_date=requested_date,
    )


__all__ = [
    "SectorFundFlowBackfillCache",
    "SectorFundFlowBackfillRefresher",
    "SectorFundFlowIntradayRepairWorker",
    "SectorFundFlowStore",
    "fetch_sector_intraday_fund_flow",
    "fetch_sector_intraday_fund_flow_backfill",
    "finalize_sector_intraday_fund_flow_backfill",
    "prepare_sector_intraday_fund_flow_backfill",
    "read_sector_intraday_fund_flow_backfill",
    "read_sector_intraday_fund_flow_repair",
    "request_sector_intraday_fund_flow_repair",
    "schedule_requested_sector_intraday_fund_flow_repair",
    "schedule_sector_intraday_fund_flow_backfill",
]
