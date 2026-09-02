"""Evidence-bounded conditional outlook derived from collector artifacts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .contracts import (
    ManualPortfolioMarketContextV1,
    ManualPortfolioMarketSnapshotV1,
    ManualPortfolioOutlookItemV1,
    ManualPortfolioOutlookV1,
    ManualPortfolioPricePlanV1,
)


def _market_context(
    value: Mapping[str, Any] | None,
) -> ManualPortfolioMarketContextV1 | None:
    if not value:
        return None
    try:
        return ManualPortfolioMarketContextV1.model_validate(value)
    except (TypeError, ValueError):
        return None


def build_manual_portfolio_outlook(
    snapshot: ManualPortfolioMarketSnapshotV1,
    *,
    generated_at: datetime,
    market_context: Mapping[str, Any] | None = None,
) -> ManualPortfolioOutlookV1:
    normalized_market_context = _market_context(market_context)
    items: list[ManualPortfolioOutlookItemV1] = []
    for quote in snapshot.items:
        non_price_degradations = {
            "amount_partial",
            "cumulative_average_partial",
        }
        price_path_usable = (
            quote.status == "accepted"
            or (
                quote.status == "degraded"
                and bool(quote.quality_flags)
                and set(quote.quality_flags).issubset(non_price_degradations)
            )
        )
        if not price_path_usable or quote.last_price is None:
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
        session_high = quote.session_high or quote.last_price
        session_low = quote.session_low or quote.last_price
        span = (session_high - session_low) if session_high and session_low else 0.0
        close_location = (
            (quote.last_price - session_low) / span
            if span > 0 and quote.last_price is not None
            else 0.5
        )
        midpoint = (session_high + session_low) / 2
        price_plan = None
        if span > 0:
            if close_location >= 0.7:
                pullback_zone = (midpoint, midpoint + span * 0.15)
            else:
                pullback_zone = (midpoint - span * 0.10, midpoint)
            pressure_zone = (session_low + span * 0.80, session_high)
            price_plan = ManualPortfolioPricePlanV1(
                previous_close=round(quote.last_price, 2),
                previous_low=round(session_low, 2),
                previous_midpoint=round(midpoint, 2),
                previous_high=round(session_high, 2),
                pullback_observation_zone=tuple(
                    round(value, 2) for value in pullback_zone
                ),
                pressure_observation_zone=tuple(
                    round(value, 2) for value in pressure_zone
                ),
                risk_reference=round(session_low, 2),
            )
        if change > 0.5 and close_location >= 0.7:
            direction = "偏强收尾"
            next_session = (
                f"若次日能守住本次区间中位 {midpoint:.2f}，并再次接近或突破 "
                f"{session_high:.2f}，才确认强势延续；失守中位且不能快速收复则不成立。"
            )
        elif change > 0.5:
            direction = "上涨但收盘确认不足"
            next_session = (
                f"日内曾上探 {session_high:.2f}，但收盘只处于区间中部附近。"
                f"次日须先守住 {midpoint:.2f} 并重新进入上部区间，才恢复偏强确认；"
                f"跌向或跌破 {session_low:.2f} 则转为风险观察。"
            )
        elif change < -0.5 and close_location <= 0.3:
            direction = "偏弱收尾"
            next_session = (
                f"若次日仍不能收复本次区间中位 {midpoint:.2f}，弱势可能延续；"
                "快速收复并站稳中位则原判断失效。"
            )
        elif change < -0.5:
            direction = "下跌但出现回收"
            next_session = (
                f"次日只有站稳本次区间中位 {midpoint:.2f} 才确认修复；"
                f"重新靠近 {session_low:.2f} 则弱势重新占优。"
            )
        else:
            direction = "震荡"
            next_session = (
                f"若次日有效离开 {session_low:.2f}–{session_high:.2f} 区间，"
                "再按突破方向观察；区间内不作方向判断。"
            )
        limitations = [
            "不含账户数量、成本、盈亏、可卖数量或真实仓位事实",
            "不提供确定性价格预测或交易指令",
        ]
        if quote.status == "degraded":
            limitations.append(
                "价格路径可核验，但成交额或累计均价字段不完整；不作量价判断"
            )
        if quote.status == "degraded":
            confirmation_conditions = (
                "后续价格样本时间连续且可核验",
                "涉及量价的结论须等待成交额与累计均价质量恢复",
            )
            invalidation_conditions = (
                "价格重新穿越本次观察区间的相反边界",
                "价格路径变为 stale、unavailable 或出现价格字段缺口",
            )
            multi_session = (
                "仅当未来 2–5 个交易日的价格路径连续同向、且时间与价格质量持续可核验时，"
                "才把短时特征视为可延续；成交额证据恢复前不作量价确认。"
            )
        else:
            confirmation_conditions = (
                "后续样本保持 accepted 且时间连续",
                "至少两个交易日的区间方向相互确认",
            )
            invalidation_conditions = (
                "价格重新穿越本次观察区间的相反边界",
                "任一关键样本变为 stale、degraded 或 unavailable",
            )
            multi_session = (
                "仅当未来 2–5 个交易日出现连续同向收盘、且行情质量持续 accepted 时，"
                "才把短时特征视为可延续；单日波动不构成确认。"
            )
        opening_scenarios = ()
        market_scenarios = ()
        if price_plan is not None:
            pullback_low, pullback_high = price_plan.pullback_observation_zone
            pressure_low, pressure_high = price_plan.pressure_observation_zone
            opening_scenarios = (
                f"高开至 {pressure_low:.2f}–{pressure_high:.2f} 或更高：先观察 5–15 分钟能否站稳，"
                "不能把高开本身当成继续走强；快速跌回压力区下方视为假强。",
                f"平开并处于昨日日内区间：重点看 {pullback_low:.2f}–{pullback_high:.2f} "
                "能否形成连续价格承接，守住后才重新观察上沿。",
                f"低开至回撤观察区下方：不直接视为低位机会；至少先收复 "
                f"{price_plan.previous_midpoint:.2f}，跌破 {price_plan.risk_reference:.2f} "
                "则上一交易日区间逻辑失效。",
            )
            if normalized_market_context is not None:
                market_scenarios = (
                    f"盘面转强：{normalized_market_context.confirmation}；个股还须站稳 "
                    f"{price_plan.previous_midpoint:.2f}，才提高向压力区运行的可信度。",
                    f"震荡轮动：{normalized_market_context.expected_shape}；个股未满足确认条件时，"
                    "只按昨日日内区间观察，不因单点冲高改变判断。",
                    f"盘面转弱：{normalized_market_context.invalidation}；个股若同步失守 "
                    f"{price_plan.previous_midpoint:.2f}，取消回撤观察，优先执行失效判断。",
                )
        items.append(ManualPortfolioOutlookItemV1(
            instrument_id=quote.instrument_id,
            status="conditional",
            evidence_status=quote.status,
            next_session=f"当前仅观察为{direction}。{next_session}",
            next_2_to_5_sessions=multi_session,
            confirmation_conditions=confirmation_conditions,
            invalidation_conditions=invalidation_conditions,
            limitations=tuple(limitations),
            price_plan=price_plan,
            opening_scenarios=opening_scenarios,
            market_scenarios=market_scenarios,
        ))
    return ManualPortfolioOutlookV1(
        portfolio_revision=snapshot.portfolio_revision,
        source_snapshot_revision=snapshot.snapshot_revision,
        source_trading_date=snapshot.trading_date,
        generated_at=generated_at,
        market_context=normalized_market_context,
        items=tuple(items),
    )


__all__ = ["build_manual_portfolio_outlook"]
