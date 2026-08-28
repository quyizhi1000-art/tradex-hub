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
SECTOR_FLOW_CONTRACT = "sector_flow_trajectory.v1"
SECTOR_FLOW_SCHEMA_VERSION = 1
SECTOR_FLOW_MAX_POINTS = 256
SECTOR_FLOW_MAX_SERIES = 64
DEFENSE_SECTOR_FLOW_SERIES = 39


SECTOR_FLOW_CATEGORY_LABELS = {
    "steady_defense": "稳态防御",
    "event_hedge": "事件与避险",
    "transport_defense": "运输防御",
    "stable_consumption": "稳定消费",
    "weight_support": "权重承接",
    "utility_defense": "公用事业",
    "medical_defense": "医疗防御",
    "technology_growth": "科技成长",
    "new_energy": "新能源",
    "event_elasticity": "事件弹性",
    "resource_cycle": "资源周期",
    "medical_growth": "医药成长",
}

SECTOR_FLOW_TIER_LABELS = {
    "confirmed_strengthening": "同步增强",
    "strong_pending": "强势待确认",
    "funds_leading": "资金先行",
    "divergence": "出现背离",
    "retreat": "退潮观察",
    "observing": "继续观察",
    "unavailable": "证据不足",
}


# Optional directions enlarge the user-selectable desktop observation pool
# without changing the legacy risk-appetite model. Exact-curve refreshes are
# coalesced asynchronously; Eastmoney board codes remain identity hints for its
# taxonomy and exact names remain the cross-provider fallback.
_OPTIONAL_SECTOR_FLOW_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "coal",
        "name": "煤炭",
        "category_key": "event_hedge",
        "board_codes": ("BK0437",),
        "industry_aliases": ("煤炭", "煤炭行业"),
    },
    {
        "key": "food_beverage",
        "name": "食品饮料",
        "category_key": "stable_consumption",
        "board_codes": ("BK0438",),
        "industry_aliases": ("食品饮料",),
    },
    {
        "key": "insurance",
        "name": "保险",
        "category_key": "weight_support",
        "board_codes": ("BK0474",),
        "industry_aliases": ("保险", "保险Ⅱ"),
    },
    {
        "key": "gas",
        "name": "燃气",
        "category_key": "utility_defense",
        "board_codes": ("BK1028",),
        "industry_aliases": ("燃气", "燃气Ⅱ", "燃气行业"),
    },
    {
        "key": "traditional_chinese_medicine",
        "name": "中药",
        "category_key": "medical_defense",
        "board_codes": ("BK1040",),
        "industry_aliases": ("中药", "中药Ⅱ"),
    },
    {
        "key": "white_goods",
        "name": "白色家电",
        "category_key": "stable_consumption",
        "board_codes": ("BK1239",),
        "industry_aliases": ("白色家电",),
    },
    {
        "key": "water_utilities",
        "name": "水务",
        "category_key": "utility_defense",
        "board_codes": ("BK1390",),
        "industry_aliases": ("水务及水治理", "水务", "水务行业"),
    },
    {
        "key": "highway",
        "name": "高速公路",
        "category_key": "transport_defense",
        "board_codes": ("BK1483", "BK0421"),
        "industry_aliases": ("高速公路", "铁路公路"),
    },
    {
        "key": "shipping",
        "name": "航运",
        "category_key": "transport_defense",
        "board_codes": (),
        "industry_aliases": ("航运", "航运港口"),
    },
    {
        "key": "pharmaceutical_biology",
        "name": "医药生物",
        "category_key": "medical_defense",
        "board_codes": (),
        "industry_aliases": ("医药生物",),
    },
    {
        "key": "medical_devices",
        "name": "医疗器械",
        "category_key": "medical_defense",
        "board_codes": (),
        "industry_aliases": ("医疗器械", "医疗设备"),
    },
    {
        "key": "environmental_protection",
        "name": "环保",
        "category_key": "utility_defense",
        "board_codes": (),
        "industry_aliases": ("环保",),
    },
)


# Provider-neutral concept taxonomy beneath the defensive industry anchors.
# Exact names come from the canonical full-market concept snapshot; provider
# identities are accepted only when the current canonical snapshot proves them.
DEFENSE_CONCEPT_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    ("green_power", "绿色电力", "electric_power"),
    ("nuclear_power", "核能核电", "electric_power"),
    ("grain_concept", "粮食概念", "agriculture"),
    ("pork_concept", "猪肉概念", "agriculture"),
    ("chicken_concept", "鸡肉概念", "agriculture"),
    ("gold_concept", "黄金概念", "precious_metals"),
    ("oil_gas_services", "油气设服", "oil_gas"),
    ("oil_gas_resources", "油气资源", "oil_gas"),
    ("retail_concept", "零售概念", "retail"),
    ("new_retail", "新零售", "retail"),
    ("duty_free", "免税概念", "retail"),
    ("coal_chemical", "煤化工概念", "coal"),
    ("dairy", "乳业", "food_beverage"),
    ("natural_gas", "天然气", "gas"),
    ("shale_gas", "页岩气", "gas"),
    ("traditional_chinese_medicine_concept", "中药概念", "traditional_chinese_medicine"),
    ("water_conservancy", "水利建设", "water_utilities"),
    ("medical_device_concept", "医疗器械概念", "medical_devices"),
    ("energy_conservation_environmental_protection", "节能环保", "environmental_protection"),
)


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


# Optional offense anchors expand the trajectory catalog without changing the
# eight-direction core-offense vote. Their exact curves share the same bounded
# asynchronous refresh owner as the core anchors.
_OPTIONAL_OFFENSE_SECTOR_FLOW_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "minor_metals",
        "name": "小金属",
        "category_key": "resource_cycle",
        "board_codes": ("BK1027",),
        "industry_aliases": ("小金属",),
    },
    {
        "key": "innovative_drugs",
        "name": "创新药",
        "category_key": "medical_growth",
        "board_codes": ("BK1106",),
        "industry_aliases": (),
        "concept_aliases": ("创新药",),
    },
)


# Provider-neutral business taxonomy for the tradable concept layer beneath
# the eight stable industry anchors. Runtime board codes are resolved from the
# current canonical sector snapshot; the catalog never invents a provider ID.
OFFENSE_CONCEPT_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    ("semiconductor_concept", "半导体概念", "semiconductor"),
    ("domestic_chip", "国产芯片", "semiconductor"),
    ("lithography_machine", "光刻机", "semiconductor"),
    ("advanced_packaging", "先进封装", "semiconductor"),
    ("ai_chip", "AI芯片", "semiconductor"),
    ("storage_chip", "存储芯片", "semiconductor"),
    ("third_generation_semiconductor", "第三代半导体", "semiconductor"),
    ("photoresist", "光刻胶", "semiconductor"),
    ("ai_application", "AI应用", "software_development"),
    ("artificial_intelligence", "人工智能", "software_development"),
    ("aigc", "AIGC概念", "software_development"),
    ("multimodal_ai", "多模态AI", "software_development"),
    ("ai_agent", "AI智能体", "software_development"),
    ("compute", "算力概念", "communication_equipment"),
    ("cpo", "CPO概念", "communication_equipment"),
    ("data_center", "数据中心", "communication_equipment"),
    ("liquid_cooling_server", "液冷服务器", "communication_equipment"),
    ("robotics", "机器人概念", "automation_equipment"),
    ("humanoid_robot", "人形机器人", "automation_equipment"),
    ("machine_vision", "机器视觉", "automation_equipment"),
    ("reducer", "减速器", "automation_equipment"),
    ("robot_actuator", "机器人执行器", "automation_equipment"),
    ("consumer_electronics_concept", "消费电子概念", "consumer_electronics"),
    ("apple_concept", "苹果概念", "consumer_electronics"),
    ("smart_wearable", "智能穿戴", "consumer_electronics"),
    ("ai_phone", "AI手机", "consumer_electronics"),
    ("ai_glasses", "AI眼镜", "consumer_electronics"),
    ("lithium_battery", "锂电池概念", "battery"),
    ("solid_state_battery", "固态电池", "battery"),
    ("energy_storage", "储能概念", "battery"),
    ("photovoltaic_concept", "光伏概念", "photovoltaic_equipment"),
    ("topcon_battery", "TOPCon电池", "photovoltaic_equipment"),
    ("hjt_battery", "HJT电池", "photovoltaic_equipment"),
    ("military", "军工", "defense"),
    ("aerospace", "航天航空", "defense"),
    ("commercial_spaceflight", "商业航天", "defense"),
    ("satellite_internet", "卫星互联网", "defense"),
    ("rare_earth_permanent_magnet", "稀土永磁", "minor_metals"),
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
    if history and history[-1].get("segment") == segment:
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


def _sector_flow_definitions() -> tuple[dict[str, Any], ...]:
    """Return the stable defensive industry and concept observation pool.

    Exact aliases stay owned by ``risk_appetite``.  This adapter only adds the
    display category and whether a direction may enter the follow-observation
    order. Optional definitions expand user choice but do not change legacy
    risk classification or request extra provider curve backfills.
    """

    from .risk_appetite import DEFENSE_FOCUS_DEFINITIONS, SECTOR_DEFINITIONS

    definitions = {
        item.key: item for item in (*SECTOR_DEFINITIONS, *DEFENSE_FOCUS_DEFINITIONS)
    }
    specs = (
        ("electric_power", "steady_defense", True),
        ("agriculture", "event_hedge", True),
        ("precious_metals", "event_hedge", True),
        ("oil_gas", "event_hedge", True),
        ("ports", "transport_defense", True),
        ("baijiu", "stable_consumption", True),
        ("retail", "stable_consumption", True),
        ("bank", "weight_support", False),
    )
    result: list[dict[str, Any]] = []
    for key, category_key, follow_eligible in specs:
        item = definitions[key]
        result.append({
            "key": item.key,
            "name": item.label,
            "category_key": category_key,
            "category_name": SECTOR_FLOW_CATEGORY_LABELS[category_key],
            "follow_eligible": follow_eligible,
            "coverage_required": True,
            "backfill_eligible": True,
            "layer": "anchor",
            "parent_sector_key": None,
            "parent_name": None,
            "board_codes": (),
            "industry_aliases": tuple(item.industry_aliases),
            "concept_aliases": tuple(item.concept_aliases),
        })
    for item in _OPTIONAL_SECTOR_FLOW_DEFINITIONS:
        result.append({
            **item,
            "category_name": SECTOR_FLOW_CATEGORY_LABELS[str(item["category_key"])],
            "follow_eligible": True,
            "coverage_required": False,
            "backfill_eligible": True,
            "layer": "anchor",
            "parent_sector_key": None,
            "parent_name": None,
            "concept_aliases": tuple(item.get("concept_aliases", ())),
        })
    parents = {str(item["key"]): item for item in result}
    for key, name, parent_key in DEFENSE_CONCEPT_DEFINITIONS:
        parent = parents[parent_key]
        category_key = str(parent["category_key"])
        result.append({
            "key": key,
            "name": name,
            "category_key": category_key,
            "category_name": SECTOR_FLOW_CATEGORY_LABELS[category_key],
            "follow_eligible": True,
            "coverage_required": False,
            "backfill_eligible": True,
            "layer": "concept",
            "parent_sector_key": parent_key,
            "parent_name": str(parent["name"]),
            "board_codes": (),
            "industry_aliases": (),
            "concept_aliases": (name,),
        })
    if len(result) != DEFENSE_SECTOR_FLOW_SERIES:
        raise ValueError("sector flow definition count must match contract maximum")
    return tuple(result)


def _offense_sector_flow_definitions() -> tuple[dict[str, Any], ...]:
    """Return stable core and optional anchors for the offense trajectory."""

    category_keys = {
        "科技成长": "technology_growth",
        "新能源": "new_energy",
        "事件弹性": "event_elasticity",
    }
    result: list[dict[str, Any]] = []
    for item in CORE_OFFENSE_DEFINITIONS:
        anchor = item["anchor"]
        category_key = category_keys[str(item["category"])]
        result.append({
            "key": item["key"],
            "name": item["name"],
            "category_key": category_key,
            "category_name": SECTOR_FLOW_CATEGORY_LABELS[category_key],
            "follow_eligible": True,
            "coverage_required": True,
            "backfill_eligible": True,
            "layer": "anchor",
            "parent_sector_key": None,
            "parent_name": None,
            "board_codes": tuple(anchor.get("board_codes", ())),
            "industry_aliases": tuple(anchor.get("exact_names", ())),
            "concept_aliases": (),
        })
    for item in _OPTIONAL_OFFENSE_SECTOR_FLOW_DEFINITIONS:
        result.append({
            **item,
            "category_name": SECTOR_FLOW_CATEGORY_LABELS[str(item["category_key"])],
            "follow_eligible": True,
            "coverage_required": False,
            "backfill_eligible": True,
            "layer": "anchor",
            "parent_sector_key": None,
            "parent_name": None,
            "concept_aliases": (),
        })
    parents = {str(item["key"]): item for item in result}
    for key, name, parent_key in OFFENSE_CONCEPT_DEFINITIONS:
        parent = parents[parent_key]
        category_key = str(parent["category_key"])
        result.append({
            "key": key,
            "name": name,
            "category_key": category_key,
            "category_name": SECTOR_FLOW_CATEGORY_LABELS[category_key],
            "follow_eligible": True,
            "coverage_required": False,
            # Provider calls are serialized by the shared asynchronous owner,
            # while the Collector continues recording its minute snapshots.
            "backfill_eligible": True,
            "layer": "concept",
            "parent_sector_key": parent_key,
            "parent_name": str(parent["name"]),
            "board_codes": (),
            "industry_aliases": (),
            "concept_aliases": (name,),
        })
    if len(result) > SECTOR_FLOW_MAX_SERIES:
        raise ValueError("offense sector flow definition count exceeds contract maximum")
    return tuple(result)


def _sector_flow_definitions_for(direction: str) -> tuple[dict[str, Any], ...]:
    if direction == "defense":
        return _sector_flow_definitions()
    if direction == "offense":
        return _offense_sector_flow_definitions()
    raise ValueError("sector flow direction must be defense or offense")


def _sector_flow_source_family(value: Any) -> str:
    source = str(value or "unknown").strip().lower()
    if any(token in source for token in ("eastmoney", "push2", "em_")):
        return "eastmoney"
    if "tushare" in source:
        return "tushare"
    if any(token in source for token in ("fuyao", "tonghuashun", "ths")):
        return "fuyao"
    return source or "unknown"


def _sector_flow_match(
    ranked: Mapping[str, Mapping[str, Any]],
    definition: Mapping[str, Any],
) -> dict[str, Any] | None:
    for taxonomy, alias_field in (
        ("industry", "industry_aliases"),
        ("concept", "concept_aliases"),
    ):
        board_codes = tuple(str(value).strip().upper() for value in definition.get("board_codes", ()))
        if board_codes:
            code_order = {code: position for position, code in enumerate(board_codes)}
            code_matches = [
                dict(item)
                for item in ranked.values()
                if item.get("taxonomy") == taxonomy
                and str(item.get("board_code") or "").strip().upper() in code_order
                and _sector_flow_source_family(item.get("source")) == "eastmoney"
            ]
            if code_matches:
                return max(
                    code_matches,
                    key=lambda item: (
                        bool(item.get("effective")),
                        -code_order[str(item.get("board_code") or "").strip().upper()],
                        _choice_key(item),
                    ),
                )
        aliases = tuple(str(value).strip() for value in definition.get(alias_field, ()))
        if not aliases:
            continue
        alias_order = {name: position for position, name in enumerate(aliases)}
        matches = [
            dict(item)
            for item in ranked.values()
            if item.get("taxonomy") == taxonomy and item.get("name") in alias_order
        ]
        if matches:
            return max(
                matches,
                key=lambda item: (
                    bool(item.get("effective")),
                    -alias_order[str(item.get("name"))],
                    _choice_key(item),
                ),
            )
    return None


def sector_flow_backfill_targets(
    industry_records: Iterable[Mapping[str, Any]],
    concept_records: Iterable[Mapping[str, Any]],
    *,
    minute_bucket: datetime,
    sources: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], ...]:
    """Resolve the fixed sector pool to explicit provider board identities."""

    snapshot = normalize_rotation_snapshot(
        industry_records,
        concept_records,
        minute_bucket=minute_bucket,
        sources=sources,
        market_phase="trading",
    )
    ranked = _rank_snapshot(snapshot)
    targets: list[dict[str, str]] = []
    definitions = (
        *_sector_flow_definitions_for("defense"),
        *_sector_flow_definitions_for("offense"),
    )
    seen_sector_keys: set[str] = set()
    for definition in definitions:
        if not definition.get("backfill_eligible"):
            continue
        sector_key = str(definition["key"])
        if sector_key in seen_sector_keys:
            continue
        matched = _sector_flow_match(ranked, definition)
        if matched is None:
            continue
        board_code = str(matched.get("board_code") or "").strip().upper()
        source_family = _sector_flow_source_family(matched.get("source"))
        # BK is an Eastmoney provider identity.  Never guess a cross-vendor
        # mapping when a paid source uses another taxonomy/code system.
        if not board_code.startswith("BK") or source_family != "eastmoney":
            continue
        targets.append({
            "sector_key": sector_key,
            "name": str(definition["name"]),
            "taxonomy": str(matched["taxonomy"]),
            "provider_sector_code": board_code,
            "source_family": source_family,
        })
        seen_sector_keys.add(sector_key)
    return tuple(targets)


def _sector_flow_baseline(
    points: Sequence[Mapping[str, Any]],
    provider_as_of: datetime,
    session_segment: str,
    minutes: int,
) -> Mapping[str, Any] | None:
    cutoff = provider_as_of - timedelta(minutes=minutes)
    for point in reversed(points):
        if (
            point.get("session_segment") == session_segment
            and point.get("provider_as_of") <= cutoff
        ):
            return point
    return None


def _sector_flow_evidence_strength(value: str | None) -> str:
    return {
        "strong": "strong",
        "medium": "moderate",
        "weak": "weak",
    }.get(str(value), "unknown")


def _sector_flow_current_strength(item: Mapping[str, Any]) -> str:
    price = str(item.get("price_state") or "unknown")
    breadth = str(item.get("breadth_state") or "unknown")
    if price == "strong" and breadth == "strong":
        return "strong"
    if price == "weak" and breadth == "weak":
        return "weak"
    if price == "unknown" and breadth == "unknown":
        return "unknown"
    return "moderate"


def _sector_flow_tier(
    *,
    current_strength: str,
    fund_strength: str,
    delta_5m: float | None,
    delta_10m: float | None,
    rank_delta_5m: float | None,
    breadth_delta_5m: float | None,
) -> str:
    recent_delta = delta_5m if delta_5m is not None else delta_10m
    positive_flow = recent_delta is not None and recent_delta > 0
    negative_flow = recent_delta is not None and recent_delta < 0
    price_or_breadth_improving = bool(
        (rank_delta_5m is not None and rank_delta_5m >= 0)
        or (breadth_delta_5m is not None and breadth_delta_5m >= 0)
    )
    if (
        current_strength == "strong"
        and (fund_strength == "weak" or negative_flow)
    ) or (
        current_strength == "weak"
        and fund_strength == "strong"
        and positive_flow
    ):
        return "divergence"
    if (
        current_strength == "strong"
        and fund_strength == "strong"
        and positive_flow
        and price_or_breadth_improving
    ):
        return "confirmed_strengthening"
    if current_strength == "strong" and fund_strength != "weak":
        return "strong_pending"
    if fund_strength == "strong" and positive_flow:
        return "funds_leading"
    if current_strength == "weak" or (negative_flow and fund_strength == "weak"):
        return "retreat"
    return "observing"


def _sector_flow_series(
    ordered: Sequence[Mapping[str, Any]],
    ranked_snapshots: Sequence[Mapping[str, Mapping[str, Any]]],
    definition: Mapping[str, Any],
    supplemental_points: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    latest_ranked = ranked_snapshots[-1]
    latest_match = _sector_flow_match(latest_ranked, definition)
    base = {
        "sector_key": definition["key"],
        "name": definition["name"],
        "category_key": definition["category_key"],
        "category_name": definition["category_name"],
        "layer": definition.get("layer", "anchor"),
        "parent_sector_key": definition.get("parent_sector_key"),
        "parent_name": definition.get("parent_name"),
        "leader_board_code": None,
        "leader_snapshot": None,
        "follow_eligible": bool(definition["follow_eligible"]),
        "eligible_for_rank": False,
        "observation_rank": None,
        "rank_total": 0,
        "observation_tier": "unavailable",
        "tier_label": SECTOR_FLOW_TIER_LABELS["unavailable"],
        "latest": None,
        "points": [],
        "supporting_evidence": [],
        "counter_evidence": [],
        "flags": [],
        "reason": None,
    }
    series_match = latest_match or next(
        (
            matched
            for ranked in reversed(ranked_snapshots[:-1])
            if (matched := _sector_flow_match(ranked, definition)) is not None
        ),
        None,
    )
    supplemental_identity = next(
        (
            raw
            for raw in supplemental_points
            if raw.get("taxonomy") in {"industry", "concept"}
        ),
        None,
    )
    if series_match is None and supplemental_identity is None:
        return {
            **base,
            "taxonomy": None,
            "status": "unavailable",
            "reason": "configured_sector_not_found",
        }

    identity = str(series_match["id"]) if series_match is not None else None
    taxonomy = str(
        series_match["taxonomy"]
        if series_match is not None
        else supplemental_identity["taxonomy"]
    )
    board_code = str(
        series_match.get("board_code") if series_match is not None else ""
    ).strip().upper()
    leader_board_code = (
        board_code
        if board_code.startswith("BK") and board_code[2:].isdigit()
        else None
    )
    flags: set[str] = set()
    if latest_match is None:
        flags.add("current_sector_snapshot_missing")
    latest_source_family = _sector_flow_source_family(
        series_match.get("source")
        if series_match is not None
        else supplemental_identity.get("source_family")
    )
    series_as_of = _as_shanghai(str(ordered[-1]["minute_bucket"])).replace(
        second=0,
        microsecond=0,
    )
    series_date = series_as_of.date()
    candidates: dict[datetime, dict[str, Any]] = {}
    for raw in sorted(
        supplemental_points,
        key=lambda item: str(item.get("provider_as_of") or ""),
    ):
        provider = _provider_datetime(raw.get("provider_as_of"))
        cumulative = raw.get("cumulative_cny")
        if (
            provider is None
            or provider.date() != series_date
            or provider.replace(second=0, microsecond=0) > series_as_of
            or _sector_flow_source_family(
                raw.get("source_family") or raw.get("provider")
            )
            != latest_source_family
            or _segment(provider) not in {"am", "pm"}
            or not isinstance(cumulative, (int, float))
        ):
            continue
        minute_key = provider.replace(second=0, microsecond=0)
        existing = candidates.get(minute_key)
        if existing is not None and provider <= existing["provider_as_of"]:
            continue
        candidates[minute_key] = {
            "sampled_at": provider,
            "provider_as_of": provider,
            "session_segment": _segment(provider),
            "cumulative_cny": float(cumulative),
            "metrics": {"flow_amount": float(cumulative)},
            "from_backfill": True,
        }

    active_source: str | None = latest_source_family
    last_live_provider: datetime | None = None
    observed_other_identity = False
    for snapshot, ranked in zip(ordered, ranked_snapshots, strict=True):
        minute = _as_shanghai(str(snapshot["minute_bucket"]))
        segment = str(snapshot.get("session_segment") or _segment(minute))
        matched = _sector_flow_match(ranked, definition)
        if identity is not None and matched is not None and matched.get("id") != identity:
            observed_other_identity = True
        item = ranked.get(identity) if identity is not None else None
        if item is None:
            continue
        source = _sector_flow_source_family(item.get("source"))
        if source != latest_source_family:
            flags.add("source_changed_baseline_reset")
            continue
        provider = _provider_datetime(item.get("provider_as_of"))
        if provider is None or provider.date() != minute.date():
            flags.add("provider_time_unavailable")
            continue
        if not item.get("effective") or item.get("flow_amount") is None:
            flags.add("incomplete_sample_ignored")
            continue
        if last_live_provider is not None and provider <= last_live_provider:
            if provider == last_live_provider:
                flags.add("repeated_provider_time_ignored")
                continue
            if (
                provider.replace(second=0, microsecond=0)
                != last_live_provider.replace(second=0, microsecond=0)
            ):
                flags.add("repeated_provider_time_ignored")
                continue
        minute_key = provider.replace(second=0, microsecond=0)
        existing = candidates.get(minute_key)
        if existing is not None:
            if provider < existing["provider_as_of"]:
                flags.add("repeated_provider_time_ignored")
                continue
            flags.add("same_minute_provider_time_replaced")
        candidates[minute_key] = {
            "sampled_at": minute,
            "provider_as_of": provider,
            "session_segment": str(
                snapshot.get("session_segment") or _segment(minute)
            ),
            "cumulative_cny": item.get("flow_amount"),
            "metrics": dict(item),
            "from_backfill": False,
        }
        last_live_provider = provider

    points: list[dict[str, Any]] = []
    for raw_point in sorted(
        candidates.values(),
        key=lambda item: item["provider_as_of"],
    ):
        provider = raw_point["provider_as_of"]
        segment = raw_point["session_segment"]
        cumulative = raw_point["cumulative_cny"]
        five = _sector_flow_baseline(points, provider, segment, 5)
        ten = _sector_flow_baseline(points, provider, segment, 10)
        delta_5m = _delta(
            cumulative,
            five.get("cumulative_cny") if five else None,
        )
        delta_10m = _delta(
            cumulative,
            ten.get("cumulative_cny") if ten else None,
        )
        metrics = raw_point["metrics"]
        point = {
            "sampled_at": raw_point["sampled_at"],
            "provider_as_of": provider,
            "session_segment": segment,
            "cumulative_cny": cumulative,
            "delta_5m_cny": delta_5m,
            "delta_5m_baseline_as_of": five.get("provider_as_of") if five else None,
            "delta_10m_cny": delta_10m,
            "delta_10m_baseline_as_of": ten.get("provider_as_of") if ten else None,
            "metrics": metrics,
            "rank_delta_5m": _delta(
                metrics.get("price_percentile"),
                five.get("metrics", {}).get("price_percentile") if five else None,
            ),
            "breadth_delta_5m": _delta(
                metrics.get("breadth_ratio"),
                five.get("metrics", {}).get("breadth_ratio") if five else None,
            ),
            "change_delta_5m_pct": _delta(
                metrics.get("change_pct"),
                five.get("metrics", {}).get("change_pct") if five else None,
            ),
        }
        points.append(point)
    if any(item.get("from_backfill") for item in candidates.values()):
        flags.add("intraday_history_backfilled")
    if len(points) > SECTOR_FLOW_MAX_POINTS:
        del points[:-SECTOR_FLOW_MAX_POINTS]

    if observed_other_identity:
        flags.add("board_identity_changed_baseline_reset")
    if not points:
        return {
            **base,
            "taxonomy": taxonomy,
            "status": "unavailable",
            "flags": sorted(flags),
            "reason": "no_comparable_flow_samples",
        }

    final = points[-1]
    metrics = final["metrics"]
    current_provider = (
        _provider_datetime(latest_match.get("provider_as_of"))
        if latest_match is not None
        else None
    )
    current_usable = bool(
        latest_match is not None
        and latest_match.get("effective")
        and current_provider is not None
        and current_provider == final["provider_as_of"]
        and _sector_flow_source_family(latest_match.get("source")) == active_source
    )
    current_strength = _sector_flow_current_strength(metrics)
    fund_strength = _sector_flow_evidence_strength(metrics.get("fund_state"))
    delta_5m = final["delta_5m_cny"]
    delta_10m = final["delta_10m_cny"]
    recent_delta = delta_5m if delta_5m is not None else delta_10m
    incremental_direction = (
        "inflow" if recent_delta is not None and recent_delta > 0
        else "outflow" if recent_delta is not None and recent_delta < 0
        else "flat" if recent_delta == 0
        else "unknown"
    )
    tier = _sector_flow_tier(
        current_strength=current_strength,
        fund_strength=fund_strength,
        delta_5m=delta_5m,
        delta_10m=delta_10m,
        rank_delta_5m=final["rank_delta_5m"],
        breadth_delta_5m=final["breadth_delta_5m"],
    )
    complete_current = all(
        metrics.get(field) is not None
        for field in ("change_pct", "breadth_ratio", "flow_amount", "flow_ratio")
    )
    eligible_for_rank = bool(
        definition["follow_eligible"]
        and current_usable
        and metrics.get("flow_percentile") is not None
        and current_strength != "unknown"
    )
    if not current_usable:
        status = "partial"
        flags.add("latest_snapshot_incomplete")
        eligible_for_rank = False
    elif not complete_current:
        status = "partial"
        flags.add("partial_current_evidence")
    elif delta_5m is None:
        status = "collecting"
        flags.add("five_minute_baseline_collecting")
    else:
        status = "ready"

    supporting: list[str] = []
    counter: list[str] = []
    cumulative = metrics.get("flow_amount")
    if isinstance(cumulative, (int, float)):
        target = supporting if cumulative > 0 else counter
        target.append("当日累计估算净流入" if cumulative > 0 else "当日累计估算净流出")
    flow_percentile = metrics.get("flow_percentile")
    if isinstance(flow_percentile, (int, float)):
        if flow_percentile >= 0.70:
            supporting.append(f"资金位置处于同类{flow_percentile:.0%}分位")
        elif flow_percentile <= 0.30:
            counter.append(f"资金位置仅处于同类{flow_percentile:.0%}分位")
    if delta_5m is not None:
        (supporting if delta_5m > 0 else counter).append(
            "近5分钟边际流入" if delta_5m > 0 else "近5分钟边际流出"
        )
    else:
        counter.append("近5分钟同源基线仍在积累")
    if tier == "divergence":
        counter.append("价格、广度与资金未形成同步确认")
    if not definition["follow_eligible"]:
        counter.append("权重承接仅作盘面背景，不进入跟随观察顺位")

    latest = {
        "provider_as_of": final["provider_as_of"],
        "change_pct": metrics.get("change_pct"),
        "breadth_ratio": metrics.get("breadth_ratio"),
        "cumulative_cny": cumulative,
        "main_net_inflow_pct": metrics.get("flow_ratio"),
        "price_percentile": metrics.get("price_percentile"),
        "flow_percentile": flow_percentile,
        "delta_5m_cny": delta_5m,
        "delta_10m_cny": delta_10m,
        "delta_5m_baseline_as_of": final["delta_5m_baseline_as_of"],
        "delta_10m_baseline_as_of": final["delta_10m_baseline_as_of"],
        "change_delta_5m_pct": final["change_delta_5m_pct"],
        "current_strength": current_strength,
        "fund_strength": fund_strength,
        "incremental_direction": incremental_direction,
    }
    public_points = [
        {
            key: point.get(key)
            for key in (
                "sampled_at",
                "provider_as_of",
                "session_segment",
                "cumulative_cny",
                "delta_5m_cny",
                "delta_5m_baseline_as_of",
            )
        }
        for point in points
    ]
    return {
        **base,
        "taxonomy": taxonomy,
        "leader_board_code": leader_board_code,
        "status": status,
        "eligible_for_rank": eligible_for_rank,
        "observation_tier": tier,
        "tier_label": SECTOR_FLOW_TIER_LABELS[tier],
        "latest": latest,
        "points": public_points,
        "supporting_evidence": supporting[:4],
        "counter_evidence": counter[:4],
        "flags": sorted(flags),
    }


def analyze_sector_flow_snapshots(
    snapshots: Iterable[Mapping[str, Any]],
    supplemental_points: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    *,
    direction: str = "defense",
) -> dict[str, Any]:
    """Build one stable, provider-neutral directional fund-flow trajectory."""

    definitions = _sector_flow_definitions_for(direction)

    ordered = sorted(
        (dict(snapshot) for snapshot in snapshots),
        key=lambda snapshot: str(snapshot.get("minute_bucket", "")),
    )
    if not ordered:
        return {
            "contract": SECTOR_FLOW_CONTRACT,
            "schema_version": SECTOR_FLOW_SCHEMA_VERSION,
            "direction": direction,
            "status": "unavailable",
            "trade_date": None,
            "as_of": None,
            "market_phase": "unknown",
            "trajectory_scope": "trading_session_to_as_of",
            "marginal_window_minutes": 5,
            "sectors": [],
            "flags": ["no_rotation_samples"],
            "reason": "no_rotation_samples",
        }
    latest_date = _as_shanghai(str(ordered[-1]["minute_bucket"])).date()
    ordered = [
        snapshot
        for snapshot in ordered
        if _as_shanghai(str(snapshot["minute_bucket"])).date() == latest_date
    ]
    ranked_snapshots = [_rank_snapshot(snapshot) for snapshot in ordered]
    supplements = supplemental_points or {}
    sectors = [
        _sector_flow_series(
            ordered,
            ranked_snapshots,
            definition,
            supplements.get(str(definition["key"]), ()),
        )
        for definition in definitions
    ]
    required_keys = {
        str(definition["key"])
        for definition in definitions
        if definition.get("coverage_required")
    }
    tier_order = {
        "confirmed_strengthening": 0,
        "strong_pending": 1,
        "funds_leading": 2,
        "observing": 3,
        "divergence": 4,
        "retreat": 5,
        "unavailable": 6,
    }

    def rank_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
        latest = item.get("latest") or {}
        status_band = {"ready": 0, "collecting": 1, "partial": 2}.get(
            str(item.get("status")), 3
        )
        return (
            status_band,
            tier_order.get(str(item.get("observation_tier")), 9),
            -(latest.get("flow_percentile") if latest.get("flow_percentile") is not None else -1),
            -(latest.get("breadth_ratio") if latest.get("breadth_ratio") is not None else -1),
            -(latest.get("price_percentile") if latest.get("price_percentile") is not None else -1),
            str(item.get("sector_key")),
        )

    ranked = sorted(
        (item for item in sectors if item.get("eligible_for_rank")),
        key=rank_key,
    )
    rank_by_key = {
        item["sector_key"]: position
        for position, item in enumerate(ranked, start=1)
    }
    rank_total = len(ranked)
    for item in sectors:
        item["rank_total"] = rank_total
        item["observation_rank"] = rank_by_key.get(item["sector_key"])
    sectors.sort(key=lambda item: (
        item.get("observation_rank") is None,
        item.get("observation_rank") or 99,
        str(item.get("category_key")),
        str(item.get("sector_key")),
    ))

    available = [item for item in sectors if item.get("latest") is not None]
    required = [item for item in sectors if item.get("sector_key") in required_keys]
    if not available:
        status = "unavailable"
        reason = "no_comparable_flow_samples"
    elif not any(
        (item.get("latest") or {}).get("delta_5m_cny") is not None
        for item in available
    ):
        status = "collecting"
        reason = None
    elif any(item.get("status") in {"partial", "unavailable"} for item in required):
        status = "partial"
        reason = None
    else:
        status = "ready"
        reason = None
    flags = [
        "provider_estimated_flow",
        "taxonomy_local_percentiles",
    ]
    if status == "collecting":
        flags.append("five_minute_baseline_collecting")
    if status == "partial":
        flags.append("partial_sector_coverage")
    if any("intraday_history_backfilled" in item.get("flags", ()) for item in sectors):
        flags.append("intraday_history_backfilled")
    latest = ordered[-1]
    return {
        "contract": SECTOR_FLOW_CONTRACT,
        "schema_version": SECTOR_FLOW_SCHEMA_VERSION,
        "direction": direction,
        "status": status,
        "trade_date": latest_date.isoformat(),
        "as_of": str(latest["minute_bucket"]),
        "market_phase": str(latest.get("market_phase") or "unknown"),
        "trajectory_scope": "trading_session_to_as_of",
        "marginal_window_minutes": 5,
        "sectors": sectors,
        "flags": flags,
        "reason": reason,
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
        "sector_flow_trajectory": analyze_sector_flow_snapshots([]),
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
        "sector_flow_trajectory": analyze_sector_flow_snapshots(ordered),
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
    "SECTOR_FLOW_CONTRACT",
    "SECTOR_FLOW_SCHEMA_VERSION",
    "analyze_sector_flow_snapshots",
    "analyze_rotation_snapshots",
    "canonical_family",
    "normalize_rotation_snapshot",
]
