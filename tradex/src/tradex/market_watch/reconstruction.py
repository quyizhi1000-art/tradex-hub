"""Exact same-day post-close reconstruction for missed market-watch minutes."""

from __future__ import annotations

import time as time_module
from collections.abc import Callable, Iterable
from datetime import date, datetime, time
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

from tradex.data_gateway import (
    fetch_a_share_universe_snapshot,
    fetch_index_intraday_series,
    fetch_intraday_minute_series,
    fetch_intraday_minute_series_batch_partial,
    fetch_market_overview,
)

from .analysis import build_market_watch_snapshot
from .contracts import FreshnessStatus, MarketPhase, MarketWatchSnapshotV1
from .history import MarketWatchHistoryStore


SHANGHAI = ZoneInfo("Asia/Shanghai")
ProgressCallback = Callable[[int, int, str, str | None], None]

_INDEX_ROLES = (
    ("000001.SH", "broad_market", "上证指数"),
    ("000300.SH", "large_cap", "沪深300"),
    ("000852.SH", "small_cap", "中证1000"),
    ("399006.SZ", "growth", "创业板指"),
)
_TURNOVER_INDICES = ("000001.SH", "399001.SZ")


class SameDayPostCloseReconstructor:
    """Load exact same-day source curves once and rebuild only requested gaps."""

    def __init__(
        self,
        *,
        history: MarketWatchHistoryStore,
        target_minutes: Callable[[date], Iterable[datetime]],
        rotation_loader: Callable[[datetime], dict[str, Any]],
        clock: Callable[[], datetime],
        sleep: Callable[[float], None] = time_module.sleep,
        batch_pause_seconds: float = 2.0,
    ) -> None:
        self._history = history
        self._target_minutes = target_minutes
        self._rotation_loader = rotation_loader
        self._clock = clock
        self._sleep = sleep
        self._batch_pause_seconds = float(batch_pause_seconds)
        self._lock = Lock()
        self._trade_date: date | None = None
        self._previous_close: dict[str, float] = {}
        self._stock_prices: dict[time, dict[str, float]] = {}
        self._index_points: dict[str, dict[time, Any]] = {}
        self._index_previous_close: dict[str, tuple[str, float]] = {}

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

        progress(4, 6, "turnover_baseline", "正在读取上一交易日同分钟已验收成交额")
        previous_date, previous_amount = self._previous_turnover(local)
        progress(5, 6, "rotation", "正在按目标分钟回放已持久化板块轮动")
        risk = dict(self._rotation_loader(local))
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
                        "status": "ready",
                        "quality": "accepted",
                        "provider_as_of": local.isoformat(),
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
            stock_prices = {minute: {} for minute in requested_times}
            codes = tuple(previous_close)
            batches = tuple(codes[offset:offset + 40] for offset in range(0, len(codes), 40))
            for number, batch in enumerate(batches, start=1):
                loaded = self._load_batch_with_backoff(batch, observed)
                omitted = [item for item in batch if item not in loaded]
                for instrument_id in omitted:
                    loaded[instrument_id] = fetch_intraday_minute_series(
                        instrument_id,
                        now=observed,
                        use_cache=False,
                        expected_trading_date=target.date(),
                    )
                for instrument_id, series in loaded.items():
                    if series.trading_date != target.date():
                        raise RuntimeError(f"minute curve belongs to another date: {instrument_id}")
                    exact = {point.minute: point.price for point in series.points}
                    for minute in requested_times:
                        if minute in exact:
                            stock_prices[minute][instrument_id] = exact[minute]
                progress(
                    number,
                    len(batches),
                    "stock_minutes",
                    f"已加载 {min(number * 40, len(codes))}/{len(codes)} 只，逐只回退 {len(omitted)} 只",
                )
                if number < len(batches) and self._batch_pause_seconds > 0:
                    self._sleep(self._batch_pause_seconds)

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
            self._trade_date = target.date()
            self._previous_close = previous_close
            self._stock_prices = stock_prices
            self._index_points = index_points
            self._index_previous_close = identities

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


__all__ = ["SameDayPostCloseReconstructor"]
