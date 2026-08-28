"""Provider-neutral gateway for exact exchange-index minute bars."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import IndexIntradaySeriesV1
from .providers.index_intraday import map_index_intraday_series
from .providers.securities import canonical_instrument_id


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _router(router: Any | None) -> Any:
    if router is not None:
        return router
    from tradex.data_sources import get_router, register_all_sources

    register_all_sources()
    return get_router()


def fetch_index_intraday_series(
    symbol: str,
    *,
    router: Any | None = None,
    now: datetime | None = None,
    days: int = 5,
) -> IndexIntradaySeriesV1:
    instrument_id = canonical_instrument_id(symbol)
    if instrument_id.endswith(".BJ"):
        raise ValueError("index intraday series supports SH/SZ indices only")
    fetched_at = now or datetime.now(_SHANGHAI)
    if fetched_at.tzinfo is None:
        raise ValueError("index intraday now must include a timezone")

    def validate(payload: Any, provider: str) -> IndexIntradaySeriesV1:
        return map_index_intraday_series(
            payload,
            provider,
            instrument_id=instrument_id,
            fetched_at=fetched_at,
        )

    series, _provider = _router(router).route_validated(
        "index_intraday_series",
        validate,
        symbol=instrument_id,
        days=days,
    )
    return series


__all__ = ["fetch_index_intraday_series"]
