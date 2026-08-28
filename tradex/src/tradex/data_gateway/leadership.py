"""Provider-neutral acquisition boundary for dashboard leadership data."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .contracts import (
    BoardLeaderSnapshotV2,
    ContractMetadata,
    LeaderQuoteSeriesV1,
    StockSectorProfileSeriesV1,
)
from .providers.leadership import (
    map_board_leader_frame,
    map_leader_quote_frame,
    map_stock_sector_profile_frame,
    payload_provider,
    provider_watermark,
)
from .providers.securities import canonical_instrument_id, frame_request_id
from .quality import (
    assess_board_leaders,
    assess_leader_quotes,
    assess_stock_sector_profiles,
)


_SHANGHAI = ZoneInfo("Asia/Shanghai")


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


def _normalise_codes(values: Iterable[str] | str) -> tuple[str, ...]:
    candidates = values.split(",") if isinstance(values, str) else list(values)
    codes: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        instrument_id = canonical_instrument_id(str(value).strip())
        code = instrument_id[:6]
        if code not in seen:
            seen.add(code)
            codes.append(code)
    if not codes:
        raise ValueError("at least one A-share code is required")
    return tuple(codes)


def _payload_provider(frame: Any, route_provider: str) -> str:
    records = frame.to_dict(orient="records") if hasattr(frame, "to_dict") else []
    return payload_provider(frame, route_provider, records)


def fetch_leader_quotes(
    codes: Iterable[str] | str,
    *,
    router: Any | None = None,
    now: datetime | None = None,
) -> LeaderQuoteSeriesV1:
    requested_codes = _normalise_codes(codes)
    requested_ids = tuple(canonical_instrument_id(code) for code in requested_codes)
    fetched_at = _now(now)

    def validate(frame: Any, route_provider: str) -> LeaderQuoteSeriesV1:
        if getattr(frame, "attrs", {}).get("source_valid") is False:
            raise RuntimeError(
                f"{route_provider} explicitly marked its leader quotes invalid"
            )
        quotes = map_leader_quote_frame(frame, requested_codes=requested_codes)
        provider_as_of = provider_watermark(quotes)
        quality, flags = assess_leader_quotes(
            quotes=quotes,
            requested_total=len(requested_codes),
            provider_as_of=provider_as_of,
        )
        return LeaderQuoteSeriesV1(
            metadata=ContractMetadata(
                contract="leader_quote.v1",
                provider=_payload_provider(frame, route_provider),
                provider_request_id=frame_request_id(frame),
                provider_as_of=provider_as_of,
                fetched_at=fetched_at,
                quality=quality,
                quality_flags=flags,
            ),
            requested_instrument_ids=requested_ids,
            quotes=quotes,
        )

    series, _route_provider = _router(router).route_validated(
        "leader_quotes",
        validate,
        symbols=list(requested_codes),
    )
    return series


def fetch_stock_sector_profiles(
    codes: Iterable[str] | str,
    *,
    trade_date: str,
    router: Any | None = None,
    now: datetime | None = None,
) -> StockSectorProfileSeriesV1:
    requested_codes = _normalise_codes(codes)
    requested_ids = tuple(canonical_instrument_id(code) for code in requested_codes)
    try:
        requested_date = date.fromisoformat(trade_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("trade_date must use YYYY-MM-DD") from exc
    fetched_at = _now(now)

    def validate(frame: Any, route_provider: str) -> StockSectorProfileSeriesV1:
        if getattr(frame, "attrs", {}).get("source_valid") is not True:
            raise RuntimeError("股票行业 profile 未通过源有效性校验")
        profiles = map_stock_sector_profile_frame(
            frame,
            requested_codes=requested_codes,
            route_provider=route_provider,
        )
        quality, flags = assess_stock_sector_profiles(
            profiles=profiles,
            requested_total=len(requested_codes),
            trade_date=requested_date.isoformat(),
        )
        return StockSectorProfileSeriesV1(
            metadata=ContractMetadata(
                contract="stock_sector_profile.v1",
                provider=_payload_provider(frame, route_provider),
                provider_request_id=frame_request_id(frame),
                provider_as_of=provider_watermark(profiles),
                fetched_at=fetched_at,
                quality=quality,
                quality_flags=flags,
            ),
            trade_date=requested_date,
            requested_instrument_ids=requested_ids,
            profiles=profiles,
        )

    series, _route_provider = _router(router).route_validated(
        "stock_sector_profiles",
        validate,
        codes=list(requested_codes),
    )
    return series


def fetch_board_leader_snapshot(
    board_code: str,
    *,
    limit: int = 3,
    speed_order: Literal["desc", "asc"] = "desc",
    source_hint: str | None = None,
    router: Any | None = None,
    now: datetime | None = None,
) -> BoardLeaderSnapshotV2:
    code = str(board_code or "").strip().upper()
    if re.fullmatch(r"BK\d+", code) is None:
        raise ValueError("board_code must be an Eastmoney BK code")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ValueError("limit must be an integer between 1 and 10")
    if speed_order not in {"desc", "asc"}:
        raise ValueError("speed_order must be desc or asc")
    fetched_at = _now(now)

    def validate(frame: Any, route_provider: str) -> BoardLeaderSnapshotV2:
        if getattr(frame, "attrs", {}).get("source_valid") is False:
            raise RuntimeError(
                f"{route_provider} explicitly marked its board leaders invalid"
            )
        leaders = sorted(
            map_board_leader_frame(
                frame,
                board_code=code,
                route_provider=route_provider,
            ),
            key=lambda item: item.speed_pct,
            reverse=speed_order == "desc",
        )[:limit]
        provider_as_of = provider_watermark(leaders)
        quality, flags = assess_board_leaders(
            leaders=leaders,
            provider_as_of=provider_as_of,
            source_row_count=len(frame),
        )
        return BoardLeaderSnapshotV2(
            metadata=ContractMetadata(
                contract="board_leader.v2",
                schema_version=2,
                provider=_payload_provider(frame, route_provider),
                provider_request_id=frame_request_id(frame),
                provider_as_of=provider_as_of,
                fetched_at=fetched_at,
                quality=quality,
                quality_flags=flags,
            ),
            board_code=code,
            speed_order=speed_order,
            leaders=leaders,
        )

    snapshot, _route_provider = _router(router).route_validated(
        "board_leaders",
        validate,
        board_code=code,
        limit=limit,
        source_hint=source_hint,
        speed_order=speed_order,
    )
    return snapshot


def leader_quotes_to_legacy_records(series: LeaderQuoteSeriesV1) -> list[dict[str, Any]]:
    return [
        {
            "代码": item.instrument_id[:6],
            "名称": item.name,
            "最新价": item.last,
            "涨跌幅": item.change_pct,
        }
        for item in series.quotes
    ]


def stock_sector_profiles_to_legacy_records(
    series: StockSectorProfileSeriesV1,
) -> list[dict[str, Any]]:
    return [
        {
            "代码": item.instrument_id[:6],
            "名称": item.name,
            "行业": item.industry,
            "地域": item.region,
            "概念标签": list(item.concept_tags),
            "provider_as_of": item.provider_as_of.isoformat(timespec="seconds"),
            "source": item.provider_variant,
        }
        for item in series.profiles
    ]


def board_leader_snapshot_to_legacy_payload(
    snapshot: BoardLeaderSnapshotV2,
) -> dict[str, Any]:
    metadata = snapshot.metadata
    return {
        "status": "ready",
        "source": metadata.provider,
        "provider_as_of": (
            metadata.provider_as_of.isoformat(timespec="seconds")
            if metadata.provider_as_of
            else None
        ),
        "fetched_at": metadata.fetched_at.isoformat(timespec="seconds"),
        "stale": False,
        "method": "board_constituents",
        "items": [
            {
                "instrument_id": item.instrument_id,
                "code": item.instrument_id[:6],
                "name": item.name,
                "price": item.price,
                "change_pct": item.change_pct,
                "speed_pct": item.speed_pct,
                "amount": item.amount_cny,
                "turnover": item.turnover_pct,
                "flow_amount": item.main_net_inflow_cny,
                "flow_ratio": item.main_net_inflow_pct,
                "provider_as_of": (
                    item.provider_as_of.isoformat(timespec="seconds")
                    if item.provider_as_of
                    else None
                ),
                "source": item.provider_variant,
            }
            for item in snapshot.leaders
        ],
    }


__all__ = [
    "board_leader_snapshot_to_legacy_payload",
    "fetch_board_leader_snapshot",
    "fetch_leader_quotes",
    "fetch_stock_sector_profiles",
    "leader_quotes_to_legacy_records",
    "stock_sector_profiles_to_legacy_records",
]
