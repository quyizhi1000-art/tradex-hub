"""Provider-neutral whole-market evidence for an immutable daily review.

Only compact, auditable summaries are retained here. Provider payloads are
normalized and quality-gated by ``tradex.data_gateway`` before this module
sees them. Every surface is collected independently so one late or failed
source cannot erase the rest of the closing evidence.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time
from enum import Enum
from statistics import mean, median
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from tradex.data_gateway.contracts import (
    AShareUniverseSnapshotV1,
    DragonTigerSeriesV1,
    EtfQuoteSeriesV1,
    LimitEventSeriesV1,
    MarketBreadthV1,
    QualityStatus,
    SectorQuoteSeriesV1,
    StockFundFlowSeriesV1,
)

from .contracts import ContractModel, MarketWatchSnapshotV1
from .session_schedule import (
    EXPECTED_MARKET_WATCH_MINUTES,
    expected_market_watch_minutes,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
EVIDENCE_COMPONENT_ORDER = (
    "market_watch",
    "market_universe",
    "market_breadth_detail",
    "etfs",
    "industry_sectors",
    "concept_sectors",
    "limit_events",
    "stock_fund_flow",
    "dragon_tiger",
    "intraday_history",
)


class EvidenceStatus(str, Enum):
    ACCEPTED = "accepted"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class EvidenceComponentV1(ContractModel):
    component: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    status: EvidenceStatus
    record_count: int = Field(default=0, ge=0)
    contract: str | None = None
    provider: str | None = Field(default=None, min_length=1)
    provider_as_of: datetime | None = None
    flags: tuple[str, ...] = ()
    error_code: str | None = None

    @field_validator("provider_as_of")
    @classmethod
    def aware_time(cls, value: datetime | None):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("provider_as_of must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> "EvidenceComponentV1":
        if self.status == EvidenceStatus.UNAVAILABLE and not self.error_code:
            raise ValueError("unavailable evidence requires error_code")
        if self.status != EvidenceStatus.UNAVAILABLE and self.error_code:
            raise ValueError("available evidence cannot carry error_code")
        return self


class MarketMoverV1(ContractModel):
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    change_pct: float
    amount_cny: float = Field(ge=0)
    turnover_pct: float | None = Field(default=None, ge=0)

    @field_validator("change_pct", "amount_cny", "turnover_pct")
    @classmethod
    def finite_numbers(cls, value: float | None, info):
        if value is not None and not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        return value


class DistributionBucketV1(ContractModel):
    bucket: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1)
    count: int = Field(ge=0)


class UniverseReviewSummaryV1(ContractModel):
    scanned_count: int = Field(gt=0)
    excluded_count: int = Field(ge=0)
    up_count: int = Field(ge=0)
    down_count: int = Field(ge=0)
    flat_count: int = Field(ge=0)
    mean_change_pct: float
    median_change_pct: float
    total_amount_cny: float = Field(ge=0)
    distribution: tuple[DistributionBucketV1, ...] = Field(min_length=8, max_length=8)
    top_gainers: tuple[MarketMoverV1, ...] = Field(default=(), max_length=10)
    top_losers: tuple[MarketMoverV1, ...] = Field(default=(), max_length=10)
    most_traded: tuple[MarketMoverV1, ...] = Field(default=(), max_length=10)
    highest_turnover: tuple[MarketMoverV1, ...] = Field(default=(), max_length=10)

    @property
    def advance_ratio(self) -> float:
        directional = self.up_count + self.down_count
        return self.up_count / directional if directional else 0.5

    @model_validator(mode="after")
    def validate_counts(self) -> "UniverseReviewSummaryV1":
        if self.up_count + self.down_count + self.flat_count != self.scanned_count:
            raise ValueError("universe direction counts must match scanned_count")
        if sum(item.count for item in self.distribution) != self.scanned_count:
            raise ValueError("universe distribution must match scanned_count")
        return self


class EtfReviewSummaryV1(ContractModel):
    scanned_count: int = Field(gt=0)
    up_count: int = Field(ge=0)
    down_count: int = Field(ge=0)
    flat_count: int = Field(ge=0)
    median_change_pct: float
    total_amount_cny: float = Field(ge=0)
    top_gainers: tuple[MarketMoverV1, ...] = Field(default=(), max_length=10)
    top_losers: tuple[MarketMoverV1, ...] = Field(default=(), max_length=10)
    most_traded: tuple[MarketMoverV1, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def validate_counts(self) -> "EtfReviewSummaryV1":
        if self.up_count + self.down_count + self.flat_count != self.scanned_count:
            raise ValueError("ETF direction counts must match scanned_count")
        return self


class SectorReviewItemV1(ContractModel):
    sector_key: str = Field(min_length=3)
    sector_type: Literal["industry", "concept"]
    name: str = Field(min_length=1)
    change_pct: float
    amount_cny: float | None = Field(default=None, ge=0)
    main_net_inflow_cny: float | None = None
    breadth_ratio: float | None = Field(default=None, ge=0, le=1)
    leader_instrument_id: str | None = Field(
        default=None, pattern=r"^\d{6}\.(?:SH|SZ|BJ)$"
    )
    leader_name: str | None = None
    leader_change_pct: float | None = None


class SectorReviewSummaryV1(ContractModel):
    sector_type: Literal["industry", "concept"]
    scanned_count: int = Field(gt=0)
    up_count: int = Field(ge=0)
    down_count: int = Field(ge=0)
    flat_count: int = Field(ge=0)
    median_change_pct: float
    top_gainers: tuple[SectorReviewItemV1, ...] = Field(default=(), max_length=10)
    top_losers: tuple[SectorReviewItemV1, ...] = Field(default=(), max_length=10)
    top_inflows: tuple[SectorReviewItemV1, ...] = Field(default=(), max_length=10)
    top_outflows: tuple[SectorReviewItemV1, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def validate_counts(self) -> "SectorReviewSummaryV1":
        if self.up_count + self.down_count + self.flat_count != self.scanned_count:
            raise ValueError("sector direction counts must match scanned_count")
        return self


class LimitReasonV1(ContractModel):
    reason: str = Field(min_length=1)
    count: int = Field(gt=0)


class BoardHeightV1(ContractModel):
    board_count: int = Field(ge=1)
    count: int = Field(gt=0)


class LimitReviewEventV1(ContractModel):
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    board_count: int | None = Field(default=None, ge=1)
    first_sealed_at: str | None = None
    open_count: int | None = Field(default=None, ge=0)
    order_amount_cny: float | None = Field(default=None, ge=0)


class LimitReviewSummaryV1(ContractModel):
    limit_up_count: int = Field(ge=0)
    limit_down_count: int | None = Field(default=None, ge=0)
    max_board_count: int | None = Field(default=None, ge=1)
    board_heights: tuple[BoardHeightV1, ...] = ()
    top_reasons: tuple[LimitReasonV1, ...] = Field(default=(), max_length=10)
    representative_events: tuple[LimitReviewEventV1, ...] = Field(default=(), max_length=12)
    reason_coverage: float = Field(ge=0, le=1)
    board_count_coverage: float = Field(ge=0, le=1)


class StockFundFlowMoverV1(ContractModel):
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str | None = None
    net_amount_cny: float
    large_net_amount_cny: float
    extra_large_net_amount_cny: float


class StockFundFlowReviewSummaryV1(ContractModel):
    scanned_count: int = Field(gt=0)
    positive_count: int = Field(ge=0)
    negative_count: int = Field(ge=0)
    flat_count: int = Field(ge=0)
    median_net_amount_cny: float
    total_net_amount_cny: float
    total_large_net_amount_cny: float
    top_inflows: tuple[StockFundFlowMoverV1, ...] = Field(default=(), max_length=15)
    top_outflows: tuple[StockFundFlowMoverV1, ...] = Field(default=(), max_length=15)

    @model_validator(mode="after")
    def validate_counts(self) -> "StockFundFlowReviewSummaryV1":
        if self.positive_count + self.negative_count + self.flat_count != self.scanned_count:
            raise ValueError("stock fund-flow counts must match scanned_count")
        return self


class DragonTigerTradeSummaryV1(ContractModel):
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    change_pct: float
    turnover_pct: float | None = Field(default=None, ge=0)
    market_amount_cny: float | None = Field(default=None, ge=0)
    buy_amount_cny: float = Field(ge=0)
    sell_amount_cny: float = Field(ge=0)
    net_amount_cny: float
    reason: str | None = None


class DragonTigerReviewSummaryV1(ContractModel):
    listed_count: int = Field(ge=0)
    positive_net_count: int = Field(ge=0)
    negative_net_count: int = Field(ge=0)
    total_buy_amount_cny: float = Field(ge=0)
    total_sell_amount_cny: float = Field(ge=0)
    total_net_amount_cny: float
    top_net_buys: tuple[DragonTigerTradeSummaryV1, ...] = Field(default=(), max_length=12)
    top_net_sells: tuple[DragonTigerTradeSummaryV1, ...] = Field(default=(), max_length=12)


class IntradayReviewSummaryV1(ContractModel):
    sample_count: int = Field(ge=0)
    expected_minutes: int = Field(default=EXPECTED_MARKET_WATCH_MINUTES, ge=1)
    coverage_ratio: float = Field(ge=0, le=1)
    first_as_of: datetime | None = None
    last_as_of: datetime | None = None
    first_regime: str | None = None
    last_regime: str | None = None
    regime_transitions: int = Field(default=0, ge=0)
    first_advance_ratio: float | None = Field(default=None, ge=0, le=1)
    last_advance_ratio: float | None = Field(default=None, ge=0, le=1)
    min_advance_ratio: float | None = Field(default=None, ge=0, le=1)
    max_advance_ratio: float | None = Field(default=None, ge=0, le=1)
    morning_close_advance_ratio: float | None = Field(default=None, ge=0, le=1)
    afternoon_open_advance_ratio: float | None = Field(default=None, ge=0, le=1)
    first_index_mean_change_pct: float | None = None
    last_index_mean_change_pct: float | None = None
    strongest_as_of: datetime | None = None
    weakest_as_of: datetime | None = None
    trajectory_statement: str = Field(min_length=1)

    @field_validator("first_as_of", "last_as_of", "strongest_as_of", "weakest_as_of")
    @classmethod
    def aware_intraday_times(cls, value: datetime | None):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("intraday timestamps must include a timezone")
        return value


class DailyMarketReviewEvidenceV1(ContractModel):
    contract: Literal["daily_market_review_evidence.v1"] = "daily_market_review_evidence.v1"
    schema_version: Literal[1] = 1
    trade_date: date
    collected_at: datetime
    market_watch: MarketWatchSnapshotV1
    universe: UniverseReviewSummaryV1 | None = None
    etfs: EtfReviewSummaryV1 | None = None
    industry_sectors: SectorReviewSummaryV1 | None = None
    concept_sectors: SectorReviewSummaryV1 | None = None
    limit_events: LimitReviewSummaryV1 | None = None
    stock_fund_flow: StockFundFlowReviewSummaryV1 | None = None
    dragon_tiger: DragonTigerReviewSummaryV1 | None = None
    intraday: IntradayReviewSummaryV1
    components: tuple[EvidenceComponentV1, ...] = Field(min_length=10, max_length=10)
    quality_notes: tuple[str, ...] = ()

    @field_validator("collected_at")
    @classmethod
    def aware_collected_at(cls, value: datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("collected_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_evidence(self) -> "DailyMarketReviewEvidenceV1":
        if self.collected_at.astimezone(SHANGHAI).date() < self.trade_date:
            raise ValueError("evidence collection cannot predate trade_date")
        if self.market_watch.market_state.trading_date != self.trade_date:
            raise ValueError("market_watch date must match evidence trade_date")
        if tuple(item.component for item in self.components) != EVIDENCE_COMPONENT_ORDER:
            raise ValueError("evidence components must use the canonical order")
        expected_presence = {
            "market_universe": self.universe is not None,
            "etfs": self.etfs is not None,
            "industry_sectors": self.industry_sectors is not None,
            "concept_sectors": self.concept_sectors is not None,
            "limit_events": self.limit_events is not None,
            "stock_fund_flow": self.stock_fund_flow is not None,
            "dragon_tiger": self.dragon_tiger is not None,
        }
        by_name = {item.component: item for item in self.components}
        for name, present in expected_presence.items():
            if present == (by_name[name].status == EvidenceStatus.UNAVAILABLE):
                raise ValueError(f"component presence does not match {name} status")
        return self


def _mover(item: Any) -> MarketMoverV1:
    return MarketMoverV1(
        instrument_id=item.instrument_id,
        name=item.name,
        change_pct=item.change_pct,
        amount_cny=item.amount_cny,
        turnover_pct=getattr(item, "turnover_pct", None),
    )


def summarize_universe(snapshot: AShareUniverseSnapshotV1) -> UniverseReviewSummaryV1:
    canonical = AShareUniverseSnapshotV1.model_validate(snapshot)
    quotes = list(canonical.quotes)
    changes = [item.change_pct for item in quotes]
    buckets = (
        ("limit_up_zone", "涨幅9.5%以上", lambda value: value >= 9.5),
        ("strong_up", "涨幅5%至9.5%", lambda value: 5 <= value < 9.5),
        ("up_2_5", "涨幅2%至5%", lambda value: 2 <= value < 5),
        ("up_0_2", "上涨不足2%", lambda value: 0 < value < 2),
        ("flat", "平盘", lambda value: value == 0),
        ("down_0_2", "下跌不足2%", lambda value: -2 < value < 0),
        ("down_2_5", "跌幅2%至5%", lambda value: -5 < value <= -2),
        ("down_5_plus", "跌幅5%以上", lambda value: value <= -5),
    )
    gainers = sorted(quotes, key=lambda item: (-item.change_pct, -item.amount_cny, item.instrument_id))
    losers = sorted(quotes, key=lambda item: (item.change_pct, -item.amount_cny, item.instrument_id))
    traded = sorted(quotes, key=lambda item: (-item.amount_cny, item.instrument_id))
    turnover = sorted(
        (item for item in quotes if item.turnover_pct is not None and item.amount_cny >= 10_000_000),
        key=lambda item: (-item.turnover_pct, -item.amount_cny, item.instrument_id),
    )
    return UniverseReviewSummaryV1(
        scanned_count=len(quotes),
        excluded_count=canonical.excluded_row_count,
        up_count=sum(value > 0 for value in changes),
        down_count=sum(value < 0 for value in changes),
        flat_count=sum(value == 0 for value in changes),
        mean_change_pct=mean(changes),
        median_change_pct=median(changes),
        total_amount_cny=sum(item.amount_cny for item in quotes),
        distribution=tuple(
            DistributionBucketV1(bucket=key, label=label, count=sum(predicate(value) for value in changes))
            for key, label, predicate in buckets
        ),
        top_gainers=tuple(_mover(item) for item in gainers[:10]),
        top_losers=tuple(_mover(item) for item in losers[:10]),
        most_traded=tuple(_mover(item) for item in traded[:10]),
        highest_turnover=tuple(_mover(item) for item in turnover[:10]),
    )


def summarize_etfs(series: EtfQuoteSeriesV1) -> EtfReviewSummaryV1:
    canonical = EtfQuoteSeriesV1.model_validate(series)
    quotes = list(canonical.quotes)
    changes = [item.change_pct for item in quotes]
    gainers = sorted(quotes, key=lambda item: (-item.change_pct, -item.amount_cny, item.instrument_id))
    losers = sorted(quotes, key=lambda item: (item.change_pct, -item.amount_cny, item.instrument_id))
    traded = sorted(quotes, key=lambda item: (-item.amount_cny, item.instrument_id))
    return EtfReviewSummaryV1(
        scanned_count=len(quotes),
        up_count=sum(value > 0 for value in changes),
        down_count=sum(value < 0 for value in changes),
        flat_count=sum(value == 0 for value in changes),
        median_change_pct=median(changes),
        total_amount_cny=sum(item.amount_cny for item in quotes),
        top_gainers=tuple(_mover(item) for item in gainers[:10]),
        top_losers=tuple(_mover(item) for item in losers[:10]),
        most_traded=tuple(_mover(item) for item in traded[:10]),
    )


def _sector_item(item: Any) -> SectorReviewItemV1:
    directional = (item.up_count or 0) + (item.down_count or 0)
    return SectorReviewItemV1(
        sector_key=item.sector_key,
        sector_type=item.sector_type,
        name=item.name,
        change_pct=item.change_pct,
        amount_cny=item.amount_cny,
        main_net_inflow_cny=item.main_net_inflow_cny,
        breadth_ratio=(item.up_count or 0) / directional if directional else None,
        leader_instrument_id=item.leader_instrument_id,
        leader_name=item.leader_name,
        leader_change_pct=item.leader_change_pct,
    )


def summarize_sectors(series: SectorQuoteSeriesV1) -> SectorReviewSummaryV1:
    canonical = SectorQuoteSeriesV1.model_validate(series)
    items = [_sector_item(item) for item in canonical.quotes]
    changes = [item.change_pct for item in items]
    inflows = sorted(
        (item for item in items if item.main_net_inflow_cny is not None),
        key=lambda item: (-item.main_net_inflow_cny, item.sector_key),
    )
    return SectorReviewSummaryV1(
        sector_type=canonical.sector_type,
        scanned_count=len(items),
        up_count=sum(value > 0 for value in changes),
        down_count=sum(value < 0 for value in changes),
        flat_count=sum(value == 0 for value in changes),
        median_change_pct=median(changes),
        top_gainers=tuple(items[:10]),
        top_losers=tuple(sorted(items, key=lambda item: (item.change_pct, item.sector_key))[:10]),
        top_inflows=tuple(inflows[:10]),
        top_outflows=tuple(sorted(inflows, key=lambda item: (item.main_net_inflow_cny, item.sector_key))[:10]),
    )


def summarize_limit_events(series: LimitEventSeriesV1, breadth: MarketBreadthV1 | None) -> LimitReviewSummaryV1:
    canonical = LimitEventSeriesV1.model_validate(series)
    board_counts = Counter(item.board_count for item in canonical.events if item.board_count is not None)
    reasons = Counter(item.reason.strip() for item in canonical.events if item.reason.strip())
    representative = sorted(
        canonical.events,
        key=lambda item: (-(item.board_count or 0), -(item.order_amount_cny or 0), item.instrument_id),
    )[:12]
    return LimitReviewSummaryV1(
        limit_up_count=canonical.pool_total,
        limit_down_count=breadth.limit_down_count if breadth is not None else None,
        max_board_count=max(board_counts, default=None),
        board_heights=tuple(BoardHeightV1(board_count=height, count=count) for height, count in sorted(board_counts.items())),
        top_reasons=tuple(LimitReasonV1(reason=reason, count=count) for reason, count in reasons.most_common(10)),
        representative_events=tuple(
            LimitReviewEventV1(
                instrument_id=item.instrument_id,
                name=item.name,
                reason=item.reason,
                board_count=item.board_count,
                first_sealed_at=item.first_sealed_at.isoformat() if item.first_sealed_at else None,
                open_count=item.open_count,
                order_amount_cny=item.order_amount_cny,
            )
            for item in representative
        ),
        reason_coverage=canonical.reason_coverage,
        board_count_coverage=canonical.board_count_coverage,
    )


def summarize_stock_fund_flows(
    series: StockFundFlowSeriesV1,
    *,
    names: Mapping[str, str] | None = None,
) -> StockFundFlowReviewSummaryV1:
    canonical = StockFundFlowSeriesV1.model_validate(series)
    flows = list(canonical.flows)
    name_map = names or {}

    def item(value: Any) -> StockFundFlowMoverV1:
        return StockFundFlowMoverV1(
            instrument_id=value.instrument_id,
            name=name_map.get(value.instrument_id),
            net_amount_cny=value.net_amount_cny,
            large_net_amount_cny=value.large_net_amount_cny,
            extra_large_net_amount_cny=value.extra_large_net_amount_cny,
        )

    ranked = sorted(flows, key=lambda value: (-value.net_amount_cny, value.instrument_id))
    return StockFundFlowReviewSummaryV1(
        scanned_count=len(flows),
        positive_count=sum(value.net_amount_cny > 0 for value in flows),
        negative_count=sum(value.net_amount_cny < 0 for value in flows),
        flat_count=sum(value.net_amount_cny == 0 for value in flows),
        median_net_amount_cny=median(value.net_amount_cny for value in flows),
        total_net_amount_cny=sum(value.net_amount_cny for value in flows),
        total_large_net_amount_cny=sum(value.large_net_amount_cny for value in flows),
        top_inflows=tuple(item(value) for value in ranked[:15]),
        top_outflows=tuple(item(value) for value in reversed(ranked[-15:])),
    )


def summarize_dragon_tiger(series: DragonTigerSeriesV1) -> DragonTigerReviewSummaryV1:
    canonical = DragonTigerSeriesV1.model_validate(series)
    trades = list(canonical.trades)

    def item(value: Any) -> DragonTigerTradeSummaryV1:
        return DragonTigerTradeSummaryV1(
            instrument_id=value.instrument_id,
            name=value.name,
            change_pct=value.change_pct,
            turnover_pct=value.turnover_pct,
            market_amount_cny=value.market_amount_cny,
            buy_amount_cny=value.buy_amount_cny,
            sell_amount_cny=value.sell_amount_cny,
            net_amount_cny=value.net_amount_cny,
            reason=value.reason,
        )

    ranked = sorted(trades, key=lambda value: (-value.net_amount_cny, value.instrument_id))
    return DragonTigerReviewSummaryV1(
        listed_count=len(trades),
        positive_net_count=sum(value.net_amount_cny > 0 for value in trades),
        negative_net_count=sum(value.net_amount_cny < 0 for value in trades),
        total_buy_amount_cny=sum(value.buy_amount_cny for value in trades),
        total_sell_amount_cny=sum(value.sell_amount_cny for value in trades),
        total_net_amount_cny=sum(value.net_amount_cny for value in trades),
        top_net_buys=tuple(item(value) for value in ranked[:12]),
        top_net_sells=tuple(item(value) for value in reversed(ranked[-12:])),
    )


def summarize_intraday(
    samples: Sequence[Mapping[str, Any]],
    *,
    trade_date: date | None = None,
) -> IntradayReviewSummaryV1:
    by_time: dict[datetime, MarketWatchSnapshotV1] = {}
    expected_by_date: dict[date, frozenset[datetime]] = {}
    for raw in samples:
        payload = raw.get("payload") if isinstance(raw, Mapping) else None
        if not isinstance(payload, Mapping):
            payload = raw if isinstance(raw, Mapping) else None
        if payload is None:
            continue
        try:
            snapshot = MarketWatchSnapshotV1.model_validate(payload)
        except Exception:
            continue
        if trade_date is not None and snapshot.market_state.trading_date != trade_date:
            continue
        local_as_of = snapshot.as_of.astimezone(SHANGHAI)
        minute = local_as_of.replace(second=0, microsecond=0)
        expected = expected_by_date.setdefault(
            minute.date(),
            frozenset(expected_market_watch_minutes(minute.date())),
        )
        if minute not in expected:
            continue
        existing = by_time.get(minute)
        if existing is None or snapshot.as_of > existing.as_of:
            by_time[minute] = snapshot
    snapshots = sorted(by_time.values(), key=lambda item: item.as_of)
    if not snapshots:
        return IntradayReviewSummaryV1(
            sample_count=0,
            coverage_ratio=0,
            trajectory_statement="未取得当日盘中快照历史，无法判断早盘与午后演变。",
        )

    def advance(item: MarketWatchSnapshotV1) -> float | None:
        return item.breadth.advance_ratio if item.breadth.available else None

    def index_mean(item: MarketWatchSnapshotV1) -> float | None:
        values = [quote.change_pct for quote in item.indices if quote.available and quote.change_pct is not None]
        return mean(values) if values else None

    regimes = [item.guardrail.regime.value for item in snapshots]
    with_ratio = [(item.as_of, advance(item)) for item in snapshots if advance(item) is not None]
    morning = [item for item in snapshots if item.as_of.astimezone(SHANGHAI).time() <= time(11, 30)]
    afternoon = [item for item in snapshots if item.as_of.astimezone(SHANGHAI).time() >= time(13, 0)]
    first_ratio = with_ratio[0][1] if with_ratio else None
    last_ratio = with_ratio[-1][1] if with_ratio else None
    transitions = sum(left != right for left, right in zip(regimes, regimes[1:]))
    if first_ratio is not None and last_ratio is not None:
        change = last_ratio - first_ratio
        shape = "参与度走强" if change >= 0.08 else "参与度走弱" if change <= -0.08 else "参与度总体稳定"
        trajectory = f"从首个有效广度样本上涨占比{first_ratio * 100:.1f}%到最后一个有效样本{last_ratio * 100:.1f}%，{shape}，结构状态切换{transitions}次。"
    else:
        trajectory = f"盘中结构状态切换{transitions}次，但上涨占比轨迹不完整。"
    strongest = max(with_ratio, key=lambda pair: pair[1])[0] if with_ratio else None
    weakest = min(with_ratio, key=lambda pair: pair[1])[0] if with_ratio else None
    return IntradayReviewSummaryV1(
        sample_count=len(snapshots),
        coverage_ratio=min(
            1.0,
            len(snapshots) / EXPECTED_MARKET_WATCH_MINUTES,
        ),
        first_as_of=snapshots[0].as_of,
        last_as_of=snapshots[-1].as_of,
        first_regime=regimes[0],
        last_regime=regimes[-1],
        regime_transitions=transitions,
        first_advance_ratio=first_ratio,
        last_advance_ratio=last_ratio,
        min_advance_ratio=min((value for _, value in with_ratio), default=None),
        max_advance_ratio=max((value for _, value in with_ratio), default=None),
        morning_close_advance_ratio=next(
            (value for value in (advance(item) for item in reversed(morning)) if value is not None),
            None,
        ),
        afternoon_open_advance_ratio=next(
            (value for value in (advance(item) for item in afternoon) if value is not None),
            None,
        ),
        first_index_mean_change_pct=index_mean(snapshots[0]),
        last_index_mean_change_pct=index_mean(snapshots[-1]),
        strongest_as_of=strongest,
        weakest_as_of=weakest,
        trajectory_statement=trajectory,
    )


def _metadata_component(name: str, value: Any, record_count: int) -> EvidenceComponentV1:
    metadata = value.metadata
    return EvidenceComponentV1(
        component=name,
        status=EvidenceStatus.ACCEPTED if metadata.quality == QualityStatus.ACCEPTED else EvidenceStatus.DEGRADED,
        record_count=record_count,
        contract=metadata.contract,
        provider=metadata.provider,
        provider_as_of=metadata.provider_as_of,
        flags=tuple(metadata.quality_flags),
    )


def _unavailable(name: str, exc: Exception | str) -> EvidenceComponentV1:
    code = exc if isinstance(exc, str) else type(exc).__name__
    return EvidenceComponentV1(component=name, status=EvidenceStatus.UNAVAILABLE, error_code=code)


def collect_daily_market_review_evidence(
    market_watch: MarketWatchSnapshotV1 | Mapping[str, Any],
    *,
    history_samples: Sequence[Mapping[str, Any]] = (),
    collected_at: datetime | None = None,
    loaders: Mapping[str, Callable[[], Any]] | None = None,
) -> DailyMarketReviewEvidenceV1:
    """Collect each configured market surface once with independent degradation."""

    snapshot = MarketWatchSnapshotV1.model_validate(market_watch)
    raw_now = collected_at or datetime.now(SHANGHAI)
    if raw_now.tzinfo is None or raw_now.utcoffset() is None:
        raise ValueError("collected_at must include a timezone")
    now = raw_now.astimezone(SHANGHAI)
    trade_date = snapshot.market_state.trading_date
    if loaders is None:
        from tradex.data_gateway import (
            fetch_a_share_universe_snapshot,
            fetch_dragon_tiger_day,
            fetch_etf_quotes,
            fetch_limit_up_events,
            fetch_market_breadth_snapshot,
            fetch_sector_quotes,
            fetch_stock_fund_flow_day,
        )

        loaders = {
            "market_universe": lambda: fetch_a_share_universe_snapshot(now=now),
            "market_breadth_detail": lambda: fetch_market_breadth_snapshot(now=now),
            "etfs": lambda: fetch_etf_quotes(limit=5000, now=now),
            "industry_sectors": lambda: fetch_sector_quotes("industry", now=now),
            "concept_sectors": lambda: fetch_sector_quotes("concept", now=now),
            "limit_events": lambda: fetch_limit_up_events(trade_date.isoformat(), now=now),
            "stock_fund_flow": lambda: fetch_stock_fund_flow_day(trade_date, now=now),
            "dragon_tiger": lambda: fetch_dragon_tiger_day(trade_date, now=now),
        }

    values: dict[str, Any] = {}
    errors: dict[str, Exception | str] = {}
    for name in EVIDENCE_COMPONENT_ORDER[1:-1]:
        loader = loaders.get(name)
        if loader is None:
            errors[name] = "loader_missing"
            continue
        try:
            values[name] = loader()
        except Exception as exc:  # component failures are intentionally isolated
            errors[name] = exc

    summaries: dict[str, Any] = {}
    names: dict[str, str] = {}
    if "market_universe" in values:
        try:
            summaries["market_universe"] = summarize_universe(values["market_universe"])
            names = {item.instrument_id: item.name for item in values["market_universe"].quotes}
        except Exception as exc:
            errors["market_universe"] = exc
            values.pop("market_universe", None)
    builders: dict[str, Callable[[], Any]] = {
        "etfs": lambda: summarize_etfs(values["etfs"]),
        "industry_sectors": lambda: summarize_sectors(values["industry_sectors"]),
        "concept_sectors": lambda: summarize_sectors(values["concept_sectors"]),
        "limit_events": lambda: summarize_limit_events(values["limit_events"], values.get("market_breadth_detail")),
        "stock_fund_flow": lambda: summarize_stock_fund_flows(values["stock_fund_flow"], names=names),
        "dragon_tiger": lambda: summarize_dragon_tiger(values["dragon_tiger"]),
    }
    for name, builder in builders.items():
        if name not in values:
            continue
        try:
            if name in {"stock_fund_flow", "dragon_tiger"} and values[name].trade_date != trade_date:
                raise ValueError("component_trade_date_mismatch")
            summaries[name] = builder()
        except Exception as exc:
            errors[name] = exc
            values.pop(name, None)

    intraday = summarize_intraday(history_samples, trade_date=trade_date)
    watch_status = EvidenceStatus.ACCEPTED if snapshot.freshness.status.value == "fresh" else EvidenceStatus.DEGRADED
    components = [EvidenceComponentV1(
        component="market_watch",
        status=watch_status,
        record_count=1,
        contract=snapshot.contract,
        provider="tradex_market_watch",
        provider_as_of=snapshot.as_of,
        flags=tuple(snapshot.freshness.flags),
    )]
    counts = {
        "market_universe": len(values["market_universe"].quotes) if "market_universe" in values else 0,
        "market_breadth_detail": 1 if "market_breadth_detail" in values else 0,
        "etfs": len(values["etfs"].quotes) if "etfs" in values else 0,
        "industry_sectors": len(values["industry_sectors"].quotes) if "industry_sectors" in values else 0,
        "concept_sectors": len(values["concept_sectors"].quotes) if "concept_sectors" in values else 0,
        "limit_events": values["limit_events"].pool_total if "limit_events" in values else 0,
        "stock_fund_flow": len(values["stock_fund_flow"].flows) if "stock_fund_flow" in values else 0,
        "dragon_tiger": len(values["dragon_tiger"].trades) if "dragon_tiger" in values else 0,
    }
    for name in EVIDENCE_COMPONENT_ORDER[1:-1]:
        components.append(_metadata_component(name, values[name], counts[name]) if name in values else _unavailable(name, errors.get(name, "unavailable")))
    components.append(EvidenceComponentV1(
        component="intraday_history",
        status=(EvidenceStatus.ACCEPTED if intraday.coverage_ratio >= 0.8 else EvidenceStatus.DEGRADED if intraday.sample_count else EvidenceStatus.UNAVAILABLE),
        record_count=intraday.sample_count,
        contract="market_watch_history.v1" if intraday.sample_count else None,
        provider="tradex_history" if intraday.sample_count else None,
        error_code=None if intraday.sample_count else "history_empty",
        flags=("session_coverage_partial",) if 0 < intraday.coverage_ratio < 0.8 else (),
    ))
    quality_notes = []
    for component in components:
        if component.status == EvidenceStatus.UNAVAILABLE:
            quality_notes.append(f"{component.component}:unavailable:{component.error_code}")
        elif component.status == EvidenceStatus.DEGRADED:
            quality_notes.append(f"{component.component}:degraded:{','.join(component.flags) or 'partial'}")
    return DailyMarketReviewEvidenceV1(
        trade_date=trade_date,
        collected_at=now,
        market_watch=snapshot,
        universe=summaries.get("market_universe"),
        etfs=summaries.get("etfs"),
        industry_sectors=summaries.get("industry_sectors"),
        concept_sectors=summaries.get("concept_sectors"),
        limit_events=summaries.get("limit_events"),
        stock_fund_flow=summaries.get("stock_fund_flow"),
        dragon_tiger=summaries.get("dragon_tiger"),
        intraday=intraday,
        components=tuple(components),
        quality_notes=tuple(quality_notes),
    )


__all__ = [
    "DailyMarketReviewEvidenceV1",
    "DragonTigerReviewSummaryV1",
    "EvidenceComponentV1",
    "EvidenceStatus",
    "EtfReviewSummaryV1",
    "IntradayReviewSummaryV1",
    "LimitReviewSummaryV1",
    "MarketMoverV1",
    "SectorReviewItemV1",
    "SectorReviewSummaryV1",
    "StockFundFlowReviewSummaryV1",
    "UniverseReviewSummaryV1",
    "collect_daily_market_review_evidence",
    "summarize_dragon_tiger",
    "summarize_etfs",
    "summarize_intraday",
    "summarize_limit_events",
    "summarize_sectors",
    "summarize_stock_fund_flows",
    "summarize_universe",
]
