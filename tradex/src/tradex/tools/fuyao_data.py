"""同花顺扶摇官方数据工具。

本模块只通过 SmartRouter 访问数据源，不直接持有密钥或发起 HTTP 请求。
行情快照和历史 K 线已作为现有工具的数据源接入；这里仅暴露项目原先
缺少、且扶摇接口契约足够完整的五项增量能力。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from mcp.server.fastmcp import FastMCP

from ..data_sources import get_router
from ..utils.cache import TTL_COMPANY, TTL_REALTIME, cache
from ..utils.formatter import df_to_json, dict_to_json, error_response


_router = get_router()


def _canonical_codes(value: str) -> str:
    """Build a stable cache suffix without changing fetcher validation."""
    return ",".join(part.strip().upper() for part in str(value or "").split(","))


def _dataframe_result(value: Any, *, context: str) -> pd.DataFrame:
    if not isinstance(value, pd.DataFrame):
        raise RuntimeError(f"{context} 数据源返回类型无效")
    return value


def register(mcp: FastMCP) -> None:
    """Register Fuyao-backed incremental tools."""

    @mcp.tool()
    async def get_valuation_snapshot(symbols: str) -> str:
        """
        批量获取 A 股最新估值快照。

        返回 PE(TTM/MRQ)、PB(MRQ)、PS(TTM) 和 PCF(TTM)，适合横向比较
        当前估值；这不是历史估值序列。

        Args:
            symbols: 一个或多个 A 股代码，逗号分隔，最多 100 个。例如
                ``600519,000001``，也接受完整 ``600519.SH`` 形式。
        """
        cache_key = f"fuyao:valuation_snapshot:{_canonical_codes(symbols)}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            value, _source = _router.route(
                "valuation_snapshot",
                symbols=symbols,
            )
            output = df_to_json(
                _dataframe_result(value, context="估值快照"),
                max_rows=100,
            )
            cache.set(cache_key, output, TTL_REALTIME)
            return output
        except Exception as exc:  # noqa: BLE001
            return error_response(
                f"获取同花顺估值快照失败: {exc}",
                "get_valuation_snapshot",
            )

    @mcp.tool()
    async def get_ths_index_catalog(tag: str = "cn_concept") -> str:
        """
        获取同花顺指数目录及稳定指数代码。

        Args:
            tag: 指数分类，可选 ``cn_concept``（概念）、``industry``（行业）、
                ``region``（地域）或 ``tszs``（特色指数）。
        """
        normalised_tag = str(tag or "cn_concept").strip().lower()
        cache_key = f"fuyao:ths_index_catalog:{normalised_tag}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            value, _source = _router.route(
                "ths_index_catalog",
                tag=normalised_tag,
            )
            output = df_to_json(
                _dataframe_result(value, context="同花顺指数目录")
            )
            cache.set(cache_key, output, TTL_COMPANY)
            return output
        except Exception as exc:  # noqa: BLE001
            return error_response(
                f"获取同花顺指数目录失败: {exc}",
                "get_ths_index_catalog",
            )

    @mcp.tool()
    async def get_ths_index_constituents(index_code: str) -> str:
        """
        获取同花顺指数或标准 A 股指数的当前成分股。

        Args:
            index_code: 完整指数代码，例如同花顺概念指数 ``886042.TI``，
                或标准指数 ``000300.SH``。
        """
        normalised_code = str(index_code or "").strip().upper()
        cache_key = f"fuyao:ths_index_constituents:{normalised_code}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            value, _source = _router.route(
                "ths_index_constituents",
                index_code=normalised_code,
            )
            output = df_to_json(
                _dataframe_result(value, context="同花顺指数成分股")
            )
            cache.set(cache_key, output, TTL_COMPANY)
            return output
        except Exception as exc:  # noqa: BLE001
            return error_response(
                f"获取同花顺指数成分股失败: {exc}",
                "get_ths_index_constituents",
            )

    @mcp.tool()
    async def get_limit_up_ladder() -> str:
        """
        获取近 30 个交易日的连板天梯。

        按二板、三板、四板、五板、六板和七板以上展示代表股票，弥补原有
        市场情绪工具中连板梯队为空的能力缺口。
        """
        cache_key = "fuyao:limit_up_ladder"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            value, _source = _router.route("limit_up_ladder")
            if not isinstance(value, dict):
                raise RuntimeError("连板天梯数据源返回类型无效")
            output = dict_to_json(value)
            cache.set(cache_key, output, TTL_REALTIME)
            return output
        except Exception as exc:  # noqa: BLE001
            return error_response(
                f"获取同花顺连板天梯失败: {exc}",
                "get_limit_up_ladder",
            )

    @mcp.tool()
    async def get_stock_anomaly_analysis(symbols: str) -> str:
        """
        批量获取当日 A 股异动原因和关键词。

        合法股票当天没有异动时返回空数组，而不是错误。

        Args:
            symbols: 一个或多个 A 股代码，逗号分隔，最多 50 个。
        """
        cache_key = f"fuyao:stock_anomaly_analysis:{_canonical_codes(symbols)}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            value, _source = _router.route(
                "stock_anomaly_analysis",
                symbols=symbols,
            )
            output = df_to_json(
                _dataframe_result(value, context="个股异动分析"),
                max_rows=50,
            )
            cache.set(cache_key, output, TTL_REALTIME)
            return output
        except Exception as exc:  # noqa: BLE001
            return error_response(
                f"获取同花顺个股异动分析失败: {exc}",
                "get_stock_anomaly_analysis",
            )
