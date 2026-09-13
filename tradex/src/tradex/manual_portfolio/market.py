"""Collector-owned portfolio market materialization and alert policy."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from tradex.data_gateway.contracts import IntradayMinuteSeriesV1, QualityStatus
from tradex.data_gateway.intraday import fetch_intraday_minute_series_batch_partial
from tradex.data_gateway.market_universe import fetch_a_share_universe_snapshot

from .contracts import (
    ManualPortfolioAlertV1,
    ManualPortfolioMarketSnapshotV1,
    ManualPortfolioQuoteV1,
    ManualPortfolioSampleV1,
    ManualPortfolioSessionPathV1,
)
from .store import ManualPortfolioStore, digest, portfolio_revision


SHANGHAI = ZoneInfo("Asia/Shanghai")
ALERT_CHANGE_THRESHOLD_PCT = 0.8
ALERT_COOLDOWN = timedelta(minutes=30)
STALE_AFTER = timedelta(minutes=5)


def _is_open(value: datetime) -> bool:
    local = value.astimezone(SHANGHAI).time().replace(tzinfo=None)
    return time(9, 30) <= local <= time(11, 30) or time(13, 0) <= local <= time(15, 0)


def _sample_time(series: IntradayMinuteSeriesV1, index: int, now: datetime) -> datetime:
    trading_date = series.trading_date or now.astimezone(SHANGHAI).date()
    return datetime.combine(trading_date, series.points[index].minute, tzinfo=SHANGHAI)


def _realtime_quote_is_stale(metadata: Any, now: datetime) -> bool:
    provider_as_of = metadata.provider_as_of
    if provider_as_of is None:
        return True
    observed = now.astimezone(SHANGHAI)
    source = provider_as_of.astimezone(SHANGHAI)
    if source.date() != observed.date():
        return True
    return _is_open(observed) and observed - source > STALE_AFTER


def _canonical_change_is_consistent(quote: Any) -> bool:
    if quote.previous_close in (None, 0) or quote.change_pct is None:
        return False
    expected = (quote.last / quote.previous_close - 1.0) * 100.0
    return abs(expected - quote.change_pct) <= 0.03


def _return_pct(end: float, start: float | None) -> float | None:
    if start in (None, 0):
        return None
    return (end / start - 1.0) * 100.0


def _session_path(
    series: IntradayMinuteSeriesV1,
    *,
    previous_close: float | None,
    now: datetime,
) -> ManualPortfolioSessionPathV1:
    points = series.points
    prices = [point.price for point in points]
    high_index = max(range(len(points)), key=lambda index: points[index].price)
    low_index = min(range(len(points)), key=lambda index: points[index].price)
    high = prices[high_index]
    low = prices[low_index]
    span = high - low
    close_location = (prices[-1] - low) / span if span > 0 else 0.5
    morning_points = [point for point in points if point.minute <= time(11, 30)]
    afternoon_points = [point for point in points if point.minute >= time(13, 0)]
    closing_reference = next(
        (
            point
            for point in reversed(points)
            if point.minute <= time(14, 30)
        ),
        None,
    )
    peak = prices[0]
    trough = prices[0]
    max_drawdown = 0.0
    max_rebound = 0.0
    for price in prices:
        peak = max(peak, price)
        trough = min(trough, price)
        max_drawdown = min(max_drawdown, _return_pct(price, peak) or 0.0)
        max_rebound = max(max_rebound, _return_pct(price, trough) or 0.0)
    flags = []
    if points[-1].minute < time(15, 0):
        flags.append("session_path_not_final")
    if not afternoon_points:
        flags.append("afternoon_path_missing")
    return ManualPortfolioSessionPathV1(
        sample_count=len(points),
        opening_reference=prices[0],
        close_reference=prices[-1],
        high_reference=high,
        low_reference=low,
        high_at=_sample_time(series, high_index, now),
        low_at=_sample_time(series, low_index, now),
        close_location=close_location,
        open_gap_pct=_return_pct(prices[0], previous_close),
        morning_return_pct=(
            _return_pct(morning_points[-1].price, prices[0])
            if morning_points
            else None
        ),
        afternoon_return_pct=(
            _return_pct(prices[-1], afternoon_points[0].price)
            if afternoon_points
            else None
        ),
        closing_30m_return_pct=(
            _return_pct(prices[-1], closing_reference.price)
            if closing_reference is not None and points[-1].minute >= time(14, 30)
            else None
        ),
        max_drawdown_pct=max_drawdown,
        max_rebound_pct=max_rebound,
        quality_flags=tuple(flags),
    )


def _quote(
    series: IntradayMinuteSeriesV1,
    *,
    realtime_quote: Any | None,
    realtime_metadata: Any | None,
    realtime_failure_code: str | None,
    now: datetime,
) -> ManualPortfolioQuoteV1:
    points = series.points
    latest = points[-1]
    samples = tuple(
        ManualPortfolioSampleV1(
            observed_at=_sample_time(series, index, now),
            price=points[index].price,
        )
        for index in range(max(0, len(points) - 2), len(points))
    )
    provider_as_of = series.metadata.provider_as_of
    flags = list(series.metadata.quality_flags)
    stale = False
    if series.trading_date is not None and series.trading_date != now.astimezone(SHANGHAI).date():
        stale = True
        flags.append("trading_date_stale")
    elif _is_open(now):
        if provider_as_of is None or now.astimezone(SHANGHAI) - provider_as_of.astimezone(SHANGHAI) > STALE_AFTER:
            stale = True
            flags.append("provider_timestamp_stale")
        if not samples or now.astimezone(SHANGHAI) - samples[-1].observed_at > STALE_AFTER:
            stale = True
            flags.append("sample_timestamp_stale")
    if stale:
        status = "stale"
        reason = "行情时间已过期，方向性观察已抑制"
    elif series.metadata.quality is QualityStatus.ACCEPTED:
        status = "accepted"
        reason = None
    else:
        status = "degraded"
        reason = "行情可展示但证据不完整，方向性观察已抑制"
    prices = [item.price for item in points]
    change_flags: list[str] = []
    if realtime_quote is None or realtime_metadata is None:
        last_price = latest.price
        previous_close = None
        change = None
        session_high = max(prices)
        session_low = min(prices)
        change_provider = None
        change_request_id = None
        change_provider_as_of = None
        change_status = "unavailable"
        change_flags.append("canonical_realtime_quote_missing")
        if realtime_failure_code is not None:
            change_flags.append(
                f"canonical_realtime_quote_unavailable:{realtime_failure_code}"
            )
    elif _realtime_quote_is_stale(realtime_metadata, now):
        last_price = latest.price
        previous_close = None
        change = None
        session_high = max(prices)
        session_low = min(prices)
        change_provider = realtime_metadata.provider
        change_request_id = realtime_metadata.provider_request_id
        change_provider_as_of = realtime_metadata.provider_as_of
        change_status = "stale"
        change_flags.append("canonical_realtime_quote_stale")
    else:
        last_price = realtime_quote.last
        previous_close = realtime_quote.previous_close
        change = (
            realtime_quote.change_pct
            if _canonical_change_is_consistent(realtime_quote)
            else None
        )
        session_high = realtime_quote.high or max(prices)
        session_low = realtime_quote.low or min(prices)
        change_provider = realtime_metadata.provider if change is not None else None
        change_request_id = realtime_metadata.provider_request_id
        change_provider_as_of = realtime_metadata.provider_as_of
        # Whole-universe degradations such as partial turnover do not degrade
        # an internally consistent price/change/previous-close tuple.
        change_status = "accepted"
        change_flags.extend(realtime_metadata.quality_flags)
        if previous_close is None:
            change_flags.append("previous_close_missing")
        if change is None:
            change_status = "degraded"
            change_flags.append("canonical_session_change_missing_or_inconsistent")
    return ManualPortfolioQuoteV1(
        instrument_id=series.instrument_id,
        trading_date=series.trading_date,
        status=status,
        reason=reason,
        last_price=last_price,
        previous_close=previous_close,
        session_change_pct=change,
        session_high=session_high,
        session_low=session_low,
        provider=series.metadata.provider,
        provider_request_id=series.metadata.provider_request_id,
        provider_as_of=provider_as_of,
        fetched_at=series.metadata.fetched_at,
        session_change_provider=change_provider,
        session_change_provider_request_id=change_request_id,
        session_change_provider_as_of=change_provider_as_of,
        session_change_basis=("canonical_realtime_quote" if change is not None else None),
        session_change_status=change_status,
        session_change_quality_flags=tuple(dict.fromkeys(change_flags)),
        quality_flags=tuple(dict.fromkeys(flags)),
        samples=samples,
        session_path=_session_path(
            series,
            previous_close=previous_close,
            now=now,
        ),
    )


def _maybe_alert(
    quote: ManualPortfolioQuoteV1,
    *,
    store: ManualPortfolioStore,
    now: datetime,
) -> ManualPortfolioAlertV1 | None:
    if quote.status != "accepted" or len(quote.samples) < 2:
        return None
    earlier, latest = quote.samples[-2:]
    change_pct = ((latest.price / earlier.price) - 1) * 100
    if abs(change_pct) < ALERT_CHANGE_THRESHOLD_PCT:
        return None
    direction = "up" if change_pct > 0 else "down"
    previous = store.latest_alert(quote.instrument_id, direction)
    if previous is not None and now - previous.observed_at < ALERT_COOLDOWN:
        return None
    label = "快速上行" if direction == "up" else "快速下行"
    invalidation = (
        f"若价格回到 {earlier.price:.2f} 以下，本次上行观察失效"
        if direction == "up"
        else f"若价格回到 {earlier.price:.2f} 以上，本次下行观察失效"
    )
    alert_id = digest({
        "instrument_id": quote.instrument_id,
        "direction": direction,
        "observed_at": latest.observed_at.isoformat(),
        "price": latest.price,
    })
    return ManualPortfolioAlertV1(
        alert_id=alert_id,
        instrument_id=quote.instrument_id,
        direction=direction,
        observed_at=now,
        title=f"{quote.instrument_id} {label}",
        message=f"最近两个新鲜分钟样本变动 {change_pct:+.2f}%，仅作盘中观察。",
        evidence=(earlier, latest),
        invalidation_condition=invalidation,
    )


def refresh_manual_portfolio_market(
    store: ManualPortfolioStore,
    *,
    now: datetime,
    fetcher: Callable[..., Mapping[str, IntradayMinuteSeriesV1]] = fetch_intraday_minute_series_batch_partial,
    quote_fetcher: Callable[..., Any] | None = None,
) -> ManualPortfolioMarketSnapshotV1:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("manual portfolio refresh time must include a timezone")
    observed = now.astimezone(SHANGHAI)
    entries = store.list_entries(enabled_only=True)
    revision = portfolio_revision(store.list_entries())
    symbols = tuple(item.instrument_id for item in entries)
    fetched: Mapping[str, IntradayMinuteSeriesV1] = {}
    fetch_error = None
    realtime_quotes: Mapping[str, Any] = {}
    realtime_metadata = None
    quote_error = None
    if symbols:
        try:
            fetched = fetcher(symbols, now=observed)
        except Exception as exc:  # noqa: BLE001 - materialize explicit unavailability
            fetch_error = type(exc).__name__
        if quote_fetcher is None and fetcher is fetch_intraday_minute_series_batch_partial:
            quote_fetcher = fetch_a_share_universe_snapshot
        if quote_fetcher is not None:
            try:
                realtime = quote_fetcher(now=observed, trade_date=observed.date())
                realtime_metadata = realtime.metadata
                realtime_quotes = {
                    item.instrument_id: item
                    for item in realtime.quotes
                    if item.instrument_id in symbols
                }
            except Exception as exc:  # noqa: BLE001 - never derive a fake daily change
                quote_error = type(exc).__name__
    quotes: list[ManualPortfolioQuoteV1] = []
    alerts: list[ManualPortfolioAlertV1] = []
    for entry in entries:
        series = fetched.get(entry.instrument_id)
        if series is None:
            reason = "批量行情未返回该证券"
            if fetch_error:
                reason = f"批量行情不可用（{fetch_error}）"
            quote = ManualPortfolioQuoteV1(
                instrument_id=entry.instrument_id,
                status="unavailable",
                reason=reason,
                quality_flags=("batch_instrument_missing",),
            )
        elif series.metadata.quality is QualityStatus.REJECTED:
            quote = ManualPortfolioQuoteV1(
                instrument_id=entry.instrument_id,
                status="unavailable",
                reason="标准行情契约已拒绝该证券结果",
                quality_flags=("canonical_result_rejected",),
            )
        else:
            quote = _quote(
                series,
                realtime_quote=realtime_quotes.get(entry.instrument_id),
                realtime_metadata=realtime_metadata,
                realtime_failure_code=quote_error,
                now=observed,
            )
        quotes.append(quote)
        if alert := _maybe_alert(quote, store=store, now=observed):
            alerts.append(alert)
    material = {
        "portfolio_revision": revision,
        "generated_at": observed.isoformat(),
        "items": [item.model_dump(mode="json") for item in quotes],
        "alerts": [item.model_dump(mode="json") for item in alerts],
    }
    trading_dates = {item.trading_date for item in quotes if item.trading_date is not None}
    if len(trading_dates) > 1:
        raise RuntimeError("manual portfolio batch contains multiple trading dates")
    trading_date = next(iter(trading_dates)) if trading_dates else None
    snapshot = ManualPortfolioMarketSnapshotV1(
        portfolio_revision=revision,
        snapshot_revision=digest(material),
        generated_at=observed,
        trading_date=trading_date,
        item_count=len(quotes),
        items=tuple(quotes),
        alerts=tuple(alerts),
    )
    return store.record_snapshot(snapshot)


__all__ = [
    "ALERT_CHANGE_THRESHOLD_PCT",
    "ALERT_COOLDOWN",
    "refresh_manual_portfolio_market",
]
