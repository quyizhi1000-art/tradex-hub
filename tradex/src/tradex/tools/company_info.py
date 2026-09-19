"""
Category 1: Company Information & Search (V0.1)

Tools:
  1. search_stock       - Search A-share stocks by name or code
  2. get_company_info   - Get company basic info (industry, market cap, shares)
  3. get_company_profile - Get company business description & revenue breakdown
  4. get_competitors    - Get peer companies in the same industry

Data source routing (via SmartRouter):
  公司信息: akshare company_info (endpoints: code_name/individual_info/profile/industry_cons)
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from ..data_sources import get_router
from ..utils.cache import TTL_COMPANY, cache
from ..utils.formatter import df_to_json, dict_to_json, error_response, slim_df
from ..utils.symbol import normalize_symbol

_router = get_router()


def _instrument_id(symbol: str) -> str:
    if symbol.startswith("6"):
        return f"{symbol}.SH"
    if symbol.startswith(("4", "8", "9")):
        return f"{symbol}.BJ"
    return f"{symbol}.SZ"


def _relationship_payload(symbol: str) -> dict | None:
    from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader
    from tradex.smart_sector_library.catalog import SmartSectorCatalog

    with InstrumentTaxonomyReader() as reader:
        profile = reader.get(_instrument_id(symbol))
        status = reader.status()
    if profile is None:
        return None
    payload = profile.model_dump(mode="json")
    payload["catalog_revision"] = status.catalog_revision if status else None
    with SmartSectorCatalog() as sectors:
        membership = sectors.get(_instrument_id(symbol))
        payload["market_sector"] = membership.primary_sector_name
        payload["market_sector_status"] = membership.status
        payload["market_sector_revision"] = sectors.revision
    return payload


def register(mcp: FastMCP):
    """Register company information tools with the MCP server."""

    @mcp.tool()
    async def search_stock(keyword: str) -> str:
        """
        搜索A股股票，支持名称或代码模糊匹配。

        Args:
            keyword: 搜索关键词，可以是股票名称（如"贵州茅台"）或代码（如"600519"）

        Returns:
            匹配的股票列表 (JSON)，包含代码(code)和名称(name)字段，最多返回20条。
        """
        cache_key = f"search_stock:{keyword}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            df, _src = _router.route("company_info", endpoint="code_name")
            # Search in both code and name columns
            mask = df["code"].str.contains(keyword, case=False, na=False) | df[
                "name"
            ].str.contains(keyword, case=False, na=False)
            matched = df[mask].head(20)
            result = df_to_json(matched)
            cache.set(cache_key, result, TTL_COMPANY)
            return result
        except Exception as e:
            return error_response(f"搜索股票失败: {e}", "search_stock")

    @mcp.tool()
    async def get_company_info(symbol: str) -> str:
        """
        获取A股公司基本信息，包括行业、市值、股本等。

        Args:
            symbol: 6位股票代码，如 "000001"（平安银行）、"600519"（贵州茅台）

        Returns:
            公司基本信息 (JSON)，包含总市值、流通市值、行业、上市日期等。
        """
        symbol = normalize_symbol(symbol)
        cache_key = f"company_info:v2:{symbol}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            # Primary: 东方财富个股信息
            try:
                df, _src = _router.route(
                    "company_info", endpoint="individual_info", symbol=symbol
                )
                if df is not None and not df.empty:
                    info = {}
                    for _, row in df.iterrows():
                        info[row.iloc[0]] = row.iloc[1]
                    if info:
                        info["证券关系"] = _relationship_payload(symbol)
                        result = dict_to_json(info)
                        cache.set(cache_key, result, TTL_COMPANY)
                        return result
            except Exception:
                pass

            # Fallback: 从 A 股全量行情列表中提取基本信息
            df, _src = _router.route("stock_list", symbol="")
            code_col = _find_code_col(df)
            row = df[df[code_col].astype(str).str.strip() == symbol]
            if row.empty:
                return error_response(
                    f"未找到股票 {symbol} 的公司信息", "get_company_info"
                )
            result = dict_to_json({
                "relationship": _relationship_payload(symbol),
                "provider_info": json.loads(df_to_json(row)),
            })
            cache.set(cache_key, result, TTL_COMPANY)
            return result
        except Exception as e:
            return error_response(
                f"获取公司信息失败 ({symbol}): {e}", "get_company_info"
            )

    @mcp.tool()
    async def get_company_profile(symbol: str) -> str:
        """
        获取公司主营业务构成和业务描述。

        Args:
            symbol: 6位股票代码，如 "000001"（平安银行）

        Returns:
            公司主营业务构成 (JSON)，包含各业务的营收占比、毛利率等。
        """
        symbol = normalize_symbol(symbol)
        cache_key = f"company_profile:v2:{symbol}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            df, _src = _router.route(
                "company_info", endpoint="profile", symbol=symbol
            )
            result = dict_to_json({
                "relationship": _relationship_payload(symbol),
                "provider_profile": json.loads(df_to_json(df)),
            })
            cache.set(cache_key, result, TTL_COMPANY)
            return result
        except Exception as e:
            return error_response(
                f"获取公司主营构成失败 ({symbol}): {e}", "get_company_profile"
            )

    @mcp.tool()
    async def get_competitors(
        symbol: str,
        industry: str = "",
        peer_basis: str = "statistical_industry",
    ) -> str:
        """
        获取同行业公司列表（竞争对手/可比公司）。

        先根据股票代码查找所属行业板块，然后返回该板块的所有成分股。
        也可以直接传入行业名称来查询。

        Args:
            symbol: 6位股票代码，如 "600519"。如果同时提供了 industry 参数则忽略此参数。
            industry: 行业板块名称，如 "白酒"、"银行"。如果为空则自动从 symbol 推断。

        Returns:
            同行业公司列表 (JSON)，包含代码、名称、最新价、涨跌幅等。
        """
        symbol = normalize_symbol(symbol)
        cache_key = f"competitors:v2:{symbol}:{industry}:{peer_basis}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader

            if peer_basis not in {"statistical_industry", "primary_business"}:
                return error_response(
                    "peer_basis 只能是 statistical_industry 或 primary_business",
                    "get_competitors",
                )
            with InstrumentTaxonomyReader() as reader:
                profile = reader.get(_instrument_id(symbol))
                status = reader.status()
                if industry:
                    peers = (
                        reader.members_by_sw_l3(industry)
                        if peer_basis == "statistical_industry"
                        else reader.members_by_primary_business(industry)
                    )
                    basis_label = industry
                elif profile is not None and peer_basis == "statistical_industry" and profile.statistical_industry:
                    basis_label = profile.statistical_industry.level3_name or ""
                    peers = reader.members_by_sw_l3(
                        profile.statistical_industry.level3_code or basis_label
                    )
                elif profile is not None and peer_basis == "primary_business" and profile.primary_business_key:
                    basis_label = profile.primary_business_name or ""
                    peers = reader.members_by_primary_business(profile.primary_business_key)
                else:
                    peers = ()
                    basis_label = ""
            if peers:
                result = dict_to_json({
                    "contract": "stock_peer_group.v1",
                    "peer_basis": peer_basis,
                    "basis_label": basis_label,
                    "catalog_revision": status.catalog_revision if status else None,
                    "members": [
                        {
                            "instrument_id": item.instrument_id,
                            "code": item.instrument_id[:6],
                            "name": item.name,
                            "primary_business": item.primary_business_name,
                            "statistical_industry": (
                                item.statistical_industry.level3_name
                                if item.statistical_industry
                                else None
                            ),
                            "verification_status": item.verification_status,
                        }
                        for item in peers[:100]
                    ],
                })
                cache.set(cache_key, result, TTL_COMPANY)
                return result

            # If no industry provided, look it up from company info
            if not industry:
                try:
                    df, _src = _router.route(
                        "company_info", endpoint="individual_info", symbol=symbol
                    )
                    if df is not None and not df.empty:
                        for _, row in df.iterrows():
                            key = str(row.iloc[0])
                            if "行业" in key:
                                industry = str(row.iloc[1])
                                break
                except Exception:
                    pass

            # Fallback: try to find industry from board industry list
            if not industry:
                try:
                    board_df, _src = _router.route(
                        "industry_data", endpoint="board_industry_name_em"
                    )
                    if board_df is not None and not board_df.empty:
                        # look up stock in A-share spot to find the industry name
                        spot_df, _src2 = _router.route("stock_list", symbol="")
                        if spot_df is not None and not spot_df.empty:
                            code_col = _find_code_col(spot_df)
                            row = spot_df[spot_df[code_col].astype(str).str.strip() == symbol]
                            if not row.empty:
                                for c in row.columns:
                                    if "行业" in c or "板块" in c:
                                        industry = str(row.iloc[0][c])
                                        break
                except Exception:
                    pass

            if not industry:
                return error_response(
                    f"无法确定 {symbol} 所属行业，请手动传入 industry 参数",
                    "get_competitors",
                )

            # 东方财富行业成分股
            df, _src = _router.route(
                "industry_data", endpoint="board_industry_cons_em", industry=industry
            )
            df = slim_df(df)
            result = df_to_json(df, max_rows=30)
            cache.set(cache_key, result, TTL_COMPANY)
            return result
        except Exception as e:
            return error_response(
                f"获取竞争对手列表失败 ({symbol}, {industry}): {e}",
                "get_competitors",
            )

    @mcp.tool()
    async def get_stock_relationship_profile(symbol: str) -> str:
        """读取统一证券关系：主营、申万统计行业、概念关系和证据状态。"""

        symbol = normalize_symbol(symbol)
        payload = _relationship_payload(symbol)
        if payload is None:
            return error_response(
                f"{symbol} 的证券关系尚未入库或待刷新",
                "get_stock_relationship_profile",
            )
        return dict_to_json(payload)


def _find_code_col(df) -> str:
    """Find the stock code column in a DataFrame (varies by data source)."""
    for c in df.columns:
        if c in ("代码", "code", "symbol"):
            return c
        if "代码" in c or "code" in c.lower() or "symbol" in c.lower():
            return c
    return df.columns[0]
