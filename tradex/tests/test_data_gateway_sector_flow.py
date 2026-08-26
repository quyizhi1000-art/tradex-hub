"""Canonical sector intraday fund-flow gateway tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradex.data_gateway import (
    SectorFundFlowBackfillCache,
    SectorFundFlowStore,
    fetch_sector_intraday_fund_flow,
    fetch_sector_intraday_fund_flow_backfill,
)
from tradex.data_sources.http_fetchers import (
    fetch_sector_intraday_fund_flow_eastmoney,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 8, 24)
NOW = datetime(2026, 8, 24, 10, 40, tzinfo=SHANGHAI)
TARGET = {
    "sector_key": "electric_power",
    "name": "电力",
    "taxonomy": "industry",
    "provider_sector_code": "BK0428",
}


def _frame(trading_date: date = TRADE_DATE) -> pd.DataFrame:
    start = datetime.combine(trading_date, datetime.min.time(), SHANGHAI).replace(
        hour=9,
        minute=31,
    )
    frame = pd.DataFrame(
        [
            {
                "provider_as_of": (start + timedelta(minutes=offset)).isoformat(),
                "main_net_inflow_cny": 100_000_000.0 + offset * 10_000_000.0,
            }
            for offset in range(6)
        ]
    )
    frame.attrs["provider_request_id"] = "fixture-request"
    return frame


class _Router:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail_first = fail_first

    def route_validated(self, data_type: str, validator, **kwargs: object):
        self.calls.append((data_type, kwargs))
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("fixture upstream unavailable")
        frame = _frame(kwargs["trade_date"])
        return validator(frame, "eastmoney"), "eastmoney"


class _Response:
    headers = {"x-request-id": "provider-fixture"}

    def __init__(self, klines: list[str]) -> None:
        self._klines = klines

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"data": {"klines": self._klines}}


def test_sector_flow_gateway_routes_and_validates_the_exact_contract():
    router = _Router()

    series = fetch_sector_intraday_fund_flow(
        **TARGET,
        trading_date=TRADE_DATE,
        now=NOW,
        router=router,
    )

    assert router.calls == [
        (
            "sector_intraday_fund_flow",
            {"provider_sector_code": "BK0428", "trade_date": TRADE_DATE},
        )
    ]
    assert series.metadata.contract == "sector_intraday_fund_flow.v1"
    assert series.metadata.provider == "eastmoney"
    assert series.metadata.provider_request_id == "fixture-request"
    assert series.trading_date == TRADE_DATE
    assert series.points[-1].cumulative_cny == 150_000_000.0


def test_eastmoney_fetcher_maps_exact_minutes_and_filters_other_dates(monkeypatch):
    captured = {}

    def em_get(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return _Response(
            [
                "2026-08-21 15:00,999,0,0,0,0,0",
                "2026-08-24 09:31,100000000,0,0,0,0,0",
                "2026-08-24 09:32,120000000,0,0,0,0,0",
            ]
        )

    monkeypatch.setattr("tradex.data_sources.em_client.em_get", em_get)

    frame = fetch_sector_intraday_fund_flow_eastmoney(
        provider_sector_code="BK0428",
        trade_date=TRADE_DATE,
    )

    assert captured["params"]["secid"] == "90.BK0428"
    assert captured["params"]["klt"] == 1
    assert frame["main_net_inflow_cny"].tolist() == [100_000_000.0, 120_000_000.0]
    assert frame.attrs["provider_sector_code"] == "BK0428"
    assert frame.attrs["provider_request_id"] == "provider-fixture"
    assert frame.attrs["provider_transport"] == "push2"


def test_eastmoney_fetcher_uses_delay_mirror_after_main_transport_failure(
    monkeypatch,
):
    calls = []

    def em_get(url, **_kwargs):
        calls.append(url)
        if "push2delay" not in url:
            raise RuntimeError("fixture main transport unavailable")
        return _Response(
            ["2026-08-24 09:31,100000000,0,0,0,0,0"]
        )

    monkeypatch.setattr("tradex.data_sources.em_client.em_get", em_get)

    frame = fetch_sector_intraday_fund_flow_eastmoney(
        provider_sector_code="BK0428",
        trade_date=TRADE_DATE,
    )

    assert calls == [
        "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
        "https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get",
    ]
    assert frame.attrs["provider_transport"] == "push2delay"


def test_eastmoney_fetcher_rejects_empty_main_before_using_delay_mirror(monkeypatch):
    calls = []

    def em_get(url, **_kwargs):
        calls.append(url)
        if "push2delay" not in url:
            return _Response(["2026-08-21 15:00,999,0,0,0,0,0"])
        return _Response(
            ["2026-08-24 09:31,100000000,0,0,0,0,0"]
        )

    monkeypatch.setattr("tradex.data_sources.em_client.em_get", em_get)

    frame = fetch_sector_intraday_fund_flow_eastmoney(
        provider_sector_code="BK0428",
        trade_date=TRADE_DATE,
    )

    assert len(calls) == 2
    assert frame.attrs["provider_transport"] == "push2delay"


def test_eastmoney_fetcher_rejects_semantically_empty_day(monkeypatch):
    monkeypatch.setattr(
        "tradex.data_sources.em_client.em_get",
        lambda *_args, **_kwargs: _Response(
            ["2026-08-21 15:00,999,0,0,0,0,0"]
        ),
    )

    with pytest.raises(RuntimeError, match="returned no sector minute fund flow"):
        fetch_sector_intraday_fund_flow_eastmoney(
            provider_sector_code="BK0428",
            trade_date=TRADE_DATE,
        )


def test_sector_flow_gateway_rejects_off_session_provider_points():
    frame = _frame()
    frame.loc[0, "provider_as_of"] = "2026-08-24T12:00:00+08:00"

    class Router:
        def route_validated(self, _data_type, validator, **_kwargs):
            return validator(frame, "eastmoney"), "eastmoney"

    with pytest.raises(ValueError, match="outside A-share sessions"):
        fetch_sector_intraday_fund_flow(
            **TARGET,
            trading_date=TRADE_DATE,
            now=NOW,
            router=Router(),
        )


def test_backfill_cache_is_success_only_and_read_paths_do_not_start_network_calls():
    cache = SectorFundFlowBackfillCache()
    router = _Router(fail_first=True)

    assert fetch_sector_intraday_fund_flow_backfill(
        (TARGET,),
        trading_date=TRADE_DATE,
        now=NOW,
        router=router,
        cache=cache,
        load_missing=False,
    ) == {}
    assert router.calls == []

    assert fetch_sector_intraday_fund_flow_backfill(
        (TARGET,),
        trading_date=TRADE_DATE,
        now=NOW,
        router=router,
        cache=cache,
        load_missing=True,
    ) == {}
    loaded = fetch_sector_intraday_fund_flow_backfill(
        (TARGET,),
        trading_date=TRADE_DATE,
        now=NOW,
        router=router,
        cache=cache,
        load_missing=True,
    )
    cached = fetch_sector_intraday_fund_flow_backfill(
        (TARGET,),
        trading_date=TRADE_DATE,
        now=NOW,
        router=router,
        cache=cache,
        load_missing=False,
    )

    assert len(router.calls) == 2
    assert loaded == cached
    assert len(loaded["electric_power"]) == 6
    assert loaded["electric_power"][0]["source_family"] == "eastmoney"


def test_backfill_success_survives_process_cache_restart(tmp_path):
    db_path = tmp_path / "sector-flow.sqlite3"
    first_router = _Router()
    with SectorFundFlowStore(db_path) as first_store:
        first_cache = SectorFundFlowBackfillCache(store=first_store)
        first = fetch_sector_intraday_fund_flow_backfill(
            (TARGET,),
            trading_date=TRADE_DATE,
            now=NOW,
            router=first_router,
            cache=first_cache,
            load_missing=True,
        )

    second_router = _Router()
    with SectorFundFlowStore(db_path) as second_store:
        second_cache = SectorFundFlowBackfillCache(store=second_store)
        restored = fetch_sector_intraday_fund_flow_backfill(
            (TARGET,),
            trading_date=TRADE_DATE,
            now=NOW,
            router=second_router,
            cache=second_cache,
            load_missing=False,
        )

    assert len(first_router.calls) == 1
    assert second_router.calls == []
    assert restored == first


def test_persistent_backfill_store_never_replaces_a_longer_curve_with_shorter_data(
    tmp_path,
):
    full = fetch_sector_intraday_fund_flow(
        **TARGET,
        trading_date=TRADE_DATE,
        now=NOW,
        router=_Router(),
    )
    short_points = full.points[:2]
    shorter = full.model_copy(
        update={
            "metadata": full.metadata.model_copy(
                update={
                    "provider_as_of": short_points[-1].provider_as_of,
                    "fetched_at": NOW + timedelta(minutes=1),
                }
            ),
            "points": short_points,
        }
    )

    with SectorFundFlowStore(tmp_path / "preserve.sqlite3") as store:
        assert store.record(full)["action"] == "inserted"
        preserved = store.record(shorter)
        restored = store.get_best(TRADE_DATE, TARGET["sector_key"])

    assert preserved["action"] == "preserved"
    assert restored is not None
    assert restored.points == full.points


def test_explicit_backfill_refresh_is_bounded_and_keeps_last_success_on_failure():
    cache = SectorFundFlowBackfillCache(refresh_min_interval_seconds=300)
    router = _Router()

    first = fetch_sector_intraday_fund_flow_backfill(
        (TARGET,),
        trading_date=TRADE_DATE,
        now=NOW,
        router=router,
        cache=cache,
        load_missing=True,
    )
    bounded = fetch_sector_intraday_fund_flow_backfill(
        (TARGET,),
        trading_date=TRADE_DATE,
        now=NOW + timedelta(minutes=1),
        router=router,
        cache=cache,
        refresh_existing=True,
    )
    router.fail_first = False
    original_route = router.route_validated

    def fail_refresh(*args, **kwargs):
        raise RuntimeError("refresh unavailable")

    router.route_validated = fail_refresh
    stale_success = fetch_sector_intraday_fund_flow_backfill(
        (TARGET,),
        trading_date=TRADE_DATE,
        now=NOW + timedelta(minutes=6),
        router=router,
        cache=cache,
        refresh_existing=True,
    )
    router.route_validated = original_route

    assert len(router.calls) == 1
    assert bounded == first
    assert stale_success == first


def test_collector_refresh_repairs_missing_curve_minutes_and_persists_them(
    tmp_path,
):
    class GrowingRouter:
        def __init__(self) -> None:
            self.calls = 0

        def route_validated(self, _data_type, validator, **kwargs):
            self.calls += 1
            frame = _frame(kwargs["trade_date"])
            if self.calls == 1:
                frame = frame.iloc[[0, 2]].reset_index(drop=True)
                frame.attrs["provider_request_id"] = "partial-curve"
            else:
                frame.attrs["provider_request_id"] = "repaired-curve"
            return validator(frame, "eastmoney"), "eastmoney"

    db_path = tmp_path / "gap-repair.sqlite3"
    router = GrowingRouter()
    with SectorFundFlowStore(db_path) as store:
        cache = SectorFundFlowBackfillCache(
            store=store,
            refresh_min_interval_seconds=0,
        )
        partial = fetch_sector_intraday_fund_flow_backfill(
            (TARGET,),
            trading_date=TRADE_DATE,
            now=NOW,
            router=router,
            cache=cache,
            load_missing=True,
        )
        repaired = fetch_sector_intraday_fund_flow_backfill(
            (TARGET,),
            trading_date=TRADE_DATE,
            now=NOW + timedelta(minutes=5),
            router=router,
            cache=cache,
            refresh_existing=True,
        )

    with SectorFundFlowStore(db_path) as reopened:
        restored = reopened.get_best(TRADE_DATE, TARGET["sector_key"])

    assert router.calls == 2
    assert len(partial[TARGET["sector_key"]]) == 2
    assert len(repaired[TARGET["sector_key"]]) == 6
    assert repaired[TARGET["sector_key"]][1]["provider_as_of"].minute == 32
    assert restored is not None
    assert len(restored.points) == 6
