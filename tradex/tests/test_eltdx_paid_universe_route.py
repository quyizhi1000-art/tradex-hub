from __future__ import annotations

import json
from unittest.mock import MagicMock

import pandas as pd


async def test_a_share_security_codes_use_paid_all_a_shares_route(monkeypatch) -> None:
    from mcp.server.fastmcp import FastMCP
    from tradex.tools import eltdx_data

    frame = pd.DataFrame(
        [
            {"代码": "sh600000", "名称": "浦发银行", "市场": "sh", "纯代码": "600000"},
            {"代码": "sz000001", "名称": "平安银行", "市场": "sz", "纯代码": "000001"},
        ]
    )
    router = MagicMock()
    router.route.return_value = (frame, "biying")
    monkeypatch.setattr(eltdx_data, "_router", router)

    mcp = FastMCP("test")
    eltdx_data.register(mcp)
    fn = mcp._tool_manager._tools["eltdx_get_security_codes"].fn
    result = json.loads(await fn(market="sh", category="a_share"))

    router.route.assert_called_once_with("all_a_shares")
    assert result["status"] == "success"
    assert result["data"]["source"] == "biying"
    assert result["data"]["count"] == 1
    assert result["data"]["items"] == [
        {
            "code": "sh600000",
            "name": "浦发银行",
            "category": "a_share",
            "board": "",
        }
    ]
