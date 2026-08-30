"""TuShare limit-list mapping for the canonical daily sentiment contract."""

from __future__ import annotations

from datetime import date, datetime
from statistics import mean, median
from typing import Any, Mapping, Sequence

from .securities import canonical_instrument_id


def _rows(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise RuntimeError(f"{label} must be a row sequence")
    return [dict(item) for item in value]


def _codes(rows: Sequence[Mapping[str, Any]], label: str) -> set[str]:
    result = {
        canonical_instrument_id(str(item.get("ts_code") or "")) for item in rows
    }
    if len(result) != len(rows):
        raise RuntimeError(f"{label} contains duplicate instruments")
    return result


def _row_date(row: Mapping[str, Any], label: str) -> date:
    raw = str(row.get("trade_date") or "").strip()
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError as exc:
        raise RuntimeError(f"{label} contains invalid trade_date") from exc


def _require_dates(rows: Sequence[Mapping[str, Any]], expected: date, label: str) -> None:
    if any(_row_date(item, label) != expected for item in rows):
        raise RuntimeError(f"{label} contains a mismatched trade date")


def _number(value: Any, label: str) -> float:
    if value in (None, "") or isinstance(value, bool):
        raise RuntimeError(f"{label} is missing")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} is invalid") from exc
    return result


def _positive_number(value: Any, label: str) -> float:
    result = _number(value, label)
    if result <= 0:
        raise RuntimeError(f"{label} must be positive")
    return result


def map_tushare_limit_sentiment(
    payload: Any,
    *,
    requested_date: date,
    previous_trade_date: date,
) -> dict[str, Any]:
    """Validate one complete same-provider bundle and calculate hard metrics."""

    if not isinstance(payload, Mapping):
        raise RuntimeError("limit sentiment provider payload must be a mapping")
    actual_date = date.fromisoformat(str(payload.get("trade_date") or ""))
    actual_previous = date.fromisoformat(
        str(payload.get("previous_trade_date") or "")
    )
    if actual_date != requested_date or actual_previous != previous_trade_date:
        raise RuntimeError("limit sentiment provider dates do not match the request")

    limit_up = _rows(payload.get("limit_up"), "limit-up pool")
    broken = _rows(payload.get("broken"), "broken pool")
    previous = _rows(payload.get("previous_limit_up"), "previous limit-up pool")
    daily = _rows(payload.get("daily"), "daily close table")
    _require_dates(limit_up, requested_date, "limit-up pool")
    _require_dates(broken, requested_date, "broken pool")
    _require_dates(previous, previous_trade_date, "previous limit-up pool")
    _require_dates(daily, requested_date, "daily close table")

    limit_up_codes = _codes(limit_up, "limit-up pool")
    broken_codes = _codes(broken, "broken pool")
    previous_codes = _codes(previous, "previous limit-up pool")
    daily_codes = _codes(daily, "daily close table")
    if limit_up_codes & broken_codes:
        raise RuntimeError("limit-up and broken pools overlap")

    daily_by_code = {
        canonical_instrument_id(str(item.get("ts_code") or "")): item
        for item in daily
    }
    eligible_codes = previous_codes & daily_codes
    eligible = [daily_by_code[code] for code in sorted(eligible_codes)]
    open_premiums = [
        (_number(item.get("open"), "daily open") / _positive_number(item.get("pre_close"), "daily pre_close") - 1)
        * 100
        for item in eligible
    ]
    close_premiums = [
        _number(item.get("pct_chg"), "daily pct_chg") for item in eligible
    ]
    first_board_codes = {
        canonical_instrument_id(str(item.get("ts_code") or ""))
        for item in previous
        if str(item.get("tag") or "").strip() == "首板"
    }

    attempted = len(limit_up) + len(broken)
    previous_count = len(previous)
    first_count = len(first_board_codes)
    return {
        "provider": "tushare_limit_list_ths",
        "provider_request_id": (
            str(payload.get("request_id")).strip()
            if payload.get("request_id") not in (None, "")
            else None
        ),
        "provider_as_of": None,
        "trade_date": requested_date,
        "previous_trade_date": previous_trade_date,
        "limit_up_count": len(limit_up),
        "broken_count": len(broken),
        "attempted_count": attempted,
        "seal_rate_pct": len(limit_up) / attempted * 100 if attempted else None,
        "break_rate_pct": len(broken) / attempted * 100 if attempted else None,
        "previous_limit_up_count": previous_count,
        "previous_feedback_eligible_count": len(eligible),
        "previous_feedback_coverage": (
            len(eligible) / previous_count if previous_count else 1.0
        ),
        "previous_limit_up_continued_count": len(previous_codes & limit_up_codes),
        "continuation_rate_pct": (
            len(previous_codes & limit_up_codes) / previous_count * 100
            if previous_count
            else None
        ),
        "previous_first_board_count": first_count,
        "first_board_promoted_count": len(first_board_codes & limit_up_codes),
        "first_board_promotion_rate_pct": (
            len(first_board_codes & limit_up_codes) / first_count * 100
            if first_count
            else None
        ),
        "previous_limit_up_avg_open_premium_pct": (
            mean(open_premiums) if open_premiums else None
        ),
        "previous_limit_up_avg_close_premium_pct": (
            mean(close_premiums) if close_premiums else None
        ),
        "previous_limit_up_median_close_premium_pct": (
            median(close_premiums) if close_premiums else None
        ),
        "previous_limit_up_red_close_rate_pct": (
            sum(value > 0 for value in close_premiums) / len(close_premiums) * 100
            if close_premiums
            else None
        ),
    }


__all__ = ["map_tushare_limit_sentiment"]
