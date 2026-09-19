"""Provider-neutral contracts for reproducible daily stock selection inputs."""

from __future__ import annotations

import math
from datetime import date

from pydantic import Field, field_validator, model_validator

from .contracts import ContractMetadata, ContractModel
from .stock_technicals_contracts import StockTechnicalWindowV1


class StockFactorCoverageV1(ContractModel):
    universe_count: int = Field(ge=1)
    master_count: int = Field(ge=0)
    daily_basic_count: int = Field(ge=0)
    momentum_20d_count: int = Field(ge=0)
    momentum_60d_count: int = Field(ge=0)
    financial_count: int = Field(ge=0)
    candlestick_history_count: int = Field(default=0, ge=0)

    @field_validator(
        "master_count",
        "daily_basic_count",
        "momentum_20d_count",
        "momentum_60d_count",
        "financial_count",
        "candlestick_history_count",
    )
    @classmethod
    def cannot_exceed_universe(cls, value: int, info) -> int:
        universe = info.data.get("universe_count")
        if universe is not None and value > universe:
            raise ValueError("factor coverage count cannot exceed the universe")
        return value

    def ratio(self, field_name: str) -> float:
        return float(getattr(self, field_name)) / self.universe_count


class DailyStockCandlestickBarV1(ContractModel):
    """One verified active-session OHLC bar used by pattern screens."""

    trade_date: date
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    previous_close: float = Field(gt=0)
    amount_cny: float = Field(gt=0)
    # Optional for immutable archives written before volume screening existed.
    volume_shares: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @field_validator("open", "high", "low", "close", "previous_close", "amount_cny")
    @classmethod
    def require_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("candlestick values must be finite")
        return value

    @model_validator(mode="after")
    def validate_ohlc(self) -> "DailyStockCandlestickBarV1":
        if self.high < max(self.open, self.close) or self.low > min(
            self.open, self.close
        ):
            raise ValueError("candlestick high/low must contain open and close")
        if self.high <= self.low:
            raise ValueError("candlestick range must be positive")
        return self


class DailyStockCandlestickHistoryV1(ContractModel):
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    bars: tuple[DailyStockCandlestickBarV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_history(self) -> "DailyStockCandlestickHistoryV1":
        dates = [item.trade_date for item in self.bars]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise ValueError("candlestick history dates must be unique and sorted")
        return self


class DailyStockFactorV1(ContractModel):
    """One point-in-time A-share row after provider fields are normalized."""

    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    # ``industry`` is the statistical peer basis.  Provider and business
    # identities stay separate so consumers cannot treat one label as all
    # three meanings.
    industry: str | None = None
    provider_industry: str | None = None
    primary_business_name: str | None = None
    business_tags: tuple[str, ...] = ()
    relationship_verification_status: str | None = None
    relationship_catalog_revision: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    market: str | None = None
    list_date: date | None = None
    delist_date: date | None = None
    trade_date: date
    open: float = Field(ge=0)
    close: float = Field(gt=0)
    amount_cny: float = Field(ge=0)
    total_market_cap_cny: float | None = Field(default=None, gt=0)
    float_market_cap_cny: float | None = Field(default=None, gt=0)
    turnover_rate_pct: float | None = Field(default=None, ge=0)
    volume_ratio: float | None = Field(default=None, ge=0)
    pe_ttm: float | None = None
    pb: float | None = None
    dividend_yield_pct: float | None = Field(default=None, ge=0)
    momentum_20d_pct: float | None = None
    momentum_60d_pct: float | None = None
    roe_pct: float | None = None
    gross_margin_pct: float | None = None
    debt_to_assets_pct: float | None = None
    revenue_yoy_pct: float | None = None
    net_profit_yoy_pct: float | None = None
    financial_report_date: date | None = None
    financial_announcement_date: date | None = None

    @field_validator(
        "open",
        "close",
        "amount_cny",
        "total_market_cap_cny",
        "float_market_cap_cny",
        "turnover_rate_pct",
        "volume_ratio",
        "pe_ttm",
        "pb",
        "dividend_yield_pct",
        "momentum_20d_pct",
        "momentum_60d_pct",
        "roe_pct",
        "gross_margin_pct",
        "debt_to_assets_pct",
        "revenue_yoy_pct",
        "net_profit_yoy_pct",
    )
    @classmethod
    def require_finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("stock factor numbers must be finite")
        return value

    @model_validator(mode="after")
    def validate_point_in_time(self) -> "DailyStockFactorV1":
        if self.list_date is not None and self.list_date > self.trade_date:
            raise ValueError("stock cannot be listed after the factor trade date")
        if (
            self.financial_announcement_date is not None
            and self.financial_announcement_date > self.trade_date
        ):
            raise ValueError("future financial announcements are not allowed")
        if (
            self.financial_report_date is not None
            and self.financial_report_date > self.trade_date
        ):
            raise ValueError("future financial periods are not allowed")
        return self


class DailyStockFactorSnapshotV1(ContractModel):
    metadata: ContractMetadata
    trade_date: date
    prior_20d_trade_date: date
    prior_60d_trade_date: date
    benchmark_instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ)$")
    benchmark_open: float = Field(gt=0)
    benchmark_close: float = Field(gt=0)
    coverage: StockFactorCoverageV1
    factors: tuple[DailyStockFactorV1, ...]
    candlestick_window_trade_dates: tuple[date, ...] = ()
    candlestick_histories: tuple[DailyStockCandlestickHistoryV1, ...] = ()
    technicals: StockTechnicalWindowV1 | None = None

    @model_validator(mode="after")
    def validate_snapshot(self) -> "DailyStockFactorSnapshotV1":
        if (
            self.metadata.contract != "daily_stock_factor_snapshot.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError(
                "daily stock factors require daily_stock_factor_snapshot.v1 metadata"
            )
        if not self.prior_60d_trade_date < self.prior_20d_trade_date < self.trade_date:
            raise ValueError("stock factor lookback dates must be strictly ordered")
        ids = [item.instrument_id for item in self.factors]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("stock factors must be unique and sorted by instrument")
        if any(item.trade_date != self.trade_date for item in self.factors):
            raise ValueError("stock factor rows must share the snapshot trade date")
        if len(self.factors) != self.coverage.universe_count:
            raise ValueError("stock factor coverage must match the factor row count")
        window_dates = list(self.candlestick_window_trade_dates)
        if self.technicals is not None and tuple(
            day.trade_date for day in self.technicals.days
        ) != self.candlestick_window_trade_dates[-6:]:
            raise ValueError("technical dates must match the last six trading sessions")
        histories = list(self.candlestick_histories)
        if window_dates:
            if (
                len(window_dates) != 15
                or window_dates != sorted(window_dates)
                or len(window_dates) != len(set(window_dates))
                or window_dates[-1] != self.trade_date
            ):
                raise ValueError(
                    "candlestick window must contain 15 sorted sessions ending on trade_date"
                )
        elif histories:
            raise ValueError("candlestick histories require a declared trade-date window")
        history_ids = [item.instrument_id for item in histories]
        if history_ids != sorted(history_ids) or len(history_ids) != len(
            set(history_ids)
        ):
            raise ValueError("candlestick histories must be unique and sorted")
        allowed_dates = set(window_dates)
        if any(
            bar.trade_date not in allowed_dates
            for history in histories
            for bar in history.bars
        ):
            raise ValueError("candlestick history contains a date outside the window")
        complete_count = sum(
            len(history.bars) == len(window_dates) and bool(window_dates)
            for history in histories
        )
        if complete_count != self.coverage.candlestick_history_count:
            raise ValueError("candlestick history coverage does not match complete rows")
        return self


__all__ = [
    "DailyStockCandlestickBarV1",
    "DailyStockCandlestickHistoryV1",
    "DailyStockFactorSnapshotV1",
    "DailyStockFactorV1",
    "StockFactorCoverageV1",
]
