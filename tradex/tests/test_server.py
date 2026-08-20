"""
Tests for the MCP server setup and tool registration.
"""

import pytest


class TestServerSetup:
    def test_server_name(self, mcp_server):
        assert mcp_server.name == "tradex"

    def test_all_137_tools_registered(self, mcp_server):
        """当前服务注册 137 个 MCP 工具。"""
        tools = mcp_server._tool_manager._tools
        assert len(tools) == 137, f"Expected 137 tools, got {len(tools)}"

    def test_read_only_lake_tools_present(self, mcp_server):
        tools = mcp_server._tool_manager._tools
        expected = {
            "get_data_lake_status",
            "get_lake_daily_bars",
            "replay_lake_capture",
        }
        assert expected <= set(tools)

    def test_v01_tools_present(self, mcp_server):
        """V0.1 company info + price data tools (8 tools)."""
        tools = mcp_server._tool_manager._tools
        v01_tools = [
            "search_stock",
            "get_company_info",
            "get_company_profile",
            "get_competitors",
            "get_realtime_quote",
            "get_historical_price",
            "get_market_capitalization",
            "get_stock_list",
        ]
        for tool_name in v01_tools:
            assert tool_name in tools, f"V0.1 tool '{tool_name}' not registered"

    def test_v02_tools_present(self, mcp_server):
        """V0.2 financial statements + valuation tools (12 tools)."""
        tools = mcp_server._tool_manager._tools
        v02_tools = [
            "get_income_statement",
            "get_balance_sheet",
            "get_cash_flow_statement",
            "get_financial_line_item",
            "get_financial_indicators",
            "get_growth_rates",
            "get_per_share_data",
            "get_segments_revenue",
            "get_valuation_metrics",
            "get_dividend_data",
            "get_institutional_holdings",
            "get_analyst_rating",
        ]
        for tool_name in v02_tools:
            assert tool_name in tools, f"V0.2 tool '{tool_name}' not registered"

    def test_v03_tools_present(self, mcp_server):
        """V0.3 industry + market + news tools (14 tools)."""
        tools = mcp_server._tool_manager._tools
        v03_tools = [
            "get_industry_list",
            "get_industry_stocks",
            "get_concept_list",
            "get_sector_fund_flow",
            "get_industry_pe",
            "get_market_overview",
            "get_money_flow",
            "get_north_bound_flow",
            "get_limit_up_down",
            "get_dragon_tiger",
            "get_stock_news",
            "get_financial_calendar",
            "get_company_announcements",
            "search_news",
        ]
        for tool_name in v03_tools:
            assert tool_name in tools, f"V0.3 tool '{tool_name}' not registered"

    def test_v04_tools_present(self, mcp_server):
        """V0.4 macro & FX tools (8 tools)."""
        tools = mcp_server._tool_manager._tools
        v04_tools = [
            "get_macro_gdp",
            "get_macro_cpi",
            "get_macro_pmi",
            "get_macro_money_supply",
            "get_fx_rate",
            "get_bond_yield_curve",
            "get_margin_trading",
            "get_insider_trading",
        ]
        for tool_name in v04_tools:
            assert tool_name in tools, f"V0.4 tool '{tool_name}' not registered"

    def test_v05_signal_data_tools_present(self, mcp_server):
        """V0.5 signal data tools (17 tools)."""
        tools = mcp_server._tool_manager._tools
        v05_tools = [
            "get_hot_stocks",
            "get_lockup_expiry",
            "get_concept_attribution",
            "get_profit_forecast",
            "get_technical_indicator",
            "list_technical_indicators",
            "get_northbound_flow_signal",
            "get_fund_flow_signal",
            "get_dragon_tiger_signal",
            "get_industry_comparison_signal",
            "get_etf_realtime_data",
            "get_etf_kline_data",
            "get_cb_realtime_data",
            "get_cb_value_analysis_data",
            "get_limit_up_board",
            "get_board_sentiment",
            "get_limit_up_insight",
        ]
        for tool_name in v05_tools:
            assert tool_name in tools, f"V0.5 tool '{tool_name}' not registered"

    def test_v06_eltdx_tools_present(self, mcp_server):
        """V0.6 eltdx tools (5 tools)."""
        tools = mcp_server._tool_manager._tools
        v06_tools = [
            "eltdx_get_auction",
            "eltdx_get_ticks",
            "eltdx_get_f10",
            "eltdx_get_minutes",
            "eltdx_get_kline",
        ]
        for tool_name in v06_tools:
            assert tool_name in tools, f"V0.6 tool '{tool_name}' not registered"

    def test_fuyao_tools_present(self, mcp_server):
        """同花顺扶摇新增能力均已暴露为 MCP 工具。"""
        tools = mcp_server._tool_manager._tools
        fuyao_tools = [
            "get_valuation_snapshot",
            "get_ths_index_catalog",
            "get_ths_index_constituents",
            "get_limit_up_ladder",
            "get_stock_anomaly_analysis",
        ]
        for tool_name in fuyao_tools:
            assert tool_name in tools, f"Fuyao tool '{tool_name}' not registered"

    def test_stock_list_uses_a_dedicated_complete_snapshot_route(self, mcp_server):
        """全市场列表不与字段较少的扶摇单标的行情混用。"""
        from astock_signals.smart_router import get_router

        entries = [
            entry
            for entry in get_router().get_registry_report()
            if entry["data_type"] == "stock_list"
        ]
        assert [(entry["source_name"], entry["priority"]) for entry in entries] == [
            ("akshare", 1)
        ]

    def test_fuyao_sources_follow_key_configuration(self, mcp_server):
        """有密钥时注册扶摇路由与可切换源，无密钥时不注入。"""
        from astock_signals.smart_router import get_router
        from tradex.data_sources.fuyao_client import is_configured

        entries = [
            entry
            for entry in get_router().get_registry_report()
            if entry["source_name"] == "ths_fuyao"
        ]
        if not is_configured():
            assert entries == []
            return

        assert {
            (entry["data_type"], entry["priority"])
            for entry in entries
        } == {
            ("realtime_quote", 50),
            ("historical_kline", 50),
            ("valuation_snapshot", 1),
            ("ths_index_catalog", 1),
            ("ths_index_constituents", 1),
            ("limit_up_ladder", 1),
            ("stock_anomaly_analysis", 1),
            ("dragon_tiger_market_day", 1),
            ("limit_up_board", 1),
            ("market_breadth", 1),
        }

    def test_dragon_tiger_contracts_use_separate_routes(self, mcp_server):
        from astock_signals.smart_router import get_router
        from tradex.data_sources.fuyao_client import is_configured

        report = get_router().get_registry_report()
        multi_day = [
            (entry["source_name"], entry["priority"])
            for entry in report
            if entry["data_type"] == "dragon_tiger"
        ]
        market_day = [
            (entry["source_name"], entry["priority"])
            for entry in report
            if entry["data_type"] == "dragon_tiger_market_day"
        ]

        assert multi_day == [("em_datacenter", 1), ("akshare", 100)]
        expected_market_day = [("akshare_exact_day", 100)]
        if is_configured():
            expected_market_day.insert(0, ("ths_fuyao", 1))
        assert market_day == expected_market_day

    def test_tool_count_per_version(self, mcp_server):
        """129 个既有工具 + 5 个同花顺扶摇 + 3 个只读湖工具。"""
        tools = mcp_server._tool_manager._tools
        assert len(tools) == 137
