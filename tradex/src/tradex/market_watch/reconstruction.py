"""Exact same-day post-close reconstruction for missed market-watch minutes."""

from __future__ import annotations

import time as time_module
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

from tradex.data_gateway import (
    fetch_a_share_universe_snapshot,
    fetch_index_intraday_series,
    fetch_intraday_minute_series,
    fetch_intraday_minute_series_batch_partial,
    fetch_market_overview,
    fetch_opening_auction_market,
)
from tradex.market_calendar import CalendarDayStatus, calendar_day_status

from .analysis import build_market_watch_snapshot
from .collection_contracts import RetryableCollectionError
from .contracts import FreshnessStatus, MarketPhase, MarketWatchSnapshotV1
from .history import MarketWatchHistoryStore
from .recovery_source_cache import RecoverySourceMatrixStore


SHANGHAI = ZoneInfo("Asia/Shanghai")
ProgressCallback = Callable[[int, int, str, str | None], None]

_INDEX_ROLES = (
    ("000001.SH", "broad_market", "上证指数"),
    ("000300.SH", "large_cap", "沪深300"),
    ("000852.SH", "small_cap", "中证1000"),
    ("399006.SZ", "growth", "创业板指"),
)
_TURNOVER_INDICES = ("000001.SH", "399001.SZ")
_ROTATION_MAX_AGE_SECONDS = 60
_OPENING_AUCTION_TIME = time(9, 25)


class HistoricalTrajectoryUnavailable(RetryableCollectionError):
    """The persisted target minute still lacks an exact trajectory branch."""

    def __init__(self, message: str, *, target: datetime) -> None:
        local = target.astimezone(SHANGHAI)
        deadline = datetime.combine(
            local.date() + timedelta(days=1),
            time.min,
            tzinfo=SHANGHAI,
        )
        super().__init__(
            message,
            retry_after_seconds=60.0,
            retry_deadline=deadline,
        )


@dataclass(frozen=True, slots=True)
class _CachedIndexPoint:
    close: float
    amount_cny: float


def _provider_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(SHANGHAI)


def _assert_rotation_trajectory_current(
    risk: dict[str, Any],
    target: datetime,
    *,
    required_sector_keys: set[str] | None = None,
) -> None:
    """Fail before costly whole-market loading when a trajectory is already stale."""

    local = target.astimezone(SHANGHAI)
    stale: list[str] = []
    observed_keys: set[str] = set()
    for field in (
        "sector_flow_trajectory",
        "offense_sector_flow_trajectory",
    ):
        trajectory = risk.get(field)
        if not isinstance(trajectory, dict):
            continue
        sectors = trajectory.get("sectors")
        if not isinstance(sectors, list) or not sectors:
            stale.append(field)
            continue
        for sector in sectors:
            key = (
                str(sector.get("sector_key") or field)
                if isinstance(sector, dict)
                else field
            )
            if required_sector_keys is not None and key not in required_sector_keys:
                continue
            observed_keys.add(key)
            latest = sector.get("latest") if isinstance(sector, dict) else None
            provider = _provider_time(
                latest.get("provider_as_of") if isinstance(latest, dict) else None
            )
            if (
                provider is None
                or provider.date() != local.date()
                or not -120
                <= (local - provider).total_seconds()
                <= _ROTATION_MAX_AGE_SECONDS
            ):
                stale.append(key)
    if required_sector_keys is not None:
        stale.extend(sorted(required_sector_keys - observed_keys))
    if stale:
        raise HistoricalTrajectoryUnavailable(
            "rotation trajectory is not current for target minute: "
            + ", ".join(stale[:8]),
            target=local,
        )


def _rotation_cross_section_is_current(
    risk: dict[str, Any],
    target: datetime,
) -> bool:
    offense = risk.get("offense")
    lists = offense.get("lists") if isinstance(offense, dict) else None
    records = [
        item
        for name in ("attacking", "rotating", "cooling", "unclassified")
        for item in (lists.get(name) or ())
        if isinstance(item, dict)
    ] if isinstance(lists, dict) else []
    if not records:
        return False
    local = target.astimezone(SHANGHAI)
    provider_times = [
        _provider_time(
            item.get("provider_as_of")
            or (
                item.get("funds", {}).get("as_of")
                if isinstance(item.get("funds"), dict)
                else None
            )
        )
        for item in records
    ]
    return all(
        provider is not None
        and provider.date() == local.date()
        and -120
        <= (local - provider).total_seconds()
        <= _ROTATION_MAX_AGE_SECONDS
        for provider in provider_times
    )


def _exact_flow_rotation_sectors(
    risk: dict[str, Any],
    target: datetime,
    *,
    required_sector_keys: set[str] | None = None,
) -> list[dict[str, Any]] | None:
    """Build current, flow-only evidence without inventing price or breadth."""

    local = target.astimezone(SHANGHAI)
    result: dict[str, dict[str, Any]] = {}
    for field in (
        "sector_flow_trajectory",
        "offense_sector_flow_trajectory",
    ):
        trajectory = risk.get(field)
        sectors = trajectory.get("sectors") if isinstance(trajectory, dict) else None
        if not isinstance(sectors, list) or not sectors:
            if required_sector_keys is None:
                return None
            continue
        for sector in sectors:
            if not isinstance(sector, dict):
                return None
            latest = sector.get("latest")
            if not isinstance(latest, dict):
                return None
            provider = _provider_time(latest.get("provider_as_of"))
            cumulative = latest.get("cumulative_cny")
            key = str(sector.get("sector_key") or "").strip()
            if required_sector_keys is not None and key not in required_sector_keys:
                continue
            name = str(sector.get("name") or "").strip()
            if (
                not key
                or not name
                or provider is None
                or provider.date() != local.date()
                or not -120
                <= (local - provider).total_seconds()
                <= _ROTATION_MAX_AGE_SECONDS
                or not isinstance(cumulative, (int, float))
            ):
                return None
            result[key] = {
                "sector_key": key,
                "name": name,
                "main_net_inflow_cny": float(cumulative),
                "main_net_inflow_ratio": latest.get("main_net_inflow_pct"),
                "provider_as_of": provider.isoformat(),
            }
    if required_sector_keys is not None and not required_sector_keys.issubset(result):
        return None
    return list(result.values()) or None


def _prepare_historical_rotation(
    risk: dict[str, Any],
    target: datetime,
    *,
    required_sector_keys: set[str] | None = None,
) -> dict[str, Any]:
    component = {
        "status": "ready",
        "quality": "accepted",
        "provider_as_of": target.isoformat(),
    }
    if _rotation_cross_section_is_current(risk, target):
        return component
    sectors = _exact_flow_rotation_sectors(
        risk,
        target,
        required_sector_keys=required_sector_keys,
    )
    if sectors is None:
        raise HistoricalTrajectoryUnavailable(
            "rotation cross-section is stale and exact flow-only evidence is unavailable",
            target=target,
        )
    risk["rotation"] = {"sectors": sectors}
    component.update({
        "quality": "degraded",
        "partial": True,
        "quality_flags": ["flow_only_historical_rotation"],
    })
    return component


class SameDayPostCloseReconstructor:
    """Load exact same-day source curves once and rebuild only requested gaps."""

    def __init__(
        self,
        *,
        history: MarketWatchHistoryStore,
        target_minutes: Callable[[date], Iterable[datetime]],
        rotation_loader: Callable[[datetime], dict[str, Any]],
        clock: Callable[[], datetime],
        rotation_curve_preparer: Callable[..., Mapping[str, Any]] | None = None,
        source_cache: RecoverySourceMatrixStore | None = None,
        sleep: Callable[[float], None] = time_module.sleep,
        batch_pause_seconds: float = 0.0,
        batch_concurrency: int = 4,
    ) -> None:
        self._history = history
        self._target_minutes = target_minutes
        self._rotation_loader = rotation_loader
        self._rotation_curve_preparer = rotation_curve_preparer
        self._source_cache = source_cache
        self._clock = clock
        self._sleep = sleep
        self._batch_pause_seconds = float(batch_pause_seconds)
        if self._batch_pause_seconds < 0:
            raise ValueError("batch_pause_seconds cannot be negative")
        if not 1 <= int(batch_concurrency) <= 4:
            raise ValueError("batch_concurrency must be between 1 and 4")
        self._batch_concurrency = int(batch_concurrency)
        self._lock = Lock()
        self._trade_date: date | None = None
        self._previous_close: dict[str, float] = {}
        self._stock_prices: dict[time, dict[str, float]] = {}
        self._index_points: dict[str, dict[time, Any]] = {}
        self._index_previous_close: dict[str, tuple[str, float]] = {}
        self._partial_trade_date: date | None = None
        self._partial_previous_close: dict[str, float] = {}
        self._partial_stock_prices: dict[time, dict[str, float]] = {}
        self._prepared_rotation_date: date | None = None
        self._prepared_rotation_minutes: set[time] = set()
        self._prepared_rotation_keys: set[str] = set()

    def reconstruct_opening_auction(
        self,
        target: datetime,
        progress: ProgressCallback,
    ) -> MarketWatchSnapshotV1:
        """Rebuild one exact 09:25 aggregate without inventing sector net flow."""

        local = target.astimezone(SHANGHAI).replace(second=0, microsecond=0)
        if local.time() != _OPENING_AUCTION_TIME:
            raise ValueError("opening-auction reconstruction requires 09:25")
        observed = self._clock().astimezone(SHANGHAI)
        progress(0, 5, "auction_market", "正在加载全市场09:25集合竞价结果")
        current = fetch_opening_auction_market(local.date(), now=observed)
        previous_date = self._previous_trading_date(local.date())
        previous = fetch_opening_auction_market(previous_date, now=observed)

        progress(1, 5, "auction_indices", "正在校验四个角色指数开盘点位")
        overview = fetch_market_overview(now=observed)
        identities = {
            item.instrument_id: item
            for item in overview.indices
            if item.available and item.previous_close is not None
        }
        indices: list[dict[str, Any]] = []
        for instrument_id, role, default_name in _INDEX_ROLES:
            identity = identities.get(instrument_id)
            if identity is None or not identity.previous_close:
                raise RuntimeError(
                    f"opening auction lacks previous close for {instrument_id}"
                )
            series = fetch_index_intraday_series(
                instrument_id,
                now=observed,
                days=1,
            )
            opening = next(
                (
                    item
                    for item in series.points
                    if item.trading_date == local.date()
                    and item.minute == time(9, 30)
                ),
                None,
            )
            if opening is None or opening.open <= 0:
                raise RuntimeError(
                    f"opening auction lacks exact index open for {instrument_id}"
                )
            indices.append(
                {
                    "instrument_id": instrument_id,
                    "role": role,
                    "name": identity.name or default_name,
                    "available": True,
                    "level": opening.open,
                    "change_pct": (
                        (opening.open - identity.previous_close)
                        / identity.previous_close
                        * 100
                    ),
                    "previous_close": identity.previous_close,
                    "provider_as_of": local.isoformat(),
                    "quality": "accepted",
                    "quality_flags": ["opening_price_from_first_minute_open"],
                }
            )

        progress(2, 5, "auction_breadth", "正在汇总竞价涨跌家数")
        current_quality = current.metadata.quality.value
        breadth_status = "fresh" if current_quality == "accepted" else "degraded"
        fetched_at = observed.isoformat()
        market_data = {
            "timestamp": fetched_at,
            "provider_as_of": local.isoformat(),
            "market_state": {"is_open": False, "phase": "pre_open"},
            "indices": indices,
            "market_turnover": {
                "available": True,
                "today_date": current.trading_date.isoformat(),
                "previous_date": previous.trading_date.isoformat(),
                "as_of": "09:25",
                "today_amount_cny": current.total_amount_cny,
                "previous_same_time_amount_cny": previous.total_amount_cny,
            },
        }
        risk_data = {
            "timestamp": fetched_at,
            "phase": "pre_open",
            "breadth": {
                "up_count": current.up_count,
                "down_count": current.down_count,
                "flat_count": current.flat_count,
                "unclassified_count": current.excluded_row_count,
                "total_count": current.provider_row_count,
                "provider_as_of": local.isoformat(),
                "quality": current_quality,
                "quality_flags": ["opening_auction_breadth"],
            },
            "rotation": {"sectors": []},
            "freshness": {
                "status": "degraded",
                "components": [
                    {
                        "component": "indices",
                        "status": "fresh",
                        "quality": "accepted",
                        "provider_as_of": local.isoformat(),
                        "fetched_at": fetched_at,
                        "flags": ["opening_price_from_first_minute_open"],
                    },
                    {
                        "component": "breadth",
                        "status": breadth_status,
                        "quality": current_quality,
                        "provider_as_of": local.isoformat(),
                        "fetched_at": fetched_at,
                        "flags": ["opening_auction_breadth"],
                    },
                    {
                        "component": "turnover",
                        "status": "fresh",
                        "quality": "accepted",
                        "provider_as_of": local.isoformat(),
                        "fetched_at": fetched_at,
                        "flags": ["opening_auction_matched_amount"],
                    },
                    {
                        "component": "rotation",
                        "status": "unavailable",
                        "quality": "unavailable",
                        "provider_as_of": None,
                        "fetched_at": fetched_at,
                        "flags": ["pre_open_sector_net_flow_not_published"],
                    },
                ],
                "flags": ["opening_auction_snapshot"],
            },
        }
        progress(4, 5, "auction_contract", "正在校验09:25竞价盘面契约")
        snapshot = build_market_watch_snapshot(
            market_data,
            risk_data,
            as_of=local,
            snapshot_id=f"mw-auction:{local:%Y%m%d-%H%M}",
        )
        if (
            snapshot.market_state.phase is not MarketPhase.PRE_OPEN
            or not snapshot.breadth.available
            or not snapshot.turnover.available
            or len(snapshot.indices) != 4
            or snapshot.rotation.sectors
            or snapshot.sector_flow_trajectory is not None
            or snapshot.offense_sector_flow_trajectory is not None
        ):
            raise RuntimeError("opening-auction snapshot failed semantic validation")
        progress(5, 5, "auction_contract", "09:25竞价盘面已通过严格校验")
        return snapshot

    def reconstruct(
        self,
        target: datetime,
        progress: ProgressCallback,
    ) -> MarketWatchSnapshotV1:
        local = target.astimezone(SHANGHAI).replace(second=0, microsecond=0)
        observed = self._clock().astimezone(SHANGHAI)
        if observed.date() != local.date() or observed.time() < time(15, 0):
            raise RuntimeError(
                "same-day exact reconstruction is available only after close and before midnight"
            )
        progress(0, 6, "preflight", "正在校验目标分钟板块轨迹与成交额基线")
        required_minutes = {
            item.astimezone(SHANGHAI).replace(second=0, microsecond=0)
            for item in self._target_minutes(local.date())
        }
        required_minutes.add(local)
        required_times = {item.time() for item in required_minutes}
        if self._prepared_rotation_date != local.date():
            self._prepared_rotation_date = local.date()
            self._prepared_rotation_minutes = set()
            self._prepared_rotation_keys = set()
        if (
            self._rotation_curve_preparer is not None
            and not required_times.issubset(self._prepared_rotation_minutes)
        ):
            preparation = dict(
                self._rotation_curve_preparer(
                    trading_date=local.date(),
                    required_minutes=tuple(sorted(required_minutes)),
                    now=observed,
                    progress=progress,
                )
            )
            if not preparation.get("complete"):
                missing = tuple(preparation.get("missing_targets") or ())
                detail = ", ".join(str(item) for item in missing[:8]) or "no known exact curves"
                raise HistoricalTrajectoryUnavailable(
                    "exact sector-flow curves are still converging: " + detail,
                    target=local,
                )
            prepared_keys = {
                str(item).strip()
                for item in preparation.get("ready_target_keys") or ()
                if str(item).strip()
            }
            if int(preparation.get("known_targets") or 0) and not prepared_keys:
                raise HistoricalTrajectoryUnavailable(
                    "exact sector-flow preparation omitted its recovery scope",
                    target=local,
                )
            self._prepared_rotation_minutes.update(required_times)
            self._prepared_rotation_keys.update(prepared_keys)
        risk = dict(self._rotation_loader(local))
        _assert_rotation_trajectory_current(
            risk,
            local,
            required_sector_keys=(
                self._prepared_rotation_keys
                if self._rotation_curve_preparer is not None
                else None
            ),
        )
        rotation_component = _prepare_historical_rotation(
            risk,
            local,
            required_sector_keys=(
                self._prepared_rotation_keys
                if self._rotation_curve_preparer is not None
                else None
            ),
        )
        previous_date, previous_amount = self._previous_turnover(local)
        self._ensure_loaded(local, progress)
        progress(2, 6, "breadth", "正在按目标分钟与昨收重算全市场涨跌家数")
        prices = self._stock_prices.get(local.time()) or {}
        missing = set(self._previous_close) - set(prices)
        if missing:
            raise RuntimeError(
                f"exact target minute is missing for {len(missing)} A-share instruments"
            )
        up = sum(prices[key] > previous for key, previous in self._previous_close.items())
        down = sum(prices[key] < previous for key, previous in self._previous_close.items())
        flat = len(prices) - up - down

        progress(3, 6, "indices", "正在重建四个角色指数与沪深成交额")
        indices: list[dict[str, Any]] = []
        for instrument_id, role, default_name in _INDEX_ROLES:
            point = (self._index_points.get(instrument_id) or {}).get(local.time())
            identity = self._index_previous_close.get(instrument_id)
            if point is None or identity is None:
                raise RuntimeError(f"exact index minute is unavailable for {instrument_id}")
            name, previous = identity
            indices.append(
                {
                    "role": role,
                    "instrument_id": instrument_id,
                    "name": name or default_name,
                    "available": True,
                    "value": point.close,
                    "change_pct": (point.close - previous) / previous * 100,
                    "previous_close": previous,
                    "provider_as_of": local.isoformat(),
                    "quality": "accepted",
                }
            )
        today_amount = 0.0
        for instrument_id in _TURNOVER_INDICES:
            series = self._index_points.get(instrument_id) or {}
            exact = series.get(local.time())
            if exact is None:
                raise RuntimeError(f"turnover index lacks exact minute {instrument_id}")
            today_amount += sum(
                point.amount_cny for minute, point in series.items() if minute <= local.time()
            )

        progress(4, 6, "turnover_baseline", "上一交易日同分钟成交额基线已通过预检")
        progress(5, 6, "rotation", "正在按目标分钟回放已持久化板块轮动")
        risk.update(
            {
                "timestamp": observed.isoformat(),
                "phase": "trading",
                "breadth": {
                    "up_count": up,
                    "down_count": down,
                    "flat_count": flat,
                    "unclassified_count": 0,
                    "total_count": len(prices),
                    "provider_as_of": local.isoformat(),
                    "quality": "accepted",
                    "quality_flags": [],
                },
                "components": {
                    "breadth": {
                        "status": "ready",
                        "quality": "accepted",
                        "provider_as_of": local.isoformat(),
                        "fetched_at": observed.isoformat(),
                    },
                    "rotation": {
                        **rotation_component,
                        "fetched_at": observed.isoformat(),
                    },
                },
            }
        )
        market_data = {
            "timestamp": observed.isoformat(),
            "provider_as_of": local.isoformat(),
            "market_state": {"is_open": True, "phase": "trading"},
            "indices": indices,
            "market_turnover": {
                "available": True,
                "scope": "all_a_shares",
                "metric": "amount_cny",
                "as_of": local.strftime("%H:%M"),
                "today_date": local.date().isoformat(),
                "previous_date": previous_date.isoformat(),
                "today_amount_cny": today_amount,
                "previous_same_time_amount_cny": previous_amount,
                "difference_cny": today_amount - previous_amount,
                "direction": "expand" if today_amount > previous_amount else "shrink" if today_amount < previous_amount else "flat",
                "label": "放量" if today_amount > previous_amount else "缩量" if today_amount < previous_amount else "持平",
            },
        }
        progress(6, 6, "contract", "正在执行 market_watch.v1 严格契约校验")
        snapshot = build_market_watch_snapshot(
            market_data,
            risk,
            as_of=local,
            snapshot_id=f"mw-recovery:{local:%Y%m%d-%H%M}",
        )
        components = {item.component: item for item in snapshot.freshness.components}
        if snapshot.market_state.phase is not MarketPhase.TRADING:
            raise RuntimeError("reconstructed snapshot is not in the target trading phase")
        if snapshot.freshness.status in {FreshnessStatus.STALE, FreshnessStatus.UNAVAILABLE}:
            raise RuntimeError("reconstructed snapshot failed freshness validation")
        if any(
            components[name].status in {FreshnessStatus.STALE, FreshnessStatus.UNAVAILABLE}
            for name in ("indices", "breadth", "turnover", "rotation")
        ):
            raise RuntimeError("reconstructed snapshot has an unavailable required component")
        if len(snapshot.indices) != 4 or not snapshot.breadth.available or not snapshot.turnover.available or not snapshot.rotation.sectors:
            raise RuntimeError("reconstructed snapshot is missing required exact market facts")
        return snapshot

    def _ensure_loaded(self, target: datetime, progress: ProgressCallback) -> None:
        with self._lock:
            if self._trade_date == target.date() and target.time() in self._stock_prices:
                progress(1, 6, "source_cache", "同日精确分钟源已加载，复用本轮缓存")
                return
            requested_times = {
                item.astimezone(SHANGHAI).time().replace(second=0, microsecond=0)
                for item in self._target_minutes(target.date())
            }
            requested_times.add(target.time())
            observed = self._clock().astimezone(SHANGHAI)
            if self._source_cache is not None:
                cached = self._source_cache.read(target.date(), requested_times)
                if cached is not None:
                    self._restore_source_cache(cached)
                    progress(
                        1,
                        6,
                        "source_cache",
                        "已复用进程重启前保存的同日精确分钟源",
                    )
                    return
            universe = fetch_a_share_universe_snapshot(
                now=observed,
                trade_date=target.date(),
            )
            provider_as_of = universe.metadata.provider_as_of
            if provider_as_of is None or provider_as_of.astimezone(SHANGHAI).date() != target.date() or provider_as_of.astimezone(SHANGHAI).time() < time(15, 0):
                raise RuntimeError("A-share universe does not prove the same-day close")
            previous_close = {
                item.instrument_id: item.previous_close
                for item in universe.quotes
                if item.previous_close is not None
            }
            if len(previous_close) != len(universe.quotes) or universe.excluded_row_count:
                raise RuntimeError("A-share universe lacks complete previous-close coverage")
            if (
                self._partial_trade_date == target.date()
                and self._partial_previous_close == previous_close
            ):
                stock_prices = {
                    minute: dict(self._partial_stock_prices.get(minute) or {})
                    for minute in requested_times
                }
            else:
                stock_prices = {minute: {} for minute in requested_times}
            self._partial_trade_date = target.date()
            self._partial_previous_close = previous_close
            self._partial_stock_prices = stock_prices
            codes = tuple(
                instrument_id
                for instrument_id in previous_close
                if any(
                    instrument_id not in stock_prices[minute]
                    for minute in requested_times
                )
            )
            batches = tuple(codes[offset:offset + 40] for offset in range(0, len(codes), 40))
            completed_batches = 0
            loaded_instruments = len(previous_close) - len(codes)

            def accept_batch(
                batch: tuple[str, ...],
                loaded: dict[str, Any],
                omitted_count: int,
            ) -> None:
                nonlocal completed_batches, loaded_instruments
                for instrument_id, series in loaded.items():
                    if series.trading_date != target.date():
                        raise RuntimeError(f"minute curve belongs to another date: {instrument_id}")
                    exact = {point.minute: point.price for point in series.points}
                    for minute in requested_times:
                        if minute in exact:
                            stock_prices[minute][instrument_id] = exact[minute]
                completed_batches += 1
                loaded_instruments += len(batch)
                progress(
                    completed_batches,
                    len(batches),
                    "stock_minutes",
                    f"已加载 {loaded_instruments}/{len(previous_close)} 只，逐只回退 {omitted_count} 只",
                )

            worker_count = (
                1 if self._batch_pause_seconds > 0 else self._batch_concurrency
            )
            if worker_count == 1:
                for number, batch in enumerate(batches, start=1):
                    loaded, omitted_count = self._load_stock_batch(
                        batch,
                        observed,
                        target.date(),
                    )
                    accept_batch(batch, loaded, omitted_count)
                    if number < len(batches) and self._batch_pause_seconds > 0:
                        self._sleep(self._batch_pause_seconds)
            else:
                with ThreadPoolExecutor(
                    max_workers=worker_count,
                    thread_name_prefix="tradex-recovery-minute",
                ) as executor:
                    futures = {
                        executor.submit(
                            self._load_stock_batch,
                            batch,
                            observed,
                            target.date(),
                        ): batch
                        for batch in batches
                    }
                    try:
                        for future in as_completed(futures):
                            batch = futures[future]
                            loaded, omitted_count = future.result()
                            accept_batch(batch, loaded, omitted_count)
                    except Exception:
                        for future in futures:
                            future.cancel()
                        raise

            overview = fetch_market_overview(now=observed)
            identities = {
                item.instrument_id: (item.name, item.previous_close)
                for item in overview.indices
                if item.available and item.previous_close is not None
            }
            index_points: dict[str, dict[time, Any]] = {}
            for instrument_id in dict.fromkeys((*[item[0] for item in _INDEX_ROLES], *_TURNOVER_INDICES)):
                series = fetch_index_intraday_series(
                    instrument_id,
                    now=observed,
                    days=1,
                )
                points = {
                    item.minute: item
                    for item in series.points
                    if item.trading_date == target.date()
                }
                if not requested_times.issubset(points):
                    raise RuntimeError(f"index curve omits requested exact minutes: {instrument_id}")
                index_points[instrument_id] = points
            missing_identities = {item[0] for item in _INDEX_ROLES} - set(identities)
            if missing_identities:
                raise RuntimeError(f"final index overview lacks previous close: {sorted(missing_identities)}")
            incomplete_minutes = {
                minute: len(previous_close) - len(stock_prices.get(minute) or {})
                for minute in requested_times
                if set(stock_prices.get(minute) or {}) != set(previous_close)
            }
            if incomplete_minutes:
                detail = ", ".join(
                    f"{minute:%H:%M} missing {count}"
                    for minute, count in sorted(incomplete_minutes.items())[:8]
                )
                raise RuntimeError("same-day stock minute matrix is incomplete: " + detail)
            if self._source_cache is not None:
                self._source_cache.record(
                    trade_date=target.date(),
                    provider_as_of=provider_as_of,
                    previous_close=previous_close,
                    stock_prices=stock_prices,
                    index_points=index_points,
                    index_previous_close=identities,
                )
            self._trade_date = target.date()
            self._previous_close = previous_close
            self._stock_prices = stock_prices
            self._index_points = index_points
            self._index_previous_close = identities
            self._partial_trade_date = None
            self._partial_previous_close = {}
            self._partial_stock_prices = {}

    def _restore_source_cache(self, payload: Mapping[str, Any]) -> None:
        trade_date = date.fromisoformat(str(payload["trade_date"]))
        previous_close = {
            str(instrument_id): float(value)
            for instrument_id, value in dict(payload["previous_close"]).items()
        }
        stock_prices = {
            time.fromisoformat(str(minute)): {
                str(instrument_id): float(value)
                for instrument_id, value in dict(values).items()
            }
            for minute, values in dict(payload["stock_prices"]).items()
        }
        index_points = {
            str(instrument_id): {
                time.fromisoformat(str(minute)): _CachedIndexPoint(
                    close=float(point["close"]),
                    amount_cny=float(point["amount_cny"]),
                )
                for minute, point in dict(series).items()
            }
            for instrument_id, series in dict(payload["index_points"]).items()
        }
        index_previous_close = {
            str(instrument_id): (str(value[0]), float(value[1]))
            for instrument_id, value in dict(payload["index_previous_close"]).items()
        }
        self._trade_date = trade_date
        self._previous_close = previous_close
        self._stock_prices = stock_prices
        self._index_points = index_points
        self._index_previous_close = index_previous_close
        self._partial_trade_date = None
        self._partial_previous_close = {}
        self._partial_stock_prices = {}

    def _load_stock_batch(
        self,
        batch: tuple[str, ...],
        observed: datetime,
        trading_date: date,
    ) -> tuple[dict[str, Any], int]:
        loaded = self._load_batch_with_backoff(batch, observed)
        omitted = [item for item in batch if item not in loaded]
        for instrument_id in omitted:
            loaded[instrument_id] = fetch_intraday_minute_series(
                instrument_id,
                now=observed,
                use_cache=False,
                expected_trading_date=trading_date,
            )
        return loaded, len(omitted)

    def _load_batch_with_backoff(self, batch: tuple[str, ...], observed: datetime):
        last: BaseException | None = None
        for attempt in range(3):
            try:
                return fetch_intraday_minute_series_batch_partial(
                    batch,
                    now=observed,
                )
            except Exception as exc:  # noqa: BLE001 - bounded provider backoff
                last = exc
                if "429" not in str(exc) or attempt == 2:
                    raise
                self._sleep(20.0 * (attempt + 1))
        raise RuntimeError("minute batch retry exhausted") from last

    def _previous_turnover(self, target: datetime) -> tuple[date, float]:
        for item in self._history.list_dates(limit=8):
            candidate_date = date.fromisoformat(item["trade_date"])
            if candidate_date >= target.date():
                continue
            records = self._history.get_collection_records(candidate_date)
            accepted = {
                datetime.fromisoformat(row["minute_bucket"]).time(): row
                for row in records
                if row.get("record_kind") == "accepted_real"
            }
            if target.time() not in accepted:
                continue
            for row in self._history.get_timeline(candidate_date):
                if datetime.fromisoformat(row["minute_bucket"]).time() != target.time():
                    continue
                turnover = row["payload"].get("turnover") or {}
                amount = turnover.get("today_amount_cny")
                if turnover.get("available") and turnover.get("today_date") == candidate_date.isoformat() and turnover.get("as_of") == target.strftime("%H:%M") and isinstance(amount, (int, float)) and amount > 0:
                    return candidate_date, float(amount)
        raise RuntimeError("no accepted previous-trading-day same-minute turnover baseline")

    @staticmethod
    def _previous_trading_date(value: date) -> date:
        candidate = value - timedelta(days=1)
        for _ in range(10):
            status = calendar_day_status(candidate)
            if status is CalendarDayStatus.VERIFIED_TRADING_DAY:
                return candidate
            if status is CalendarDayStatus.UNKNOWN:
                raise RuntimeError("previous trading day is outside the verified calendar")
            candidate -= timedelta(days=1)
        raise RuntimeError("previous trading day is unavailable")


__all__ = ["HistoricalTrajectoryUnavailable", "SameDayPostCloseReconstructor"]
