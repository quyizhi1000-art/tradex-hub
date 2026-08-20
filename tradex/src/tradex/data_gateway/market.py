"""Unified market-overview gateway and the legacy dashboard view adapter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import (
    ContractMetadata,
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
from .quality import assess_market_overview


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


def market_state(now: datetime) -> MarketStateV1:
    minute = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return MarketStateV1(label="周末休市", is_open=False)
    if minute < 9 * 60 + 15:
        return MarketStateV1(label="等待开盘", is_open=False)
    if minute < 9 * 60 + 30:
        return MarketStateV1(label="集合竞价", is_open=False)
    if minute <= 11 * 60 + 30 or 13 * 60 <= minute <= 15 * 60:
        return MarketStateV1(label="盘中交易", is_open=True)
    if minute < 13 * 60:
        return MarketStateV1(label="午间休市", is_open=False)
    return MarketStateV1(label="今日收盘", is_open=False)


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


def fetch_market_overview(
    *,
    now: datetime | None = None,
    router: Any | None = None,
    turnover_fetcher: Callable[..., list[dict[str, Any]]] | None = None,
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

    frame, provider = router.route("market_overview")
    if frame is None or not hasattr(frame, "to_dict"):
        raise RuntimeError("行情源返回了无法识别的数据")
    records = frame.to_dict(orient="records")

    if turnover_fetcher is None:
        from tradex.data_sources.akshare_fetchers import fetch_index_intraday_amount

        turnover_fetcher = fetch_index_intraday_amount

    try:
        series = {
            "000001.SH": turnover_fetcher(symbol="sh000001", days=5),
            "399001.SZ": turnover_fetcher(symbol="sz399001", days=5),
        }
    except Exception:
        turnover = MarketTurnoverV1(
            available=False,
            reason="全市场同期成交额暂不可用",
        )
    else:
        # Contract or calculation failures are implementation defects and must
        # remain visible; only provider-fetch failures degrade this component.
        turnover = build_market_turnover(series, now)

    indices = map_indices(records, provider)
    participation = map_participation_indices(records)
    provider_as_of = provider_watermark(records) or parse_provider_time(
        getattr(frame, "attrs", {}).get("provider_as_of")
    )
    provider_request_id = (
        str(request_id)
        if (request_id := getattr(frame, "attrs", {}).get("request_id"))
        else None
    )
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
    "build_market_turnover",
    "fetch_market_overview",
    "index_quotes_to_legacy",
    "market_overview_to_legacy_payload",
    "market_turnover_to_legacy",
    "market_state",
    "participation_indices_to_legacy",
]
