"""Validated editorial overlays for immutable post-market review archives."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .contracts import ContractModel
from .review import (
    ArticleSectionV4,
    PostMarketReviewPresentationV4,
    PostMarketReviewV1,
    ReviewThemeV2,
    WatchItemV4,
)


ENV_EDITORIAL_DIR = "TRADEX_REVIEW_EDITORIAL_DIR"


class ReviewEditorialOverrideV1(ContractModel):
    """A researched article layer that may change presentation, never evidence."""

    contract: Literal["post_market_review_editorial_override.v1"] = (
        "post_market_review_editorial_override.v1"
    )
    schema_version: Literal[1] = 1
    review_id: str = Field(min_length=1)
    trade_date: date
    source_count: int = Field(ge=50)
    source_audit_path: str = Field(min_length=1)
    title: str = Field(min_length=1)
    standfirst: str = Field(min_length=1)
    day_character: str = Field(min_length=1)
    core_conclusion: str = Field(min_length=1)
    sections: tuple[ArticleSectionV4, ...] = Field(min_length=5, max_length=7)
    watch_items: tuple[WatchItemV4, ...] = Field(min_length=1, max_length=5)
    themes: tuple[ReviewThemeV2, ...] = Field(default=(), max_length=3)
    money_making_effect: tuple[str, ...] = Field(min_length=1, max_length=4)
    loss_making_effect: tuple[str, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_article(self) -> "ReviewEditorialOverrideV1":
        section_ids = [item.section_id for item in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("editorial section ids must be unique")
        ranks = [item.rank for item in self.watch_items]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("editorial watch ranks must be consecutive and start at one")
        return self


class ReviewEditorialOverrideStore:
    """Read date-keyed, revisionable editorial JSON without owning market facts."""

    def __init__(self, directory: str | Path | None = None) -> None:
        configured = directory or os.environ.get(ENV_EDITORIAL_DIR)
        self.directory = (
            Path(configured)
            if configured
            else Path.home() / ".tradex" / "post_market_review_editorial"
        )

    def _path(self, trade_date_value: date | str) -> Path:
        value = date.fromisoformat(str(trade_date_value))
        return self.directory / f"{value.isoformat()}.json"

    def get(self, trade_date_value: date | str) -> ReviewEditorialOverrideV1 | None:
        path = self._path(trade_date_value)
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ReviewEditorialOverrideV1.model_validate(payload)

    def revision(self, trade_date_value: date | str) -> str | None:
        path = self._path(trade_date_value)
        if not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_review_editorial_override(
    presentation: PostMarketReviewPresentationV4,
    review: PostMarketReviewV1,
    override: ReviewEditorialOverrideV1 | None,
) -> PostMarketReviewPresentationV4:
    """Apply a matching researched article while preserving computed appendices."""

    if override is None:
        return presentation
    if override.review_id != review.review_id or override.trade_date != review.trade_date:
        raise ValueError("editorial override does not match the immutable review")
    payload = presentation.model_dump(mode="json")
    for key in (
        "title",
        "standfirst",
        "day_character",
        "core_conclusion",
        "sections",
        "watch_items",
        "themes",
        "money_making_effect",
        "loss_making_effect",
    ):
        payload[key] = getattr(override, key)
    return PostMarketReviewPresentationV4.model_validate(payload)


__all__ = [
    "ENV_EDITORIAL_DIR",
    "ReviewEditorialOverrideStore",
    "ReviewEditorialOverrideV1",
    "apply_review_editorial_override",
]
