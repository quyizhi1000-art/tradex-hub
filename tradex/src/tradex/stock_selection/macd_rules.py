"""Shared daily/intraday MACD rule on consecutive verified trading sessions."""


def recent_macd_cross_index(spreads):
    """Latest <=0 to >0 crossing at T, T-1 or T-2, while still above DEA."""
    if len(spreads) < 4 or spreads[-1] <= 0:
        return None
    for index in range(len(spreads)-1, len(spreads)-4, -1):
        if spreads[index-1] <= 0 < spreads[index]:
            return index
    return None


def v4_signal(points, *, max_j_lead=1):
    """Disjoint confirmed/early signals, using only information through T."""
    if max_j_lead not in (1, 2):
        raise ValueError("unsupported J lead window")
    if len(points) < max_j_lead + 3:
        return None, None
    js = [p.j for p in points]
    if js[-1] <= js[-2]:
        return None, None
    turns = [i for i in range(len(js)-1-max_j_lead, len(js))
             if js[i-2] >= js[i-1] and js[i] > js[i-1]]
    if not turns:
        return None, None
    spreads = [p.dif-p.dea for p in points[-3:]]
    if spreads[-2] <= 0 < spreads[-1]:
        return "confirmed", turns[-1]
    if (spreads[0] < spreads[1] < spreads[2] <= 0
            and points[-1].dif > points[-2].dif):
        return "pending_cross", turns[-1]
    return None, None


def price_filter_evidence(series, *, dates, price, volume_ratio, current_high=None,
                          previous_close=None):
    """Exact 60-session QFQ range, inclusive MA5, strict drawdown boundary."""
    import math
    from decimal import Decimal
    if series is None:
        return None, "price_history_unavailable"
    expected = dates[:-1] if current_high is not None else dates
    if (series.adjustment != "forward" or series.period != "daily"
            or tuple(b.trading_date for b in series.bars) != tuple(expected)):
        return None, "incomplete_60_session_history"
    if volume_ratio is None or not math.isfinite(volume_ratio) or volume_ratio < 0:
        return None, "volume_ratio_unavailable"
    if volume_ratio > 1.5:
        return None, "volume_ratio_above_1_5"
    anchor = previous_close if current_high is not None else price
    if anchor is None or abs(series.bars[-1].close-anchor) > .011:
        return None, "price_history_basis_mismatch"
    highs = [b.high for b in series.bars]
    closes = [b.close for b in series.bars]
    if current_high is not None:
        highs.append(current_high)
        closes.append(price)
    high = max(highs)
    dec = lambda v: Decimal(str(v))
    ma5 = sum(map(dec, closes[-5:])) / 5
    if dec(price) < ma5:
        return None, "price_below_ma5"
    if dec(price) >= dec(high) * Decimal("0.8"):
        return None, "drawdown_not_below_minus_20"
    return dict(high_60=high, high_60_date=dates[highs.index(high)], ma5=float(ma5),
                drawdown_60_pct=float((dec(price)/dec(high)-1)*100),
                volume_ratio=volume_ratio, price_window_start=dates[0],
                price_window_end=dates[-1]), None
