"""Provider mappings for exact exchange-index minute bars."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from ..contracts import (
    ContractMetadata,
    IndexIntradaySeriesV1,
    IndexMinuteQuoteV1,
    QualityStatus,
)
from .market_overview import finite_number


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return payload
    if hasattr(payload, "to_dict"):
        return list(payload.to_dict(orient="records"))
    raise RuntimeError("index intraday provider returned an unsupported payload")


def map_index_intraday_series(
    payload: Any,
    provider: str,
    *,
    instrument_id: str,
    fetched_at: datetime,
) -> IndexIntradaySeriesV1:
    points: list[IndexMinuteQuoteV1] = []
    request_id: str | None = None
    for row in _records(payload):
        raw = str(row.get("datetime") or row.get("时间") or "")
        try:
            observed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("index intraday datetime is invalid") from exc
        values = {
            name: finite_number(row.get(name))
            for name in ("open", "close", "high", "low", "amount_cny")
        }
        if any(value is None for value in values.values()):
            raise ValueError("index intraday OHLC and amount must be complete")
        request_id = request_id or row.get("provider_request_id")
        points.append(
            IndexMinuteQuoteV1(
                trading_date=observed.date(),
                minute=observed.time().replace(second=0, microsecond=0),
                **values,
            )
        )
    points.sort(key=lambda item: (item.trading_date, item.minute))
    provider_as_of = (
        datetime.combine(points[-1].trading_date, points[-1].minute, tzinfo=_SHANGHAI)
        if points
        else None
    )
    return IndexIntradaySeriesV1(
        metadata=ContractMetadata(
            contract="index_intraday_series.v1",
            provider=provider,
            provider_request_id=str(request_id) if request_id else None,
            provider_as_of=provider_as_of,
            fetched_at=fetched_at,
            quality=QualityStatus.ACCEPTED,
        ),
        instrument_id=instrument_id,
        points=tuple(points),
    )


__all__ = ["map_index_intraday_series"]
