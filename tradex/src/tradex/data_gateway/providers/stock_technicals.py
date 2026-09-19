"""Normalize the published forward-adjusted indicator table before routing success."""

from datetime import date, datetime

from pydantic import ValidationError

from ..contracts import ContractMetadata, QualityStatus
from ..stock_technicals_contracts import StockTechnicalDayV1, StockTechnicalPointV1


def map_stock_technical_day(raw, *, requested: date, provider: str,
                           fetched_at: datetime, check_st: bool) -> StockTechnicalDayV1:
    if not isinstance(raw, dict) or raw.get("trade_date") != requested.isoformat():
        raise ValueError("technical source returned the wrong date")
    rows = raw.get("indicators")
    if not isinstance(rows, list) or not rows:
        raise ValueError("technical source returned no indicator rows")
    expected = requested.strftime("%Y%m%d")
    points, rejected, seen = [], [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get("trade_date") != expected:
            raise ValueError("technical row has an invalid date")
        instrument = row.get("ts_code")
        if not isinstance(instrument, str) or instrument in seen:
            raise ValueError("duplicate or invalid technical instrument")
        seen.add(instrument)
        try:
            point = StockTechnicalPointV1(
                instrument_id=instrument, close=row.get("close"),
                amount_cny=float(row["amount"]) * 1000,
                dif=row.get("macd_dif"), dea=row.get("macd_dea"),
                k=row.get("kdj_k"), d=row.get("kdj_d"), j=row.get("kdj_j"),
            )
        except (ValidationError, TypeError, ValueError, KeyError):
            rejected.append(instrument)
            continue
        points.append(point)
    if not points:
        raise ValueError("technical source has no valid indicators")
    st_ids = None
    if check_st:
        st_rows = raw.get("special_treatment")
        if not isinstance(st_rows, list) or not st_rows:
            raise ValueError("signal-day ST membership is unavailable")
        st_ids = []
        for row in st_rows:
            if not isinstance(row, dict) or row.get("trade_date") != expected:
                raise ValueError("ST membership returned the wrong date")
            instrument = row.get("ts_code")
            if not isinstance(instrument, str) or instrument in st_ids:
                raise ValueError("duplicate or invalid ST membership")
            st_ids.append(instrument)
        st_ids = tuple(sorted(st_ids))
    return StockTechnicalDayV1(
        metadata=ContractMetadata(
            contract="stock_technical_day.v1", provider=provider,
            provider_request_id=raw.get("request_id"), fetched_at=fetched_at,
            provider_as_of=None,
            quality=QualityStatus.DEGRADED,
            quality_flags=("provider_timestamp_unavailable", *(
                (f"invalid_indicator_rows:{len(rejected)}",) if rejected else ()
            )),
        ),
        trade_date=requested, points=tuple(sorted(points, key=lambda p: p.instrument_id)),
        rejected_instrument_ids=tuple(sorted(rejected)), special_treatment_ids=st_ids,
    )
