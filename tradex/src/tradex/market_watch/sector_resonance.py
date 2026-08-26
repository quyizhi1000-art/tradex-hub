"""Provider-neutral intraday sector/stock minute resonance.

The accepted market-watch snapshot owns the sector flow trajectory.  This
feature reads that immutable trajectory, obtains canonical current-session
stock minutes through the gateway, and emits a separately revisioned result.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, model_validator

from tradex.data_gateway.contracts import (
    BoardLeaderSnapshotV2,
    BoardLeaderV2,
    IntradayMinuteSeriesV1,
)

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
MAX_SHARED_INTERVAL_MINUTES = 2
MIN_MATCHED_INTERVALS = 4


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
    entries: tuple[SectorResonanceEntryV1, ...] = Field(max_length=8)
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


def evaluate_minute_resonance(
    sector: SectorFlowSeriesV1,
    candidate: BoardLeaderV2,
    stock_series: IntradayMinuteSeriesV1,
) -> SectorFlowLeaderV1 | None:
    """Return one high-resonance leader or fail closed.

    Both curves are reduced to the same exact timestamps.  A missing minute is
    tolerated only when both curves therefore use the same interval, and no
    shared interval may exceed two minutes.
    """

    if sector.latest is None or not sector.points:
        return None
    target = _minute(sector.latest.provider_as_of)
    baseline = target - timedelta(minutes=5)
    if stock_series.trading_date != target.date():
        return None

    sector_by_minute = {
        _minute(point.provider_as_of): point.cumulative_cny
        for point in sector.points
        if baseline <= _minute(point.provider_as_of) <= target
    }
    stock_by_minute = {
        datetime.combine(
            target.date(),
            point.minute,
            tzinfo=target.tzinfo,
        ): point.price
        for point in stock_series.points
        if baseline.time() <= point.minute <= target.time()
    }
    common = sorted(set(sector_by_minute).intersection(stock_by_minute))
    if (
        len(common) - 1 < MIN_MATCHED_INTERVALS
        or not common
        or common[0] != baseline
        or common[-1] != target
    ):
        return None
    if any(
        (later - earlier).total_seconds() / 60 > MAX_SHARED_INTERVAL_MINUTES
        for earlier, later in zip(common, common[1:])
    ):
        return None

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
    correlation = _pearson(sector_increments, stock_returns)
    if (
        sector_flow_5m <= 0
        or stock_return_5m < MIN_STOCK_RETURN_5M_PCT
        or correlation is None
        or correlation < MIN_RESONANCE_CORRELATION
    ):
        return None
    return SectorFlowLeaderV1(
        instrument_id=candidate.instrument_id,
        name=candidate.name,
        change_pct=candidate.change_pct,
        speed_pct=stock_return_5m,
        resonance_correlation=correlation,
        matched_interval_count=len(common) - 1,
        resonance_strength="high",
        price=stock_by_minute[target],
        provider_as_of=target,
    )


def _unavailable(label: str) -> SectorFlowLeaderSnapshotV1:
    return SectorFlowLeaderSnapshotV1(
        status="unavailable",
        status_label=label,
        selection_method="sector_fund_flow_minute_correlation.v1",
        marginal_window_minutes=5,
    )


def build_sector_resonance_snapshot(
    sector: SectorFlowSeriesV1,
    *,
    board_fetcher: Callable[..., BoardLeaderSnapshotV2],
    minute_fetcher: Callable[..., IntradayMinuteSeriesV1],
    candidate_limit: int = 3,
    fetched_at: datetime | None = None,
) -> SectorFlowLeaderSnapshotV1:
    if sector.latest is None or not sector.leader_board_code:
        return _unavailable("板块或成分股映射不可用")
    try:
        observed = fetched_at or datetime.now(sector.latest.provider_as_of.tzinfo)
        candidates = board_fetcher(
            sector.leader_board_code,
            limit=candidate_limit,
            now=observed,
        )
    except Exception:
        return _unavailable("成分股候选暂不可用")

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
        evaluated += 1
        minute_providers.add(series.metadata.provider)
        leader = evaluate_minute_resonance(sector, candidate, series)
        if leader is not None:
            leaders.append(leader)

    leaders.sort(
        key=lambda item: (
            -(item.resonance_correlation or -1.0),
            -(item.speed_pct or 0.0),
            item.instrument_id,
        )
    )
    source = "+".join(sorted(minute_providers)) or None
    if not leaders:
        return SectorFlowLeaderSnapshotV1(
            status="no_match" if evaluated else "unavailable",
            status_label="暂无高共振快涨股" if evaluated else "分钟证据不足",
            selection_method="sector_fund_flow_minute_correlation.v1",
            marginal_window_minutes=5,
            source=source,
            provider_as_of=sector.latest.provider_as_of,
        )
    return SectorFlowLeaderSnapshotV1(
        status="full",
        status_label="高共振快涨",
        selection_method="sector_fund_flow_minute_correlation.v1",
        marginal_window_minutes=5,
        source=source,
        provider_as_of=sector.latest.provider_as_of,
        leaders=tuple(leaders[:1]),
    )


def build_sector_resonance_batch(
    snapshot: MarketWatchSnapshotV1 | Mapping,
    *,
    source_snapshot_revision: str,
    generated_at: datetime | None = None,
    sectors_per_direction: int = 4,
    candidate_limit: int = 3,
    board_fetcher: Callable[..., BoardLeaderSnapshotV2] | None = None,
    minute_fetcher: Callable[..., IntradayMinuteSeriesV1] | None = None,
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
    if not 1 <= sectors_per_direction <= 4:
        raise ValueError("sectors_per_direction must be between 1 and 4")
    if not 1 <= candidate_limit <= 5:
        raise ValueError("candidate_limit must be between 1 and 5")
    observed = generated_at or datetime.now(canonical.as_of.tzinfo)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("generated_at must include a timezone")
    if canonical.market_state.trading_date != observed.date():
        raise ValueError("current-session minute providers cannot backfill another date")

    if board_fetcher is None:
        from tradex.data_gateway.leadership import fetch_board_leader_snapshot

        board_fetcher = fetch_board_leader_snapshot
    if minute_fetcher is None:
        from tradex.data_gateway.intraday import fetch_intraday_minute_series

        minute_fetcher = fetch_intraday_minute_series

    entries: list[SectorResonanceEntryV1] = []
    for direction, trajectory in (
        ("defense", canonical.sector_flow_trajectory),
        ("offense", canonical.offense_sector_flow_trajectory),
    ):
        if trajectory is None:
            continue
        visible = [item for item in trajectory.sectors if item.latest is not None][
            :sectors_per_direction
        ]
        for sector in visible:
            entries.append(
                SectorResonanceEntryV1(
                    direction=direction,
                    sector_key=sector.sector_key,
                    sector_name=sector.name,
                    leader_snapshot=build_sector_resonance_snapshot(
                        sector,
                        board_fetcher=board_fetcher,
                        minute_fetcher=minute_fetcher,
                        candidate_limit=candidate_limit,
                        fetched_at=observed,
                    ),
                )
            )
    return SectorResonanceBatchV1.create(
        source_snapshot_revision=source_snapshot_revision,
        source_snapshot_id=canonical.snapshot_id,
        source_as_of=canonical.as_of,
        generated_at=observed,
        entries=tuple(entries),
    )


__all__ = [
    "MAX_SHARED_INTERVAL_MINUTES",
    "MIN_MATCHED_INTERVALS",
    "MIN_RESONANCE_CORRELATION",
    "MIN_STOCK_RETURN_5M_PCT",
    "SectorResonanceBatchV1",
    "SectorResonanceEntryV1",
    "build_sector_resonance_batch",
    "build_sector_resonance_snapshot",
    "evaluate_minute_resonance",
]
