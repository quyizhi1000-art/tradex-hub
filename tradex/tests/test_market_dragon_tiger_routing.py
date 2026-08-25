from __future__ import annotations

import asyncio
import json

import pandas as pd

from tradex.tools import market


class _CaptureMCP:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorate


class _NoCache:
    def get(self, key):
        return None

    def set(self, key, value, ttl):
        return None


def _registered_tool(monkeypatch, route):
    capture = _CaptureMCP()
    monkeypatch.setattr(market, "_router", type("Router", (), {"route": route})())
    monkeypatch.setattr(market, "cache", _NoCache())
    market.register(capture)
    return capture.tools["get_dragon_tiger"]


def test_single_day_market_view_uses_strict_fuyao_route(monkeypatch):
    calls = []

    def route(_self, data_type, **kwargs):
        calls.append((data_type, kwargs))
        return pd.DataFrame([{"代码": "600519"}]), "ths_fuyao"

    tool = _registered_tool(monkeypatch, route)
    asyncio.run(tool(num_days=1, trade_date="2026-08-19", board_type="all"))

    assert calls == [
        (
            "dragon_tiger_market_day",
            {
                "code": "",
                "trade_date": "2026-08-19",
                "board_type": "all",
                "look_back_days": 1,
            },
        )
    ]


def test_multi_day_view_never_enters_single_day_fuyao_contract(monkeypatch):
    calls = []

    def route(_self, data_type, **kwargs):
        calls.append((data_type, kwargs))
        return pd.DataFrame([{"代码": "600519"}]), "em_datacenter"

    tool = _registered_tool(monkeypatch, route)
    asyncio.run(tool(num_days=5, trade_date="2026-08-19"))

    assert calls == [
        (
            "dragon_tiger",
            {"code": "", "trade_date": "2026-08-19", "look_back_days": 10},
        )
    ]


def test_index_volume_compare_uses_the_deadline_aware_router(monkeypatch):
    calls = []

    def route(_self, data_type, **kwargs):
        calls.append((data_type, kwargs))
        return ([{"date": "2026-08-21", "volume": 100, "amount": 200}], "eastmoney")

    capture = _CaptureMCP()
    monkeypatch.setattr(market, "_router", type("Router", (), {"route": route})())
    monkeypatch.setattr(market, "cache", _NoCache())
    market.register(capture)

    payload = json.loads(asyncio.run(capture.tools["get_index_volume_compare"](days=3)))

    assert [call[0] for call in calls] == ["index_daily_amount"] * 3
    assert [call[1]["symbol"] for call in calls] == [
        "sh000001",
        "sz399001",
        "sz399006",
    ]
    assert all(call[1]["days"] == 3 for call in calls)
    assert payload["sh000001"]["series"][0]["amount_yuan"] == 200.0
