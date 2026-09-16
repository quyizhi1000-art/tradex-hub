"""Provider-neutral gateway for the low-frequency instrument relationship catalog."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
INDUSTRY_COVERAGE_CHECKED = "industry_coverage_checked:v1"
MARKET_INDUSTRY_CHECKED = "market_industry_coverage_checked:v1"


def validate_market_industries(raw: Any, requested: date) -> dict[str, Any]:
    if (not isinstance(raw, Mapping) or raw.get("contract") != "instrument_industry_blocks_source.v1"
            or raw.get("schema_version") != 1 or raw.get("as_of") != requested.isoformat()
            or raw.get("taxonomy") != "ths"):
        raise RuntimeError("invalid market industry contract or date")
    ids = [row["ts_code"] for row in raw["stocks"]]
    if not ids or len(ids) != len(set(ids)):
        raise RuntimeError("invalid market industry stock universe")
    members = raw["memberships"]
    mapped = [row["instrument_id"] for row in members]
    if (len(mapped) != len(set(mapped)) or not set(mapped) <= set(ids)
            or any(not row.get("code") or not row.get("name", "").strip() for row in members)):
        raise RuntimeError("invalid or conflicting market industry membership")
    missing = raw["missing_instruments"]
    if (len(missing) != len(set(missing)) or set(missing) != set(ids) - set(mapped)
            or len(missing) * 100 > len(ids)):
        raise RuntimeError("market industry coverage is incomplete or below 99%")
    conflicts = raw.get("conflicting_instruments", [])
    if len(conflicts) != len(set(conflicts)) or not set(conflicts) <= set(missing):
        raise RuntimeError("invalid market industry conflict evidence")
    return dict(raw)


def fetch_market_industry_source(as_of: date | str | None = None, *, router=None) -> dict[str, Any]:
    requested = _as_of(as_of)
    bundle, _ = _router(router).route_validated(
        "instrument_taxonomy", lambda raw, _: validate_market_industries(raw, requested),
        deadline_seconds=300, provider_deadline_seconds=300,
        as_of=requested.strftime("%Y%m%d"), section="industry_blocks",
    )
    return bundle


def validate_industry_coverage(source: Mapping[str, Any], requested: date) -> None:
    """Require complete paths or explicitly rechecked absences, with a 99% floor."""
    stock_ids = [str(row.get("ts_code") or "").strip() for row in source["stocks"]]
    if not stock_ids or any(not key for key in stock_ids) or len(set(stock_ids)) != len(stock_ids):
        raise RuntimeError("instrument taxonomy stock master contains missing or duplicate ids")
    universe = set(stock_ids)
    classified = set()
    for row in source["sw_memberships"]:
        key = str(row.get("ts_code") or "").strip()
        if key not in universe or key in classified:
            raise RuntimeError("industry membership contains foreign or duplicate instruments")
        if any(not str(row.get(field) or "").strip() for field in (
            "l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name",
        )):
            raise RuntimeError("industry membership contains incomplete paths")
        for field in ("in_date", "out_date"):
            raw_date = str(row.get(field) or "").strip().replace("-", "")
            try:
                value = datetime.strptime(raw_date, "%Y%m%d").date() if raw_date else None
            except ValueError as exc:
                raise RuntimeError("industry membership contains invalid dates") from exc
            if value and ((field == "in_date" and value > requested)
                          or (field == "out_date" and value <= requested)):
                raise RuntimeError("industry membership is not effective on the catalog date")
        if row.get("is_new") == "N":
            raise RuntimeError("industry membership is not current")
        classified.add(key)
    missing = universe - classified
    confirmed = source.get("industry_missing_instruments", [])
    if (not isinstance(confirmed, (list, tuple))
            or any(not isinstance(key, str) for key in confirmed)
            or len(set(confirmed)) != len(confirmed) or set(confirmed) != missing):
        raise RuntimeError("industry coverage has unverified missing instruments")
    if len(missing) * 100 > len(universe):
        raise RuntimeError(f"industry coverage below 99%: {len(missing)}/{len(universe)} missing")


def _router(router: Any | None) -> Any:
    if router is not None:
        return router
    from tradex.data_sources import get_router, register_all_sources

    register_all_sources()
    return get_router()


def _as_of(value: date | str | None) -> date:
    if value is None or str(value).strip() == "":
        return datetime.now(SHANGHAI).date()
    if isinstance(value, date):
        return value
    raw = str(value).strip().replace("-", "")
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError("taxonomy as_of must be YYYY-MM-DD or YYYYMMDD") from exc


def _validate_bundle(raw: Any, route_provider: str, requested: date) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise RuntimeError("instrument taxonomy provider returned a non-object bundle")
    if (
        raw.get("contract") != "instrument_taxonomy_source_bundle.v1"
        or raw.get("schema_version") != 1
        or str(raw.get("as_of") or "") != requested.isoformat()
    ):
        raise RuntimeError("instrument taxonomy provider returned the wrong contract or date")
    result = dict(raw)
    for name in ("stocks", "companies", "sw_memberships", "business_segments"):
        rows = result.get(name)
        if not isinstance(rows, (list, tuple)) or any(not isinstance(item, Mapping) for item in rows):
            raise RuntimeError(f"instrument taxonomy provider returned invalid {name}")
    if not result["stocks"] or not result["business_segments"]:
        raise RuntimeError("instrument taxonomy provider omitted required stock or business rows")
    instrument_ids = [str(item.get("ts_code") or "").strip() for item in result["stocks"]]
    if any(not item for item in instrument_ids) or len(instrument_ids) != len(set(instrument_ids)):
        raise RuntimeError("instrument taxonomy stock master contains missing or duplicate ids")
    validate_industry_coverage(result, requested)
    if "market_industries" in result:
        validate_market_industries(result["market_industries"], requested)
        if set(instrument_ids) != {row["ts_code"] for row in result["market_industries"]["stocks"]}:
            raise RuntimeError("market industry universe differs from the stock master")
    providers = tuple(dict.fromkeys((route_provider, *result.get("source_providers", ()))))
    result["source_providers"] = list(providers)
    return result


def fetch_stock_relationship_source_bundle(
    as_of: date | str | None = None,
    *,
    router: Any | None = None,
) -> dict[str, Any]:
    requested = _as_of(as_of)
    bundle, _provider = _router(router).route_validated(
        "instrument_taxonomy",
        lambda raw, provider: _validate_bundle(raw, provider, requested),
        # This low-frequency background refresh intentionally spans several
        # bounded whole-market tables; it must not inherit the interactive
        # request deadline used by dashboard and MCP reads.
        deadline_seconds=300,
        provider_deadline_seconds=300,
        as_of=requested.strftime("%Y%m%d"),
    )
    return bundle


__all__ = ["fetch_stock_relationship_source_bundle"]
