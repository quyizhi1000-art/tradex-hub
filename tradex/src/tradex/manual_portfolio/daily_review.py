"""Daily evidence review for the previous next-session outlook."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from tradex.market_calendar import CalendarDayStatus, calendar_day_status

from .contracts import (
    ManualPortfolioDailyReviewItemV1,
    ManualPortfolioDailyReviewV1,
    ManualPortfolioMarketSnapshotV1,
    ManualPortfolioOutlookV1,
)
from .intraday_analysis import _price_path_usable


def _next_verified_trading_date(value: date) -> date | None:
    candidate = value + timedelta(days=1)
    for _ in range(14):
        status = calendar_day_status(candidate)
        if status is CalendarDayStatus.VERIFIED_TRADING_DAY:
            return candidate
        if status is CalendarDayStatus.UNVERIFIED:
            return None
        candidate += timedelta(days=1)
    return None


def build_manual_portfolio_daily_review(
    previous: ManualPortfolioOutlookV1,
    realized: ManualPortfolioMarketSnapshotV1,
    *,
    generated_at: datetime,
) -> ManualPortfolioDailyReviewV1 | None:
    """Compare only measurable prior price-plan conditions with the next session."""

    if previous.source_trading_date is None or realized.trading_date is None:
        return None
    if realized.trading_date != _next_verified_trading_date(
        previous.source_trading_date
    ):
        return None
    quotes = {item.instrument_id: item for item in realized.items}
    items: list[ManualPortfolioDailyReviewItemV1] = []
    for outlook_item in previous.items:
        quote = quotes.get(outlook_item.instrument_id)
        plan = outlook_item.price_plan
        if (
            quote is None
            or plan is None
            or quote.last_price is None
            or quote.session_low is None
            or quote.session_high is None
            or not _price_path_usable(quote.status, quote.quality_flags)
        ):
            items.append(
                ManualPortfolioDailyReviewItemV1(
                    instrument_id=outlook_item.instrument_id,
                    outcome="not_evaluable",
                    headline="今天没有足够路径，昨天的方案不能打分",
                    observation="当日价格路径不足，无法核验前一日的条件观察方案。",
                    worked="保留了证据不足状态，没有把缺失行情算成未命中。",
                    missed="缺少可核验的当日高低区间或收盘路径。",
                    next_adjustment="等完整收盘路径恢复后再复核；不把后续某个单点补成全天结果。",
                    limitations=(quote.reason if quote and quote.reason else "缺少可核验的完整价格区间",),
                )
            )
            continue

        pullback_low, pullback_high = plan.pullback_observation_zone
        pressure_low, pressure_high = plan.pressure_observation_zone
        pullback_touched = quote.session_low <= pullback_high and quote.session_high >= pullback_low
        pressure_touched = quote.session_high >= pressure_low
        risk_breached = quote.session_low < plan.risk_reference
        midpoint_held = quote.last_price >= plan.previous_midpoint
        evidence = (
            f"当日区间 {quote.session_low:.2f}–{quote.session_high:.2f}，收盘参考 {quote.last_price:.2f}",
            f"回撤观察区{'触及' if pullback_touched else '未触及'}，压力观察区{'触及' if pressure_touched else '未触及'}",
            f"收盘参考{'位于' if midpoint_held else '低于'}前一日中位 {plan.previous_midpoint:.2f}",
            *(
                (
                    f"低点 {quote.session_path.low_at:%H:%M}、高点 {quote.session_path.high_at:%H:%M}，"
                    f"尾盘30分钟 {quote.session_path.closing_30m_return_pct:+.2f}%",
                )
                if quote.session_path is not None
                and quote.session_path.closing_30m_return_pct is not None
                else ()
            ),
        )
        if risk_breached:
            outcome = "invalidated"
            headline = "昨天的风险线被跌破，原方案到这里结束"
            observation = "当日跌破前一日风险参考位，原条件观察方案失效。"
            worked = "风险参考位及时阻止了继续沿用旧区间。"
            missed = "昨天的区间没有覆盖今天更低的价格接受区。"
            next_adjustment = "下一份分析从今天的新区间重建，不再引用昨天的回撤观察区。"
        elif quote.session_high >= pressure_high and quote.last_price >= pressure_low:
            outcome = "conditions_met"
            headline = "价格不只到了压力区，收盘也留在里面"
            observation = "昨天的上沿观察得到收盘确认，但这只说明条件成立，不等于收益预测命中。"
            worked = "区分了“盘中触及”和“收盘接受”，避免把一次冲高当成确认。"
            missed = "这份复核仍未回答隔夜公告、竞价订单和同目录板块是否共同推动。"
            next_adjustment = "明天把今天的压力区当作新的接受区，先看回踩能否守住，再谈延续。"
        else:
            outcome = "mixed"
            headline = "价格碰到了观察区，但没有完成收盘确认"
            observation = "昨天的方案只解释了盘中到达，没有解释收盘接受；这次记为部分成立。"
            worked = "观察区帮助定位了盘中反应位置。"
            missed = (
                "压力区被触及后没有收住。"
                if pressure_touched
                else "价格没有到达压力区，原强势路径未出现。"
            )
            next_adjustment = "下一次提高“收盘留在区间内”的权重，单点触及不再升级判断。"
        limitations = ["只复核可量化的价格区间条件，不把结果解释为交易收益"]
        if quote.status == "degraded":
            limitations.append("成交额或累计均价字段不完整，本次不复核量价条件")
        items.append(
            ManualPortfolioDailyReviewItemV1(
                instrument_id=outlook_item.instrument_id,
                outcome=outcome,
                headline=headline,
                observation=observation,
                worked=worked,
                missed=missed,
                next_adjustment=next_adjustment,
                evidence=evidence,
                limitations=tuple(limitations),
            )
        )

    counts = {key: sum(item.outcome == key for item in items) for key in (
        "conditions_met", "mixed", "invalidated", "not_evaluable"
    )}
    evaluable = len(items) - counts["not_evaluable"]
    if evaluable == 0:
        self_summary = "今天没有一只股票具备完整复核证据；不做命中率，也不拿缺失数据替昨天的分析开脱。"
    elif counts["invalidated"]:
        self_summary = (
            f"今天有 {counts['invalidated']} 只跌破昨天的风险线。最重要的不是统计命中，"
            "而是停止沿用已经失效的区间，并用今天的新路径重建明日观察。"
        )
    elif counts["mixed"]:
        self_summary = (
            f"今天可复核 {evaluable} 只，其中 {counts['mixed']} 只只完成部分条件。"
            "这说明“到过某个价位”不足以证明判断成立，下一版继续把触及和收盘接受分开。"
        )
    else:
        self_summary = (
            f"今天可复核的 {evaluable} 只都完成了收盘条件。仍不把它写成预测胜率；"
            "下一步要检查这种确认能否跨交易日重复，而不是用一次结果自证。"
        )
    return ManualPortfolioDailyReviewV1(
        reviewed_outlook_date=previous.source_trading_date,
        realized_trading_date=realized.trading_date,
        generated_at=generated_at,
        self_summary=self_summary,
        items=tuple(items),
    )


__all__ = ["build_manual_portfolio_daily_review"]
