"""Provider-neutral gateway for the low-frequency instrument relationship catalog."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


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
