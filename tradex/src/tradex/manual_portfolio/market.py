"""Collector-owned portfolio market materialization and alert policy."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

from tradex.data_gateway.contracts import IntradayMinuteSeriesV1, QualityStatus
from tradex.data_gateway.intraday import fetch_intraday_minute_series_batch_partial

from .contracts import (
    ManualPortfolioAlertV1,
    ManualPortfolioMarketSnapshotV1,
    ManualPortfolioQuoteV1,
    ManualPortfolioSampleV1,
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


def _quote(series: IntradayMinuteSeriesV1, *, now: datetime) -> ManualPortfolioQuoteV1:
    points = series.points
    latest = points[-1]
    first = points[0]
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
    change = ((latest.price / first.price) - 1) * 100 if first.price else None
    return ManualPortfolioQuoteV1(
        instrument_id=series.instrument_id,
        trading_date=series.trading_date,
        status=status,
        reason=reason,
        last_price=latest.price,
        session_change_pct=change,
        session_high=max(prices),
        session_low=min(prices),
        provider=series.metadata.provider,
        provider_request_id=series.metadata.provider_request_id,
        provider_as_of=provider_as_of,
        fetched_at=series.metadata.fetched_at,
        quality_flags=tuple(dict.fromkeys(flags)),
        samples=samples,
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
) -> ManualPortfolioMarketSnapshotV1:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("manual portfolio refresh time must include a timezone")
    observed = now.astimezone(SHANGHAI)
    entries = store.list_entries(enabled_only=True)
    revision = portfolio_revision(store.list_entries())
    symbols = tuple(item.instrument_id for item in entries)
    fetched: Mapping[str, IntradayMinuteSeriesV1] = {}
    fetch_error = None
    if symbols:
        try:
            fetched = fetcher(symbols, now=observed)
        except Exception as exc:  # noqa: BLE001 - materialize explicit unavailability
            fetch_error = type(exc).__name__
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
            quote = _quote(series, now=observed)
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
