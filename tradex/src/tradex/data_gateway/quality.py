"""Quality gates for canonical gateway contracts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from .contracts import (
    BoardLeaderV1,
    EtfQuoteV1,
    IndexQuoteV1,
    LeaderQuoteV1,
    LimitEventTradeStatusV1,
    LimitUpEventV1,
    MarketTurnoverV1,
    OHLCVBarV1,
    ParticipationIndexV1,
    QualityStatus,
    SectorQuoteV1,
    StockSectorProfileV1,
)


class DataQualityError(RuntimeError):
    """Raised when provider data cannot satisfy the canonical contract."""


def assess_market_overview(
    *,
    indices: Sequence[IndexQuoteV1],
    participation_indices: Sequence[ParticipationIndexV1],
    turnover: MarketTurnoverV1,
    provider_as_of: datetime | None,
) -> tuple[QualityStatus, tuple[str, ...]]:
    """Return a deterministic quality status without inventing missing data."""

    available = [item for item in indices if item.available]
    if not available:
        raise DataQualityError("行情结果中未找到可用的上证指数或深证成指")

    flags: list[str] = []
    if len(available) < len(indices):
        flags.append("required_index_partial")
    if not participation_indices:
        flags.append("participation_indices_missing")
    if not turnover.available:
        flags.append("market_turnover_unavailable")
    if provider_as_of is None:
        flags.append("provider_timestamp_missing")

    return (
        QualityStatus.DEGRADED if flags else QualityStatus.ACCEPTED,
        tuple(flags),
    )


def assess_quote_snapshot(
    *,
    previous_close: float | None,
    open: float | None,
    high: float | None,
    low: float | None,
    volume_shares: float | None,
    amount_cny: float | None,
    provider_as_of: datetime | None,
    provider_units_verified: bool,
) -> tuple[QualityStatus, tuple[str, ...]]:
    flags: list[str] = []
    if any(value is None for value in (previous_close, open, high, low)):
        flags.append("session_prices_partial")
    if volume_shares is None:
        flags.append("volume_missing")
    if amount_cny is None:
        flags.append("amount_missing")
    if provider_as_of is None:
        flags.append("provider_timestamp_missing")
    if not provider_units_verified:
        flags.append("provider_units_unverified")
    return (
        QualityStatus.DEGRADED if flags else QualityStatus.ACCEPTED,
        tuple(flags),
    )


def assess_ohlcv_series(
    *,
    bars: Sequence[OHLCVBarV1],
    provider_as_of: datetime | None,
    provider_units_verified: bool,
) -> tuple[QualityStatus, tuple[str, ...]]:
    if not bars:
        raise DataQualityError("历史 K 线结果为空")

    flags: list[str] = []
    if any(item.volume_shares is None for item in bars):
        flags.append("volume_partial")
    if any(item.amount_cny is None for item in bars):
        flags.append("amount_partial")
    if provider_as_of is None:
        flags.append("provider_timestamp_missing")
    if not provider_units_verified:
        flags.append("provider_units_unverified")
    return (
        QualityStatus.DEGRADED if flags else QualityStatus.ACCEPTED,
        tuple(flags),
    )


def assess_market_breadth(
    *,
    provider_as_of: datetime | None,
    unclassified_count: int,
    universe_verified: bool,
) -> tuple[QualityStatus, tuple[str, ...]]:
    flags: list[str] = []
    if unclassified_count:
        flags.append("participation_unclassified")
    if not universe_verified:
        flags.append("universe_definition_unverified")
    if provider_as_of is None:
        flags.append("provider_timestamp_missing")
    return (
        QualityStatus.DEGRADED if flags else QualityStatus.ACCEPTED,
        tuple(flags),
    )


def assess_sector_quotes(
    *,
    quotes: Sequence[SectorQuoteV1],
    provider_as_of: datetime | None,
    provider_units_verified: bool,
) -> tuple[QualityStatus, tuple[str, ...]]:
    if not quotes:
        raise DataQualityError("板块行情结果为空")

    flags: list[str] = []
    if any(item.amount_cny is None for item in quotes):
        flags.append("amount_partial")
    if any(item.main_net_inflow_cny is None for item in quotes):
        flags.append("main_net_inflow_partial")
    if any(item.up_count is None or item.down_count is None for item in quotes):
        flags.append("constituent_breadth_partial")
    if provider_as_of is None:
        flags.append("provider_timestamp_missing")
    elif any(item.provider_as_of is None for item in quotes):
        flags.append("provider_timestamp_partial")
    if not provider_units_verified:
        flags.append("provider_units_unverified")
    return (
        QualityStatus.DEGRADED if flags else QualityStatus.ACCEPTED,
        tuple(flags),
    )


def assess_etf_quotes(
    *,
    quotes: Sequence[EtfQuoteV1],
    provider_as_of: datetime | None,
    provider_units_verified: bool,
) -> tuple[QualityStatus, tuple[str, ...]]:
    if not quotes:
        raise DataQualityError("ETF 行情结果为空")

    flags: list[str] = []
    if provider_as_of is None:
        flags.append("provider_timestamp_missing")
    elif any(item.provider_as_of is None for item in quotes):
        flags.append("provider_timestamp_partial")
    if not provider_units_verified:
        flags.append("provider_units_unverified")
    return (
        QualityStatus.DEGRADED if flags else QualityStatus.ACCEPTED,
        tuple(flags),
    )


def assess_leader_quotes(
    *,
    quotes: Sequence[LeaderQuoteV1],
    requested_total: int,
    provider_as_of: datetime | None,
) -> tuple[QualityStatus, tuple[str, ...]]:
    if not quotes:
        raise DataQualityError("领涨股行情结果为空")
    if len(quotes) > requested_total:
        raise DataQualityError("领涨股行情数量超过请求数量")

    flags: list[str] = []
    if len(quotes) < requested_total:
        flags.append("quote_coverage_partial")
    if provider_as_of is None:
        flags.append("provider_timestamp_missing")
    elif any(item.provider_as_of is None for item in quotes):
        flags.append("provider_timestamp_partial")
    return (
        QualityStatus.DEGRADED if flags else QualityStatus.ACCEPTED,
        tuple(flags),
    )


def assess_stock_sector_profiles(
    *,
    profiles: Sequence[StockSectorProfileV1],
    requested_total: int,
    trade_date: str,
) -> tuple[QualityStatus, tuple[str, ...]]:
    if len(profiles) != requested_total:
        raise DataQualityError("股票行业 profile 返回行数与请求不一致")
    if any(item.provider_as_of.date().isoformat() != trade_date for item in profiles):
        raise DataQualityError("股票行业 profile 存在缺失或错误交易日")
    return QualityStatus.ACCEPTED, ()


def assess_board_leaders(
    *,
    leaders: Sequence[BoardLeaderV1],
    provider_as_of: datetime | None,
) -> tuple[QualityStatus, tuple[str, ...]]:
    if not leaders:
        raise DataQualityError("板块领涨成分为空")

    flags: list[str] = []
    if any(
        any(
            value is None
            for value in (
                item.price,
                item.change_pct,
                item.amount_cny,
                item.turnover_pct,
                item.main_net_inflow_cny,
                item.main_net_inflow_pct,
            )
        )
        for item in leaders
    ):
        flags.append("leader_metrics_partial")
    if provider_as_of is None:
        flags.append("provider_timestamp_missing")
    elif any(item.provider_as_of is None for item in leaders):
        flags.append("provider_timestamp_partial")
    return (
        QualityStatus.DEGRADED if flags else QualityStatus.ACCEPTED,
        tuple(flags),
    )


def assess_limit_events(
    *,
    events: Sequence[LimitUpEventV1],
    trade_status: LimitEventTradeStatusV1,
    provider_as_of: datetime | None,
) -> tuple[QualityStatus, tuple[str, ...]]:
    flags: list[str] = []
    if any(item.board_count is None for item in events):
        flags.append("board_count_partial")
    if any(
        any(
            value is None
            for value in (
                item.price_cny,
                item.change_pct,
                item.limit_up_type,
                item.seal_success_pct,
                item.open_count,
                item.order_amount_cny,
                item.first_sealed_at,
                item.resealed,
            )
        )
        for item in events
    ):
        flags.append("event_details_partial")
    if trade_status.code == "unknown":
        flags.append("trade_status_unrecognized")
    if provider_as_of is None:
        flags.append("provider_timestamp_missing")
    return (
        QualityStatus.DEGRADED if flags else QualityStatus.ACCEPTED,
        tuple(flags),
    )


__all__ = [
    "DataQualityError",
    "assess_board_leaders",
    "assess_etf_quotes",
    "assess_leader_quotes",
    "assess_limit_events",
    "assess_market_breadth",
    "assess_market_overview",
    "assess_ohlcv_series",
    "assess_quote_snapshot",
    "assess_sector_quotes",
    "assess_stock_sector_profiles",
]
