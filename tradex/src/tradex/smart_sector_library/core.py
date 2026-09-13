"""Evidence-gated core of the reusable Smart Sector Library.

The instrument taxonomy answers what a company does.  The Smart Sector Library
answers which verified business theme the market is trading on one date.  A provider
reason can never pass by itself: the same rule must also match disclosed
business evidence.  Human-reviewed entries are exact-date evidence.  A review
that lacks reusable public support must remain an explicit human override and
cannot silently train a generic attribution rule.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from importlib.resources import files
from pathlib import Path
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradex.instrument_taxonomy.contracts import EvidenceRefV1


def _patterns(*values: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(value, re.IGNORECASE) for value in values)


@dataclass(frozen=True)
class MarketThemeRule:
    rule_id: str
    category_key: str
    category_name: str
    reason_patterns: tuple[re.Pattern[str], ...]
    business_patterns: tuple[re.Pattern[str], ...]
    logic_type: Literal[
        "direct_product_or_service",
        "system_function",
        "operating_asset_or_output",
        "downstream_demand",
        "material_domain",
        "commercialized_new_business",
    ] = "direct_product_or_service"
    evidence_stage: Literal[
        "operating_output",
        "revenue",
        "orders",
        "delivery",
        "project",
        "product",
        "pilot",
    ] = "product"
    materiality: Literal["dominant", "meaningful", "emerging", "unknown"] = (
        "meaningful"
    )
    granularity: Literal["specific", "parent"] = "specific"


@dataclass(frozen=True)
class MarketThemeMatch:
    rule_id: str
    category_key: str
    category_name: str
    logic_type: Literal[
        "direct_product_or_service",
        "system_function",
        "operating_asset_or_output",
        "downstream_demand",
        "material_domain",
        "commercialized_new_business",
    ]
    evidence_stage: Literal[
        "operating_output",
        "revenue",
        "orders",
        "delivery",
        "project",
        "product",
        "pilot",
    ] = "product"
    materiality: Literal["dominant", "meaningful", "emerging", "unknown"] = (
        "meaningful"
    )
    granularity: Literal["specific", "parent"] = "specific"
    matched_business_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class BusinessSectorRule:
    """A conservative market node supported by business evidence alone."""

    rule_id: str
    category_key: str
    category_name: str
    business_patterns: tuple[re.Pattern[str], ...]
    logic_type: Literal[
        "direct_product_or_service",
        "system_function",
        "operating_asset_or_output",
        "downstream_demand",
        "material_domain",
        "commercialized_new_business",
    ] = "direct_product_or_service"
    evidence_stage: Literal[
        "operating_output",
        "revenue",
        "orders",
        "delivery",
        "project",
        "product",
        "pilot",
    ] = "product"
    materiality: Literal["dominant", "meaningful", "emerging", "unknown"] = (
        "meaningful"
    )
    granularity: Literal["specific", "parent"] = "specific"


# More specific themes precede broader themes.  Each business expression is a
# direct product, service, contract, or application relationship; generic
# concept membership is intentionally insufficient.
MARKET_THEME_RULES: tuple[MarketThemeRule, ...] = (
    MarketThemeRule(
        "cpo_from_direct_optical_communications_component",
        "cpo",
        "CPO",
        _patterns(r"\bCPO\b", r"共封装光学", r"光模块", r"高速光通信"),
        _patterns(
            r"(?:CPO|光模块|高速光通信|空间激光通信).{0,20}(?:光学元件|光学元组件|透镜|棱镜|光引擎)",
            r"(?:光学元件|光学元组件|透镜|棱镜|光引擎).{0,20}(?:CPO|光模块|高速光通信|空间激光通信)",
        ),
    ),
    MarketThemeRule(
        "switches_from_data_center_networking",
        "network_switches",
        "交换机",
        _patterns(r"交换机", r"超节点", r"算力网络", r"智算网络", r"\bCPO\b"),
        _patterns(
            r"数据中心.{0,12}交换机",
            r"(?:400G|800G|1\.6T).{0,12}交换机",
            r"交换机.{0,12}(?:400G|800G|1\.6T|数据中心)",
            r"企业级网络设备",
        ),
    ),
    MarketThemeRule(
        "optical_fiber_from_direct_fiber_business",
        "optical_fiber",
        "光纤",
        _patterns(r"光纤", r"光缆", r"光通信", r"光棒", r"光纤预制棒"),
        _patterns(
            r"光纤预制棒",
            r"光纤光缆",
            r"光通信.{0,12}(?:光纤|光缆)",
            r"(?:A2|G\.654)光纤",
            r"(?:光纤|光通信).{0,12}(?:互联|线缆)",
            r"(?:互联|线缆).{0,12}(?:光纤|光通信)",
            r"^线缆$",
        ),
    ),
    MarketThemeRule(
        "financial_technology_from_payment_or_digital_identity",
        "financial_technology",
        "金融科技",
        _patterns(r"金融科技", r"数字人民币", r"数字支付", r"移动支付", r"跨境支付"),
        _patterns(
            r"数字人民币",
            r"支付收单",
            r"数字身份认证",
            r"智能卡生命周期管理",
            r"数字发行与支付",
            r"金融科技",
            r"数据化安全及身份认证",
        ),
        "system_function",
    ),
    MarketThemeRule(
        "military_from_aerospace_or_defense_equipment",
        "military",
        "军工",
        _patterns(
            r"军工",
            r"军用",
            r"航空装备",
            r"航天",
            r"国防",
            r"无人装备",
            r"弹药",
            r"军贸",
        ),
        _patterns(
            r"军用电源",
            r"航空机轮.{0,8}刹车",
            r"航天用.{0,8}复合材料",
            r"航空装备制造",
            r"弹药装备",
            r"军品(?:业务|装备|产品)?",
            r"轮履装甲车辆",
            r"火炮系列",
        ),
        "downstream_demand",
    ),
    MarketThemeRule(
        "consumer_electronics_from_terminal_components",
        "consumer_electronics",
        "消费电子",
        _patterns(r"消费电子", r"苹果产业链", r"AI手机", r"折叠屏", r"智能终端"),
        _patterns(
            r"消费电子.{0,16}(?:连接器|线缆|组件|零部件)",
            r"手机终端.{0,16}(?:导热|散热|膜)",
            r"(?:苹果|手机|折叠屏).{0,16}(?:供应链|零部件|组件)",
            r"智能终端精密结构件",
            r"(?:光学显示材料|石墨烯导热膜|精密金属结构件).{0,20}(?:消费电子|3C|手机)",
            r"(?:消费电子|3C|手机).{0,20}(?:光学显示材料|石墨烯导热膜|精密金属结构件)",
            r"光学显示材料",
        ),
        "downstream_demand",
    ),
    MarketThemeRule(
        "chips_from_direct_chip_business",
        "chips",
        "芯片",
        _patterns(r"芯片", r"半导体", r"AIoT", r"端侧AI"),
        _patterns(
            r"芯片设计",
            r"集成电路",
            r"处理器芯片",
            r"数模混合芯片",
            r"功率半导体",
        ),
    ),
    MarketThemeRule(
        "physical_ai_from_machine_perception",
        "physical_ai",
        "物理AI",
        _patterns(r"物理AI", r"机器视觉", r"人形机器人", r"无人驾驶", r"无人机"),
        _patterns(
            r"(?:机器视觉|机器人|无人驾驶|无人机).{0,20}(?:光学镜头|视觉感知|3D视觉|空间感知)",
            r"(?:光学镜头|视觉感知|3D视觉|空间感知).{0,20}(?:机器视觉|机器人|无人驾驶|无人机)",
            r"AI感知层",
            r"光学镜头",
            r"镜头",
        ),
        "system_function",
    ),
    MarketThemeRule(
        "physical_ai_from_embodied_endpoint_stack",
        "physical_ai",
        "物理AI",
        _patterns(r"物理AI", r"具身智能", r"端侧AI", r"智能体终端", r"人形机器人"),
        _patterns(
            r"物理AI",
            r"智能体终端",
            r"端侧AI全栈能力",
            r"AI模组.{0,8}(?:Agent|智能体)",
            r"(?:Agent|智能体).{0,8}AI模组",
            r"连接、感知、决策、执行",
            r"具身(?:智能)?机器人.{0,16}(?:方案|终端|落地)",
            r"机器人.{0,16}(?:大小脑|域控制器|核心算力板卡)",
        ),
        "system_function",
    ),
    MarketThemeRule(
        "ai_application_from_deployed_product",
        "ai_application",
        "AI应用",
        _patterns(
            r"AI应用",
            r"AI营销",
            r"AI办公",
            r"AI软件",
            r"AI影视",
            r"AI漫剧",
            r"AI内容",
            r"AI教育",
            r"AI语料",
            r"短剧",
            r"智能体",
            r"\bAIGC\b",
            r"人工智能应用",
        ),
        _patterns(
            r"AI营销",
            r"AI应用分发",
            r"企业级AI应用",
            r"空间智能MaaS",
            r"MaaS(?:服务)?平台",
            r"AI Agent",
            r"AI智能体",
            r"行业智能体",
            r"智能体产品",
            r"EIOSpace",
            r"企业智能运行空间",
            r"工业软件AI",
            r"智能Shell",
            r"智能运维",
            r"AI场景",
            r"AI影视剧",
            r"AI影视创作平台",
            r"视听传媒智创大模型",
            r"AI剧",
            r"AI短剧",
            r"AI漫剧",
            r"泡漫",
            r"一站式生成平台",
            r"AI内容生产",
            r"AI生成内容",
            r"AI智审",
            r"AI译制",
            r"AI剪辑",
            r"微短剧智能服务平台",
            r"AIGC.{0,16}(?:漫剧|短剧|内容|IP)",
            r"教育大模型",
        ),
    ),
    MarketThemeRule(
        "computing_power_from_edge_ai_compute_module",
        "computing_power",
        "算力",
        _patterns(r"AI算力", r"算力模组", r"端侧AI", r"边缘AI", r"智能模组"),
        _patterns(
            r"AI算力模组",
            r"算力模组",
            r"(?:100|[1-9]\d*)\s*TOPS",
            r"高算力.{0,8}(?:模组|座舱)",
            r"智能模组.{0,16}算力",
        ),
    ),
    MarketThemeRule(
        "pcb_from_lamination_equipment",
        "pcb",
        "PCB",
        _patterns(r"PCB", r"CCL", r"覆铜板"),
        _patterns(
            r"(?:PCB|CCL).{0,16}(?:层压设备|层压机)",
            r"(?:层压设备|层压机).{0,16}(?:PCB|CCL)",
        ),
    ),
    MarketThemeRule(
        "pcb_from_direct_material_or_board",
        "pcb",
        "PCB",
        _patterns(r"PCB", r"覆铜板", r"电子布", r"PTFE", r"铜箔"),
        _patterns(
            r"PCB",
            r"覆铜板",
            r"PCB材料",
            r"PTFE",
            r"电子布",
            r"电子信息材料",
            r"铜箔",
        ),
    ),
    MarketThemeRule(
        "liquid_cooling_from_validated_pilot_material",
        "liquid_cooling",
        "液冷",
        _patterns(r"液冷", r"数据中心.{0,8}冷却", r"服务器.{0,8}散热"),
        _patterns(
            r"液冷导热硅油.{0,24}(?:研发|验证|样品|产能|尚未市场投放)",
            r"(?:研发|验证|样品|产能).{0,24}液冷导热硅油",
        ),
        "commercialized_new_business",
        "pilot",
        "emerging",
    ),
    MarketThemeRule(
        "liquid_cooling_from_commercialized_project_or_order",
        "liquid_cooling",
        "液冷",
        _patterns(r"液冷", r"数据中心.{0,8}冷却", r"服务器.{0,8}散热"),
        _patterns(
            r"液冷.{0,16}(?:订单|项目|客户|交付|收入|供货)",
            r"(?:订单|项目|客户|交付|收入|供货).{0,16}液冷",
            r"数据中心冷却系统集成",
            r"液冷泵.{0,16}(?:订单|客户|交付)",
            r"液冷硅油.{0,16}(?:供货|量产|订单)",
        ),
        "commercialized_new_business",
        "orders",
        "emerging",
    ),
    MarketThemeRule(
        "energy_storage_from_operating_project_or_revenue",
        "energy_storage",
        "储能",
        _patterns(r"储能", r"储能电站", r"合同能源管理"),
        _patterns(
            r"储能业务.{0,16}(?:收入|项目|运营|订单)",
            r"(?:收入|项目|运营|订单).{0,16}储能业务",
            r"储能合同能源管理",
            r"工商业储能电站",
            r"储能电站.{0,16}(?:投运|运营|收入)",
        ),
        "commercialized_new_business",
        "project",
        "emerging",
    ),
    MarketThemeRule(
        "agriculture_from_owned_laser_weeding_pilot",
        "agriculture",
        "农业",
        _patterns(r"农业", r"农机", r"除草机", r"激光农业"),
        _patterns(r"激光除草(?:机|机器).{0,16}(?:研发|试验|样机|产业化)"),
        "downstream_demand",
        "pilot",
        "emerging",
    ),
    MarketThemeRule(
        "agriculture_from_direct_agricultural_application",
        "agriculture",
        "农业",
        _patterns(
            r"农业",
            r"农机",
            r"除草机",
            r"植物照明",
            r"垂直农场",
            r"温室种植",
            r"农资",
            r"饲料",
        ),
        _patterns(
            r"植物照明",
            r"温室种植",
            r"垂直农场",
            r"农业植保",
            r"农技服务",
            r"作物解决方案",
            r"化肥、农药",
            r"饲料(?:的)?(?:研发|生产|销售)",
        ),
        "downstream_demand",
    ),
    MarketThemeRule(
        "robotics_from_direct_product_or_component",
        "robotics",
        "机器人",
        _patterns(r"机器人", r"Microduck", r"机械臂", r"关节"),
        _patterns(
            r"机器人(?:本体|工作站|零部件|组件|关节|控制器|执行器|解决方案)?",
            r"机械臂",
            r"谐波减速器",
            r"行星滚柱丝杠",
            r"机器人.{0,12}(?:电源|芯片|模组|视觉|装备)",
            r"(?:电源|芯片|模组|视觉|装备).{0,12}机器人",
            r"\bAGV\b",
        ),
    ),
    MarketThemeRule(
        "automotive_from_parts_or_aftermarket_business",
        "automotive",
        "汽车",
        _patterns(r"汽车电子", r"汽车零部件", r"汽车后市场", r"汽车维修", r"汽保设备"),
        _patterns(
            r"汽车维修保养设备",
            r"汽车配套零部件",
            r"汽车后市场",
            r"专业汽保维修设备",
            r"新能源汽车维修装备",
        ),
    ),
    MarketThemeRule(
        "liquid_cooling_from_cooling_equipment",
        "liquid_cooling",
        "液冷",
        _patterns(r"液冷", r"数据中心.{0,8}冷却", r"冷却.{0,8}数据中心"),
        _patterns(r"液冷.{0,12}冷却塔", r"冷却塔.{0,12}液冷", r"数据中心.{0,12}冷却塔"),
    ),
    MarketThemeRule(
        "liquid_cooling_from_direct_component",
        "liquid_cooling",
        "液冷",
        _patterns(r"液冷", r"数据中心.{0,8}散热", r"AI.{0,8}散热"),
        _patterns(
            r"制冷(?:管路|配件|集成)",
            r"液冷(?:组件|机柜|泵|板|冷却系统|温控系统|接头|管路|产品)",
            r"向心式油冷",
            r"齿部油冷",
            r"电机精准散热",
            r"数据中心.{0,20}散热",
            r"数据中心.{0,16}(?:压缩机|中央空调|制冷)",
            r"(?:压缩机|中央空调|制冷).{0,16}数据中心",
            r"散热(?:产品|组件|器)",
            r"(?:服务器|数据中心).{0,12}机柜",
            r"液冷.{0,12}(?:蝶阀|控制阀|节水阀门)",
            r"(?:蝶阀|控制阀|节水阀门).{0,12}液冷",
        ),
        "commercialized_new_business",
        "product",
        "meaningful",
    ),
    MarketThemeRule(
        "lithium_hexafluorophosphate_from_direct_product",
        "lithium_hexafluorophosphate",
        "六氟磷酸锂",
        _patterns(r"六氟磷酸锂"),
        _patterns(r"六氟磷酸锂"),
    ),
    MarketThemeRule(
        "chemicals_from_direct_material_or_chemical_products",
        "chemicals",
        "化工",
        _patterns(
            r"化工",
            r"化学原料",
            r"化学制品",
            r"锂盐",
            r"高分子材料",
            r"玻璃纤维",
            r"多元醇",
            r"纯碱",
            r"甲醇",
        ),
        _patterns(
            r"锂盐新材料",
            r"硫酸锂",
            r"碳酸锂",
            r"超高分子量聚乙烯纤维",
            r"特种玻璃纤维",
            r"玻璃纤维增强复合材料",
            r"化工产品",
            r"锂盐(?:加工|生产|新材料)?",
            r"多元醇",
            r"纯碱",
            r"精甲醇",
        ),
        "material_domain",
    ),
    MarketThemeRule(
        "small_metals_from_industrial_precious_materials",
        "small_metals",
        "小金属",
        _patterns(r"小金属", r"黄金", r"白银", r"贵金属", r"金银"),
        _patterns(
            r"电接触材料.{0,16}白银",
            r"触头材料",
            r"贵金属回收",
            r"稀贵金属提纯",
            r"(?:银锭|黄金).{0,12}(?:回收|循环利用)",
        ),
    ),
    MarketThemeRule(
        "coal_from_coke_or_coal_business",
        "coal",
        "煤炭",
        _patterns(r"煤炭", r"焦炭", r"煤焦", r"焦煤", r"煤化工"),
        _patterns(r"焦炭", r"煤焦", r"焦煤", r"煤化工"),
    ),
    MarketThemeRule(
        "oil_gas_from_resource_or_core_equipment",
        "oil_gas",
        "油气",
        _patterns(r"油气", r"石油", r"天然气", r"页岩气", r"燃气", r"油服"),
        _patterns(
            r"页岩气勘探开发",
            r"城市燃气",
            r"天然气输配",
            r"油气(?:钻采|开采|集输|装备)",
            r"石油机械设备",
            r"压裂装备",
        ),
    ),
    MarketThemeRule(
        "electric_power_from_generation_business",
        "electric_power",
        "电力",
        _patterns(
            r"电力",
            r"绿色电力",
            r"绿电",
            r"供热",
            r"热力",
            r"垃圾焚烧",
            r"光伏发电",
            r"光伏",
            r"储能",
        ),
        _patterns(
            r"垃圾处理及发电",
            r"垃圾焚烧发电",
            r"发电供汽",
            r"热力生产与供应",
            r"综合能源服务",
            r"光伏发电量",
            r"热力(?:服务|供应|收入)",
            r"垃圾焚烧发电",
        ),
        "operating_asset_or_output",
    ),
    MarketThemeRule(
        "power_grid_from_transmission_equipment",
        "power_grid",
        "电力电网",
        _patterns(
            r"电力电网",
            r"电网设备",
            r"特高压",
            r"输电线路",
            r"配电网",
            r"海底电缆",
            r"电线电缆",
        ),
        _patterns(
            r"输电线路铁塔",
            r"角钢塔",
            r"钢管塔",
            r"1000kV",
            r"变电构支架",
            r"配售电",
            r"配电网",
            r"交联电力电缆",
            r"架空绝缘电缆",
            r"输配电.{0,12}(?:电缆|设备)",
            r"电网用复合绝缘子",
            r"电力电缆",
            r"特种电缆",
            r"(?:变电站|输配电线路).{0,12}复合外绝缘",
        ),
    ),
    MarketThemeRule(
        "media_from_publishing_or_broadcast_business",
        "media",
        "传媒",
        _patterns(
            r"传媒",
            r"出版",
            r"广电",
            r"有线电视",
            r"IPTV",
            r"广告运营",
        ),
        _patterns(
            r"图书期刊出版",
            r"新媒体出版",
            r"出版主业",
            r"有线电视(?:业务|运营|传输)",
            r"IPTV(?:集成播控|视频业务|运营)",
            r"广播电视节目(?:传输|制作|发行)",
            r"影视节目制作",
            r"广告(?:运营|设计|制作|发布|代理)",
        ),
    ),
    MarketThemeRule(
        "medical_pharma_from_drug_business",
        "medical_pharma",
        "医疗医药",
        _patterns(r"医药", r"中药", r"药品", r"制药", r"创新药", r"外用药"),
        _patterns(
            r"中药制剂",
            r"中成药",
            r"药品(?:制造|生产|流通)",
            r"医药制造",
            r"原料药",
            r"外用药",
        ),
    ),
    MarketThemeRule(
        "gaming_from_direct_game_business",
        "gaming",
        "游戏",
        _patterns(r"游戏", r"网络游戏", r"手游", r"端游", r"互动影游"),
        _patterns(
            r"网络游戏",
            r"游戏(?:研发|发行|运营|开发)",
            r"(?:手游|端游).{0,12}(?:研发|发行|运营|开发)",
        ),
    ),
    MarketThemeRule(
        "securities_from_brokerage_business",
        "securities",
        "证券",
        _patterns(r"证券", r"券商", r"大金融", r"资本市场"),
        _patterns(
            r"证券公司",
            r"代理买卖证券",
            r"财富管理类业务",
            r"投资银行类业务",
            r"资产管理类业务",
        ),
    ),
    MarketThemeRule(
        "media_from_film_cinema_business",
        "media",
        "传媒",
        _patterns(r"传媒", r"电影", r"影视", r"院线", r"短剧", r"互动影游"),
        _patterns(
            r"电影发行",
            r"电影放映",
            r"电影院线",
            r"影院及院线",
            r"影院经营",
            r"电影发行放映产业链",
        ),
    ),
    MarketThemeRule(
        "computing_power_from_data_center_generation_equipment",
        "computing_power",
        "算力",
        _patterns(
            r"(?:算力|数据中心|AIDC).{0,24}(?:柴发|燃机|燃气轮机|发电机组|备用电源)",
            r"(?:柴发|燃机|燃气轮机|发电机组|备用电源).{0,24}(?:算力|数据中心|AIDC)",
        ),
        _patterns(
            r"发电用大缸径柴油发动机",
            r"燃气轮机发电机组",
            r"数据中心.{0,16}备用电源",
            r"算力中心.{0,16}发动机(?:缸体|缸盖)",
        ),
    ),
    MarketThemeRule(
        "compute_from_compute_service",
        "computing_power",
        "算力",
        _patterns(r"算力", r"智算", r"人工智能基础设施", r"数据中心"),
        _patterns(
            r"算力服务",
            r"算力及相关服务",
            r"算力出租",
            r"人工智能算力中心",
            r"智算中心.{0,16}(?:建设|运营|托管)",
            r"算力中心.{0,16}(?:设备采购|运营|建设)",
            r"(?:500P|1000P).{0,12}算力",
        ),
    ),
    MarketThemeRule(
        "compute_from_idc_service",
        "computing_power",
        "算力",
        _patterns(r"算力", r"IDC", r"数据中心"),
        _patterns(r"互联网数据中心", r"IDC", r"数据中心业务", r"主机托管"),
    ),
    MarketThemeRule(
        "media_from_short_drama_business",
        "media",
        "传媒",
        _patterns(r"短剧", r"互动影游", r"微短剧", r"漫剧"),
        _patterns(
            r"数字阅读",
            r"版权",
            r"影视剧",
            r"视频节目",
            r"电影",
            r"互动娱乐",
            r"传媒",
            r"图书",
        ),
    ),
    MarketThemeRule(
        "real_estate_from_property_business",
        "real_estate",
        "房地产",
        _patterns(r"房地产", r"住宅装饰", r"建筑装饰", r"装配式装修", r"全屋定制", r"家居"),
        _patterns(
            r"房地产",
            r"房产经纪",
            r"物业管理",
            r"住宅装饰",
            r"建筑装饰",
            r"装饰(?:装修|施工|工程|收入)",
            r"房屋建筑施工",
            r"建筑工程",
            r"建筑施工",
            r"木门、墙板、柜类",
            r"整木定制家居",
        ),
        "downstream_demand",
    ),
    MarketThemeRule(
        "agriculture_from_crop_or_forestry_business",
        "agriculture",
        "农业",
        _patterns(r"农业", r"种业", r"育种", r"玉米", r"棉花", r"林业", r"农机", r"植物照明"),
        _patterns(
            r"农业",
            r"种子",
            r"玉米",
            r"棉",
            r"杉",
            r"林业",
            r"原木",
            r"植物照明",
            r"温室种植",
            r"垂直农场",
            r"农资流通",
            r"农技服务",
            r"作物解决方案",
            r"饲料(?:的)?(?:研发|生产|销售)",
            r"化肥、农药",
        ),
        "downstream_demand",
    ),
    MarketThemeRule(
        "fertilizer_from_fertilizer_business",
        "fertilizer",
        "化肥",
        _patterns(r"化肥", r"尿素"),
        _patterns(r"化肥", r"尿素"),
    ),
    MarketThemeRule(
        "industrial_machine_tools_from_direct_machine_tools",
        "industrial_machine_tools",
        "工业母机",
        _patterns(r"工业母机", r"五轴机床"),
        _patterns(r"数控机床", r"机床(?:产品|销售|研发|制造)?", r"五轴"),
    ),
    MarketThemeRule(
        "media_from_film_or_publishing_business",
        "film_media",
        "影视传媒",
        _patterns(
            r"影视院线",
            r"院线经营",
            r"电影放映",
            r"IP商业化",
            r"出版发行",
            r"数字阅读",
            r"传媒内容",
        ),
        _patterns(r"传媒", r"电影", r"影院", r"院线", r"图书", r"数字阅读", r"版权"),
    ),
    MarketThemeRule(
        "consumer_from_retail_food_or_home_business",
        "consumer",
        "消费",
        _patterns(
            r"零售",
            r"食品",
            r"家居",
            r"木作",
            r"全屋定制",
            r"五金",
            r"拉链",
            r"厨卫",
            r"鞋履",
            r"皮鞋",
            r"鞋服",
            r"大消费",
            r"童装",
            r"男裤",
            r"男装",
            r"服装",
            r"包装一体化",
            r"消费电子包装",
        ),
        _patterns(
            r"零售",
            r"食品饮料",
            r"家居",
            r"五金",
            r"拉链",
            r"厨卫",
            r"家具",
            r"鞋履",
            r"皮鞋",
            r"鞋服",
            r"鞋类",
            r"童装",
            r"儿童服饰",
            r"男裤",
            r"男装",
            r"服装",
            r"包装一体化",
            r"精品包装",
            r"消费电子.{0,12}包装",
        ),
    ),
    MarketThemeRule(
        "energy_storage_from_direct_energy_business",
        "energy_storage",
        "储能",
        _patterns(r"储能"),
        _patterns(
            r"储能",
            r"数字能源",
            r"工商业储能",
            r"合同能源管理",
            r"清洁环保能源装备",
        ),
        "commercialized_new_business",
    ),
    MarketThemeRule(
        "precious_metals_from_jewellery_following",
        "precious_metals",
        "贵金属",
        _patterns(r"黄金", r"贵金属", r"金银"),
        _patterns(r"黄金珠宝", r"黄金首饰", r"黄金镶嵌", r"金银珠宝"),
    ),
)


# Business-only candidates are deliberately narrower than same-day rules.  They
# provide a real operating baseline when the provider reason is a weak concept,
# but they are never evaluated when the market reason itself is absent.
BUSINESS_SECTOR_RULES: tuple[BusinessSectorRule, ...] = (
    BusinessSectorRule(
        "business_liquid_cooling_from_direct_thermal_product",
        "liquid_cooling",
        "液冷",
        _patterns(
            r"液冷(?:组件|机柜|泵|板|冷却系统|温控系统|接头|管路|产品)",
            r"向心式油冷",
            r"齿部油冷",
            r"电机精准散热",
            r"数据中心.{0,16}(?:压缩机|中央空调|制冷|散热)",
            r"(?:压缩机|中央空调|制冷|散热).{0,16}数据中心",
        ),
        "commercialized_new_business",
        "product",
        "meaningful",
    ),
    BusinessSectorRule(
        "business_optical_fiber_from_direct_product",
        "optical_fiber",
        "光纤",
        _patterns(r"光纤预制棒", r"光纤光缆", r"光纤", r"光缆"),
    ),
    BusinessSectorRule(
        "business_financial_technology_from_financial_system",
        "financial_technology",
        "金融科技",
        _patterns(r"金融科技", r"数字人民币", r"支付收单", r"数字身份认证"),
        "system_function",
    ),
    BusinessSectorRule(
        "business_consumer_electronics_from_terminal_product",
        "consumer_electronics",
        "消费电子",
        _patterns(
            r"消费电子",
            r"3C消费电子",
            r"智能终端精密结构件",
            r"光学显示材料",
            r"石墨烯导热膜",
        ),
        "downstream_demand",
    ),
    BusinessSectorRule(
        "business_military_from_defense_output",
        "military",
        "军工",
        _patterns(
            r"军品(?:业务|装备|产品)?",
            r"弹药装备",
            r"轮履装甲车辆",
            r"火炮系列",
            r"航空装备制造",
            r"军用电源",
        ),
        "downstream_demand",
        "revenue",
    ),
    BusinessSectorRule(
        "business_power_grid_from_transmission_product",
        "power_grid",
        "电力电网",
        _patterns(
            r"电力电缆",
            r"特种电缆",
            r"输配电线路复合外绝缘",
            r"变电站复合外绝缘",
            r"电网设备",
            r"输电线路铁塔",
        ),
        "direct_product_or_service",
        "revenue",
    ),
    BusinessSectorRule(
        "business_power_from_generation_or_heat_output",
        "electric_power",
        "电力",
        _patterns(
            r"垃圾焚烧发电",
            r"热力(?:生产|供应|服务|收入)",
            r"光伏发电(?:量|业务|收入)?",
            r"发电供汽",
        ),
        "operating_asset_or_output",
        "operating_output",
    ),
    BusinessSectorRule(
        "business_agriculture_from_crop_input_or_feed",
        "agriculture",
        "农业",
        _patterns(
            r"农技服务",
            r"农业技术服务",
            r"农资流通",
            r"农业生产资料",
            r"饲料(?:销售|研发|生产)",
            r"种子(?:生产|销售|研发)?",
        ),
        "downstream_demand",
        "revenue",
    ),
    BusinessSectorRule(
        "business_chemicals_from_direct_material",
        "chemicals",
        "化工",
        _patterns(
            r"锂盐(?:加工|生产|新材料)?",
            r"多元醇",
            r"纯碱",
            r"精甲醇",
            r"煤化工",
            r"化工产品",
        ),
        "material_domain",
        "revenue",
    ),
    BusinessSectorRule(
        "business_real_estate_from_construction_or_property",
        "real_estate",
        "房地产",
        _patterns(
            r"房地产",
            r"房产经纪",
            r"物业管理",
            r"房屋建筑施工",
            r"建筑工程",
            r"建筑施工",
            r"整木定制家居",
        ),
        "downstream_demand",
        "revenue",
    ),
    BusinessSectorRule(
        "business_consumer_from_apparel_or_retail",
        "consumer",
        "消费",
        _patterns(
            r"男裤",
            r"男装",
            r"服装",
            r"皮鞋",
            r"鞋类",
            r"童装",
            r"零售",
        ),
        "downstream_demand",
        "revenue",
    ),
    BusinessSectorRule(
        "business_medical_pharma_from_drug_output",
        "medical_pharma",
        "医疗医药",
        _patterns(r"外用药", r"中成药", r"药品(?:研发|生产|销售)", r"医药制造"),
        "direct_product_or_service",
        "revenue",
    ),
    BusinessSectorRule(
        "business_photovoltaic_from_generation_product",
        "photovoltaic",
        "光伏",
        _patterns(r"HJT电池", r"光伏组件", r"光伏电站", r"电池组件款"),
    ),
    BusinessSectorRule(
        "business_automotive_from_direct_parts",
        "automotive",
        "汽车",
        _patterns(r"汽车零部件", r"电驱动定转子", r"汽车维修保养设备"),
        "direct_product_or_service",
        "revenue",
    ),
    BusinessSectorRule(
        "business_cultural_tourism_from_operating_asset",
        "cultural_tourism",
        "文旅",
        _patterns(r"景区经营", r"景区景点", r"旅行社", r"旅游"),
        "operating_asset_or_output",
        "revenue",
    ),
)


def _normalized_business_evidence(
    business_evidence: Iterable[str],
) -> tuple[str, ...]:
    return tuple(
        str(item or "").strip()
        for item in business_evidence
        if str(item or "").strip()
    )


def _matched_business_values(
    values: tuple[str, ...],
    patterns: tuple[re.Pattern[str], ...],
) -> tuple[str, ...]:
    matched = tuple(
        value for value in values if any(pattern.search(value) for pattern in patterns)
    )
    if matched:
        return matched
    joined = "；".join(values)
    return (joined,) if joined and any(pattern.search(joined) for pattern in patterns) else ()


def market_context_has_known_theme(reason_text: str | None) -> bool:
    reason = str(reason_text or "").strip()
    return bool(
        reason
        and any(
            pattern.search(reason)
            for rule in MARKET_THEME_RULES
            for pattern in rule.reason_patterns
        )
    )


def infer_current_market_categories(
    reason_text: str | None,
    business_evidence: Iterable[str],
) -> tuple[MarketThemeMatch, ...]:
    """Return every current theme whose market and business evidence agree."""

    reason = str(reason_text or "").strip()
    values = _normalized_business_evidence(business_evidence)
    if not reason or not values:
        return ()
    matches: list[MarketThemeMatch] = []
    for rule in MARKET_THEME_RULES:
        if not any(pattern.search(reason) for pattern in rule.reason_patterns):
            continue
        matched_values = _matched_business_values(values, rule.business_patterns)
        if not matched_values:
            continue
        matches.append(MarketThemeMatch(
            rule_id=rule.rule_id,
            category_key=rule.category_key,
            category_name=rule.category_name,
            logic_type=rule.logic_type,
            evidence_stage=rule.evidence_stage,
            materiality=rule.materiality,
            granularity=rule.granularity,
            matched_business_values=matched_values,
        ))
    return tuple(matches)


def infer_business_categories(
    business_evidence: Iterable[str],
) -> tuple[MarketThemeMatch, ...]:
    """Return conservative operating baselines from direct business evidence."""

    values = _normalized_business_evidence(business_evidence)
    if not values:
        return ()
    matches: list[MarketThemeMatch] = []
    for rule in BUSINESS_SECTOR_RULES:
        matched_values = _matched_business_values(values, rule.business_patterns)
        if not matched_values:
            continue
        matches.append(MarketThemeMatch(
            rule_id=rule.rule_id,
            category_key=rule.category_key,
            category_name=rule.category_name,
            logic_type=rule.logic_type,
            evidence_stage=rule.evidence_stage,
            materiality=rule.materiality,
            granularity=rule.granularity,
            matched_business_values=matched_values,
        ))
    return tuple(matches)


def infer_current_market_category(
    reason_text: str | None,
    business_evidence: Iterable[str],
) -> MarketThemeMatch | None:
    """Compatibility helper returning the first reusable cross-check."""

    matches = infer_current_market_categories(reason_text, business_evidence)
    return matches[0] if matches else None


class ReviewedMarketAttributionV1(BaseModel):
    """One human-reviewed display category, valid for exactly one trade date."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    effective_on: date
    category_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    category_name: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    review_basis: Literal["rule_supported", "human_review_override"] = "rule_supported"
    reason_text: str = Field(min_length=1)
    business_evidence: tuple[str, ...] = Field(min_length=1)
    evidence: tuple[EvidenceRefV1, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def require_reusable_rule_support(self) -> "ReviewedMarketAttributionV1":
        # A rule-supported review records the evidence and the rule version that
        # was accepted at the time.  Current rules are deliberately re-evaluated
        # by SmartSectorLibrary; an improved rule must not make the whole
        # historical evidence archive unloadable.
        if (
            self.review_basis == "human_review_override"
            and self.rule_id != "manual_market_review_override"
        ):
            raise ValueError("human review overrides require the explicit manual rule id")
        source_kinds = {item.source_kind for item in self.evidence}
        if not source_kinds & {"official_filing", "official_company"}:
            raise ValueError("reviewed market attribution requires official business evidence")
        if "web_secondary" not in source_kinds:
            raise ValueError("reviewed market attribution requires market-following evidence")
        return self


def _reviewed_attribution_path() -> Path:
    return Path(str(files(__package__).joinpath("reviewed_market_attribution.v1.json")))


def load_reviewed_market_attributions(
    effective_on: date,
    path: str | Path | None = None,
) -> dict[str, ReviewedMarketAttributionV1]:
    """Load exact-date reviewed entries; stale entries never bleed forward."""

    target = Path(path) if path is not None else _reviewed_attribution_path()
    payload = json.loads(target.read_text(encoding="utf-8"))
    if (
        payload.get("library_name") != "聪明板块库"
        or payload.get("contract") != "reviewed_market_attribution.v1"
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("entries"), list)
    ):
        raise ValueError("reviewed market attribution contract is invalid")
    result: dict[str, ReviewedMarketAttributionV1] = {}
    seen: set[tuple[str, date]] = set()
    for raw in payload["entries"]:
        entry = ReviewedMarketAttributionV1.model_validate(raw)
        identity = (entry.instrument_id, entry.effective_on)
        if identity in seen:
            raise ValueError("reviewed market attribution contains a duplicate entry")
        seen.add(identity)
        if entry.effective_on == effective_on:
            result[entry.instrument_id] = entry
    return result


__all__ = [
    "BUSINESS_SECTOR_RULES",
    "MARKET_THEME_RULES",
    "BusinessSectorRule",
    "MarketThemeMatch",
    "MarketThemeRule",
    "ReviewedMarketAttributionV1",
    "infer_business_categories",
    "infer_current_market_categories",
    "infer_current_market_category",
    "load_reviewed_market_attributions",
    "market_context_has_known_theme",
]
