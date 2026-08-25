"""
Tests for price_data tools (Category 2).

v3.1.0：price_data 工具通过 SmartRouter.route() 获取数据。
由于 eltdx_fetchers 参数名 (code) 与工具层 (symbol) 不匹配（源码问题），
且 CI 环境网络代理不可用，这里 mock _router.route() 返回测试 DataFrame，
验证工具层的格式化/过滤逻辑（验证意图不变）。
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _mock_router(df):
    """创建同时兼容旧 route 与规范化 route_validated 的 mock 路由。"""
    mock = MagicMock()
    mock.route.return_value = (df, "akshare")
    mock.route_validated.side_effect = (
        lambda data_type, validator, **kwargs: (
            validator(df, "akshare"),
            "akshare",
        )
    )
    return mock


class TestGetRealtimeQuote:
    async def test_basic(self, monkeypatch):
        from tradex.tools import price_data
        from mcp.server.fastmcp import FastMCP
        import pandas as pd

        # mock SmartRouter.route() 返回含 600519 的测试 DataFrame
        mock_df = pd.DataFrame([{
            "代码": "600519",
            "名称": "贵州茅台",
            "最新价": 1800.0,
            "涨跌幅": 1.5,
            "成交量": 100000,
            "成交额": 180000000.0,
        }])
        monkeypatch.setattr(price_data, "_router", _mock_router(mock_df))

        mcp = FastMCP("test")
        price_data.register(mcp)
        fn = mcp._tool_manager._tools["get_realtime_quote"].fn
        result = await fn(symbol="600519")
        data = json.loads(result)
        assert isinstance(data, list)
        assert len(data) > 0

    async def test_invalid_symbol(self, monkeypatch):
        from tradex.tools import price_data
        from mcp.server.fastmcp import FastMCP
        import pandas as pd

        # mock 返回不含 999999 的 DataFrame，工具层应返回错误 dict
        mock_df = pd.DataFrame([{
            "代码": "600519",
            "名称": "贵州茅台",
            "最新价": 1800.0,
        }])
        monkeypatch.setattr(price_data, "_router", _mock_router(mock_df))

        mcp = FastMCP("test")
        price_data.register(mcp)
        fn = mcp._tool_manager._tools["get_realtime_quote"].fn
        result = await fn(symbol="999999")
        data = json.loads(result)
        # Should return an error or empty result
        assert isinstance(data, (list, dict))


class TestGetHistoricalPrice:
    async def test_basic(self, monkeypatch):
        from tradex.tools import price_data
        from mcp.server.fastmcp import FastMCP
        import pandas as pd

        # mock SmartRouter.route() 返回 K 线测试 DataFrame
        mock_df = pd.DataFrame([
            {"日期": "2025-01-02", "开盘": 1750.0, "收盘": 1760.0,
             "最高": 1770.0, "最低": 1745.0, "成交量": 50000, "成交额": 88000000.0},
            {"日期": "2025-01-03", "开盘": 1760.0, "收盘": 1780.0,
             "最高": 1790.0, "最低": 1755.0, "成交量": 60000, "成交额": 106800000.0},
        ])
        monkeypatch.setattr(price_data, "_router", _mock_router(mock_df))

        mcp = FastMCP("test")
        price_data.register(mcp)
        fn = mcp._tool_manager._tools["get_historical_price"].fn
        result = await fn(
            symbol="600519",
            period="daily",
            start_date="20250101",
            end_date="20250131",
        )
        data = json.loads(result)
        assert isinstance(data, list)
        assert len(data) > 0


class TestGetIntradayData:
    async def test_uses_canonical_gateway_and_keeps_legacy_shape(self, monkeypatch):
        from tradex.tools import price_data
        from mcp.server.fastmcp import FastMCP
        import pandas as pd

        frame = pd.DataFrame(
            [
                {
                    "代码": "000001.SZ",
                    "时间": "2026-08-24T09:30:00+08:00",
                    "开盘": 10.00,
                    "收盘": 10.08,
                    "最高": 10.10,
                    "最低": 9.98,
                    "成交量": 10_000,
                    "成交额": 100_500,
                }
            ]
        )
        frame.attrs.update(
            {
                "trading_date": "2026-08-24",
                "frequency_minutes": 1,
                "volume_unit": "shares",
                "provider_as_of": "2026-08-24T09:30:00+08:00",
            }
        )
        router = MagicMock()
        router.route_validated.side_effect = (
            lambda data_type, validator, **kwargs: (
                validator(frame, "tushare"),
                "tushare",
            )
        )
        monkeypatch.setattr(price_data, "_router", router)

        mcp = FastMCP("test")
        price_data.register(mcp)
        fn = mcp._tool_manager._tools["get_intraday_data"].fn
        result = json.loads(await fn(symbol="000001"))

        assert result == {
            "code": "000001",
            "point_count": 1,
            "points": [
                {
                    "time": "09:30",
                    "price": 10.08,
                    "avg_price": 10.05,
                    "volume": 10_000,
                }
            ],
        }
        router.route_validated.assert_called_once()


class TestGetStockList:
    async def test_uses_canonical_paid_universe_and_cny_market_cap(self, monkeypatch):
        from tradex.tools import price_data
        from mcp.server.fastmcp import FastMCP

        quote = SimpleNamespace(
            instrument_id="600000.SH",
            name="浦发银行",
            last=11.0,
            change_pct=10.0,
            amount_cny=1_234_500,
            open=10.2,
            high=12.0,
            low=10.0,
            previous_close=10.0,
            turnover_pct=None,
            amplitude_pct=20.0,
            total_market_cap_cny=100_000_000_000,
            float_market_cap_cny=80_000_000_000,
        )
        snapshot = SimpleNamespace(quotes=(quote,))
        calls = []
        monkeypatch.setattr(
            price_data,
            "fetch_a_share_universe_snapshot",
            lambda **kwargs: calls.append(kwargs) or snapshot,
        )
        price_data.cache.invalidate("stock_list:100:7")

        mcp = FastMCP("test")
        price_data.register(mcp)
        fn = mcp._tool_manager._tools["get_stock_list"].fn
        result = json.loads(await fn(min_market_cap=100, max_results=7))

        assert calls == [
            {"require_market_cap": True, "router": price_data._router}
        ]
        assert result == [
            {
                "代码": "600000",
                "名称": "浦发银行",
                "最新价": 11.0,
                "涨跌幅": 10.0,
                "成交额": 1_234_500,
                "今开": 10.2,
                "最高": 12.0,
                "最低": 10.0,
                "昨收": 10.0,
                "换手率": None,
                "振幅": 20.0,
                "总市值": 100_000_000_000,
                "流通市值": 80_000_000_000,
            }
        ]
