"""One read-only, versioned authority for current stock market membership.

Business taxonomy is input evidence, never an automatic market-sector fallback.
Reviewed dossiers are data, shared evidence gates determine the single primary.
No provider I/O or writes occur on a consumer request.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradex.instrument_taxonomy.contracts import EvidenceRefV1
from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader

SHANGHAI = ZoneInfo("Asia/Shanghai")
METHOD_VERSION = "market-business-evidence.v2.1"


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MarketSectorCandidateV2(CatalogModel):
    sector_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    sector_name: str = Field(min_length=1)
    chain: tuple[str, ...] = ()
    relation: Literal["direct_business", "investment", "plan", "rumor"]
    investment_reviewed: bool = False
    stage: Literal["product", "pilot", "orders", "delivery", "revenue", "unknown"]
    market_role: Literal["primary", "secondary", "membership_only"]
    business_review_basis: Literal["official_disclosure", "crosschecked_sources"] = "official_disclosure"
    business_evidence_ids: tuple[str, ...] = ()
    market_evidence_ids: tuple[str, ...] = ()
    rationale: str = Field(min_length=1)


class MarketSectorDossierV2(CatalogModel):
    instrument_id: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    reviewed_on: date
    effective_from: date
    review_due: date
    candidates: tuple[MarketSectorCandidateV2, ...]
    evidence: tuple[EvidenceRefV1, ...]
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_evidence(self):
        if not self.reviewed_on <= self.effective_from <= self.review_due:
            raise ValueError("review/effective dates are not ordered")
        if self.review_due > self.reviewed_on + timedelta(days=30):
            raise ValueError("market assignment requires review within 30 days")
        refs = {e.evidence_id: e for e in self.evidence}
        if len(refs) != len(self.evidence):
            raise ValueError("duplicate evidence ids")
        if len({c.sector_key for c in self.candidates}) != len(self.candidates):
            raise ValueError("duplicate candidate sectors")
        for e in self.evidence:
            if e.retrieved_at.astimezone(SHANGHAI).date() > self.reviewed_on:
                raise ValueError("evidence retrieved after review")
            if e.published_at and e.published_at > self.reviewed_on:
                raise ValueError("evidence published after review")
            if not e.source_url or not e.source_url.startswith(("https://", "http://")):
                raise ValueError("evidence requires an inspectable source URL")
        for c in self.candidates:
            if not set((*c.business_evidence_ids, *c.market_evidence_ids)) <= refs.keys():
                raise ValueError("candidate refers to absent evidence")
        return self


class MarketMembershipV2(CatalogModel):
    contract: Literal["smart_sector_membership.v2"] = "smart_sector_membership.v2"
    schema_version: Literal[2] = 2
    instrument_id: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    name: str
    as_of: date
    status: Literal["verified", "unresolved", "stale", "disputed"]
    primary_sector_key: str | None = None
    primary_sector_name: str | None = None
    chain: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    rationale: str | None = None
    reviewed_on: date | None = None
    review_due: date | None = None
    evidence: tuple[EvidenceRefV1, ...] = ()
    flags: tuple[str, ...] = ()
    business_review_basis: Literal["official_disclosure", "crosschecked_sources"] | None = None

    @model_validator(mode="after")
    def check_primary(self):
        if self.status == "verified":
            if not self.primary_sector_key or not self.primary_sector_name or not self.evidence:
                raise ValueError("verified membership requires one primary and evidence")
        elif self.primary_sector_key or self.primary_sector_name or self.chain or self.tags:
            raise ValueError("unverified membership cannot publish attribution")
        return self


def decide_membership(dossier: MarketSectorDossierV2, as_of: date) -> MarketMembershipV2:
    base = dict(instrument_id=dossier.instrument_id, name=dossier.name, as_of=as_of)
    if as_of < dossier.effective_from or as_of < dossier.reviewed_on:
        return MarketMembershipV2(**base, status="unresolved", flags=("review_not_yet_effective",))
    base.update(reviewed_on=dossier.reviewed_on, review_due=dossier.review_due,
                evidence=dossier.evidence)
    if as_of > dossier.review_due:
        return MarketMembershipV2(**base, status="stale", flags=("market_review_expired",))
    refs = {e.evidence_id: e for e in dossier.evidence}
    supported = []
    for candidate in dossier.candidates:
        official = any(refs[k].source_kind in {"official_filing", "official_company"}
                       for k in candidate.business_evidence_ids)
        business_refs = [refs[k] for k in candidate.business_evidence_ids]
        crosschecked = (
            candidate.business_review_basis == "crosschecked_sources"
            and len({ref.publisher for ref in business_refs}) >= 2
            and any(ref.source_kind in {"web_secondary", "provider_membership"} for ref in business_refs)
            and any(ref.source_kind in {"structured_provider", "official_filing", "official_company"} for ref in business_refs)
        )
        market = any(refs[k].source_kind in {"web_secondary", "provider_membership"}
                     for k in candidate.market_evidence_ids)
        relation_supported = candidate.relation == "direct_business" or (
            candidate.relation == "investment" and candidate.investment_reviewed and official
        )
        if (relation_supported and candidate.stage != "unknown"
                and (official or crosschecked) and market):
            supported.append(candidate)
    primary = [c for c in supported if c.market_role == "primary"]
    if len(primary) != 1:
        return MarketMembershipV2(**base, status="disputed" if len(primary) > 1 else "unresolved",
                                 rationale="；".join(dossier.notes) or "尚无足够证据区分唯一市场主归属。",
                                 flags=("conflicting_primary_evidence" if len(primary) > 1
                                        else "market_business_evidence_incomplete",))
    chosen = primary[0]
    return MarketMembershipV2(**base, status="verified", primary_sector_key=chosen.sector_key,
                             primary_sector_name=chosen.sector_name, chain=chosen.chain,
                             tags=tuple(c.sector_name for c in supported if c != chosen),
                             rationale=chosen.rationale, business_review_basis=chosen.business_review_basis,
                             flags=(("secondary_sources_crosschecked",) if chosen.business_review_basis == "crosschecked_sources" else ())
                                   + (("investment_association",) if chosen.relation == "investment" else ()))


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


class SmartSectorCatalog:
    """Public membership reader; original business profiles remain separate."""

    def __init__(self, *, as_of: date | None = None, taxonomy_path=None, evidence_path=None):
        self.as_of = as_of or datetime.now(SHANGHAI).date()
        self._reader = InstrumentTaxonomyReader(taxonomy_path)
        path = Path(evidence_path or os.environ.get("TRADEX_SMART_SECTOR_EVIDENCE")
                    or Path(__file__).with_name("market_membership_evidence.v2.json"))
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("contract") != "smart_sector_evidence.v2" or raw.get("schema_version") != 2:
                raise ValueError("unexpected smart sector evidence contract")
            dossiers = tuple(MarketSectorDossierV2.model_validate(d) for d in raw["entries"])
            identities = [(d.instrument_id, d.effective_from) for d in dossiers]
            if len(identities) != len(set(identities)):
                raise ValueError("duplicate stock/effective-date evidence")
            self._evidence_error = None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raw, dossiers = {}, ()
            self._evidence_error = type(exc).__name__
        self._dossiers = {}
        for dossier in sorted(dossiers, key=lambda d: d.effective_from):
            if dossier.effective_from <= self.as_of:
                self._dossiers[dossier.instrument_id] = dossier
        self.taxonomy_status = self._reader.status()
        self.revision = _digest({"method": METHOD_VERSION, "as_of": str(self.as_of),
                                 "taxonomy": self.taxonomy_status.catalog_revision if self.taxonomy_status else None,
                                 "evidence": raw})

    def get(self, instrument_id: str) -> MarketMembershipV2:
        instrument_id = instrument_id.strip().upper()
        dossier = self._dossiers.get(instrument_id)
        if dossier:
            return decide_membership(dossier, self.as_of)
        profile = self._reader.get(instrument_id)
        return MarketMembershipV2(instrument_id=instrument_id,
                                 name=profile.name if profile else instrument_id, as_of=self.as_of,
                                 status="unresolved", flags=("evidence_store_invalid" if self._evidence_error
                                                            else "market_business_review_missing",))

    def get_many(self, instrument_ids) -> dict[str, MarketMembershipV2]:
        return {key: self.get(key) for key in dict.fromkeys(instrument_ids)}

    def business_profile(self, instrument_id):
        """Explicit background evidence, never the market membership answer."""
        return self._reader.get(instrument_id)

    def browse(self) -> dict:
        from .research import read_snapshot

        research = read_snapshot()
        profiles = self._reader.all_profiles()
        if self._reader.status() != self.taxonomy_status:
            raise RuntimeError("证券目录更新中，请重新读取")
        by_id = {p.instrument_id: p for p in profiles}
        items = []
        for key in sorted(set(by_id) | set(self._dossiers)):
            p = by_id.get(key)
            membership = self.get(key)
            row = membership.model_dump(mode="json")
            row.update(business_background=p.primary_business_name if p else None,
                       business_summary=p.business_summary if p else None,
                       business_tags=list(p.business_tags) if p else [])
            row["research"] = research["items"].get(key)
            items.append(row)
        sectors = Counter(row["primary_sector_name"] for row in items if row["status"] == "verified")
        return {"contract": "smart_sector_catalog.v2", "schema_version": 2,
                "library_name": "聪明板块库", "method_version": METHOD_VERSION,
                "revision": self.revision, "as_of": str(self.as_of),
                "taxonomy_as_of": str(self.taxonomy_status.as_of) if self.taxonomy_status else None,
                "total": len(items), "verified_total": sum(sectors.values()),
                "reviewed_total": sum(row["reviewed_on"] is not None for row in items),
                "pending_total": len(items) - sum(sectors.values()),
                "sectors": [{"name": name, "count": n} for name, n in sorted(sectors.items())],
                "quality": "degraded" if self._evidence_error or len(items) != sum(sectors.values()) else "accepted",
                "research_progress": {k: v for k, v in research.items() if k != "items"},
                "items": items}

    def close(self):
        self._reader.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def read_market_memberships(instrument_ids, *, as_of: date | None = None):
    with SmartSectorCatalog(as_of=as_of) as catalog:
        return catalog.get_many(instrument_ids)
