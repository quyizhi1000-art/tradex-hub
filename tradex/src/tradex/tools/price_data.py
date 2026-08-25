"""
Category 2: Price & Quote Data (V0.1)

Tools:
  5. get_realtime_quote       - Real-time quote (A-share + global codes, v3.3.1)
  6. get_historical_price     - Historical OHLCV (daily/weekly/monthly)
  7. get_intraday_data        - Intraday minute data (today)
  8. get_market_capitalization - Total & free-float market cap
  9. get_stock_list            - Full A-share list with basic data

Data source routing (via SmartRouter):
  实时行情(A股): tushare(priority=1，配置后) → biying → ths_fuyao → eltdx → akshare → tencent_http
  实时行情(全球): tencent_http(priority=1)  [global_market_quote]
  历史K线:  tushare(priority=1，配置后) → biying → ths_fuyao → eltdx → akshare
  分时数据: tushare(rt_min_daily, priority=1，配置后) → eltdx
  股票列表: market_universe 规范网关 → tushare(rt_k，全市场) → akshare
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from ..data_sources import get_router
from ..data_gateway.intraday import (
    fetch_intraday_minute_series,
    intraday_minute_to_legacy_payload,
)
from ..data_gateway.market_universe import (
    a_share_universe_to_legacy_records,
    fetch_a_share_universe_snapshot,
)
from ..data_gateway.securities import (
    fetch_ohlcv_series,
    fetch_quote_snapshot,
    ohlcv_series_to_legacy_records,
    quote_snapshot_to_legacy_records,
)
from ..utils.cache import TTL_DAILY, TTL_REALTIME, cache
from ..utils.formatter import df_to_json, dict_to_json, error_response, slim_df
from ..utils.symbol import format_with_exchange, normalize_symbol

logger = logging.getLogger("tradex")

_router = get_router()


def register(mcp: FastMCP):
    """Register price data tools with the MCP server."""

    @mcp.tool()
    async def get_realtime_quote(symbol: str) -> str:
        """
        获取实时行情数据。支持A股6位代码和全球行情代码。

        **A股代码**：6位数字，如 "600519"（贵州茅台）、"000001"（平安银行）。

        **全球行情代码**：
        - 美股指数：usDJI（道琼斯）、usIXIC（纳斯达克）、usINX（标普500）
        - 热门美股：usNVDA（英伟达）、usTSLA（特斯拉）、usAAPL（苹果）、usMSFT（微软）、usAMZN（亚马逊）、usGOOGL（谷歌）、usMETA（Meta）、usMU（美光科技）、usAMAT（应用材料）
        - 亚太指数：hkHSI（恒生指数）、hkHSTECH（恒生科技）
        - 韩股：kr005930（三星电子）、kr000660（SK海力士）
        - 外汇：whDINIW（美元指数）

        Args:
            symbol: 股票代码，A股为6位数字，全球行情使用前缀代码

        Returns:
            实时行情数据 (JSON)，包含最新价、涨跌幅、成交量、成交额、
            最高价、最低价、开盘价、昨收价、换手率、市盈率、市净率等。
        """
        # 检测是否为全球行情代码
        if _is_global_code(symbol):
            cache_key = f"global_quote:{symbol}"
            cached = cache.get(cache_key)
            if cached is not None:
                return cached

            try:
                df, _src = _router.route("global_market_quote")
                if df is None or df.empty:
                    return error_response(
                        f"获取全球行情失败 ({symbol}): 数据源返回空数据", "get_realtime_quote"
                    )
                row = df[df["代码"] == symbol]
                if row.empty:
                    return error_response(
                        f"未找到代码 {symbol} 的全球行情", "get_realtime_quote"
                    )
                result = df_to_json(row)
                cache.set(cache_key, result, TTL_REALTIME)
                return result
            except Exception as e:
                return error_response(
                    f"获取全球行情失败 ({symbol}): {e}", "get_realtime_quote"
                )

        # A股代码处理
        symbol = normalize_symbol(symbol)
        cache_key = f"realtime_quote:v1:{symbol}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            snapshot = fetch_quote_snapshot(symbol, router=_router)
            if snapshot.metadata.quality.value != "accepted":
                logger.info(
                    "quote_snapshot.v1 degraded provider=%s flags=%s",
                    snapshot.metadata.provider,
                    ",".join(snapshot.metadata.quality_flags),
                )
            result = dict_to_json(quote_snapshot_to_legacy_records(snapshot))
            cache.set(cache_key, result, TTL_REALTIME)
            return result
        except Exception as e:
            return error_response(
                f"获取实时行情失败 ({symbol}): {e}", "get_realtime_quote"
            )

    @mcp.tool()
    async def get_historical_price(
        symbol: str,
        period: str = "daily",
        start_date: str = "",
        end_date: str = "",
        adjust: str = "qfq",
    ) -> str:
        """
        获取A股股票历史K线数据 (OHLCV)。

        Args:
            symbol: 6位股票代码，如 "600519"
            period: K线周期，可选 "daily"（日线）, "weekly"（周线）, "monthly"（月线）
            start_date: 开始日期，格式 "YYYYMMDD"，如 "20240101"。默认为空返回所有数据。
            end_date: 结束日期，格式 "YYYYMMDD"，如 "20241231"。默认为空返回至今。
            adjust: 复权类型，"qfq"（前复权）, "hfq"（后复权）, ""（不复权）

        Returns:
            K线数据 (JSON)，包含日期、开盘价、收盘价、最高价、最低价、
            成交量、成交额、振幅、涨跌幅、涨跌额、换手率。
        """
        symbol = normalize_symbol(symbol)
        cache_key = f"hist_price:v1:{symbol}:{period}:{start_date}:{end_date}:{adjust}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            series = fetch_ohlcv_series(
                symbol,
                period=period,
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
                router=_router,
            )
            if series.metadata.quality.value != "accepted":
                logger.info(
                    "ohlcv_bar.v1 degraded provider=%s flags=%s",
                    series.metadata.provider,
                    ",".join(series.metadata.quality_flags),
                )
            result = dict_to_json(ohlcv_series_to_legacy_records(series))
            cache.set(cache_key, result, TTL_DAILY)
            return result
        except Exception as e:
            return error_response(
                f"获取历史K线失败 ({symbol}): {e}", "get_historical_price"
            )

    @mcp.tool()
    async def get_intraday_data(symbol: str) -> str:
        """
        获取A股股票当日分时数据（1分钟K线）。

        返回当天从开盘到当前的每分钟价格、均价、成交量数据，
        可用于绘制分时图。

        Args:
            symbol: 6位股票代码，如 "600519"

        Returns:
            分时数据 (JSON)，包含时间、价格、均价、成交量。
        """
        symbol = normalize_symbol(symbol)
        try:
            series = fetch_intraday_minute_series(
                symbol,
                router=_router,
            )
            return dict_to_json(intraday_minute_to_legacy_payload(series))
        except Exception as e:
            return error_response(
                f"获取分时数据失败 ({symbol}): {e}", "get_intraday_data"
            )

    @mcp.tool()
    async def get_market_capitalization(symbol: str) -> str:
        """
        获取A股股票的总市值和流通市值。

        Args:
            symbol: 6位股票代码，如 "600519"

        Returns:
            市值数据 (JSON)，包含总市值、流通市值、最新价、成交量等关键字段。
        """
        symbol = normalize_symbol(symbol)
        cache_key = f"market_cap:{symbol}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        # === Tier 1: 个股基本信息（含市值） ===
        try:
            df, _src = _router.route(
                "company_info", endpoint="individual_info", symbol=symbol
            )
            if df is not None and not df.empty:
                info = {}
                for _, row in df.iterrows():
                    info[row.iloc[0]] = row.iloc[1]
                if any("市值" in str(k) for k in info):
                    result = dict_to_json(info)
                    cache.set(cache_key, result, TTL_REALTIME)
                    return result
        except Exception:
            pass

        # === Tier 2: 全量行情快照 ===
        try:
            df, _src = _router.route("realtime_quote", symbol=symbol)
            if df is not None and not df.empty:
                code_col = _find_code_col(df)
                if len(df) > 1:
                    row = df[df[code_col].astype(str).str.strip() == symbol]
                else:
                    row = df
                if not row.empty:
                    cap_keywords = ["代码", "名称", "最新价", "总市值", "流通市值",
                                    "涨跌幅", "成交量", "成交额", "市盈率", "市净率",
                                    "code", "name", "trade", "volume", "amount",
                                    "changepercent", "settlement", "mktcap"]
                    available_cols = [
                        c for c in row.columns
                        if any(k in c for k in cap_keywords)
                    ]
                    if not available_cols:
                        available_cols = list(row.columns)
                    result = df_to_json(row[available_cols])
                    cache.set(cache_key, result, TTL_REALTIME)
                    return result
        except Exception:
            pass

        return error_response(
            f"获取市值数据失败 ({symbol}): 所有数据源均不可用",
            "get_market_capitalization",
        )

    @mcp.tool()
    async def get_stock_list(
        min_market_cap: float = 0,
        max_results: int = 50,
    ) -> str:
        """
        获取A股完整股票列表，附带行情摘要信息。可按市值筛选。

        Args:
            min_market_cap: 最低总市值过滤（单位：亿元），默认0不过滤。如传入100则只返回市值>=100亿的股票。
            max_results: 最大返回条数，默认50。

        Returns:
            股票列表 (JSON)，包含代码、名称、最新价、涨跌幅、总市值、流通市值、
            成交量、成交额、市盈率、市净率等。
        """
        cache_key = f"stock_list:{min_market_cap}:{max_results}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            snapshot = fetch_a_share_universe_snapshot(
                require_market_cap=min_market_cap > 0,
                router=_router,
            )
            records = a_share_universe_to_legacy_records(snapshot)
            if min_market_cap > 0:
                threshold = min_market_cap * 1e8
                records = [
                    row
                    for row in records
                    if row["总市值"] is not None and row["总市值"] >= threshold
                ]
            records.sort(
                key=lambda row: row["总市值"] if row["总市值"] is not None else -1,
                reverse=True,
            )
            result = dict_to_json(records[:max_results])
            cache.set(cache_key, result, TTL_DAILY)
            return result
        except Exception as e:
            return error_response(f"获取股票列表失败: {e}", "get_stock_list")


def _is_global_code(symbol: str) -> bool:
    """检测是否为全球行情代码（非A股6位数字代码）。"""
    return symbol.startswith(("us", "hk", "kr", "wh", "int_", "hf_"))


def _find_code_col(df) -> str:
    """Find the stock code column in a DataFrame (varies by data source)."""
    for c in df.columns:
        if c in ("代码", "code", "symbol"):
            return c
        if "代码" in c or "code" in c.lower() or "symbol" in c.lower():
            return c
    return df.columns[0]
