"""Provider-neutral gateway for A-share quotes and historical bars."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractMetadata, OHLCVSeriesV1, QuoteSnapshotV1
from .providers.securities import (
    canonical_instrument_id,
    frame_provider_time,
    frame_request_id,
    map_ohlcv_frame,
    map_quote_frame,
    provider_units_verified,
)
from .quality import DataQualityError, assess_ohlcv_series, assess_quote_snapshot


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_PERIODS = {
    "1d": "daily",
    "day": "daily",
    "daily": "daily",
    "1w": "weekly",
    "week": "weekly",
    "weekly": "weekly",
    "1m": "monthly",
    "month": "monthly",
    "monthly": "monthly",
}
_ADJUSTMENTS = {
    "": "none",
    "none": "none",
    "qfq": "forward",
    "forward": "forward",
    "hfq": "backward",
    "backward": "backward",
}


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


def _provider_metadata(frame: Any) -> tuple[datetime | None, str | None]:
    return frame_provider_time(frame), frame_request_id(frame)


def fetch_quote_snapshot(
    symbol: str,
    *,
    router: Any | None = None,
    now: datetime | None = None,
) -> QuoteSnapshotV1:
    fetched_at = _now(now)

    def validate(frame: Any, provider: str) -> QuoteSnapshotV1:
        mapped = map_quote_frame(
            frame,
            provider=provider,
            requested_symbol=symbol,
        )
        provider_as_of = mapped.pop("provider_as_of")
        _, provider_request_id = _provider_metadata(frame)
        quality, flags = assess_quote_snapshot(
            previous_close=mapped["previous_close"],
            open=mapped["open"],
            high=mapped["high"],
            low=mapped["low"],
            volume_shares=mapped["volume_shares"],
            amount_cny=mapped["amount_cny"],
            provider_as_of=provider_as_of,
            provider_units_verified=provider_units_verified(provider),
        )
        return QuoteSnapshotV1(
            metadata=ContractMetadata(
                contract="quote_snapshot.v1",
                provider=provider,
                provider_request_id=provider_request_id,
                provider_as_of=provider_as_of,
                fetched_at=fetched_at,
                quality=quality,
                quality_flags=flags,
            ),
            **mapped,
        )

    snapshot, _provider = _router(router).route_validated(
        "realtime_quote",
        validate,
        symbol=symbol,
    )
    return snapshot


def _normalize_period(value: str) -> str:
    key = str(value or "daily").strip().lower()
    if key not in _PERIODS:
        raise ValueError(f"unsupported OHLCV period: {value!r}")
    return _PERIODS[key]


def _normalize_adjustment(value: str) -> str:
    key = str(value or "").strip().lower()
    if key not in _ADJUSTMENTS:
        raise ValueError(f"unsupported OHLCV adjustment: {value!r}")
    return _ADJUSTMENTS[key]


def _date_bound(value: str) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) == 8 and raw.isdigit():
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"invalid date bound: {value!r}") from exc


def fetch_ohlcv_series(
    symbol: str,
    *,
    period: str = "daily",
    start_date: str = "",
    end_date: str = "",
    adjust: str = "qfq",
    router: Any | None = None,
    now: datetime | None = None,
) -> OHLCVSeriesV1:
    normalized_period = _normalize_period(period)
    normalized_adjustment = _normalize_adjustment(adjust)
    start = _date_bound(start_date)
    end = _date_bound(end_date)
    if start is not None and end is not None and start > end:
        raise ValueError("start_date cannot be later than end_date")

    fetched_at = _now(now)

    def validate(frame: Any, provider: str) -> OHLCVSeriesV1:
        attrs = getattr(frame, "attrs", {})
        if attrs.get("period") and attrs["period"] != normalized_period:
            raise DataQualityError("provider returned a different OHLCV period")
        if attrs.get("adjust") and attrs["adjust"] != normalized_adjustment:
            raise DataQualityError("provider returned a different adjustment basis")

        bars = map_ohlcv_frame(frame, provider=provider)
        bars = tuple(
            bar
            for bar in bars
            if (start is None or bar.trading_date >= start)
            and (end is None or bar.trading_date <= end)
        )
        if not bars:
            raise DataQualityError("requested date range contains no OHLCV bars")

        provider_as_of, provider_request_id = _provider_metadata(frame)
        quality, flags = assess_ohlcv_series(
            bars=bars,
            provider_as_of=provider_as_of,
            provider_units_verified=provider_units_verified(provider),
        )
        return OHLCVSeriesV1(
            metadata=ContractMetadata(
                contract="ohlcv_bar.v1",
                provider=provider,
                provider_request_id=provider_request_id,
                provider_as_of=provider_as_of,
                fetched_at=fetched_at,
                quality=quality,
                quality_flags=flags,
            ),
            instrument_id=canonical_instrument_id(symbol),
            period=normalized_period,
            adjustment=normalized_adjustment,
            bars=bars,
        )

    series, _provider = _router(router).route_validated(
        "historical_kline",
        validate,
        symbol=symbol,
        period=period,
        start_date=start_date,
        end_date=end_date,
        adjust=adjust,
    )
    return series


def quote_snapshot_to_legacy_records(snapshot: QuoteSnapshotV1) -> list[dict[str, Any]]:
    """Keep the existing list-of-Chinese-fields MCP representation."""

    return [
        {
            "代码": snapshot.instrument_id[:6],
            "名称": snapshot.name,
            "最新价": snapshot.last,
            "涨跌额": snapshot.change,
            "涨跌幅": snapshot.change_pct,
            "今开": snapshot.open,
            "最高": snapshot.high,
            "最低": snapshot.low,
            "昨收": snapshot.previous_close,
            "成交量": snapshot.volume_shares,
            "成交额": snapshot.amount_cny,
            "换手率": snapshot.turnover_pct,
            "市盈率": snapshot.pe_ttm,
            "市净率": snapshot.pb,
            "总市值": snapshot.total_market_cap_cny,
            "流通市值": snapshot.float_market_cap_cny,
            "更新时间": (
                snapshot.metadata.provider_as_of.isoformat(timespec="seconds")
                if snapshot.metadata.provider_as_of
                else None
            ),
        }
    ]


def ohlcv_series_to_legacy_records(
    series: OHLCVSeriesV1,
    *,
    max_rows: int = 500,
) -> list[dict[str, Any]]:
    return [
        {
            "日期": item.trading_date.isoformat(),
            "开盘": item.open,
            "收盘": item.close,
            "最高": item.high,
            "最低": item.low,
            "成交量": item.volume_shares,
            "成交额": item.amount_cny,
            "振幅": item.amplitude_pct,
            "涨跌幅": item.change_pct,
            "涨跌额": item.change,
            "换手率": item.turnover_pct,
        }
        for item in series.bars[:max_rows]
    ]


__all__ = [
    "fetch_ohlcv_series",
    "fetch_quote_snapshot",
    "ohlcv_series_to_legacy_records",
    "quote_snapshot_to_legacy_records",
]
