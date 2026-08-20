"""同花顺数据源 —— 一致预期 / 热点归因 / 涨停揭秘 / 热榜（零鉴权）。

借鉴 a-stock-data 的同花顺实现，均为零鉴权直连接口。
同花顺是不封 IP 的低风险源，东财被封时的备选。
"""
from __future__ import annotations

import logging
import math
import re
import threading
from datetime import datetime
from io import StringIO

import pandas as pd
import requests

logger = logging.getLogger("tradex.ths")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36")

_SESSION_LOCAL = threading.local()

_THS_LIMIT_UP_COLUMNS = [
    "代码", "名称", "价格", "涨幅%", "涨停原因", "板型", "封板成功率", "炸板次数",
    "封单额", "连板", "首封时间", "是否回封", "数据日期", "交易状态",
]

_THS_DAY_BOARD_PATTERN = re.compile(r"^(?P<days>\d+)天(?P<boards>\d+)板$")
_THS_STREAK_BOARD_PATTERN = re.compile(r"^(?P<boards>\d+)连板$")
_THS_BOARD_PATTERN = re.compile(r"^(?P<boards>\d+)板$")


def _session() -> requests.Session:
    """Return a worker-local session so concurrent tools never share cookies."""
    session = getattr(_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False  # 直连，不读系统代理
        _SESSION_LOCAL.session = session
    return session


def _get(url: str, params: dict | None = None, headers: dict | None = None, timeout: int = 10, **kw):
    h = {"User-Agent": _UA}
    if headers:
        h.update(headers)
    return _session().get(url, params=params, headers=h, timeout=timeout, **kw)


def fetch_ths_eps_forecast(code: str, **kwargs) -> pd.DataFrame:
    """同花顺机构一致预期 EPS（直连 basic.10jqka.com.cn，解析 HTML 表格）。

    Returns:
        DataFrame: 年度 / 预测机构数 / 最小值 / 均值 / 最大值（均值=一致预期 EPS）
    """
    code = str(code).split(".")[0].split("_")[0]  # 纯 6 位，带前缀会 404
    url = f"https://basic.10jqka.com.cn/new/{code}/worth.html"
    r = _get(url, headers={"Referer": "https://basic.10jqka.com.cn/"}, timeout=15)
    r.encoding = "gbk"
    dfs = pd.read_html(StringIO(r.text))
    for df in dfs:
        cols = [str(c) for c in df.columns]
        if any("每股收益" in c or "均值" in c for c in cols):
            return df
    return dfs[0] if dfs else pd.DataFrame()


def fetch_ths_hot_reason(date: str | None = None, **kwargs) -> pd.DataFrame:
    """同花顺当日强势股 + 题材归因（reason 人工运营标签，核心字段）。

    实测 73ms 拿到 ~125 只 + 完整字段。
    Returns columns: 名称 / 代码 / 题材归因 / 涨幅% / 换手率% / 成交额 / 大单净量 ...
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    url = (f"http://zx.10jqka.com.cn/event/api/getharden/"
           f"date/{date}/orderby/date/orderway/desc/charset/GBK/")
    r = _get(url, timeout=10)
    data = r.json()
    if data.get("errocode", 0) != 0:
        raise RuntimeError(f"同花顺热点错误: {data.get('errormsg', '')}")
    rows = data.get("data") or []
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    rename = {
        "name": "名称", "code": "代码", "reason": "题材归因",
        "close": "收盘价", "zhangdie": "涨跌额", "zhangfu": "涨幅%",
        "huanshou": "换手率%", "chengjiaoe": "成交额",
        "chengjiaoliang": "成交量", "ddejingliang": "大单净量", "market": "市场",
    }
    return df.rename(columns=rename)


def _normalize_ths_pool_date(value: str) -> str:
    """校验日期并归一为同花顺接口要求的 YYYYMMDD。"""
    if not isinstance(value, str):
        raise ValueError("同花顺涨停池日期必须是字符串")
    raw = value.strip()
    if re.fullmatch(r"\d{8}", raw):
        fmt = "%Y%m%d"
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        fmt = "%Y-%m-%d"
    else:
        raise ValueError(f"无效的同花顺涨停池日期: {value!r}")
    try:
        parsed = datetime.strptime(raw, fmt)
    except ValueError as exc:
        raise ValueError(f"无效的同花顺涨停池日期: {value!r}") from exc
    return parsed.strftime("%Y%m%d")


def _normalize_ths_high_days(item: dict) -> object:
    """Normalize only provider-declared first boards; keep other unknowns unknown."""
    value = item["high_days"]
    if value is not None and str(value).strip():
        return value
    if str(item.get("change_tag") or "").strip().upper() == "FIRST_LIMIT":
        return "首板"
    # LIMIT_BACK does not encode a board number, so guessing one would turn
    # incomplete provider data into false leadership evidence.
    return None if value is None else ""


def _has_parseable_ths_board_count(value: object) -> bool:
    """Mirror the strict board-label grammar used by sector attribution."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, float):
        return math.isfinite(value) and value.is_integer() and value > 0

    label = "".join(str(value).split())
    if label == "首板":
        return True
    for pattern in (_THS_DAY_BOARD_PATTERN, _THS_STREAK_BOARD_PATTERN, _THS_BOARD_PATTERN):
        match = pattern.fullmatch(label)
        if match is not None:
            return int(match.group("boards")) > 0
    return False


def _ths_limit_up_page(url: str, params: dict, expected_date: str) -> tuple[dict, list[dict], object]:
    """获取并验证一页涨停池，避免将远程异常降级为“空池”。"""
    requested_page = params["page"]
    try:
        response = _get(url, params=params, timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"同花顺涨停池 HTTP 请求失败（第 {requested_page} 页）") from exc

    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"同花顺涨停池返回了无效 JSON（第 {requested_page} 页）") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"同花顺涨停池根响应不是对象（第 {requested_page} 页）")
    if payload.get("status_code") != 0 or payload.get("status_msg") != "success":
        raise RuntimeError(
            f"同花顺涨停池接口错误（第 {requested_page} 页）: "
            f"status_code={payload.get('status_code')!r}, status_msg={payload.get('status_msg')!r}"
        )

    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"同花顺涨停池缺少有效 data（第 {requested_page} 页）")
    data_date = data.get("date")
    if not isinstance(data_date, str) or not re.fullmatch(r"\d{8}", data_date):
        raise RuntimeError(f"同花顺涨停池 data.date 格式无效（第 {requested_page} 页）")
    if data_date != expected_date:
        raise RuntimeError(
            f"同花顺涨停池日期不匹配（第 {requested_page} 页）: "
            f"请求 {expected_date}，返回 {data_date}"
        )
    trade_status = data.get("trade_status")
    if not isinstance(trade_status, dict):
        raise RuntimeError(f"同花顺涨停池 trade_status 不是对象（第 {requested_page} 页）")
    for key in ("id", "name"):
        value = trade_status.get(key)
        if value is None or not str(value).strip():
            raise RuntimeError(
                f"同花顺涨停池 trade_status.{key} 缺失（第 {requested_page} 页）"
            )

    page_meta = data.get("page")
    if not isinstance(page_meta, dict):
        raise RuntimeError(f"同花顺涨停池缺少有效 page（第 {requested_page} 页）")
    for key in ("limit", "total", "count", "page"):
        value = page_meta.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise RuntimeError(
                f"同花顺涨停池 page.{key} 必须是整数（第 {requested_page} 页）"
            )
    if page_meta["limit"] <= 0 or page_meta["total"] < 0 or page_meta["count"] < 0:
        raise RuntimeError(f"同花顺涨停池 page 计数无效（第 {requested_page} 页）")
    valid_page_number = page_meta["page"] == requested_page
    # 真实接口在有效空池时会把 page 返回为 0，尽管请求的是第 1 页。
    if page_meta["total"] == 0 and requested_page == 1:
        valid_page_number = page_meta["page"] in (0, 1)
    if not valid_page_number:
        raise RuntimeError(
            f"同花顺涨停池页码不匹配: 请求 {requested_page}，返回 {page_meta['page']}"
        )

    info = data.get("info")
    if not isinstance(info, list):
        raise RuntimeError(f"同花顺涨停池缺少有效 info（第 {requested_page} 页）")
    if len(info) > page_meta["limit"]:
        raise RuntimeError(f"同花顺涨停池单页记录数超过 limit（第 {requested_page} 页）")
    for index, item in enumerate(info):
        if not isinstance(item, dict):
            raise RuntimeError(
                f"同花顺涨停池 info[{index}] 不是对象（第 {requested_page} 页）"
            )
        missing_fields = [
            key
            for key in ("code", "name", "reason_type")
            if item.get(key) is None or not str(item.get(key)).strip()
        ]
        # Historical pages contain legitimate null/empty high_days values.
        # Its presence is still part of the source schema even when the value
        # cannot provide board-count evidence.
        if "high_days" not in item:
            missing_fields.append("high_days")
        if missing_fields:
            raise RuntimeError(
                f"同花顺涨停池 info[{index}] 缺少必需字段 "
                f"{','.join(missing_fields)}（第 {requested_page} 页）"
            )
        code = str(item["code"]).strip()
        if re.fullmatch(r"[036489]\d{5}", code) is None:
            raise RuntimeError(
                f"同花顺涨停池 info[{index}].code 不是受支持的 A 股代码"
                f"（第 {requested_page} 页）"
            )
    return page_meta, info, trade_status


def fetch_ths_limit_up_pool(date: str, **kwargs) -> pd.DataFrame:
    """同花顺涨停揭秘：涨停原因 + 封板质量。date=YYYYMMDD 或 YYYY-MM-DD。

    Returns columns: 代码 / 名称 / 涨停原因 / 板型 / 封板成功率 / 炸板次数 / 封单额 / 连板 / 首封时间 / 数据日期 / 交易状态
    """
    normalized_date = _normalize_ths_pool_date(date)
    url = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
    params = {
        "page": 1, "limit": 200,
        "field": "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004",
        "filter": "HS,GEM2STAR", "order_field": "330324", "order_type": "0", "date": normalized_date,
    }
    first_page, first_info, trade_status = _ths_limit_up_page(url, params, normalized_date)
    total = first_page["total"]
    page_count = first_page["count"]
    expected_page_count = (total + first_page["limit"] - 1) // first_page["limit"] if total else 0
    valid_page_count = page_count == expected_page_count
    # 某些接口版本把空集表示为一个空页；同样属于有效空池。
    if total == 0:
        valid_page_count = page_count in (0, 1)
    if not valid_page_count:
        raise RuntimeError(
            "同花顺涨停池分页计数不一致: "
            f"total={total}, limit={first_page['limit']}, count={page_count}, "
            f"预期 count={expected_page_count}"
        )

    all_info = list(first_info)
    for page_number in range(2, page_count + 1):
        page_params = dict(params)
        page_params["page"] = page_number
        page_meta, page_info, page_trade_status = _ths_limit_up_page(url, page_params, normalized_date)
        if (
            page_meta["limit"] != first_page["limit"]
            or page_meta["total"] != total
            or page_meta["count"] != page_count
        ):
            raise RuntimeError(f"同花顺涨停池分页元数据不一致（第 {page_number} 页）")
        if page_trade_status != trade_status:
            raise RuntimeError(f"同花顺涨停池交易状态跨页不一致（第 {page_number} 页）")
        all_info.extend(page_info)

    raw_count = len(all_info)
    if raw_count != total:
        raise RuntimeError(
            f"同花顺涨停池记录数与 total 不一致: 收到 {raw_count}，total={total}"
        )

    codes = [str(item["code"]).strip() for item in all_info]
    if len(set(codes)) != len(codes):
        raise RuntimeError("同花顺涨停池存在重复代码，唯一股票数与 total 不一致")
    info = all_info

    rows = []
    board_labels = []
    for it in info:
        ft = it.get("first_limit_up_time")
        board_label = _normalize_ths_high_days(it)
        board_labels.append(board_label)
        rows.append({
            "代码": it.get("code"), "名称": it.get("name"),
            "价格": it.get("latest"), "涨幅%": it.get("change_rate"),
            "涨停原因": it.get("reason_type", ""), "板型": it.get("limit_up_type", ""),
            "封板成功率": it.get("limit_up_suc_rate"), "炸板次数": it.get("open_num") or 0,
            "封单额": it.get("order_amount"), "连板": board_label,
            "首封时间": datetime.fromtimestamp(int(ft)).strftime("%H:%M:%S") if ft else "",
            "是否回封": it.get("is_again_limit"),
            "数据日期": normalized_date, "交易状态": trade_status,
        })
    df = pd.DataFrame(rows, columns=_THS_LIMIT_UP_COLUMNS)
    unknown_board_count = sum(
        not _has_parseable_ths_board_count(value) for value in board_labels
    )
    board_count_coverage = (
        (total - unknown_board_count) / total if total else 1.0
    )
    df.attrs.update({
        "data_date": normalized_date,
        "trade_status": trade_status,
        "pool_total": total,
        "unique_total": len(info),
        "reason_coverage": 1.0,
        "board_count_coverage": board_count_coverage,
        "unknown_board_count": unknown_board_count,
        "page_count": page_count,
        "source_valid": True,
        "valid_empty": total == 0,
    })
    return df


def fetch_ths_hot_list(period: str = "hour", **kwargs) -> pd.DataFrame:
    """同花顺热榜：人气值 + 概念标签 + 排名变化。period: hour/day。

    Returns columns: 排名 / 代码 / 名称 / 人气值 / 涨幅% / 排名变化 / 概念标签 / 热度标签
    """
    r = _get("https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock",
             params={"stock_type": "a", "type": period, "list_type": "normal"}, timeout=10)
    lst = (r.json().get("data") or {}).get("stock_list") or []
    rows = []
    for it in lst:
        tag = it.get("tag") or {}
        rows.append({
            "排名": it.get("order"), "代码": it.get("code"), "名称": it.get("name"),
            "人气值": it.get("rate"), "涨幅%": it.get("rise_and_fall"),
            "排名变化": it.get("hot_rank_chg"),
            "概念标签": tag.get("concept_tag") or [],
            "热度标签": tag.get("popularity_tag", ""),
        })
    return pd.DataFrame(rows)
