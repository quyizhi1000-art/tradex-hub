"""HTTP semantics for market-watch payloads without an HTTP-server dependency."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from .contracts import ContractModel
from .integrity import REVISION_PATTERN, canonical_json_bytes
from .web_payload_service import (
    JSON_CONTENT_TYPE,
    MarketWatchWebPayloadService,
    SerializedWebPayload,
    WebPayloadError,
)


_REVISION_RE = re.compile(REVISION_PATTERN)
_SECTOR_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class MarketWatchRequestInvalidV1(ContractModel):
    contract: Literal["market_watch_request_invalid.v1"] = (
        "market_watch_request_invalid.v1"
    )
    schema_version: Literal[1] = 1
    field: str = Field(min_length=1)
    reason: Literal["missing_or_invalid"] = "missing_or_invalid"


@dataclass(frozen=True, slots=True)
class MarketWatchHttpResponse:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class MarketWatchWebApi:
    """Validate request parameters and map payload errors to exact HTTP status."""

    def __init__(self, payload_service: MarketWatchWebPayloadService) -> None:
        self._payload_service = payload_service

    def get_collection_status(
        self,
        *,
        if_none_match: str | None = None,
    ) -> MarketWatchHttpResponse:
        return self._success(
            self._payload_service.get_collection_status(
                if_none_match=if_none_match,
            )
        )

    def get_summary(
        self,
        *,
        source_snapshot_revision: str | None,
        if_none_match: str | None = None,
    ) -> MarketWatchHttpResponse:
        invalid = self._validate_revision(
            source_snapshot_revision,
            field="source_snapshot_revision",
        )
        if invalid is not None:
            return invalid
        assert source_snapshot_revision is not None
        try:
            payload = self._payload_service.get_summary(
                expected_source_snapshot_revision=source_snapshot_revision,
                if_none_match=if_none_match,
            )
        except WebPayloadError as error:
            return self._error(error)
        return self._success(payload)

    def get_detail(
        self,
        *,
        direction: str | None,
        sector_keys: tuple[str, ...],
        source_snapshot_revision: str | None,
        trajectory_revision: str | None,
        if_none_match: str | None = None,
    ) -> MarketWatchHttpResponse:
        if direction not in {"defense", "offense"}:
            return self._invalid("direction")
        for value, field in (
            (source_snapshot_revision, "source_snapshot_revision"),
            (trajectory_revision, "trajectory_revision"),
        ):
            invalid = self._validate_revision(value, field=field)
            if invalid is not None:
                return invalid
        if (
            not sector_keys
            or len(sector_keys) > 48
            or len(sector_keys) != len(set(sector_keys))
            or any(_SECTOR_KEY_RE.fullmatch(item) is None for item in sector_keys)
        ):
            return self._invalid("sector_keys")
        assert source_snapshot_revision is not None
        assert trajectory_revision is not None
        try:
            payload = self._payload_service.get_detail(
                direction=direction,
                sector_keys=sector_keys,
                expected_source_snapshot_revision=source_snapshot_revision,
                expected_trajectory_revision=trajectory_revision,
                if_none_match=if_none_match,
            )
        except WebPayloadError as error:
            return self._error(error)
        return self._success(payload)

    @staticmethod
    def _success(payload: SerializedWebPayload) -> MarketWatchHttpResponse:
        headers = [
            ("Content-Type", payload.content_type),
            ("Cache-Control", "private, no-cache"),
            ("ETag", payload.etag),
            ("X-Market-Watch-View-Revision", payload.view_revision),
        ]
        if payload.source_snapshot_revision is not None:
            headers.append(
                ("X-Source-Snapshot-Revision", payload.source_snapshot_revision)
            )
        if payload.trajectory_revision is not None:
            headers.append(("X-Trajectory-Revision", payload.trajectory_revision))
        return MarketWatchHttpResponse(
            status_code=payload.status_code,
            headers=tuple(headers),
            body=payload.body,
        )

    @staticmethod
    def _error(error: WebPayloadError) -> MarketWatchHttpResponse:
        headers = [
            ("Content-Type", JSON_CONTENT_TYPE),
            ("Cache-Control", "no-store"),
        ]
        if error.status_code == 503:
            headers.append(("Retry-After", "5"))
        return MarketWatchHttpResponse(
            status_code=error.status_code,
            headers=tuple(headers),
            body=error.body,
        )

    @classmethod
    def _validate_revision(
        cls,
        value: str | None,
        *,
        field: str,
    ) -> MarketWatchHttpResponse | None:
        if value is None or _REVISION_RE.fullmatch(value) is None:
            return cls._invalid(field)
        return None

    @staticmethod
    def _invalid(field: str) -> MarketWatchHttpResponse:
        body = canonical_json_bytes(MarketWatchRequestInvalidV1(field=field))
        return MarketWatchHttpResponse(
            status_code=400,
            headers=(
                ("Content-Type", JSON_CONTENT_TYPE),
                ("Cache-Control", "no-store"),
            ),
            body=body,
        )


__all__ = [
    "MarketWatchHttpResponse",
    "MarketWatchRequestInvalidV1",
    "MarketWatchWebApi",
]
