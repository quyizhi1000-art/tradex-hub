"""Unified market-overview gateway and the legacy dashboard view adapter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from threading import Condition
from typing import Any
from zoneinfo import ZoneInfo

from tradex.market_calendar import (
    CalendarDayStatus,
    a_share_session,
    calendar_day_status,
)

from .contracts import (
    ContractMetadata,
    IndexDailyAmountSeriesV1,
    IndexIntradayAmountSeriesV1,
    IndexQuoteV1,
    MarketOverviewV1,
    MarketStateV1,
    MarketTurnoverV1,
)
from .providers.market_overview import (
    finite_number,
    map_indices,
    map_participation_indices,
    parse_provider_time,
    provider_watermark,
)
from .providers.market_turnover import (
    map_index_daily_amount,
    map_index_intraday_amount,
)
from .quality import DataQualityError, assess_market_overview


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_LEGACY_INDEX_CODES = {
    "000001.SH": "sh000001",
    "399001.SZ": "sz399001",
    "399006.SZ": "sz399006",
}
_SOURCE_LABELS = {
    "akshare": "AKShare 行情",
    "tencent_http": "腾讯行情",
    "ths_fuyao": "同花顺扶摇",
    "biying": "必盈",
}
_TURNOVER_INSTRUMENTS = {
    "000001.SH": "sh000001",
    "399001.SZ": "sz399001",
}


class TurnoverBaselineUnavailable(RuntimeError):
    """The immutable previous-day turnover baseline could not be acquired."""


@dataclass(frozen=True)
class PreviousTurnoverBaseline:
    cache_key: date
    previous_date: date
    series: tuple[IndexIntradayAmountSeriesV1, ...] = ()
    today_close_amount_cny: float | None = None
    previous_close_amount_cny: float | None = None

    def __post_init__(self) -> None:
        minute_mode = bool(self.series)
        close_mode = (
            self.today_close_amount_cny is not None
            and self.previous_close_amount_cny is not None
        )
        if minute_mode == close_mode:
            raise ValueError("turnover baseline must use exactly one comparison mode")
        if close_mode and (
            self.today_close_amount_cny <= 0
            or self.previous_close_amount_cny <= 0
        ):
            raise ValueError("completed-session turnover amounts must be positive")


@dataclass(frozen=True)
class _MarketOverviewCandidate:
    records: tuple[dict[str, Any], ...]
    indices: tuple[IndexQuoteV1, ...]
    provider_as_of: datetime | None
    provider_request_id: str | None


class TurnoverBaselineCache:
    """Own the once-per-trading-day historical baseline and its single-flight."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._entry: PreviousTurnoverBaseline | None = None
        self._loading_key: date | None = None
        self._generation = 0
        self._failures: dict[tuple[date, int], Exception] = {}

    def get_or_load(
        self,
        *,
        trading_date: date,
        loader: Callable[[], PreviousTurnoverBaseline],
    ) -> PreviousTurnoverBaseline:
        """Return one atomic baseline; a failed cohort retries next request."""

        waited_generation: int | None = None
        with self._condition:
            while True:
                failed_key = (
                    (trading_date, waited_generation)
                    if waited_generation is not None
                    else None
                )
                if failed_key is not None and failed_key in self._failures:
                    error = self._failures[failed_key]
                    raise TurnoverBaselineUnavailable(
                        "previous turnover baseline unavailable"
                    ) from error
                if self._entry is not None and self._entry.cache_key == trading_date:
                    return self._entry
                if self._loading_key is None:
                    self._loading_key = trading_date
                    self._generation += 1
                    generation = self._generation
                    break
                if self._loading_key == trading_date:
                    waited_generation = self._generation
                self._condition.wait()

        try:
            entry = loader()
            if entry.cache_key != trading_date:
                raise ValueError("turnover baseline cache key mismatch")
        except Exception as error:
            with self._condition:
                self._failures[(trading_date, generation)] = error
                failures_for_date = sorted(
                    key for key in self._failures if key[0] == trading_date
                )
                for old_key in failures_for_date[:-4]:
                    self._failures.pop(old_key, None)
                self._loading_key = None
                self._condition.notify_all()
            raise TurnoverBaselineUnavailable(
                "previous turnover baseline unavailable"
            ) from error

        with self._condition:
            self._entry = entry
            self._loading_key = None
            self._condition.notify_all()
            return entry


_TURNOVER_BASELINE_CACHE = TurnoverBaselineCache()


def market_state(now: datetime) -> MarketStateV1:
    session = a_share_session(now)
    return MarketStateV1(label=session.label, is_open=session.is_open)


def _sum_amount_until(series: list[dict[str, Any]], trading_date: str, as_of: str) -> float:
    return sum(
        amount
        for item in series or []
        if str(item.get("date") or "") == trading_date
        and str(item.get("time") or "")[:5] <= as_of
        and (amount := finite_number(item.get("amount"))) is not None
    )


def build_market_turnover(
    series_by_instrument: dict[str, list[dict[str, Any]]],
    now: datetime,
) -> MarketTurnoverV1:
    today_date = now.date().isoformat()
    required = {"000001.SH", "399001.SZ"}
    if set(series_by_instrument) != required:
        return MarketTurnoverV1(available=False, reason="沪深成交额数据不完整")

    dates_by_instrument: dict[str, set[str]] = {}
    latest_times: list[str] = []
    for instrument_id, series in series_by_instrument.items():
        dates = {str(item.get("date") or "") for item in series if item.get("date")}
        dates_by_instrument[instrument_id] = dates
        today_times = [
            str(item.get("time") or "")[:5]
            for item in series
            if str(item.get("date") or "") == today_date
            and finite_number(item.get("amount")) is not None
        ]
        if not today_times:
            return MarketTurnoverV1(available=False, reason="今日尚无成交额")
        latest_times.append(max(today_times))

    as_of = min(latest_times)
    common_dates = set.intersection(*dates_by_instrument.values())
    previous_dates = sorted(value for value in common_dates if value < today_date)
    if not previous_dates:
        return MarketTurnoverV1(available=False, reason="上一交易日同期数据暂缺")
    previous_date = previous_dates[-1]

    today_amount = sum(
        _sum_amount_until(series, today_date, as_of)
        for series in series_by_instrument.values()
    )
    previous_amount = sum(
        _sum_amount_until(series, previous_date, as_of)
        for series in series_by_instrument.values()
    )
    if today_amount <= 0 or previous_amount <= 0:
        return MarketTurnoverV1(available=False, reason="同期成交额暂缺")

    difference = today_amount - previous_amount
    if difference > 0:
        direction, label = "expand", "放量"
    elif difference < 0:
        direction, label = "shrink", "缩量"
    else:
        direction, label = "flat", "持平"

    return MarketTurnoverV1(
        available=True,
        scope="all_a_shares",
        metric="amount_cny",
        as_of=as_of,
        today_date=today_date,
        previous_date=previous_date,
        today_amount_cny=today_amount,
        previous_same_time_amount_cny=previous_amount,
        difference_cny=difference,
        direction=direction,
        label=label,
    )


def _unavailable_turnover(reason: str) -> MarketTurnoverV1:
    return MarketTurnoverV1(available=False, reason=reason)


def _direction(difference: float) -> tuple[str, str]:
    if difference > 0:
        return "expand", "放量"
    if difference < 0:
        return "shrink", "缩量"
    return "flat", "持平"


def _latest_completed_trading_date(reference_date: date) -> date | None:
    candidate = reference_date - timedelta(days=1)
    while True:
        status = calendar_day_status(candidate)
        if status is CalendarDayStatus.VERIFIED_TRADING_DAY:
            return candidate
        if status is CalendarDayStatus.UNVERIFIED:
            return None
        candidate -= timedelta(days=1)


def _uses_completed_session_turnover(trading_date: date, now: datetime) -> bool:
    reference_date = now.astimezone(_SHANGHAI).date()
    return trading_date < reference_date or (
        trading_date == reference_date
        and a_share_session(now).phase.value == "closed"
    )


def _live_turnover_context(
    indices: tuple[IndexQuoteV1, ...],
    now: datetime,
) -> tuple[date, time, float] | MarketTurnoverV1:
    by_instrument = {item.instrument_id: item for item in indices}
    required = [by_instrument.get(item) for item in _TURNOVER_INSTRUMENTS]
    if any(item is None or not item.available for item in required):
        return _unavailable_turnover("沪深实时成交额数据不完整")
    if any(item.amount_cny is None or item.amount_cny <= 0 for item in required):
        return _unavailable_turnover("沪深实时成交额暂缺")
    if any(item.provider_as_of is None for item in required):
        return _unavailable_turnover("沪深成交额更新时间暂缺")

    local_times = [item.provider_as_of.astimezone(_SHANGHAI) for item in required]
    provider_dates = {item.date() for item in local_times}
    if len(provider_dates) != 1:
        return _unavailable_turnover("沪深成交额交易日不一致")
    trading_date = next(iter(provider_dates))
    reference_date = now.astimezone(_SHANGHAI).date()
    if calendar_day_status(trading_date) is not CalendarDayStatus.VERIFIED_TRADING_DAY:
        return _unavailable_turnover("沪深成交额不是有效交易日数据")
    if trading_date != reference_date and (
        _requires_live_turnover(now)
        or trading_date != _latest_completed_trading_date(reference_date)
    ):
        return _unavailable_turnover("沪深成交额不是当前交易日数据")
    provider_minutes = {
        item.time().replace(second=0, microsecond=0) for item in local_times
    }
    if len(provider_minutes) != 1:
        return _unavailable_turnover("沪深成交额更新时间不一致")
    as_of = (
        time(15, 0)
        if _uses_completed_session_turnover(trading_date, now)
        else next(iter(provider_minutes))
    )
    today_amount = sum(float(item.amount_cny) for item in required)
    return trading_date, as_of, today_amount


def _requires_live_turnover(now: datetime) -> bool:
    session = a_share_session(now)
    local_time = now.astimezone(_SHANGHAI).time().replace(tzinfo=None)
    return session.is_trading_day and local_time >= time(9, 30)


def _map_market_overview_candidate(
    payload: Any,
    provider: str,
    *,
    now: datetime,
) -> _MarketOverviewCandidate:
    if payload is None or not hasattr(payload, "to_dict"):
        raise DataQualityError("行情源返回了无法识别的数据")
    records = payload.to_dict(orient="records")
    frame_provider_as_of = parse_provider_time(
        getattr(payload, "attrs", {}).get("provider_as_of")
    )
    provider_as_of = provider_watermark(records) or frame_provider_as_of
    indices = map_indices(records, provider)
    required_indices = [
        item for item in indices if item.instrument_id in _TURNOVER_INSTRUMENTS
    ]
    if (
        frame_provider_as_of is not None
        and required_indices
        and all(item.provider_as_of is None for item in required_indices)
    ):
        indices = tuple(
            item.model_copy(update={"provider_as_of": frame_provider_as_of})
            if item.available and item.provider_as_of is None
            else item
            for item in indices
        )
    if _requires_live_turnover(now):
        context = _live_turnover_context(indices, now)
        if isinstance(context, MarketTurnoverV1):
            raise DataQualityError(context.reason or "沪深实时成交额不可用")
    request_id = getattr(payload, "attrs", {}).get("request_id")
    return _MarketOverviewCandidate(
        records=tuple(records),
        indices=indices,
        provider_as_of=provider_as_of,
        provider_request_id=str(request_id) if request_id else None,
    )


def _load_previous_turnover_baseline(
    *,
    router: Any,
    trading_date: date,
    fetched_at: datetime,
    turnover_fetcher: Callable[..., list[dict[str, Any]]] | None = None,
) -> PreviousTurnoverBaseline:
    series: list[IndexIntradayAmountSeriesV1] = []
    for instrument_id, symbol in _TURNOVER_INSTRUMENTS.items():
        validator = lambda payload, provider, instrument_id=instrument_id: (
            map_index_intraday_amount(
                payload,
                provider,
                instrument_id=instrument_id,
                fetched_at=fetched_at,
            )
        )
        if turnover_fetcher is None:
            canonical, _provider = router.route_validated(
                "index_intraday_amount",
                validator,
                symbol=symbol,
                days=5,
            )
        else:
            canonical = validator(
                turnover_fetcher(symbol=symbol, days=5),
                "injected_fixture",
            )
        series.append(canonical)

    dates_by_instrument = [
        {point.trading_date for point in item.points}
        for item in series
    ]
    common_dates = set.intersection(*dates_by_instrument)
    previous_dates = sorted(item for item in common_dates if item < trading_date)
    if not previous_dates:
        raise ValueError("no common previous trading date in turnover history")
    return PreviousTurnoverBaseline(
        cache_key=trading_date,
        previous_date=previous_dates[-1],
        series=tuple(series),
    )


def _load_completed_turnover_baseline(
    *,
    router: Any,
    trading_date: date,
    fetched_at: datetime,
) -> PreviousTurnoverBaseline:
    series: list[IndexDailyAmountSeriesV1] = []
    for instrument_id, symbol in _TURNOVER_INSTRUMENTS.items():
        canonical, _provider = router.route_validated(
            "index_daily_amount",
            lambda payload, provider, instrument_id=instrument_id: (
                map_index_daily_amount(
                    payload,
                    provider,
                    instrument_id=instrument_id,
                    fetched_at=fetched_at,
                )
            ),
            symbol=symbol,
            days=5,
        )
        series.append(canonical)

    amounts_by_instrument = [
        {point.trading_date: point.amount_cny for point in item.points}
        for item in series
    ]
    common_dates = set.intersection(
        *(set(item) for item in amounts_by_instrument)
    )
    if trading_date not in common_dates:
        raise ValueError("completed turnover history does not include the current session")
    previous_dates = sorted(item for item in common_dates if item < trading_date)
    if not previous_dates:
        raise ValueError("completed turnover history has no previous session")
    previous_date = previous_dates[-1]
    return PreviousTurnoverBaseline(
        cache_key=trading_date,
        previous_date=previous_date,
        today_close_amount_cny=sum(
            item[trading_date] for item in amounts_by_instrument
        ),
        previous_close_amount_cny=sum(
            item[previous_date] for item in amounts_by_instrument
        ),
    )


def build_market_turnover_from_indices(
    indices: tuple[IndexQuoteV1, ...],
    baseline: PreviousTurnoverBaseline,
    now: datetime,
) -> MarketTurnoverV1:
    """Combine live canonical amounts with one immutable prior-day curve."""

    context = _live_turnover_context(indices, now)
    if isinstance(context, MarketTurnoverV1):
        return context
    trading_date, as_of, today_amount = context
    if baseline.cache_key != trading_date:
        raise ValueError("turnover baseline belongs to another trading date")

    if baseline.today_close_amount_cny is not None:
        as_of = time(15, 0)
        today_amount = baseline.today_close_amount_cny
        previous_amount = baseline.previous_close_amount_cny or 0.0
    else:
        previous_amount = sum(
            point.amount_cny
            for series in baseline.series
            for point in series.points
            if point.trading_date == baseline.previous_date and point.minute <= as_of
        )
    if previous_amount <= 0:
        return _unavailable_turnover("上一交易日同期成交额暂缺")

    difference = today_amount - previous_amount
    direction, label = _direction(difference)
    return MarketTurnoverV1(
        available=True,
        scope="all_a_shares",
        metric="amount_cny",
        as_of=as_of.strftime("%H:%M"),
        today_date=trading_date.isoformat(),
        previous_date=baseline.previous_date.isoformat(),
        today_amount_cny=today_amount,
        previous_same_time_amount_cny=previous_amount,
        difference_cny=difference,
        direction=direction,
        label=label,
    )


def fetch_market_overview(
    *,
    now: datetime | None = None,
    router: Any | None = None,
    turnover_fetcher: Callable[..., list[dict[str, Any]]] | None = None,
    turnover_cache: TurnoverBaselineCache | None = None,
) -> MarketOverviewV1:
    """Fetch providers once and return a validated provider-neutral snapshot."""

    if now is None:
        now = datetime.now(_SHANGHAI)
    if now.tzinfo is None:
        raise ValueError("market gateway now must include a timezone")

    if router is None:
        from tradex.data_sources import get_router, register_all_sources

        register_all_sources()
        router = get_router()

    candidate, provider = router.route_validated(
        "market_overview",
        lambda payload, source: _map_market_overview_candidate(
            payload,
            source,
            now=now,
        ),
    )
    records = list(candidate.records)
    indices = candidate.indices
    context = _live_turnover_context(indices, now)
    if isinstance(context, MarketTurnoverV1):
        turnover = context
    else:
        trading_date, _as_of, _today_amount = context
        cache = turnover_cache or (
            TurnoverBaselineCache()
            if turnover_fetcher is not None
            else _TURNOVER_BASELINE_CACHE
        )
        try:
            baseline = cache.get_or_load(
                trading_date=trading_date,
                loader=lambda: (
                    _load_completed_turnover_baseline(
                        router=router,
                        trading_date=trading_date,
                        fetched_at=now,
                    )
                    if _uses_completed_session_turnover(trading_date, now)
                    else _load_previous_turnover_baseline(
                        router=router,
                        trading_date=trading_date,
                        fetched_at=now,
                        turnover_fetcher=turnover_fetcher,
                    )
                ),
            )
        except TurnoverBaselineUnavailable:
            turnover = _unavailable_turnover(
                "上一交易日同期成交额暂不可用，稍后自动重试"
            )
        else:
            # Calculation and contract failures remain visible as defects.  Only
            # provider/baseline acquisition is allowed to degrade this component.
            turnover = build_market_turnover_from_indices(indices, baseline, now)

    participation = map_participation_indices(records)
    provider_as_of = candidate.provider_as_of
    provider_request_id = candidate.provider_request_id
    quality, flags = assess_market_overview(
        indices=indices,
        participation_indices=participation,
        turnover=turnover,
        provider_as_of=provider_as_of,
    )

    return MarketOverviewV1(
        metadata=ContractMetadata(
            provider=provider,
            provider_request_id=provider_request_id,
            provider_as_of=provider_as_of,
            fetched_at=now,
            quality=quality,
            quality_flags=flags,
        ),
        market_state=market_state(now),
        indices=indices,
        participation_indices=participation,
        market_turnover=turnover,
    )


def _legacy_code(instrument_id: str) -> str:
    return _LEGACY_INDEX_CODES.get(
        instrument_id,
        instrument_id.split(".", 1)[1].lower() + instrument_id.split(".", 1)[0],
    )


def index_quotes_to_legacy(indices: Any) -> list[dict[str, Any]]:
    """Map canonical index quotes to the existing dashboard representation."""

    return [
        {
            "code": _legacy_code(item.instrument_id),
            "name": item.name,
            "available": item.available,
            "provider_as_of": (
                item.provider_as_of.isoformat(timespec="seconds")
                if item.provider_as_of
                else None
            ),
            **(
                {
                    "price": item.value,
                    "change": item.change,
                    "change_pct": item.change_pct,
                    "previous_close": item.previous_close,
                    "open": item.open,
                    "high": item.high,
                    "low": item.low,
                    "amount": item.amount_cny,
                }
                if item.available
                else {}
            ),
        }
        for item in indices
    ]


def participation_indices_to_legacy(indices: Any) -> list[dict[str, Any]]:
    """Map canonical participation indices to the existing Chinese fields."""

    return [
        {
            "名称": item.name,
            "代码": _legacy_code(item.instrument_id),
            "涨跌幅": item.change_pct,
            "更新时间": (
                item.provider_as_of.isoformat(timespec="seconds")
                if item.provider_as_of
                else None
            ),
        }
        for item in indices
    ]


def market_turnover_to_legacy(turnover: MarketTurnoverV1) -> dict[str, Any]:
    """Map explicit CNY turnover fields to the current dashboard contract."""

    if turnover.available:
        return {
            "available": True,
            "scope": turnover.scope,
            "metric": "amount",
            "as_of": turnover.as_of,
            "today_date": turnover.today_date,
            "previous_date": turnover.previous_date,
            "today_amount": turnover.today_amount_cny,
            "previous_same_time_amount": turnover.previous_same_time_amount_cny,
            "difference": turnover.difference_cny,
            "direction": turnover.direction,
            "label": turnover.label,
        }
    return {"available": False, "reason": turnover.reason}


def market_overview_to_legacy_payload(snapshot: MarketOverviewV1) -> dict[str, Any]:
    """Preserve the existing dashboard JSON while consumers migrate."""

    meta = snapshot.metadata
    return {
        "timestamp": meta.fetched_at.isoformat(timespec="seconds"),
        "provider_as_of": (
            meta.provider_as_of.isoformat(timespec="seconds")
            if meta.provider_as_of
            else None
        ),
        "market_state": snapshot.market_state.model_dump(),
        "source": meta.provider,
        "source_label": _SOURCE_LABELS.get(meta.provider, meta.provider),
        "indices": index_quotes_to_legacy(snapshot.indices),
        "participation_indices": participation_indices_to_legacy(
            snapshot.participation_indices
        ),
        "market_turnover": market_turnover_to_legacy(snapshot.market_turnover),
        "contract": meta.contract,
        "schema_version": meta.schema_version,
        "quality": meta.quality.value,
        "quality_flags": list(meta.quality_flags),
    }


__all__ = [
    "PreviousTurnoverBaseline",
    "TurnoverBaselineCache",
    "build_market_turnover",
    "build_market_turnover_from_indices",
    "fetch_market_overview",
    "index_quotes_to_legacy",
    "market_overview_to_legacy_payload",
    "market_turnover_to_legacy",
    "market_state",
    "participation_indices_to_legacy",
]
