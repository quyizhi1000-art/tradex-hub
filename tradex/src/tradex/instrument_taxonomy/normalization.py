"""Controlled business vocabulary for source-backed company descriptions.

Rules may normalize disclosed text, but they never promote an unsupported
concept membership into a primary business assertion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class BusinessRule:
    key: str
    name: str
    patterns: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class BusinessDomain:
    """Stable top-level directory for a verified primary-business leaf."""

    key: str
    name: str
    business_keys: frozenset[str]


@dataclass(frozen=True)
class DirectoryRule:
    """Evidence-gated mapping from an exact business leaf to a market directory."""

    category_key: str
    category_name: str
    business_keys: frozenset[str]
    evidence_patterns: tuple[re.Pattern[str], ...] = ()


def _patterns(*values: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(value, re.IGNORECASE) for value in values)


# Specific product-chain nodes come before broad industries.  The list is a
# normalization aid for disclosed text, not a substitute for evidence.
BUSINESS_RULES: tuple[BusinessRule, ...] = (
    BusinessRule("storage", "存储", _patterns(
        r"存储(?:控制)?芯片", r"存储器(?:件)?", r"存储模组", r"存储半导体",
        r"(?:高端|企业级|数据|固态|嵌入式)存储", r"海普存储", r"固态硬盘",
        r"\bSSD\b", r"\bNAND\b", r"\bDRAM\b", r"\beMMC\b", r"\bUFS\b",
        r"内存条", r"闪存",
    )),
    BusinessRule("package_substrate", "封装基板", _patterns(
        r"封装基板", r"IC载板", r"芯片载板", r"FC[-－]?BGA", r"FC[-－]?CSP", r"BT类基板",
    )),
    BusinessRule("pcb", "PCB", _patterns(r"印制电路板", r"印刷电路板", r"\bPCB\b", r"覆铜板")),
    BusinessRule("optical_fiber_cable", "光纤光缆", _patterns(
        r"光纤预制棒", r"光纤光缆", r"光纤", r"光缆", r"光传输产品",
    )),
    BusinessRule("optical_module", "光模块", _patterns(r"光模块", r"光互联组件", r"\bAOC\b")),
    BusinessRule("electronic_assembly", "电子装联", _patterns(r"电子装联", r"\bPCBA\b", r"系统级封装", r"\bSiP\b")),
    BusinessRule("semiconductor_equipment", "半导体设备", _patterns(r"半导体设备", r"刻蚀机", r"薄膜沉积", r"离子注入")),
    BusinessRule("semiconductor_material", "半导体材料", _patterns(
        r"半导体材料", r"半导体(?:级)?硅片", r"硅(?:抛光|外延|测试)片",
        r"光刻胶", r"电子特气",
    )),
    BusinessRule("wafer_foundry", "晶圆制造", _patterns(r"晶圆代工", r"晶圆制造")),
    BusinessRule("chip_design", "芯片设计", _patterns(
        r"芯片设计", r"集成电路设计", r"控制芯片", r"处理器芯片",
        r"高端处理器", r"中央处理器", r"通用处理器", r"图形处理器",
    )),
    BusinessRule("electronics_distribution", "电子分销", _patterns(
        r"电子元器件(?:产品)?(?:分销|交易(?:服务)?)", r"通信产品销售",
        r"芯片分销", r"半导体分销", r"授权分销",
    )),
    BusinessRule("consumer_electronics", "消费电子", _patterns(r"消费电子", r"智能终端", r"手机零部件")),
    BusinessRule("communications_equipment", "通信设备", _patterns(r"通信设备", r"通信系统", r"基站设备", r"网络设备")),
    BusinessRule("data_center", "数据中心", _patterns(r"数据中心", r"服务器", r"算力基础设施")),
    BusinessRule("software", "软件", _patterns(r"软件开发", r"软件产品", r"信息化软件", r"SaaS")),
    BusinessRule("cyber_security", "网络安全", _patterns(r"网络安全", r"信息安全", r"数据安全")),
    BusinessRule("robotics", "机器人", _patterns(r"机器人", r"减速器", r"伺服系统")),
    BusinessRule("industrial_automation", "工业自动化", _patterns(r"工业自动化", r"工业控制", r"自动化控制")),
    BusinessRule("auto_parts", "汽车零部件", _patterns(r"汽车零部件", r"汽车电子", r"汽车配件")),
    BusinessRule("new_energy_vehicle", "新能源汽车", _patterns(r"新能源汽车", r"电动汽车")),
    BusinessRule("lithium_battery", "锂电池", _patterns(r"锂电池", r"锂离子电池", r"正极材料", r"负极材料", r"电解液", r"隔膜")),
    BusinessRule("photovoltaic", "光伏", _patterns(
        r"光伏", r"太阳能(?:电池|组件|硅片|硅棒|产品|发电)", r"硅料",
        r"单晶(?:硅片|硅棒|电池|组件)", r"硅片及硅棒",
    )),
    BusinessRule("wind_power", "风电", _patterns(r"风力发电", r"风电", r"风机叶片", r"风电塔筒")),
    BusinessRule("power_grid", "电网设备", _patterns(r"电网设备", r"输配电", r"变压器", r"电力设备")),
    BusinessRule("electric_power", "电力", _patterns(r"电力生产", r"火力发电", r"水力发电", r"核电", r"发电业务")),
    BusinessRule("fertilizer", "化肥", _patterns(r"尿素", r"复合肥", r"磷肥", r"氮肥", r"化肥")),
    BusinessRule("coal", "煤炭", _patterns(r"煤炭", r"焦煤", r"动力煤", r"煤化工")),
    BusinessRule("oil_gas", "油气", _patterns(r"石油", r"天然气", r"油气")),
    BusinessRule("nonferrous_metals", "有色金属", _patterns(r"有色金属", r"铜产品", r"电解铝", r"锌锭", r"稀土")),
    BusinessRule("jewelry_retail", "珠宝首饰零售", _patterns(
        r"珠宝首饰(?:的)?(?:品牌运营管理|零售)", r"珠宝(?:品牌运营|零售)",
    )),
    BusinessRule("precious_metals", "贵金属", _patterns(
        r"黄金", r"白银", r"贵金属", r"金银珠宝", r"黄金珠宝",
    )),
    BusinessRule("steel", "钢铁", _patterns(r"钢铁", r"钢材", r"特钢")),
    BusinessRule("apparel", "服装", _patterns(r"童装", r"服装品牌", r"儿童服饰")),
    BusinessRule("packaging", "包装", _patterns(r"包装一体化", r"运输包装", r"精品包装", r"包装产品")),
    BusinessRule("textiles", "纺织", _patterns(r"纺织品", r"纺织面料", r"纺织服装")),
    BusinessRule("fluorochemicals", "氟化工", _patterns(
        r"高端氟材料", r"氟化工", r"氟碳化学品",
        r"含氟(?:聚合物|精细化学品|锂电材料|气体)",
    )),
    BusinessRule("traditional_chinese_medicine", "中成药", _patterns(
        r"中成药", r"中药制剂", r"中药材", r"中药饮片",
    )),
    BusinessRule("pesticide", "农药", _patterns(r"化学农药", r"农药", r"杀虫剂", r"除草剂", r"杀菌剂")),
    BusinessRule("chemical", "化工", _patterns(r"^化工$", r"化工产品", r"精细化工", r"化学原料")),
    BusinessRule("innovative_drugs", "创新药", _patterns(r"创新药", r"生物药", r"单克隆抗体", r"疫苗")),
    BusinessRule("medical_devices", "医疗器械", _patterns(r"医疗器械", r"医疗设备", r"诊断试剂", r"检测试剂")),
    BusinessRule("cro", "医药研发服务", _patterns(r"\bCRO\b", r"\bCDMO\b", r"医药研发服务", r"临床研究服务")),
    BusinessRule("banking", "银行", _patterns(r"商业银行", r"银行业务")),
    BusinessRule("securities", "证券", _patterns(r"证券经纪", r"证券投资", r"证券承销", r"券商")),
    BusinessRule("insurance", "保险", _patterns(r"保险业务", r"人寿保险", r"财产保险")),
    BusinessRule("real_estate", "房地产", _patterns(r"房地产开发", r"商品房")),
    BusinessRule("commercial_real_estate_operations", "商贸流通运营", _patterns(
        r"商贸流通运营", r"家居商贸(?:运营|卖场)",
    )),
    BusinessRule("construction", "建筑工程", _patterns(r"建筑施工", r"工程承包", r"基础设施建设")),
    BusinessRule("building_materials", "建材", _patterns(r"建筑材料", r"水泥", r"玻璃纤维", r"防水材料")),
    BusinessRule("agriculture", "农业", _patterns(r"种子", r"种业", r"农作物", r"农业种植")),
    BusinessRule("animal_husbandry", "养殖", _patterns(r"生猪养殖", r"家禽养殖", r"水产养殖", r"饲料")),
    BusinessRule("food_beverage", "食品饮料", _patterns(
        r"食品", r"饮料", r"冲饮", r"乳制品", r"调味品",
    )),
    BusinessRule("liquor", "白酒", _patterns(r"白酒")),
    BusinessRule("retail", "零售", _patterns(r"商品零售", r"百货", r"连锁零售", r"电商平台")),
    BusinessRule("smart_logistics", "智能物流", _patterns(
        r"智能物流系统", r"智能仓储物流", r"智能生产物流", r"自动化立体仓库",
    )),
    BusinessRule("logistics", "物流", _patterns(r"物流", r"供应链服务", r"快递")),
    BusinessRule("shipping", "航运港口", _patterns(r"航运", r"港口", r"海运")),
    BusinessRule("air_transport", "航空运输", _patterns(r"航空运输", r"航空客运", r"航空货运")),
    BusinessRule("tourism", "旅游", _patterns(r"旅游", r"景区", r"酒店运营")),
    BusinessRule("media", "传媒", _patterns(r"传媒", r"广告业务", r"影视", r"出版")),
    BusinessRule("games", "游戏", _patterns(r"网络游戏", r"移动游戏", r"游戏研发")),
    BusinessRule("education", "教育", _patterns(r"教育培训", r"职业教育", r"教育服务")),
)


# Domains are deliberately much coarser than primary-business leaves.  They
# power directory navigation only and must never replace the leaf used for
# company identity or peer comparisons.
BUSINESS_DOMAINS: tuple[BusinessDomain, ...] = (
    BusinessDomain("chips", "芯片", frozenset({
        "storage",
        "package_substrate",
        "pcb",
        "electronic_assembly",
        "semiconductor_equipment",
        "semiconductor_material",
        "wafer_foundry",
        "chip_design",
        "electronics_distribution",
    })),
    BusinessDomain("communications", "通信", frozenset({
        "optical_fiber_cable",
        "optical_module",
        "communications_equipment",
        "data_center",
    })),
    BusinessDomain("digital_technology", "数字科技", frozenset({
        "software",
        "cyber_security",
        "games",
    })),
    BusinessDomain("industrial_manufacturing", "工业制造", frozenset({
        "robotics",
        "industrial_automation",
        "smart_logistics",
        "cooling_tower",
        "color_sorter",
    })),
    BusinessDomain("automotive", "汽车", frozenset({
        "auto_parts",
        "new_energy_vehicle",
    })),
    BusinessDomain("new_energy", "新能源", frozenset({
        "lithium_battery",
        "photovoltaic",
        "wind_power",
    })),
    BusinessDomain("utilities", "电力公用", frozenset({
        "power_grid",
        "electric_power",
    })),
    BusinessDomain("chemicals", "化工", frozenset({
        "fertilizer",
        "fluorochemicals",
        "chemical",
        "specialty_polymer_materials",
    })),
    BusinessDomain("resources", "资源", frozenset({
        "coal",
        "oil_gas",
        "nonferrous_metals",
        "precious_metals",
        "steel",
        "strontium_salts",
    })),
    BusinessDomain("medical_pharma", "医疗医药", frozenset({
        "pharmaceuticals",
        "traditional_chinese_medicine",
        "innovative_drugs",
        "medical_devices",
        "cro",
    })),
    BusinessDomain("real_estate", "房地产", frozenset({
        "real_estate",
        "real_estate_services",
        "commercial_real_estate_operations",
    })),
    BusinessDomain("consumer", "消费", frozenset({
        "consumer_electronics",
        "precision_structural_components",
        "home_appliances",
        "apparel",
        "packaging",
        "textiles",
        "food_beverage",
        "liquor",
        "retail",
        "tourism",
        "bathroom_kitchen_products",
        "jewelry_retail",
    })),
    BusinessDomain("finance", "金融", frozenset({
        "banking",
        "securities",
        "insurance",
    })),
    BusinessDomain("infrastructure", "基建建材", frozenset({
        "construction",
        "building_materials",
        "architectural_design",
    })),
    BusinessDomain("agriculture", "农业", frozenset({
        "agriculture",
        "corn_seed",
        "animal_husbandry",
        "pesticide",
    })),
    BusinessDomain("transport_logistics", "交通物流", frozenset({
        "logistics",
        "shipping",
        "air_transport",
    })),
    BusinessDomain("media_education", "传媒教育", frozenset({
        "media",
        "education",
    })),
)


# Human review teaches reusable directory relationships rather than permanent
# per-symbol aliases.  Exact leaves remain the company identity.  A thematic
# or downstream directory is selected only when the controlled leaf and the
# disclosed evidence text satisfy the same rule.
DIRECTORY_RULES: tuple[DirectoryRule, ...] = (
    DirectoryRule(
        "innovative_drugs",
        "创新药",
        frozenset({"traditional_chinese_medicine"}),
        _patterns(r"创新药研发", r"中药创新药", r"化药创新药"),
    ),
    DirectoryRule(
        "tungsten_hexafluoride",
        "六氟化钨",
        frozenset({"fluorochemicals"}),
        _patterns(r"六氟化钨"),
    ),
    DirectoryRule(
        "pcb",
        "PCB",
        frozenset({"specialty_polymer_materials"}),
        _patterns(r"\bPCB\b", r"印制电路板", r"服务器用PCB"),
    ),
    DirectoryRule(
        "ai_application",
        "AI应用",
        frozenset({"architectural_design"}),
        _patterns(r"AI(?:建筑)?设计", r"AI Agent", r"自动成图", r"智能审图"),
    ),
    DirectoryRule(
        "robotics",
        "机器人",
        frozenset({"smart_logistics"}),
        _patterns(r"机器人", r"\bAGV\b"),
    ),
    DirectoryRule(
        "consumer_electronics",
        "消费电子",
        frozenset({"precision_structural_components"}),
        _patterns(r"消费电子", r"\b3C\b", r"智能终端", r"手机零部件"),
    ),
    DirectoryRule(
        "chips",
        "芯片",
        frozenset({"electronics_distribution"}),
        _patterns(r"半导体", r"芯片", r"授权分销"),
    ),
    DirectoryRule("small_metals", "小金属", frozenset({"strontium_salts"})),
    DirectoryRule("medical_pharma", "医疗医药", frozenset({"pharmaceuticals"})),
    DirectoryRule("real_estate", "房地产", frozenset({"real_estate_services"})),
    DirectoryRule(
        "consumer",
        "消费",
        frozenset({"textiles", "bathroom_kitchen_products"}),
    ),
    DirectoryRule("agriculture", "农业", frozenset({"corn_seed", "pesticide"})),
)


_GENERIC_SEGMENTS = {
    "产品",
    "业务",
    "主营业务",
    "其他",
    "其他业务",
    "其他产品",
    "合计",
    "小计",
    "地区",
    "境内",
    "境外",
    "内销",
    "外销",
    "合计特别调整",
    "分部间抵销",
}
_SUFFIXES = re.compile(r"(?:类)?(?:产品|业务|分部)$")


def clean_segment_name(value: str) -> str | None:
    name = "".join(str(value or "").split()).strip("：:；;，,")
    if not name or name in _GENERIC_SEGMENTS:
        return None
    cleaned = _SUFFIXES.sub("", name).strip()
    return cleaned if cleaned and cleaned not in _GENERIC_SEGMENTS else None


def matched_business_rules(texts: Iterable[str]) -> tuple[BusinessRule, ...]:
    corpus = "；".join(str(item or "") for item in texts if str(item or "").strip())
    if not corpus:
        return ()
    return tuple(
        rule
        for rule in BUSINESS_RULES
        if any(pattern.search(corpus) for pattern in rule.patterns)
    )


def business_rule_by_name(name: str) -> BusinessRule | None:
    normalized = str(name or "").strip()
    return next((rule for rule in BUSINESS_RULES if rule.name == normalized), None)


def business_rule_by_key(key: str) -> BusinessRule | None:
    normalized = str(key or "").strip()
    return next((rule for rule in BUSINESS_RULES if rule.key == normalized), None)


def business_domain_by_business_key(key: str | None) -> BusinessDomain | None:
    normalized = str(key or "").strip()
    if not normalized:
        return None
    return next(
        (domain for domain in BUSINESS_DOMAINS if normalized in domain.business_keys),
        None,
    )


def reviewed_directory_category(
    business_key: str | None,
    evidence_texts: Iterable[str],
) -> tuple[str, str] | None:
    """Return a learned market directory only when its evidence gate passes."""

    normalized_key = str(business_key or "").strip()
    if not normalized_key:
        return None
    corpus = "；".join(
        str(item or "").strip()
        for item in evidence_texts
        if str(item or "").strip()
    )
    for rule in DIRECTORY_RULES:
        if normalized_key not in rule.business_keys:
            continue
        if rule.evidence_patterns and not any(pattern.search(corpus) for pattern in rule.evidence_patterns):
            continue
        return rule.category_key, rule.category_name
    return None


__all__ = [
    "BUSINESS_DOMAINS",
    "BUSINESS_RULES",
    "DIRECTORY_RULES",
    "BusinessDomain",
    "BusinessRule",
    "DirectoryRule",
    "business_domain_by_business_key",
    "business_rule_by_key",
    "business_rule_by_name",
    "clean_segment_name",
    "matched_business_rules",
    "reviewed_directory_category",
]
