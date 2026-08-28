"""
数据源注册中心 — register_all_sources()。

注册全部数据类型到 SmartRouter，按数据源矩阵定义优先级与独占标记。

核心数据源矩阵（其余独有数据类型在下方集中注册）：
  | data_type            | priority=1        | priority=50   | priority=100/200 | exclusive |
  |----------------------|-------------------|---------------|------------------|-----------|
  | realtime_quote       | tushare           | biying/ths_fuyao | eltdx/akshare/tencent_http |  |
  | stock_list           | akshare           |               |               |           |
  | market_universe      | tushare           |               | akshare       |           |
  | historical_kline     | tushare           | biying/ths_fuyao | eltdx/akshare  |           |
  | minute_data          | tushare           | eastmoney     | eltdx        |           |
  | minute_data_batch/partial | tushare (max 40) |            |              |           |
  | call_auction         | eltdx             |               |               | 是        |
  | auction_data         | tushare           | eltdx         |               |           |
  | tick_data            | eltdx             |               |               | 是        |
  | f10_profile          | eltdx             |               |               | 是        |
  | company_info         | biying            |               | akshare       |           |
  | financial_stmt       | biying            |               | akshare       |           |
  | valuation            | biying            |               | akshare       |           |
  | industry_data        | biying            |               | akshare       |           |
  | market_overview      | biying            |               | tencent_http/akshare |      |
  | index_intraday_amount | eastmoney        |               |               |           |
  | index_intraday_series | eastmoney        |               |               |           |
  | sector_intraday_fund_flow | (exact paid unavailable) |   | eastmoney |               |
  | news_data            | em_news_direct    | akshare       |               |           |
  | telegraph_news       | cls_telegraph     |               |               |           |
  | cninfo_announcement  | cninfo_direct     |               |               |           |
  | macro_data           | akshare           |               |               |           |
  | etf_data             | astock_signals    |               |               |           |
  | etf_quotes           | tushare           |               | astock_signals |          |
  | cb_data              | akshare           |               |               |           |
  | fund_flow            | em_push2          | akshare       |               |           |
  | stock_fund_flow_day  | tushare           |               |               |           |
  | dragon_tiger         | em_datacenter     |               | akshare          |           |
  | dragon_tiger_market_day | tushare         | ths_fuyao     | akshare(exact day) |         |
  | industry_comparison  | em_push2          | akshare       |               |           |
  | northbound           | ths_hsgt          | akshare       |               |           |
  | hot_money            | ths_editorial     |               |               | 是        |
  | lockup_expiry        | em_datacenter     |               |               | 是        |
  | limit_up_board       | biying            | ths_fuyao     | em_push2_clist   |           |
  | limit_events         | ths               |               |                  |           |
  | limit_event_status   | ths               |               |                  |           |
  | market_breadth       | ths_fuyao         |               | em_push2ex       |           |
  | leader_quotes        | tencent_http      |               |                  |           |
  | stock_sector_profiles | em_push2delay    |               |                  |           |
  | board_leaders        | eastmoney         |               |                  |           |
  | hot_stocks           | akshare           |               |               |           |
  | profit_forecast      | akshare           | tencent_http  |               |           |
  | concept_attribution  | biying            |               | em_push2delay |           |
  | baidu_economic_calendar | akshare_baidu_economic |           |               |           |
  | baidu_trade_notify   | akshare_baidu_notify |             |               |           |
  | index_news_sentiment | akshare_index_sentiment |           |               |           |
  | futures_news         | akshare_futures_news |             |               |           |
  | sina_finance_news    | sina_direct       |               |               |           |
  | hot_search           | akshare_hot_search |               |               |           |
  | hot_rank             | akshare_hot_rank   |               |               |           |
  | xueqiu_hot           | akshare_xueqiu_hot |               |               |           |
  | fund_hold            | biying             |               | akshare_fund_hold |        |
  | wencai_query         | pywencai           |               |               |           |
  | wencai_news          | iwencai_openapi    |               |               |           |
"""

from __future__ import annotations

import logging
import threading

from astock_signals.smart_router import get_router

from . import akshare_fetchers as akf
from . import eltdx_fetchers as ef
from . import http_fetchers as hf
from . import news_fetchers as nf
from . import astock_signals_fetchers as asf
from . import wencai_fetchers as wf
from . import em_client as emc
from . import fuyao_fetchers as ff
from .fuyao_client import is_configured as fuyao_is_configured
from . import biying_fetchers as bf
from .biying_client import provides as biying_provides
from . import tushare_fetchers as tsf
from .tushare_client import provides as tushare_provides
from . import ths_fetchers as ths
from . import tdx_local as tdx

logger = logging.getLogger("tradex.data_sources")

_registered = False
_registration_lock = threading.Lock()


def _register_all_sources_unlocked() -> None:
    """注册全部数据类型到 SmartRouter 全局单例。

    幂等：重复调用不会重复注册。
    """
    global _registered
    if _registered:
        logger.debug("register_all_sources: already registered, skip")
        return
    router = get_router()

    # ── 行情类：Tushare 付费主源 → 必盈/Fuyao → 原有源 ──
    tushare_realtime = tushare_provides("realtime_quote")
    if tushare_realtime:
        router.register(
            "realtime_quote", "tushare", tsf.fetch_realtime_quote, priority=1
        )
    biying_realtime = biying_provides("realtime_quote")
    if biying_realtime:
        router.register(
            "realtime_quote",
            "biying",
            bf.fetch_realtime_quote,
            priority=25 if tushare_realtime else 1,
        )
    if fuyao_is_configured():
        router.register(
            "realtime_quote", "ths_fuyao", ff.fetch_realtime_quote, priority=50
        )
    paid_realtime = tushare_realtime or biying_realtime
    router.register("realtime_quote", "eltdx", ef.fetch_realtime_quote, priority=100 if paid_realtime else 1)
    router.register("realtime_quote", "akshare", akf.fetch_realtime_quote, priority=200 if paid_realtime else 100)
    router.register("realtime_quote", "tencent_http", hf.fetch_realtime_quote_tencent, priority=300 if paid_realtime else 200)

    # 全市场列表需要名称、行业和市值字段，不能与仅含价格字段的单标的
    # snapshot 共用路由，否则上游成功但字段不完整时不会触发 fallback。
    router.register("stock_list", "akshare", akf.fetch_realtime_quote, priority=1)
    tushare_universe = tushare_provides("market_universe")
    if tushare_universe:
        router.register(
            "market_universe", "tushare", tsf.fetch_market_universe, priority=1
        )
    router.register(
        "market_universe",
        "akshare",
        akf.fetch_realtime_quote,
        priority=100 if tushare_universe else 1,
    )

    tushare_history = tushare_provides("historical_kline")
    if tushare_history:
        router.register(
            "historical_kline", "tushare", tsf.fetch_historical_kline, priority=1
        )
    for capability, fetcher in (
        ("stock_selection_calendar", tsf.fetch_stock_selection_calendar),
        ("stock_selection_daily", tsf.fetch_stock_selection_daily),
        ("stock_selection_daily_basic", tsf.fetch_stock_selection_daily_basic),
        ("stock_selection_master", tsf.fetch_stock_selection_master),
        (
            "stock_selection_financial_period",
            tsf.fetch_stock_selection_financial_period,
        ),
        ("instrument_taxonomy", tsf.fetch_instrument_taxonomy_source),
    ):
        if tushare_provides(capability):
            router.register(
                capability,
                "tushare",
                fetcher,
                priority=1,
                exclusive=True,
            )
    biying_history = biying_provides("historical_kline")
    if biying_history:
        router.register(
            "historical_kline",
            "biying",
            bf.fetch_historical_kline,
            priority=25 if tushare_history else 1,
        )
    if fuyao_is_configured():
        router.register(
            "historical_kline", "ths_fuyao", ff.fetch_historical_kline, priority=50
        )
    paid_history = tushare_history or biying_history
    router.register("historical_kline", "eltdx", ef.fetch_historical_kline, priority=100 if paid_history else 1)
    router.register("historical_kline", "akshare", akf.fetch_historical_kline, priority=200 if paid_history else 100)

    # ── 同花顺扶摇官方独有能力 ──
    # 每项使用独立 data_type，避免某个不支持的接口影响其他扶摇能力的
    # SmartRouter 健康分。未配置密钥时完全不注册，现有工具仍可正常启动。
    if biying_provides("valuation_snapshot"):
        router.register(
            "valuation_snapshot", "biying", bf.fetch_valuation_snapshot, priority=1
        )
    if biying_provides("ths_index_catalog"):
        router.register(
            "ths_index_catalog", "biying", bf.fetch_ths_index_catalog, priority=1
        )
    if biying_provides("ths_index_constituents"):
        router.register(
            "ths_index_constituents", "biying", bf.fetch_ths_index_constituents, priority=1
        )
    if fuyao_is_configured():
        fuyao_capability_priority = 50 if biying_provides("valuation_snapshot") else 1
        router.register(
            "valuation_snapshot",
            "ths_fuyao",
            ff.fetch_valuation_snapshot,
            priority=fuyao_capability_priority,
        )
        router.register(
            "ths_index_catalog",
            "ths_fuyao",
            ff.fetch_ths_index_catalog,
            priority=50 if biying_provides("ths_index_catalog") else 1,
        )
        router.register(
            "ths_index_constituents",
            "ths_fuyao",
            ff.fetch_ths_index_constituents,
            priority=50 if biying_provides("ths_index_constituents") else 1,
        )
        router.register(
            "limit_up_ladder",
            "ths_fuyao",
            ff.fetch_limit_up_ladder,
            priority=1,
        )
        router.register(
            "stock_anomaly_analysis",
            "ths_fuyao",
            ff.fetch_stock_anomaly_analysis,
            priority=1,
        )

    tushare_minutes = tushare_provides("minute_data")
    if tushare_minutes:
        router.register(
            "minute_data", "tushare", tsf.fetch_minute_data, priority=1
        )
        router.register(
            "minute_data_batch",
            "tushare",
            tsf.fetch_minute_data_batch,
            priority=1,
        )
        router.register(
            "minute_data_batch_partial",
            "tushare",
            tsf.fetch_minute_data_batch_partial,
            priority=1,
        )
    router.register(
        "minute_data",
        "eastmoney",
        hf.fetch_minute_data_eastmoney,
        priority=50 if tushare_minutes else 1,
    )
    router.register(
        "minute_data",
        "eltdx",
        ef.fetch_minute_data,
        priority=100 if tushare_minutes else 1,
    )

    # ── eltdx 独占源 ──
    router.register("call_auction", "eltdx", ef.fetch_call_auction, priority=1, exclusive=True)
    router.register("tick_data", "eltdx", ef.fetch_tick_data, priority=1, exclusive=True)
    router.register("f10_profile", "eltdx", ef.fetch_f10_profile, priority=1, exclusive=True)
    router.register("security_codes", "eltdx", ef.fetch_security_codes, priority=1, exclusive=True)
    biying_all_a = biying_provides("all_a_shares")
    if biying_all_a:
        router.register("all_a_shares", "biying", bf.fetch_all_a_shares, priority=1)
    router.register("all_a_shares", "eltdx", ef.fetch_all_a_shares, priority=100 if biying_all_a else 1, exclusive=not biying_all_a)
    router.register("minute_history", "eltdx", ef.fetch_minute_history, priority=1, exclusive=True)
    router.register("minute_aux", "eltdx", ef.fetch_minute_aux, priority=1, exclusive=True)
    router.register("today_ticks", "eltdx", ef.fetch_today_ticks, priority=1, exclusive=True)
    router.register("opening_match", "eltdx", ef.fetch_opening_match, priority=1, exclusive=True)
    biying_full_kline = biying_provides("full_kline")
    if biying_full_kline:
        router.register("full_kline", "biying", bf.fetch_full_kline, priority=1)
    router.register("full_kline", "eltdx", ef.fetch_full_kline, priority=100 if biying_full_kline else 1, exclusive=not biying_full_kline)
    biying_adjusted = biying_provides("adjusted_kline")
    if biying_adjusted:
        router.register("adjusted_kline", "biying", bf.fetch_adjusted_kline, priority=1)
    router.register("adjusted_kline", "eltdx", ef.fetch_adjusted_kline, priority=100 if biying_adjusted else 1, exclusive=not biying_adjusted)
    router.register("stock_profile", "eltdx", ef.fetch_stock_profile, priority=1, exclusive=True)
    router.register("shortline_indicators", "eltdx", ef.fetch_shortline_indicators, priority=1, exclusive=True)
    router.register("finance_batch", "eltdx", ef.fetch_finance_batch, priority=1, exclusive=True)
    router.register("special_limits", "eltdx", ef.fetch_special_limits, priority=1, exclusive=True)
    biying_finance_report = biying_provides("finance_report")
    if biying_finance_report:
        router.register("finance_report", "biying", bf.fetch_finance_report, priority=1)
    router.register("finance_report", "eltdx", ef.fetch_finance_report, priority=100 if biying_finance_report else 1, exclusive=not biying_finance_report)
    biying_dividend = biying_provides("dividend_financing")
    if biying_dividend:
        router.register("dividend_financing", "biying", bf.fetch_dividend_financing, priority=1)
    router.register("dividend_financing", "eltdx", ef.fetch_dividend_financing, priority=100 if biying_dividend else 1, exclusive=not biying_dividend)
    router.register("company_news", "eltdx", ef.fetch_company_news, priority=1, exclusive=True)
    router.register("northbound_holding", "eltdx", ef.fetch_northbound_holding, priority=1, exclusive=True)
    router.register("stock_topics", "eltdx", ef.fetch_stock_topics, priority=1, exclusive=True)
    router.register("topic_stocks", "eltdx", ef.fetch_topic_stocks, priority=1, exclusive=True)
    tushare_auction = tushare_provides("auction_data")
    if tushare_auction:
        router.register(
            "auction_data", "tushare", tsf.fetch_auction_data, priority=1
        )
    router.register(
        "auction_data",
        "eltdx",
        ef.fetch_auction_data,
        priority=100 if tushare_auction else 1,
        exclusive=not tushare_auction,
    )
    router.register("category_quotes", "eltdx", ef.fetch_category_quotes, priority=1, exclusive=True)
    router.register("trading_day", "eltdx", ef.fetch_trading_day, priority=1, exclusive=True)
    router.register("opening_match_history", "eltdx", ef.fetch_opening_match_history, priority=1, exclusive=True)
    router.register("capital_changes", "eltdx", ef.fetch_capital_changes, priority=1, exclusive=True)
    router.register("special_limits_scan", "eltdx", ef.fetch_special_limits_scan, priority=1, exclusive=True)
    router.register("f10_extra", "eltdx", ef.fetch_f10_extra, priority=1, exclusive=True)

    # ── 基本面/板块/市场：按能力独立启停必盈 ──
    for data_type, fetcher in (
        ("company_info", bf.fetch_company_info),
        ("financial_stmt", bf.fetch_financial_stmt),
        ("valuation", bf.fetch_valuation),
        ("industry_data", bf.fetch_industry_data),
        ("market_overview", bf.fetch_market_overview),
        ("index_daily_amount", bf.fetch_index_daily_amount),
    ):
        if biying_provides(data_type):
            router.register(data_type, "biying", fetcher, priority=1)
    router.register("company_info", "akshare", akf.fetch_company_info, priority=100 if biying_provides("company_info") else 1)
    router.register("financial_stmt", "akshare", akf.fetch_financial_stmt, priority=100 if biying_provides("financial_stmt") else 1)
    router.register("valuation", "akshare", akf.fetch_valuation, priority=100 if biying_provides("valuation") else 1)
    router.register("industry_data", "akshare", akf.fetch_industry_data, priority=100 if biying_provides("industry_data") else 1)
    biying_market_overview = biying_provides("market_overview")
    router.register(
        "market_overview",
        "tencent_http",
        hf.fetch_market_overview_tencent,
        priority=100 if biying_market_overview else 1,
    )
    router.register(
        "market_overview",
        "akshare",
        akf.fetch_market_overview,
        priority=200 if biying_market_overview else 100,
    )
    router.register(
        "index_intraday_amount",
        "eastmoney",
        hf.fetch_index_intraday_amount_eastmoney,
        priority=1,
    )
    router.register(
        "index_intraday_series",
        "eastmoney",
        hf.fetch_index_intraday_series_eastmoney,
        priority=1,
    )
    # Exact-capability order is Tushare -> Fuyao -> free.  The currently
    # verified Tushare and Fuyao board-flow interfaces are daily-only, so they
    # are not registered under this minute contract.  Eastmoney is the first
    # eligible exact source; priority 100 keeps the paid-source tier reserved.
    router.register(
        "sector_intraday_fund_flow",
        "eastmoney",
        hf.fetch_sector_intraday_fund_flow_eastmoney,
        priority=100,
    )
    router.register("index_daily_amount", "akshare", akf.fetch_index_daily_amount, priority=100 if biying_provides("index_daily_amount") else 1)
    router.register("news_data", "em_news_direct", nf.fetch_em_news_direct, priority=1)
    router.register("news_data", "akshare", akf.fetch_news_data, priority=100)
    router.register("telegraph_news", "cls_telegraph", nf.fetch_cls_telegraph, priority=1)
    router.register("cninfo_announcement", "cninfo_direct", nf.fetch_cninfo_direct, priority=1)
    router.register("macro_data", "akshare", akf.fetch_macro_data, priority=1)
    router.register("etf_data", "astock_signals", asf.fetch_etf_data, priority=1)
    tushare_etfs = tushare_provides("etf_quotes")
    if tushare_etfs:
        router.register("etf_quotes", "tushare", tsf.fetch_etf_quotes, priority=1)
    router.register(
        "etf_quotes",
        "astock_signals",
        asf.fetch_etf_data,
        priority=100 if tushare_etfs else 1,
    )
    router.register("cb_data", "astock_signals", asf.fetch_cb_data, priority=1)
    router.register("hot_stocks", "akshare", akf.fetch_hot_stocks, priority=1)

    # ── 东财主 + akshare 备 ──
    router.register("fund_flow", "em_push2", asf.fetch_fund_flow_em, priority=1)
    router.register("fund_flow", "akshare", akf.fetch_fund_flow, priority=100)

    if tushare_provides("stock_fund_flow"):
        router.register(
            "stock_fund_flow_day",
            "tushare",
            tsf.fetch_stock_fund_flow,
            priority=1,
        )

    router.register("dragon_tiger", "em_datacenter", asf.fetch_dragon_tiger_em, priority=1)
    router.register("dragon_tiger", "akshare", akf.fetch_dragon_tiger, priority=100)

    # Single-day market list is a separate contract from per-stock, multi-day
    # seat details.  Splitting the route prevents a fallback from silently
    # ignoring board_type/look-back parameters and returning a different view.
    tushare_dragon_tiger = tushare_provides("dragon_tiger_market_day")
    if tushare_dragon_tiger:
        router.register(
            "dragon_tiger_market_day",
            "tushare",
            tsf.fetch_dragon_tiger_market_day,
            priority=1,
        )
    if biying_provides("dragon_tiger_market_day"):
        router.register(
            "dragon_tiger_market_day",
            "biying",
            bf.fetch_dragon_tiger,
            priority=25 if tushare_dragon_tiger else 1,
        )
    if fuyao_is_configured():
        router.register(
            "dragon_tiger_market_day",
            "ths_fuyao",
            ff.fetch_dragon_tiger,
            priority=(
                50
                if tushare_dragon_tiger
                or biying_provides("dragon_tiger_market_day")
                else 1
            ),
        )
    router.register(
        "dragon_tiger_market_day",
        "akshare_exact_day",
        akf.fetch_dragon_tiger_market_day,
        priority=100,
    )

    router.register("industry_comparison", "em_push2", asf.fetch_industry_comparison_em, priority=1)
    router.register("industry_comparison", "akshare", akf.fetch_industry_comparison, priority=100)

    # ── 同花顺主 + akshare 备 ──
    router.register("northbound", "ths_hsgt", asf.fetch_northbound_ths, priority=1)
    router.register("northbound", "akshare", akf.fetch_northbound, priority=100)

    # ── 独占源 ──
    router.register("hot_money", "ths_editorial", asf.fetch_hot_money, priority=1, exclusive=True)
    biying_lockup = biying_provides("lockup_expiry")
    if biying_lockup:
        router.register("lockup_expiry", "biying", bf.fetch_lockup_expiry, priority=1)
    router.register("lockup_expiry", "em_datacenter", asf.fetch_lockup_expiry, priority=100 if biying_lockup else 1, exclusive=not biying_lockup)
    if biying_provides("limit_up_board"):
        router.register(
            "limit_up_board", "biying", bf.fetch_limit_up_board, priority=1
        )
    if fuyao_is_configured():
        router.register(
            "limit_up_board", "ths_fuyao", ff.fetch_limit_up_board,
            priority=50 if biying_provides("limit_up_board") else 1
        )
    router.register(
        "limit_up_board", "em_push2_clist", asf.fetch_limit_up_board, priority=100
    )

    # ── akshare 主 + tencent_http 备 ──
    router.register("profit_forecast", "akshare", akf.fetch_profit_forecast, priority=1)
    router.register("profit_forecast", "tencent_http", hf.fetch_profit_forecast_tencent, priority=100)

    # ── 单源/主备 ──
    if biying_provides("concept_attribution"):
        router.register("concept_attribution", "biying", bf.fetch_concept_attribution, priority=1)
    router.register("concept_attribution", "em_push2delay", asf.fetch_concept_attribution, priority=100 if biying_provides("concept_attribution") else 1)

    # ── v3.3.1 新增：全局行情（腾讯直连，美股/大宗/亚太/外汇） ──
    router.register("global_market_quote", "tencent_http", hf.fetch_global_quote_tencent, priority=1)

    # ── v3.3.8 新增：市场级统计（实时涨跌家数 / 行业板块涨幅） ──
    # market_breadth：东财 push2ex 涨跌分布（实时）
    # industry_quotes：东财 push2 行业板块（内部自动降级 push2delay 镜像）
    if fuyao_is_configured():
        router.register(
            "market_breadth", "ths_fuyao", ff.fetch_market_breadth, priority=1
        )
    router.register("market_breadth", "em_push2ex", hf.fetch_market_breadth, priority=100)
    tushare_sectors = tushare_provides("sector_quotes")
    if tushare_sectors:
        router.register(
            "industry_quotes", "tushare", tsf.fetch_sector_quotes, priority=1
        )
    if biying_provides("industry_quotes"):
        router.register(
            "industry_quotes",
            "biying",
            bf.fetch_industry_quotes,
            priority=25 if tushare_sectors else 1,
        )
    router.register(
        "industry_quotes",
        "em_push2",
        hf.fetch_industry_quotes,
        priority=100
        if tushare_sectors or biying_provides("industry_quotes")
        else 1,
    )

    # Dashboard leadership acquisition.  The gateway above this registry owns
    # canonical mapping and payload validation, so future paid adapters can be
    # inserted here without changing dashboard consumers.
    router.register(
        "leader_quotes",
        "tencent_http",
        hf.fetch_realtime_quotes_tencent,
        priority=1,
    )
    router.register(
        "stock_sector_profiles",
        "em_push2delay",
        hf.fetch_stock_sector_profiles,
        priority=1,
    )
    router.register(
        "board_leaders",
        "eastmoney",
        hf.fetch_board_leaders,
        priority=1,
    )

    # ── v3.3.9 新增：同花顺备源 + 东财 slist + 通达信本地数据 ──
    router.register("stock_boards", "em_slist", emc.fetch_stock_boards, priority=1)
    router.register("ths_eps_forecast", "ths", ths.fetch_ths_eps_forecast, priority=1)
    router.register("ths_hot_reason", "ths", ths.fetch_ths_hot_reason, priority=1)
    router.register("limit_events", "ths", ths.fetch_ths_limit_up_pool, priority=1)
    router.register(
        "limit_event_status",
        "ths",
        ths.fetch_ths_limit_up_status,
        priority=1,
    )
    router.register("ths_hot_list", "ths", ths.fetch_ths_hot_list, priority=1)
    router.register("local_kline", "tdx_local", tdx.fetch_local_kline, priority=1)
    router.register("local_minute", "tdx_local", tdx.fetch_local_minute, priority=1)

    # ── v3.3.0 新增：新闻/资讯类数据源 ──
    router.register("baidu_economic_calendar", "akshare_baidu_economic", akf.fetch_baidu_economic_calendar, priority=1)
    router.register("baidu_trade_notify", "akshare_baidu_notify", akf.fetch_baidu_trade_notify, priority=1)
    router.register("index_news_sentiment", "akshare_index_sentiment", akf.fetch_index_news_sentiment, priority=1)
    router.register("futures_news", "akshare_futures_news", akf.fetch_futures_news, priority=1)
    router.register("sina_finance_news", "sina_direct", nf.fetch_sina_finance_news, priority=1)
    router.register("hot_search", "akshare_hot_search", akf.fetch_hot_search_baidu, priority=1)
    router.register("hot_rank", "akshare_hot_rank", akf.fetch_hot_rank_data, priority=1)
    router.register("xueqiu_hot", "akshare_xueqiu_hot", akf.fetch_xueqiu_hot, priority=1)
    if biying_provides("fund_hold"):
        router.register("fund_hold", "biying", bf.fetch_fund_hold_data, priority=1)
    router.register("fund_hold", "akshare_fund_hold", akf.fetch_fund_hold_data, priority=100 if biying_provides("fund_hold") else 1)

    # ── v3.3.0 新增：同花顺问财数据源（可选依赖） ──
    router.register("wencai_query", "pywencai", wf.fetch_wencai_query, priority=1)
    router.register("wencai_news", "iwencai_openapi", wf.fetch_wencai_news, priority=1)

    _registered = True
    report = router.get_registry_report()
    data_types = sorted({x["data_type"] for x in report})
    logger.info(
        "register_all_sources: 已注册 %d 个数据类型, %d 个数据源",
        len(data_types), len(report),
    )


def register_all_sources() -> None:
    """Register every source exactly once, even under concurrent imports."""
    with _registration_lock:
        _register_all_sources_unlocked()
