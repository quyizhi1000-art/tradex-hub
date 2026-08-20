from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pandas as pd
import pytest
from mcp.server.fastmcp import FastMCP

from tradex.tools import fuyao_data
from tradex.utils.cache import TTL_COMPANY, TTL_REALTIME


_TOOL_NAMES = {
    "get_valuation_snapshot",
    "get_ths_index_catalog",
    "get_ths_index_constituents",
    "get_limit_up_ladder",
    "get_stock_anomaly_analysis",
}


def _registered_tools():
    mcp = FastMCP("fuyao-tools-test")
    fuyao_data.register(mcp)
    return mcp._tool_manager._tools


def _uncached(monkeypatch):
    fake_cache = MagicMock()
    fake_cache.get.return_value = None
    monkeypatch.setattr(fuyao_data, "cache", fake_cache)
    return fake_cache


def _call(tools, tool_name: str, **kwargs):
    return asyncio.run(tools[tool_name].fn(**kwargs))


def test_register_exposes_exactly_five_fuyao_capability_tools():
    assert set(_registered_tools()) == _TOOL_NAMES


def test_get_valuation_snapshot_routes_serializes_and_uses_realtime_ttl(
    monkeypatch,
):
    tools = _registered_tools()
    frame = pd.DataFrame(
        [
            {
                "同花顺代码": "600519.SH",
                "代码": "600519",
                "名称": "贵州茅台",
                "市盈率TTM": 20.1,
            }
        ]
    )
    router = MagicMock()
    router.route.return_value = (frame, "ths_fuyao")
    monkeypatch.setattr(fuyao_data, "_router", router)
    fake_cache = _uncached(monkeypatch)

    result = _call(
        tools, "get_valuation_snapshot", symbols="600519,000001"
    )

    router.route.assert_called_once_with(
        "valuation_snapshot", symbols="600519,000001"
    )
    assert json.loads(result) == frame.to_dict("records")
    fake_cache.set.assert_called_once_with(
        "fuyao:valuation_snapshot:600519,000001",
        result,
        TTL_REALTIME,
    )


@pytest.mark.parametrize(
    ("tool_name", "kwargs", "data_type", "route_kwargs", "cache_key"),
    [
        (
            "get_ths_index_catalog",
            {"tag": "Industry"},
            "ths_index_catalog",
            {"tag": "industry"},
            "fuyao:ths_index_catalog:industry",
        ),
        (
            "get_ths_index_constituents",
            {"index_code": " 886042.ti "},
            "ths_index_constituents",
            {"index_code": "886042.TI"},
            "fuyao:ths_index_constituents:886042.TI",
        ),
    ],
)
def test_reference_tools_route_normalized_values_and_use_24h_ttl(
    monkeypatch,
    tool_name,
    kwargs,
    data_type,
    route_kwargs,
    cache_key,
):
    tools = _registered_tools()
    frame = pd.DataFrame([{"代码": "example", "名称": "示例"}])
    router = MagicMock()
    router.route.return_value = (frame, "ths_fuyao")
    monkeypatch.setattr(fuyao_data, "_router", router)
    fake_cache = _uncached(monkeypatch)

    result = _call(tools, tool_name, **kwargs)

    router.route.assert_called_once_with(data_type, **route_kwargs)
    assert json.loads(result) == frame.to_dict("records")
    fake_cache.set.assert_called_once_with(cache_key, result, TTL_COMPANY)
    assert TTL_COMPANY == 86_400


def test_get_limit_up_ladder_keeps_nested_payload_and_uses_realtime_ttl(
    monkeypatch,
):
    tools = _registered_tools()
    payload = {
        "timestamp": 1_784_275_991_000,
        "window": {
            "length": 30,
            "date_list": ["2026-08-19"],
            "board_caps": {"two_board": 4, "seven_over": 4},
        },
        "item": [
            {
                "date": "2026-08-19",
                "boards": {
                    "two_board": [
                        {
                            "thscode": "000001.SZ",
                            "seal_nextday": None,
                        }
                    ]
                },
            }
        ],
    }
    router = MagicMock()
    router.route.return_value = (payload, "ths_fuyao")
    monkeypatch.setattr(fuyao_data, "_router", router)
    fake_cache = _uncached(monkeypatch)

    result = _call(tools, "get_limit_up_ladder")

    router.route.assert_called_once_with("limit_up_ladder")
    assert json.loads(result) == payload
    fake_cache.set.assert_called_once_with(
        "fuyao:limit_up_ladder", result, TTL_REALTIME
    )


def test_get_stock_anomaly_analysis_preserves_documented_empty_result(
    monkeypatch,
):
    tools = _registered_tools()
    frame = pd.DataFrame(
        columns=["代码", "同花顺代码", "名称", "异动标签", "异动解读", "关键词"]
    )
    router = MagicMock()
    router.route.return_value = (frame, "ths_fuyao")
    monkeypatch.setattr(fuyao_data, "_router", router)
    fake_cache = _uncached(monkeypatch)

    result = _call(
        tools,
        "get_stock_anomaly_analysis",
        symbols="600519,000001",
    )

    router.route.assert_called_once_with(
        "stock_anomaly_analysis", symbols="600519,000001"
    )
    assert json.loads(result) == []
    fake_cache.set.assert_called_once_with(
        "fuyao:stock_anomaly_analysis:600519,000001",
        result,
        TTL_REALTIME,
    )


@pytest.mark.parametrize(
    ("tool_name", "kwargs"),
    [
        ("get_valuation_snapshot", {"symbols": "600519"}),
        ("get_ths_index_catalog", {"tag": "cn_concept"}),
        ("get_ths_index_constituents", {"index_code": "886042.TI"}),
        ("get_limit_up_ladder", {}),
        ("get_stock_anomaly_analysis", {"symbols": "600519"}),
    ],
)
def test_fuyao_tools_return_standard_error_json(
    monkeypatch,
    tool_name,
    kwargs,
):
    tools = _registered_tools()
    router = MagicMock()
    router.route.side_effect = RuntimeError("provider unavailable")
    monkeypatch.setattr(fuyao_data, "_router", router)
    _uncached(monkeypatch)

    result = json.loads(_call(tools, tool_name, **kwargs))

    assert result["error"] is True
    assert result["tool"] == tool_name
    assert "provider unavailable" in result["message"]


def test_dataframe_tool_rejects_wrong_router_return_type(monkeypatch):
    tools = _registered_tools()
    router = MagicMock()
    router.route.return_value = ({"item": []}, "ths_fuyao")
    monkeypatch.setattr(fuyao_data, "_router", router)
    _uncached(monkeypatch)

    result = json.loads(
        _call(tools, "get_valuation_snapshot", symbols="600519")
    )

    assert result["error"] is True
    assert result["tool"] == "get_valuation_snapshot"
    assert "类型无效" in result["message"]
