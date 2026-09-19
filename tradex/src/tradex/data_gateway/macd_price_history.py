"""One cache owner for exact, completed QFQ price windows used by both screens."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import logging
import os
import uuid

from tradex.market_calendar import CalendarDayStatus, calendar_day_status
from .contracts import OHLCVSeriesV1
from .securities import fetch_ohlcv_series


def sixty_sessions(day):
    dates = []
    for _ in range(150):
        status = calendar_day_status(day)
        if status is CalendarDayStatus.UNVERIFIED:
            raise ValueError("60-session calendar unavailable")
        if status is CalendarDayStatus.VERIFIED_TRADING_DAY:
            dates.append(day)
            if len(dates) == 60:
                return tuple(reversed(dates))
        day -= timedelta(days=1)
    raise ValueError("60 verified sessions unavailable")


def fetch_macd_price_histories(instruments, day, *, intraday=False, root=None, loader=None):
    dates = sixty_sessions(day)
    expected = dates[:-1] if intraday else dates
    root = Path(root) if root is not None else Path.home()/".tradex"/"macd_price_history"
    loader = loader or fetch_ohlcv_series

    def one(instrument):
        path = root / f"{expected[0]}_{expected[-1]}" / f"{instrument}.json"
        try:
            try:
                series = OHLCVSeriesV1.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                series = loader(instrument, start_date=str(expected[0]),
                                end_date=str(expected[-1]), adjust="qfq")
            if (series.instrument_id != instrument or series.adjustment != "forward"
                    or series.period != "daily"
                    or tuple(b.trading_date for b in series.bars) != expected):
                raise ValueError("incomplete exact price window")
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
                tmp.write_text(series.model_dump_json(), encoding="utf-8")
                os.replace(tmp, path)
            return series
        except Exception as exc:
            logging.getLogger(__name__).warning("MACD price window unavailable for %s: %s", instrument, type(exc).__name__)
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        return tuple(s for s in pool.map(one, sorted(set(instruments))) if s is not None)
