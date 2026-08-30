"""CNInfo mapping for candidate-bound official announcements."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from tradex.data_gateway.review_announcement_contracts import (
    OfficialAnnouncementV1,
    ReviewAnnouncementCandidateManifestV1,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def map_cninfo_review_announcements(
    payload: Any,
    *,
    manifest: ReviewAnnouncementCandidateManifestV1,
    window_start: date,
    window_end: date,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise RuntimeError("CNInfo candidate payload must be a mapping")
    if payload.get("provider") != "cninfo":
        raise RuntimeError("unexpected official announcement provider")
    if date.fromisoformat(str(payload.get("start_date") or "")) != window_start:
        raise RuntimeError("CNInfo start date does not match the request")
    if date.fromisoformat(str(payload.get("end_date") or "")) != window_end:
        raise RuntimeError("CNInfo end date does not match the request")
    rows = payload.get("announcements")
    if not isinstance(rows, list):
        raise RuntimeError("CNInfo candidate announcements must be a list")

    names = {item.instrument_id: item.name for item in manifest.candidates}
    announcements: list[OfficialAnnouncementV1] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise RuntimeError("CNInfo candidate announcement row is invalid")
        instrument_id = str(row.get("instrument_id") or "")
        if instrument_id not in names:
            raise RuntimeError("CNInfo returned a non-candidate instrument")
        published_at = datetime.fromisoformat(str(row.get("published_at") or ""))
        if published_at.tzinfo is None or published_at.utcoffset() is None:
            raise RuntimeError("CNInfo published_at must include a timezone")
        announcements.append(
            OfficialAnnouncementV1(
                announcement_id=str(row.get("announcement_id") or ""),
                instrument_id=instrument_id,
                name=names[instrument_id],
                title=str(row.get("title") or ""),
                published_at=published_at.astimezone(SHANGHAI),
                publication_precision=str(row.get("publication_precision") or ""),
                announcement_type=(
                    str(row.get("announcement_type"))
                    if row.get("announcement_type") not in (None, "")
                    else None
                ),
                source_url=str(row.get("source_url") or ""),
                source="cninfo",
            )
        )
    announcements.sort(
        key=lambda item: (item.published_at, item.instrument_id, item.announcement_id),
        reverse=True,
    )
    return {
        "provider": "cninfo",
        "provider_as_of": (
            max(item.published_at for item in announcements)
            if announcements
            else None
        ),
        "trade_date": manifest.trade_date,
        "review_id": manifest.review_id,
        "candidate_manifest_revision": manifest.manifest_revision,
        "window_start": window_start,
        "window_end": window_end,
        "candidates": manifest.candidates,
        "announcements": tuple(announcements),
    }


__all__ = ["map_cninfo_review_announcements"]
