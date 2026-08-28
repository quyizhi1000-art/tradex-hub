"""Provider-neutral gateway for complete daily stock factor snapshots."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractMetadata, QualityStatus
from .providers.stock_selection import map_daily_stock_factor_bundle
from .stock_selection_contracts import DailyStockFactorSnapshotV1


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _router(router: Any | None) -> Any:
    if router is not None:
        return router
    from tradex.data_sources import get_router, register_all_sources

    register_all_sources()
    return get_router()


def _trade_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    raw = str(value or "").strip().replace("-", "")
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError("trade_date must be YYYY-MM-DD or YYYYMMDD") from exc


def _now(value: datetime | None) -> datetime:
    result = value or datetime.now(SHANGHAI)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("gateway now must include a timezone")
    return result.astimezone(SHANGHAI)


def _validated_slice(
    raw: Any,
    *,
    requested: date,
    required_lists: tuple[str, ...],
    label: str,
    allow_empty_lists: tuple[str, ...] = (),
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RuntimeError(f"{label} provider returned an unsupported payload")
    raw_date = _trade_date(str(raw.get("trade_date") or ""))
    if raw_date != requested:
        raise RuntimeError(f"{label} provider returned the wrong trade date")
    for name in required_lists:
        value = raw.get(name)
        if not isinstance(value, (list, tuple)) or (
            not value and name not in allow_empty_lists
        ):
            raise RuntimeError(f"{label} provider returned invalid {name}")
        if any(not isinstance(item, dict) for item in value):
            raise RuntimeError(f"{label} provider returned malformed {name}")
    return dict(raw)


def _financial_periods(target: date, count: int = 8) -> tuple[str, ...]:
    candidates: list[date] = []
    for year in range(target.year, target.year - 4, -1):
        candidates.extend(
            date(year, month, day)
            for month, day in ((12, 31), (9, 30), (6, 30), (3, 31))
        )
    return tuple(
        item.strftime("%Y%m%d")
        for item in sorted(
            (item for item in candidates if item <= target), reverse=True
        )[:count]
    )


def _validated_daily_slice(
    raw: Any,
    *,
    requested: date,
    session_date: date,
    label: str,
) -> dict[str, Any]:
    result = _validated_slice(
        raw,
        requested=requested,
        required_lists=("daily",),
        label=label,
    )
    actual_session = _trade_date(str(result.get("session_date") or ""))
    if actual_session != session_date:
        raise RuntimeError(f"{label} provider returned the wrong session date")
    return result


def _candlestick_window_dates(
    calendar: dict[str, Any],
    *,
    requested: date,
) -> tuple[date, ...]:
    raw_dates = calendar.get("candlestick_window_trade_dates")
    if not isinstance(raw_dates, (list, tuple)):
        raise RuntimeError("stock selection calendar omitted candlestick window")
    dates = tuple(_trade_date(str(value)) for value in raw_dates)
    if (
        len(dates) != 15
        or dates != tuple(sorted(dates))
        or len(dates) != len(set(dates))
        or dates[-1] != requested
    ):
        raise RuntimeError("stock selection calendar returned invalid candlestick window")
    return dates


def _validated_financial_slice(
    raw: Any,
    *,
    requested: date,
    period: str,
) -> dict[str, Any]:
    result = _validated_slice(
        raw,
        requested=requested,
        required_lists=("financials",),
        allow_empty_lists=("financials",),
        label=f"stock selection financials {period}",
    )
    if str(result.get("period") or "").strip() != period:
        raise RuntimeError("stock selection financial provider returned wrong period")
    return result


def fetch_daily_stock_factor_snapshot(
    trade_date: date | str,
    *,
    router: Any | None = None,
    now: datetime | None = None,
    apply_relationship_catalog: bool = False,
) -> DailyStockFactorSnapshotV1:
    requested = _trade_date(trade_date)
    fetched_at = _now(now)

    route = _router(router)
    calendar, calendar_provider = route.route_validated(
        "stock_selection_calendar",
        lambda raw, _provider: _validated_slice(
            raw,
            requested=requested,
            required_lists=("benchmark",),
            label="stock selection calendar",
        ),
        trade_date=requested.strftime("%Y%m%d"),
    )
    prior_20d = _trade_date(str(calendar.get("prior_20d_trade_date") or ""))
    prior_60d = _trade_date(str(calendar.get("prior_60d_trade_date") or ""))
    if not prior_60d < prior_20d < requested:
        raise RuntimeError("stock selection calendar returned invalid lookback dates")
    candlestick_window_dates = _candlestick_window_dates(
        calendar,
        requested=requested,
    )
    daily, daily_provider = route.route_validated(
        "stock_selection_daily",
        lambda raw, _provider: _validated_daily_slice(
            raw,
            requested=requested,
            session_date=requested,
            label="stock selection daily",
        ),
        trade_date=requested.strftime("%Y%m%d"),
        session_date=requested.strftime("%Y%m%d"),
    )
    daily_basic, daily_basic_provider = route.route_validated(
        "stock_selection_daily_basic",
        lambda raw, _provider: _validated_slice(
            raw,
            requested=requested,
            required_lists=("daily_basic",),
            label="stock selection daily basic",
        ),
        trade_date=requested.strftime("%Y%m%d"),
    )
    daily_20d, daily_20d_provider = route.route_validated(
        "stock_selection_daily",
        lambda raw, _provider: _validated_daily_slice(
            raw,
            requested=requested,
            session_date=prior_20d,
            label="stock selection daily 20d",
        ),
        trade_date=requested.strftime("%Y%m%d"),
        session_date=prior_20d.strftime("%Y%m%d"),
    )
    daily_60d, daily_60d_provider = route.route_validated(
        "stock_selection_daily",
        lambda raw, _provider: _validated_daily_slice(
            raw,
            requested=requested,
            session_date=prior_60d,
            label="stock selection daily 60d",
        ),
        trade_date=requested.strftime("%Y%m%d"),
        session_date=prior_60d.strftime("%Y%m%d"),
    )
    master, master_provider = route.route_validated(
        "stock_selection_master",
        lambda raw, _provider: _validated_slice(
            raw,
            requested=requested,
            required_lists=("stock_basic",),
            label="stock selection master",
        ),
        trade_date=requested.strftime("%Y%m%d"),
    )
    candlestick_slices: dict[date, tuple[dict[str, Any], str]] = {
        requested: (daily, daily_provider)
    }
    for session_date in candlestick_window_dates:
        if session_date == requested:
            continue
        candlestick_slices[session_date] = route.route_validated(
            "stock_selection_daily",
            lambda raw, _provider, expected=session_date: _validated_daily_slice(
                raw,
                requested=requested,
                session_date=expected,
                label=f"stock selection candlestick {expected.isoformat()}",
            ),
            trade_date=requested.strftime("%Y%m%d"),
            session_date=session_date.strftime("%Y%m%d"),
        )
    financial_periods = _financial_periods(requested)

    def fetch_financial_period(period: str) -> tuple[dict[str, Any], str]:
        return route.route_validated(
            "stock_selection_financial_period",
            lambda raw, _provider: _validated_financial_slice(
                raw,
                requested=requested,
                period=period,
            ),
            trade_date=requested.strftime("%Y%m%d"),
            period=period,
        )

    with ThreadPoolExecutor(
        max_workers=4,
        thread_name_prefix="stock-selection-financial-gateway",
    ) as executor:
        jobs = {
            period: executor.submit(fetch_financial_period, period)
            for period in financial_periods
        }
        financial_slices = {
            period: jobs[period].result() for period in financial_periods
        }
    financial_rows = [
        row
        for period in financial_periods
        for row in financial_slices[period][0]["financials"]
    ]
    if not financial_rows:
        raise RuntimeError("stock selection financials returned no usable rows")
    providers = {
        calendar_provider,
        daily_provider,
        daily_basic_provider,
        daily_20d_provider,
        daily_60d_provider,
        master_provider,
        *(provider for _payload, provider in candlestick_slices.values()),
        *(provider for _payload, provider in financial_slices.values()),
    }
    if len(providers) != 1:
        raise RuntimeError("daily stock factor slices must use one semantic provider")
    provider = calendar_provider
    request_parts = [
        f"{label}:{request_id}"
        for label, payload in (
            ("calendar", calendar),
            ("daily", daily),
            ("daily_basic", daily_basic),
            ("daily_20d", daily_20d),
            ("daily_60d", daily_60d),
            ("master", master),
            *(
                (f"daily_window:{session_date.isoformat()}", candlestick_slices[session_date][0])
                for session_date in candlestick_window_dates
                if session_date != requested
            ),
            *(
                (f"financials:{period}", financial_slices[period][0])
                for period in financial_periods
            ),
        )
        if (request_id := str(payload.get("request_id") or "").strip())
    ]
    raw = {
        **calendar,
        "daily": daily["daily"],
        "daily_basic": daily_basic["daily_basic"],
        "daily_20d": daily_20d["daily"],
        "daily_60d": daily_60d["daily"],
        "candlestick_window_trade_dates": [
            value.isoformat() for value in candlestick_window_dates
        ],
        "daily_window": [
            candlestick_slices[session_date][0]
            for session_date in candlestick_window_dates
        ],
        "provider_as_of": calendar.get("provider_as_of"),
        "stock_basic": master["stock_basic"],
        "financials": financial_rows,
        "request_id": "|".join(request_parts) or None,
    }

    def build_snapshot() -> DailyStockFactorSnapshotV1:
        (
            factors,
            coverage,
            prior_20d,
            prior_60d,
            benchmark_open,
            benchmark_close,
            provider_as_of,
            request_id,
            candlestick_window,
            candlestick_histories,
        ) = map_daily_stock_factor_bundle(
            raw, provider=provider, requested_date=requested
        )
        master_ratio = coverage.ratio("master_count")
        basic_ratio = coverage.ratio("daily_basic_count")
        momentum_ratio = min(
            coverage.ratio("momentum_20d_count"),
            coverage.ratio("momentum_60d_count"),
        )
        if min(master_ratio, basic_ratio, momentum_ratio) < 0.90:
            raise RuntimeError("daily stock factor core coverage is below 90%")
        financial_ratio = coverage.ratio("financial_count")
        quality = (
            QualityStatus.ACCEPTED
            if financial_ratio >= 0.60
            else QualityStatus.DEGRADED
        )
        flags = () if quality is QualityStatus.ACCEPTED else (
            f"financial_coverage:{financial_ratio:.3f}",
        )
        if apply_relationship_catalog:
            from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader

            with InstrumentTaxonomyReader() as taxonomy_reader:
                taxonomy_status = taxonomy_reader.status()
                if taxonomy_status is None:
                    flags = (*flags, "relationship_catalog_unavailable")
                    quality = QualityStatus.DEGRADED
                elif taxonomy_status.as_of > requested:
                    flags = (*flags, "relationship_catalog_future_as_of")
                    quality = QualityStatus.DEGRADED
                else:
                    relationships = taxonomy_reader.get_many(
                        item.instrument_id for item in factors
                    )
                    relationship_coverage = len(relationships) / len(factors)
                    factors = tuple(
                        item.model_copy(update={
                            "industry": (
                                relationship.statistical_industry.level3_name
                                if relationship and relationship.statistical_industry
                                else item.industry
                            ),
                            "provider_industry": item.industry,
                            "primary_business_name": (
                                relationship.primary_business_name
                                if relationship
                                else None
                            ),
                            "business_tags": (
                                relationship.business_tags if relationship else ()
                            ),
                            "relationship_verification_status": (
                                relationship.verification_status
                                if relationship
                                else "unresolved"
                            ),
                            "relationship_catalog_revision": (
                                taxonomy_status.catalog_revision
                            ),
                        })
                        for item in factors
                        for relationship in (relationships.get(item.instrument_id),)
                    )
                    if relationship_coverage < 0.98:
                        flags = (*flags, f"relationship_coverage:{relationship_coverage:.3f}")
                        quality = QualityStatus.DEGRADED
        return DailyStockFactorSnapshotV1(
            metadata=ContractMetadata(
                contract="daily_stock_factor_snapshot.v1",
                provider=provider,
                provider_request_id=request_id,
                provider_as_of=provider_as_of,
                fetched_at=fetched_at,
                quality=quality,
                quality_flags=flags,
            ),
            trade_date=requested,
            prior_20d_trade_date=prior_20d,
            prior_60d_trade_date=prior_60d,
            benchmark_instrument_id="000300.SH",
            benchmark_open=benchmark_open,
            benchmark_close=benchmark_close,
            coverage=coverage,
            factors=factors,
            candlestick_window_trade_dates=candlestick_window,
            candlestick_histories=candlestick_histories,
        )
    return build_snapshot()


__all__ = ["fetch_daily_stock_factor_snapshot"]
