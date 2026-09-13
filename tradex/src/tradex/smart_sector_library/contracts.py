"""Public contracts for the Smart Sector Library (聪明板块库)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SMART_SECTOR_LIBRARY_NAME = "聪明板块库"


class SmartSectorPolicyV1(BaseModel):
    """Machine-readable boundary of the shared classification capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: Literal["smart_sector_policy.v1"] = "smart_sector_policy.v1"
    schema_version: Literal[1] = 1
    library_name: Literal["聪明板块库"] = SMART_SECTOR_LIBRARY_NAME
    scope: Literal["exact_trade_date_market_attribution"] = (
        "exact_trade_date_market_attribution"
    )
    market_context_required: Literal[True] = True
    business_evidence_required: Literal[True] = True
    reviewed_effective_scope: Literal["exact_trade_date"] = "exact_trade_date"
    stable_taxonomy_writeback: Literal[False] = False
    new_business_gate: Literal["commercial_evidence_required"] = (
        "commercial_evidence_required"
    )
    category_granularity: Literal["most_specific_supported_market_node"] = (
        "most_specific_supported_market_node"
    )
    unresolved_behavior: Literal["abstain"] = "abstain"
    primary_resolution: Literal["reusable_evidence_rule"] = "reusable_evidence_rule"
    human_review_role: Literal["calibration_and_exception_only"] = (
        "calibration_and_exception_only"
    )


class SmartSectorCandidateV1(BaseModel):
    """One evidence-backed interpretation considered by the smart resolver."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: Literal["smart_sector_candidate.v1"] = "smart_sector_candidate.v1"
    schema_version: Literal[1] = 1
    category_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    category_name: str = Field(min_length=1)
    relation_type: Literal[
        "direct_product_or_service",
        "system_function",
        "operating_asset_or_output",
        "downstream_demand",
        "material_domain",
        "commercialized_new_business",
        "capital_relation",
        "production_method",
        "statistical_industry",
    ]
    evidence_stage: Literal[
        "operating_output",
        "revenue",
        "orders",
        "delivery",
        "project",
        "product",
        "pilot",
        "plan",
        "investment",
        "rumor",
    ]
    materiality: Literal["dominant", "meaningful", "emerging", "unknown"]
    market_alignment: Literal[
        "leading_cluster",
        "co_movement",
        "reason_only",
        "none",
    ]
    granularity: Literal["specific", "parent"] = "specific"
    direct_business_evidence: bool
    evidence_quality: Literal[
        "verified",
        "corroborated",
        "provider_only",
        "unverified",
    ] = "unverified"
    evidence_refs: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence_refs(self) -> "SmartSectorCandidateV1":
        if any(not item.strip() for item in self.evidence_refs):
            raise ValueError("smart sector candidate evidence refs cannot be empty")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("smart sector candidate evidence refs must be unique")
        return self


class SmartSectorDecisionV1(BaseModel):
    """One date-bound decision returned to any Tradex consumer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: Literal["smart_sector_decision.v1"] = "smart_sector_decision.v1"
    schema_version: Literal[1] = 1
    library_name: Literal["聪明板块库"] = SMART_SECTOR_LIBRARY_NAME
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    effective_on: date
    category_key: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    category_name: str | None = None
    basis: Literal[
        "manual_market_review",
        "event_business_crosscheck",
        "evidence_candidate_ranking",
        "unresolved",
    ]
    rule_id: str | None = None
    review_basis: Literal["rule_supported", "human_review_override"] | None = None
    logic_type: Literal[
        "direct_product_or_service",
        "system_function",
        "operating_asset_or_output",
        "downstream_demand",
        "material_domain",
        "commercialized_new_business",
        "human_exception",
    ] | None = None
    quality: Literal["accepted", "degraded"]
    quality_flags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_decision(self) -> "SmartSectorDecisionV1":
        if bool(self.category_key) != bool(self.category_name):
            raise ValueError("smart sector key and name must be present together")
        if self.basis == "unresolved":
            if self.category_key or self.rule_id or self.review_basis or self.logic_type:
                raise ValueError("unresolved smart sector cannot carry a decision")
            if self.quality != "degraded" or not self.quality_flags:
                raise ValueError("unresolved smart sector must explain degraded quality")
        else:
            if not self.category_key or not self.rule_id or not self.logic_type:
                raise ValueError("resolved smart sector requires category and rule")
            if self.quality != "accepted" or self.quality_flags:
                raise ValueError("resolved smart sector must be accepted")
        if self.basis == "manual_market_review" and self.review_basis is None:
            raise ValueError("manual market review requires its review basis")
        if self.basis != "manual_market_review" and self.review_basis is not None:
            raise ValueError("only manual market review can carry review basis")
        return self


__all__ = [
    "SMART_SECTOR_LIBRARY_NAME",
    "SmartSectorCandidateV1",
    "SmartSectorDecisionV1",
    "SmartSectorPolicyV1",
]
