"""Canonical sector intraday fund-flow gateway tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradex.data_gateway import (
    SectorFundFlowBackfillCache,
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
