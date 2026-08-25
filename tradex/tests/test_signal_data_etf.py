from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo


async def test_etf_realtime_tool_uses_canonical_paid_quote_gateway(monkeypatch) -> None:
    from mcp.server.fastmcp import FastMCP
    from tradex.tools import signal_data_etf

    as_of = datetime(2026, 8, 24, 14, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
    quotes = (
        SimpleNamespace(
            instrument_id="510300.SH",
            name="沪深300ETF",
            last=4.2,
            change_pct=0.12,
            amount_cny=33_600_000,
            provider_as_of=as_of,
            provider_variant="tushare",
        ),
        SimpleNamespace(
            instrument_id="159915.SZ",
            name="创业板ETF",
            last=2.1,
            change_pct=1.3,
            amount_cny=6_300_000,
            provider_as_of=as_of,
            provider_variant="tushare",
        ),
    )
    series = SimpleNamespace(
        metadata=SimpleNamespace(provider="tushare", provider_as_of=as_of),
        quotes=quotes,
    )
    calls = []
    monkeypatch.setattr(
        signal_data_etf,
        "fetch_etf_quotes",
        lambda **kwargs: calls.append(kwargs) or series,
    )
    signal_data_etf.cache.invalidate("etf_realtime:2:涨跌幅")

    mcp = FastMCP("test")
    signal_data_etf.register(mcp)
    fn = mcp._tool_manager._tools["get_etf_realtime_data"].fn
    result = json.loads(await fn(top_n=2, sort_by="涨跌幅"))

    assert calls == [{"limit": 2, "router": signal_data_etf._router}]
    assert result["source"] == "tushare"
    assert result["timestamp"] == "2026-08-24T14:59:00+08:00"
    assert [item["etf_code"] for item in result["etfs"]] == ["159915", "510300"]
    assert result["etfs"][0]["amount"] == 6_300_000
