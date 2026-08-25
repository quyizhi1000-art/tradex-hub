"""必盈 API 到 Tradex 既有路由契约的字段适配。"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from astock_signals.smart_router import SourceCapabilityError

from ..utils.symbol import get_exchange, normalize_symbol
from .biying_client import request as _request


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_INDEX_NAMES = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
    "000688.SH": "科创50",
    "000905.SH": "中证500",
    "399852.SZ": "中证1000",
}
_PERIODS = {
    "daily": "d",
    "day": "d",
    "d": "d",
    "weekly": "w",
    "week": "w",
    "w": "w",
    "monthly": "m",
    "month": "m",
    "m": "m",
    "yearly": "y",
    "year": "y",
    "y": "y",
    "5": "5",
    "5m": "5",
    "15": "15",
    "15m": "15",
    "30": "30",
    "30m": "30",
    "60": "60",
    "60m": "60",
}
_ADJUSTS = {
    "": "n",
    "none": "n",
    "n": "n",
    "qfq": "f",
    "f": "f",
    "hfq": "b",
    "b": "b",
    "fr": "fr",
    "br": "br",
}


def _records(payload: Any, context: str, *, allow_empty: bool = False) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise RuntimeError(f"必盈{context}响应结构无效")
    if not payload and not allow_empty:
        raise RuntimeError(f"必盈{context}返回空数据")
    return payload


def _object(payload: Any, context: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError(f"必盈{context}响应结构无效")
    return payload


def _code6(value: str) -> str:
    raw = str(value or "").strip().upper()
    suffix_match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", raw)
    code = suffix_match.group(1) if suffix_match else normalize_symbol(raw)
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"无效 A 股代码: {value!r}")
    return code


def _exchange(value: str) -> str:
    raw = str(value or "").strip().upper()
    suffix_match = re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", raw)
    if suffix_match:
        return suffix_match.group(1).lower()
    prefix_match = re.fullmatch(r"(SH|SZ|BJ)\.?\d{6}", raw)
    if prefix_match:
        return prefix_match.group(1).lower()
    return get_exchange(raw)


def _market_symbol(value: str) -> str:
    code = _code6(value)
    return f"{code}.{_exchange(value).upper()}"


def _index_symbol(value: str) -> str:
    raw = str(value or "").strip().upper()
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", raw):
        return raw
    code = normalize_symbol(raw)
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"无效指数代码: {value!r}")
    return f"{code}.{get_exchange(value).upper()}"


def _compact_date(value: str) -> str:
    return str(value or "").strip().replace("-", "").replace("/", "")


def _provider_time(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw[:19], fmt)
            return parsed.replace(tzinfo=_SHANGHAI).isoformat()
        except ValueError:
            continue
    return None


def _number(value: Any) -> float | int | None:
    if value in (None, "", "-"):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return int(result) if result.is_integer() else result


def _shares_from_lots(value: Any) -> float | int | None:
    lots = _number(value)
    return lots * 100 if lots is not None else None


def _frame(rows: Iterable[dict[str, Any]], **attrs: Any) -> pd.DataFrame:
    frame = pd.DataFrame(list(rows))
    frame.attrs.update({"source_valid": True, **attrs})
    return frame


def _quote_row(code: str, item: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    raw_shares = item.get("pv")
    if raw_shares is None:
        # 京市实时示例不总是返回 pv；其 v 仍为手。
        raw_shares = _shares_from_lots(item.get("v"))
    return {
        "代码": code,
        "名称": name,
        "最新价": item.get("p"),
        "今开": item.get("o"),
        "最高": item.get("h"),
        "最低": item.get("l"),
        "昨收": item.get("yc"),
        "成交额": item.get("cje"),
        # 实测 pv 为原始股数，v 为手（pv/v=100）；gateway 统一输出股。
        "成交量": raw_shares,
        "涨跌额": item.get("ud"),
        "涨跌幅": item.get("pc"),
        "振幅": item.get("zf"),
        "换手率": item.get("tr", item.get("hs")),
        "市盈率-动态": item.get("pe"),
        "市净率": item.get("pb_ratio", item.get("sjl")),
        "总市值": item.get("sz"),
        "流通市值": item.get("lt"),
        "更新时间": _provider_time(item.get("t")),
    }


def fetch_realtime_quote(symbol: str = "", code: str = "", **kwargs: Any) -> pd.DataFrame:
    requested = _code6(symbol or code)
    exchange = _exchange(symbol or code)
    path = ["bj", "stock", "real", "time", requested] if exchange == "bj" else [
        "hsstock", "real", "time", requested
    ]
    item = _object(_request(path), "实时行情")
    row = _quote_row(requested, item)
    return _frame([row], provider_as_of=row["更新时间"])


def _normalise_period(period: str) -> str:
    result = _PERIODS.get(str(period or "daily").strip().lower())
    if result is None:
        if str(period).strip().lower() in {"1", "1m", "minute"}:
            raise SourceCapabilityError("biying standard API does not provide 1-minute bars")
        raise ValueError("period 必须是 daily/weekly/monthly/yearly 或 5/15/30/60 分钟")
    return result


def _normalise_adjust(adjust: str, period: str) -> str:
    result = _ADJUSTS.get(str(adjust or "").strip().lower())
    if result is None:
        raise ValueError("adjust 必须是空、qfq、hfq、fr 或 br")
    if period.isdigit() and result != "n":
        raise SourceCapabilityError("biying minute bars support only unadjusted prices")
    return result


def _bar_frame(payload: Any, *, period: str, adjust: str) -> pd.DataFrame:
    records = _records(payload, "K线")
    rows: list[dict[str, Any]] = []
    for item in records:
        close = _number(item.get("c"))
        previous = _number(item.get("pc"))
        high = _number(item.get("h"))
        low = _number(item.get("l"))
        change = close - previous if close is not None and previous is not None else None
        change_pct = change / previous * 100 if change is not None and previous else None
        amplitude = (high - low) / previous * 100 if high is not None and low is not None and previous else None
        rows.append(
            {
                "日期": str(item.get("t") or "")[:19],
                "开盘": item.get("o"),
                "收盘": item.get("c"),
                "最高": item.get("h"),
                "最低": item.get("l"),
                # 历史 K 线 v 为手；Tradex provider-neutral 合同使用股。
                "成交量": _shares_from_lots(item.get("v")),
                "成交额": item.get("a"),
                "振幅": amplitude,
                "涨跌幅": change_pct,
                "涨跌额": change,
                "停牌": item.get("sf"),
            }
        )
    period_attr = {"d": "daily", "w": "weekly", "m": "monthly", "y": "yearly"}.get(period, period)
    adjust_attr = {"n": "none", "f": "forward", "b": "backward", "fr": "forward", "br": "backward"}[adjust]
    provider_as_of = _provider_time(rows[-1]["日期"]) if rows else None
    frame = _frame(
        rows,
        period=period_attr,
        adjust=adjust_attr,
        provider_as_of=provider_as_of,
    )
    frame.sort_values("日期", inplace=True, ignore_index=True)
    return frame


def fetch_historical_kline(
    symbol: str = "",
    code: str = "",
    period: str = "daily",
    start_date: str = "",
    end_date: str = "",
    adjust: str = "qfq",
    count: int | None = None,
    **kwargs: Any,
) -> pd.DataFrame:
    requested = symbol or code
    period_code = _normalise_period(period)
    adjust_code = _normalise_adjust(adjust, period_code)
    market_symbol = _market_symbol(requested)
    params: dict[str, Any] = {}
    if start_date:
        params["st"] = _compact_date(start_date)
    if end_date:
        params["et"] = _compact_date(end_date)
    if count is not None:
        params["lt"] = max(1, int(count))
    if _exchange(requested) == "bj":
        path = ["bj", "history", market_symbol, period_code, adjust_code]
    else:
        path = ["hsstock", "history", market_symbol, period_code, adjust_code]
    return _bar_frame(_request(path, params=params), period=period_code, adjust=adjust_code)


def fetch_full_kline(
    code: str = "",
    symbol: str = "",
    period: str = "day",
    max_pages: int = 30,
    **kwargs: Any,
) -> pd.DataFrame:
    # eltdx 每页约 800 条；显式保留 max_pages 的有界语义。
    return fetch_historical_kline(
        symbol=symbol or code,
        period=period,
        adjust="",
        count=max(1, int(max_pages)) * 800,
    )


def fetch_adjusted_kline(
    code: str = "",
    symbol: str = "",
    period: str = "day",
    adjust: str = "qfq",
    count: int = 800,
    **kwargs: Any,
) -> pd.DataFrame:
    return fetch_historical_kline(
        symbol=symbol or code,
        period=period,
        adjust=adjust,
        count=count,
    )


def _index_quote(index_symbol: str) -> dict[str, Any]:
    item = _object(_request(["hsindex", "real", "time", index_symbol]), "指数实时行情")
    return {
        "代码": index_symbol,
        "指数名称": _INDEX_NAMES.get(index_symbol, index_symbol),
        "最新价": item.get("p"),
        "涨跌额": item.get("ud"),
        "涨跌幅": item.get("pc"),
        "昨收": item.get("yc"),
        "今开": item.get("o"),
        "最高": item.get("h"),
        "最低": item.get("l"),
        "成交量": item.get("tv", item.get("v")),
        "成交额": item.get("cje"),
        "更新时间": _provider_time(item.get("t")),
    }


def fetch_market_overview(symbol: str = "", **kwargs: Any) -> pd.DataFrame:
    if symbol:
        targets = [_index_symbol(symbol)]
    else:
        # Keep the legacy Shanghai/Shenzhen pair while also supplying the four
        # role indices used by the desktop market-watch view: broad market,
        # large-cap, small-cap and growth.  Provider codes remain confined to
        # this mapper; consumers select canonical instrument ids by role.
        targets = [
            "000001.SH",
            "399001.SZ",
            "000300.SH",
            "399852.SZ",
            "399006.SZ",
        ]
    rows = [_index_quote(target) for target in targets]
    timestamps = [row["更新时间"] for row in rows if row.get("更新时间")]
    return _frame(rows, provider_as_of=max(timestamps, default=None))


def fetch_index_daily_amount(
    symbol: str = "", code: str = "", days: int = 6, **kwargs: Any
) -> pd.DataFrame:
    target = _index_symbol(symbol or code)
    payload = _request(["hsindex", "history", target, "d"], params={"lt": max(1, int(days))})
    rows = [
        {"日期": str(item.get("t") or "")[:10], "成交量": item.get("v"), "成交额": item.get("a")}
        for item in _records(payload, "指数日线")
    ]
    return _frame(rows)


def fetch_all_a_shares(**kwargs: Any) -> pd.DataFrame:
    hs = _records(_request(["hslt", "list"]), "股票列表")
    bj = _records(_request(["bj", "list", "all"]), "北交所股票列表", allow_empty=True)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*hs, *bj]:
        code = _code6(str(item.get("dm") or ""))
        if code in seen:
            continue
        seen.add(code)
        exchange = str(item.get("jys") or get_exchange(code)).lower()
        rows.append(
            {"代码": f"{exchange}{code}", "名称": item.get("mc"), "市场": exchange, "纯代码": code}
        )
    return _frame(rows, total=len(rows))


_COMPANY_LABELS = {
    "name": "公司名称",
    "ename": "英文名称",
    "market": "上市市场",
    "idea": "概念及板块",
    "ldate": "上市日期",
    "sprice": "发行价格",
    "principal": "主承销商",
    "rdate": "成立日期",
    "rprice": "注册资本",
    "instype": "机构类型",
    "organ": "组织形式",
    "secre": "董事会秘书",
    "phone": "公司电话",
    "email": "公司邮箱",
    "site": "公司网站",
    "addr": "注册地址",
    "oaddr": "办公地址",
    "bscope": "经营范围",
    "desc": "公司简介",
}


def fetch_company_info(
    endpoint: str = "individual_info",
    symbol: str = "",
    keyword: str = "",
    industry: str = "",
    **kwargs: Any,
) -> pd.DataFrame:
    if endpoint == "code_name":
        records = _records(_request(["hslt", "list"]), "股票列表")
        records.extend(
            _records(_request(["bj", "list", "all"]), "北交所股票列表", allow_empty=True)
        )
        return _frame({"code": item.get("dm"), "name": item.get("mc")} for item in records)
    if endpoint == "individual_info":
        if _exchange(symbol) == "bj":
            raise SourceCapabilityError("biying company profile endpoint does not cover BSE stocks")
        item = _object(_request(["hscp", "gsjj", _code6(symbol)]), "公司简介")
        rows = [
            {"item": label, "value": item.get(key)}
            for key, label in _COMPANY_LABELS.items()
            if item.get(key) not in (None, "")
        ]
        return _frame(rows)
    # 主营构成和按行业查同行必须保留原有数据源的精确语义。
    raise SourceCapabilityError(f"biying company_info does not support endpoint {endpoint!r}")


_INCOME_MAP = {
    "jzrq": "REPORT_DATE_NAME",
    "yyzsr": "TOTAL_OPERATE_INCOME",
    "yysr": "OPERATE_INCOME",
    "yycb": "OPERATE_COST",
    "xsfy": "SALE_EXPENSE",
    "glfy": "MANAGE_EXPENSE",
    "yffy": "RESEARCH_EXPENSE",
    "cwfy": "FINANCE_EXPENSE",
    "yylr": "OPERATE_PROFIT",
    "lrze": "TOTAL_PROFIT",
    "sdsfy": "INCOME_TAX",
    "jlr": "NETPROFIT",
    "gsmgsyzzdjlr": "PARENT_NETPROFIT",
    "jbmgsy": "BASIC_EPS",
    "xsmgsy": "DILUTED_EPS",
}
_BALANCE_MAP = {
    "jzrq": "REPORT_DATE_NAME",
    "hbzj": "MONETARYFUNDS",
    "yszk": "ACCOUNTS_RECE",
    "yfkx": "PREPAYMENT",
    "ch": "INVENTORY",
    "ldzchj": "TOTAL_CURRENT_ASSETS",
    "gdzc": "FIXED_ASSETS",
    "zjgc": "CIP",
    "wxzc": "INTANGIBLE_ASSETS",
    "sy": "GOODWILL",
    "fldzchj": "TOTAL_NONCURRENT_ASSETS",
    "zczj": "TOTAL_ASSETS",
    "dqjk": "SHORT_LOAN",
    "yfzk": "ACCOUNTS_PAYABLE",
    "ldfzhj": "TOTAL_CURRENT_LIAB",
    "cqjk": "LONG_LOAN",
    "yfzq": "BOND_PAYABLE",
    "fldfzhj": "TOTAL_NONCURRENT_LIAB",
    "fzhj": "TOTAL_LIABILITIES",
    "gsmgdqsyhj": "TOTAL_PARENT_EQUITY",
    "ssgdqy": "MINORITY_EQUITY",
    "syzqyhj": "TOTAL_EQUITY",
}
_CASHFLOW_MAP = {
    "jzrq": "REPORT_DATE_NAME",
    "jyhdxjlrxj": "TOTAL_OPERATE_INFLOW",
    "jyhdxjlcxj": "TOTAL_OPERATE_OUTFLOW",
    "jyhdcsdxjlje": "NETCASH_OPERATE",
    "tzhdxjlrxj": "TOTAL_INVEST_INFLOW",
    "tzhdxjlcxj": "TOTAL_INVEST_OUTFLOW",
    "tzhdcsdxjlxj": "NETCASH_INVEST",
    "czhdxjlrxj": "TOTAL_FINANCE_INFLOW",
    "czhdxjlcxj": "TOTAL_FINANCE_OUTFLOW",
    "czhdcsdxjlxj": "NETCASH_FINANCE",
    "xjxjdhwjzje": "CCE_ADD",
    "qcxjjxjdhwye": "BEGIN_CCE",
    "qmxjjxjdhwye": "END_CCE",
}
_INDICATOR_MAP = {
    "jzrq": "报告期",
    "jbmgsy": "基本每股收益",
    "xsmgsy": "稀释每股收益",
    "mgjzc": "每股净资产",
    "mgjyhdxjl": "每股经营现金流",
    "jzcsyl": "净资产收益率",
    "jqjzcsyl": "加权净资产收益率",
    "mlv": "毛利率",
    "jlv": "净利率",
    "zcfzl": "资产负债率",
    "yskyysr": "应收账款周转率",
    "chzzl": "存货周转率",
}


def _mapped_financial_frame(payload: Any, context: str, mapping: dict[str, str]) -> pd.DataFrame:
    records = _records(payload, context, allow_empty=True)
    rows: list[dict[str, Any]] = []
    for item in records:
        row = dict(item)
        for source, target in mapping.items():
            if source in item:
                row[target] = item[source]
        rows.append(row)
    frame = _frame(rows)
    sort_col = mapping.get("jzrq", "jzrq")
    if sort_col in frame.columns:
        frame.sort_values(sort_col, ascending=False, inplace=True, ignore_index=True)
    return frame


def fetch_financial_stmt(
    endpoint: str = "profit", symbol: str = "", **kwargs: Any
) -> pd.DataFrame:
    route = {
        "profit": ("income", _INCOME_MAP),
        "balance": ("balance", _BALANCE_MAP),
        "cashflow": ("cashflow", _CASHFLOW_MAP),
        "indicator": ("pershareindex", _INDICATOR_MAP),
    }.get(endpoint)
    if route is None:
        raise SourceCapabilityError(f"biying financial_stmt does not support endpoint {endpoint!r}")
    api_name, mapping = route
    market_symbol = _market_symbol(symbol)
    path = (
        ["bj", "financial", api_name, market_symbol]
        if _exchange(symbol) == "bj"
        else ["hsstock", "financial", api_name, market_symbol]
    )
    return _mapped_financial_frame(
        _request(path),
        f"{endpoint}财务数据",
        mapping,
    )


def fetch_finance_report(
    code: str = "", symbol: str = "", report_type: str = "zcfzb", **kwargs: Any
) -> pd.DataFrame:
    route = {
        "zcfzb": ("balance", _BALANCE_MAP),
        "lrb": ("income", _INCOME_MAP),
        "xjllb": ("cashflow", _CASHFLOW_MAP),
    }.get(str(report_type).strip().lower())
    if route is None:
        raise ValueError("report_type 必须是 zcfzb、lrb 或 xjllb")
    api_name, mapping = route
    requested = code or symbol
    market_symbol = _market_symbol(requested)
    path = (
        ["bj", "financial", api_name, market_symbol]
        if _exchange(requested) == "bj"
        else ["hsstock", "financial", api_name, market_symbol]
    )
    return _mapped_financial_frame(
        _request(path),
        "财务报表",
        mapping,
    )


def _parse_symbols(value: Any, *, maximum: int = 100) -> list[str]:
    if isinstance(value, str):
        raw = [item.strip() for item in value.split(",") if item.strip()]
    else:
        try:
            raw = [str(item).strip() for item in value if str(item).strip()]
        except TypeError as exc:
            raise ValueError("symbols 必须是代码或代码列表") from exc
    result: list[str] = []
    for item in raw:
        market = _market_symbol(item)
        if market not in result:
            result.append(market)
    if not result or len(result) > maximum:
        raise ValueError(f"symbols 必须包含 1-{maximum} 个 A 股代码")
    return result


def fetch_valuation_snapshot(
    symbols: Any = "", thscodes: Any = "", **kwargs: Any
) -> pd.DataFrame:
    requested = _parse_symbols(symbols or thscodes)
    rows: list[dict[str, Any]] = []
    for offset in range(0, len(requested), 20):
        chunk = requested[offset : offset + 20]
        codes = [item.split(".", 1)[0] for item in chunk]
        records = _records(
            _request(["hsrl", "ssjy_more"], params={"stock_codes": ",".join(codes)}),
            "批量估值快照",
            allow_empty=True,
        )
        by_code = {str(item.get("dm") or ""): item for item in records}
        for market_symbol in chunk:
            code = market_symbol.split(".", 1)[0]
            item = by_code.get(code)
            if item is None:
                continue
            rows.append(
                {
                    "同花顺代码": market_symbol,
                    "代码": code,
                    "名称": item.get("mc"),
                    "市盈率TTM": item.get("pe"),
                    "市盈率MRQ": None,
                    "市净率MRQ": item.get("pb_ratio", item.get("sjl")),
                    "市销率TTM": None,
                    "市现率TTM": None,
                    "更新时间": _provider_time(item.get("t")),
                }
            )
    return _frame(rows, total=len(rows), requested_total=len(requested))


def fetch_valuation(
    endpoint: str = "baidu", symbol: str = "", indicator: str = "", **kwargs: Any
) -> pd.DataFrame:
    market_symbol = _market_symbol(symbol) if symbol else ""
    if endpoint in {"dividend_detail", "dividend_cninfo"}:
        if _exchange(symbol) == "bj":
            raise SourceCapabilityError("biying dividend endpoint does not cover BSE stocks")
        return _frame(_records(_request(["hscp", "jnfh", _code6(symbol)]), "分红数据", allow_empty=True))
    if endpoint == "circulate_holder":
        path = (
            ["bj", "financial", "flowholder", market_symbol]
            if _exchange(symbol) == "bj"
            else ["hsstock", "financial", "flowholder", market_symbol]
        )
        return _frame(
            _records(
                _request(path),
                "流通股东",
                allow_empty=True,
            )
        )
    raise SourceCapabilityError(f"biying valuation does not support endpoint {endpoint!r}")


def fetch_dividend_financing(code: str = "", symbol: str = "", **kwargs: Any) -> pd.DataFrame:
    requested = code or symbol
    if _exchange(requested) == "bj":
        raise SourceCapabilityError("biying dividend endpoint does not cover BSE stocks")
    records = _records(
        _request(["hscp", "jnfh", _code6(requested)]),
        "分红融资",
        allow_empty=True,
    )
    labels = {
        "cdate": "公告日期",
        "edate": "除权除息日",
        "hdate": "股权登记日",
        "sdate": "送转股上市日",
        "send": "每10股送股",
        "give": "每10股转增",
        "line": "每10股派息",
        "change": "方案进度",
    }
    return _frame({labels.get(key, key): value for key, value in item.items()} for item in records)


def _tree_records() -> list[dict[str, Any]]:
    return _records(_request(["hszg", "list"]), "指数行业概念树")


def _short_tree_name(value: Any) -> str:
    return str(value or "").rsplit("-", 1)[-1].strip()


def fetch_industry_data(
    endpoint: str = "board_industry_name_em",
    industry: str = "",
    sector_type: str = "行业资金流",
    indicator: str = "今日",
    period: str = "日k",
    start_date: str = "",
    end_date: str = "",
    **kwargs: Any,
) -> pd.DataFrame:
    if endpoint in {"board_industry_name_em", "board_industry_name_ths"}:
        selected = [item for item in _tree_records() if item.get("type1") == 0 and item.get("type2") in {0, 1, 5} and item.get("isleaf") == 1]
        return _frame(
            {"板块代码": item.get("code"), "板块名称": _short_tree_name(item.get("name")), "分类": item.get("pname")}
            for item in selected
        )
    if endpoint in {"board_concept_name_em", "board_concept_name_ths"}:
        selected = [item for item in _tree_records() if item.get("type1") == 0 and item.get("type2") in {2, 3} and item.get("isleaf") == 1]
        return _frame(
            {"板块代码": item.get("code"), "板块名称": _short_tree_name(item.get("name")), "分类": item.get("pname")}
            for item in selected
        )
    if endpoint == "board_industry_cons_em":
        candidates = [
            item for item in _tree_records()
            if item.get("type1") == 0
            and item.get("isleaf") == 1
            and _short_tree_name(item.get("name")) == str(industry).strip()
        ]
        if not candidates:
            raise SourceCapabilityError(f"biying cannot resolve industry {industry!r}")
        records = _records(_request(["hszg", "gg", str(candidates[0]["code"])]), "行业成分股", allow_empty=True)
        return _frame(
            {"代码": item.get("dm"), "名称": item.get("mc"), "市场": item.get("jys")}
            for item in records
        )
    if endpoint == "sector_fund_flow_rank":
        if str(indicator or "今日") != "今日":
            raise SourceCapabilityError("biying sector fund flow provides only the latest trading day")
        path = ["hibk", "gnbk"] if "概念" in str(sector_type) else ["hibk", "zjhhy"]
        return _sector_flow_frame(_request(path))
    raise SourceCapabilityError(f"biying industry_data does not support endpoint {endpoint!r}")


def fetch_concept_attribution(symbol: str = "", code: str = "", **kwargs: Any) -> dict[str, Any]:
    requested = _code6(symbol or code)
    records = _records(_request(["hszg", "zg", requested]), "概念归属", allow_empty=True)
    concepts: list[dict[str, Any]] = []
    industries: list[dict[str, Any]] = []
    regions: list[dict[str, Any]] = []
    for item in records:
        name = str(item.get("name") or "")
        target = concepts
        if "地域" in name:
            target = regions
        elif "行业" in name:
            target = industries
        target.append({"code": item.get("code"), "name": _short_tree_name(name)})
    return {
        "code": requested,
        "concepts": concepts,
        "industries": industries,
        "regions": regions,
        "source": "biying",
    }


def fetch_ths_index_catalog(tag: str = "cn_concept", **kwargs: Any) -> pd.DataFrame:
    normalised = str(tag or "cn_concept").strip().lower()
    tag_types = {
        "cn_concept": {2, 3},
        "industry": {0, 1, 5},
        "region": {4},
        "tszs": {6, 7, 8, 9, 10, 11, 12},
    }
    if normalised not in tag_types:
        raise ValueError("tag 必须是 cn_concept、region、tszs 或 industry")
    selected = [
        item for item in _tree_records()
        if item.get("type1") == 0 and item.get("type2") in tag_types[normalised] and item.get("isleaf") == 1
    ]
    return _frame(
        {
            "同花顺指数代码": item.get("code"),
            "名称": _short_tree_name(item.get("name")),
            "标签": normalised,
            "更新时间": None,
        }
        for item in selected
    )


def fetch_ths_index_constituents(
    index_code: str = "", thscode: str = "", **kwargs: Any
) -> pd.DataFrame:
    requested = str(index_code or thscode or "").strip()
    if not requested:
        raise ValueError("index_code 不能为空")
    records = _records(_request(["hszg", "gg", requested]), "指数成分股", allow_empty=True)
    return _frame(
        {
            "指数代码": requested,
            "同花顺代码": f"{item.get('dm')}.{str(item.get('jys') or '').upper()}",
            "代码": item.get("dm"),
            "名称": item.get("mc"),
            "更新时间": None,
        }
        for item in records
    )


def _sector_flow_frame(payload: Any) -> pd.DataFrame:
    records = _records(payload, "板块资金流", allow_empty=True)
    rows = []
    for item in records:
        leader = str(item.get("lzgdm") or "")
        match = re.search(r"(\d{6})", leader)
        rows.append(
            {
                "板块代码": item.get("dm"),
                "最新点位": item.get("jj"),
                "板块名称": item.get("mc"),
                "涨跌幅": item.get("zdf"),
                "成交额": None,
                "主力净流入": item.get("jlr"),
                "主力净流入-占比": item.get("jlrl"),
                "领涨股代码": match.group(1) if match else None,
                "领涨股票": item.get("lzgmc"),
                "更新时间": _provider_time(item.get("t")),
                "source": "biying_hibk",
            }
        )
    return _frame(rows)


def fetch_industry_quotes(
    board_type: str = "industry", exact: Any = None, **kwargs: Any
) -> pd.DataFrame:
    normalised = str(board_type or "industry").strip().lower()
    if normalised not in {"industry", "concept"}:
        raise ValueError("board_type must be 'industry' or 'concept'")
    path = ["hibk", "zjhhy"] if normalised == "industry" else ["hibk", "gnbk"]
    frame = _sector_flow_frame(_request(path))
    if exact is None or frame.empty:
        return frame
    names = {exact} if isinstance(exact, str) else {str(item) for item in exact}
    return frame[frame["板块名称"].isin(names)].reset_index(drop=True)


_POOL_PATHS = {"zt": "ztgc", "zb": "zbgc", "dt": "dtgc"}


def fetch_limit_up_board(
    board_type: str = "zt", date_ms: int | None = None, **kwargs: Any
) -> dict[str, Any]:
    normalised = str(board_type or "zt").strip().lower()
    if normalised in {"sentiment", "prev_zt"}:
        raise SourceCapabilityError(f"biying does not support limit_up_board type {normalised!r}")
    endpoint = _POOL_PATHS.get(normalised)
    if endpoint is None:
        raise ValueError("board_type 必须是 zt、zb、dt、prev_zt 或 sentiment")
    if date_ms is None:
        trade_date = datetime.now(_SHANGHAI).date().isoformat()
    else:
        trade_date = datetime.fromtimestamp(int(date_ms) / 1000, tz=_SHANGHAI).date().isoformat()
    records = _records(_request(["hslt", endpoint, trade_date]), "涨跌停池", allow_empty=True)
    rows = [
        {
            "code": item.get("dm"),
            "name": item.get("mc"),
            "price": item.get("p"),
            "change_pct": item.get("zf"),
            "amount": item.get("cje"),
            "float_market_cap": item.get("lt"),
            "total_market_cap": item.get("zsz"),
            "turnover_rate": item.get("hs"),
            "board_count": item.get("lbc"),
            "first_limit_time": item.get("fbt"),
            "last_limit_time": item.get("lbt"),
            "seal_amount": item.get("zj"),
            "open_count": item.get("zbc"),
            "limit_stats": item.get("tj"),
            "industry": item.get("hy"),
        }
        for item in records
    ]
    return {"type": normalised, "data": rows, "count": len(rows), "source": "biying", "trade_date": trade_date}


def fetch_dragon_tiger(
    code: str = "",
    symbol: str = "",
    trade_date: str = "",
    board_type: str = "all",
    look_back_days: int = 1,
    **kwargs: Any,
) -> pd.DataFrame:
    if code or symbol:
        raise SourceCapabilityError("biying market-day dragon tiger does not accept a stock code")
    if str(board_type or "all").strip().lower() != "all" or look_back_days != 1:
        raise SourceCapabilityError("biying daily dragon tiger supports only board_type='all' and one day")
    payload = _object(_request(["hilh", "mrxq"]), "龙虎榜每日详情")
    provider_date = str(payload.get("t") or "")[:10]
    if trade_date and provider_date != str(trade_date)[:10]:
        raise SourceCapabilityError("biying daily dragon tiger does not provide the requested historical date")
    rows: list[dict[str, Any]] = []
    for category, items in payload.items():
        if category == "t" or items is None:
            continue
        if not isinstance(items, list):
            raise RuntimeError("必盈龙虎榜分类结构无效")
        for item in items:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "代码": item.get("dm"),
                    "名称": item.get("mc"),
                    "上榜日": provider_date,
                    "解读": category,
                    "涨跌幅": item.get("val"),
                    "收盘价": item.get("c"),
                    # 文档单位为万元，保留显式列名，避免与元口径混用。
                    "成交量(万)": item.get("v"),
                    "成交额(万)": item.get("e"),
                }
            )
    return _frame(rows, provider_as_of=provider_date)


def fetch_lockup_expiry(
    symbol: str = "",
    code: str = "",
    trade_date: str = "",
    forward_days: int = 90,
    **kwargs: Any,
) -> dict[str, Any]:
    requested = _code6(symbol or code)
    anchor = datetime.strptime(trade_date, "%Y-%m-%d").date() if trade_date else datetime.now(_SHANGHAI).date()
    end_date = anchor + timedelta(days=max(0, int(forward_days)))
    records = _records(_request(["hscp", "jjxs", requested]), "限售解禁", allow_empty=True)
    result: dict[str, Any] = {
        "code": requested,
        "trade_date": anchor.isoformat(),
        "history": [],
        "upcoming": [],
        "total_upcoming_ratio": None,
        "risk_warning": False,
        "source": "biying",
    }
    for item in records:
        raw_date = str(item.get("rdate") or "")[:10]
        try:
            release_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        row = {
            "date": raw_date,
            "type": item.get("batch"),
            # 文档 ramount 单位为万股；工具合同统一输出股。
            "shares": (
                _number(item.get("ramount")) * 10_000
                if _number(item.get("ramount")) is not None
                else None
            ),
            "ratio": None,
            # rprice 实际为解禁市值（亿元），不能误标为每股价格。
            "market_value_100m_cny": item.get("rprice"),
            "publish_date": item.get("pdate"),
        }
        if release_date < anchor:
            result["history"].append(row)
        elif release_date <= end_date:
            result["upcoming"].append(row)
    return result


def fetch_fund_hold_data(
    endpoint: str = "hold", symbol: str = "基金持仓", date: str = "", **kwargs: Any
) -> pd.DataFrame:
    if endpoint != "hold":
        raise SourceCapabilityError("biying institutional holdings do not provide single-fund detail")
    source_map = {"基金持仓": "jj", "QFII持仓": "qf", "社保持仓": "sb"}
    api_name = source_map.get(str(symbol))
    if api_name is None:
        raise SourceCapabilityError(f"biying does not support holding category {symbol!r}")
    if date:
        parsed = datetime.strptime(_compact_date(date), "%Y%m%d").date()
    else:
        today = datetime.now(_SHANGHAI).date()
        current_q = (today.month - 1) // 3 + 1
        year = today.year if current_q > 1 else today.year - 1
        quarter = current_q - 1 if current_q > 1 else 4
        parsed = datetime(year, quarter * 3, 1).date()
    year = parsed.year
    quarter = (parsed.month - 1) // 3 + 1
    records = _records(_request(["hijg", api_name, str(year), str(quarter)]), "机构持仓", allow_empty=True)
    return _frame(records)


__all__ = [name for name in globals() if name.startswith("fetch_")]
