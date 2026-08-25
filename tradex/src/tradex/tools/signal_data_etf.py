"""
Signal Data — ETF 类子模块 (signal_data_etf)。

原 signal_data.py 拆分自 v3.0.0，本子模块承载 ETF 品种数据工具：
ETF 实时行情、ETF 历史 K 线。

V0.8 品种扩展层。

实时行情通过 provider-neutral ETF 网关；历史 K 线保留 SmartRouter 路由。

Tools (共 2 个):
  get_etf_realtime_data - ETF实时行情（AKShare fund_etf_spot_em）
  get_etf_kline_data    - ETF历史K线（AKShare fund_etf_hist_em）
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..data_sources import get_router
from ..data_gateway.etfs import etf_quotes_to_legacy_records, fetch_etf_quotes
from ..utils.cache import TTL_DAILY, TTL_REALTIME, cache
from ..utils.formatter import error_response, dict_to_json

_router = get_router()


def register(mcp: FastMCP):
    """Register signal data ETF tools with the MCP server."""

    # ----------------------------------------------------------------
    # V0.8: 品种扩展 — ETF
    # ----------------------------------------------------------------

    @mcp.tool()
    async def get_etf_realtime_data(
        top_n: int = 50,
        sort_by: str = "成交额",
    ) -> str:
        """
        获取ETF实时行情（全部ETF按成交额/涨跌幅排序）。

        数据源优先级：TuShare 付费 rt_etf_k → 规范化后备源。

        Args:
            top_n: 返回前N只ETF，默认50。
            sort_by: 排序字段，默认成交额，可选涨跌幅。

        Returns:
            ETF实时行情 (JSON)，含代码/名称/价格/涨跌幅/成交额和源时间。
        """
        cache_key = f"etf_realtime:{top_n}:{sort_by}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            series = fetch_etf_quotes(limit=top_n, router=_router)
            rows = etf_quotes_to_legacy_records(series)
            if sort_by == "涨跌幅":
                rows.sort(key=lambda row: row["change_pct"], reverse=True)
            result = {
                "source": series.metadata.provider,
                "data_type": "etf_realtime",
                "timestamp": (
                    series.metadata.provider_as_of.isoformat(timespec="seconds")
                    if series.metadata.provider_as_of
                    else None
                ),
                "etfs": rows,
            }
            output = dict_to_json(result)
            if result.get("etfs"):
                cache.set(cache_key, output, TTL_REALTIME)
            return output
        except Exception as e:
            return error_response(
                f"获取ETF实时行情失败: {e}", "get_etf_realtime_data"
            )

    @mcp.tool()
    async def get_etf_kline_data(
        symbol: str,
        period: str = "daily",
        start_date: str = "",
        end_date: str = "",
        adjust: str = "",
    ) -> str:
        """
        获取ETF历史K线数据。

        数据源：AKShare fund_etf_hist_em（东方财富）。
        支持日/周/月线，可选前复权/后复权/不复权。

        Args:
            symbol: ETF代码，如 "513500"。
            period: K线周期 daily/weekly/monthly，默认daily。
            start_date: 开始日期 YYYYMMDD，默认近1年。
            end_date: 结束日期 YYYYMMDD，默认今天。
            adjust: 复权方式（''不复权/'qfq'前复权/'hfq'后复权）。

        Returns:
            ETF K线数据 (JSON)，含日期/开高低收/成交量/成交额/涨跌幅。
        """
        cache_key = f"etf_kline:{symbol}:{period}:{start_date}:{end_date}:{adjust}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            # etf_data: symbol 非空 → get_etf_kline_json(symbol, period, ...)
            result, _src = _router.route(
                "etf_data",
                symbol=symbol,
                period=period,
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
            output = dict_to_json(result)
            if result.get("klines"):
                cache.set(cache_key, output, TTL_DAILY)
            return output
        except Exception as e:
            return error_response(
                f"获取ETF K线失败: {e}", "get_etf_kline_data"
            )
