"""Evidence-bounded analysis for the currently open A-share session."""

from __future__ import annotations

from datetime import datetime

from .contracts import (
    ManualPortfolioIntradayAnalysisItemV1,
    ManualPortfolioIntradayAnalysisV1,
    ManualPortfolioMarketSnapshotV1,
)


_NON_PRICE_DEGRADATIONS = {
    "amount_partial",
    "cumulative_average_partial",
}


def _price_path_usable(status: str, quality_flags: tuple[str, ...]) -> bool:
    return status == "accepted" or (
        status == "degraded"
        and bool(quality_flags)
        and set(quality_flags).issubset(_NON_PRICE_DEGRADATIONS)
    )


def build_manual_portfolio_intraday_analysis(
    snapshot: ManualPortfolioMarketSnapshotV1,
    *,
    generated_at: datetime,
) -> ManualPortfolioIntradayAnalysisV1:
    items: list[ManualPortfolioIntradayAnalysisItemV1] = []
    for quote in snapshot.items:
        if not _price_path_usable(quote.status, quote.quality_flags):
            items.append(
                ManualPortfolioIntradayAnalysisItemV1(
                    instrument_id=quote.instrument_id,
                    status="abstain",
                    evidence_status=quote.status,
                    current_observation="盘中证据质量不足，不形成方向判断。",
                    confirmation_conditions=(),
                    invalidation_conditions=("行情质量恢复前保持观望",),
                    limitations=(quote.reason or "盘中行情证据不完整",),
                )
            )
            continue
        change = quote.session_change_pct or 0.0
        if change >= 1.0:
            state = "盘中偏强"
            confirmation = "后续新鲜分钟样本继续处于当日区间中上部"
            invalidation = "价格跌回当日区间中部以下"
        elif change <= -1.0:
            state = "盘中偏弱"
            confirmation = "后续新鲜分钟样本继续处于当日区间中下部"
            invalidation = "价格收复当日区间中部以上"
        else:
            state = "盘中震荡"
            confirmation = "新鲜分钟样本有效离开当日区间并连续确认"
            invalidation = "突破后重新回到原区间"
        last_price = f"{quote.last_price:.2f}" if quote.last_price is not None else "--"
        observation = f"{state}；最新价 {last_price}，当日路径变化 {change:+.2f}%。"
        limitations = ["仅分析当前交易时段，不替代昨晚的次日前瞻"]
        if quote.status == "degraded":
            limitations.append("成交额或累计均价字段不完整；不作量价判断")
        items.append(
            ManualPortfolioIntradayAnalysisItemV1(
                instrument_id=quote.instrument_id,
                status="conditional",
                evidence_status=quote.status,
                current_observation=observation,
                confirmation_conditions=(confirmation, "后续样本时间连续且未过期"),
                invalidation_conditions=(invalidation, "行情变为 stale 或 unavailable"),
                limitations=tuple(limitations),
            )
        )
    return ManualPortfolioIntradayAnalysisV1(
        portfolio_revision=snapshot.portfolio_revision,
        source_snapshot_revision=snapshot.snapshot_revision,
        source_trading_date=snapshot.trading_date,
        generated_at=generated_at,
        items=tuple(items),
    )


__all__ = ["build_manual_portfolio_intraday_analysis"]
