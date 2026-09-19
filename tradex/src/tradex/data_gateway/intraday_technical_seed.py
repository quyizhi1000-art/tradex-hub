"""Exact completed sessions needed to project today's daily MACD and KDJ."""

from datetime import date, datetime
import re

from pydantic import Field, ValidationError

from .contracts import ContractMetadata, ContractModel, QualityStatus
from .stock_technicals_contracts import StockTechnicalPointV1


class IntradayTechnicalSeedPointV1(StockTechnicalPointV1):
    high: float = Field(gt=0, allow_inf_nan=False)
    low: float = Field(gt=0, allow_inf_nan=False)
    adjustment_factor: float = Field(gt=0, allow_inf_nan=False)
    adjusted_close: float = Field(gt=0, allow_inf_nan=False)


class IntradayTechnicalSeedDayV1(ContractModel):
    metadata: ContractMetadata
    trade_date: date
    points: tuple[IntradayTechnicalSeedPointV1, ...]
    rejected_count: int = Field(ge=0)
    st_trade_date: date | None = None
    special_treatment_ids: tuple[str, ...] = ()


def map_intraday_seed(raw, *, requested: date, st_date: date | None,
                      provider: str, now: datetime) -> IntradayTechnicalSeedDayV1:
    if not isinstance(raw, dict) or raw.get("trade_date") != requested.isoformat():
        raise ValueError("seed date mismatch")
    rows = raw.get("indicators")
    if not isinstance(rows, list) or not rows:
        raise ValueError("published seed unavailable")
    points, seen, rejected = [], set(), 0
    for row in rows:
        if not isinstance(row, dict) or row.get("trade_date") != requested.strftime("%Y%m%d"):
            raise ValueError("seed row date mismatch")
        instrument = row.get("ts_code")
        if not isinstance(instrument, str) or instrument in seen:
            raise ValueError("duplicate seed instrument")
        seen.add(instrument)
        try:
            point = IntradayTechnicalSeedPointV1(
                instrument_id=instrument, close=row.get("close"),
                amount_cny=float(row["amount"]) * 1000,
                dif=row.get("macd_dif"), dea=row.get("macd_dea"),
                k=row.get("kdj_k"), d=row.get("kdj_d"), j=row.get("kdj_j"),
                high=row.get("high"), low=row.get("low"),
                adjustment_factor=row.get("adj_factor"), adjusted_close=row.get("close_qfq"),
            )
            if not point.low <= point.close <= point.high:
                raise ValueError("invalid seed range")
        except (ValidationError, ValueError, KeyError, TypeError):
            rejected += 1
            continue
        points.append(point)
    if not points:
        raise ValueError("no usable seed indicators")
    st_ids = []
    if st_date is not None:
        st_rows = raw.get("special_treatment")
        if not isinstance(st_rows, list) or not st_rows:
            raise ValueError("current-session ST membership unavailable")
        for row in st_rows:
            instrument = row.get("ts_code")
            if (row.get("trade_date") != st_date.strftime("%Y%m%d")
                    or not isinstance(instrument, str)
                    or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", instrument) or instrument in st_ids):
                raise ValueError("invalid current-session ST membership")
            st_ids.append(instrument)
    return IntradayTechnicalSeedDayV1(
        metadata=ContractMetadata(contract="intraday_technical_seed_day.v1", provider=provider,
            provider_request_id=raw.get("request_id"), provider_as_of=None, fetched_at=now,
            quality=QualityStatus.DEGRADED, quality_flags=("provider_timestamp_unavailable",)),
        trade_date=requested, points=tuple(sorted(points, key=lambda p: p.instrument_id)),
        rejected_count=rejected, st_trade_date=st_date, special_treatment_ids=tuple(sorted(st_ids)),
    )


def fetch_intraday_seed_day(day: date, *, st_date: date | None, now: datetime,
                            router=None) -> IntradayTechnicalSeedDayV1:
    if router is None:
        from tradex.data_sources import get_router, register_all_sources
        register_all_sources()
        router = get_router()
    result, _ = router.route_validated(
        "stock_selection_technicals",
        lambda raw, provider: map_intraday_seed(raw, requested=day, st_date=st_date,
                                               provider=provider, now=now),
        trade_date=day.strftime("%Y%m%d"), include_seed=True,
        check_st=st_date is not None, st_trade_date=st_date.strftime("%Y%m%d") if st_date else "",
        deadline_seconds=20, provider_deadline_seconds=20,
    )
    return result
