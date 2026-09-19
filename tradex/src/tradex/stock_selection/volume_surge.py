"""Seven-session upward volume events using verified daily share volume."""

from collections import Counter
from decimal import Decimal
from statistics import mean

from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1

from .contracts import VolumeSurgeCandidateV1, VolumeSurgeEvidenceV1, VolumeSurgeScreenV1
from .engine import _closed_at_main_board_limit_up, _special_treatment_name


def screen_volume_surge(snapshot: DailyStockFactorSnapshotV1) -> VolumeSurgeScreenV1:
    required_dates = snapshot.candlestick_window_trade_dates[-12:]
    histories = {item.instrument_id: item for item in snapshot.candlestick_histories}
    excluded: Counter[str] = Counter()
    candidates = []
    eligible = evaluated = 0
    for row in snapshot.factors:
        if row.market != "主板":
            excluded["not_main_board"] += 1
            continue
        if _special_treatment_name(row.name):
            excluded["special_treatment"] += 1
            continue
        eligible += 1
        history = histories.get(row.instrument_id)
        by_date = {bar.trade_date: bar for bar in history.bars} if history else {}
        if len(required_dates) != 12 or any(day not in by_date for day in required_dates):
            excluded["incomplete_candlestick_window"] += 1
            continue
        bars = tuple(by_date[day] for day in required_dates)
        if any(bar.volume_shares is None or bar.volume_shares <= 0 for bar in bars):
            excluded["missing_volume_window"] += 1
            continue
        evaluated += 1
        if any(_closed_at_main_board_limit_up(bar) for bar in bars[-7:]):
            excluded["limit_up_in_7_sessions"] += 1
            continue
        evidence = []
        anchor = None
        minimum_subsequent_close = None
        rounds = 0
        unconfirmed = excessive_next_volume = False
        for index in range(5, 12):
            bar = bars[index]
            # Invalidate before evaluating today's event: a breach day may
            # itself start a fresh round, but an intact round never re-anchors.
            if anchor is not None:
                if bar.close < anchor.low:
                    evidence = []
                    anchor = None
                    minimum_subsequent_close = None
                else:
                    minimum_subsequent_close = min(
                        bar.close, minimum_subsequent_close
                        if minimum_subsequent_close is not None else bar.close,
                    )
            average = mean(item.volume_shares for item in bars[index - 5:index])
            multiple = bar.volume_shares / average
            if bar.close > bar.previous_close and multiple >= 2.0:
                if index + 1 >= len(bars):
                    unconfirmed = True
                    continue
                following = bars[index + 1]
                if Decimal(str(following.volume_shares)) * 100 > Decimal(str(bar.volume_shares)) * 66:
                    excessive_next_volume = True
                    continue
                if anchor is None:
                    anchor = bar
                    rounds += 1
                evidence.append(VolumeSurgeEvidenceV1(
                    trade_date=bar.trade_date, close=bar.close, low=bar.low,
                    previous_close=bar.previous_close,
                    change_pct=(bar.close / bar.previous_close - 1) * 100,
                    volume_shares=bar.volume_shares,
                    prior_5d_average_volume_shares=average,
                    volume_multiple=multiple,
                    next_trade_date=following.trade_date,
                    next_volume_shares=following.volume_shares,
                    next_volume_ratio=following.volume_shares / bar.volume_shares,
                ))
        if not evidence:
            reason = ("close_below_anchor_low" if rounds else
                      "next_day_volume_above_66pct" if excessive_next_volume else
                      "pending_next_day_confirmation" if unconfirmed else "no_upward_volume_surge")
            excluded[reason] += 1
            continue
        candidates.append(VolumeSurgeCandidateV1(
            instrument_id=row.instrument_id, name=row.name, industry=row.industry,
            reference_close=row.close, evidence=tuple(evidence),
            anchor_trade_date=anchor.trade_date, anchor_low=anchor.low,
            minimum_subsequent_close=minimum_subsequent_close, reset_count=rounds - 1,
        ))
    candidates.sort(key=lambda item: (
        -item.evidence[-1].trade_date.toordinal(),
        -max(event.volume_multiple for event in item.evidence), item.instrument_id,
    ))
    return VolumeSurgeScreenV1(
        screen_version="upward-volume-surge-main-board.v3",
        quality=("unavailable" if evaluated == 0 else
                 "accepted" if evaluated == eligible and snapshot.metadata.quality.value == "accepted"
                 else "degraded"),
        universe_count=len(snapshot.factors), board_eligible_count=eligible,
        evaluated_count=evaluated, matched_count=len(candidates),
        excluded_counts=dict(sorted(excluded.items())), candidates=tuple(candidates),
        methodology=(
            "以档案交易日为末日，最近 7 个交易日至少一天收盘价高于该日昨收价。",
            "该日成交量（股）须达到此前 5 个交易日平均成交量的 2 倍；均量不含该日。",
            "每个放量日须经下一交易日确认：次日成交量不高于放量日的 66%（等于允许），才计为有效命中并参与本轮底线计算。",
            "档案当日放量尚无次日数据时不计为命中；只使用截至档案日已收盘的数据，不引用未来成交量。",
            "同一 7 日窗口无收盘涨停；涨停价按昨收 × 1.10 四舍五入到 0.01 元判定。",
            "从窗口内首次放量命中开始，以命中日最低价为底线；此后每日收盘价不得低于底线，等于底线允许。",
            "收盘跌破则本轮失效；跌破当天或之后再次放量命中可重启，以新命中日最低价为底线。未跌破时再次命中不重置。",
            "仅保留截至档案日仍有效的最后一轮；命中次数和逐日证据只统计该轮。盘中跌破但收盘守住仍有效。",
            "仅主板非 ST、非退市风险标识股票；完整核验 12 个交易日，不以更早交易日替代缺失日。",
            "展示全部命中股票，按最近命中日期、最大放量倍数、股票代码排序。",
        ),
        limitations=(
            "盘中触板但收盘未封板不排除；非 ST 使用数据源股票名称中的 ST / 退市标识。",
            "缺少成交量、停牌或一字行情导致窗口证据不完整时排除，不用成交额推算成交量。",
            "这是历史条件筛选，不代表未来涨幅或收益预测。",
        ),
    )
