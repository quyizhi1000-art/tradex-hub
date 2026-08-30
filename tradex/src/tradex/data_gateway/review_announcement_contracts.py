"""Canonical candidate-bound official announcement contracts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import ContractMetadata, ContractModel


class ReviewAnnouncementCandidateV1(ContractModel):
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1, max_length=80)
    watch_item_rank: int = Field(ge=1, le=20)
    watch_item_title: str = Field(min_length=1, max_length=200)


class ReviewAnnouncementCandidateManifestV1(ContractModel):
    contract: Literal["review_announcement_candidate_manifest.v1"] = (
        "review_announcement_candidate_manifest.v1"
    )
    schema_version: Literal[1] = 1
    trade_date: date
    review_id: str = Field(min_length=1)
    review_generated_at: datetime
    source_artifact_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidates: tuple[ReviewAnnouncementCandidateV1, ...] = Field(
        min_length=1,
        max_length=12,
    )

    @field_validator("review_generated_at")
    @classmethod
    def require_aware_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("review_generated_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_candidates(self) -> "ReviewAnnouncementCandidateManifestV1":
        codes = [item.instrument_id for item in self.candidates]
        if len(codes) != len(set(codes)):
            raise ValueError("announcement candidate instruments must be unique")
        if self.review_generated_at.date() != self.trade_date:
            raise ValueError("review generation must belong to trade_date")
        return self


class OfficialAnnouncementV1(ContractModel):
    announcement_id: str = Field(min_length=1, max_length=128)
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=500)
    published_at: datetime
    publication_precision: Literal["date", "timestamp"]
    announcement_type: str | None = Field(default=None, max_length=120)
    source_url: str = Field(pattern=r"^https://static\.cninfo\.com\.cn/.+")
    source: Literal["cninfo"] = "cninfo"

    @field_validator("published_at")
    @classmethod
    def require_aware_published_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("announcement published_at must include a timezone")
        return value


class ReviewOfficialAnnouncementArchiveV1(ContractModel):
    metadata: ContractMetadata
    trade_date: date
    review_id: str = Field(min_length=1)
    candidate_manifest_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    window_start: date
    window_end: date
    candidates: tuple[ReviewAnnouncementCandidateV1, ...] = Field(
        min_length=1,
        max_length=12,
    )
    announcements: tuple[OfficialAnnouncementV1, ...] = Field(max_length=240)

    @model_validator(mode="after")
    def validate_archive(self) -> "ReviewOfficialAnnouncementArchiveV1":
        if (
            self.metadata.contract != "review_official_announcements.v1"
            or self.metadata.schema_version != 1
        ):
            raise ValueError("official announcements require v1 metadata")
        if self.window_start != self.trade_date or self.window_end < self.window_start:
            raise ValueError("official announcement window is invalid")
        candidate_codes = {item.instrument_id for item in self.candidates}
        keys: set[tuple[str, str]] = set()
        for item in self.announcements:
            if item.instrument_id not in candidate_codes:
                raise ValueError("announcement is outside the candidate manifest")
            if not self.window_start <= item.published_at.date() <= self.window_end:
                raise ValueError("announcement is outside the archive window")
            key = (item.instrument_id, item.announcement_id)
            if key in keys:
                raise ValueError("duplicate official announcement")
            keys.add(key)
        return self


__all__ = [
    "OfficialAnnouncementV1",
    "ReviewAnnouncementCandidateManifestV1",
    "ReviewAnnouncementCandidateV1",
    "ReviewOfficialAnnouncementArchiveV1",
]
