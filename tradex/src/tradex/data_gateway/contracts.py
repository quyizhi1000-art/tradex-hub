"""Versioned, provider-neutral contracts at the market-data boundary."""

from __future__ import annotations

import math
from datetime import date, datetime, time
from enum import Enum
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class QualityStatus(str, Enum):
    """Whether a canonical payload is safe for downstream consumption."""

    ACCEPTED = "accepted"
    DEGRADED = "degraded"
    REJECTED = "rejected"


class ContractModel(BaseModel):
    """Strict immutable base for data crossing the gateway boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ContractMetadata(ContractModel):
    contract: str = Field(
        default="market_overview.v1",
        pattern=r"^[a-z][a-z0-9_]*\.v\d+$",
    )
    schema_version: int = Field(default=1, ge=1)
    provider: str = Field(min_length=1)
    provider_request_id: str | None = None
    provider_as_of: datetime | None = None
    fetched_at: datetime
    quality: QualityStatus
    quality_flags: tuple[str, ...] = ()

    @field_validator("provider_as_of", "fetched_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("gateway timestamps must include a timezone")
        return value


class MarketStateV1(ContractModel):
    label: str = Field(min_length=1)
    is_open: bool


class IndexQuoteV1(ContractModel):
    """One canonical index quote.

    ``change_pct`` is expressed in percentage points: ``5.23`` means 5.23%.
    Amount values are always Chinese yuan, never lots or ten-thousand yuan.
    """

    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ|TI)$")
    name: str = Field(min_length=1)
    available: bool
    value: float | None = Field(default=None, ge=0)
    change: float | None = None
    change_pct: float | None = None
    previous_close: float | None = Field(default=None, ge=0)
    open: float | None = Field(default=None, ge=0)
    high: float | None = Field(default=None, ge=0)
    low: float | None = Field(default=None, ge=0)
    amount_cny: float | None = Field(default=None, ge=0)
    provider_as_of: datetime | None = None

    @field_validator("value", "change", "change_pct", "previous_close", "open", "high", "low", "amount_cny")
    @classmethod
    def require_finite_number(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("market numbers must be finite")
        return value

    @field_validator("provider_as_of")
    @classmethod
    def require_provider_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("index provider_as_of must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_quote_shape(self) -> "IndexQuoteV1":
        if self.available and self.value is None:
            raise ValueError("an available index quote requires value")
        if self.high is not None and self.low is not None and self.high < self.low:
            raise ValueError("index high cannot be lower than index low")
        return self


class ParticipationIndexV1(ContractModel):
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ|TI)$")
    name: str = Field(min_length=1)
    change_pct: float
    provider_as_of: datetime | None = None

    @field_validator("change_pct")
    @classmethod
    def require_finite_change(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("index change_pct must be finite")
        return value

    @field_validator("provider_as_of")
    @classmethod
    def require_provider_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("provider_as_of must include a timezone")
        return value


class MarketTurnoverV1(ContractModel):
    available: bool
    reason: str | None = None
    scope: Literal["all_a_shares"] | None = None
    metric: Literal["amount_cny"] | None = None
    as_of: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    today_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    previous_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    today_amount_cny: float | None = Field(default=None, ge=0)
    previous_same_time_amount_cny: float | None = Field(default=None, ge=0)
    difference_cny: float | None = None
    direction: Literal["expand", "shrink", "flat"] | None = None
    label: Literal["放量", "缩量", "持平"] | None = None

    @model_validator(mode="after")
    def validate_availability(self) -> "MarketTurnoverV1":
        required = (
            self.scope,
            self.metric,
            self.as_of,
            self.today_date,
            self.previous_date,
            self.today_amount_cny,
            self.previous_same_time_amount_cny,
            self.difference_cny,
            self.direction,
            self.label,
        )
        if self.available and any(value is None for value in required):
            raise ValueError("available turnover requires a complete comparison")
        if not self.available and not self.reason:
            raise ValueError("unavailable turnover requires a reason")
        if self.available and self.reason is not None:
            raise ValueError("available turnover cannot include an error reason")
        if self.available:
            expected = self.today_amount_cny - self.previous_same_time_amount_cny
            if not math.isclose(expected, self.difference_cny, rel_tol=1e-9, abs_tol=1e-6):
                raise ValueError("turnover difference does not match its amounts")
        return self


class IndexDailyAmountV1(ContractModel):
    """One provider-neutral completed-session amount for an exchange index."""

    trading_date: date
    amount_cny: float = Field(gt=0)

    @field_validator("amount_cny")
    @classmethod
    def require_finite_amount(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("index daily amount must be finite")
        return value


class IndexDailyAmountSeriesV1(ContractModel):
    """Validated daily amounts for one canonical index."""

    metadata: ContractMetadata
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ)$")
    points: tuple[IndexDailyAmountV1, ...]

    @model_validator(mode="after")
    def validate_series(self) -> "IndexDailyAmountSeriesV1":
        if (
            self.metadata.contract != "index_daily_amount.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError("index daily amount requires index_daily_amount.v1 metadata")
        if not self.points:
            raise ValueError("index daily amount series cannot be empty")
        dates = [item.trading_date for item in self.points]
        if dates != sorted(dates):
            raise ValueError("index daily amount points must be ordered")
        if len(dates) != len(set(dates)):
            raise ValueError("index daily amount points must be unique")
        return self


class IndexMinuteAmountV1(ContractModel):
    """One provider-neutral minute amount for an exchange index."""

    trading_date: date
    minute: time
    amount_cny: float = Field(ge=0)

    @field_validator("amount_cny")
    @classmethod
    def require_finite_amount(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("index minute amount must be finite")
        return value


class IndexIntradayAmountSeriesV1(ContractModel):
    """Validated historical minute amounts for one canonical index."""

    metadata: ContractMetadata
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ)$")
    points: tuple[IndexMinuteAmountV1, ...]

    @model_validator(mode="after")
    def validate_series(self) -> "IndexIntradayAmountSeriesV1":
        if (
            self.metadata.contract != "index_intraday_amount.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError(
                "index intraday amount requires index_intraday_amount.v1 metadata"
            )
        if not self.points:
            raise ValueError("index intraday amount series cannot be empty")
        keys = [(item.trading_date, item.minute) for item in self.points]
        if keys != sorted(keys):
            raise ValueError("index intraday amount points must be ordered")
        if len(keys) != len(set(keys)):
            raise ValueError("index intraday amount points must be unique")
        return self


class IndexMinuteQuoteV1(ContractModel):
    """One exact provider-neutral minute bar for an exchange index."""

    trading_date: date
    minute: time
    open: float = Field(ge=0)
    close: float = Field(ge=0)
    high: float = Field(ge=0)
    low: float = Field(ge=0)
    amount_cny: float = Field(ge=0)

    @field_validator("open", "close", "high", "low", "amount_cny")
    @classmethod
    def require_finite_quote_number(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("index minute values must be finite")
        return value

    @model_validator(mode="after")
    def validate_bar(self) -> "IndexMinuteQuoteV1":
        if self.high < self.low:
            raise ValueError("index minute high cannot be lower than low")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("index minute OHLC values are inconsistent")
        return self


class IndexIntradaySeriesV1(ContractModel):
    """Validated exact minute bars for one canonical exchange index."""

    metadata: ContractMetadata
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ)$")
    points: tuple[IndexMinuteQuoteV1, ...]

    @model_validator(mode="after")
    def validate_series(self) -> "IndexIntradaySeriesV1":
        if (
            self.metadata.contract != "index_intraday_series.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError("index intraday bars require index_intraday_series.v1 metadata")
        if not self.points:
            raise ValueError("index intraday series cannot be empty")
        keys = [(item.trading_date, item.minute) for item in self.points]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("index intraday points must be ordered and unique")
        return self


class SectorFundFlowMinuteV1(ContractModel):
    """One exact minute observation of cumulative main-net sector flow."""

    provider_as_of: datetime
    cumulative_cny: float

    @field_validator("provider_as_of")
    @classmethod
    def require_provider_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("sector fund-flow provider_as_of must include a timezone")
        return value

    @field_validator("cumulative_cny")
    @classmethod
    def require_finite_amount(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("sector fund-flow amount must be finite")
        return value


class SectorFundFlowIntradayV1(ContractModel):
    """Provider-neutral intraday main-net-flow curve for one canonical sector."""

    metadata: ContractMetadata
    sector_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    taxonomy: Literal["industry", "concept"]
    trading_date: date
    points: tuple[SectorFundFlowMinuteV1, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_series(self) -> "SectorFundFlowIntradayV1":
        if (
            self.metadata.contract != "sector_intraday_fund_flow.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError(
                "sector fund flow requires sector_intraday_fund_flow.v1 metadata"
            )
        times = [item.provider_as_of for item in self.points]
        if times != sorted(times) or len(times) != len(set(times)):
            raise ValueError("sector fund-flow points must be unique and ordered")
        for point in self.points:
            local = point.provider_as_of.astimezone(ZoneInfo("Asia/Shanghai"))
            minute = local.hour * 60 + local.minute
            if local.date() != self.trading_date:
                raise ValueError("sector fund-flow point belongs to another trading date")
            if not (570 <= minute <= 690 or 780 <= minute <= 900):
                raise ValueError("sector fund-flow point is outside A-share sessions")
        if self.metadata.provider_as_of != times[-1]:
            raise ValueError("sector fund-flow metadata time must match the final point")
        return self


class MarketOverviewV1(ContractModel):
    metadata: ContractMetadata
    market_state: MarketStateV1
    indices: tuple[IndexQuoteV1, ...]
    participation_indices: tuple[ParticipationIndexV1, ...]
    market_turnover: MarketTurnoverV1

    @model_validator(mode="after")
    def require_usable_index(self) -> "MarketOverviewV1":
        if (
            self.metadata.contract != "market_overview.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError("market overview requires market_overview.v1 metadata")
        if not any(item.available for item in self.indices):
            raise ValueError("market overview requires at least one available index")
        return self


class QuoteSnapshotV1(ContractModel):
    """One A-share quote with explicit price, amount, and volume units.

    Percentage fields use percentage points: ``1.5`` means 1.5%. Volume is
    always shares and amount is always Chinese yuan, regardless of provider.
    """

    metadata: ContractMetadata
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str | None = None
    currency: Literal["CNY"] = "CNY"
    last: float = Field(gt=0)
    change: float | None = None
    change_pct: float | None = None
    previous_close: float | None = Field(default=None, gt=0)
    open: float | None = Field(default=None, ge=0)
    high: float | None = Field(default=None, ge=0)
    low: float | None = Field(default=None, ge=0)
    volume_shares: float | None = Field(default=None, ge=0)
    amount_cny: float | None = Field(default=None, ge=0)
    turnover_pct: float | None = Field(default=None, ge=0)
    pe_ttm: float | None = None
    pb: float | None = None
    total_market_cap_cny: float | None = Field(default=None, ge=0)
    float_market_cap_cny: float | None = Field(default=None, ge=0)

    @field_validator(
        "last",
        "change",
        "change_pct",
        "previous_close",
        "open",
        "high",
        "low",
        "volume_shares",
        "amount_cny",
        "turnover_pct",
        "pe_ttm",
        "pb",
        "total_market_cap_cny",
        "float_market_cap_cny",
    )
    @classmethod
    def require_finite_quote_number(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("quote numbers must be finite")
        return value

    @model_validator(mode="after")
    def validate_quote(self) -> "QuoteSnapshotV1":
        if (
            self.metadata.contract != "quote_snapshot.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError("quote snapshot requires quote_snapshot.v1 metadata")
        if self.high is not None and self.low is not None and self.high < self.low:
            raise ValueError("quote high cannot be lower than quote low")
        if (
            self.volume_shares == 0
            and self.amount_cny not in (None, 0)
        ):
            raise ValueError("quote zero volume is inconsistent with positive amount")
        if (
            self.volume_shares is not None
            and self.volume_shares > 0
            and self.amount_cny is not None
            and self.low is not None
            and self.low > 0
            and self.high is not None
        ):
            implied_average = self.amount_cny / self.volume_shares
            tolerance = 0.01
            if not (
                self.low - tolerance
                <= implied_average
                <= self.high + tolerance
            ):
                raise ValueError(
                    "quote amount and volume imply a price outside session range"
                )
        return self


class AShareUniverseQuoteV1(ContractModel):
    """One actively quoted A-share row in the whole-market close scan."""

    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    last: float = Field(gt=0)
    change_pct: float
    amount_cny: float = Field(ge=0)
    open: float | None = Field(default=None, ge=0)
    high: float | None = Field(default=None, ge=0)
    low: float | None = Field(default=None, ge=0)
    previous_close: float | None = Field(default=None, gt=0)
    turnover_pct: float | None = Field(default=None, ge=0)
    amplitude_pct: float | None = Field(default=None, ge=0)
    total_market_cap_cny: float | None = Field(default=None, ge=0)
    float_market_cap_cny: float | None = Field(default=None, ge=0)

    @field_validator(
        "last",
        "change_pct",
        "amount_cny",
        "open",
        "high",
        "low",
        "previous_close",
        "turnover_pct",
        "amplitude_pct",
        "total_market_cap_cny",
        "float_market_cap_cny",
    )
    @classmethod
    def require_finite_universe_number(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("A-share universe quote numbers must be finite")
        return value

    @model_validator(mode="after")
    def validate_session_range(self) -> "AShareUniverseQuoteV1":
        if self.high is not None and self.low is not None and self.high < self.low:
            raise ValueError("A-share universe high cannot be lower than low")
        return self


class AShareUniverseSnapshotV1(ContractModel):
    """Provider-neutral all-market quote surface scanned by the daily review."""

    metadata: ContractMetadata
    scope: Literal["provider_a_share_active_quotes"] = (
        "provider_a_share_active_quotes"
    )
    provider_row_count: int = Field(gt=0)
    active_quote_count: int = Field(gt=0)
    excluded_row_count: int = Field(ge=0)
    quotes: tuple[AShareUniverseQuoteV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_universe(self) -> "AShareUniverseSnapshotV1":
        if (
            self.metadata.contract != "a_share_universe_quote.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError(
                "A-share universe requires a_share_universe_quote.v1 metadata"
            )
        if self.active_quote_count != len(self.quotes):
            raise ValueError("active_quote_count must equal quote count")
        if self.provider_row_count != self.active_quote_count + self.excluded_row_count:
            raise ValueError("provider row count must equal active plus excluded rows")
        ids = [item.instrument_id for item in self.quotes]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError(
                "A-share universe quotes must have unique sorted instrument ids"
            )
        return self


class OpeningAuctionSnapshotV1(ContractModel):
    """Final 9:25 opening-auction match for one A-share instrument."""

    metadata: ContractMetadata
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    trading_date: date | None = None
    currency: Literal["CNY"] = "CNY"
    price: float = Field(gt=0)
    volume_shares: float = Field(ge=0)
    amount_cny: float = Field(ge=0)
    previous_close: float = Field(gt=0)
    change_pct: float | None = None
    turnover_pct: float | None = Field(default=None, ge=0)
    volume_ratio: float | None = Field(default=None, ge=0)
    float_shares: float | None = Field(default=None, ge=0)

    @field_validator(
        "price",
        "volume_shares",
        "amount_cny",
        "previous_close",
        "change_pct",
        "turnover_pct",
        "volume_ratio",
        "float_shares",
    )
    @classmethod
    def require_finite_auction_number(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("opening-auction numbers must be finite")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> "OpeningAuctionSnapshotV1":
        if (
            self.metadata.contract != "opening_auction_snapshot.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError(
                "opening auction requires opening_auction_snapshot.v1 metadata"
            )
        if self.volume_shares > 0 and not math.isclose(
            self.amount_cny,
            self.price * self.volume_shares,
            rel_tol=0.02,
            abs_tol=10.0,
        ):
            raise ValueError("opening-auction amount is inconsistent with price and volume")
        return self


class IntradayMinutePointV1(ContractModel):
    """One normalized A-share minute; volume is shares and amount is CNY."""

    minute: time
    price: float = Field(gt=0)
    cumulative_average_price: float | None = Field(default=None, gt=0)
    volume_shares: float = Field(ge=0)
    amount_cny: float | None = Field(default=None, ge=0)
    open: float | None = Field(default=None, gt=0)
    high: float | None = Field(default=None, gt=0)
    low: float | None = Field(default=None, gt=0)

    @field_validator(
        "price",
        "cumulative_average_price",
        "volume_shares",
        "amount_cny",
        "open",
        "high",
        "low",
    )
    @classmethod
    def require_finite_minute_number(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("intraday minute numbers must be finite")
        return value

    @model_validator(mode="after")
    def validate_minute(self) -> "IntradayMinutePointV1":
        if self.minute.second or self.minute.microsecond:
            raise ValueError("intraday minute timestamps must be minute-aligned")
        minute_of_day = self.minute.hour * 60 + self.minute.minute
        if not (570 <= minute_of_day <= 690 or 780 <= minute_of_day <= 900):
            raise ValueError("intraday point is outside A-share sessions")

        ohl = (self.open, self.high, self.low)
        if any(value is not None for value in ohl) and any(
            value is None for value in ohl
        ):
            raise ValueError("intraday OHLC fields must be complete when present")
        if self.high is not None and self.low is not None:
            if self.high + 1e-8 < max(self.open, self.price, self.low):
                raise ValueError("intraday high is inconsistent with OHLC values")
            if self.low - 1e-8 > min(self.open, self.price, self.high):
                raise ValueError("intraday low is inconsistent with OHLC values")

        if self.volume_shares == 0 and self.amount_cny not in (None, 0):
            raise ValueError("intraday zero volume conflicts with positive amount")
        if (
            self.volume_shares > 0
            and self.amount_cny is not None
            and self.high is not None
            and self.low is not None
        ):
            implied_average = self.amount_cny / self.volume_shares
            if not self.low - 0.01 <= implied_average <= self.high + 0.01:
                raise ValueError(
                    "intraday amount and volume imply a price outside the bar"
                )
        return self


class IntradayMinuteSeriesV1(ContractModel):
    """Current-session A-share minute series with an explicit timezone."""

    metadata: ContractMetadata
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    trading_date: date | None = None
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    frequency_minutes: Literal[1] = 1
    points: tuple[IntradayMinutePointV1, ...] = Field(
        min_length=1, max_length=1000
    )

    @model_validator(mode="after")
    def validate_series(self) -> "IntradayMinuteSeriesV1":
        if (
            self.metadata.contract != "intraday_minute_series.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError(
                "intraday minutes require intraday_minute_series.v1 metadata"
            )
        times = [item.minute for item in self.points]
        if times != sorted(times):
            raise ValueError("intraday minute points must be ordered")
        if len(times) != len(set(times)):
            raise ValueError("intraday minute points must be unique")
        if self.trading_date is not None and self.metadata.provider_as_of is not None:
            provider_date = self.metadata.provider_as_of.astimezone(
                ZoneInfo(self.timezone)
            ).date()
            if provider_date != self.trading_date:
                raise ValueError("intraday provider timestamp belongs to another date")
        return self


class OHLCVBarV1(ContractModel):
    """One normalized bar; volume is shares and amount is Chinese yuan."""

    trading_date: date
    open: float = Field(gt=0)
    close: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    volume_shares: float | None = Field(default=None, ge=0)
    amount_cny: float | None = Field(default=None, ge=0)
    amplitude_pct: float | None = Field(default=None, ge=0)
    change_pct: float | None = None
    change: float | None = None
    turnover_pct: float | None = Field(default=None, ge=0)

    @field_validator(
        "open",
        "close",
        "high",
        "low",
        "volume_shares",
        "amount_cny",
        "amplitude_pct",
        "change_pct",
        "change",
        "turnover_pct",
    )
    @classmethod
    def require_finite_bar_number(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("bar numbers must be finite")
        return value

    @model_validator(mode="after")
    def validate_ohlc(self) -> "OHLCVBarV1":
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("bar high is inconsistent with OHLC values")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("bar low is inconsistent with OHLC values")
        return self


class OHLCVSeriesV1(ContractModel):
    metadata: ContractMetadata
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    period: Literal["daily", "weekly", "monthly"]
    adjustment: Literal["none", "forward", "backward"]
    currency: Literal["CNY"] = "CNY"
    bars: tuple[OHLCVBarV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_series(self) -> "OHLCVSeriesV1":
        if (
            self.metadata.contract != "ohlcv_bar.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError("OHLCV series requires ohlcv_bar.v1 metadata")
        dates = [bar.trading_date for bar in self.bars]
        if dates != sorted(dates):
            raise ValueError("OHLCV bars must be sorted by trading_date")
        if len(dates) != len(set(dates)):
            raise ValueError("OHLCV bars cannot contain duplicate trading dates")
        return self


class MarketBreadthV1(ContractModel):
    """One whole-market participation snapshot with mutually exclusive counts."""

    metadata: ContractMetadata
    scope: Literal["provider_a_share_universe"] = "provider_a_share_universe"
    up_count: int = Field(ge=0)
    down_count: int = Field(ge=0)
    flat_count: int = Field(ge=0)
    unclassified_count: int = Field(ge=0)
    limit_up_count: int = Field(ge=0)
    limit_down_count: int = Field(ge=0)
    total_count: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_breadth(self) -> "MarketBreadthV1":
        if (
            self.metadata.contract != "market_breadth.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError("market breadth requires market_breadth.v1 metadata")
        if self.total_count != (
            self.up_count
            + self.down_count
            + self.flat_count
            + self.unclassified_count
        ):
            raise ValueError("market breadth total does not match participation counts")
        if self.limit_up_count > self.up_count:
            raise ValueError("limit-up count cannot exceed up count")
        if self.limit_down_count > self.down_count:
            raise ValueError("limit-down count cannot exceed down count")
        return self


class SectorQuoteV1(ContractModel):
    """One provider-neutral industry or concept quote."""

    sector_key: str = Field(min_length=3)
    sector_type: Literal["industry", "concept"]
    name: str = Field(min_length=1)
    provider_sector_code: str | None = None
    provider_variant: str | None = None
    value: float | None = Field(default=None, ge=0)
    change_pct: float
    amount_cny: float | None = Field(default=None, ge=0)
    main_net_inflow_cny: float | None = None
    main_net_inflow_pct: float | None = None
    main_net_inflow_rank: int | None = Field(default=None, ge=0)
    up_count: int | None = Field(default=None, ge=0)
    down_count: int | None = Field(default=None, ge=0)
    leader_instrument_id: str | None = Field(
        default=None,
        pattern=r"^\d{6}\.(?:SH|SZ|BJ)$",
    )
    leader_name: str | None = None
    leader_change_pct: float | None = None
    provider_as_of: datetime | None = None

    @field_validator(
        "value",
        "change_pct",
        "amount_cny",
        "main_net_inflow_cny",
        "main_net_inflow_pct",
        "leader_change_pct",
    )
    @classmethod
    def require_finite_sector_number(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("sector quote numbers must be finite")
        return value

    @field_validator("provider_as_of")
    @classmethod
    def require_sector_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("sector provider_as_of must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_sector_key(self) -> "SectorQuoteV1":
        if self.sector_key != f"{self.sector_type}:{self.name}":
            raise ValueError("sector_key must be derived from sector_type and name")
        return self


class SectorQuoteSeriesV1(ContractModel):
    metadata: ContractMetadata
    sector_type: Literal["industry", "concept"]
    quotes: tuple[SectorQuoteV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_sector_series(self) -> "SectorQuoteSeriesV1":
        if (
            self.metadata.contract != "sector_quote.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError("sector quotes require sector_quote.v1 metadata")
        if any(item.sector_type != self.sector_type for item in self.quotes):
            raise ValueError("sector quote type does not match its series")
        keys = [item.sector_key for item in self.quotes]
        if len(keys) != len(set(keys)):
            raise ValueError("sector quotes cannot contain duplicate sector keys")
        changes = [item.change_pct for item in self.quotes]
        if changes != sorted(changes, reverse=True):
            raise ValueError("sector quotes must be sorted by change_pct descending")
        return self


class EtfQuoteV1(ContractModel):
    """One provider-neutral mainland ETF quote used by dashboard context."""

    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ)$")
    name: str = Field(min_length=1)
    last: float = Field(gt=0)
    change_pct: float
    amount_cny: float = Field(ge=0)
    provider_as_of: datetime | None = None
    provider_variant: str = Field(min_length=1)

    @field_validator("last", "change_pct", "amount_cny")
    @classmethod
    def require_finite_etf_number(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("ETF quote numbers must be finite")
        return value

    @field_validator("provider_as_of")
    @classmethod
    def require_etf_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("ETF provider_as_of must include a timezone")
        return value


class EtfQuoteSeriesV1(ContractModel):
    metadata: ContractMetadata
    requested_limit: int = Field(ge=1, le=5000)
    sort_by: Literal["amount_cny"] = "amount_cny"
    quotes: tuple[EtfQuoteV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_etf_series(self) -> "EtfQuoteSeriesV1":
        if (
            self.metadata.contract != "etf_quote.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError("ETF quotes require etf_quote.v1 metadata")
        if len(self.quotes) > self.requested_limit:
            raise ValueError("ETF quotes exceed the requested limit")
        ids = [item.instrument_id for item in self.quotes]
        if len(ids) != len(set(ids)):
            raise ValueError("ETF quotes cannot contain duplicate instruments")
        amounts = [item.amount_cny for item in self.quotes]
        if amounts != sorted(amounts, reverse=True):
            raise ValueError("ETF quotes must be sorted by amount_cny descending")
        return self


class StockFundFlowV1(ContractModel):
    """One A-share's normalized post-close active-order fund flow."""

    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    net_amount_cny: float
    large_net_amount_cny: float
    extra_large_net_amount_cny: float

    @field_validator(
        "net_amount_cny",
        "large_net_amount_cny",
        "extra_large_net_amount_cny",
    )
    @classmethod
    def require_finite_stock_flow_number(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("stock fund-flow numbers must be finite")
        return value


class StockFundFlowSeriesV1(ContractModel):
    metadata: ContractMetadata
    trade_date: date
    flows: tuple[StockFundFlowV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_stock_flow_series(self) -> "StockFundFlowSeriesV1":
        if (
            self.metadata.contract != "stock_fund_flow_day.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError(
                "stock fund flows require stock_fund_flow_day.v1 metadata"
            )
        ids = [item.instrument_id for item in self.flows]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("stock fund flows must use unique sorted instruments")
        return self


class DragonTigerTradeV1(ContractModel):
    """One market-wide dragon-tiger list entry for an exact trading day."""

    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    close: float | None = Field(default=None, gt=0)
    change_pct: float
    turnover_pct: float | None = Field(default=None, ge=0)
    market_amount_cny: float | None = Field(default=None, ge=0)
    buy_amount_cny: float = Field(ge=0)
    sell_amount_cny: float = Field(ge=0)
    net_amount_cny: float
    reason: str | None = Field(default=None, min_length=1)

    @field_validator(
        "close",
        "change_pct",
        "turnover_pct",
        "market_amount_cny",
        "buy_amount_cny",
        "sell_amount_cny",
        "net_amount_cny",
    )
    @classmethod
    def require_finite_dragon_tiger_number(cls, value: float | None):
        if value is not None and not math.isfinite(value):
            raise ValueError("dragon-tiger numbers must be finite")
        return value


class DragonTigerSeriesV1(ContractModel):
    metadata: ContractMetadata
    trade_date: date
    valid_empty: bool = False
    trades: tuple[DragonTigerTradeV1, ...] = ()

    @model_validator(mode="after")
    def validate_dragon_tiger_series(self) -> "DragonTigerSeriesV1":
        if (
            self.metadata.contract != "dragon_tiger_market_day.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError(
                "dragon-tiger list requires dragon_tiger_market_day.v1 metadata"
            )
        if self.valid_empty != (not self.trades):
            raise ValueError("dragon-tiger valid_empty must match the trade list")
        ordering = [
            (-item.net_amount_cny, item.instrument_id, item.reason or "")
            for item in self.trades
        ]
        if ordering != sorted(ordering):
            raise ValueError("dragon-tiger trades must be net-buy sorted")
        return self


class LeaderQuoteV1(ContractModel):
    """A small provider-neutral quote used by configured dashboard leaders."""

    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    last: float = Field(ge=0)
    change_pct: float
    provider_as_of: datetime | None = None

    @field_validator("last", "change_pct")
    @classmethod
    def require_finite_leader_number(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("leader quote numbers must be finite")
        return value

    @field_validator("provider_as_of")
    @classmethod
    def require_leader_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("leader provider_as_of must include a timezone")
        return value


class LeaderQuoteSeriesV1(ContractModel):
    metadata: ContractMetadata
    requested_instrument_ids: tuple[str, ...] = Field(min_length=1)
    quotes: tuple[LeaderQuoteV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_leader_series(self) -> "LeaderQuoteSeriesV1":
        if (
            self.metadata.contract != "leader_quote.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError("leader quotes require leader_quote.v1 metadata")
        if len(self.requested_instrument_ids) != len(set(self.requested_instrument_ids)):
            raise ValueError("requested leader instruments cannot contain duplicates")
        quote_ids = [item.instrument_id for item in self.quotes]
        if len(quote_ids) != len(set(quote_ids)):
            raise ValueError("leader quotes cannot contain duplicate instruments")
        if not set(quote_ids).issubset(self.requested_instrument_ids):
            raise ValueError("leader quotes contain an unrequested instrument")
        return self


class StockSectorProfileV1(ContractModel):
    """Stable industry/profile fields used for daily limit-up attribution."""

    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    industry: str = Field(min_length=1)
    region: str | None = None
    concept_tags: tuple[str, ...] = ()
    provider_as_of: datetime
    provider_variant: str = Field(min_length=1)

    @field_validator("provider_as_of")
    @classmethod
    def require_profile_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("profile provider_as_of must include a timezone")
        return value

    @field_validator("concept_tags")
    @classmethod
    def require_non_empty_concept_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("profile concept tags cannot be empty")
        if len(value) != len(set(value)):
            raise ValueError("profile concept tags cannot contain duplicates")
        return value


class StockSectorProfileSeriesV1(ContractModel):
    metadata: ContractMetadata
    trade_date: date
    requested_instrument_ids: tuple[str, ...] = Field(min_length=1)
    profiles: tuple[StockSectorProfileV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_profile_series(self) -> "StockSectorProfileSeriesV1":
        if (
            self.metadata.contract != "stock_sector_profile.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError(
                "stock sector profiles require stock_sector_profile.v1 metadata"
            )
        requested = self.requested_instrument_ids
        if len(requested) != len(set(requested)):
            raise ValueError("requested profile instruments cannot contain duplicates")
        profile_ids = tuple(item.instrument_id for item in self.profiles)
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("stock sector profiles cannot contain duplicate instruments")
        if set(profile_ids) != set(requested):
            raise ValueError("stock sector profile coverage must match the request")
        return self


class BoardLeaderV1(ContractModel):
    """One gain-sorted constituent returned for a provider-neutral board."""

    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    price: float | None = Field(default=None, ge=0)
    change_pct: float | None = None
    amount_cny: float | None = Field(default=None, ge=0)
    turnover_pct: float | None = Field(default=None, ge=0)
    main_net_inflow_cny: float | None = None
    main_net_inflow_pct: float | None = None
    provider_as_of: datetime | None = None
    provider_variant: str = Field(min_length=1)

    @field_validator(
        "price",
        "change_pct",
        "amount_cny",
        "turnover_pct",
        "main_net_inflow_cny",
        "main_net_inflow_pct",
    )
    @classmethod
    def require_finite_board_number(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("board leader numbers must be finite")
        return value

    @field_validator("provider_as_of")
    @classmethod
    def require_board_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("board leader provider_as_of must include a timezone")
        return value


class BoardLeaderSnapshotV1(ContractModel):
    metadata: ContractMetadata
    board_code: str = Field(pattern=r"^BK\d+$")
    leaders: tuple[BoardLeaderV1, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_board_snapshot(self) -> "BoardLeaderSnapshotV1":
        if (
            self.metadata.contract != "board_leader.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError("board leaders require board_leader.v1 metadata")
        ids = [item.instrument_id for item in self.leaders]
        if len(ids) != len(set(ids)):
            raise ValueError("board leaders cannot contain duplicate instruments")
        return self


class BoardLeaderV2(ContractModel):
    """One speed-sorted board constituent used for resonance selection."""

    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    price: float | None = Field(default=None, ge=0)
    change_pct: float | None = None
    speed_pct: float
    amount_cny: float | None = Field(default=None, ge=0)
    turnover_pct: float | None = Field(default=None, ge=0)
    main_net_inflow_cny: float | None = None
    main_net_inflow_pct: float | None = None
    provider_as_of: datetime | None = None
    provider_variant: str = Field(min_length=1)

    @field_validator(
        "price",
        "change_pct",
        "speed_pct",
        "amount_cny",
        "turnover_pct",
        "main_net_inflow_cny",
        "main_net_inflow_pct",
    )
    @classmethod
    def require_finite_board_number(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("board resonance numbers must be finite")
        return value

    @field_validator("provider_as_of")
    @classmethod
    def require_board_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("board resonance provider_as_of must include a timezone")
        return value


class BoardLeaderSnapshotV2(ContractModel):
    metadata: ContractMetadata
    board_code: str = Field(pattern=r"^BK\d+$")
    speed_order: Literal["desc", "asc"] = "desc"
    leaders: tuple[BoardLeaderV2, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_board_snapshot(self) -> "BoardLeaderSnapshotV2":
        if (
            self.metadata.contract != "board_leader.v2"
            or self.metadata.schema_version != 2
        ):
            raise ValueError("board resonance candidates require board_leader.v2 metadata")
        ids = [item.instrument_id for item in self.leaders]
        if len(ids) != len(set(ids)):
            raise ValueError("board resonance candidates cannot contain duplicates")
        speeds = [item.speed_pct for item in self.leaders]
        expected = sorted(speeds, reverse=self.speed_order == "desc")
        if speeds != expected:
            raise ValueError("board resonance candidates do not match speed_order")
        return self


class LimitEventTradeStatusV1(ContractModel):
    """Provider-neutral market phase attached to a daily limit-event pool."""

    code: Literal["pre_open", "trading", "closed", "non_trading", "unknown"]
    label: str = Field(min_length=1)

    @property
    def eligible_for_scoring(self) -> bool:
        return self.code in {"trading", "closed"}


class LimitUpEventV1(ContractModel):
    """One daily A-share limit-up event with normalized units."""

    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    event_type: Literal["limit_up"] = "limit_up"
    price_cny: float | None = Field(default=None, ge=0)
    change_pct: float | None = None
    reason: str = Field(min_length=1)
    limit_up_type: str | None = None
    seal_success_pct: float | None = Field(default=None, ge=0, le=100)
    open_count: int | None = Field(default=None, ge=0)
    order_amount_cny: float | None = Field(default=None, ge=0)
    board_label: str | None = None
    board_count: int | None = Field(default=None, ge=1)
    first_sealed_at: time | None = None
    resealed: bool | None = None

    @field_validator(
        "price_cny",
        "change_pct",
        "seal_success_pct",
        "order_amount_cny",
    )
    @classmethod
    def require_finite_limit_event_number(
        cls, value: float | None
    ) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("limit-event numbers must be finite")
        return value


class LimitUpStatusV1(ContractModel):
    """One live limit-up status without requiring attribution details."""

    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    event_type: Literal["limit_up"] = "limit_up"
    price_cny: float | None = Field(default=None, ge=0)
    change_pct: float | None = None
    reason: str | None = Field(default=None, min_length=1)
    limit_up_type: str | None = None
    board_label: str | None = None
    board_count: int | None = Field(default=None, ge=1)
    first_sealed_at: time | None = None
    resealed: bool | None = None

    @field_validator("price_cny", "change_pct")
    @classmethod
    def require_finite_status_number(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("limit-up status numbers must be finite")
        return value


class LimitUpStatusSeriesV1(ContractModel):
    metadata: ContractMetadata
    trading_date: date
    trade_status: LimitEventTradeStatusV1
    events: tuple[LimitUpStatusV1, ...] = ()
    pool_total: int = Field(ge=0)
    board_count_coverage: float = Field(ge=0, le=1)
    unknown_board_count: int = Field(ge=0)
    valid_empty: bool
    provider_page_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_limit_status_series(self) -> "LimitUpStatusSeriesV1":
        if (
            self.metadata.contract != "limit_up_status.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError("limit-up status requires limit_up_status.v1 metadata")
        if self.pool_total != len(self.events):
            raise ValueError("limit-up status total must equal its event count")
        ids = [item.instrument_id for item in self.events]
        if len(ids) != len(set(ids)):
            raise ValueError("limit-up status cannot contain duplicate instruments")
        if self.valid_empty != (self.pool_total == 0):
            raise ValueError("limit-up status valid_empty does not match pool_total")
        unknown = sum(item.board_count is None for item in self.events)
        if self.unknown_board_count != unknown:
            raise ValueError("limit-up status unknown board count is inconsistent")
        expected_coverage = (
            (self.pool_total - unknown) / self.pool_total
            if self.pool_total
            else 1.0
        )
        if not math.isclose(
            self.board_count_coverage,
            expected_coverage,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise ValueError("limit-up status board-count coverage is inconsistent")
        return self


class LimitEventSeriesV1(ContractModel):
    metadata: ContractMetadata
    trading_date: date
    trade_status: LimitEventTradeStatusV1
    events: tuple[LimitUpEventV1, ...] = ()
    pool_total: int = Field(ge=0)
    reason_coverage: float = Field(ge=0, le=1)
    board_count_coverage: float = Field(ge=0, le=1)
    unknown_board_count: int = Field(ge=0)
    valid_empty: bool
    provider_page_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_limit_event_series(self) -> "LimitEventSeriesV1":
        if (
            self.metadata.contract != "limit_event.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError("limit events require limit_event.v1 metadata")
        if self.pool_total != len(self.events):
            raise ValueError("limit-event pool_total must equal its event count")
        ids = [item.instrument_id for item in self.events]
        if len(ids) != len(set(ids)):
            raise ValueError("limit events cannot contain duplicate instruments")
        if self.valid_empty != (self.pool_total == 0):
            raise ValueError("limit-event valid_empty does not match pool_total")
        if self.reason_coverage != 1.0:
            raise ValueError("limit-event reasons must have complete coverage")
        unknown = sum(item.board_count is None for item in self.events)
        if self.unknown_board_count != unknown:
            raise ValueError("limit-event unknown board count is inconsistent")
        expected_coverage = (
            (self.pool_total - unknown) / self.pool_total
            if self.pool_total
            else 1.0
        )
        if not math.isclose(
            self.board_count_coverage,
            expected_coverage,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise ValueError("limit-event board-count coverage is inconsistent")
        return self
