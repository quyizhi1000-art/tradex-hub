"""Provider-neutral contracts for stock-to-sector and business relationships."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


INSTRUMENT_PATTERN = r"^\d{6}\.(?:SH|SZ|BJ)$"
REVISION_PATTERN = r"^[0-9a-f]{64}$"


class TaxonomyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


VerificationStatus = Literal[
    "verified",
    "corroborated",
    "provider_only",
    "disputed",
    "stale",
    "unresolved",
]


class EvidenceRefV1(TaxonomyModel):
    """One traceable source; search result pages themselves are never evidence."""

    evidence_id: str = Field(min_length=1)
    source_kind: Literal[
        "official_filing",
        "official_company",
        "regulatory_classification",
        "structured_provider",
        "provider_membership",
        "web_secondary",
    ]
    publisher: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source_url: str | None = None
    published_at: date | None = None
    retrieved_at: datetime
    assertions: tuple[str, ...] = Field(min_length=1)
    content_sha256: str | None = Field(default=None, pattern=REVISION_PATTERN)

    @field_validator("retrieved_at")
    @classmethod
    def require_aware_retrieved_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence retrieved_at must include a timezone")
        return value


class IndustryPathV1(TaxonomyModel):
    taxonomy: Literal["capco", "sw", "provider"]
    taxonomy_version: str = Field(min_length=1)
    level1_code: str | None = None
    level1_name: str | None = None
    level2_code: str | None = None
    level2_name: str | None = None
    level3_code: str | None = None
    level3_name: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_a_named_level(self) -> "IndustryPathV1":
        if not any((self.level1_name, self.level2_name, self.level3_name)):
            raise ValueError("industry path requires at least one named level")
        if self.effective_from and self.effective_to and self.effective_from > self.effective_to:
            raise ValueError("industry path effective dates must be ordered")
        return self


class BusinessSegmentV1(TaxonomyModel):
    name: str = Field(min_length=1)
    report_period: date
    revenue_cny: float | None = None
    profit_cny: float | None = None
    cost_cny: float | None = None
    revenue_share: float | None = Field(default=None, ge=0, le=1)
    source: str = Field(min_length=1)

    @field_validator("revenue_cny", "profit_cny", "cost_cny")
    @classmethod
    def require_finite_segment_number(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("segment numbers must be finite")
        return value


class ConceptMembershipV1(TaxonomyModel):
    taxonomy: str = Field(min_length=1)
    code: str | None = None
    name: str = Field(min_length=1)
    effective_on: date | None = None
    reason: str | None = None
    source: str = Field(min_length=1)


class StockRelationshipProfileV1(TaxonomyModel):
    """One stock's typed relationships; no field is a universal sector label."""

    contract: Literal["stock_relationship_profile.v1"] = "stock_relationship_profile.v1"
    schema_version: Literal[1] = 1
    instrument_id: str = Field(pattern=INSTRUMENT_PATTERN)
    name: str = Field(min_length=1)
    as_of: date
    regulatory_industry: IndustryPathV1 | None = None
    statistical_industry: IndustryPathV1 | None = None
    provider_industry: str | None = None
    business_domain_key: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    business_domain_name: str | None = None
    directory_category_key: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    directory_category_name: str | None = None
    primary_business_key: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    primary_business_name: str | None = None
    business_tags: tuple[str, ...] = ()
    business_summary: str | None = None
    business_segments: tuple[BusinessSegmentV1, ...] = ()
    concept_memberships: tuple[ConceptMembershipV1, ...] = ()
    verification_status: VerificationStatus
    evidence: tuple[EvidenceRefV1, ...] = ()
    flags: tuple[str, ...] = ()

    @field_validator("business_tags", "flags")
    @classmethod
    def require_unique_non_empty_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("profile tags and flags cannot contain empty values")
        if len(value) != len(set(value)):
            raise ValueError("profile tags and flags cannot contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_primary_business(self) -> "StockRelationshipProfileV1":
        if bool(self.business_domain_key) != bool(self.business_domain_name):
            raise ValueError("business domain key and name must be present together")
        if bool(self.directory_category_key) != bool(self.directory_category_name):
            raise ValueError("directory category key and name must be present together")
        if bool(self.primary_business_key) != bool(self.primary_business_name):
            raise ValueError("primary business key and name must be present together")
        if self.business_domain_key and not self.primary_business_key:
            raise ValueError("business domain requires a primary-business leaf")
        if self.directory_category_key and not self.primary_business_key:
            raise ValueError("directory category requires a primary-business leaf")
        if self.primary_business_name and self.primary_business_name not in self.business_tags:
            raise ValueError("primary business must also appear in business_tags")
        if self.verification_status in {"verified", "corroborated"} and not self.evidence:
            raise ValueError("verified relationships require evidence")
        return self


class StockRelationshipCatalogStatusV1(TaxonomyModel):
    contract: Literal["stock_relationship_catalog_status.v1"] = (
        "stock_relationship_catalog_status.v1"
    )
    schema_version: Literal[1] = 1
    catalog_revision: str = Field(pattern=REVISION_PATTERN)
    as_of: date
    generated_at: datetime
    profile_total: int = Field(ge=0)
    verified_total: int = Field(ge=0)
    corroborated_total: int = Field(ge=0)
    provider_only_total: int = Field(ge=0)
    unresolved_total: int = Field(ge=0)
    source_providers: tuple[str, ...] = ()
    source_request_ids: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()

    @field_validator("generated_at")
    @classmethod
    def require_aware_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("catalog generated_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_counts(self) -> "StockRelationshipCatalogStatusV1":
        classified = (
            self.verified_total
            + self.corroborated_total
            + self.provider_only_total
            + self.unresolved_total
        )
        if classified != self.profile_total:
            raise ValueError("catalog verification counts must cover every profile")
        return self


__all__ = [
    "BusinessSegmentV1",
    "ConceptMembershipV1",
    "EvidenceRefV1",
    "IndustryPathV1",
    "StockRelationshipCatalogStatusV1",
    "StockRelationshipProfileV1",
    "VerificationStatus",
]
