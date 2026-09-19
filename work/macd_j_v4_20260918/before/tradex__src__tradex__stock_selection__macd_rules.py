"""Shared daily/intraday MACD rule on consecutive verified trading sessions."""


def recent_macd_cross_index(spreads):
    """Latest <=0 to >0 crossing at T, T-1 or T-2, while still above DEA."""
    if len(spreads) < 4 or spreads[-1] <= 0:
        return None
    for index in range(len(spreads)-1, len(spreads)-4, -1):
        if spreads[index-1] <= 0 < spreads[index]:
            return index
    return None
