"""Versioned bullish MACD state plus a dated J upturn; low J is a label only."""

from collections import Counter

from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1

from .contracts import MacdJCandidateV1, MacdJEvidenceV1, MacdJScreenV1
from .engine import _special_treatment_name


def screen_macd_j(snapshot: DailyStockFactorSnapshotV1, *, version="v2") -> MacdJScreenV1:
    if version not in {"v1", "v2"}:
        raise ValueError("unsupported MACD J screen version")
    window = snapshot.technicals
    days = window.days if window else ()
    maps = [{p.instrument_id: p for p in day.points} for day in days]
    st_ids = set(days[-1].special_treatment_ids) if days else set()
    excluded = Counter()
    candidates = []
    eligible = evaluated = 0
    for row in snapshot.factors:
        if row.market != "主板" or not row.instrument_id.endswith((".SH", ".SZ")):
            excluded["not_main_board"] += 1
            continue
        if _special_treatment_name(row.name) or row.instrument_id in st_ids:
            excluded["special_treatment"] += 1
            continue
        if row.delist_date is not None and row.delist_date <= snapshot.trade_date:
            excluded["delisted"] += 1
            continue
        eligible += 1
        if not days:
            excluded["technical_window_unavailable"] += 1
            continue
        points = [items.get(row.instrument_id) for items in maps]
        if any(p is None for p in points):
            excluded["incomplete_indicator_window"] += 1
            continue
        if row.amount_cny <= 0 or row.open <= 0 or any(p.amount_cny <= 0 for p in points):
            excluded["inactive_session"] += 1
            continue
        if abs(points[-1].close - row.close) > 0.011:
            excluded["indicator_price_mismatch"] += 1
            continue
        evaluated += 1
        yesterday, today = points[-2:]
        if today.dif <= today.dea or (version == "v1" and yesterday.dif > yesterday.dea):
            excluded["no_fresh_macd_cross" if version == "v1" else "macd_not_bullish"] += 1
            continue
        if today.j <= yesterday.j:
            excluded["j_not_rising"] += 1
            continue
        # Indices 2..5 cover T-3 through T. Two earlier values establish a turn.
        turns = [i for i in range(2, 6)
                 if points[i - 2].j > points[i - 1].j and points[i].j > points[i - 1].j]
        if not turns:
            excluded["no_recent_j_turn"] += 1
            continue
        index = turns[-1]
        gap = 5 - index
        low = points[index - 1].j
        zone = ("above_zero" if min(today.dif, today.dea) > 0 else
                "below_zero" if max(today.dif, today.dea) < 0 else "crossing_zero")
        candidates.append(MacdJCandidateV1(
            signal_rule="fresh_cross" if version == "v1" else "bullish_state",
            instrument_id=row.instrument_id, name=row.name, industry=row.industry,
            reference_close=row.close, signal_trade_date=snapshot.trade_date,
            signal_group="same_day" if gap == 0 else "prior_3_sessions",
            zero_axis_zone=zone, j_turn_date=days[index].trade_date,
            gap_sessions=gap, j_trough=low, j_turn_value=points[index].j,
            low_j_tags=tuple(tag for threshold, tag in ((20, "J<20"), (0, "J<0")) if low < threshold),
            evidence=tuple(MacdJEvidenceV1(
                trade_date=day.trade_date, dif=p.dif, dea=p.dea, k=p.k, d=p.d, j=p.j,
            ) for day, p in zip(days, points)),
        ))
    candidates.sort(key=lambda item: (item.gap_sessions, item.instrument_id))
    return MacdJScreenV1(
        screen_version=f"macd-j-upturn-main-board.{version}",
        quality="unavailable" if not evaluated else "degraded",
        universe_count=len(snapshot.factors), board_eligible_count=eligible,
        evaluated_count=evaluated, matched_count=len(candidates),
        same_day_count=sum(c.gap_sessions == 0 for c in candidates),
        prior_3_sessions_count=sum(c.gap_sessions > 0 for c in candidates),
        candidates=tuple(candidates), excluded_counts=dict(sorted(excluded.items())),
        source_metadata=tuple(day.metadata for day in days),
        methodology=(
            "仅沪深主板非 ST、非退市股票；以信号日 ST 名单和证券名称交叉排除。",
            "收盘日线、前复权 MACD(12,26,9) 与 KDJ(9,3,3)，采用数据源已发布的历史指标。",
            ("新金叉：昨天 DIF ≤ DEA，今天 DIF > DEA。" if version == "v1" else
             "金叉状态：今天 DIF > DEA，不要求当天刚发生金叉；昨天已满足的股票可以继续入选。")
            + "今天双线均正标上方、均负标下方，其余标零轴附近。",
            "J 刚拐头：前天 J > 昨天 J，今天 J > 昨天 J；平走不算下降。",
            "对照两组：同日拐头；此前 1～3 个交易日内拐头且筛选当天 J 仍高于昨天。取最近一次拐头，两组互斥。",
            "低位标签按拐头前一日的 J 谷值标注 J<20、J<0，只做标签，不过滤；不要求穿过 20 或 0。",
            "核验最近 6 个交易日指标与成交状态；缺失、停牌和指标源收盘价不一致时明确排除。",
        ),
        limitations=(
            "J 先拐头组只要求筛选当天仍上行，不要求中间每天连续上涨；超过 3 日窗口会退出，不代表趋势已经反转。",
            "指标来自数据源全历史计算，初始化与历史修订由数据源定义；不是用最近 6 日重新计算指标。",
            "数据源未提供精确发布时间，质量标为降级；保留交易日期、获取时间与请求标识，不伪造发布时间。",
            "金叉和 J 拐头不证明趋势反转或超额收益；第一版没有经过收益回测。",
        ),
    )
