"""Evidence-bounded conditional outlook derived from collector artifacts."""

from __future__ import annotations

from datetime import datetime

from .contracts import (
    ManualPortfolioMarketSnapshotV1,
    ManualPortfolioOutlookItemV1,
    ManualPortfolioOutlookV1,
)


def build_manual_portfolio_outlook(
    snapshot: ManualPortfolioMarketSnapshotV1,
    *,
    generated_at: datetime,
) -> ManualPortfolioOutlookV1:
    items: list[ManualPortfolioOutlookItemV1] = []
    for quote in snapshot.items:
        if quote.status != "accepted" or quote.last_price is None:
            items.append(ManualPortfolioOutlookItemV1(
                instrument_id=quote.instrument_id,
                status="abstain",
                evidence_status=quote.status,
                next_session="当前证据质量不足，不形成方向性前瞻。",
                next_2_to_5_sessions="等待连续、可核验的新鲜交易样本后再评估。",
                confirmation_conditions=(),
                invalidation_conditions=("行情质量恢复为 accepted 前保持观望",),
                limitations=(quote.reason or "行情证据不完整",),
            ))
            continue
        change = quote.session_change_pct or 0.0
        if change > 0.5:
            direction = "偏强"
            next_session = "若次日仍能保持在本次观察区间中上部，可继续观察强势延续；否则不成立。"
        elif change < -0.5:
            direction = "偏弱"
            next_session = "若次日仍处于本次观察区间中下部，应继续观察弱势延续；快速收复区间中部则不成立。"
        else:
            direction = "震荡"
            next_session = "若次日有效离开本次观察区间，再按突破方向观察；区间内不作方向判断。"
        items.append(ManualPortfolioOutlookItemV1(
            instrument_id=quote.instrument_id,
            status="conditional",
            evidence_status=quote.status,
            next_session=f"当前仅观察为{direction}。{next_session}",
            next_2_to_5_sessions=(
                "仅当未来 2–5 个交易日出现连续同向收盘、且行情质量持续 accepted 时，"
                "才把短时特征视为可延续；单日波动不构成确认。"
            ),
            confirmation_conditions=(
                "后续样本保持 accepted 且时间连续",
                "至少两个交易日的区间方向相互确认",
            ),
            invalidation_conditions=(
                "价格重新穿越本次观察区间的相反边界",
                "任一关键样本变为 stale、degraded 或 unavailable",
            ),
            limitations=(
                "不含账户数量、成本、盈亏、可卖数量或真实仓位事实",
                "不提供确定性价格预测或交易指令",
            ),
        ))
    return ManualPortfolioOutlookV1(
        portfolio_revision=snapshot.portfolio_revision,
        source_snapshot_revision=snapshot.snapshot_revision,
        source_trading_date=snapshot.trading_date,
        generated_at=generated_at,
        items=tuple(items),
    )


__all__ = ["build_manual_portfolio_outlook"]
