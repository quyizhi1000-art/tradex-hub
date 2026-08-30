"""Provider-neutral daily A-share limit-sentiment gateway."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractMetadata, QualityStatus
from .limit_sentiment_contracts import LimitSentimentDailyV1
from .providers.limit_sentiment import map_tushare_limit_sentiment


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _router(router: Any | None) -> Any:
    if router is not None:
        return router
    from tradex.data_sources import get_router, register_all_sources

    register_all_sources()
    return get_router()


def _date(value: date | str, label: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD") from exc


def _revision(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: item.isoformat(),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fetch_limit_sentiment_daily(
    trade_date: date | str,
    previous_trade_date: date | str,
    *,
    router: Any | None = None,
    now: datetime | None = None,
) -> LimitSentimentDailyV1:
    requested = _date(trade_date, "trade_date")
    previous = _date(previous_trade_date, "previous_trade_date")
    fetched_at = now or datetime.now(SHANGHAI)
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("gateway now must include a timezone")
    fetched_at = fetched_at.astimezone(SHANGHAI)

    def validate(payload: Any, route_provider: str) -> LimitSentimentDailyV1:
        mapped = map_tushare_limit_sentiment(
            payload,
            requested_date=requested,
            previous_trade_date=previous,
        )
        provider = mapped.pop("provider")
        request_id = mapped.pop("provider_request_id")
        provider_as_of = mapped.pop("provider_as_of")
        flags = ["provider_timestamp_missing"] if provider_as_of is None else []
        if mapped["previous_feedback_coverage"] < 1:
            flags.append("previous_feedback_partial")
        source_revision = _revision(mapped)
        return LimitSentimentDailyV1(
            metadata=ContractMetadata(
                contract="limit_sentiment_daily.v1",
                provider=provider or route_provider,
                provider_request_id=request_id,
                provider_as_of=provider_as_of,
                fetched_at=fetched_at,
                quality=(QualityStatus.DEGRADED if flags else QualityStatus.ACCEPTED),
                quality_flags=tuple(flags),
            ),
            source_revision=source_revision,
            **mapped,
        )

    result, _provider = _router(router).route_validated(
        "limit_sentiment_daily",
        validate,
        trade_date=requested.isoformat(),
        previous_trade_date=previous.isoformat(),
    )
    return result


__all__ = ["fetch_limit_sentiment_daily"]
