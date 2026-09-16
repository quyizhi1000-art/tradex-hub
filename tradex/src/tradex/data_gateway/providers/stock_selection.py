"""Provider mappings for point-in-time daily stock-selection inputs."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Mapping

from ..stock_selection_contracts import (
    DailyStockCandlestickBarV1,
    DailyStockCandlestickHistoryV1,
    DailyStockFactorV1,
    StockFactorCoverageV1,
)
from .market_overview import finite_number, parse_provider_time
from .securities import canonical_instrument_id


def _date(value: Any, label: str, *, optional: bool = False) -> date | None:
    raw = str(value or "").strip().replace("-", "")
    if not raw and optional:
        return None
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc


def _number(value: Any) -> float | None:
    return finite_number(value)


def _first_number(row: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        result = _number(row.get(name))
        if result is not None:
            return result
    return None


def _records(bundle: dict[str, Any], name: str) -> list[dict[str, Any]]:
    value = bundle.get(name)
    if not isinstance(value, (list, tuple)):
        raise RuntimeError(f"daily stock factor provider omitted {name}")
    if any(not isinstance(item, dict) for item in value):
        raise RuntimeError(f"daily stock factor provider returned invalid {name}")
    return [dict(item) for item in value]


def _unique_by_code(
    records: Iterable[dict[str, Any]],
    *,
    name: str,
    allow_status_duplicates: bool = False,
    skip_non_a_share: bool = False,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in records:
        try:
            code = canonical_instrument_id(row.get("ts_code") or row.get("code"))
        except ValueError:
            if skip_non_a_share:
                continue
            raise
        previous = result.get(code)
        if previous is not None and not allow_status_duplicates:
            raise RuntimeError(f"daily stock factor provider duplicated {name} {code}")
        if previous is None or str(row.get("list_status") or "") == "L":
            result[code] = row
    return result


def _financial_by_code(
    records: Iterable[dict[str, Any]],
    *,
    trade_date: date,
) -> dict[str, dict[str, Any]]:
    selected: dict[str, tuple[tuple[date, date, int], dict[str, Any]]] = {}
    seen: dict[tuple[str, date, date, int], dict[str, Any]] = {}
    for row in records:
        try:
            code = canonical_instrument_id(row.get("ts_code") or row.get("code"))
        except ValueError:
            # The VIP table can include provider test instruments.  Financial
            # rows only enrich the canonical daily A-share universe.
            continue
        announcement = _date(row.get("ann_date"), "ann_date", optional=True)
        period = _date(row.get("end_date"), "end_date", optional=True)
        if announcement is None or period is None:
            continue
        update_flag = int(_number(row.get("update_flag")) or 0)
        key = (code, announcement, period, update_flag)
        previous = seen.get(key)
        if previous is not None:
            if previous != row:
                raise RuntimeError(
                    f"daily stock factor provider conflicted financial row {code}"
                )
            continue
        seen[key] = row
        if announcement > trade_date or period > trade_date:
            continue
        rank = (period, announcement, update_flag)
        if code not in selected or rank > selected[code][0]:
            selected[code] = (rank, row)
    return {code: value[1] for code, value in selected.items()}


def _daily_map(
    records: Iterable[dict[str, Any]],
    *,
    expected_date: date,
    name: str,
) -> dict[str, dict[str, Any]]:
    result = _unique_by_code(records, name=name)
    for code, row in result.items():
        actual = _date(row.get("trade_date"), f"{name}.trade_date")
        if actual != expected_date:
            raise RuntimeError(f"daily stock factor {name} date mismatch for {code}")
    return result


def _valid_price(row: dict[str, Any] | None, field: str = "close") -> float | None:
    value = _number(row.get(field)) if row is not None else None
    return value if value is not None and value > 0 else None


def _momentum(current: float, prior: dict[str, Any] | None) -> float | None:
    previous = _valid_price(prior)
    if previous is None:
        return None
    return (current / previous - 1.0) * 100.0


def _candlestick_bar(
    row: dict[str, Any] | None,
    *,
    trade_date: date,
) -> DailyStockCandlestickBarV1 | None:
    if row is None:
        return None
    open_price = _number(row.get("open"))
    high = _number(row.get("high"))
    low = _number(row.get("low"))
    close = _number(row.get("close"))
    previous_close = _number(row.get("pre_close"))
    amount = _number(row.get("amount"))
    volume = _number(row.get("vol"))
    values = (open_price, high, low, close, previous_close, amount)
    if any(value is None for value in values):
        raise RuntimeError(
            "daily stock candlestick provider omitted OHLC, previous close, or amount"
        )
    assert open_price is not None and high is not None and low is not None
    assert close is not None and previous_close is not None and amount is not None
    if open_price == 0 and high == 0 and low == 0 and close > 0 and amount == 0:
        return None
    if min(open_price, high, low, close, amount) <= 0:
        raise RuntimeError("daily stock candlestick provider returned invalid values")
    if high == low and open_price == high and close == high:
        # A one-price session has no measurable upper shadow.  Preserve the
        # strict positive-range canonical bar and let this instrument fail the
        # 15-session completeness gate instead of rejecting the market batch.
        return None
    return DailyStockCandlestickBarV1(
        trade_date=trade_date,
        open=open_price,
        high=high,
        low=low,
        close=close,
        previous_close=previous_close,
        amount_cny=amount * 1_000.0,
        # Daily A-share volume is reported in lots of 100 shares.
        volume_shares=volume * 100.0 if volume is not None and volume > 0 else None,
    )


def map_daily_stock_factor_bundle(
    raw: Any,
    *,
    provider: str,
    requested_date: date,
) -> tuple[
    tuple[DailyStockFactorV1, ...],
    StockFactorCoverageV1,
    date,
    date,
    float,
    float,
    datetime | None,
    str | None,
    tuple[date, ...],
    tuple[DailyStockCandlestickHistoryV1, ...],
]:
    del provider  # Aliases and units are normalized here before consumers see them.
    if not isinstance(raw, dict):
        raise RuntimeError("daily stock factor provider returned an unsupported payload")
    trade_date = _date(raw.get("trade_date"), "bundle.trade_date")
    prior_20d = _date(raw.get("prior_20d_trade_date"), "prior_20d_trade_date")
    prior_60d = _date(raw.get("prior_60d_trade_date"), "prior_60d_trade_date")
    if trade_date != requested_date:
        raise RuntimeError("daily stock factor provider returned the wrong trade date")
    if prior_20d is None or prior_60d is None:
        raise RuntimeError("daily stock factor provider omitted lookback dates")
    candlestick_window = tuple(
        _date(value, "candlestick_window_trade_dates")
        for value in raw.get("candlestick_window_trade_dates") or ()
    )
    if (
        len(candlestick_window) != 15
        or any(value is None for value in candlestick_window)
        or tuple(sorted(candlestick_window)) != candlestick_window
        or candlestick_window[-1] != trade_date
    ):
        raise RuntimeError("daily stock factor provider returned an invalid candlestick window")

    daily = _daily_map(_records(raw, "daily"), expected_date=trade_date, name="daily")
    daily_basic = _daily_map(
        _records(raw, "daily_basic"), expected_date=trade_date, name="daily_basic"
    )
    history_20d = _daily_map(
        _records(raw, "daily_20d"), expected_date=prior_20d, name="daily_20d"
    )
    history_60d = _daily_map(
        _records(raw, "daily_60d"), expected_date=prior_60d, name="daily_60d"
    )
    window_maps: dict[date, dict[str, dict[str, Any]]] = {}
    window_slices = _records(raw, "daily_window")
    if len(window_slices) != len(candlestick_window):
        raise RuntimeError("daily stock factor provider returned an incomplete daily window")
    for item in window_slices:
        session_date = _date(item.get("session_date"), "daily_window.session_date")
        if session_date is None or session_date not in candlestick_window:
            raise RuntimeError("daily stock factor provider returned a wrong window date")
        if session_date in window_maps:
            raise RuntimeError("daily stock factor provider duplicated a window date")
        window_maps[session_date] = _daily_map(
            _records(item, "daily"),
            expected_date=session_date,
            name=f"daily_window_{session_date.isoformat()}",
        )
    master = _unique_by_code(
        _records(raw, "stock_basic"),
        name="stock_basic",
        allow_status_duplicates=True,
        skip_non_a_share=True,
    )
    financials = _financial_by_code(
        _records(raw, "financials"), trade_date=trade_date
    )

    factors: list[DailyStockFactorV1] = []
    histories: list[DailyStockCandlestickHistoryV1] = []
    coverage = {
        "master_count": 0,
        "daily_basic_count": 0,
        "momentum_20d_count": 0,
        "momentum_60d_count": 0,
        "financial_count": 0,
        "candlestick_history_count": 0,
    }
    for instrument_id in sorted(daily):
        bar = daily[instrument_id]
        current = _valid_price(bar)
        open_price = _number(bar.get("open"))
        amount = _number(bar.get("amount"))
        volume = _number(bar.get("vol"))
        if current is None or open_price is None or open_price < 0:
            raise RuntimeError(f"daily stock factor OHLC is invalid for {instrument_id}")
        if amount is None or amount < 0:
            raise RuntimeError(f"daily stock factor amount is invalid for {instrument_id}")
        if open_price == 0 and (amount != 0 or volume != 0):
            raise RuntimeError(
                f"daily stock factor suspended row is inconsistent for {instrument_id}"
            )
        basic = daily_basic.get(instrument_id)
        profile = master.get(instrument_id)
        financial = financials.get(instrument_id)
        momentum_20d = _momentum(current, history_20d.get(instrument_id))
        momentum_60d = _momentum(current, history_60d.get(instrument_id))
        coverage["master_count"] += int(profile is not None)
        coverage["daily_basic_count"] += int(basic is not None)
        coverage["momentum_20d_count"] += int(momentum_20d is not None)
        coverage["momentum_60d_count"] += int(momentum_60d is not None)
        coverage["financial_count"] += int(financial is not None)

        history_bars = tuple(
            bar
            for session_date in candlestick_window
            if (
                bar := _candlestick_bar(
                    window_maps[session_date].get(instrument_id),
                    trade_date=session_date,
                )
            )
            is not None
        )
        if history_bars:
            histories.append(
                DailyStockCandlestickHistoryV1(
                    instrument_id=instrument_id,
                    bars=history_bars,
                )
            )
        coverage["candlestick_history_count"] += int(
            len(history_bars) == len(candlestick_window)
        )

        total_mv = _number(basic.get("total_mv")) if basic else None
        circ_mv = _number(basic.get("circ_mv")) if basic else None
        factors.append(
            DailyStockFactorV1(
                instrument_id=instrument_id,
                name=str((profile or {}).get("name") or instrument_id).strip(),
                industry=(str(profile.get("industry") or "").strip() or None)
                if profile
                else None,
                market=(str(profile.get("market") or "").strip() or None)
                if profile
                else None,
                list_date=_date(profile.get("list_date"), "list_date", optional=True)
                if profile
                else None,
                delist_date=_date(profile.get("delist_date"), "delist_date", optional=True)
                if profile
                else None,
                trade_date=trade_date,
                open=open_price,
                close=current,
                amount_cny=amount * 1_000.0,
                total_market_cap_cny=total_mv * 10_000.0
                if total_mv is not None and total_mv > 0
                else None,
                float_market_cap_cny=circ_mv * 10_000.0
                if circ_mv is not None and circ_mv > 0
                else None,
                turnover_rate_pct=_number(basic.get("turnover_rate")) if basic else None,
                volume_ratio=_number(basic.get("volume_ratio")) if basic else None,
                pe_ttm=_number(basic.get("pe_ttm")) if basic else None,
                pb=_number(basic.get("pb")) if basic else None,
                dividend_yield_pct=_number(basic.get("dv_ratio")) if basic else None,
                momentum_20d_pct=momentum_20d,
                momentum_60d_pct=momentum_60d,
                roe_pct=_first_number(financial, "roe_waa", "roe")
                if financial
                else None,
                gross_margin_pct=_number(financial.get("grossprofit_margin"))
                if financial
                else None,
                debt_to_assets_pct=_number(financial.get("debt_to_assets"))
                if financial
                else None,
                revenue_yoy_pct=_number(financial.get("or_yoy"))
                if financial
                else None,
                net_profit_yoy_pct=_number(financial.get("netprofit_yoy"))
                if financial
                else None,
                financial_report_date=_date(financial.get("end_date"), "end_date", optional=True)
                if financial
                else None,
                financial_announcement_date=_date(
                    financial.get("ann_date"), "ann_date", optional=True
                )
                if financial
                else None,
            )
        )

    benchmark_rows = _daily_map(
        _records(raw, "benchmark"), expected_date=trade_date, name="benchmark"
    )
    benchmark = benchmark_rows.get("000300.SH")
    benchmark_open = _valid_price(benchmark, "open")
    benchmark_close = _valid_price(benchmark)
    if benchmark_open is None or benchmark_close is None:
        raise RuntimeError("daily stock factor benchmark is missing or invalid")

    provider_as_of = parse_provider_time(raw.get("provider_as_of"))
    request_id = str(raw.get("request_id") or "").strip() or None
    return (
        tuple(factors),
        StockFactorCoverageV1(universe_count=len(factors), **coverage),
        prior_20d,
        prior_60d,
        benchmark_open,
        benchmark_close,
        provider_as_of,
        request_id,
        tuple(value for value in candlestick_window if value is not None),
        tuple(histories),
    )


__all__ = ["map_daily_stock_factor_bundle"]
