"""Zero-refresh read facade for accepted market-watch snapshots.

The facade reads the collector ledger first, proves its pointer against
lightweight history metadata, then loads exactly one history payload.  It has
no dependency on ``MarketWatchService`` or any provider refresh path.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from tradex.market_calendar import (
    CalendarDayStatus,
    TradingSessionPhase,
    a_share_session,
    calendar_day_status,
)

from .collection_contracts import (
    AcceptedSnapshotPointerV1,
    MarketWatchCollectorEnvelopeV1,
)
from .contracts import MarketWatchSnapshotV1
from .integrity import stable_sha256
from .session_schedule import OPENING_AUCTION_RESULT_TIME


SHANGHAI = ZoneInfo("Asia/Shanghai")


class AcceptedSnapshotReadError(RuntimeError):
    """An accepted ledger pointer cannot be proven against strict history."""


class CollectionEnvelopeReader(Protocol):
    read_only: bool

    def read_envelope(self, *, as_of: datetime) -> MarketWatchCollectorEnvelopeV1:
        """Return collector state without mutating the ledger."""


class ExactHistoryReader(Protocol):
    read_only: bool

    def get_collection_records(self, trade_date: date | str) -> list[dict[str, Any]]:
        """Return ordered history metadata without reading payload blobs."""

    def get_snapshot_by_pointer(
        self,
        *,
        trade_date: date | str,
        minute_bucket: datetime | str,
        snapshot_id: str,
        payload_digest: str,
    ) -> dict[str, Any] | None:
        """Return exactly one raw canonical payload for a proven pointer."""


@dataclass(frozen=True, slots=True)
class AcceptedMarketWatchSnapshot:
    pointer: AcceptedSnapshotPointerV1
    snapshot: MarketWatchSnapshotV1
    source_payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class MarketWatchReadView:
    collector_envelope: MarketWatchCollectorEnvelopeV1
    accepted: AcceptedMarketWatchSnapshot | None
    payload_cache_hit: bool


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _minute(value: datetime | str, *, name: str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AcceptedSnapshotReadError(f"{name} must include a timezone")
    return parsed.astimezone(SHANGHAI).replace(second=0, microsecond=0)


def _previous_trading_date(value: date) -> date | None:
    candidate = value - timedelta(days=1)
    while True:
        status = calendar_day_status(candidate)
        if status is CalendarDayStatus.VERIFIED_TRADING_DAY:
            return candidate
        if status is CalendarDayStatus.UNVERIFIED:
            return None
        candidate -= timedelta(days=1)


def _current_session_envelope(
    envelope: MarketWatchCollectorEnvelopeV1,
    *,
    as_of: datetime,
) -> MarketWatchCollectorEnvelopeV1:
    """Expose the previous verified close only until the next session opens.

    The persistent ledger deliberately keeps its latest accepted pointer across
    dates.  Before 09:25 on a verified trading day, that exact previous-trading-
    day snapshot remains useful and is safe to display with its original date.
    Once the market opens, the Web reader must wait for the current day's first
    accepted-real snapshot.  Non-trading days retain the latest close because
    their completeness contract has no expected minute buckets.
    """

    completeness = envelope.collection_completeness
    if completeness.expected_minute_buckets == 0:
        return envelope
    trade_date = completeness.trade_date
    updates: dict[str, Any] = {}
    accepted = envelope.latest_accepted_real
    session = a_share_session(as_of)
    previous_trading_date = _previous_trading_date(trade_date)
    may_show_previous_close = bool(
        accepted is not None
        and session.phase is TradingSessionPhase.PRE_OPEN
        and as_of.astimezone(SHANGHAI).time().replace(tzinfo=None)
        < OPENING_AUCTION_RESULT_TIME
        and session.trading_date == trade_date
        and previous_trading_date is not None
        and accepted.trade_date == previous_trading_date
    )
    if (
        accepted is not None
        and accepted.trade_date != trade_date
        and not may_show_previous_close
    ):
        updates["latest_accepted_real"] = None
    cursor = envelope.collection_cursor
    if cursor is not None and cursor.trade_date != trade_date:
        updates["collection_cursor"] = None
    published = envelope.latest_published_status
    if published is not None and published.trade_date != trade_date:
        updates["latest_published_status"] = None
    return envelope if not updates else envelope.model_copy(update=updates)


class MarketWatchReadFacade:
    """Read one latest accepted real snapshot with a bounded one-revision cache."""

    def __init__(
        self,
        *,
        collection_reader: CollectionEnvelopeReader,
        history_reader: ExactHistoryReader,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if getattr(collection_reader, "read_only", False) is not True:
            raise ValueError("collection_reader must be opened with read_only=True")
        if getattr(history_reader, "read_only", False) is not True:
            raise ValueError("history_reader must be opened with read_only=True")
        self._collection_reader = collection_reader
        self._history_reader = history_reader
        self._clock = clock or (lambda: datetime.now(SHANGHAI))
        self._lock = threading.RLock()
        self._cached: AcceptedMarketWatchSnapshot | None = None

    def read(self, *, as_of: datetime | None = None) -> MarketWatchReadView:
        envelope = self.read_collection_status(as_of=as_of)
        pointer = envelope.latest_accepted_real
        if pointer is None:
            return MarketWatchReadView(
                collector_envelope=envelope,
                accepted=None,
                payload_cache_hit=False,
            )

        with self._lock:
            if self._cache_matches(pointer):
                cached = self._cached
                assert cached is not None
                accepted = AcceptedMarketWatchSnapshot(
                    pointer=pointer,
                    snapshot=cached.snapshot,
                    source_payload=cached.source_payload,
                )
                self._cached = accepted
                return MarketWatchReadView(
                    collector_envelope=envelope,
                    accepted=accepted,
                    payload_cache_hit=True,
                )
            accepted = self._load(pointer)
            self._cached = accepted
            return MarketWatchReadView(
                collector_envelope=envelope,
                accepted=accepted,
                payload_cache_hit=False,
            )

    def read_collection_status(
        self,
        *,
        as_of: datetime | None = None,
    ) -> MarketWatchCollectorEnvelopeV1:
        """Read only the small ledger envelope without touching history payloads."""

        observed = self._aware(as_of or self._clock(), name="as_of")
        return _current_session_envelope(
            MarketWatchCollectorEnvelopeV1.model_validate(
                self._collection_reader.read_envelope(as_of=observed)
            ),
            as_of=observed,
        )

    def _cache_matches(self, pointer: AcceptedSnapshotPointerV1) -> bool:
        cached = self._cached
        return bool(
            cached is not None
            and cached.pointer.snapshot_id == pointer.snapshot_id
            and cached.pointer.source_snapshot_revision
            == pointer.source_snapshot_revision
            and cached.pointer.minute_bucket == pointer.minute_bucket
        )

    def _load(
        self,
        pointer: AcceptedSnapshotPointerV1,
    ) -> AcceptedMarketWatchSnapshot:
        records = self._history_reader.get_collection_records(pointer.trade_date)
        matching = [
            item
            for item in records
            if _minute(item.get("minute_bucket"), name="history minute_bucket")
            == pointer.minute_bucket
        ]
        if len(matching) != 1:
            raise AcceptedSnapshotReadError(
                "latest_accepted_real must identify exactly one history metadata row"
            )
        metadata = matching[0]
        if (
            metadata.get("snapshot_id") != pointer.snapshot_id
            or metadata.get("payload_digest") != pointer.source_snapshot_revision
            # The ledger is the publication authority.  A collector-accepted
            # snapshot can honestly have overall stale freshness when one
            # component degrades, even though its exact canonical payload was
            # captured and published for this minute.  Keep accepting only
            # real history rows with the exact ledger identity; derived gap
            # heartbeats and unknown row kinds must still fail closed.
            or metadata.get("record_kind")
            not in {"accepted_real", "stale_snapshot"}
        ):
            raise AcceptedSnapshotReadError(
                "latest_accepted_real does not match accepted history metadata"
            )

        item = self._history_reader.get_snapshot_by_pointer(
            trade_date=pointer.trade_date,
            minute_bucket=pointer.minute_bucket,
            snapshot_id=pointer.snapshot_id,
            payload_digest=pointer.source_snapshot_revision,
        )
        if item is None:
            raise AcceptedSnapshotReadError(
                "latest_accepted_real payload is absent or its pointer changed"
            )
        if (
            str(item.get("trade_date")) != pointer.trade_date.isoformat()
            or _minute(item.get("minute_bucket"), name="payload minute_bucket")
            != pointer.minute_bucket
            or item.get("payload_digest") != pointer.source_snapshot_revision
        ):
            raise AcceptedSnapshotReadError(
                "exact history item does not match latest_accepted_real"
            )
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            raise AcceptedSnapshotReadError("exact history item has no payload mapping")
        if stable_sha256(payload) != pointer.source_snapshot_revision:
            raise AcceptedSnapshotReadError(
                "exact history payload does not match its source revision"
            )
        snapshot = MarketWatchSnapshotV1.model_validate(payload)
        if snapshot.snapshot_id != pointer.snapshot_id:
            raise AcceptedSnapshotReadError(
                "exact history payload has the wrong snapshot id"
            )
        if _minute(snapshot.as_of, name="snapshot as_of") != pointer.minute_bucket:
            raise AcceptedSnapshotReadError(
                "exact history payload has the wrong minute bucket"
            )
        frozen_payload = _freeze_json(payload)
        return AcceptedMarketWatchSnapshot(
            pointer=pointer,
            snapshot=snapshot,
            source_payload=frozen_payload,
        )

    @staticmethod
    def _aware(value: datetime, *, name: str) -> datetime:
        if not isinstance(value, datetime):
            raise TypeError(f"{name} must be a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must include a timezone")
        return value.astimezone(SHANGHAI)


__all__ = [
    "AcceptedMarketWatchSnapshot",
    "AcceptedSnapshotReadError",
    "CollectionEnvelopeReader",
    "ExactHistoryReader",
    "MarketWatchReadFacade",
    "MarketWatchReadView",
]
