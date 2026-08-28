import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TRADEX_TOOLS = {
    "get_data_source_health",
    "list_all_tools",
    "search_stock",
    "get_realtime_quote",
    "get_historical_price",
    "get_market_overview",
    "get_index_volume_compare",
    "get_money_flow",
}


def test_codex_project_tool_allowlist() -> None:
    with (PROJECT_ROOT / ".codex" / "config.toml").open("rb") as config_file:
        config = tomllib.load(config_file)

    mcp_servers = config["mcp_servers"]
    enabled_servers = {
        name for name, server in mcp_servers.items() if server.get("enabled") is True
    }
    assert enabled_servers == {"tradex"}
    assert set(mcp_servers["tradex"]["enabled_tools"]) == EXPECTED_TRADEX_TOOLS

    plugins = config["plugins"]
    assert plugins
    assert all(plugin.get("enabled") is False for plugin in plugins.values())

    assert mcp_servers["lmgamedev-workhub"]["enabled"] is False
    assert mcp_servers["MCP_DOCKER"]["enabled"] is False
    assert mcp_servers["node_repl"]["enabled"] is False
