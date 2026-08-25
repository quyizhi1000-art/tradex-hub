"""Provider mappings for canonical historical index turnover amounts."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from ..contracts import (
    ContractMetadata,
    IndexDailyAmountSeriesV1,
    IndexDailyAmountV1,
    IndexIntradayAmountSeriesV1,
    IndexMinuteAmountV1,
    QualityStatus,
)
from .market_overview import finite_number


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _records(payload: Any, *, capability: str) -> list[dict[str, Any]]:
    if hasattr(payload, "to_dict"):
        payload = payload.to_dict(orient="records")
    if not isinstance(payload, (list, tuple)):
        raise ValueError(f"{capability} payload must be a record list")
    if not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"{capability} rows must be objects")
    return list(payload)


def _first(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def map_index_intraday_amount(
    payload: Any,
    provider: str,
    *,
    instrument_id: str,
    fetched_at: datetime,
) -> IndexIntradayAmountSeriesV1:
    """Normalize one provider payload before the router records success."""

    if fetched_at.tzinfo is None:
        raise ValueError("index intraday amount fetched_at must include a timezone")
    local_fetched_at = fetched_at.astimezone(_SHANGHAI)
    points: list[IndexMinuteAmountV1] = []
    for row in _records(payload, capability="index intraday amount"):
        raw_datetime = _first(row, "datetime", "日期时间")
        raw_date = _first(row, "date", "日期")
        raw_time = _first(row, "time", "时间")
        if raw_date in (None, "") and raw_datetime not in (None, ""):
            raw_date = str(raw_datetime).split(" ", 1)[0]
        if raw_time in (None, "") and raw_datetime not in (None, ""):
            parts = str(raw_datetime).split(" ", 1)
            raw_time = parts[1] if len(parts) == 2 else None
        try:
            trading_date = date.fromisoformat(str(raw_date)[:10])
            minute = time.fromisoformat(str(raw_time)[:5])
        except (TypeError, ValueError) as exc:
            raise ValueError("index minute date/time is invalid") from exc
        if trading_date > local_fetched_at.date():
            raise ValueError("index minute date cannot be in the future")
        amount_cny = finite_number(
            _first(row, "amount_cny", "amount", "成交额(元)", "成交额")
        )
        if amount_cny is None or amount_cny < 0:
            raise ValueError("index minute amount must be a non-negative CNY value")
        points.append(
            IndexMinuteAmountV1(
                trading_date=trading_date,
                minute=minute.replace(second=0, microsecond=0),
                amount_cny=amount_cny,
            )
        )

    points.sort(key=lambda item: (item.trading_date, item.minute))
    provider_as_of = None
    if points:
        last = points[-1]
        provider_as_of = datetime.combine(
            last.trading_date,
            last.minute,
            tzinfo=_SHANGHAI,
        )
    return IndexIntradayAmountSeriesV1(
        metadata=ContractMetadata(
            contract="index_intraday_amount.v1",
            provider=provider,
            provider_as_of=provider_as_of,
            fetched_at=fetched_at,
            quality=QualityStatus.ACCEPTED,
        ),
        instrument_id=instrument_id,
        points=tuple(points),
    )


def map_index_daily_amount(
    payload: Any,
    provider: str,
    *,
    instrument_id: str,
    fetched_at: datetime,
) -> IndexDailyAmountSeriesV1:
    """Normalize completed-session index amounts before router success."""

    if fetched_at.tzinfo is None:
        raise ValueError("index daily amount fetched_at must include a timezone")
    local_fetched_at = fetched_at.astimezone(_SHANGHAI)
    points: list[IndexDailyAmountV1] = []
    for row in _records(payload, capability="index daily amount"):
        raw_date = _first(row, "trading_date", "date", "日期")
        try:
            trading_date = date.fromisoformat(str(raw_date)[:10])
        except (TypeError, ValueError) as exc:
            raise ValueError("index daily amount date is invalid") from exc
        if trading_date > local_fetched_at.date():
            raise ValueError("index daily amount date cannot be in the future")
        amount_cny = finite_number(
            _first(row, "amount_cny", "amount", "成交额(元)", "成交额")
        )
        if amount_cny is None or amount_cny <= 0:
            raise ValueError("index daily amount must be a positive CNY value")
        points.append(
            IndexDailyAmountV1(
                trading_date=trading_date,
                amount_cny=amount_cny,
            )
        )

    points.sort(key=lambda item: item.trading_date)
    provider_as_of = (
        datetime.combine(points[-1].trading_date, time(15, 0), tzinfo=_SHANGHAI)
        if points
        else None
    )
    return IndexDailyAmountSeriesV1(
        metadata=ContractMetadata(
            contract="index_daily_amount.v1",
            provider=provider,
            provider_as_of=provider_as_of,
            fetched_at=fetched_at,
            quality=QualityStatus.ACCEPTED,
        ),
        instrument_id=instrument_id,
        points=tuple(points),
    )


__all__ = ["map_index_daily_amount", "map_index_intraday_amount"]
