"""Pure V1 domain model for the dashboard risk-appetite panel.

The module deliberately performs no I/O.  Callers provide full industry and
concept universes so that price and fund-flow percentiles are not biased by a
top-N response.  ETF and leader data are display context only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from math import isfinite
from typing import Any, Iterable, Mapping, Sequence

from .sector_attribution import (
    ATTRIBUTION_TAXONOMY_VERSION,
    attribute_limit_up_records,
)


Record = Mapping[str, Any]

STRONG = "strong"
MEDIUM = "medium"
WEAK = "weak"
UNKNOWN = "unknown"

LEVEL_LABELS = {
    STRONG: "强",
    MEDIUM: "中",
    WEAK: "弱",
    UNKNOWN: "数据不足",
}

CONFIG_VERSION = "risk-appetite-v1.3"
DEFENSE_FOCUS_VERSION = "defense-focus-v1"


@dataclass(frozen=True)
class LeaderDefinition:
    name: str
    code: str


@dataclass(frozen=True)
class SectorDefinition:
    key: str
    label: str
    layer: str
    industry_aliases: tuple[str, ...]
    concept_aliases: tuple[str, ...]
    etf_keywords: tuple[str, ...]
    leaders: tuple[LeaderDefinition, ...]


# Aliases are exact board names.  A caller may trim surrounding whitespace,
# but substring matching is intentionally reserved for ETF display selection.
SECTOR_DEFINITIONS: tuple[SectorDefinition, ...] = (
    SectorDefinition(
        "bank",
        "银行",
        "bank_support",
        ("银行", "银行行业"),
        (),
        ("银行ETF", "银行"),
        (LeaderDefinition("工商银行", "601398"), LeaderDefinition("招商银行", "600036")),
    ),
    SectorDefinition(
        "securities",
        "证券",
        "financial_core",
        ("证券", "证券行业", "券商", "证券Ⅱ", "证券Ⅲ"),
        ("券商概念",),
        ("证券ETF", "券商ETF", "证券", "券商"),
        (LeaderDefinition("中信证券", "600030"), LeaderDefinition("华泰证券", "601688")),
    ),
    SectorDefinition(
        "internet_finance",
        "互联网金融",
        "financial_core",
        (),
        ("互联网金融", "金融科技"),
        ("金融科技ETF", "互联网金融", "金融科技"),
        (LeaderDefinition("东方财富", "300059"), LeaderDefinition("恒生电子", "600570")),
    ),
    SectorDefinition(
        "oil_gas",
        "油气",
        "event_hedge",
        ("油气", "石油行业", "油气开采", "油气开采及服务", "油服工程"),
        ("油气改革",),
        ("油气ETF", "石化ETF", "油气", "石油"),
        (LeaderDefinition("中国石油", "601857"), LeaderDefinition("中国石化", "600028")),
    ),
    SectorDefinition(
        "agriculture",
        "农业",
        "event_hedge",
        ("农业", "农牧饲渔", "种植业", "农业综合Ⅱ", "农业综合Ⅲ"),
        ("农业种植", "生态农业"),
        ("农业ETF", "养殖ETF", "农业", "畜牧"),
        (LeaderDefinition("牧原股份", "002714"), LeaderDefinition("海大集团", "002311")),
    ),
    SectorDefinition(
        "nonferrous",
        "有色金属",
        "cyclical_resource",
        ("有色金属",),
        (),
        ("有色金属ETF", "有色ETF", "有色"),
        (LeaderDefinition("紫金矿业", "601899"), LeaderDefinition("中国铝业", "601600")),
    ),
    SectorDefinition(
        "rare_earth",
        "稀土",
        "cyclical_resource",
        (),
        ("稀土", "稀土永磁"),
        ("稀土ETF", "稀土"),
        (LeaderDefinition("北方稀土", "600111"), LeaderDefinition("中国稀土", "000831")),
    ),
    SectorDefinition(
        "precious_metals",
        "贵金属",
        "event_hedge",
        ("贵金属",),
        (),
        ("黄金ETF", "贵金属ETF", "黄金产业", "黄金", "贵金属"),
        (LeaderDefinition("山东黄金", "600547"), LeaderDefinition("中金黄金", "600489")),
    ),
    SectorDefinition(
        "electric_power",
        "电力",
        "cashflow_defense",
        ("电力", "电力行业"),
        (),
        ("电力ETF", "绿电ETF", "电力"),
        (LeaderDefinition("长江电力", "600900"), LeaderDefinition("华能国际", "600011")),
    ),
    SectorDefinition(
        "retail",
        "零售",
        "consumer_stability",
        ("零售", "商业百货", "一般零售", "百货"),
        ("零售概念", "新零售"),
        ("零售ETF", "消费ETF", "零售"),
        (LeaderDefinition("永辉超市", "601933"), LeaderDefinition("王府井", "600859")),
    ),
    SectorDefinition(
        "baijiu",
        "白酒",
        "consumer_stability",
        (),
        ("白酒",),
        ("白酒ETF", "酒ETF", "白酒"),
        (LeaderDefinition("贵州茅台", "600519"), LeaderDefinition("五粮液", "000858")),
    ),
)


# Supplemental observations can be surfaced without changing the legacy risk
# groups or headline classification.  Keep these definitions separate until a
# later, explicitly reviewed model change promotes them into SECTOR_DEFINITIONS.
DEFENSE_FOCUS_DEFINITIONS: tuple[SectorDefinition, ...] = (
    SectorDefinition(
        "ports",
        "港口",
        "transport_defense",
        ("港口",),
        (),
        ("交通运输ETF", "物流ETF", "港口"),
        (LeaderDefinition("上港集团", "600018"), LeaderDefinition("宁波港", "601018")),
    ),
)


_NAME_FIELDS = ("name", "板块", "板块名称", "行业名称", "名称")
_CHANGE_FIELDS = ("change_pct", "涨跌幅")
_PRICE_FIELDS = ("price", "最新点位", "最新价")
_CODE_FIELDS = ("code", "板块代码", "代码")
_UP_FIELDS = ("up_count", "上涨家数", "上涨股数", "上涨")
_DOWN_FIELDS = ("down_count", "下跌家数", "下跌股数", "下跌")
_FLOW_FIELDS = (
    "main_net_ratio",
    "主力净流入-占比",
    "主力净流入占比",
    "主力净流入比例",
)
_FLOW_AMOUNT_FIELDS = ("main_net_amount", "主力净流入")
_FLOW_RANK_FIELDS = ("main_net_rank", "主力净流入排名")
_PROVIDER_TIME_FIELDS = ("provider_as_of", "更新时间")
_AMOUNT_FIELDS = ("amount", "成交额", "成交额(元)")


def _field(record: Record | None, fields: Sequence[str]) -> Any:
    if record is None:
        return None
    for field in fields:
        if field in record:
            value = record[field]
            if value is not None and value != "":
                return value
    return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value.endswith("%"):
            value = value[:-1]
        if value in {"", "-", "--", "None", "null"}:
            return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _record_name(record: Record) -> str | None:
    value = _field(record, _NAME_FIELDS)
    if value is None:
        return None
    name = str(value).strip()
    return name or None


def _records_by_name(records: Sequence[Record]) -> dict[str, Record]:
    result: dict[str, Record] = {}
    for record in records:
        name = _record_name(record)
        if name is not None and name not in result:
            result[name] = record
    return result


def _find_exact(index: Mapping[str, Record], aliases: Sequence[str]) -> tuple[str | None, Record | None]:
    for alias in aliases:
        if alias in index:
            return alias, index[alias]
    return None, None


def percentile_rank(value: float | None, universe: Iterable[float | None]) -> float | None:
    """Return an inclusive, tie-adjusted percentile in the range [0, 1]."""

    if value is None:
        return None
    values = sorted(item for item in universe if item is not None and isfinite(item))
    if not values:
        return None
    if len(values) == 1:
        return 0.5
    less = sum(item < value for item in values)
    equal = sum(item == value for item in values)
    if equal == 0:
        # The observed value may come from an overlay that is not in the supplied
        # universe.  Insertion rank preserves the endpoint behaviour.
        return min(1.0, max(0.0, less / len(values)))
    mid_rank = less + (equal - 1) / 2
    return mid_rank / (len(values) - 1)


def _metric_universe(
    quote_records: Sequence[Record],
    flow_records: Sequence[Record],
    fields: Sequence[str],
) -> list[float | None]:
    quote_index = _records_by_name(quote_records)
    flow_index = _records_by_name(flow_records)
    names = set(quote_index) | set(flow_index)
    return [
        _number(_field(flow_index.get(name), fields))
        if _number(_field(flow_index.get(name), fields)) is not None
        else _number(_field(quote_index.get(name), fields))
        for name in names
    ]


def _evidence_level(value: float | None, *, high: float, low: float) -> str | None:
    if value is None:
        return None
    if value >= high:
        return STRONG
    if value <= low:
        return WEAK
    return MEDIUM


def _vote_level(votes: Iterable[str | None], minimum_valid: int = 2) -> str:
    valid = [vote for vote in votes if vote is not None]
    if len(valid) < minimum_valid:
        return UNKNOWN
    strong_count = valid.count(STRONG)
    weak_count = valid.count(WEAK)
    if strong_count >= 2 and strong_count > weak_count:
        return STRONG
    if weak_count >= 2 and weak_count > strong_count:
        return WEAK
    return MEDIUM


def _aggregate_sector_levels(levels: Iterable[str], minimum_valid: int) -> str:
    valid = [level for level in levels if level != UNKNOWN]
    if len(valid) < minimum_valid:
        return UNKNOWN
    strong_needed = len(valid) // 2 + 1
    if valid.count(STRONG) >= strong_needed:
        return STRONG
    if valid.count(WEAK) >= strong_needed:
        return WEAK
    return MEDIUM


def _select_etf(definition: SectorDefinition, etf_records: Sequence[Record]) -> dict[str, Any] | None:
    candidates: list[tuple[float, int, Record]] = []
    for position, record in enumerate(etf_records):
        name = _record_name(record)
        if name is None or not any(keyword in name for keyword in definition.etf_keywords):
            continue
        amount = _number(_field(record, _AMOUNT_FIELDS))
        candidates.append((amount if amount is not None else -1.0, -position, record))
    if not candidates:
        return None
    selected = max(candidates, key=lambda item: (item[0], item[1]))[2]
    return dict(selected)


def _sector_signal(
    definition: SectorDefinition,
    industry_records: Sequence[Record],
    concept_records: Sequence[Record],
    industry_flow_records: Sequence[Record],
    concept_flow_records: Sequence[Record],
    industry_change_universe: Sequence[float | None],
    concept_change_universe: Sequence[float | None],
    industry_flow_universe: Sequence[float | None],
    concept_flow_universe: Sequence[float | None],
    etf_records: Sequence[Record],
    leadership_signal: Mapping[str, Any] | None,
    leadership_vote_enabled: bool,
) -> dict[str, Any]:
    industry_index = _records_by_name(industry_records)
    concept_index = _records_by_name(concept_records)
    industry_flow_index = _records_by_name(industry_flow_records)
    concept_flow_index = _records_by_name(concept_flow_records)

    taxonomy: str | None = None
    alias: str | None = None
    quote: Record | None = None
    flow: Record | None = None
    change_universe: Sequence[float | None] = ()
    flow_universe: Sequence[float | None] = ()

    alias, quote = _find_exact(industry_index, definition.industry_aliases)
    if quote is not None:
        taxonomy = "industry"
        _, flow = _find_exact(industry_flow_index, definition.industry_aliases)
        change_universe = industry_change_universe
        flow_universe = industry_flow_universe
    else:
        alias, quote = _find_exact(concept_index, definition.concept_aliases)
        if quote is not None:
            taxonomy = "concept"
            _, flow = _find_exact(concept_flow_index, definition.concept_aliases)
            change_universe = concept_change_universe
            flow_universe = concept_flow_universe

    # A flow-only match is useful, but it must stay in its configured taxonomy.
    if quote is None:
        alias, flow = _find_exact(industry_flow_index, definition.industry_aliases)
        if flow is not None:
            taxonomy = "industry"
            flow_universe = industry_flow_universe
            change_universe = industry_change_universe
        else:
            alias, flow = _find_exact(concept_flow_index, definition.concept_aliases)
            if flow is not None:
                taxonomy = "concept"
                flow_universe = concept_flow_universe
                change_universe = concept_change_universe

    # Prefer the quote row because price, breadth and fund fields share one
    # provider watermark. The slower flow table is only a compatibility fallback.
    quote_has_flow = _number(_field(quote, _FLOW_FIELDS)) is not None
    flow = quote if quote_has_flow else (flow or quote)
    change_pct = _number(_field(quote, _CHANGE_FIELDS))
    index_level = _number(_field(quote, _PRICE_FIELDS))
    board_code = _field(quote, _CODE_FIELDS)
    up_count = _number(_field(quote, _UP_FIELDS))
    down_count = _number(_field(quote, _DOWN_FIELDS))
    main_net_ratio = _number(_field(flow, _FLOW_FIELDS))

    price_percentile = percentile_rank(change_pct, change_universe)
    flow_percentile = percentile_rank(main_net_ratio, flow_universe)
    breadth_ratio = None
    if up_count is not None and down_count is not None and up_count + down_count > 0:
        breadth_ratio = up_count / (up_count + down_count)

    price_vote = _evidence_level(price_percentile, high=0.70, low=0.30)
    breadth_vote = _evidence_level(breadth_ratio, high=0.55, low=0.45)
    flow_vote = None
    if main_net_ratio is not None and flow_percentile is not None:
        if main_net_ratio > 0 and flow_percentile >= 0.70:
            flow_vote = STRONG
        elif main_net_ratio < 0 and flow_percentile <= 0.30:
            flow_vote = WEAK
        else:
            flow_vote = MEDIUM

    core_evidence = {
        "price": price_vote,
        "breadth": breadth_vote,
        "fund_flow": flow_vote,
    }
    core_level = _vote_level(core_evidence.values())
    leadership = dict(leadership_signal or {})
    raw_leadership_vote = leadership.get("vote")
    if leadership_vote_enabled and raw_leadership_vote == UNKNOWN:
        # An explicitly valid empty pool is neutral evidence. UNKNOWN here only
        # describes attribution without a source-validity contract.
        leadership_vote = MEDIUM
    elif leadership_vote_enabled and raw_leadership_vote in {STRONG, MEDIUM, WEAK}:
        leadership_vote = raw_leadership_vote
    else:
        leadership_vote = None
    evidence = {
        **core_evidence,
        "leadership": leadership_vote,
    }
    core_available = sum(value is not None for value in core_evidence.values())
    # A neutral leadership pool must not manufacture confidence when the quote
    # proxy itself has fewer than two usable observations.
    level = _vote_level(evidence.values()) if core_available >= 2 else UNKNOWN
    available_evidence = sum(value is not None for value in evidence.values())
    if level == STRONG and core_level == STRONG:
        strength_profile = "broad"
    elif level == STRONG and leadership_vote == STRONG:
        strength_profile = "leader_concentrated"
    elif leadership_vote == STRONG and core_level == WEAK:
        strength_profile = "core_leadership_divergence"
    elif core_level == STRONG and leadership_vote == WEAK:
        strength_profile = "broad_without_ladder"
    else:
        strength_profile = "mixed"

    leadership["vote"] = leadership_vote
    leadership["vote_enabled"] = leadership_vote_enabled

    return {
        "key": definition.key,
        "label": definition.label,
        "layer": definition.layer,
        "taxonomy": taxonomy,
        "matched_alias": alias,
        "board_code": str(board_code) if board_code is not None else None,
        "level": level,
        "level_label": LEVEL_LABELS[level],
        "core_level": core_level,
        "core_level_label": LEVEL_LABELS[core_level],
        "strength_profile": strength_profile,
        "available_evidence": available_evidence,
        "core_evidence_available": core_available,
        "evidence": evidence,
        "leadership": leadership,
        "metrics": {
            "index_level": index_level,
            "change_pct": change_pct,
            "price_percentile": price_percentile,
            "up_count": up_count,
            "down_count": down_count,
            "breadth_ratio": breadth_ratio,
            "main_net_ratio": main_net_ratio,
            "main_net_amount": _number(_field(flow, _FLOW_AMOUNT_FIELDS)),
            "main_net_rank": _number(_field(flow, _FLOW_RANK_FIELDS)),
            "fund_flow_percentile": flow_percentile,
            "provider_as_of": _field(quote, _PROVIDER_TIME_FIELDS),
        },
        "etf_keywords": list(definition.etf_keywords),
        "etf": _select_etf(definition, etf_records),
        "leaders": [
            {"name": leader.name, "code": leader.code}
            for leader in definition.leaders
        ],
    }


_INDEX_NAMES = {
    "shanghai": ("上证指数", "上证综合指数", "sh000001", "000001.SH"),
    "shenzhen": ("深证成指", "深证指数", "sz399001", "399001.SZ"),
    "csi300": ("沪深300", "sh000300", "000300.SH"),
    "chinext": ("创业板指", "sz399006", "399006.SZ"),
    "csi500": ("中证500", "sh000905", "000905.SH"),
}


def _index_change(index_records: Sequence[Record], key: str) -> float | None:
    aliases = _INDEX_NAMES[key]
    for record in index_records:
        names = {
            str(value).strip()
            for value in (
                _field(record, _NAME_FIELDS),
                record.get("code"),
                record.get("代码"),
            )
            if value is not None
        }
        if any(alias in names for alias in aliases):
            return _number(_field(record, _CHANGE_FIELDS))
    return None


def _market_breadth_vote(
    market_breadth: Record | None,
) -> tuple[str | None, dict[str, float | None]]:
    up_count = _number(_field(market_breadth, _UP_FIELDS))
    down_count = _number(_field(market_breadth, _DOWN_FIELDS))
    ratio = None
    if up_count is not None and down_count is not None and up_count + down_count > 0:
        ratio = up_count / (up_count + down_count)
    return _evidence_level(ratio, high=0.55, low=0.45), {
        "up_count": up_count,
        "down_count": down_count,
        "ratio": ratio,
    }


def _index_vote(index_records: Sequence[Record]) -> tuple[str | None, dict[str, Any]]:
    shanghai = _index_change(index_records, "shanghai")
    shenzhen = _index_change(index_records, "shenzhen")
    csi300 = _index_change(index_records, "csi300")
    elastic_values = [
        value
        for value in (
            _index_change(index_records, "chinext"),
            _index_change(index_records, "csi500"),
        )
        if value is not None
    ]
    baseline_values = [value for value in (csi300, shanghai, shenzhen) if value is not None]
    elastic_change = sum(elastic_values) / len(elastic_values) if elastic_values else None
    baseline_change = sum(baseline_values) / len(baseline_values) if baseline_values else None
    vote = None
    if (
        shanghai is not None
        and shenzhen is not None
        and elastic_change is not None
        and baseline_change is not None
    ):
        if shanghai > 0 and shenzhen > 0 and elastic_change > baseline_change:
            vote = STRONG
        elif shanghai < 0 and shenzhen < 0 and elastic_change < baseline_change:
            vote = WEAK
        else:
            vote = MEDIUM
    return vote, {
        "shanghai_change_pct": shanghai,
        "shenzhen_change_pct": shenzhen,
        "baseline_change_pct": baseline_change,
        "elastic_change_pct": elastic_change,
    }


def _turnover_vote(market_turnover: Record | None) -> tuple[str | None, dict[str, Any]]:
    if market_turnover is None or market_turnover.get("available") is False:
        return None, {"direction": None, "difference": None}
    direction = market_turnover.get("direction") or market_turnover.get("label")
    difference = _number(market_turnover.get("difference"))
    if direction in {"expand", "放量"} or (direction is None and difference is not None and difference > 0):
        vote = STRONG
    elif direction in {"shrink", "缩量"} or (direction is None and difference is not None and difference < 0):
        vote = WEAK
    elif direction is not None or difference == 0:
        vote = MEDIUM
    else:
        vote = None
    return vote, {"direction": direction, "difference": difference}


def _market_participation(
    index_records: Sequence[Record],
    market_breadth: Record | None,
    market_turnover: Record | None,
) -> dict[str, Any]:
    breadth_vote, breadth_metrics = _market_breadth_vote(market_breadth)
    index_vote, index_metrics = _index_vote(index_records)
    turnover_vote, turnover_metrics = _turnover_vote(market_turnover)
    votes = {
        "market_breadth": breadth_vote,
        "index_participation": index_vote,
        "same_time_turnover": turnover_vote,
    }
    level = _vote_level(votes.values())
    return {
        "level": level,
        "level_label": LEVEL_LABELS[level],
        "available_evidence": sum(value is not None for value in votes.values()),
        "votes": votes,
        "metrics": {
            "market_breadth": breadth_metrics,
            "indices": index_metrics,
            "same_time_turnover": turnover_metrics,
        },
    }


def _group_snapshot(sectors: Mapping[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    definitions = {
        "financial_core": ("金融核心", ("securities", "internet_finance"), 2),
        "bank_support": ("银行支撑", ("bank",), 1),
        "cashflow_defense": ("现金流防御", ("electric_power",), 1),
        "event_hedge": (
            "事件避险",
            ("oil_gas", "agriculture", "precious_metals"),
            2,
        ),
        "cyclical_resource": ("周期资源", ("nonferrous", "rare_earth"), 2),
        "consumer_stability": ("消费稳定", ("retail", "baijiu"), 2),
    }
    result: dict[str, dict[str, Any]] = {}
    for key, (label, sector_keys, minimum_valid) in definitions.items():
        level = _aggregate_sector_levels(
            (sectors[sector_key]["level"] for sector_key in sector_keys),
            minimum_valid,
        )
        result[key] = {
            "label": label,
            "sector_keys": list(sector_keys),
            "level": level,
            "level_label": LEVEL_LABELS[level],
        }
    return result


def _classify_structure(market_level: str, groups: Mapping[str, Mapping[str, Any]]) -> str:
    financial = groups["financial_core"]["level"]
    bank = groups["bank_support"]["level"]
    cashflow = groups["cashflow_defense"]["level"]
    hedge = groups["event_hedge"]["level"]
    resource = groups["cyclical_resource"]["level"]
    consumer = groups["consumer_stability"]["level"]

    if market_level == STRONG and financial == STRONG:
        return "金融进攻"
    if bank == STRONG and financial != STRONG and market_level != STRONG:
        return "银行护盘"
    if resource == STRONG:
        return "资源通胀"
    if market_level != STRONG and (cashflow == STRONG or hedge == STRONG):
        return "防御升温"
    if consumer == STRONG:
        return "消费修复"
    if market_level == STRONG:
        return "其他主线"
    protective = any(level == STRONG for level in (cashflow, hedge, resource, consumer))
    if market_level == WEAK and not protective:
        return "普遍退潮"
    return "分化观察"


def _parse_as_of(as_of: datetime | str | None) -> tuple[str | None, time | None]:
    if as_of is None:
        return None, None
    if isinstance(as_of, datetime):
        return as_of.isoformat(timespec="seconds"), as_of.timetz().replace(tzinfo=None)
    value = str(as_of).strip()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed_time = time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("as_of must be a datetime or ISO date/time string") from exc
        return value, parsed_time.replace(tzinfo=None)
    return parsed.isoformat(timespec="seconds"), parsed.timetz().replace(tzinfo=None)


def build_risk_appetite_snapshot(
    industry_records: Iterable[Record] = (),
    concept_records: Iterable[Record] = (),
    *,
    industry_flow_records: Iterable[Record] = (),
    concept_flow_records: Iterable[Record] = (),
    index_records: Iterable[Record] = (),
    market_breadth: Record | None = None,
    market_turnover: Record | None = None,
    etf_records: Iterable[Record] = (),
    leadership_records: Iterable[Record] = (),
    leadership_source_status: Mapping[str, Any] | None = None,
    trade_date: str | None = None,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    """Build a deterministic risk-appetite snapshot from ordinary records.

    Full industry/concept universes should be supplied.  Missing fields remain
    missing and never become zero.  ETF records and configured leaders are
    returned for display but are excluded from all evidence votes.
    """

    industries = tuple(industry_records)
    concepts = tuple(concept_records)
    industry_flows = tuple(industry_flow_records)
    concept_flows = tuple(concept_flow_records)
    indices = tuple(index_records)
    etfs = tuple(etf_records)
    limit_up_records = tuple(leadership_records)

    leadership_status = dict(leadership_source_status or {})
    leadership_vote_enabled = bool(leadership_status.get("eligible_for_vote"))
    source_date = leadership_status.get("data_date")
    if trade_date and source_date and str(source_date).replace("-", "") != trade_date.replace("-", ""):
        leadership_vote_enabled = False
        leadership_status["reason"] = "wrong_trade_date"
    leadership_status["eligible_for_vote"] = leadership_vote_enabled
    attribution = attribute_limit_up_records(limit_up_records)

    industry_change_universe = [_number(_field(record, _CHANGE_FIELDS)) for record in industries]
    concept_change_universe = [_number(_field(record, _CHANGE_FIELDS)) for record in concepts]
    industry_flow_universe = _metric_universe(industries, industry_flows, _FLOW_FIELDS)
    concept_flow_universe = _metric_universe(concepts, concept_flows, _FLOW_FIELDS)

    sectors = {
        definition.key: _sector_signal(
            definition,
            industries,
            concepts,
            industry_flows,
            concept_flows,
            industry_change_universe,
            concept_change_universe,
            industry_flow_universe,
            concept_flow_universe,
            etfs,
            attribution.get("sectors", {}).get(definition.key),
            leadership_vote_enabled,
        )
        for definition in SECTOR_DEFINITIONS
    }
    defense_focus_sectors = {
        definition.key: _sector_signal(
            definition,
            industries,
            concepts,
            industry_flows,
            concept_flows,
            industry_change_universe,
            concept_change_universe,
            industry_flow_universe,
            concept_flow_universe,
            etfs,
            None,
            False,
        )
        for definition in DEFENSE_FOCUS_DEFINITIONS
    }

    if leadership_status.get("source_valid"):
        ranked = sorted(
            sectors.values(),
            key=lambda item: (
                -int(item.get("leadership", {}).get("matched_count") or 0),
                -int(item.get("leadership", {}).get("max_board_count") or 0),
                item["key"],
            ),
        )
        previous_score: tuple[int, int] | None = None
        rank = 0
        for position, sector in enumerate(ranked, start=1):
            signal = sector["leadership"]
            score = (
                int(signal.get("matched_count") or 0),
                int(signal.get("max_board_count") or 0),
            )
            if score != previous_score:
                rank = position
                previous_score = score
            signal["leadership_rank"] = rank
            signal["leadership_rank_total"] = len(ranked)
    market = _market_participation(indices, market_breadth, market_turnover)
    groups = _group_snapshot(sectors)
    structure = _classify_structure(market["level"], groups)
    emotion = {
        STRONG: "积极",
        WEAK: "谨慎",
        MEDIUM: "中性",
        UNKNOWN: "中性",
    }[market["level"]]

    as_of_value, local_time = _parse_as_of(as_of)
    opening_observation = (
        local_time is not None
        and time(9, 25) <= local_time < time(9, 45)
    )
    phase = "opening_observation" if opening_observation else "regular"
    if local_time is None:
        phase = "unknown"

    sector_evidence_total = len(SECTOR_DEFINITIONS) * 4
    sector_evidence_available = sum(item["available_evidence"] for item in sectors.values())
    total_evidence = sector_evidence_total + 3
    available_evidence = sector_evidence_available + market["available_evidence"]
    coverage = available_evidence / total_evidence
    data_quality = "high" if coverage >= 0.80 else "medium" if coverage >= 0.50 else "low"

    return {
        "version": CONFIG_VERSION,
        "attribution_version": ATTRIBUTION_TAXONOMY_VERSION,
        "trade_date": trade_date,
        "as_of": as_of_value,
        "phase": phase,
        "opening_observation": opening_observation,
        "emotion": emotion,
        "structure": structure,
        "market_participation": market,
        "groups": groups,
        "sectors": sectors,
        "defense_focus_version": DEFENSE_FOCUS_VERSION,
        "defense_focus_sectors": defense_focus_sectors,
        "leadership_pool": {
            "taxonomy_version": attribution.get(
                "taxonomy_version", ATTRIBUTION_TAXONOMY_VERSION
            ),
            "pool_total": attribution.get("pool_total", len(limit_up_records)),
            "reason_coverage": attribution.get("reason_coverage"),
            "industry_profile_coverage": attribution.get("industry_profile_coverage"),
            "unmapped_tags": attribution.get("unmapped_tags", []),
            "unmapped_industries": attribution.get("unmapped_industries", []),
            "status": leadership_status,
        },
        "data_quality": {
            "level": data_quality,
            "coverage": coverage,
            "available_evidence": available_evidence,
            "total_evidence": total_evidence,
        },
    }


__all__ = [
    "CONFIG_VERSION",
    "DEFENSE_FOCUS_DEFINITIONS",
    "DEFENSE_FOCUS_VERSION",
    "ATTRIBUTION_TAXONOMY_VERSION",
    "LEVEL_LABELS",
    "SECTOR_DEFINITIONS",
    "LeaderDefinition",
    "SectorDefinition",
    "build_risk_appetite_snapshot",
    "percentile_rank",
]
