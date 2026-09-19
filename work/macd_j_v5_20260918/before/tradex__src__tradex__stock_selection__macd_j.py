"""Versioned bullish MACD state plus a dated J upturn; low J is a label only."""

from collections import Counter

from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1

from .contracts import MacdJCandidateV1, MacdJEvidenceV1, MacdJScreenV1
from .engine import _special_treatment_name
from .macd_rules import recent_macd_cross_index, v4_signal, price_filter_evidence
from tradex.data_gateway.macd_price_history import sixty_sessions


def price_history_requests(snapshot):
    if snapshot.technicals is None:
        return ()
    maps = [{p.instrument_id: p for p in day.points} for day in snapshot.technicals.days]
    requests = []
    for row in snapshot.factors:
        if row.market != "主板" or _special_treatment_name(row.name):
            continue
        points = [m.get(row.instrument_id) for m in maps]
        if all(p is not None for p in points) and v4_signal(points)[0] is not None:
            requests.append(row.instrument_id)
    return tuple(requests)


def screen_macd_j(snapshot: DailyStockFactorSnapshotV1, *, version="v4") -> MacdJScreenV1:
    if version not in {"v1", "v2", "v3", "v4"}:
        raise ValueError("unsupported MACD J screen version")
    window = snapshot.technicals
    days = window.days if window else ()
    maps = [{p.instrument_id: p for p in day.points} for day in days]
    st_ids = set(days[-1].special_treatment_ids) if days else set()
    excluded = Counter()
    candidates = []
    pending = []
    histories = {h.instrument_id: h for h in window.price_histories} if window else {}
    dates60 = sixty_sessions(snapshot.trade_date) if version == "v4" else ()
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
        if version == "v4":
            kind, index = v4_signal(points)
            if kind is None:
                excluded["no_v4_signal"] += 1
                continue
            filters, reason = price_filter_evidence(histories.get(row.instrument_id), dates=dates60,
                                                    price=row.close, volume_ratio=row.volume_ratio)
            if reason:
                excluded[reason] += 1
                if reason in {"price_history_unavailable", "incomplete_60_session_history",
                              "volume_ratio_unavailable", "price_history_basis_mismatch"}:
                    evaluated -= 1
                continue
            gap, low = 5-index, points[index-1].j
            item = MacdJCandidateV1(
                signal_rule="strict_cross_v4" if kind == "confirmed" else "pending_cross_v4",
                macd_cross_date=snapshot.trade_date if kind == "confirmed" else None,
                macd_cross_age_sessions=0 if kind == "confirmed" else None,
                instrument_id=row.instrument_id, name=row.name, industry=row.industry,
                reference_close=row.close, signal_trade_date=snapshot.trade_date,
                signal_group="same_day" if gap == 0 else "prior_1_session",
                zero_axis_zone="above_zero" if min(today.dif, today.dea) > 0 else
                               "below_zero" if max(today.dif, today.dea) < 0 else "crossing_zero",
                j_turn_date=days[index].trade_date, gap_sessions=gap, j_trough=low,
                j_turn_value=points[index].j,
                low_j_tags=tuple(tag for limit, tag in ((20, "J<20"), (0, "J<0")) if low < limit),
                evidence=tuple(MacdJEvidenceV1(trade_date=day.trade_date, dif=p.dif, dea=p.dea,
                    k=p.k, d=p.d, j=p.j) for day, p in zip(days, points)), **filters)
            (candidates if kind == "confirmed" else pending).append(item)
            continue
        cross = recent_macd_cross_index([p.dif-p.dea for p in points])
        if today.dif <= today.dea or (version == "v1" and yesterday.dif > yesterday.dea):
            excluded["no_fresh_macd_cross" if version == "v1" else "macd_not_bullish"] += 1
            continue
        if version == "v3" and cross is None:
            excluded["no_macd_cross_within_two_sessions"] += 1
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
            signal_rule={"v1": "fresh_cross", "v2": "bullish_state", "v3": "recent_cross_2_sessions"}[version],
            macd_cross_date=days[cross].trade_date if version == "v3" else None,
            macd_cross_age_sessions=len(days)-1-cross if version == "v3" else None,
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
        pending_candidates=tuple(sorted(pending, key=lambda c: (c.gap_sessions, c.instrument_id))),
        pending_count=len(pending),
        source_metadata=tuple(day.metadata for day in days) + tuple(h.metadata for h in histories.values()),
        methodology=(
            "主板非 ST；MACD(12,26,9)、KDJ(9,3,3)，不限制金叉在零轴上方或下方。",
            "正式入选：昨天 DIF≤DEA，今天 DIF>DEA；J 当天或前一交易日由下降/走平转为上升，今天仍上升。",
            "待金叉预警单独列出：DIF≤DEA，DIF 今天上升，两线差距连续两个交易日缩小；J 条件相同。",
            "含筛选日最近60个交易日的前复权最高价；当前价/最高价−1 严格小于−20%，当前价≥含当天的MA5，量比≤1.5。",
            "不额外要求 K>D；J<20、J<0 仅作标签；缺失历史、量比或复权口径不一致时不入选。",
        ) if version == "v4" else (
            "仅沪深主板非 ST、非退市股票；以信号日 ST 名单和证券名称交叉排除。",
            "收盘日线、前复权 MACD(12,26,9) 与 KDJ(9,3,3)，采用数据源已发布的历史指标。",
            ({"v1": "新金叉：昨天 DIF ≤ DEA，今天 DIF > DEA。",
              "v2": "金叉状态：今天 DIF > DEA，不要求当天刚发生金叉；昨天已满足的股票可以继续入选。",
              "v3": "最近一次金叉在当天或此前 1～2 个交易日，当前 DIF > DEA；金叉当天记第 0 日，超过 2 日或当前已死叉不入选。"}[version])
            + "今天双线均正标上方、均负标下方，其余标零轴附近。",
            "J 刚拐头：前天 J > 昨天 J，今天 J > 昨天 J；平走不算下降。",
            "对照两组：同日拐头；此前 1～3 个交易日内拐头且筛选当天 J 仍高于昨天。取最近一次拐头，两组互斥。",
            "低位标签按拐头前一日的 J 谷值标注 J<20、J<0，只做标签，不过滤；不要求穿过 20 或 0。",
            "核验最近 6 个交易日指标与成交状态；缺失、停牌和指标源收盘价不一致时明确排除。",
        ),
        limitations=(
            "预警不是金叉，不保证未来金叉；正式入选与预警分别计数。",
            "仅使用筛选日及之前证据；60日高点、MA5使用同一前复权窗口，量比使用当日数据源指标。",
            "数据缺失保持未核验，不填旧值；未进行收益回测。",
        ) if version == "v4" else (
            "J 先拐头组只要求筛选当天仍上行，不要求中间每天连续上涨；超过 3 日窗口会退出，不代表趋势已经反转。",
            "指标来自数据源全历史计算，初始化与历史修订由数据源定义；不是用最近 6 日重新计算指标。",
            "数据源未提供精确发布时间，质量标为降级；保留交易日期、获取时间与请求标识，不伪造发布时间。",
            "金叉和 J 拐头不证明趋势反转或超额收益；第一版没有经过收益回测。",
        ),
    )
