"""
统一配置管理 — 支持环境变量覆盖，零配置可运行。

设计原则：
  1. 零配置可运行：所有配置有默认值，不配置 .env 也能启动
  2. 环境变量覆盖：通过 .env 文件或环境变量覆盖默认值
  3. 集中管理：所有配置项集中在此文件，便于维护和查找

Usage:
    from .config import config

    timeout = config.AKSHARE_TIMEOUT
    port = config.MCP_PORT
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
    # ``python -m tradex`` is often launched from the repository parent.  In
    # that case python-dotenv's cwd search misses tradex/.env, so load the
    # package project file as a non-overriding fallback.
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
except ImportError:
    # python-dotenv 未安装时忽略，直接读取环境变量
    pass


def _get_env(key: str, default: Any, cast: type = str) -> Any:
    """从环境变量读取配置值，支持类型转换。

    Args:
        key: 环境变量名
        default: 默认值
        cast: 目标类型（str/int/float/bool）

    Returns:
        转换后的配置值
    """
    value = os.getenv(key)
    if value is None:
        return default

    if cast is bool:
        return value.lower() in ("1", "true", "yes", "on")
    if cast is int:
        try:
            return int(value)
        except ValueError:
            return default
    if cast is float:
        try:
            return float(value)
        except ValueError:
            return default
    return value


class Config:
    """全局配置。

    所有配置项有默认值，可通过环境变量或 .env 文件覆盖。
    """

    # ── 数据源 ──────────────────────────────────────────────
    AKSHARE_TIMEOUT: int = _get_env("AKSHARE_TIMEOUT", 30, int)
    """AKShare 请求超时时间（秒）"""

    TENCENT_TIMEOUT: int = _get_env("TENCENT_TIMEOUT", 10, int)
    """腾讯行情接口超时时间（秒）"""

    ELTDX_SERVER: str = _get_env("ELTDX_SERVER", "auto")
    """通达信服务器地址，auto 表示自动选择最优"""

    FUYAO_BASE_URL: str = _get_env(
        "FUYAO_BASE_URL", "https://fuyao.aicubes.cn"
    )
    """同花顺扶摇金融数据 REST API 根地址"""

    FUYAO_API_KEY: str = _get_env("FUYAO_API_KEY", "")
    """同花顺扶摇 API Key；优先于 FUYAO_API_KEY_FILE"""

    FUYAO_API_KEY_FILE: str = _get_env("FUYAO_API_KEY_FILE", "")
    """保存同花顺扶摇 API Key 的本地文件路径"""

    FUYAO_TIMEOUT: int = _get_env("FUYAO_TIMEOUT", 15, int)
    """同花顺扶摇 API 请求超时时间（秒）"""

    FUYAO_RATE_LIMIT_PER_MINUTE: int = _get_env(
        "FUYAO_RATE_LIMIT_PER_MINUTE", 300, int
    )
    """跨进程共享的扶摇本地安全预算；可按实际合同频次覆盖"""

    BIYING_ENABLED: bool = _get_env("BIYING_ENABLED", True, bool)
    """是否允许注册必盈数据源；可按能力独立回滚"""

    BIYING_BASE_URL: str = _get_env(
        "BIYING_BASE_URL", "https://api.biyingapi.com"
    )
    """必盈 API 根地址"""

    BIYING_LICENCE: str = _get_env("BIYING_LICENCE", "")
    """必盈许可证；优先于 BIYING_LICENCE_FILE"""

    BIYING_LICENCE_FILE: str = _get_env("BIYING_LICENCE_FILE", "")
    """保存必盈许可证的本地文件路径"""

    BIYING_TIMEOUT: int = _get_env("BIYING_TIMEOUT", 15, int)
    """必盈 API 请求超时时间（秒）"""

    BIYING_MAX_INFLIGHT: int = _get_env("BIYING_MAX_INFLIGHT", 4, int)
    """进程内必盈请求最大并发数"""

    BIYING_RATE_LIMIT_PER_MINUTE: int = _get_env(
        "BIYING_RATE_LIMIT_PER_MINUTE", 300, int
    )
    """跨进程共享的必盈普通接口每分钟请求上限"""

    BIYING_PRIMARY_CAPABILITIES: str = _get_env(
        "BIYING_PRIMARY_CAPABILITIES",
        (
            "realtime_quote,historical_kline,market_overview,"
            "index_daily_amount,all_a_shares,full_kline,adjusted_kline,"
            "company_info,financial_stmt,finance_report,valuation,"
            "dividend_financing,industry_data,concept_attribution,"
            "limit_up_board,fund_hold"
        ),
    )
    """以逗号分隔的必盈主源能力；移除单项即可独立回滚"""

    TUSHARE_ENABLED: bool = _get_env("TUSHARE_ENABLED", True, bool)
    """是否允许注册 Tushare 数据源；未配置 token 时不会注册"""

    TUSHARE_BASE_URL: str = _get_env(
        "TUSHARE_BASE_URL", "https://api.tushare.pro"
    )
    """Tushare Pro 兼容 HTTP API 根地址"""

    TUSHARE_TOKEN_FILE: str = _get_env("TUSHARE_TOKEN_FILE", "")
    """保存 Tushare token 的本地文件路径"""

    TUSHARE_TIMEOUT: int = _get_env("TUSHARE_TIMEOUT", 15, int)
    """Tushare HTTP 请求超时时间（秒）"""

    TUSHARE_MAX_INFLIGHT: int = _get_env("TUSHARE_MAX_INFLIGHT", 4, int)
    """进程内 Tushare 请求最大并发数"""

    TUSHARE_POINTS_RATE_LIMIT_PER_MINUTE: int = _get_env(
        "TUSHARE_POINTS_RATE_LIMIT_PER_MINUTE", 500, int
    )
    """15000 积分型接口的跨进程共享频次"""

    TUSHARE_REALTIME_DAILY_RATE_LIMIT_PER_MINUTE: int = _get_env(
        "TUSHARE_REALTIME_DAILY_RATE_LIMIT_PER_MINUTE", 50, int
    )
    """实时日线类独立权限的频次；不同产品使用独立共享桶"""

    TUSHARE_REALTIME_MINUTE_RATE_LIMIT_PER_MINUTE: int = _get_env(
        "TUSHARE_REALTIME_MINUTE_RATE_LIMIT_PER_MINUTE", 500, int
    )
    """实时分钟独立权限的跨进程共享频次"""

    TUSHARE_AUCTION_RATE_LIMIT_PER_MINUTE: int = _get_env(
        "TUSHARE_AUCTION_RATE_LIMIT_PER_MINUTE", 500, int
    )
    """集合竞价独立权限的跨进程共享频次"""

    TUSHARE_PRIMARY_CAPABILITIES: str = _get_env(
        "TUSHARE_PRIMARY_CAPABILITIES",
        (
            "realtime_quote,historical_kline,auction_data,market_universe,"
            "etf_quotes,stock_fund_flow,dragon_tiger_market_day,"
            "minute_data,stock_selection_calendar,stock_selection_daily,"
            "stock_selection_daily_basic,stock_selection_master,"
            "stock_selection_financial_period"
        ),
    )
    """已验证且可逐项回滚的 Tushare priority=1 能力"""

    # ── 智能路由 ────────────────────────────────────────────
    ROUTER_HEALTH_CHECK_INTERVAL: int = _get_env("ROUTER_HEALTH_CHECK_INTERVAL", 60, int)
    """健康检查间隔（秒）"""

    ROUTER_MIN_SCORE: float = _get_env("ROUTER_MIN_SCORE", 20.0, float)
    """数据源最低健康评分，低于此值不再选中"""

    ROUTER_RECOVERY_AMOUNT: float = _get_env("ROUTER_RECOVERY_AMOUNT", 10.0, float)
    """定时恢复评分的增量"""

    # ── MCP 服务 ────────────────────────────────────────────
    MCP_TRANSPORT: str = _get_env("MCP_TRANSPORT", "stdio")
    """MCP 传输模式：stdio | sse | http"""

    MCP_HOST: str = _get_env("MCP_HOST", "127.0.0.1")
    """MCP HTTP/SSE 模式监听地址。默认 127.0.0.1 仅本地访问，外网部署需显式设置 MCP_HOST=0.0.0.0"""

    MCP_PORT: int = _get_env("MCP_PORT", 8000, int)
    """MCP HTTP/SSE 模式监听端口"""

    # ── WebSocket 推送服务 ──────────────────────────────────
    WS_SERVER_ENABLED: bool = _get_env("WS_SERVER_ENABLED", "false").lower() == "true"
    """是否启用 WebSocket 实时推送服务（默认关闭）"""

    WS_PORT: int = int(_get_env("WS_PORT", "8765"))
    """WebSocket 服务监听端口"""

    WS_TOKEN: str = _get_env("WS_TOKEN", "")
    """WebSocket 客户端认证 token（空字符串表示不要求认证）"""

    # ── 缓存 ────────────────────────────────────────────────
    CACHE_MAX_SIZE: int = _get_env("CACHE_MAX_SIZE", 5000, int)
    """内存缓存最大条目数，0 表示无限"""

    CACHE_FILE_DIR: str = _get_env("CACHE_FILE_DIR", ".cache")
    """文件缓存目录路径"""

    CACHE_FILE_ENABLED: bool = _get_env("CACHE_FILE_ENABLED", True, bool)
    """是否启用文件缓存（长 TTL 数据持久化）"""

    # ── 技术分析 ────────────────────────────────────────────
    INDICATOR_LOOKBACK_DAYS: int = _get_env("INDICATOR_LOOKBACK_DAYS", 400, int)
    """技术指标计算回溯天数"""

    CHIP_DISTRIBUTION_BINS: int = _get_env("CHIP_DISTRIBUTION_BINS", 50, int)
    """筹码分布直方图分箱数"""

    # ── 日志 ────────────────────────────────────────────────
    LOG_LEVEL: str = _get_env("LOG_LEVEL", "INFO")
    """日志级别：DEBUG | INFO | WARNING | ERROR"""

    LOG_DIR: str = _get_env("LOG_DIR", "logs")
    """日志文件目录"""


# 全局配置单例
config = Config()
