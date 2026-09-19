"""Evidence-bounded conditional outlook derived from collector artifacts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from .contracts import (
    ManualPortfolioMarketContextV1,
    ManualPortfolioMarketSnapshotV1,
    ManualPortfolioOutlookItemV1,
    ManualPortfolioOutlookV1,
    ManualPortfolioPricePlanV1,
    ManualPortfolioRelationshipContextV1,
)


def _market_context(
    value: Mapping[str, Any] | None,
    *,
    source_trading_date: date | None,
) -> ManualPortfolioMarketContextV1 | None:
    if not value:
        return None
    try:
        context = ManualPortfolioMarketContextV1.model_validate(value)
    except (TypeError, ValueError):
        return None
    if (
        source_trading_date is not None
        and context.source_trade_date != source_trading_date
    ):
        return None
    evidence = tuple(
        item
        for item in context.supporting_evidence
        if "板块" not in item or item.startswith("板块中位数")
    )
    counter = tuple(
        item
        for item in context.counter_evidence
        if "板块" not in item or item.startswith("板块中位数")
    )
    language = {
        "constructive": {
            "thesis": "市场基准偏强，但明早仍要先看上涨是否扩散；高开本身不是答案。",
            "expected_shape": "若指数、全A广度和成交强度同步改善，强势才有延续基础；否则按高位分化处理。",
            "invalidation": "主要指数回落、全A中位数转弱或上涨家数失去多数时，偏强基准取消。",
            "risk_control": "先确认市场接受高位，再评估个股；不因一次高开追认强势。",
        },
        "balanced": {
            "thesis": "市场基准没有给出单一方向，明早先等广度和成交选边。",
            "expected_shape": "更可能先分化再选择方向；单个指数或单只股票走强都不能代表环境完成确认。",
            "invalidation": "指数、全A广度和成交强度若连续背离，继续维持震荡判断。",
            "risk_control": "把个股区间与市场确认分开看，条件没凑齐时不追加方向判断。",
        },
        "defensive": {
            "thesis": "市场基准偏防守，明早先看修复有没有广度；单只股票高开不等于环境转强。",
            "expected_shape": "先弱后修复与低位震荡都可能出现；只有多数股票和成交一起改善，才把反弹升级为修复。",
            "invalidation": "主要指数转强、全A上涨家数形成多数且中位数同步回升时，防守基准取消。",
            "risk_control": "先保护失效条件，等市场修复扩散后再提高对个股突破的信任。",
        },
        "uncertain": {
            "thesis": "市场证据不足，明早不预设方向。",
            "expected_shape": "先等指数、全A广度和成交强度形成一致信号，再解释个股波动。",
            "invalidation": "任何单点异动都不足以解除证据不足状态。",
            "risk_control": "缺失证据恢复前，只记录事实，不生成方向结论。",
        },
    }[context.bias]
    return context.model_copy(
        update={
            **language,
            "confirmation": (
                "09:45 与 10:00 两个观察点中，主要指数方向、全A上涨占比、"
                "全A中位数和成交强度至少三项连续同向，才提高市场判断等级。"
            ),
            "supporting_evidence": evidence,
            "counter_evidence": counter,
        }
    )


def _relationship_context(
    value: Mapping[str, Any] | None,
) -> ManualPortfolioRelationshipContextV1 | None:
    if not value:
        return None
    try:
        return ManualPortfolioRelationshipContextV1.model_validate(value)
    except (TypeError, ValueError):
        return None


def _relationship_text(context: ManualPortfolioRelationshipContextV1 | None) -> str:
    if context is None:
        return "本地关系库没有可用归属；本次不使用供应商板块名称补位。"
    sector = context.market_sector_name if context.market_sector_status == "verified" else None
    return (
        f"聪明板块库：市场主归属 {sector or '待核验'}。"
        "次日只有同目录股票多数同向时，才把个股强弱解释为板块共振；"
        "未取得同目录聚合时明确记为未核验。"
    )


def _path_evidence(quote: Any) -> tuple[str, ...]:
    path = quote.session_path
    evidence = []
    if quote.session_change_pct is not None and quote.previous_close is not None:
        evidence.append(
            f"收盘 {quote.last_price:.2f}，较前收 {quote.session_change_pct:+.2f}%"
        )
    elif quote.last_price is not None:
        evidence.append(f"收盘参考 {quote.last_price:.2f}；前收涨幅基准未取得")
    if path is None:
        return tuple(evidence)
    evidence.append(
        f"日内 {path.low_reference:.2f}–{path.high_reference:.2f}，"
        f"收在区间 {path.close_location * 100:.0f}% 位置；"
        f"低点 {path.low_at:%H:%M}，高点 {path.high_at:%H:%M}"
    )
    segments = []
    if path.open_gap_pct is not None:
        segments.append(f"09:30参考相对前收 {path.open_gap_pct:+.2f}%")
    if path.morning_return_pct is not None:
        segments.append(f"上午 {path.morning_return_pct:+.2f}%")
    if path.afternoon_return_pct is not None:
        segments.append(f"下午 {path.afternoon_return_pct:+.2f}%")
    if path.closing_30m_return_pct is not None:
        segments.append(f"尾盘30分钟 {path.closing_30m_return_pct:+.2f}%")
    if segments:
        evidence.append("；".join(segments))
    evidence.append(
        f"路径内最大回撤 {path.max_drawdown_pct:.2f}%，"
        f"低点后最大回升 {path.max_rebound_pct:+.2f}%"
    )
    return tuple(evidence)


def _headline(change: float | None, close_location: float, closing_return: float | None) -> str:
    if change is None:
        return "价格路径可读，但涨幅基准缺失；明天只验证区间，不猜方向"
    if change > 0.5 and close_location >= 0.75:
        return (
            "强势收盘，明天先检验高位能否被接受"
            if closing_return is None or closing_return >= 0
            else "涨幅仍在，但尾盘回落；明天先看抛压是否延续"
        )
    if change > 0.5:
        return "今天上涨但没有收在高位，明天先看中位能否重新成为支撑"
    if change < -0.5 and close_location <= 0.25:
        return "弱势收盘，明天先看是否继续在低位成交"
    if change < -0.5:
        return "今天收跌但有回收，明天先验证修复能否延续"
    return "今天仍在区间里，明天等价格和市场一起选边"


def build_manual_portfolio_outlook(
    snapshot: ManualPortfolioMarketSnapshotV1,
    *,
    generated_at: datetime,
    market_context: Mapping[str, Any] | None = None,
    daily_review: Mapping[str, Any] | None = None,
    relationship_contexts: Mapping[str, Mapping[str, Any]] | None = None,
) -> ManualPortfolioOutlookV1:
    normalized_market_context = _market_context(
        market_context,
        source_trading_date=snapshot.trading_date,
    )
    items: list[ManualPortfolioOutlookItemV1] = []
    for quote in snapshot.items:
        relationship = _relationship_context(
            (relationship_contexts or {}).get(quote.instrument_id)
        )
        sector_interpretation = _relationship_text(relationship)
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
                display_name=(relationship.profile_name if relationship else None),
                status="abstain",
                evidence_status=quote.status,
                headline="证据不够，今天不把缺失行情写成观点",
                evidence_digest=_path_evidence(quote),
                relationship_context=relationship,
                sector_interpretation=sector_interpretation,
                next_session="当前证据质量不足，不形成方向性前瞻。",
                next_2_to_5_sessions="等待连续、可核验的新鲜交易样本后再评估。",
                confirmation_conditions=(),
                invalidation_conditions=("行情质量恢复为 accepted 前保持观望",),
                limitations=(quote.reason or "行情证据不完整",),
            ))
            continue
        change = quote.session_change_pct
        path = quote.session_path
        session_high = (
            path.high_reference if path is not None else quote.session_high
        ) or quote.last_price
        session_low = (
            path.low_reference if path is not None else quote.session_low
        ) or quote.last_price
        span = (session_high - session_low) if session_high and session_low else 0.0
        close_location = (
            path.close_location
            if path is not None
            else (quote.last_price - session_low) / span
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
        if change is None:
            next_session = (
                f"前收涨幅基准没有取得，但 {session_low:.2f}–{session_high:.2f} 的"
                "价格路径仍可核验。明天先看价格是否有效离开这个区间；"
                "在此之前不把未知涨幅写成震荡或转强。"
            )
        elif change > 0.5 and close_location >= 0.7:
            next_session = (
                f"今天收在日内高位区。明天真正要回答的是：{midpoint:.2f} 能否从"
                f"中位线变成支撑，并让价格重新接受 {session_high:.2f} 附近；"
                "高开后很快跌回中位线下，说明高位没有被接受。"
            )
        elif change > 0.5:
            next_session = (
                f"今天涨了，但冲到 {session_high:.2f} 后没有收在最高区域。"
                f"明天先看 {midpoint:.2f} 能否被重新站稳，再看上沿；"
                f"如果价格反而回到 {session_low:.2f} 附近，今天的上涨不再提供延续证据。"
            )
        elif change < -0.5 and close_location <= 0.3:
            next_session = (
                f"今天收在低位区。明天若始终回不到 {midpoint:.2f}，说明市场仍在"
                "接受低价；若开盘后快速收复并连续站稳中位，弱势读法才失效。"
            )
        elif change < -0.5:
            next_session = (
                f"今天虽然收跌，但离开了 {session_low:.2f} 的低点。明天先验证这段"
                f"回收能否把价格带回并留在 {midpoint:.2f} 上方；重新靠近低点则修复失败。"
            )
        else:
            next_session = (
                f"今天没有离开 {session_low:.2f}–{session_high:.2f} 的核心区间。"
                "明天先等价格在区间外持续，而不是对区间内每次冲高或回落改口。"
            )
        limitations = [
            "不含账户数量、成本、盈亏、可卖数量或真实仓位事实",
            "不提供确定性价格预测或交易指令",
        ]
        if change is None:
            limitations.append("标准实时行情涨幅基准不可用；不作当日强弱归类")
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
        tomorrow_checkpoints = ()
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
            catalog_label = (
                relationship.market_sector_name
                if relationship is not None and relationship.market_sector_status == "verified"
                else "聪明板块库待核验"
            )
            tomorrow_checkpoints = (
                f"09:25：只记录相对前收 {price_plan.previous_close:.2f} 的竞价缺口；"
                "集合竞价是开盘条件，不是一条趋势线。",
                f"09:45：先看价格能否留在 {pullback_low:.2f}–{pullback_high:.2f} "
                f"之上，并核对 {catalog_label} 同类是否多数同向；未取得同目录聚合就标记未核验。",
                "10:00：再核对主要指数、全A上涨占比、全A中位数和成交强度；"
                "至少三项在两个观察点连续同向，才提高对个股突破的信任。",
                f"14:30以后：触及 {pressure_low:.2f}–{pressure_high:.2f} 只算到达；"
                "能否收在区间内才算接受，触及后回落必须单独记录。",
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
            display_name=(relationship.profile_name if relationship else None),
            status="conditional",
            evidence_status=quote.status,
            headline=_headline(
                change,
                close_location,
                path.closing_30m_return_pct if path is not None else None,
            ),
            evidence_digest=_path_evidence(quote),
            tomorrow_checkpoints=tomorrow_checkpoints,
            relationship_context=relationship,
            sector_interpretation=sector_interpretation,
            next_session=next_session,
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
        daily_review=daily_review,
        items=tuple(items),
    )


__all__ = ["build_manual_portfolio_outlook"]
