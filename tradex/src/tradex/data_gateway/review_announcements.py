"""Provider-neutral gateway for review candidate official announcements."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractMetadata, QualityStatus
from .providers.review_announcements import map_cninfo_review_announcements
from .review_announcement_contracts import (
    ReviewAnnouncementCandidateManifestV1,
    ReviewOfficialAnnouncementArchiveV1,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _revision(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: (
            item.model_dump(mode="json")
            if hasattr(item, "model_dump")
            else item.isoformat()
        ),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _router(router: Any | None) -> Any:
    if router is not None:
        return router
    from tradex.data_sources import get_router, register_all_sources

    register_all_sources()
    return get_router()


def fetch_review_official_announcements(
    manifest: ReviewAnnouncementCandidateManifestV1,
    *,
    window_end: date | str,
    router: Any | None = None,
    now: datetime | None = None,
) -> ReviewOfficialAnnouncementArchiveV1:
    canonical_manifest = ReviewAnnouncementCandidateManifestV1.model_validate(manifest)
    end = (
        window_end
        if isinstance(window_end, date) and not isinstance(window_end, datetime)
        else date.fromisoformat(str(window_end))
    )
    if end < canonical_manifest.trade_date or (end - canonical_manifest.trade_date).days > 7:
        raise ValueError("official announcement window must be between 0 and 7 days")
    fetched_at = now or datetime.now(SHANGHAI)
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("gateway now must include a timezone")
    fetched_at = fetched_at.astimezone(SHANGHAI)

    def validate(payload: Any, route_provider: str) -> ReviewOfficialAnnouncementArchiveV1:
        mapped = map_cninfo_review_announcements(
            payload,
            manifest=canonical_manifest,
            window_start=canonical_manifest.trade_date,
            window_end=end,
        )
        provider = mapped.pop("provider")
        provider_as_of = mapped.pop("provider_as_of")
        source_revision = _revision(mapped)
        return ReviewOfficialAnnouncementArchiveV1(
            metadata=ContractMetadata(
                contract="review_official_announcements.v1",
                provider=provider or route_provider,
                provider_as_of=provider_as_of,
                fetched_at=fetched_at,
                quality=QualityStatus.ACCEPTED,
            ),
            source_revision=source_revision,
            **mapped,
        )

    result, _provider = _router(router).route_validated(
        "review_candidate_announcements",
        validate,
        candidates=[item.model_dump(mode="json") for item in canonical_manifest.candidates],
        start_date=canonical_manifest.trade_date.isoformat(),
        end_date=end.isoformat(),
        page_size=50,
        max_pages_per_candidate=2,
    )
    return result


__all__ = ["fetch_review_official_announcements"]
