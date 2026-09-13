"""Provider-neutral intraday sector/stock minute resonance.

The accepted market-watch snapshot owns the sector flow trajectory.  This
feature reads that immutable trajectory, obtains canonical current-session
stock minutes through the gateway, and emits a separately revisioned result.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from threading import Lock
from typing import Literal

from pydantic import Field, model_validator

from tradex.data_gateway.contracts import (
    BoardLeaderSnapshotV2,
    BoardLeaderV2,
    IntradayMinuteSeriesV1,
)
from tradex.data_gateway.intraday import MAX_INTRADAY_BATCH_INSTRUMENTS

from .contracts import (
    ContractModel,
    MarketWatchSnapshotV1,
    SectorFlowLeaderSnapshotV1,
    SectorFlowLeaderV1,
    SectorFlowSeriesV1,
)
from .integrity import REVISION_PATTERN, stable_sha256


MIN_STOCK_RETURN_5M_PCT = 0.10
MIN_RESONANCE_CORRELATION = 0.60
MIN_DIRECTIONAL_AGREEMENT_RATIO = 0.60
MAX_SHARED_INTERVAL_MINUTES = 2
MAX_TARGET_LAG_MINUTES = 2
MIN_MATCHED_INTERVALS = 3


class SectorResonanceEntryV1(ContractModel):
    direction: Literal["defense", "offense"]
    sector_key: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    sector_name: str = Field(min_length=1)
    leader_snapshot: SectorFlowLeaderSnapshotV1


class SectorResonanceBatchV1(ContractModel):
    contract: Literal["sector_resonance_batch.v1"] = "sector_resonance_batch.v1"
    schema_version: Literal[1] = 1
    source_snapshot_revision: str = Field(pattern=REVISION_PATTERN)
    source_snapshot_id: str = Field(min_length=1)
    source_as_of: datetime
    generated_at: datetime
    entries: tuple[SectorResonanceEntryV1, ...] = Field(max_length=16)
    resonance_revision: str = Field(pattern=REVISION_PATTERN)

    @model_validator(mode="after")
    def validate_batch(self) -> "SectorResonanceBatchV1":
        keys = [(item.direction, item.sector_key) for item in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("sector resonance entries must be unique")
        if self.source_as_of.tzinfo is None or self.generated_at.tzinfo is None:
            raise ValueError("sector resonance timestamps must include a timezone")
        expected = stable_sha256(
            self.model_dump(mode="json", exclude={"resonance_revision"})
        )
        if self.resonance_revision != expected:
            raise ValueError("resonance_revision does not match the batch payload")
        return self

    @classmethod
    def create(cls, **values) -> "SectorResonanceBatchV1":
        revision = stable_sha256(
            {
                "contract": "sector_resonance_batch.v1",
                "schema_version": 1,
                **values,
            }
        )
        return cls(**values, resonance_revision=revision)


def _minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < MIN_MATCHED_INTERVALS:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    left_centered = [item - left_mean for item in left]
    right_centered = [item - right_mean for item in right]
    numerator = sum(
        x_value * y_value
        for x_value, y_value in zip(left_centered, right_centered, strict=True)
    )
    left_scale = math.sqrt(sum(item * item for item in left_centered))
    right_scale = math.sqrt(sum(item * item for item in right_centered))
    if left_scale <= 1e-12 or right_scale <= 1e-12:
        return None
    return max(-1.0, min(1.0, numerator / (left_scale * right_scale)))


def _sector_resonance_direction(
    sector: SectorFlowSeriesV1,
) -> Literal["up", "down"] | None:
    if sector.latest is None:
        return None
    flow_delta = sector.latest.delta_5m_cny
    if flow_delta is None or flow_delta <= 0:
        return None
    return "up"


def select_sector_resonance_candidates(
    sectors: tuple[SectorFlowSeriesV1, ...],
    *,
    limit: int,
) -> tuple[SectorFlowSeriesV1, ...]:
    """Match the Dashboard's current-fund-amount ordering before bounding calls."""

    available = [
        item
        for item in sectors
        if item.latest is not None
        and item.latest.delta_5m_cny is not None
        and item.latest.delta_5m_cny > 0
    ]
    available.sort(
        key=lambda item: (
            item.latest.cumulative_cny is None,
            -(item.latest.cumulative_cny or 0.0),
            item.name,
        )
    )
    return tuple(available[:limit])


def _build_sector_resonance_entries(
    jobs: tuple[
        tuple[Literal["defense", "offense"], SectorFlowSeriesV1], ...
    ],
    *,
    board_fetcher: Callable[..., BoardLeaderSnapshotV2],
    minute_fetcher: Callable[..., IntradayMinuteSeriesV1],
    candidate_limit: int,
    fetched_at: datetime,
    max_workers: int,
) -> tuple[SectorResonanceEntryV1, ...]:
    """Evaluate independent sectors concurrently while preserving rank order."""

    if not 1 <= max_workers <= 8:
        raise ValueError("max_workers must be between 1 and 8")
    minute_fetch_lock = Lock()

    def load_minutes(*args, **kwargs):
        # Custom single-symbol fetchers may reject concurrent bursts. Keep board
        # acquisition parallel while serializing only those compatibility calls.
        with minute_fetch_lock:
            return minute_fetcher(*args, **kwargs)

    def build(job):
        direction, sector = job
        return SectorResonanceEntryV1(
            direction=direction,
            sector_key=sector.sector_key,
            sector_name=sector.name,
            leader_snapshot=build_sector_resonance_snapshot(
                sector,
                board_fetcher=board_fetcher,
                minute_fetcher=load_minutes,
                candidate_limit=candidate_limit,
                fetched_at=fetched_at,
            ),
        )

    if max_workers == 1 or len(jobs) <= 1:
        return tuple(build(job) for job in jobs)
    with ThreadPoolExecutor(
        max_workers=min(max_workers, len(jobs)),
        thread_name_prefix="sector-resonance",
    ) as executor:
        return tuple(executor.map(build, jobs))


def _build_sector_resonance_entries_batched(
    jobs: tuple[
        tuple[Literal["defense", "offense"], SectorFlowSeriesV1], ...
    ],
    *,
    board_fetcher: Callable[..., BoardLeaderSnapshotV2],
    minute_batch_fetcher: Callable[..., Mapping[str, IntradayMinuteSeriesV1]],
    candidate_limit: int,
    fetched_at: datetime,
    max_workers: int,
) -> tuple[SectorResonanceEntryV1, ...]:
    """Deduplicate candidates and fetch all curves in bounded batches."""

    if not 1 <= max_workers <= 8:
        raise ValueError("max_workers must be between 1 and 8")

    def fetch_candidates(job):
        _direction, sector = job
        if not sector.leader_board_code:
            return None
        try:
            return board_fetcher(
                sector.leader_board_code,
                limit=candidate_limit,
                speed_order="desc",
                now=fetched_at,
            )
        except Exception:
            return None

    if max_workers == 1 or len(jobs) <= 1:
        candidate_snapshots = tuple(fetch_candidates(job) for job in jobs)
    else:
        with ThreadPoolExecutor(
            max_workers=min(max_workers, len(jobs)),
            thread_name_prefix="sector-resonance-board",
        ) as executor:
            candidate_snapshots = tuple(executor.map(fetch_candidates, jobs))

    instruments = tuple(sorted({
        candidate.instrument_id
        for snapshot in candidate_snapshots
        if snapshot is not None
        for candidate in snapshot.leaders
    }))
    minute_series: dict[str, IntradayMinuteSeriesV1] = {}
    for offset in range(0, len(instruments), MAX_INTRADAY_BATCH_INSTRUMENTS):
        chunk = instruments[offset:offset + MAX_INTRADAY_BATCH_INSTRUMENTS]
        try:
            minute_series.update(minute_batch_fetcher(chunk, now=fetched_at))
        except Exception:
            # Missing curves stay unavailable without discarding another batch.
            continue

    entries: list[SectorResonanceEntryV1] = []
    for (direction, sector), candidates in zip(
        jobs,
        candidate_snapshots,
        strict=True,
    ):
        def preloaded_board(*_args, **_kwargs):
            if candidates is None:
                raise RuntimeError("board candidates unavailable")
            return candidates

        def preloaded_minutes(symbol, **_kwargs):
            return minute_series[symbol]

        entries.append(SectorResonanceEntryV1(
            direction=direction,
            sector_key=sector.sector_key,
            sector_name=sector.name,
            leader_snapshot=build_sector_resonance_snapshot(
                sector,
                board_fetcher=preloaded_board,
                minute_fetcher=preloaded_minutes,
                candidate_limit=candidate_limit,
                fetched_at=fetched_at,
            ),
        ))
    return tuple(entries)


def _evaluate_minute_resonance_with_coverage(
    sector: SectorFlowSeriesV1,
    candidate: BoardLeaderV2,
    stock_series: IntradayMinuteSeriesV1,
) -> tuple[SectorFlowLeaderV1 | None, bool]:
    """Return one high-resonance leader or fail closed.

    Both curves are reduced to the same exact timestamps.  When the stock
    minute provider trails the sector trajectory, use only the newest complete
    shared five-minute window within two minutes of the sector's latest point.
    A missing minute is tolerated only when both curves therefore use the same
    interval, and no shared interval may exceed two minutes.
    """

    if sector.latest is None or not sector.points:
        return None, False
    direction = _sector_resonance_direction(sector)
    if direction is None:
        return None, False
    latest_target = _minute(sector.latest.provider_as_of)
    if stock_series.trading_date != latest_target.date():
        return None, False

    sector_points = {
        _minute(point.provider_as_of): point
        for point in sector.points
        if _minute(point.provider_as_of).date() == latest_target.date()
        and _minute(point.provider_as_of) <= latest_target
    }
    stock_by_minute = {
        datetime.combine(
            latest_target.date(),
            point.minute,
            tzinfo=latest_target.tzinfo,
        ): point.price
        for point in stock_series.points
    }

    aligned = None
    for lag_minutes in range(MAX_TARGET_LAG_MINUTES + 1):
        target = latest_target - timedelta(minutes=lag_minutes)
        target_point = sector_points.get(target)
        if target_point is None:
            continue
        if lag_minutes == 0:
            baseline_as_of = sector.latest.delta_5m_baseline_as_of
            declared_flow_delta = sector.latest.delta_5m_cny
        else:
            baseline_as_of = target_point.delta_5m_baseline_as_of
            declared_flow_delta = target_point.delta_5m_cny
        if baseline_as_of is None or declared_flow_delta is None:
            continue
        if declared_flow_delta == 0 or (
            (declared_flow_delta > 0) != (direction == "up")
        ):
            continue
        baseline = _minute(baseline_as_of)
        if baseline >= target:
            continue
        sector_by_minute = {
            observed: point.cumulative_cny
            for observed, point in sector_points.items()
            if baseline <= observed <= target
        }
        common = sorted(set(sector_by_minute).intersection(stock_by_minute))
        if (
            len(common) - 1 < MIN_MATCHED_INTERVALS
            or not common
            or common[0] != baseline
            or common[-1] != target
        ):
            continue
        if any(
            (later - earlier).total_seconds() / 60 > MAX_SHARED_INTERVAL_MINUTES
            for earlier, later in zip(common, common[1:])
        ):
            continue
        aligned = (target, baseline, sector_by_minute, common)
        break
    if aligned is None:
        return None, False
    target, baseline, sector_by_minute, common = aligned

    sector_increments = [
        sector_by_minute[later] - sector_by_minute[earlier]
        for earlier, later in zip(common, common[1:])
    ]
    stock_returns = [
        (stock_by_minute[later] / stock_by_minute[earlier] - 1.0) * 100.0
        for earlier, later in zip(common, common[1:])
    ]
    sector_flow_5m = sector_by_minute[target] - sector_by_minute[baseline]
    stock_return_5m = (
        stock_by_minute[target] / stock_by_minute[baseline] - 1.0
    ) * 100.0
    sector_path = [
        sector_by_minute[observed] - sector_by_minute[baseline]
        for observed in common[1:]
    ]
    stock_path = [
        (stock_by_minute[observed] / stock_by_minute[baseline] - 1.0) * 100.0
        for observed in common[1:]
    ]
    correlation = _pearson(sector_path, stock_path)
    directional_agreement = sum(
        1
        for flow_increment, stock_return in zip(
            sector_increments,
            stock_returns,
            strict=True,
        )
        if flow_increment * stock_return > 0
    ) / len(sector_increments)
    if (
        sector_flow_5m * stock_return_5m <= 0
        or abs(stock_return_5m) < MIN_STOCK_RETURN_5M_PCT
        or correlation is None
        or correlation < MIN_RESONANCE_CORRELATION
        or directional_agreement < MIN_DIRECTIONAL_AGREEMENT_RATIO
    ):
        return None, True
    return SectorFlowLeaderV1(
        instrument_id=candidate.instrument_id,
        name=candidate.name,
        change_pct=candidate.change_pct,
        speed_pct=stock_return_5m,
        resonance_correlation=correlation,
        directional_agreement_ratio=directional_agreement,
        matched_interval_count=len(common) - 1,
        resonance_strength="high",
        price=stock_by_minute[target],
        provider_as_of=target,
    ), True


def evaluate_minute_resonance(
    sector: SectorFlowSeriesV1,
    candidate: BoardLeaderV2,
    stock_series: IntradayMinuteSeriesV1,
) -> SectorFlowLeaderV1 | None:
    leader, _ = _evaluate_minute_resonance_with_coverage(
        sector,
        candidate,
        stock_series,
    )
    return leader


def _unavailable(
    label: str,
    *,
    direction: Literal["up", "down"] | None = None,
) -> SectorFlowLeaderSnapshotV1:
    return SectorFlowLeaderSnapshotV1(
        status="unavailable",
        status_label=label,
        selection_method="sector_fund_flow_path_resonance.v2",
        marginal_window_minutes=5,
        resonance_direction=direction,
    )


def build_sector_resonance_snapshot(
    sector: SectorFlowSeriesV1,
    *,
    board_fetcher: Callable[..., BoardLeaderSnapshotV2],
    minute_fetcher: Callable[..., IntradayMinuteSeriesV1],
    candidate_limit: int = 3,
    fetched_at: datetime | None = None,
) -> SectorFlowLeaderSnapshotV1:
    direction = _sector_resonance_direction(sector)
    if sector.latest is None:
        return _unavailable("板块5分钟证据不足")
    if direction is None:
        flow_delta = sector.latest.delta_5m_cny
        if flow_delta is None:
            return _unavailable("板块5分钟资金证据不足")
        return SectorFlowLeaderSnapshotV1(
            status="no_match",
            status_label="板块近5分钟未边际流入",
            selection_method="sector_fund_flow_path_resonance.v2",
            marginal_window_minutes=5,
            provider_as_of=sector.latest.provider_as_of,
        )
    if not sector.leader_board_code:
        return _unavailable("板块或成分股映射不可用")
    try:
        observed = fetched_at or datetime.now(sector.latest.provider_as_of.tzinfo)
        candidates = board_fetcher(
            sector.leader_board_code,
            limit=candidate_limit,
            speed_order="desc",
            now=observed,
        )
    except Exception:
        return _unavailable("成分股候选暂不可用", direction=direction)

    leaders: list[SectorFlowLeaderV1] = []
    minute_providers: set[str] = set()
    evaluated = 0
    for candidate in candidates.leaders:
        try:
            series = minute_fetcher(
                candidate.instrument_id,
                now=observed,
            )
        except Exception:
            continue
        minute_providers.add(series.metadata.provider)
        leader, has_aligned_coverage = _evaluate_minute_resonance_with_coverage(
            sector,
            candidate,
            series,
        )
        if has_aligned_coverage:
            evaluated += 1
        if leader is not None:
            leaders.append(leader)

    leaders.sort(
        key=lambda item: (
            -(item.resonance_correlation or -1.0),
            -(item.directional_agreement_ratio or 0.0),
            -abs(item.speed_pct or 0.0),
            item.instrument_id,
        )
    )
    source = "+".join(sorted(minute_providers)) or None
    if not leaders:
        return SectorFlowLeaderSnapshotV1(
            status="no_match" if evaluated else "unavailable",
            status_label=(
                "暂无上行共振快涨股"
                if evaluated
                else "分钟证据不足"
            ),
            selection_method="sector_fund_flow_path_resonance.v2",
            marginal_window_minutes=5,
            resonance_direction=direction,
            source=source,
            provider_as_of=sector.latest.provider_as_of,
        )
    return SectorFlowLeaderSnapshotV1(
        status="full",
        status_label="上行高共振快涨",
        selection_method="sector_fund_flow_path_resonance.v2",
        marginal_window_minutes=5,
        resonance_direction=direction,
        source=source,
        provider_as_of=leaders[0].provider_as_of,
        leaders=tuple(leaders[:1]),
    )


def build_sector_resonance_batch(
    snapshot: MarketWatchSnapshotV1 | Mapping,
    *,
    source_snapshot_revision: str,
    generated_at: datetime | None = None,
    sectors_per_direction: int = 8,
    candidate_limit: int = 3,
    max_workers: int = 4,
    board_fetcher: Callable[..., BoardLeaderSnapshotV2] | None = None,
    minute_fetcher: Callable[..., IntradayMinuteSeriesV1] | None = None,
    minute_batch_fetcher: (
        Callable[..., Mapping[str, IntradayMinuteSeriesV1]] | None
    ) = None,
) -> SectorResonanceBatchV1:
    """Backtrack the visible ranked sectors for one immutable accepted snapshot."""

    source_payload = (
        snapshot.model_dump(mode="json")
        if isinstance(snapshot, MarketWatchSnapshotV1)
        else snapshot
    )
    if stable_sha256(source_payload) != source_snapshot_revision:
        raise ValueError("source snapshot revision does not match its payload")
    canonical = MarketWatchSnapshotV1.model_validate(source_payload)
    if not 1 <= sectors_per_direction <= 8:
        raise ValueError("sectors_per_direction must be between 1 and 8")
    if not 1 <= candidate_limit <= 5:
        raise ValueError("candidate_limit must be between 1 and 5")
    if not 1 <= max_workers <= 8:
        raise ValueError("max_workers must be between 1 and 8")
    observed = generated_at or datetime.now(canonical.as_of.tzinfo)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("generated_at must include a timezone")
    if canonical.market_state.trading_date != observed.date():
        raise ValueError("current-session minute providers cannot backfill another date")

    if board_fetcher is None:
        from tradex.data_gateway.leadership import fetch_board_leader_snapshot

        board_fetcher = fetch_board_leader_snapshot
        # The default gateway shares one rate-limited request queue. Parallel
        # board attempts consume each other's short queue budget before mirror
        # fallback can run. Custom independent fetchers retain their concurrency.
        max_workers = 1
    if minute_fetcher is None and minute_batch_fetcher is None:
        from tradex.data_gateway.intraday import (
            fetch_intraday_minute_series,
            fetch_intraday_minute_series_batch_partial,
        )

        minute_fetcher = fetch_intraday_minute_series
        minute_batch_fetcher = fetch_intraday_minute_series_batch_partial

    jobs: list[tuple[Literal["defense", "offense"], SectorFlowSeriesV1]] = []
    for direction, trajectory in (
        ("defense", canonical.sector_flow_trajectory),
        ("offense", canonical.offense_sector_flow_trajectory),
    ):
        if trajectory is None:
            continue
        visible = select_sector_resonance_candidates(
            trajectory.sectors,
            limit=sectors_per_direction,
        )
        for sector in visible:
            jobs.append((direction, sector))
    if minute_batch_fetcher is not None:
        entries = _build_sector_resonance_entries_batched(
            tuple(jobs),
            board_fetcher=board_fetcher,
            minute_batch_fetcher=minute_batch_fetcher,
            candidate_limit=candidate_limit,
            fetched_at=observed,
            max_workers=max_workers,
        )
    else:
        assert minute_fetcher is not None
        entries = _build_sector_resonance_entries(
            tuple(jobs),
            board_fetcher=board_fetcher,
            minute_fetcher=minute_fetcher,
            candidate_limit=candidate_limit,
            fetched_at=observed,
            max_workers=max_workers,
        )
    return SectorResonanceBatchV1.create(
        source_snapshot_revision=source_snapshot_revision,
        source_snapshot_id=canonical.snapshot_id,
        source_as_of=canonical.as_of,
        generated_at=observed,
        entries=entries,
    )


__all__ = [
    "MAX_SHARED_INTERVAL_MINUTES",
    "MAX_TARGET_LAG_MINUTES",
    "MIN_MATCHED_INTERVALS",
    "MIN_DIRECTIONAL_AGREEMENT_RATIO",
    "MIN_RESONANCE_CORRELATION",
    "MIN_STOCK_RETURN_5M_PCT",
    "SectorResonanceBatchV1",
    "SectorResonanceEntryV1",
    "build_sector_resonance_batch",
    "build_sector_resonance_snapshot",
    "evaluate_minute_resonance",
    "select_sector_resonance_candidates",
]
