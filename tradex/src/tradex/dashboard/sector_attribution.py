"""Deterministic limit-up attribution for the risk-appetite sectors.

The module is deliberately I/O free.  It separates exact, versioned taxonomy
rules from record aggregation so the same rules can attribute both editorial
limit-up reasons and static stock-board names.  Unregistered text never falls
back to substring matching.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


Record = Mapping[str, Any]

ATTRIBUTION_TAXONOMY_VERSION = "sector-attribution-v2"

STRONG = "strong"
MEDIUM = "medium"
UNKNOWN = "unknown"

SECTOR_LABELS: dict[str, str] = {
    "bank": "银行",
    "securities": "证券",
    "internet_finance": "互联网金融",
    "oil_gas": "油气",
    "agriculture": "农业",
    "nonferrous": "有色金属",
    "rare_earth": "稀土",
    "precious_metals": "贵金属",
    "electric_power": "电力",
    "retail": "零售",
    "baijiu": "白酒",
}
SECTOR_KEYS: tuple[str, ...] = tuple(SECTOR_LABELS)


# Each entry is (canonical tag, sector key, chain node, exact aliases).  Keep
# aliases here instead of scattering special cases through the scoring code.
_RULE_DEFINITIONS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "商业银行",
        "bank",
        "银行",
        (
            "银行",
            "银行行业",
            "银行Ⅱ",
            "银行Ⅲ",
            "商业银行",
            "国有大型银行",
            "股份制银行",
            "城商行",
            "城市商业银行",
            "农商行",
            "农村商业银行",
        ),
    ),
    (
        "证券",
        "securities",
        "券商",
        ("证券", "证券行业", "证券Ⅱ", "证券Ⅲ", "券商", "券商概念"),
    ),
    (
        "互联网金融",
        "internet_finance",
        "互联网金融",
        ("互联网金融", "互联网金融概念"),
    ),
    (
        "金融科技",
        "internet_finance",
        "金融科技",
        ("金融科技", "金融科技概念"),
    ),
    (
        "石油产业",
        "oil_gas",
        "石油产业",
        ("石油", "石油行业", "油气", "油气改革"),
    ),
    (
        "油气开采",
        "oil_gas",
        "油气开采",
        ("油气开采", "油气开采Ⅱ", "油气开采及服务"),
    ),
    (
        "油田服务",
        "oil_gas",
        "油田服务",
        ("油服", "油服工程", "油田服务"),
    ),
    (
        "石油炼化",
        "oil_gas",
        "石油炼化",
        ("石化", "炼油", "石油炼化", "炼化及贸易"),
    ),
    (
        "原油物流",
        "oil_gas",
        "石油物流",
        ("原油中转", "原油运输", "原油仓储"),
    ),
    (
        "农业",
        "agriculture",
        "农业综合",
        ("农业", "农牧饲渔", "农业综合", "农业综合Ⅱ", "农业综合Ⅲ"),
    ),
    (
        "粮食",
        "agriculture",
        "粮食产业",
        ("粮食", "粮食概念", "粮食安全", "粮油"),
    ),
    (
        "种业",
        "agriculture",
        "种业",
        ("种业", "种子", "玉米种业", "转基因"),
    ),
    (
        "农业种植",
        "agriculture",
        "种植",
        ("农业种植", "种植业", "种植", "生态农业", "林业Ⅱ"),
    ),
    (
        "养殖",
        "agriculture",
        "养殖",
        (
            "养殖",
            "养殖业",
            "畜牧养殖",
            "生猪养殖",
            "禽类养殖",
            "水产养殖",
            "渔业",
        ),
    ),
    (
        "饲料",
        "agriculture",
        "饲料",
        ("饲料", "饲料加工"),
    ),
    (
        "农产品加工",
        "agriculture",
        "农产品加工",
        ("农产品加工",),
    ),
    (
        "化肥",
        "agriculture",
        "农业投入品",
        ("化肥", "复合肥", "氮肥", "磷肥", "钾肥"),
    ),
    (
        "农药",
        "agriculture",
        "农业投入品",
        ("农药", "农化制品"),
    ),
    (
        "兽药",
        "agriculture",
        "农业投入品",
        ("兽药", "动物保健Ⅱ"),
    ),
    (
        "农业机械",
        "agriculture",
        "农机",
        ("农机", "农业机械"),
    ),
    (
        "农业生产资料",
        "agriculture",
        "农资",
        ("农资", "农业生产资料"),
    ),
    (
        "节水灌溉",
        "agriculture",
        "节水灌溉",
        ("节水灌溉",),
    ),
    (
        "有色金属",
        "nonferrous",
        "有色金属",
        ("有色金属", "有色"),
    ),
    (
        "工业金属",
        "nonferrous",
        "工业金属",
        ("工业金属", "铜", "铝", "铅锌"),
    ),
    (
        "铝加工",
        "nonferrous",
        "铝加工",
        ("铝型材", "铝挤压模具", "铝加工"),
    ),
    (
        "小金属",
        "nonferrous",
        "小金属",
        ("小金属",),
    ),
    (
        "金属新材料",
        "nonferrous",
        "金属新材料",
        ("金属新材料",),
    ),
    (
        "稀土",
        "rare_earth",
        "稀土产业",
        ("稀土", "稀土概念", "稀土产业", "稀土永磁", "钕铁硼"),
    ),
    (
        "贵金属",
        "precious_metals",
        "贵金属",
        ("贵金属",),
    ),
    (
        "黄金",
        "precious_metals",
        "黄金",
        ("黄金", "黄金概念", "黄金产业", "金矿"),
    ),
    (
        "白银",
        "precious_metals",
        "白银",
        ("白银", "白银概念"),
    ),
    (
        "电力",
        "electric_power",
        "电力生产",
        ("电力", "电力行业", "电力生产", "电力改革"),
    ),
    (
        "火电",
        "electric_power",
        "火电",
        ("火电", "火力发电"),
    ),
    (
        "水电",
        "electric_power",
        "水电",
        ("水电", "水力发电"),
    ),
    (
        "核电运营",
        "electric_power",
        "核电",
        ("核电运营", "核能发电"),
    ),
    (
        "绿色电力",
        "electric_power",
        "绿色电力",
        ("绿电", "绿色电力"),
    ),
    (
        "零售",
        "retail",
        "零售",
        ("零售", "零售概念", "一般零售", "新零售", "专业连锁Ⅱ"),
    ),
    (
        "百货",
        "retail",
        "百货",
        ("百货", "商业百货"),
    ),
    (
        "超市",
        "retail",
        "超市",
        ("超市", "连锁超市"),
    ),
    (
        "白酒",
        "baijiu",
        "白酒",
        ("白酒", "白酒Ⅱ", "白酒概念", "白酒生产"),
    ),
)


CANONICAL_TAG_RULES: dict[str, dict[str, str]] = {
    canonical_tag: {
        "sector_key": sector_key,
        "chain_node": chain_node,
        "rule_id": f"{sector_key}.{canonical_tag}",
    }
    for canonical_tag, sector_key, chain_node, _aliases in _RULE_DEFINITIONS
}

TAG_ALIASES: dict[str, str] = {
    alias: canonical_tag
    for canonical_tag, _sector_key, _chain_node, aliases in _RULE_DEFINITIONS
    for alias in aliases
}

_REASON_SEPARATOR = re.compile(r"[+＋、，,；;|/]")
_DAY_BOARD_PATTERN = re.compile(r"^(?P<days>\d+)天(?P<boards>\d+)板$")
_STREAK_BOARD_PATTERN = re.compile(r"^(?P<boards>\d+)连板$")
_BOARD_PATTERN = re.compile(r"^(?P<boards>\d+)板$")

_CODE_FIELDS = ("code", "代码", "股票代码")
_NAME_FIELDS = ("name", "名称", "股票名称")
_REASON_FIELDS = ("raw_reason", "涨停原因", "reason", "题材归因")
_BOARD_LABEL_FIELDS = ("board_label", "连板", "high_days", "连板描述")
_BOARD_COUNT_FIELDS = ("board_count", "连板数", "连板天数")
_ORDER_AMOUNT_FIELDS = ("order_amount", "封单额")

# A downstream industry label alone does not prove that the stock is trading
# on an agricultural theme today.  The same exact token remains valid when it
# appears in the provider's current limit-up reason.
_INDUSTRY_PROFILE_AUDIT_ONLY_TAGS = frozenset({"农产品加工"})


def _normalize_tag(value: Any) -> str:
    if value is None:
        return ""
    # Taxonomy labels do not use meaningful whitespace.  Removing all Unicode
    # whitespace also makes provider variants such as " 复合肥 " deterministic.
    return "".join(str(value).split())


def _first_value(record: Record, fields: Sequence[str]) -> Any:
    for field in fields:
        if field in record:
            value = record[field]
            if value is not None and value != "":
                return value
    return None


def _unique(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def parse_reason_tags(reason: Any) -> list[str]:
    """Split an editorial reason using only the registered separators.

    The returned tokens are whitespace-normalized, but otherwise unchanged.
    In particular, this function does not infer tags from arbitrary substrings.
    """

    if reason is None:
        return []
    text = str(reason)
    if not text.strip():
        return []
    return _unique(
        token
        for part in _REASON_SEPARATOR.split(text)
        if (token := _normalize_tag(part))
    )


def parse_board_count(value: Any) -> int | None:
    """Return the semantic board count from a provider streak label.

    ``3天2板`` is two boards, not three.  Strict full-label patterns prevent an
    unrelated first number from being mistaken for the streak count.
    """

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() and value > 0 else None

    label = _normalize_tag(value)
    if label == "首板":
        return 1
    for pattern in (_DAY_BOARD_PATTERN, _STREAK_BOARD_PATTERN, _BOARD_PATTERN):
        match = pattern.fullmatch(label)
        if match is not None:
            count = int(match.group("boards"))
            return count if count > 0 else None
    return None


def attribute_tokens(tokens: Iterable[Any] | Any, *, source: str = "reason") -> dict[str, Any]:
    """Attribute already separated tags with exact, registered aliases only."""

    if isinstance(tokens, (str, bytes)) or tokens is None:
        values: Iterable[Any] = [] if tokens is None else (tokens,)
    else:
        try:
            values = iter(tokens)
        except TypeError:
            values = (tokens,)

    normalized_tags = _unique(
        tag for value in values if (tag := _normalize_tag(value))
    )
    matched_tags: list[str] = []
    attributions: list[dict[str, str]] = []
    unmapped_tags: list[str] = []

    for tag in normalized_tags:
        if source == "industry_profile" and tag in _INDUSTRY_PROFILE_AUDIT_ONLY_TAGS:
            unmapped_tags.append(tag)
            continue
        canonical_tag = TAG_ALIASES.get(tag)
        if canonical_tag is None:
            unmapped_tags.append(tag)
            continue
        rule = CANONICAL_TAG_RULES[canonical_tag]
        matched_tags.append(tag)
        attributions.append(
            {
                "matched_tag": tag,
                "canonical_tag": canonical_tag,
                "sector_key": rule["sector_key"],
                "chain_node": rule["chain_node"],
                "rule_id": rule["rule_id"],
                "source": str(source),
            }
        )

    return {
        "taxonomy_version": ATTRIBUTION_TAXONOMY_VERSION,
        "matched_tags": matched_tags,
        "attributions": attributions,
        "unmapped_tags": unmapped_tags,
    }


def attribute_reason(reason: Any) -> dict[str, Any]:
    """Parse and attribute one editorial reason."""

    parsed_tags = parse_reason_tags(reason)
    result = attribute_tokens(parsed_tags, source="reason")
    return {"parsed_tags": parsed_tags, **result}


def attribute_board_names(board_names: Iterable[Any] | Any) -> dict[str, Any]:
    """Attribute static stock-board names for independent corroboration."""

    if isinstance(board_names, (str, bytes)) or board_names is None:
        values: Iterable[Any] = [] if board_names is None else (board_names,)
    else:
        try:
            values = iter(board_names)
        except TypeError:
            values = (board_names,)

    parsed_tags: list[str] = []
    for value in values:
        parsed_tags.extend(parse_reason_tags(value))
    parsed_tags = _unique(parsed_tags)
    result = attribute_tokens(parsed_tags, source="stock_board")
    return {"parsed_tags": parsed_tags, **result}


def _normalize_code(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    if text.isdigit() and len(text) <= 6:
        return text.zfill(6)
    return text


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _attribution_key(attribution: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        str(attribution.get("matched_tag", "")),
        str(attribution.get("canonical_tag", "")),
        str(attribution.get("sector_key", "")),
        str(attribution.get("chain_node", "")),
        str(attribution.get("rule_id", "")),
        str(attribution.get("source", "")),
    )


def _merge_stock_record(current: dict[str, Any], incoming: dict[str, Any]) -> None:
    if not current["code"] and incoming["code"]:
        current["code"] = incoming["code"]
    if not current["name"] and incoming["name"]:
        current["name"] = incoming["name"]

    for reason in incoming["raw_reasons"]:
        if reason and reason not in current["raw_reasons"]:
            current["raw_reasons"].append(reason)
    current["parsed_tags"] = _unique([*current["parsed_tags"], *incoming["parsed_tags"]])
    current["matched_tags"] = _unique([*current["matched_tags"], *incoming["matched_tags"]])
    current["unmapped_industries"] = _unique([
        *current["unmapped_industries"],
        *incoming["unmapped_industries"],
    ])

    existing_attributions = {_attribution_key(item) for item in current["attributions"]}
    for attribution in incoming["attributions"]:
        key = _attribution_key(attribution)
        if key not in existing_attributions:
            current["attributions"].append(attribution)
            existing_attributions.add(key)

    current_count = current["board_count"] or 0
    incoming_count = incoming["board_count"] or 0
    if incoming_count > current_count:
        current["board_count"] = incoming["board_count"]
        current["board_label"] = incoming["board_label"]
    elif not current["board_label"] and incoming["board_label"]:
        current["board_label"] = incoming["board_label"]

    incoming_amount = incoming["order_amount"]
    current_amount = current["order_amount"]
    if incoming_amount is not None and (current_amount is None or incoming_amount > current_amount):
        current["order_amount"] = incoming_amount

    if current["sector_profile"] is None and incoming["sector_profile"] is not None:
        current["sector_profile"] = incoming["sector_profile"]
    elif current["sector_profile"] is not None and incoming["sector_profile"] is not None:
        current["sector_profile"]["concept_tags"] = _unique([
            *current["sector_profile"].get("concept_tags", []),
            *incoming["sector_profile"].get("concept_tags", []),
        ])


def _record_identity(record: Record, index: int) -> tuple[str, str]:
    code = _normalize_code(_first_value(record, _CODE_FIELDS))
    if code:
        return "code", code
    name = str(_first_value(record, _NAME_FIELDS) or "").strip()
    if name:
        return "name", name
    return "row", str(index)


def _prepare_stock(record: Record) -> dict[str, Any]:
    code = _normalize_code(_first_value(record, _CODE_FIELDS))
    name = str(_first_value(record, _NAME_FIELDS) or "").strip()
    raw_value = _first_value(record, _REASON_FIELDS)
    raw_reason = "" if raw_value is None else str(raw_value).strip()
    reason_attribution = attribute_reason(raw_reason)
    raw_profile = record.get("sector_profile")
    profile = raw_profile if isinstance(raw_profile, Mapping) else {}
    industry = _normalize_tag(profile.get("industry"))
    industry_attribution = attribute_tokens(
        (industry,) if industry else (),
        source="industry_profile",
    )
    concept_tags_value = profile.get("concept_tags")
    if isinstance(concept_tags_value, (str, bytes)):
        concept_tags = parse_reason_tags(concept_tags_value)
    elif isinstance(concept_tags_value, Iterable):
        concept_tags = _unique(
            tag
            for value in concept_tags_value
            if (tag := _normalize_tag(value))
        )
    else:
        concept_tags = []

    board_label_value = _first_value(record, _BOARD_LABEL_FIELDS)
    board_label = "" if board_label_value is None else str(board_label_value).strip()
    explicit_count = parse_board_count(_first_value(record, _BOARD_COUNT_FIELDS))
    board_count = explicit_count if explicit_count is not None else parse_board_count(board_label_value)

    return {
        "code": code,
        "name": name,
        "board_count": board_count,
        "board_label": board_label,
        "raw_reasons": [raw_reason] if raw_reason else [],
        "parsed_tags": reason_attribution["parsed_tags"],
        "matched_tags": _unique([
            *reason_attribution["matched_tags"],
            *industry_attribution["matched_tags"],
        ]),
        "attributions": [
            *reason_attribution["attributions"],
            *industry_attribution["attributions"],
        ],
        "unmapped_tags": reason_attribution["unmapped_tags"],
        "unmapped_industries": industry_attribution["unmapped_tags"],
        "sector_profile": {
            "industry": industry or None,
            "region": _normalize_tag(profile.get("region")) or None,
            "concept_tags": concept_tags,
            "source": str(profile.get("source") or "industry_profile"),
            "provider_as_of": profile.get("provider_as_of"),
        } if industry else None,
        "order_amount": _finite_number(_first_value(record, _ORDER_AMOUNT_FIELDS)),
    }


def _leader_view(stock: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "code": stock["code"],
        "name": stock["name"],
        "board_count": stock["board_count"],
        "board_label": stock["board_label"],
        "raw_board": stock["board_label"],
        "raw_reason": " + ".join(stock["raw_reasons"]),
        "original_reason": " + ".join(stock["raw_reasons"]),
        "parsed_tags": list(stock["parsed_tags"]),
        "matched_tags": list(stock["matched_tags"]),
        "attributions": [dict(item) for item in stock["attributions"]],
        "sector_profile": dict(stock["sector_profile"]) if stock["sector_profile"] else None,
        "order_amount": stock["order_amount"],
    }


def _leader_sort_key(stock: Mapping[str, Any]) -> tuple[float, float, str]:
    board_count = stock.get("board_count")
    order_amount = stock.get("order_amount")
    return (
        -float(board_count if isinstance(board_count, int) else 0),
        -float(order_amount if isinstance(order_amount, (int, float)) else 0),
        str(stock.get("code", "")),
    )


def attribute_limit_up_records(records: Sequence[Record] | Iterable[Record]) -> dict[str, Any]:
    """Build leadership evidence for all eleven risk-appetite sectors.

    A stock may appear in multiple sectors, while duplicate records or multiple
    matching tags never inflate a sector's unique-stock count.
    """

    stocks_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    unmapped_tags: list[str] = []
    unmapped_industries: list[str] = []

    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            continue
        stock = _prepare_stock(record)
        identity = _record_identity(record, index)
        if identity in stocks_by_identity:
            _merge_stock_record(stocks_by_identity[identity], stock)
        else:
            stocks_by_identity[identity] = stock
        unmapped_tags.extend(stock["unmapped_tags"])
        unmapped_industries.extend(stock["unmapped_industries"])

    stocks = list(stocks_by_identity.values())
    pool_total = len(stocks)
    records_with_reason = sum(bool(stock["raw_reasons"]) for stock in stocks)
    # A structurally validated empty pool has no missing reason rows.  Treat
    # coverage as complete while keeping every sector vote unknown here; the
    # domain layer decides whether that valid empty pool is neutral evidence.
    reason_coverage = records_with_reason / pool_total if pool_total else 1.0
    records_with_profile = sum(stock["sector_profile"] is not None for stock in stocks)
    industry_profile_coverage = (
        records_with_profile / pool_total if pool_total else 1.0
    )

    sectors: dict[str, dict[str, Any]] = {}
    for sector_key in SECTOR_KEYS:
        matched_stocks = [
            stock
            for stock in stocks
            if any(
                attribution["sector_key"] == sector_key
                for attribution in stock["attributions"]
            )
        ]
        matched_stocks.sort(key=_leader_sort_key)
        matched_count = len(matched_stocks)
        max_board_count = max(
            (stock["board_count"] or 0 for stock in matched_stocks),
            default=0,
        )
        if not pool_total:
            vote = UNKNOWN
        elif matched_count >= 3 or (matched_count >= 2 and max_board_count >= 2):
            vote = STRONG
        else:
            vote = MEDIUM
        sectors[sector_key] = {
            "sector_key": sector_key,
            "label": SECTOR_LABELS[sector_key],
            "matched_count": matched_count,
            "max_board_count": max_board_count,
            "limit_up_leaders": [_leader_view(stock) for stock in matched_stocks],
            "vote": vote,
        }

    return {
        "taxonomy_version": ATTRIBUTION_TAXONOMY_VERSION,
        "pool_total": pool_total,
        "reason_coverage": reason_coverage,
        "industry_profile_coverage": industry_profile_coverage,
        "sectors": sectors,
        "unmapped_tags": sorted(set(unmapped_tags)),
        "unmapped_industries": sorted(set(unmapped_industries)),
    }


__all__ = [
    "ATTRIBUTION_TAXONOMY_VERSION",
    "CANONICAL_TAG_RULES",
    "SECTOR_KEYS",
    "SECTOR_LABELS",
    "TAG_ALIASES",
    "attribute_board_names",
    "attribute_limit_up_records",
    "attribute_reason",
    "attribute_tokens",
    "parse_board_count",
    "parse_reason_tags",
]
