"""Pure intraday rotation radar for industry and concept boards.

The module performs no I/O.  It consumes timestamped full-market snapshots,
builds taxonomy-local ranks, and replays a small deterministic lifecycle.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from math import isfinite
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


ROTATION_SCHEMA_VERSION = "rotation-radar-v1"
ROTATION_CONFIG_VERSION = "rotation-radar-config-v1"
CORE_OFFENSE_SCHEMA_VERSION = "core-offense-v1"
CORE_OFFENSE_CONFIG_VERSION = "core-offense-config-v1"


# Stable industry anchors are always present in the core-offense view.  Board
# codes are the primary identity; exact names are only a provider-compatibility
# fallback.  Concept boards corroborate or contradict an anchor but never vote
# on its level.
CORE_OFFENSE_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "semiconductor",
        "name": "半导体",
        "category": "科技成长",
        "anchor_mode": "structural",
        "conditional": False,
        "anchor": {
            "taxonomy": "industry",
            "board_codes": ("BK1036",),
            "exact_names": ("半导体",),
        },
        "confirmations": (),
    },
    {
        "key": "software_development",
        "name": "软件开发",
        "category": "科技成长",
        "anchor_mode": "structural",
        "conditional": False,
        "anchor": {
            "taxonomy": "industry",
            "board_codes": ("BK0737",),
            "exact_names": ("软件开发",),
        },
        "confirmations": (
            {
                "key": "ai_application",
                "name": "AI应用",
                "taxonomy": "concept",
                "board_codes": ("BK1629",),
                "exact_names": ("AI应用",),
            },
            {
                "key": "artificial_intelligence",
                "name": "人工智能",
                "taxonomy": "concept",
                "board_codes": ("BK0800",),
                "exact_names": ("人工智能",),
            },
            {
                "key": "aigc",
                "name": "AIGC概念",
                "taxonomy": "concept",
                "board_codes": ("BK1111",),
                "exact_names": ("AIGC概念",),
            },
        ),
    },
    {
        "key": "communication_equipment",
        "name": "通信设备",
        "category": "科技成长",
        "anchor_mode": "structural",
        "conditional": False,
        "anchor": {
            "taxonomy": "industry",
            "board_codes": ("BK0448",),
            "exact_names": ("通信设备",),
        },
        "confirmations": (
            {
                "key": "compute",
                "name": "算力概念",
                "taxonomy": "concept",
                "board_codes": ("BK1134",),
                "exact_names": ("算力概念",),
            },
            {
                "key": "cpo",
                "name": "CPO概念",
                "taxonomy": "concept",
                "board_codes": ("BK1128",),
                "exact_names": ("CPO概念",),
            },
        ),
    },
    {
        "key": "automation_equipment",
        "name": "自动化设备",
        "category": "科技成长",
        "anchor_mode": "structural",
        "conditional": False,
        "anchor": {
            "taxonomy": "industry",
            "board_codes": ("BK1237",),
            "exact_names": ("自动化设备",),
        },
        "confirmations": (
            {
                "key": "robotics",
                "name": "机器人概念",
                "taxonomy": "concept",
                "board_codes": ("BK1090",),
                "exact_names": ("机器人概念",),
            },
        ),
    },
    {
        "key": "consumer_electronics",
        "name": "消费电子",
        "category": "科技成长",
        "anchor_mode": "structural",
        "conditional": False,
        "anchor": {
            "taxonomy": "industry",
            "board_codes": ("BK1037",),
            "exact_names": ("消费电子",),
        },
        "confirmations": (),
    },
    {
        "key": "battery",
        "name": "电池",
        "category": "新能源",
        "anchor_mode": "structural",
        "conditional": False,
        "anchor": {
            "taxonomy": "industry",
            "board_codes": ("BK1033",),
            "exact_names": ("电池",),
        },
        "confirmations": (
            {
                "key": "lithium_battery",
                "name": "锂电池概念",
                "taxonomy": "concept",
                "board_codes": ("BK0574",),
                "exact_names": ("锂电池概念",),
            },
        ),
    },
    {
        "key": "photovoltaic_equipment",
        "name": "光伏设备",
        "category": "新能源",
        "anchor_mode": "structural",
        "conditional": False,
        "anchor": {
            "taxonomy": "industry",
            "board_codes": ("BK1031",),
            "exact_names": ("光伏设备",),
        },
        "confirmations": (),
    },
    {
        "key": "defense",
        "name": "国防军工",
        "category": "事件弹性",
        "anchor_mode": "conditional_event",
        "conditional": True,
        "anchor": {
            "taxonomy": "industry",
            "board_codes": ("BK1204",),
            "exact_names": ("国防军工",),
        },
        "confirmations": (
            {
                "key": "military",
                "name": "军工",
                "taxonomy": "concept",
                "board_codes": ("BK0490",),
                "exact_names": ("军工",),
            },
        ),
    },
)

TAXONOMIES = ("industry", "concept")
MAX_PROVIDER_LAG_SECONDS = 600
COVERAGE_DROP_RATIO = 0.70
MIN_EFFECTIVE_RATIO = 0.60
COMPLETE_EFFECTIVE_RATIO = 0.80
COOLING_RETENTION_STEPS = 30

STATE_LABELS = {
    "pending": "待确认",
    "rotating": "新近轮动增强",
    "attacking": "正在进攻",
    "cooling": "观察/退潮",
    "expired": "已过期",
}

ROLE_LABELS = {
    "technology_offense": "科技进攻",
    "financial_offense": "金融进攻",
    "resource_offense": "资源进攻",
    "defensive_countertrend": "防御逆势",
    "consumer_offense": "消费进攻",
    "other_offense": "其他进攻",
}


# Ordered from specific to broad.  Exact board identities remain dynamic; this
# configuration only collapses overlapping board taxonomies for presentation.
FAMILY_RULES: tuple[dict[str, Any], ...] = (
    {
        "key": "brain_computer",
        "label": "脑机接口",
        "role": "technology_offense",
        "keywords": ("脑机接口", "脑科学"),
    },
    {
        "key": "smart_driving",
        "label": "智能驾驶",
        "role": "technology_offense",
        "keywords": ("智能驾驶", "无人驾驶", "汽车芯片", "车路云", "激光雷达"),
    },
    {
        "key": "semiconductor_materials",
        "label": "半导体材料与设备",
        "role": "technology_offense",
        "keywords": (
            "半导体材料",
            "半导体设备",
            "半导体",
            "先进封装",
            "光刻机",
            "光刻胶",
            "第三代半导体",
            "芯片",
        ),
    },
    {
        "key": "humanoid_robotics",
        "label": "机器人",
        "role": "technology_offense",
        "keywords": ("人形机器人", "机器人", "自动化设备", "机器视觉", "减速器"),
    },
    {
        "key": "cpo_optics",
        "label": "CPO与光通信",
        "role": "technology_offense",
        "keywords": ("CPO", "光模块", "光通信", "通信设备"),
    },
    {
        "key": "compute",
        "label": "算力",
        "role": "technology_offense",
        "keywords": ("算力", "数据中心", "服务器", "液冷", "东数西算"),
    },
    {
        "key": "ai_applications",
        "label": "AI应用",
        "role": "technology_offense",
        "keywords": (
            "AI应用",
            "人工智能",
            "AIGC",
            "CHATGPT",
            "多模态AI",
            "软件开发",
            "传媒",
            "游戏",
        ),
    },
    {
        "key": "consumer_electronics",
        "label": "消费电子",
        "role": "technology_offense",
        "keywords": ("消费电子", "苹果概念", "智能穿戴", "电子元件"),
    },
    {
        "key": "battery",
        "label": "电池产业链",
        "role": "technology_offense",
        "keywords": ("电池", "锂电", "固态电池", "储能"),
    },
    {
        "key": "photovoltaic",
        "label": "光伏",
        "role": "technology_offense",
        "keywords": ("光伏", "太阳能", "HJT", "TOPCON"),
    },
    {
        "key": "defense",
        "label": "国防军工",
        "role": "technology_offense",
        "keywords": ("国防军工", "军工", "军工电子", "航天", "航空", "船舶制造"),
    },
    {
        "key": "financial",
        "label": "金融",
        "role": "financial_offense",
        "keywords": ("银行", "证券", "券商", "保险", "互联网金融", "金融科技"),
    },
    {
        "key": "resource_metals",
        "label": "金属资源",
        "role": "resource_offense",
        "keywords": ("有色", "贵金属", "黄金", "稀土", "小金属", "工业金属", "钢铁"),
    },
    {
        "key": "resource_energy",
        "label": "能源资源",
        "role": "resource_offense",
        "keywords": ("油气", "石油", "煤炭", "天然气", "化工"),
    },
    {
        "key": "agriculture",
        "label": "农业",
        "role": "resource_offense",
        "keywords": ("农业", "种植", "养殖", "农牧", "粮食"),
    },
    {
        "key": "power_utilities",
        "label": "公用事业",
        "role": "defensive_countertrend",
        "keywords": ("电力", "公用事业", "水务", "燃气"),
    },
    {
        "key": "stable_consumption",
        "label": "稳定消费",
        "role": "defensive_countertrend",
        "keywords": ("白酒", "食品饮料", "零售", "商业百货", "医药", "中药"),
    },
)


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_NAME_FIELDS = ("name", "板块名称", "板块", "名称", "行业名称")
_CODE_FIELDS = ("board_code", "板块代码", "代码", "code")
_CHANGE_FIELDS = ("change_pct", "涨跌幅")
_UP_FIELDS = ("up_count", "上涨家数", "上涨股数", "上涨")
_DOWN_FIELDS = ("down_count", "下跌家数", "下跌股数", "下跌")
_FLOW_AMOUNT_FIELDS = ("flow_amount", "主力净流入", "主力净流入额")
_FLOW_RATIO_FIELDS = (
    "flow_ratio",
    "main_net_ratio",
    "主力净流入-占比",
    "主力净流入占比",
    "主力净流入比例",
)
_FLOW_RANK_FIELDS = ("flow_rank", "主力净流入排名")
_PROVIDER_FIELDS = ("provider_as_of", "更新时间")


def _first(record: Mapping[str, Any], fields: Sequence[str]) -> Any:
    for field in fields:
        if field in record and record[field] not in (None, ""):
            return record[field]
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


def _as_shanghai(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    return parsed.astimezone(_SHANGHAI)


def _provider_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return _as_shanghai(str(value))
    except (TypeError, ValueError):
        return None


def _minute_iso(value: datetime | str) -> str:
    return _as_shanghai(value).replace(second=0, microsecond=0).isoformat(timespec="seconds")


def _segment(value: datetime | str) -> str:
    current = _as_shanghai(value).time()
    return "am" if current.hour < 12 else "pm"


def _identity(taxonomy: str, code: str) -> str:
    return f"{taxonomy}:{code}"


def canonical_family(name: str) -> dict[str, str] | None:
    normalized = "".join(str(name).upper().split())
    for rule in FAMILY_RULES:
        if any("".join(keyword.upper().split()) in normalized for keyword in rule["keywords"]):
            return {
                "key": rule["key"],
                "label": rule["label"],
                "role": rule["role"],
            }
    return None


def _fallback_role(name: str) -> str:
    normalized = str(name)
    if any(word in normalized for word in ("消费", "家电", "汽车", "旅游", "酒店")):
        return "consumer_offense"
    return "other_offense"


def _normalize_record(
    record: Mapping[str, Any],
    taxonomy: str,
    source: str,
    minute: datetime,
    market_phase: str,
) -> dict[str, Any] | None:
    code_value = _first(record, _CODE_FIELDS)
    name_value = _first(record, _NAME_FIELDS)
    if code_value in (None, "") or name_value in (None, ""):
        return None
    code = str(code_value).strip()
    name = str(name_value).strip()
    if not code or not name:
        return None
    up_count = _number(_first(record, _UP_FIELDS))
    down_count = _number(_first(record, _DOWN_FIELDS))
    breadth_ratio = None
    if up_count is not None and down_count is not None and up_count + down_count > 0:
        breadth_ratio = up_count / (up_count + down_count)
    provider = _provider_datetime(_first(record, _PROVIDER_FIELDS))
    provider_as_of = provider.isoformat(timespec="seconds") if provider else None
    lag_seconds = (minute - provider).total_seconds() if provider else None
    same_trade_date = bool(provider and provider.date() == minute.date())
    if str(market_phase) in {"midday_break", "closed"}:
        provider_fresh = bool(same_trade_date and lag_seconds is not None and lag_seconds >= -120)
    else:
        provider_fresh = bool(
            same_trade_date
            and lag_seconds is not None
            and -120 <= lag_seconds <= MAX_PROVIDER_LAG_SECONDS
        )
    item = {
        "id": _identity(taxonomy, code),
        "taxonomy": taxonomy,
        "board_code": code,
        "name": name,
        "change_pct": _number(_first(record, _CHANGE_FIELDS)),
        "up_count": up_count,
        "down_count": down_count,
        "breadth_ratio": breadth_ratio,
        "flow_amount": _number(_first(record, _FLOW_AMOUNT_FIELDS)),
        "flow_ratio": _number(_first(record, _FLOW_RATIO_FIELDS)),
        "flow_rank": _number(_first(record, _FLOW_RANK_FIELDS)),
        "provider_as_of": provider_as_of,
        "provider_fresh": provider_fresh,
        "source": str(_first(record, ("source",)) or source or "unknown"),
    }
    evidence_fields = (
        item["change_pct"],
        item["breadth_ratio"],
        item["flow_ratio"],
        item["flow_amount"],
    )
    item["effective"] = provider_fresh and item["change_pct"] is not None and sum(
        value is not None for value in evidence_fields
    ) >= 2
    family = canonical_family(name)
    item["family"] = family
    role_key = family["role"] if family else _fallback_role(name)
    item["role"] = {"key": role_key, "label": ROLE_LABELS[role_key]}
    return item


def _choice_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    provider = record.get("provider_as_of") or ""
    evidence = sum(
        record.get(field) is not None
        for field in ("change_pct", "breadth_ratio", "flow_amount", "flow_ratio", "flow_rank")
    )
    numeric = tuple(
        float("-inf") if record.get(field) is None else record[field]
        for field in ("change_pct", "breadth_ratio", "flow_amount", "flow_ratio")
    )
    return (provider, evidence, numeric, record.get("name", ""), record.get("source", ""))


def _field_coverage(records: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    total = len(records)
    fields = (
        "change_pct",
        "breadth_ratio",
        "flow_amount",
        "flow_ratio",
        "flow_rank",
        "provider_as_of",
    )
    if total == 0:
        return {field: 0.0 for field in fields}
    return {
        field: round(sum(record.get(field) is not None for record in records) / total, 4)
        for field in fields
    }


def _coverage(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(records)
    effective = sum(bool(record.get("effective")) for record in records)
    return {
        "total": total,
        "effective": effective,
        "effective_ratio": round(effective / total, 4) if total else 0.0,
        "field_coverage": _field_coverage(records),
    }


def normalize_rotation_snapshot(
    industry_records: Iterable[Mapping[str, Any]],
    concept_records: Iterable[Mapping[str, Any]],
    *,
    minute_bucket: datetime | str,
    sources: Mapping[str, str] | None = None,
    market_phase: str = "trading",
) -> dict[str, Any]:
    """Normalize a full-market snapshot into deterministic raw board records."""

    minute = _as_shanghai(minute_bucket).replace(second=0, microsecond=0)
    source_values = dict(sources or {})
    by_id: dict[str, dict[str, Any]] = {}
    for taxonomy, records in (
        ("industry", industry_records),
        ("concept", concept_records),
    ):
        source = source_values.get(taxonomy, "unknown")
        for raw in records:
            item = _normalize_record(
                raw,
                taxonomy,
                source,
                minute,
                str(market_phase),
            )
            if item is None:
                continue
            existing = by_id.get(item["id"])
            if existing is None or _choice_key(item) > _choice_key(existing):
                by_id[item["id"]] = item
    boards = sorted(by_id.values(), key=lambda item: (item["taxonomy"], item["board_code"]))
    by_taxonomy = {
        taxonomy: [item for item in boards if item["taxonomy"] == taxonomy]
        for taxonomy in TAXONOMIES
    }
    return {
        "schema_version": ROTATION_SCHEMA_VERSION,
        "minute_bucket": minute.isoformat(timespec="seconds"),
        "market_phase": str(market_phase),
        "session_segment": _segment(minute),
        "boards": boards,
        "coverage": {
            taxonomy: _coverage(by_taxonomy[taxonomy])
            for taxonomy in TAXONOMIES
        },
    }


def _tie_percentiles(records: Sequence[Mapping[str, Any]], field: str) -> dict[str, float | None]:
    pairs = sorted(
        (float(record[field]), record["id"])
        for record in records
        if record.get(field) is not None
    )
    if not pairs:
        return {record["id"]: None for record in records}
    by_value: dict[float, float] = {}
    position = 0
    denominator = max(1, len(pairs) - 1)
    while position < len(pairs):
        end = position + 1
        while end < len(pairs) and pairs[end][0] == pairs[position][0]:
            end += 1
        mid_rank = position + (end - position - 1) / 2
        by_value[pairs[position][0]] = 0.5 if len(pairs) == 1 else mid_rank / denominator
        position = end
    return {
        record["id"]: by_value.get(float(record[field])) if record.get(field) is not None else None
        for record in records
    }


def _evidence_state(value: float | None, *, high: float, low: float) -> str:
    if value is None:
        return "unknown"
    if value >= high:
        return "strong"
    if value <= low:
        return "weak"
    return "medium"


def _rank_snapshot(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for taxonomy in TAXONOMIES:
        records = [
            record for record in snapshot.get("boards", [])
            if record.get("taxonomy") == taxonomy
        ]
        price_percentiles = _tie_percentiles(records, "change_pct")
        flow_percentiles = _tie_percentiles(records, "flow_ratio")
        for record in records:
            item = dict(record)
            price_percentile = price_percentiles[item["id"]]
            flow_percentile = flow_percentiles[item["id"]]
            price_state = _evidence_state(price_percentile, high=0.75, low=0.30)
            if item.get("change_pct") is not None:
                if item["change_pct"] <= 0 and price_state == "strong":
                    price_state = "medium"
                elif item["change_pct"] < 0 and price_percentile is not None and price_percentile <= 0.30:
                    price_state = "weak"
            breadth_state = _evidence_state(item.get("breadth_ratio"), high=0.55, low=0.45)
            flow_value = item.get("flow_amount")
            if flow_value is None:
                flow_value = item.get("flow_ratio")
            if flow_value is None or flow_percentile is None:
                fund_state = "unknown"
            elif flow_value > 0 and flow_percentile >= 0.70:
                fund_state = "strong"
            elif flow_value < 0 and flow_percentile <= 0.30:
                fund_state = "weak"
            else:
                fund_state = "medium"
            votes = (price_state, breadth_state, fund_state)
            valid = [vote for vote in votes if vote != "unknown"]
            if len(valid) < 2:
                strength = "unknown"
            elif valid.count("strong") >= 2:
                strength = "strong"
            elif valid.count("weak") >= 2:
                strength = "weak"
            else:
                strength = "medium"
            divergence = None
            if price_state == "strong" and fund_state == "weak":
                divergence = "price_strong_fund_weak"
            elif price_state == "weak" and fund_state == "strong":
                divergence = "price_weak_fund_strong"
            item.update({
                "price_percentile": price_percentile,
                "flow_percentile": flow_percentile,
                "price_state": price_state,
                "breadth_state": breadth_state,
                "fund_state": fund_state,
                "strength": strength,
                "price_fund_divergence": divergence,
            })
            result[item["id"]] = item
    return result


def _baseline(
    history: Sequence[Mapping[str, Any]],
    current_minute: datetime,
    segment: str,
    minutes: int,
) -> Mapping[str, Any] | None:
    cutoff = current_minute - timedelta(minutes=minutes)
    for point in reversed(history):
        if point["segment"] == segment and point["minute"] <= cutoff:
            return point["metrics"]
    return None


def _delta(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    return current - previous


def _temporal_metrics(
    item: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    minute: datetime,
    segment: str,
) -> dict[str, Any]:
    five = _baseline(history, minute, segment, 5)
    ten = _baseline(history, minute, segment, 10)
    rank_delta_5m = _delta(item.get("price_percentile"), five.get("price_percentile") if five else None)
    rank_delta_10m = _delta(item.get("price_percentile"), ten.get("price_percentile") if ten else None)
    breadth_delta_5m = _delta(item.get("breadth_ratio"), five.get("breadth_ratio") if five else None)
    breadth_delta_10m = _delta(item.get("breadth_ratio"), ten.get("breadth_ratio") if ten else None)
    change_delta_5m = _delta(item.get("change_pct"), five.get("change_pct") if five else None)
    change_delta_10m = _delta(item.get("change_pct"), ten.get("change_pct") if ten else None)
    fund_delta = None
    previous_provider_as_of = None
    fund_direction = "unknown"
    if history:
        previous = history[-1]["metrics"]
        fund_delta = _delta(item.get("flow_amount"), previous.get("flow_amount"))
        previous_provider_as_of = previous.get("provider_as_of")
        fund_values = {"weak": -1, "medium": 0, "strong": 1}
        current_fund = fund_values.get(str(item.get("fund_state")))
        previous_fund = fund_values.get(str(previous.get("fund_state")))
        if current_fund is not None and previous_fund is not None:
            if current_fund > previous_fund:
                fund_direction = "strengthening"
            elif current_fund < previous_fund:
                fund_direction = "weakening"
            else:
                fund_direction = "stable"

    def improved(
        rank_delta: float | None,
        change_delta: float | None,
        breadth_delta: float | None,
        threshold: float,
    ) -> bool:
        return bool(
            rank_delta is not None
            and rank_delta >= threshold
            and (
                (change_delta is not None and change_delta >= 0.10)
                or (breadth_delta is not None and breadth_delta >= 0.05)
            )
        )

    has_baseline = five is not None or ten is not None
    ranking_improved = improved(rank_delta_5m, change_delta_5m, breadth_delta_5m, 0.10) or improved(
        rank_delta_10m,
        change_delta_10m,
        breadth_delta_10m,
        0.15,
    )
    rotation_signal = bool(
        has_baseline
        and ranking_improved
        and item.get("strength") != "weak"
        and item.get("fund_state") != "weak"
    )
    attack_signal = item.get("strength") == "strong"
    return {
        "rank_delta_5m": rank_delta_5m,
        "rank_delta_10m": rank_delta_10m,
        "breadth_delta_5m": breadth_delta_5m,
        "breadth_delta_10m": breadth_delta_10m,
        "change_delta_5m": change_delta_5m,
        "change_delta_10m": change_delta_10m,
        "fund_delta": fund_delta,
        "fund_direction": fund_direction,
        "previous_provider_as_of": previous_provider_as_of,
        "has_temporal_baseline": has_baseline,
        "ranking_improved": ranking_improved,
        "rotation_signal": rotation_signal,
        "attack_signal": attack_signal,
    }


_CORE_METRIC_FIELDS = (
    "change_pct",
    "price_percentile",
    "price_state",
    "breadth_ratio",
    "breadth_state",
    "flow_amount",
    "flow_ratio",
    "flow_rank",
    "flow_percentile",
    "fund_state",
    "rank_delta_5m",
    "rank_delta_10m",
    "breadth_delta_5m",
    "breadth_delta_10m",
    "change_delta_5m",
    "change_delta_10m",
    "fund_delta",
    "fund_direction",
    "previous_provider_as_of",
    "direction_window_minutes",
)

_CORE_DIRECTION_LABELS = {
    "strengthening": "相对增强",
    "weakening": "相对转弱",
    "flat": "相对持平",
    "insufficient": "趋势待积累",
}


def _select_core_board(
    ranked: Mapping[str, Mapping[str, Any]],
    selector: Mapping[str, Any],
) -> dict[str, Any] | None:
    taxonomy = str(selector.get("taxonomy") or "")
    candidates = sorted(
        (
            dict(item) for item in ranked.values()
            if item.get("taxonomy") == taxonomy
        ),
        key=lambda item: (str(item.get("board_code", "")), str(item.get("name", ""))),
    )
    by_code = {str(item.get("board_code")): item for item in candidates}
    for code in selector.get("board_codes", ()):
        if str(code) in by_code:
            return by_code[str(code)]
    for exact_name in selector.get("exact_names", ()):
        for item in candidates:
            if str(item.get("name")) == str(exact_name):
                return item
    return None


def _core_direction(
    item: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    minute: datetime,
    segment: str,
) -> tuple[str, int | None]:
    ten = _baseline(history, minute, segment, 10)
    five = _baseline(history, minute, segment, 5)
    baseline = ten or five
    window = 10 if ten is not None else 5 if five is not None else None
    if baseline is None:
        return "insufficient", None

    level_values = {"weak": -1, "medium": 0, "strong": 1}
    current_level = level_values.get(str(item.get("strength")))
    previous_level = level_values.get(str(baseline.get("strength")))
    if current_level is None or previous_level is None:
        return "insufficient", window
    if current_level > previous_level:
        return "strengthening", window
    if current_level < previous_level:
        return "weakening", window

    suffix = "10m" if window == 10 else "5m"
    rank_delta = item.get(f"rank_delta_{suffix}")
    breadth_delta = item.get(f"breadth_delta_{suffix}")
    change_delta = item.get(f"change_delta_{suffix}")
    evidence: list[int] = []
    if isinstance(rank_delta, (int, float)):
        if rank_delta >= 0.03 and (
            not isinstance(change_delta, (int, float)) or change_delta >= 0
        ):
            evidence.append(1)
        elif rank_delta <= -0.03 and (
            not isinstance(change_delta, (int, float)) or change_delta <= 0
        ):
            evidence.append(-1)
    if isinstance(breadth_delta, (int, float)):
        if breadth_delta >= 0.03:
            evidence.append(1)
        elif breadth_delta <= -0.03:
            evidence.append(-1)
    current_fund = level_values.get(str(item.get("fund_state")))
    previous_fund = level_values.get(str(baseline.get("fund_state")))
    if current_fund is not None and previous_fund is not None:
        if current_fund > previous_fund:
            evidence.append(1)
        elif current_fund < previous_fund:
            evidence.append(-1)
    if evidence.count(1) >= 2:
        return "strengthening", window
    if evidence.count(-1) >= 2:
        return "weakening", window
    return "flat", window


def _core_ranked_latest(
    snapshots: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    histories: dict[str, list[dict[str, Any]]] = {}
    references: dict[str, dict[str, Any] | None] = {
        taxonomy: None for taxonomy in TAXONOMIES
    }
    latest: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        minute = _as_shanghai(str(snapshot["minute_bucket"]))
        segment = str(snapshot.get("session_segment") or _segment(minute))
        ranked = _rank_snapshot(snapshot)
        usable_taxonomies: dict[str, bool] = {}
        for taxonomy in TAXONOMIES:
            current_coverage = dict(snapshot.get("coverage", {}).get(taxonomy, {}))
            usable, complete = _coverage_gate(current_coverage, references[taxonomy])
            usable_taxonomies[taxonomy] = usable
            if complete:
                references[taxonomy] = current_coverage
        current: dict[str, dict[str, Any]] = {}
        for item_id, raw_item in ranked.items():
            item = dict(raw_item)
            if not usable_taxonomies.get(str(item.get("taxonomy")), False):
                item["effective"] = False
            provider = _provider_datetime(item.get("provider_as_of"))
            points = histories.setdefault(item_id, [])
            if points and points[-1]["metrics"].get("source") != item.get("source"):
                points.clear()
            if not item.get("effective") or provider is None:
                item.update({
                    "rank_delta_5m": None,
                    "rank_delta_10m": None,
                    "breadth_delta_5m": None,
                    "breadth_delta_10m": None,
                    "change_delta_5m": None,
                    "change_delta_10m": None,
                    "fund_delta": None,
                    "fund_direction": "unknown",
                    "previous_provider_as_of": None,
                    "core_direction": "insufficient",
                    "direction_window_minutes": None,
                })
                current[item_id] = item
                continue
            if points and provider <= points[-1]["provider"]:
                current[item_id] = dict(points[-1]["metrics"])
                continue
            temporal = _temporal_metrics(item, points, minute, segment)
            enriched = {**item, **temporal}
            direction, window = _core_direction(enriched, points, minute, segment)
            enriched["core_direction"] = direction
            enriched["direction_window_minutes"] = window
            points.append({
                "minute": minute,
                "segment": segment,
                "provider": provider,
                "source": item.get("source"),
                "metrics": enriched,
            })
            if len(points) > 48:
                del points[:-48]
            current[item_id] = enriched
        latest = current
    return latest


def _core_metrics(item: Mapping[str, Any] | None) -> dict[str, Any]:
    source = item or {}
    return {field: source.get(field) for field in _CORE_METRIC_FIELDS}


def _core_board_view(item: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "taxonomy": item.get("taxonomy"),
        "board_code": item.get("board_code"),
        "name": item.get("name"),
        "provider_as_of": item.get("provider_as_of"),
        "source": item.get("source"),
    }


def _core_confirmation_view(
    definition: Mapping[str, Any],
    item: Mapping[str, Any] | None,
    anchor_level: str,
) -> dict[str, Any]:
    effective = bool(item and item.get("effective"))
    level = str(item.get("strength")) if effective else "unknown"
    if not effective or anchor_level == "unknown":
        alignment = "unavailable"
    elif {level, anchor_level} == {"strong", "weak"}:
        alignment = "divergent"
    elif level == anchor_level and level in {"strong", "weak"}:
        alignment = "confirming"
    else:
        alignment = "neutral"
    return {
        "key": definition.get("key"),
        "name": definition.get("name"),
        "level": level,
        "level_label": {
            "strong": "强", "medium": "中", "weak": "弱", "unknown": "数据不足"
        }[level],
        "alignment": alignment,
        "direction": str(item.get("core_direction")) if effective else "insufficient",
        "metrics": _core_metrics(item),
        "representative_board": _core_board_view(item),
        "coverage": {
            "status": "ready" if effective else "stale" if item else "missing",
            "found": item is not None,
            "effective": effective,
        },
    }


def _core_item_view(
    definition: Mapping[str, Any],
    ranked: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    anchor = _select_core_board(ranked, definition["anchor"])
    effective = bool(anchor and anchor.get("effective"))
    level = str(anchor.get("strength")) if effective else "unknown"
    evidence_states = (
        anchor.get("price_state") if effective else "unknown",
        anchor.get("breadth_state") if effective else "unknown",
        anchor.get("fund_state") if effective else "unknown",
    )
    confirmations = [
        _core_confirmation_view(
            confirmation,
            _select_core_board(ranked, confirmation),
            level,
        )
        for confirmation in definition.get("confirmations", ())
    ]
    direction = str(anchor.get("core_direction")) if effective else "insufficient"
    return {
        "key": definition["key"],
        "name": definition["name"],
        "category": definition["category"],
        "conditional": bool(definition["conditional"]),
        "anchor_mode": definition["anchor_mode"],
        "level": level,
        "level_label": {
            "strong": "强", "medium": "中", "weak": "弱", "unknown": "数据不足"
        }[level],
        "evidence_count": sum(state != "unknown" for state in evidence_states),
        "evidence_total": 3,
        "direction": direction,
        "direction_label": _CORE_DIRECTION_LABELS[direction],
        "metrics": _core_metrics(anchor),
        "confirmations": confirmations,
        "representative_board": _core_board_view(anchor),
        "coverage": {
            "status": "ready" if effective else "stale" if anchor else "missing",
            "anchor_found": anchor is not None,
            "anchor_effective": effective,
            "confirmations_available": sum(
                confirmation["coverage"]["effective"] for confirmation in confirmations
            ),
            "confirmations_total": len(confirmations),
        },
    }


def _analyze_core_offense(
    snapshots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    ranked = _core_ranked_latest(snapshots) if snapshots else {}
    items = [_core_item_view(definition, ranked) for definition in CORE_OFFENSE_DEFINITIONS]
    available = sum(item["coverage"]["anchor_effective"] for item in items)
    total = len(items)
    latest_phase = str(snapshots[-1].get("market_phase")) if snapshots else ""
    if not snapshots or latest_phase == "opening_observation":
        status = "collecting"
        status_label = "常规进攻样本积累中"
    elif available == total:
        status = "ready"
        status_label = "常规进攻锚已更新"
    elif available:
        status = "partial"
        status_label = "常规进攻锚部分可用"
    else:
        status = "stale"
        status_label = "常规进攻锚数据延迟"
    level_counts = {
        level: sum(item["level"] == level for item in items)
        for level in ("strong", "medium", "weak", "unknown")
    }
    direction_counts = {
        direction: sum(item["direction"] == direction for item in items)
        for direction in ("strengthening", "weakening", "flat", "insufficient")
    }
    provider_times = [
        item["representative_board"].get("provider_as_of")
        for item in items
        if isinstance(item.get("representative_board"), Mapping)
        and item["representative_board"].get("provider_as_of")
    ]
    return {
        "schema_version": CORE_OFFENSE_SCHEMA_VERSION,
        "config_version": CORE_OFFENSE_CONFIG_VERSION,
        "status": status,
        "status_label": status_label,
        "as_of": max(provider_times) if provider_times else (
            str(snapshots[-1].get("minute_bucket")) if snapshots else None
        ),
        "coverage": {
            "available": available,
            "total": total,
            "ratio": round(available / total, 4) if total else 0.0,
            "partial": available < total,
            "missing": [
                item["key"] for item in items
                if not item["coverage"]["anchor_effective"]
            ],
        },
        "summary": {
            "counts": level_counts,
            "direction_counts": direction_counts,
        },
        "items": items,
    }


def _coverage_gate(current: Mapping[str, Any], reference: Mapping[str, Any] | None) -> tuple[bool, bool]:
    total = int(current.get("total", 0))
    effective = int(current.get("effective", 0))
    ratio = float(current.get("effective_ratio", 0.0))
    usable = total > 0 and effective > 0 and ratio >= MIN_EFFECTIVE_RATIO
    if usable and reference:
        usable = bool(
            total >= float(reference["total"]) * COVERAGE_DROP_RATIO
            and effective >= float(reference["effective"]) * COVERAGE_DROP_RATIO
        )
        current_fields = current.get("field_coverage", {})
        reference_fields = reference.get("field_coverage", {})
        for field in ("change_pct", "breadth_ratio", "flow_ratio", "provider_as_of"):
            reference_value = float(reference_fields.get(field, 0.0))
            current_value = float(current_fields.get(field, 0.0))
            if reference_value >= 0.50 and current_value < reference_value * COVERAGE_DROP_RATIO:
                usable = False
                break
    fields = current.get("field_coverage", {})
    complete = bool(
        usable
        and ratio >= COMPLETE_EFFECTIVE_RATIO
        and float(fields.get("change_pct", 0.0)) >= COMPLETE_EFFECTIVE_RATIO
        and float(fields.get("provider_as_of", 0.0)) >= COMPLETE_EFFECTIVE_RATIO
    )
    return usable, complete


def _transition(
    state: dict[str, Any],
    target: str,
    minute_iso: str,
    events: list[dict[str, Any]],
    *,
    step: int,
) -> None:
    previous = state["state"]
    if previous == target:
        return
    state["state"] = target
    state["state_since"] = minute_iso
    state["entry_hits"] = 0
    state["attack_hits"] = 0
    state["exit_hits"] = 0
    if target == "cooling":
        state["cooling_step"] = step
    elif target != "expired":
        state["cooling_step"] = None
    events.append({
        "at": minute_iso,
        "id": state["id"],
        "name": state.get("name"),
        "from": previous,
        "to": target,
        "label": f"{STATE_LABELS[previous]}转为{STATE_LABELS[target]}",
    })


def _new_state(item: Mapping[str, Any], minute_iso: str) -> dict[str, Any]:
    return {
        "id": item["id"],
        "taxonomy": item["taxonomy"],
        "board_code": item["board_code"],
        "name": item["name"],
        "state": "pending",
        "state_since": minute_iso,
        "entry_hits": 0,
        "attack_hits": 0,
        "exit_hits": 0,
        "cooling_step": None,
        "last_provider": None,
        "last_source": None,
        "last_segment": None,
        "last_metrics": dict(item),
        "current": True,
    }


def _price_band(value: float | None) -> int:
    if value is None:
        return -1
    if value >= 0.85:
        return 3
    if value >= 0.70:
        return 2
    if value >= 0.50:
        return 1
    return 0


def _velocity_band(item: Mapping[str, Any]) -> int:
    values = [
        value for value in (item.get("rank_delta_5m"), item.get("rank_delta_10m"))
        if value is not None
    ]
    value = max(values) if values else None
    if value is None:
        return -1
    if value >= 0.20:
        return 3
    if value >= 0.10:
        return 2
    if value > 0:
        return 1
    return 0


def _breadth_band(value: float | None) -> int:
    if value is None:
        return -1
    if value >= 0.65:
        return 3
    if value >= 0.55:
        return 2
    if value >= 0.45:
        return 1
    return 0


def _fund_band(value: str) -> int:
    return {"strong": 3, "medium": 2, "weak": 0, "unknown": -1}.get(value, -1)


def _state_band(value: str) -> int:
    return {"attacking": 3, "rotating": 2, "cooling": 1, "pending": 0}.get(value, -1)


def _sort_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    metrics = candidate.get("metrics", candidate)
    flow_rank = metrics.get("flow_rank")
    rank_band = -1 if flow_rank is None else max(0, 5 - min(5, int(flow_rank) // 20))
    return (
        -_state_band(str(candidate.get("state", "pending"))),
        -_fund_band(str(metrics.get("fund_state", "unknown"))),
        -_price_band(metrics.get("price_percentile")),
        -_velocity_band(metrics),
        -_breadth_band(metrics.get("breadth_ratio")),
        -rank_band,
        str(candidate.get("taxonomy", "")),
        str(candidate.get("board_code", "")),
    )


def _candidate_view(state: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(state.get("last_metrics", {}))
    family = item.get("family") or canonical_family(str(state.get("name", "")))
    role = item.get("role") or {
        "key": _fallback_role(str(state.get("name", ""))),
        "label": ROLE_LABELS[_fallback_role(str(state.get("name", "")))],
    }
    return {
        "id": state["id"],
        "taxonomy": state["taxonomy"],
        "board_code": state["board_code"],
        "name": state.get("name"),
        "family": family,
        "role": role,
        "state": state["state"],
        "state_label": STATE_LABELS[state["state"]],
        "state_since": state.get("state_since"),
        "current": bool(state.get("current")),
        "strength": item.get("strength", "unknown"),
        "direction": "strengthening" if item.get("rotation_signal") else (
            "stable" if item.get("attack_signal") else "weakening"
        ),
        "flags": [item["price_fund_divergence"]] if item.get("price_fund_divergence") else [],
        "provider_as_of": item.get("provider_as_of"),
        "source": item.get("source"),
        "metrics": {
            key: item.get(key)
            for key in (
                "change_pct",
                "price_percentile",
                "breadth_ratio",
                "flow_amount",
                "flow_ratio",
                "flow_rank",
                "flow_percentile",
                "fund_state",
                "rank_delta_5m",
                "rank_delta_10m",
                "breadth_delta_5m",
                "breadth_delta_10m",
                "fund_delta",
                "fund_direction",
                "previous_provider_as_of",
            )
        },
    }


def _current_leader_view(item: Mapping[str, Any]) -> dict[str, Any]:
    family = item.get("family")
    if item.get("rotation_signal"):
        direction = "strengthening"
    elif item.get("attack_signal"):
        direction = "stable"
    else:
        direction = "unknown" if not item.get("has_temporal_baseline") else "weakening"
    return {
        "id": item["id"],
        "taxonomy": item["taxonomy"],
        "board_code": item["board_code"],
        "name": item["name"],
        "family": family,
        "role": item["role"],
        "strength": item["strength"],
        "direction": direction,
        "provider_as_of": item.get("provider_as_of"),
        "metrics": {
            key: item.get(key)
            for key in (
                "change_pct",
                "price_percentile",
                "breadth_ratio",
                "flow_amount",
                "flow_ratio",
                "flow_rank",
                "flow_percentile",
                "fund_state",
                "rank_delta_5m",
                "rank_delta_10m",
                "breadth_delta_5m",
                "breadth_delta_10m",
                "fund_delta",
                "fund_direction",
                "previous_provider_as_of",
            )
        },
    }


def _family_groups(candidates: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    classified: dict[str, list[dict[str, Any]]] = {}
    unclassified: list[dict[str, Any]] = []
    for candidate in candidates:
        family = candidate.get("family")
        if not family:
            if candidate["state"] in {"attacking", "rotating"} and candidate["strength"] == "strong":
                unclassified.append(candidate)
            continue
        classified.setdefault(family["key"], []).append(candidate)

    primary: list[dict[str, Any]] = []
    for members in classified.values():
        ordered = sorted(members, key=_sort_key)
        selected = dict(ordered[0])
        selected["confirmations"] = [
            {
                "id": item["id"],
                "taxonomy": item["taxonomy"],
                "board_code": item["board_code"],
                "name": item["name"],
                "state": item["state"],
                "strength": item["strength"],
            }
            for item in ordered[1:6]
        ]
        selected["expansion_eligible"] = any(
            item["taxonomy"] == "industry" and item["state"] in {"attacking", "rotating"}
            for item in members
        )
        primary.append(selected)
    return sorted(primary, key=_sort_key), sorted(unclassified, key=_sort_key)[:3]


def _empty_view() -> dict[str, Any]:
    return {
        "schema_version": ROTATION_SCHEMA_VERSION,
        "config_version": ROTATION_CONFIG_VERSION,
        "status": "collecting",
        "status_label": "轮动样本积累中",
        "as_of": None,
        "sample_count": 0,
        "coverage": {
            taxonomy: {
                "total": 0,
                "effective": 0,
                "effective_ratio": 0.0,
                "field_coverage": {},
                "frozen": True,
            }
            for taxonomy in TAXONOMIES
        },
        "summary": {
            "strength": {"key": "unknown", "label": "观察"},
            "direction": {"key": "unknown", "label": "样本不足"},
            "structure": {"key": "observing", "label": "观察"},
            "counts": {"attacking": 0, "rotating": 0, "cooling": 0, "families": 0, "expanding_families": 0},
            "current_leaders": [],
        },
        "attacking": [],
        "rotating": [],
        "cooling": [],
        "unclassified": [],
        "events": [],
        "core": _analyze_core_offense([]),
    }


def analyze_rotation_snapshots(
    snapshots: Iterable[Mapping[str, Any]],
    *,
    config_version: str = ROTATION_CONFIG_VERSION,
) -> dict[str, Any]:
    """Replay normalized snapshots and return a bounded current radar view."""

    ordered = sorted(
        (dict(snapshot) for snapshot in snapshots),
        key=lambda snapshot: str(snapshot.get("minute_bucket", "")),
    )
    if not ordered:
        result = _empty_view()
        result["config_version"] = config_version
        return result
    latest_trade_date = _as_shanghai(str(ordered[-1]["minute_bucket"])).date()
    ordered = [
        snapshot for snapshot in ordered
        if _as_shanghai(str(snapshot["minute_bucket"])).date() == latest_trade_date
    ]

    history: dict[str, list[dict[str, Any]]] = {}
    states: dict[str, dict[str, Any]] = {}
    references: dict[str, dict[str, Any] | None] = {taxonomy: None for taxonomy in TAXONOMIES}
    steps = {taxonomy: 0 for taxonomy in TAXONOMIES}
    events: list[dict[str, Any]] = []
    latest_ranked: dict[str, dict[str, Any]] = {}
    latest_gate = {taxonomy: False for taxonomy in TAXONOMIES}
    previous_segment: str | None = None

    for snapshot in ordered:
        minute = _as_shanghai(str(snapshot["minute_bucket"]))
        minute_iso = minute.isoformat(timespec="seconds")
        segment = str(snapshot.get("session_segment") or _segment(minute))
        advance_lifecycle = str(snapshot.get("market_phase", "trading")) == "trading"
        if previous_segment is not None and segment != previous_segment:
            for state in states.values():
                state["entry_hits"] = 0
                state["attack_hits"] = 0
                state["exit_hits"] = 0
        previous_segment = segment
        ranked = _rank_snapshot(snapshot)
        coverage = snapshot.get("coverage", {})
        for state in states.values():
            state["current"] = False

        for taxonomy in TAXONOMIES:
            current_coverage = dict(coverage.get(taxonomy, {}))
            usable, complete = _coverage_gate(current_coverage, references[taxonomy])
            latest_gate[taxonomy] = usable
            if complete:
                references[taxonomy] = current_coverage
            if not usable:
                continue
            if advance_lifecycle:
                steps[taxonomy] += 1
            taxonomy_step = steps[taxonomy]
            for item in ranked.values():
                if item["taxonomy"] != taxonomy or not item.get("effective"):
                    continue
                provider = _provider_datetime(item.get("provider_as_of"))
                if provider is None:
                    continue
                state = states.get(item["id"])
                if state is not None and state.get("last_source") not in (None, item["source"]):
                    state["entry_hits"] = 0
                    state["attack_hits"] = 0
                    state["exit_hits"] = 0
                    state["last_provider"] = None
                    history.pop(item["id"], None)
                last_provider = _provider_datetime(state.get("last_provider")) if state else None
                if last_provider is not None and provider <= last_provider:
                    if state is not None:
                        state["current"] = True
                    continue

                points = history.setdefault(item["id"], [])
                if points and points[-1].get("source") != item["source"]:
                    points.clear()
                temporal = _temporal_metrics(item, points, minute, segment)
                enriched = {**item, **temporal}
                ranked[item["id"]] = enriched
                if not advance_lifecycle:
                    points.append({
                        "minute": minute,
                        "segment": segment,
                        "provider": provider,
                        "source": item["source"],
                        "metrics": enriched,
                    })
                    if len(points) > 48:
                        del points[:-48]
                    continue
                if state is None and (temporal["rotation_signal"] or temporal["attack_signal"]):
                    state = _new_state(enriched, minute_iso)
                    states[item["id"]] = state
                if state is not None:
                    state.update({
                        "name": item["name"],
                        "last_provider": item["provider_as_of"],
                        "last_source": item["source"],
                        "last_segment": segment,
                        "last_metrics": enriched,
                        "current": True,
                    })
                    if state["state"] == "pending":
                        if temporal["attack_signal"]:
                            state["attack_hits"] += 1
                            state["entry_hits"] = 0
                            if state["attack_hits"] >= 2:
                                _transition(state, "attacking", minute_iso, events, step=taxonomy_step)
                        elif temporal["rotation_signal"]:
                            state["entry_hits"] += 1
                            state["attack_hits"] = 0
                            if state["entry_hits"] >= 2:
                                _transition(state, "rotating", minute_iso, events, step=taxonomy_step)
                        else:
                            state["entry_hits"] = 0
                            state["attack_hits"] = 0
                    elif state["state"] == "rotating":
                        if temporal["attack_signal"]:
                            state["attack_hits"] += 1
                            state["exit_hits"] = 0
                            if state["attack_hits"] >= 2:
                                _transition(state, "attacking", minute_iso, events, step=taxonomy_step)
                        elif temporal["rotation_signal"]:
                            state["attack_hits"] = 0
                            state["exit_hits"] = 0
                        else:
                            state["attack_hits"] = 0
                            state["exit_hits"] += 1
                            if state["exit_hits"] >= 2:
                                _transition(state, "cooling", minute_iso, events, step=taxonomy_step)
                    elif state["state"] == "attacking":
                        if temporal["attack_signal"]:
                            state["exit_hits"] = 0
                        else:
                            state["exit_hits"] += 1
                            if state["exit_hits"] >= 2:
                                _transition(state, "cooling", minute_iso, events, step=taxonomy_step)
                    elif state["state"] == "cooling":
                        if temporal["rotation_signal"]:
                            state["entry_hits"] += 1
                            if state["entry_hits"] >= 2:
                                _transition(state, "rotating", minute_iso, events, step=taxonomy_step)
                        else:
                            state["entry_hits"] = 0

                points.append({
                    "minute": minute,
                    "segment": segment,
                    "provider": provider,
                    "source": item["source"],
                    "metrics": enriched,
                })
                if len(points) > 48:
                    del points[:-48]

            for state in states.values():
                if state["taxonomy"] != taxonomy or state["state"] != "cooling":
                    continue
                cooling_step = state.get("cooling_step")
                if cooling_step is not None and taxonomy_step - cooling_step >= COOLING_RETENTION_STEPS:
                    _transition(state, "expired", minute_iso, events, step=taxonomy_step)
            latest_ranked.update({
                key: value for key, value in ranked.items()
                if value["taxonomy"] == taxonomy
            })

    latest = ordered[-1]
    core = _analyze_core_offense(ordered)
    latest_minute = str(latest["minute_bucket"])
    coverage_view: dict[str, dict[str, Any]] = {}
    for taxonomy in TAXONOMIES:
        value = dict(latest.get("coverage", {}).get(taxonomy, {}))
        value["frozen"] = not latest_gate[taxonomy]
        coverage_view[taxonomy] = value
    effective_sum = sum(int(value.get("effective", 0)) for value in coverage_view.values())
    if str(latest.get("market_phase")) == "opening_observation":
        status = "collecting"
        status_label = "开盘观察，轮动尚未确认"
    elif effective_sum == 0:
        status = "stale"
        status_label = "轮动数据延迟"
    elif any(value["frozen"] for value in coverage_view.values()):
        status = "partial"
        status_label = "轮动数据部分可用"
    else:
        status = "ready"
        status_label = "轮动雷达已更新"

    candidates = [
        _candidate_view(state) for state in states.values()
        if state["state"] in {"attacking", "rotating", "cooling"}
    ]
    primary, unclassified = _family_groups(candidates)
    buckets = {
        state: [candidate for candidate in primary if candidate["state"] == state]
        for state in ("attacking", "rotating", "cooling")
    }
    for state in buckets:
        buckets[state] = buckets[state][:8]

    current_pool = [
        item for item in latest_ranked.values()
        if item.get("effective") and item.get("strength") == "strong"
    ]
    current_candidates = []
    for item in current_pool:
        view = _current_leader_view(item)
        view["state"] = "pending"
        current_candidates.append(view)
    current_primary: dict[str, dict[str, Any]] = {}
    current_unclassified: list[dict[str, Any]] = []
    for candidate in sorted(current_candidates, key=_sort_key):
        family = candidate.get("family")
        if family:
            current_primary.setdefault(family["key"], candidate)
        else:
            current_unclassified.append(candidate)
    current_leaders = list(current_primary.values()) + current_unclassified[:3]
    current_leaders = sorted(current_leaders, key=_sort_key)[:3]

    counts = {
        "attacking": sum(candidate["state"] == "attacking" for candidate in primary),
        "rotating": sum(candidate["state"] == "rotating" for candidate in primary),
        "cooling": sum(candidate["state"] == "cooling" for candidate in primary),
        "families": len(primary),
        "expanding_families": sum(bool(candidate.get("expansion_eligible")) for candidate in primary),
        "unclassified": len(unclassified),
    }
    if counts["attacking"] >= 3:
        strength = {"key": "strong", "label": "强"}
    elif counts["attacking"] or counts["rotating"]:
        strength = {"key": "medium", "label": "中"}
    elif counts["cooling"]:
        strength = {"key": "weak", "label": "弱"}
    else:
        strength = {"key": "unknown", "label": "观察"}

    latest_segment = str(latest.get("session_segment") or _segment(str(latest["minute_bucket"])))
    recent_minutes = {
        str(snapshot["minute_bucket"])
        for snapshot in ordered
        if str(snapshot.get("session_segment") or _segment(str(snapshot["minute_bucket"]))) == latest_segment
    }
    recent_minutes = set(sorted(recent_minutes)[-10:])
    recent_events = [event for event in events if event["at"] in recent_minutes]
    positive = sum(event["to"] in {"rotating", "attacking"} for event in recent_events)
    negative = sum(event["to"] in {"cooling", "expired"} for event in recent_events)
    if positive > negative:
        direction = {"key": "strengthening", "label": "相对增强"}
    elif negative > positive:
        direction = {"key": "weakening", "label": "相对转弱"}
    else:
        direction = {"key": "stable", "label": "相对稳定"}

    if status != "ready":
        structure = {"key": "observing", "label": "覆盖不足，暂不判断扩散"}
    elif counts["expanding_families"] >= 3:
        structure = {"key": "broadening", "label": "行业骨架扩散"}
    elif counts["attacking"] + counts["rotating"] > 0:
        structure = {"key": "focused", "label": "局部主线聚焦"}
    elif counts["cooling"] > 0:
        structure = {"key": "cooling", "label": "方向退潮观察"}
    else:
        structure = {"key": "observing", "label": "尚无确认方向"}

    return {
        "schema_version": ROTATION_SCHEMA_VERSION,
        "config_version": config_version,
        "status": status,
        "status_label": status_label,
        "as_of": latest_minute,
        "sample_count": len(ordered),
        "coverage": coverage_view,
        "summary": {
            "strength": strength,
            "direction": direction,
            "structure": structure,
            "counts": counts,
            "current_leaders": current_leaders,
        },
        "attacking": buckets["attacking"],
        "rotating": buckets["rotating"],
        "cooling": buckets["cooling"],
        "unclassified": unclassified[:3],
        "events": events[-12:],
        "core": core,
    }


__all__ = [
    "COOLING_RETENTION_STEPS",
    "CORE_OFFENSE_CONFIG_VERSION",
    "CORE_OFFENSE_DEFINITIONS",
    "CORE_OFFENSE_SCHEMA_VERSION",
    "FAMILY_RULES",
    "ROTATION_CONFIG_VERSION",
    "ROTATION_SCHEMA_VERSION",
    "STATE_LABELS",
    "analyze_rotation_snapshots",
    "canonical_family",
    "normalize_rotation_snapshot",
]
