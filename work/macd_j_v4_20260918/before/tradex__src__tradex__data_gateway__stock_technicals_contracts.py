"""Published daily technical indicators; never initialized from a short window."""

from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from .contracts import ContractMetadata, ContractModel


class StockTechnicalPointV1(ContractModel):
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    close: float = Field(gt=0, allow_inf_nan=False)
    amount_cny: float = Field(ge=0, allow_inf_nan=False)
    dif: float = Field(allow_inf_nan=False)
    dea: float = Field(allow_inf_nan=False)
    k: float = Field(ge=0, le=100, allow_inf_nan=False)
    d: float = Field(ge=0, le=100, allow_inf_nan=False)
    j: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_j(self):
        if abs(self.j - (3 * self.k - 2 * self.d)) > 0.02:
            raise ValueError("J does not match 3K - 2D")
        return self


class StockTechnicalDayV1(ContractModel):
    metadata: ContractMetadata
    trade_date: date
    adjustment: Literal["forward"] = "forward"
    macd_parameters: tuple[Literal[12], Literal[26], Literal[9]] = (12, 26, 9)
    kdj_parameters: tuple[Literal[9], Literal[3], Literal[3]] = (9, 3, 3)
    calculation_basis: Literal["published_history_indicators"] = "published_history_indicators"
    points: tuple[StockTechnicalPointV1, ...]
    rejected_instrument_ids: tuple[str, ...] = ()
    # None means not checked, not an empty ST list.
    special_treatment_ids: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def validate_day(self):
        if self.metadata.contract != "stock_technical_day.v1":
            raise ValueError("wrong technical metadata contract")
        ids = [point.instrument_id for point in self.points]
        if ids != sorted(set(ids)) or set(ids) & set(self.rejected_instrument_ids):
            raise ValueError("technical instruments must be unique and sorted")
        return self


class StockTechnicalWindowV1(ContractModel):
    days: tuple[StockTechnicalDayV1, ...] = Field(min_length=6, max_length=6)

    @model_validator(mode="after")
    def validate_window(self):
        dates = [day.trade_date for day in self.days]
        if dates != sorted(set(dates)):
            raise ValueError("technical dates must be unique and sorted")
        if self.days[-1].special_treatment_ids is None:
            raise ValueError("signal-day ST membership must be verified")
        return self
