"""Six exact-session indicator reads through the existing public router."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .providers.stock_technicals import map_stock_technical_day
from .stock_technicals_contracts import StockTechnicalWindowV1


def fetch_stock_technical_window(dates: tuple[date, ...], *, router=None,
                                 now: datetime | None = None) -> StockTechnicalWindowV1:
    if len(dates) != 6 or list(dates) != sorted(set(dates)):
        raise ValueError("six exact ordered trading sessions are required")
    if router is None:
        from tradex.data_sources import get_router, register_all_sources
        register_all_sources()
        router = get_router()
    fetched_at = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    days = []
    for day in dates:
        check_st = day == dates[-1]
        result, _provider = router.route_validated(
            "stock_selection_technicals",
            lambda raw, provider, day=day, check_st=check_st: map_stock_technical_day(
                raw, requested=day, provider=provider, fetched_at=fetched_at, check_st=check_st,
            ),
            trade_date=day.strftime("%Y%m%d"), check_st=check_st,
            deadline_seconds=90, provider_deadline_seconds=90,
        )
        days.append(result)
    return StockTechnicalWindowV1(days=tuple(days))
