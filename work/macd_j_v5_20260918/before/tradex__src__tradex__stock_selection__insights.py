"""Read-only strategy comparisons and five-session archive/limit-up evidence."""

from datetime import date, timedelta

from tradex.market_calendar import CalendarDayStatus, calendar_day_status


INSIGHTS_CONTRACT = "stock_selection_insights.v1"
COMPARISON_POLICY = "displayed_selection_evidence.v2"
# Only the evidence shown by each strategy, never ranking/fundamental inputs.
STRATEGY_FIELDS = {
    "balanced-multifactor-a-share": (
        "reasons", "risks",
    ),
    "next-session-limit-up-tendency-main-board": (
        "reasons", "risks",
    ),
    "long-upper-shadow-main-board": (
        "occurrence_count", "latest_occurrence_date", "evidence",
    ),
    "upward-volume-surge-main-board": (
        "anchor_trade_date", "anchor_low", "minimum_subsequent_close", "reset_count", "evidence",
    ),
    "macd-j-upturn-main-board": (
        "signal_trade_date", "signal_group", "zero_axis_zone", "j_turn_date",
        "gap_sessions", "j_trough", "j_turn_value", "low_j_tags", "evidence",
    ),
}

EVIDENCE_FIELDS = {
    "long-upper-shadow-main-board": (
        "trade_date", "upper_shadow_pct_of_close", "upper_shadow_body_multiple", "upper_shadow_range_ratio",
    ),
    "upward-volume-surge-main-board": (
        "trade_date", "change_pct", "volume_multiple", "volume_shares", "prior_5d_average_volume_shares",
    ),
    "macd-j-upturn-main-board": ("trade_date", "dif", "dea", "k", "d", "j"),
}


def evidence_projection(candidate, strategy_id):
    result = {field: candidate.get(field) for field in STRATEGY_FIELDS[strategy_id]}
    if strategy_id == "macd-j-upturn-main-board" and candidate.get("signal_rule") == "strict_cross_v4":
        result.update({key: candidate.get(key) for key in ("macd_cross_date", "high_60", "high_60_date",
            "drawdown_60_pct", "ma5", "volume_ratio", "price_window_start", "price_window_end")})
    if strategy_id in EVIDENCE_FIELDS:
        result["evidence"] = [
            {field: event.get(field) for field in EVIDENCE_FIELDS[strategy_id]}
            for event in candidate.get("evidence", ())
        ]
    if strategy_id == "balanced-multifactor-a-share":
        result["reasons"] = (candidate.get("reasons") or [])[:1]
        result["risks"] = (candidate.get("risks") or [])[:1]
    elif strategy_id == "next-session-limit-up-tendency-main-board":
        result["reasons"] = (candidate.get("reasons") or [])[:4]
        result["risks"] = (candidate.get("risks") or [])[:3]
    return result


def trading_window(target: date, count: int = 5) -> list[date]:
    """Fail closed outside the calendar; never substitute available archive days."""
    if calendar_day_status(target) is not CalendarDayStatus.VERIFIED_TRADING_DAY:
        return []
    result = []
    cursor = target
    for _ in range(count * 20):
        status = calendar_day_status(cursor)
        if status is CalendarDayStatus.UNVERIFIED:
            return []
        if status is CalendarDayStatus.VERIFIED_TRADING_DAY:
            result.append(cursor)
            if len(result) == count:
                return list(reversed(result))
        cursor -= timedelta(days=1)
    return []


def _usable(result):
    return result is not None and result.quality != "unavailable"


def compare_result(current, previous, previous_date):
    comparable = _usable(current) and _usable(previous)
    status = "available" if comparable else "unavailable"
    old = {c.instrument_id: c.model_dump(mode="json") for c in previous.payload.candidates} if comparable else {}
    rows = {}
    for candidate in current.payload.candidates if current else ():
        value = candidate.model_dump(mode="json")
        prior = old.get(candidate.instrument_id)
        current_evidence = evidence_projection(value, current.strategy_id)
        prior_evidence = evidence_projection(prior, current.strategy_id) if prior is not None else None
        changes = [
            {"field": field, "before": prior_evidence.get(field), "after": current_evidence.get(field)}
            for field in STRATEGY_FIELDS[current.strategy_id]
            if prior_evidence is not None and prior_evidence.get(field) != current_evidence.get(field)
        ]
        rows[candidate.instrument_id] = {
            "status": ("unknown" if not comparable else "new" if prior is None
                       else "changed" if changes else "unchanged"),
            "changes": changes,
        }
    return {
        "policy": COMPARISON_POLICY,
        "status": status,
        "quality": "degraded" if comparable and (current.quality == "degraded" or previous.quality == "degraded") else status,
        "previous_trade_date": previous_date.isoformat() if previous_date else None,
        "result_id": current.result_id if current else None,
        "previous_result_id": previous.result_id if previous else None,
        "rows": rows,
        "counts": {state: sum(row["status"] == state for row in rows.values())
                   for state in ("new", "changed", "unchanged", "unknown")},
    }


def build_insights(store, target: date, definitions) -> dict:
    window = trading_window(target)
    memberships = {day: store.get_limit_up_membership(day) for day in window}
    by_day = {day: store.list_strategy_results(day) for day in window}
    strategies = {}
    for definition in definitions:
        strategy_id = definition.strategy_id
        current_options = [r for r in by_day.get(target, ()) if r.strategy_id == strategy_id]
        current = next((r for r in current_options if r.strategy_version == definition.strategy_version), None)
        current = current or max(current_options, key=lambda r: int(r.strategy_version[1:]), default=None)
        version = current.strategy_version if current else definition.strategy_version
        previous_day = window[-2] if len(window) > 1 else None
        previous = store.get_strategy_result(previous_day, strategy_id, strategy_version=version) if previous_day else None
        comparison = compare_result(current, previous, previous_day)
        stocks = {}
        missing_archives, degraded_archives = [], []
        for day in window:
            result = next((r for r in by_day[day] if r.strategy_id == strategy_id and r.strategy_version == version), None)
            if not _usable(result):
                missing_archives.append(day.isoformat())
                continue
            if result.quality == "degraded":
                degraded_archives.append(day.isoformat())
            for candidate in result.payload.candidates:
                data = candidate.model_dump(mode="json")
                row = stocks.setdefault(candidate.instrument_id, {
                    "instrument_id": candidate.instrument_id, "name": candidate.name,
                    "archive_records": [], "limit_up_records": [],
                })
                row["candidate"] = data
                row["archive_records"].append({
                    "trade_date": day.isoformat(), "result_id": result.result_id,
                    "strategy_version": version, "generated_at": result.generated_at.isoformat(),
                    "candidate": data,
                })
        for day, membership in memberships.items():
            if membership is None:
                continue
            for instrument_id in membership.instrument_ids:
                row = stocks.get(instrument_id)
                if row is None:
                    continue
                eligible = [r["trade_date"] for r in row["archive_records"] if r["trade_date"] <= day.isoformat()]
                if eligible:
                    row["limit_up_records"].append({
                        "trade_date": day.isoformat(), "archive_dates": eligible,
                        "basis": "daily_closed_limit_up_membership",
                        "source": membership.metadata.model_dump(mode="json"),
                    })
        missing_memberships = [day.isoformat() for day, value in memberships.items() if value is None]
        rows = [row for row in stocks.values() if row["limit_up_records"]]
        rows.sort(key=lambda row: (-len(row["limit_up_records"]), row["instrument_id"]))
        strategies[strategy_id] = {
            "strategy_version": version, "comparison": comparison,
            "followup": {
                "quality": "unavailable" if not window else "partial" if missing_archives or missing_memberships or degraded_archives else "complete",
                "missing_archive_dates": missing_archives,
                "degraded_archive_dates": degraded_archives,
                "missing_limit_up_dates": missing_memberships,
                "rows": rows,
            },
        }
    return {"contract": INSIGHTS_CONTRACT, "schema_version": 1,
            "trade_date": target.isoformat(), "window_dates": [d.isoformat() for d in window],
            "includes_entry_day": True, "limit_up_basis": "closing_price", "strategies": strategies}
