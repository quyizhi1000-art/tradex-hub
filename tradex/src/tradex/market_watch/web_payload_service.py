"""Serialized summary/detail payloads backed only by the read facade."""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from .collection_contracts import MarketWatchCollectorEnvelopeV1
from .contracts import ContractModel
from .integrity import REVISION_PATTERN, canonical_json_bytes, stable_sha256
from .read_facade import MarketWatchReadFacade, MarketWatchReadView
from .web_projection import (
    SectorSelectionError,
    TrajectoryRevisionMismatch,
    build_market_watch_summary,
    build_sector_flow_detail,
    overlay_sector_resonance,
)


JSON_CONTENT_TYPE = "application/json; charset=utf-8"


class MarketWatchUnavailableV1(ContractModel):
    contract: Literal["market_watch_unavailable.v1"] = "market_watch_unavailable.v1"
    schema_version: Literal[1] = 1
    availability: Literal["unavailable"] = "unavailable"
    reason: Literal["no_accepted_real"] = "no_accepted_real"
    collector_envelope: MarketWatchCollectorEnvelopeV1


class MarketWatchRevisionConflictV1(ContractModel):
    contract: Literal["market_watch_revision_conflict.v1"] = (
        "market_watch_revision_conflict.v1"
    )
    schema_version: Literal[1] = 1
    scope: Literal["source_snapshot", "trajectory"]
    expected_revision: str = Field(pattern=REVISION_PATTERN)
    current_revision: str | None = Field(default=None, pattern=REVISION_PATTERN)
    source_snapshot_revision: str | None = Field(
        default=None,
        pattern=REVISION_PATTERN,
    )
    action: Literal["discard_batch_and_retry"] = "discard_batch_and_retry"


class MarketWatchSelectionRejectedV1(ContractModel):
    contract: Literal["market_watch_selection_rejected.v1"] = (
        "market_watch_selection_rejected.v1"
    )
    schema_version: Literal[1] = 1
    reason: Literal["invalid_or_missing_sector_selection"] = (
        "invalid_or_missing_sector_selection"
    )
    source_snapshot_revision: str = Field(pattern=REVISION_PATTERN)
    direction: Literal["defense", "offense"]


class WebPayloadError(RuntimeError):
    status_code: int
    payload: ContractModel

    def __init__(self, payload: ContractModel, *, status_code: int) -> None:
        super().__init__(payload.contract)
        self.status_code = status_code
        self.payload = payload

    @property
    def body(self) -> bytes:
        return canonical_json_bytes(self.payload)


class WebAcceptedRealUnavailable(WebPayloadError):
    def __init__(self, envelope: MarketWatchCollectorEnvelopeV1) -> None:
        super().__init__(
            MarketWatchUnavailableV1(collector_envelope=envelope),
            status_code=503,
        )


class WebRevisionConflict(WebPayloadError):
    def __init__(
        self,
        *,
        scope: Literal["source_snapshot", "trajectory"],
        expected_revision: str,
        current_revision: str | None,
        source_snapshot_revision: str | None,
    ) -> None:
        super().__init__(
            MarketWatchRevisionConflictV1(
                scope=scope,
                expected_revision=expected_revision,
                current_revision=current_revision,
                source_snapshot_revision=source_snapshot_revision,
            ),
            status_code=409,
        )


class WebSectorSelectionRejected(WebPayloadError):
    def __init__(
        self,
        *,
        source_snapshot_revision: str,
        direction: Literal["defense", "offense"],
    ) -> None:
        super().__init__(
            MarketWatchSelectionRejectedV1(
                source_snapshot_revision=source_snapshot_revision,
                direction=direction,
            ),
            status_code=422,
        )


@dataclass(frozen=True, slots=True)
class SerializedWebPayload:
    status_code: Literal[200, 304]
    content_type: str
    body: bytes
    etag: str
    view_revision: str
    source_snapshot_revision: str | None
    trajectory_revision: str | None
    cache_hit: bool


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if if_none_match is None:
        return False
    for token in if_none_match.split(","):
        candidate = token.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        if candidate == etag:
            return True
    return False


def _serialized(
    payload: ContractModel,
    *,
    source_snapshot_revision: str | None,
    trajectory_revision: str | None = None,
) -> SerializedWebPayload:
    body = canonical_json_bytes(payload)
    view_revision = hashlib.sha256(body).hexdigest()
    return SerializedWebPayload(
        status_code=200,
        content_type=JSON_CONTENT_TYPE,
        body=body,
        etag=f'"{view_revision}"',
        view_revision=view_revision,
        source_snapshot_revision=source_snapshot_revision,
        trajectory_revision=trajectory_revision,
        cache_hit=False,
    )


def _response(
    cached: SerializedWebPayload,
    *,
    if_none_match: str | None,
    cache_hit: bool,
) -> SerializedWebPayload:
    if _etag_matches(if_none_match, cached.etag):
        return SerializedWebPayload(
            status_code=304,
            content_type=cached.content_type,
            body=b"",
            etag=cached.etag,
            view_revision=cached.view_revision,
            source_snapshot_revision=cached.source_snapshot_revision,
            trajectory_revision=cached.trajectory_revision,
            cache_hit=cache_hit,
        )
    return SerializedWebPayload(
        status_code=200,
        content_type=cached.content_type,
        body=cached.body,
        etag=cached.etag,
        view_revision=cached.view_revision,
        source_snapshot_revision=cached.source_snapshot_revision,
        trajectory_revision=cached.trajectory_revision,
        cache_hit=cache_hit,
    )


class MarketWatchWebPayloadService:
    """Build and cache immutable Web bytes for accepted snapshot revisions."""

    def __init__(
        self,
        facade: MarketWatchReadFacade,
        *,
        detail_cache_size: int = 64,
        resonance_reader=None,
    ) -> None:
        if (
            isinstance(detail_cache_size, bool)
            or not isinstance(detail_cache_size, int)
            or detail_cache_size < 1
        ):
            raise ValueError("detail_cache_size must be a positive integer")
        self._facade = facade
        if resonance_reader is not None and getattr(
            resonance_reader, "read_only", False
        ) is not True:
            raise ValueError("resonance_reader must be opened with read_only=True")
        self._resonance_reader = resonance_reader
        self._detail_cache_size = int(detail_cache_size)
        self._lock = threading.RLock()
        self._summary_key: tuple[str, str | None] | None = None
        self._summary: SerializedWebPayload | None = None
        self._details: OrderedDict[
            tuple[str, str, str, tuple[str, ...]],
            SerializedWebPayload,
        ] = OrderedDict()

    def get_collection_status(
        self,
        *,
        if_none_match: str | None = None,
    ) -> SerializedWebPayload:
        envelope = self._facade.read_collection_status()
        accepted = envelope.latest_accepted_real
        payload = _serialized(
            envelope,
            source_snapshot_revision=(
                None if accepted is None else accepted.source_snapshot_revision
            ),
        )
        return _response(payload, if_none_match=if_none_match, cache_hit=False)

    def get_summary(
        self,
        *,
        expected_source_snapshot_revision: str,
        if_none_match: str | None = None,
    ) -> SerializedWebPayload:
        view = self._facade.read()
        accepted = self._require_source_revision(
            view,
            expected_source_snapshot_revision=expected_source_snapshot_revision,
        )
        resonance = None
        if self._resonance_reader is not None:
            resonance = self._resonance_reader.get_by_source_revision(
                accepted.pointer.source_snapshot_revision
            )
            if resonance is None:
                resonance = self._resonance_reader.get_latest_before(
                    accepted.snapshot.as_of,
                    max_age_minutes=6,
                )
        summary_key = (
            accepted.pointer.source_snapshot_revision,
            None if resonance is None else resonance.resonance_revision,
        )
        with self._lock:
            cache_hit = (
                self._summary is not None
                and self._summary_key == summary_key
            )
            if not cache_hit:
                summary = build_market_watch_summary(
                    accepted.source_payload,
                    source_snapshot_revision=accepted.pointer.source_snapshot_revision,
                    collector_envelope=view.collector_envelope,
                )
                if resonance is not None:
                    summary = overlay_sector_resonance(summary, resonance)
                self._summary = _serialized(
                    summary,
                    source_snapshot_revision=accepted.pointer.source_snapshot_revision,
                )
                self._summary_key = summary_key
                self._details.clear()
            assert self._summary is not None
            return _response(
                self._summary,
                if_none_match=if_none_match,
                cache_hit=cache_hit,
            )

    def get_detail(
        self,
        *,
        direction: Literal["defense", "offense"],
        sector_keys: tuple[str, ...],
        expected_source_snapshot_revision: str,
        expected_trajectory_revision: str,
        if_none_match: str | None = None,
    ) -> SerializedWebPayload:
        view = self._facade.read()
        accepted = self._require_source_revision(
            view,
            expected_source_snapshot_revision=expected_source_snapshot_revision,
        )
        normalized_sector_keys = tuple(sorted(set(sector_keys)))
        if not sector_keys or len(normalized_sector_keys) != len(sector_keys):
            raise WebSectorSelectionRejected(
                source_snapshot_revision=accepted.pointer.source_snapshot_revision,
                direction=direction,
            )
        cache_key = (
            accepted.pointer.source_snapshot_revision,
            expected_trajectory_revision,
            direction,
            normalized_sector_keys,
        )
        with self._lock:
            cached = self._details.get(cache_key)
            cache_hit = cached is not None
            if cached is None:
                try:
                    detail = build_sector_flow_detail(
                        accepted.source_payload,
                        source_snapshot_revision=(
                            accepted.pointer.source_snapshot_revision
                        ),
                        collector_envelope=view.collector_envelope,
                        direction=direction,
                        sector_keys=normalized_sector_keys,
                        expected_trajectory_revision=expected_trajectory_revision,
                    )
                except TrajectoryRevisionMismatch as error:
                    current = self._trajectory_revision(view, direction=direction)
                    raise WebRevisionConflict(
                        scope="trajectory",
                        expected_revision=expected_trajectory_revision,
                        current_revision=current,
                        source_snapshot_revision=(
                            accepted.pointer.source_snapshot_revision
                        ),
                    ) from error
                except SectorSelectionError as error:
                    raise WebSectorSelectionRejected(
                        source_snapshot_revision=(
                            accepted.pointer.source_snapshot_revision
                        ),
                        direction=direction,
                    ) from error
                cached = _serialized(
                    detail,
                    source_snapshot_revision=accepted.pointer.source_snapshot_revision,
                    trajectory_revision=detail.trajectory_revision,
                )
                self._details[cache_key] = cached
                self._details.move_to_end(cache_key)
                while len(self._details) > self._detail_cache_size:
                    self._details.popitem(last=False)
            else:
                self._details.move_to_end(cache_key)
            return _response(
                cached,
                if_none_match=if_none_match,
                cache_hit=cache_hit,
            )

    @staticmethod
    def _require_source_revision(
        view: MarketWatchReadView,
        *,
        expected_source_snapshot_revision: str,
    ):
        accepted = view.accepted
        if accepted is None:
            raise WebAcceptedRealUnavailable(view.collector_envelope)
        current = accepted.pointer.source_snapshot_revision
        if expected_source_snapshot_revision != current:
            raise WebRevisionConflict(
                scope="source_snapshot",
                expected_revision=expected_source_snapshot_revision,
                current_revision=current,
                source_snapshot_revision=current,
            )
        return accepted

    @staticmethod
    def _trajectory_revision(
        view: MarketWatchReadView,
        *,
        direction: Literal["defense", "offense"],
    ) -> str | None:
        accepted = view.accepted
        if accepted is None:
            return None
        trajectory = (
            accepted.source_payload.get("sector_flow_trajectory")
            if direction == "defense"
            else accepted.source_payload.get("offense_sector_flow_trajectory")
        )
        return None if trajectory is None else stable_sha256(trajectory)


__all__ = [
    "JSON_CONTENT_TYPE",
    "MarketWatchRevisionConflictV1",
    "MarketWatchSelectionRejectedV1",
    "MarketWatchUnavailableV1",
    "MarketWatchWebPayloadService",
    "SerializedWebPayload",
    "WebAcceptedRealUnavailable",
    "WebPayloadError",
    "WebRevisionConflict",
    "WebSectorSelectionRejected",
]
