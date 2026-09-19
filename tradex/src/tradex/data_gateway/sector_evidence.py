"""Normalize supplier field names before evidence reaches the sector library."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class BusinessEvidenceSectionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    title: str
    kind: str
    text: str


class StockSectorEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract: Literal["stock_sector_evidence.v1"] = "stock_sector_evidence.v1"
    instrument_id: str
    name: str
    memberships: tuple[str, ...]
    business_sections: tuple[BusinessEvidenceSectionV1, ...]
    provider: str
    source_url: str
    fetched_at: datetime
    provider_as_of: datetime | None = None
    quality_flags: tuple[str, ...] = ("provider_timestamp_missing", "secondary_business_digest")


def map_sector_evidence(raw, provider: str, instrument_id: str) -> StockSectorEvidenceV1:
    payload = raw["payload"]
    boards, sections = payload.get("ssbk"), payload.get("hxtc")
    if not isinstance(boards, list) or not isinstance(sections, list):
        raise ValueError("sector evidence missing membership/business sections")
    rows = [*boards, *sections]
    if not rows or any(row.get("SECUCODE") != instrument_id for row in rows):
        raise ValueError("sector evidence identity mismatch or empty result")
    names = {row.get("SECURITY_NAME_ABBR") for row in rows}
    if len(names) != 1 or not next(iter(names)):
        raise ValueError("sector evidence name mismatch")
    fetched_at = datetime.fromisoformat(raw["fetched_at"])
    if fetched_at.utcoffset() is None:
        raise ValueError("evidence fetch time must have timezone")
    # Industry outlook and registered business scope are not proof of products.
    business = tuple(BusinessEvidenceSectionV1(
        title=str(row.get("KEYWORD") or ""), kind=str(row.get("KEY_CLASSIF") or ""),
        text=str(row.get("MAINPOINT_CONTENT") or "")) for row in sections
        if row.get("KEY_CLASSIF") in {"主营业务", "核心竞争力", "公司简介", "经营分析"}
        and row.get("MAINPOINT_CONTENT"))
    return StockSectorEvidenceV1(
        instrument_id=instrument_id, name=next(iter(names)),
        memberships=tuple(dict.fromkeys(str(row["BOARD_NAME"]).strip() for row in boards
                                       if row.get("BOARD_NAME"))),
        business_sections=business, provider=provider,
        source_url=raw["source_url"], fetched_at=fetched_at,
    )


def fetch_stock_sector_evidence(instrument_id: str, *, router=None) -> StockSectorEvidenceV1:
    if router is None:
        from tradex.data_sources import get_router, register_all_sources
        register_all_sources()
        router = get_router()
    result, _ = router.route_validated(
        "stock_sector_evidence", lambda raw, source: map_sector_evidence(raw, source, instrument_id),
        instrument_id=instrument_id, deadline_seconds=20, provider_deadline_seconds=18,
    )
    return result
