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
from .contracts import (
    MarketPhase,
    MarketWatchSnapshotV1,
    SectorFlowSeriesV1,
    SectorFlowTrajectoryStatus,
)
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

    def list_dates(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return newest retained verified trading dates."""

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

    def get_daily_sector_flow_projection(
        self,
        trade_date: date | str,
        *,
        direction: str,
        sector_keys: tuple[str, ...],
    ) -> dict[str, Any] | None:
        """Return selected Collector-materialized close series when available."""


@dataclass(frozen=True, slots=True)
class AcceptedMarketWatchSnapshot:
    pointer: AcceptedSnapshotPointerV1
    snapshot: MarketWatchSnapshotV1
    source_payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class HistoricalMarketWatchSnapshot:
    trade_date: date
    minute_bucket: datetime
    snapshot_id: str
    source_snapshot_revision: str
    snapshot: MarketWatchSnapshotV1
    source_payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class HistoricalSectorFlowProjection:
    trade_date: date
    minute_bucket: datetime
    snapshot_id: str
    source_snapshot_revision: str
    trajectory_revision: str
    direction: str
    status: SectorFlowTrajectoryStatus
    as_of: datetime
    market_phase: MarketPhase
    sectors: tuple[SectorFlowSeriesV1, ...]
    flags: tuple[str, ...]
    reason: str | None


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
        self._daily_cached: dict[
            tuple[date, datetime, str, str],
            HistoricalMarketWatchSnapshot,
        ] = {}

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

    def read_recent_daily_snapshots(
        self,
        *,
        limit: int = 5,
        as_of: datetime | None = None,
    ) -> tuple[HistoricalMarketWatchSnapshot, ...]:
        """Read the newest real trajectory-bearing snapshot for each trade date.

        The current trade date is bounded by the collector's published pointer.
        Older dates are resolved from their final real history row.  No point is
        synthesized when a date or exact payload is absent.
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer between 1 and 20")
        observed = self._aware(as_of or self._clock(), name="as_of")
        view = self.read(as_of=observed)
        accepted = view.accepted
        current_trade_date = view.collector_envelope.collection_completeness.trade_date
        dates = self._history_reader.list_dates(limit=min(20, limit + 5))
        results: list[HistoricalMarketWatchSnapshot] = []
        for item in dates:
            raw_date = item.get("trade_date")
            try:
                trade_date = date.fromisoformat(str(raw_date))
            except (TypeError, ValueError):
                raise AcceptedSnapshotReadError("history trade_date is invalid") from None
            if trade_date > observed.date():
                continue
            if accepted is not None and accepted.pointer.trade_date == trade_date:
                results.append(
                    HistoricalMarketWatchSnapshot(
                        trade_date=trade_date,
                        minute_bucket=accepted.pointer.minute_bucket,
                        snapshot_id=accepted.pointer.snapshot_id,
                        source_snapshot_revision=(
                            accepted.pointer.source_snapshot_revision
                        ),
                        snapshot=accepted.snapshot,
                        source_payload=accepted.source_payload,
                    )
                )
            elif trade_date == current_trade_date:
                # The read facade intentionally hides an unpublished or stale
                # cross-day pointer once the current session has started.
                continue
            else:
                records = self._history_reader.get_collection_records(trade_date)
                eligible = [
                    record
                    for record in records
                    if record.get("record_kind")
                    in {"accepted_real", "stale_snapshot"}
                    and self._is_session_minute(record.get("minute_bucket"), trade_date)
                ]
                if not eligible:
                    continue
                results.append(self._load_historical_record(eligible[-1], trade_date))
            if len(results) >= limit:
                break
        return tuple(reversed(results))

    def read_recent_daily_sector_flow_projections(
        self,
        *,
        direction: str,
        sector_keys: tuple[str, ...],
        limit: int = 5,
        as_of: datetime | None = None,
    ) -> tuple[HistoricalSectorFlowProjection, ...] | None:
        """Read selected daily series without decoding full historical snapshots.

        ``None`` means the compact read model is unavailable or incomplete and
        instructs the caller to use the existing strict full-snapshot path.
        """

        if direction not in {"defense", "offense"}:
            raise ValueError("direction must be defense or offense")
        normalized_keys = tuple(sorted(set(sector_keys)))
        if not sector_keys or len(normalized_keys) != len(sector_keys):
            raise ValueError("sector_keys must be non-empty and unique")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer between 1 and 20")
        getter = getattr(self._history_reader, "get_daily_sector_flow_projection", None)
        if not callable(getter):
            return None

        observed = self._aware(as_of or self._clock(), name="as_of")
        envelope = self.read_collection_status(as_of=observed)
        accepted_pointer = envelope.latest_accepted_real
        current_trade_date = envelope.collection_completeness.trade_date
        dates = self._history_reader.list_dates(limit=min(20, limit + 5))
        results: list[HistoricalSectorFlowProjection] = []
        accepted_view: MarketWatchReadView | None = None
        for item in dates:
            try:
                trade_date = date.fromisoformat(str(item.get("trade_date")))
            except (TypeError, ValueError):
                raise AcceptedSnapshotReadError("history trade_date is invalid") from None
            if trade_date > observed.date():
                continue
            raw_projection = getter(
                trade_date,
                direction=direction,
                sector_keys=normalized_keys,
            )
            projection = (
                None
                if raw_projection is None
                else self._validate_daily_sector_flow_projection(
                    raw_projection,
                    trade_date=trade_date,
                    direction=direction,
                    sector_keys=normalized_keys,
                )
            )
            if accepted_pointer is not None and accepted_pointer.trade_date == trade_date:
                if (
                    projection is None
                    or projection.source_snapshot_revision
                    != accepted_pointer.source_snapshot_revision
                ):
                    accepted_view = accepted_view or self.read(as_of=observed)
                    accepted = accepted_view.accepted
                    if accepted is None:
                        return None
                    projection = self._sector_flow_projection_from_snapshot(
                        HistoricalMarketWatchSnapshot(
                            trade_date=trade_date,
                            minute_bucket=accepted.pointer.minute_bucket,
                            snapshot_id=accepted.pointer.snapshot_id,
                            source_snapshot_revision=(
                                accepted.pointer.source_snapshot_revision
                            ),
                            snapshot=accepted.snapshot,
                            source_payload=accepted.source_payload,
                        ),
                        direction=direction,
                        sector_keys=normalized_keys,
                    )
            elif trade_date == current_trade_date:
                continue
            elif projection is None:
                return None
            if projection is not None:
                results.append(projection)
            if len(results) >= limit:
                break
        return tuple(reversed(results))

    @staticmethod
    def _sector_flow_projection_from_snapshot(
        retained: HistoricalMarketWatchSnapshot,
        *,
        direction: str,
        sector_keys: tuple[str, ...],
    ) -> HistoricalSectorFlowProjection | None:
        trajectory = (
            retained.snapshot.sector_flow_trajectory
            if direction == "defense"
            else retained.snapshot.offense_sector_flow_trajectory
        )
        if trajectory is None:
            return None
        requested = set(sector_keys)
        return HistoricalSectorFlowProjection(
            trade_date=retained.trade_date,
            minute_bucket=retained.minute_bucket,
            snapshot_id=retained.snapshot_id,
            source_snapshot_revision=retained.source_snapshot_revision,
            trajectory_revision=stable_sha256(trajectory),
            direction=direction,
            status=trajectory.status,
            as_of=trajectory.as_of or retained.snapshot.as_of,
            market_phase=trajectory.market_phase,
            sectors=tuple(
                item for item in trajectory.sectors if item.sector_key in requested
            ),
            flags=trajectory.flags,
            reason=trajectory.reason,
        )

    @staticmethod
    def _validate_daily_sector_flow_projection(
        raw: Mapping[str, Any],
        *,
        trade_date: date,
        direction: str,
        sector_keys: tuple[str, ...],
    ) -> HistoricalSectorFlowProjection:
        if raw.get("contract") != "sector_flow_daily_projection.v1":
            raise AcceptedSnapshotReadError("daily sector-flow projection contract is invalid")
        if raw.get("schema_version") != 1 or raw.get("direction") != direction:
            raise AcceptedSnapshotReadError("daily sector-flow projection identity is invalid")
        if str(raw.get("trade_date")) != trade_date.isoformat():
            raise AcceptedSnapshotReadError("daily sector-flow projection date is invalid")
        snapshot_id = str(raw.get("snapshot_id") or "").strip()
        source_revision = str(raw.get("source_snapshot_revision") or "")
        trajectory_revision = str(raw.get("trajectory_revision") or "")
        if not snapshot_id or any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in (source_revision, trajectory_revision)
        ):
            raise AcceptedSnapshotReadError("daily sector-flow projection revision is invalid")
        minute_bucket = _minute(raw.get("minute_bucket"), name="projection minute_bucket")
        as_of = _minute(raw.get("as_of"), name="projection as_of")
        if minute_bucket.date() != trade_date or as_of.date() != trade_date:
            raise AcceptedSnapshotReadError("daily sector-flow projection time is invalid")
        sectors = tuple(
            SectorFlowSeriesV1.model_validate(item) for item in raw.get("sectors", ())
        )
        requested = set(sector_keys)
        keys = tuple(item.sector_key for item in sectors)
        if len(keys) != len(set(keys)) or any(key not in requested for key in keys):
            raise AcceptedSnapshotReadError("daily sector-flow projection selection is invalid")
        if any(
            point.provider_as_of.date() != trade_date
            for series in sectors
            for point in series.points
        ):
            raise AcceptedSnapshotReadError("daily sector-flow projection point date is invalid")
        try:
            status = SectorFlowTrajectoryStatus(str(raw.get("status")))
            market_phase = MarketPhase(str(raw.get("market_phase")))
        except ValueError:
            raise AcceptedSnapshotReadError(
                "daily sector-flow projection status is invalid"
            ) from None
        flags = tuple(str(item) for item in raw.get("flags", ()))
        reason = raw.get("reason")
        return HistoricalSectorFlowProjection(
            trade_date=trade_date,
            minute_bucket=minute_bucket,
            snapshot_id=snapshot_id,
            source_snapshot_revision=source_revision,
            trajectory_revision=trajectory_revision,
            direction=direction,
            status=status,
            as_of=as_of,
            market_phase=market_phase,
            sectors=sectors,
            flags=flags,
            reason=None if reason is None else str(reason),
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

    def _load_historical_record(
        self,
        record: Mapping[str, Any],
        trade_date: date,
    ) -> HistoricalMarketWatchSnapshot:
        minute_bucket = _minute(
            record.get("minute_bucket"),
            name="history minute_bucket",
        )
        snapshot_id = str(record.get("snapshot_id") or "").strip()
        source_revision = str(record.get("payload_digest") or "").strip().lower()
        if not snapshot_id or len(source_revision) != 64:
            raise AcceptedSnapshotReadError("historical snapshot pointer is incomplete")
        cache_key = (trade_date, minute_bucket, snapshot_id, source_revision)
        with self._lock:
            cached = self._daily_cached.get(cache_key)
            if cached is not None:
                return cached
        item = self._history_reader.get_snapshot_by_pointer(
            trade_date=trade_date,
            minute_bucket=minute_bucket,
            snapshot_id=snapshot_id,
            payload_digest=source_revision,
        )
        if item is None:
            raise AcceptedSnapshotReadError("historical snapshot payload is absent")
        payload = item.get("payload")
        if not isinstance(payload, Mapping) or stable_sha256(payload) != source_revision:
            raise AcceptedSnapshotReadError("historical snapshot revision does not match")
        snapshot = MarketWatchSnapshotV1.model_validate(payload)
        if (
            snapshot.snapshot_id != snapshot_id
            or _minute(snapshot.as_of, name="historical snapshot as_of") != minute_bucket
            or snapshot.market_state.trading_date != trade_date
        ):
            raise AcceptedSnapshotReadError("historical snapshot identity does not match")
        result = HistoricalMarketWatchSnapshot(
            trade_date=trade_date,
            minute_bucket=minute_bucket,
            snapshot_id=snapshot_id,
            source_snapshot_revision=source_revision,
            snapshot=snapshot,
            source_payload=_freeze_json(payload),
        )
        with self._lock:
            self._daily_cached[cache_key] = result
            while len(self._daily_cached) > 20:
                self._daily_cached.pop(next(iter(self._daily_cached)))
        return result

    @staticmethod
    def _is_session_minute(value: Any, trade_date: date) -> bool:
        try:
            minute = _minute(value, name="history minute_bucket")
        except (AcceptedSnapshotReadError, TypeError, ValueError):
            return False
        if minute.date() != trade_date:
            return False
        minute_of_day = minute.hour * 60 + minute.minute
        return (9 * 60 + 25 <= minute_of_day <= 11 * 60 + 30) or (
            13 * 60 <= minute_of_day <= 15 * 60
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
    "HistoricalMarketWatchSnapshot",
    "MarketWatchReadFacade",
    "MarketWatchReadView",
]
