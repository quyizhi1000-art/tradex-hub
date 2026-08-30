"""Evidence-gated, day-level market display attribution for hot stocks.

The instrument taxonomy answers what a company does.  This module answers
which verified business theme the market is trading on one date.  A provider
reason can never pass by itself: the same rule must also match disclosed
business evidence.  Human-reviewed entries are exact-date evidence and are
validated through the same reusable rules when loaded.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from importlib.resources import files
from pathlib import Path
from typing import Iterable

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


@dataclass(frozen=True)
class MarketThemeMatch:
    rule_id: str
    category_key: str
    category_name: str


# More specific themes precede broader themes.  Each business expression is a
# direct product, service, contract, or application relationship; generic
# concept membership is intentionally insufficient.
MARKET_THEME_RULES: tuple[MarketThemeRule, ...] = (
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
        "liquid_cooling_from_cooling_equipment",
        "liquid_cooling",
        "液冷",
        _patterns(r"液冷", r"数据中心.{0,8}冷却", r"冷却.{0,8}数据中心"),
        _patterns(r"液冷.{0,12}冷却塔", r"冷却塔.{0,12}液冷", r"数据中心.{0,12}冷却塔"),
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
        "power_grid_from_transmission_equipment",
        "power_grid",
        "电力电网",
        _patterns(r"电力电网", r"电网设备", r"特高压", r"输电线路"),
        _patterns(r"输电线路铁塔", r"角钢塔", r"钢管塔", r"1000kV", r"变电构支架"),
    ),
    MarketThemeRule(
        "compute_from_compute_service",
        "computing_power",
        "算力",
        _patterns(r"算力", r"智算", r"人工智能基础设施", r"数据中心"),
        _patterns(
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
        _patterns(r"算力", r"\bIDC\b", r"数据中心"),
        _patterns(r"互联网数据中心", r"\bIDC\b", r"数据中心业务", r"主机托管"),
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
            r"智能体",
            r"\bAIGC\b",
            r"人工智能应用",
        ),
        _patterns(
            r"AI营销",
            r"AI应用分发",
            r"企业级AI应用",
            r"空间智能MaaS",
            r"AI Agent",
            r"AI智能体",
            r"智能体产品",
            r"EIOSpace",
            r"企业智能运行空间",
            r"工业软件AI",
        ),
    ),
    MarketThemeRule(
        "precious_metals_from_jewellery_following",
        "precious_metals",
        "贵金属",
        _patterns(r"黄金", r"贵金属", r"金银"),
        _patterns(r"黄金珠宝", r"黄金首饰", r"黄金镶嵌", r"金银珠宝"),
    ),
)


def infer_current_market_category(
    reason_text: str | None,
    business_evidence: Iterable[str],
) -> MarketThemeMatch | None:
    """Return a current theme only when market and business evidence agree."""

    reason = str(reason_text or "").strip()
    business = "；".join(
        str(item or "").strip()
        for item in business_evidence
        if str(item or "").strip()
    )
    if not reason or not business:
        return None
    for rule in MARKET_THEME_RULES:
        if not any(pattern.search(reason) for pattern in rule.reason_patterns):
            continue
        if not any(pattern.search(business) for pattern in rule.business_patterns):
            continue
        return MarketThemeMatch(
            rule_id=rule.rule_id,
            category_key=rule.category_key,
            category_name=rule.category_name,
        )
    return None


class ReviewedMarketAttributionV1(BaseModel):
    """One human-reviewed display category, valid for exactly one trade date."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    effective_on: date
    category_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    category_name: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    reason_text: str = Field(min_length=1)
    business_evidence: tuple[str, ...] = Field(min_length=1)
    evidence: tuple[EvidenceRefV1, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def require_reusable_rule_support(self) -> "ReviewedMarketAttributionV1":
        inferred = infer_current_market_category(self.reason_text, self.business_evidence)
        if inferred is None:
            raise ValueError("reviewed market attribution does not pass a reusable rule")
        if (
            inferred.rule_id,
            inferred.category_key,
            inferred.category_name,
        ) != (self.rule_id, self.category_key, self.category_name):
            raise ValueError("reviewed market attribution conflicts with its reusable rule")
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
        payload.get("contract") != "reviewed_market_attribution.v1"
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
    "MARKET_THEME_RULES",
    "MarketThemeMatch",
    "MarketThemeRule",
    "ReviewedMarketAttributionV1",
    "infer_current_market_category",
    "load_reviewed_market_attributions",
]
