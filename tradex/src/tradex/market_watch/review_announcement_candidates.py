"""Derive a bounded official-announcement watchlist from review artifacts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from tradex.data_gateway.review_announcement_contracts import (
    ReviewAnnouncementCandidateManifestV1,
    ReviewAnnouncementCandidateV1,
)

from .integrity import stable_sha256


class NoReviewAnnouncementCandidates(LookupError):
    """The review is valid but has no stock-level watch candidates."""


def manifest_from_review_artifact(
    artifact: Mapping[str, Any],
) -> ReviewAnnouncementCandidateManifestV1:
    if artifact.get("contract") != "tradex_analysis_artifact.v1":
        raise ValueError("candidate source must be an analysis artifact")
    source_revision = str(artifact.get("source_revision") or "")
    payload = artifact.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("review artifact payload is missing")
    review = payload.get("review")
    presentation = payload.get("presentation")
    if not isinstance(review, Mapping) or not isinstance(presentation, Mapping):
        raise ValueError("review artifact is not materialized")
    trade_date = date.fromisoformat(str(review.get("trade_date") or ""))
    review_id = str(review.get("review_id") or "").strip()
    generated_at = datetime.fromisoformat(str(review.get("generated_at") or ""))
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("review generated_at must include a timezone")

    candidates: list[ReviewAnnouncementCandidateV1] = []
    seen: set[str] = set()
    watch_items = presentation.get("watch_items")
    if not isinstance(watch_items, list):
        raise ValueError("review watch items are missing")
    for watch_item in watch_items:
        if not isinstance(watch_item, Mapping):
            continue
        rank = int(watch_item.get("rank") or 0)
        title = str(watch_item.get("title") or "").strip()
        stocks = watch_item.get("stocks")
        if rank < 1 or not title or not isinstance(stocks, list):
            continue
        for stock in stocks:
            if not isinstance(stock, Mapping):
                continue
            instrument_id = str(stock.get("instrument_id") or "").strip()
            name = str(stock.get("name") or "").strip()
            if not instrument_id or not name or instrument_id in seen:
                continue
            candidates.append(
                ReviewAnnouncementCandidateV1(
                    instrument_id=instrument_id,
                    name=name,
                    watch_item_rank=rank,
                    watch_item_title=title,
                )
            )
            seen.add(instrument_id)
            if len(candidates) >= 12:
                break
        if len(candidates) >= 12:
            break
    if not candidates:
        raise NoReviewAnnouncementCandidates(
            "review artifact has no announcement candidates"
        )
    manifest_payload = {
        "trade_date": trade_date,
        "review_id": review_id,
        "review_generated_at": generated_at,
        "source_artifact_revision": source_revision,
        "candidates": candidates,
    }
    return ReviewAnnouncementCandidateManifestV1(
        **manifest_payload,
        manifest_revision=stable_sha256(manifest_payload),
    )


__all__ = [
    "NoReviewAnnouncementCandidates",
    "manifest_from_review_artifact",
]
