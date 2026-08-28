"""Revision-bound limit-up pool with fund-flow follow attribution.

The accepted market-watch snapshot owns the exact sector trajectories.  This
feature joins those immutable curves to the canonical daily limit-event pool,
stock sector profiles, and current-session stock minute curves.  Provider
editorial reasons are retained for audit only and never select the followed
sector.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import Field, field_validator, model_validator

from tradex.data_gateway.contracts import (
    IntradayMinuteSeriesV1,
    LimitEventSeriesV1,
    LimitUpEventV1,
    LimitUpStatusSeriesV1,
    LimitUpStatusV1,
    StockSectorProfileSeriesV1,
    StockSectorProfileV1,
)
from tradex.instrument_taxonomy.contracts import StockRelationshipProfileV1

from .contracts import ContractModel, MarketWatchSnapshotV1, SectorFlowSeriesV1
from .integrity import REVISION_PATTERN, stable_sha256


MIN_STOCK_RETURN_PCT = 0.10
MIN_CORRELATION = 0.60
MIN_DIRECTIONAL_AGREEMENT = 0.60
MIN_MATCHED_INTERVALS = 3
MAX_SHARED_INTERVAL_MINUTES = 2
MAX_SEAL_TARGET_LAG_MINUTES = 1
OPENING_EVIDENCE_CUTOFF = time(9, 33)
OPENING_COHORT_MIN_CONFIRMED_PEERS = 2
MAX_STOCK_MINUTE_BATCH_SIZE = 40

PATH_METHOD = "limit_up_seal_window_sector_flow_resonance.v1"
OPENING_COHORT_METHOD = "limit_up_opening_cohort_confirmation.v1"
UNRESOLVED_METHOD = "insufficient_verified_follow_evidence.v1"


class LimitUpFollowEvidenceV1(ContractModel):
    sector_key: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    sector_name: str = Field(min_length=1)
    taxonomy: Literal["industry", "concept"] | None = None
    window_start: datetime
    window_end: datetime
    sector_flow_delta_cny: float
    stock_return_pct: float
    correlation: float = Field(ge=-1, le=1)
    directional_agreement_ratio: float = Field(ge=0, le=1)
    matched_interval_count: int = Field(ge=3, le=6)
    score: float = Field(ge=0, le=1)

    @field_validator("window_start", "window_end")
    @classmethod
    def require_aware_time(cls, value: datetime, info):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> "LimitUpFollowEvidenceV1":
        if self.window_start >= self.window_end:
            raise ValueError("follow evidence window must be ordered")
        if self.sector_flow_delta_cny <= 0 or self.stock_return_pct <= 0:
            raise ValueError("confirmed follow evidence must move upward")
        return self


class LimitUpFollowItemV1(ContractModel):
    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    board_count: int | None = Field(default=None, ge=1)
    board_label: str | None = None
    first_sealed_at: datetime | None = None
    limit_up_type: str | None = None
    is_one_word_board: bool = False
    resealed: bool | None = None
    order_amount_cny: float | None = Field(default=None, ge=0)
    source_reason: str | None = Field(default=None, min_length=1)
    profile_provider: str | None = None
    profile_industry: str | None = None
    profile_concept_tags: tuple[str, ...] = ()
    primary_business_key: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    primary_business_name: str | None = None
    business_tags: tuple[str, ...] = ()
    statistical_industry_name: str | None = None
    business_verification_status: Literal[
        "verified",
        "corroborated",
        "provider_only",
        "disputed",
        "stale",
        "unresolved",
    ] | None = None
    stock_minute_provider: str | None = None
    follow_status: Literal["confirmed", "provisional", "unresolved"]
    confidence: Literal["high", "low", "unresolved"]
    attribution_method: Literal[
        "limit_up_seal_window_sector_flow_resonance.v1",
        "limit_up_opening_cohort_confirmation.v1",
        "insufficient_verified_follow_evidence.v1",
    ]
    followed_sector_key: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    followed_sector_name: str | None = None
    followed_sector_taxonomy: Literal["industry", "concept"] | None = None
    evidence: LimitUpFollowEvidenceV1 | None = None
    cohort_confirmed_peer_count: int | None = Field(default=None, ge=2)
    flags: tuple[str, ...] = ()

    @field_validator("first_sealed_at")
    @classmethod
    def require_aware_seal_time(cls, value: datetime | None):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("first_sealed_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_attribution(self) -> "LimitUpFollowItemV1":
        sector_fields = (
            self.followed_sector_key,
            self.followed_sector_name,
            self.followed_sector_taxonomy,
        )
        if self.follow_status == "confirmed":
            if self.confidence != "high" or self.attribution_method != PATH_METHOD:
                raise ValueError("confirmed follow attribution requires high path evidence")
            if self.evidence is None or any(value is None for value in sector_fields[:2]):
                raise ValueError("confirmed follow attribution requires a sector and evidence")
        elif self.follow_status == "provisional":
            if self.confidence != "low" or self.attribution_method != OPENING_COHORT_METHOD:
                raise ValueError("provisional attribution requires opening cohort evidence")
            if self.evidence is not None or self.cohort_confirmed_peer_count is None:
                raise ValueError("opening cohort attribution cannot carry path evidence")
            if any(value is None for value in sector_fields[:2]):
                raise ValueError("opening cohort attribution requires a sector")
        else:
            if self.confidence != "unresolved" or self.attribution_method != UNRESOLVED_METHOD:
                raise ValueError("unresolved attribution must remain explicit")
            if self.evidence is not None or any(value is not None for value in sector_fields):
                raise ValueError("unresolved attribution cannot claim a followed sector")
        return self


class LimitUpFollowCategoryV1(ContractModel):
    sector_key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    count: int = Field(ge=1)
    confidence: Literal["high", "mixed", "unresolved"]


class LimitUpFollowPoolV1(ContractModel):
    contract: Literal["limit_up_follow_pool.v1"] = "limit_up_follow_pool.v1"
    schema_version: Literal[1] = 1
    source_snapshot_revision: str = Field(pattern=REVISION_PATTERN)
    source_snapshot_id: str = Field(min_length=1)
    source_as_of: datetime
    trade_date: date
    generated_at: datetime
    limit_event_provider: str = Field(min_length=1)
    attribution_method: Literal[
        "limit_up_seal_window_sector_flow_resonance.v1"
    ] = PATH_METHOD
    quality: Literal["accepted", "degraded"]
    quality_flags: tuple[str, ...] = ()
    pool_total: int = Field(ge=0, le=300)
    confirmed_count: int = Field(ge=0)
    provisional_count: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    categories: tuple[LimitUpFollowCategoryV1, ...] = ()
    business_categories: tuple[LimitUpFollowCategoryV1, ...] = ()
    relationship_catalog_revision: str | None = Field(
        default=None,
        pattern=REVISION_PATTERN,
    )
    items: tuple[LimitUpFollowItemV1, ...] = Field(default=(), max_length=300)
    attribution_revision: str = Field(pattern=REVISION_PATTERN)

    @field_validator("source_as_of", "generated_at")
    @classmethod
    def require_aware_pool_time(cls, value: datetime, info):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_pool(self) -> "LimitUpFollowPoolV1":
        if self.trade_date != self.source_as_of.date():
            raise ValueError("limit-up pool trade date must match the source snapshot")
        if self.pool_total != len(self.items):
            raise ValueError("limit-up pool total must equal item count")
        counts = Counter(item.follow_status for item in self.items)
        if (
            self.confirmed_count != counts["confirmed"]
            or self.provisional_count != counts["provisional"]
            or self.unresolved_count != counts["unresolved"]
        ):
            raise ValueError("limit-up follow counts are inconsistent")
        if self.pool_total != (
            self.confirmed_count + self.provisional_count + self.unresolved_count
        ):
            raise ValueError("limit-up follow counts do not cover the pool")
        category_keys = [item.sector_key for item in self.categories]
        if len(category_keys) != len(set(category_keys)):
            raise ValueError("limit-up follow categories must be unique")
        if sum(item.count for item in self.categories) != self.pool_total:
            raise ValueError("limit-up follow categories do not cover the pool")
        if self.business_categories and sum(
            item.count for item in self.business_categories
        ) != self.pool_total:
            raise ValueError("limit-up business categories do not cover the pool")
        expected_quality = "degraded" if self.unresolved_count or self.provisional_count else "accepted"
        if self.quality != expected_quality:
            raise ValueError("limit-up pool quality does not match attribution coverage")
        expected_revision = stable_sha256(
            self.model_dump(mode="json", exclude={"attribution_revision"})
        )
        if self.attribution_revision != expected_revision:
            raise ValueError("limit-up attribution revision does not match its payload")
        return self

    @classmethod
    def create(cls, **values) -> "LimitUpFollowPoolV1":
        items = tuple(values.get("items", ()))
        payload = {
            "contract": "limit_up_follow_pool.v1",
            "schema_version": 1,
            "attribution_method": PATH_METHOD,
            "quality_flags": (),
            "categories": _categories(items),
            "business_categories": (),
            "relationship_catalog_revision": None,
            "items": items,
            **values,
        }
        return cls(**payload, attribution_revision=stable_sha256(payload))


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
        x * y for x, y in zip(left_centered, right_centered, strict=True)
    )
    left_scale = math.sqrt(sum(item * item for item in left_centered))
    right_scale = math.sqrt(sum(item * item for item in right_centered))
    if left_scale <= 1e-12 or right_scale <= 1e-12:
        return None
    return max(-1.0, min(1.0, numerator / (left_scale * right_scale)))


_TRAILING_TAXONOMY = re.compile(r"(?:行业|概念|[ⅠⅡⅢIV]+)$", re.IGNORECASE)


def _normalise_label(value: str | None) -> str:
    return "".join(str(value or "").split()).casefold()


def _label_variants(value: str | None) -> set[str]:
    exact = _normalise_label(value)
    if not exact:
        return set()
    stripped = _TRAILING_TAXONOMY.sub("", exact)
    return {exact, stripped} if stripped else {exact}


def _candidate_sectors(
    profile: StockSectorProfileV1 | None,
    sectors: tuple[SectorFlowSeriesV1, ...],
    relationship: StockRelationshipProfileV1 | None = None,
) -> tuple[SectorFlowSeriesV1, ...]:
    if profile is None and relationship is None:
        return ()
    industry_values = []
    concept_values = []
    if profile is not None:
        industry_values.append(profile.industry)
        concept_values.extend(profile.concept_tags)
    if relationship is not None:
        industry_values.extend((
            relationship.provider_industry,
            relationship.primary_business_name,
        ))
        industry_path = relationship.statistical_industry
        if industry_path is not None:
            industry_values.extend((
                industry_path.level1_name,
                industry_path.level2_name,
                industry_path.level3_name,
            ))
        concept_values.extend(relationship.business_tags)
    industry = set().union(
        *(_label_variants(item) for item in industry_values if item)
    ) if any(industry_values) else set()
    concepts = set().union(
        *(_label_variants(item) for item in concept_values if item)
    ) if any(concept_values) else set()
    result = []
    seen = set()
    for sector in sectors:
        names = _label_variants(sector.name)
        matches = bool(names & (concepts if sector.layer == "concept" else industry | concepts))
        if matches and sector.sector_key not in seen:
            result.append(sector)
            seen.add(sector.sector_key)
    return tuple(result)


def _seal_datetime(event: LimitUpEventV1, trade_date: date, tzinfo) -> datetime | None:
    if event.first_sealed_at is None:
        return None
    return datetime.combine(trade_date, event.first_sealed_at, tzinfo=tzinfo)


def _is_one_word(event: LimitUpEventV1) -> bool:
    return "一字" in _normalise_label(event.limit_up_type)


def _evaluate_follow_path(
    sector: SectorFlowSeriesV1,
    stock: IntradayMinuteSeriesV1,
    seal_at: datetime,
) -> LimitUpFollowEvidenceV1 | None:
    if stock.trading_date != seal_at.date() or not sector.points:
        return None
    target_limit = _minute(seal_at)
    sector_points = {
        _minute(point.provider_as_of): point
        for point in sector.points
        if _minute(point.provider_as_of).date() == target_limit.date()
        and _minute(point.provider_as_of) <= target_limit
    }
    stock_prices = {
        datetime.combine(
            target_limit.date(),
            point.minute,
            tzinfo=target_limit.tzinfo,
        ): point.price
        for point in stock.points
    }
    aligned = None
    for lag in range(MAX_SEAL_TARGET_LAG_MINUTES + 1):
        target = target_limit - timedelta(minutes=lag)
        target_point = sector_points.get(target)
        if target_point is None:
            continue
        baseline_as_of = target_point.delta_5m_baseline_as_of
        declared_delta = target_point.delta_5m_cny
        if baseline_as_of is None or declared_delta is None or declared_delta <= 0:
            continue
        baseline = _minute(baseline_as_of)
        if baseline >= target:
            continue
        sector_values = {
            observed: point.cumulative_cny
            for observed, point in sector_points.items()
            if baseline <= observed <= target
        }
        common = sorted(set(sector_values).intersection(stock_prices))
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
        aligned = target, baseline, sector_values, common
        break
    if aligned is None:
        return None
    target, baseline, sector_values, common = aligned
    flow_increments = [
        sector_values[later] - sector_values[earlier]
        for earlier, later in zip(common, common[1:])
    ]
    stock_returns = [
        (stock_prices[later] / stock_prices[earlier] - 1.0) * 100.0
        for earlier, later in zip(common, common[1:])
    ]
    flow_delta = sector_values[target] - sector_values[baseline]
    stock_return = (stock_prices[target] / stock_prices[baseline] - 1.0) * 100.0
    sector_path = [sector_values[item] - sector_values[baseline] for item in common[1:]]
    stock_path = [
        (stock_prices[item] / stock_prices[baseline] - 1.0) * 100.0
        for item in common[1:]
    ]
    correlation = _pearson(sector_path, stock_path)
    agreement = sum(
        flow * stock_move > 0
        for flow, stock_move in zip(flow_increments, stock_returns, strict=True)
    ) / len(flow_increments)
    if (
        flow_delta <= 0
        or stock_return < MIN_STOCK_RETURN_PCT
        or correlation is None
        or correlation < MIN_CORRELATION
        or agreement < MIN_DIRECTIONAL_AGREEMENT
    ):
        return None
    score = min(
        1.0,
        correlation * 0.65 + agreement * 0.25 + min(stock_return / 2.0, 1.0) * 0.10,
    )
    return LimitUpFollowEvidenceV1(
        sector_key=sector.sector_key,
        sector_name=sector.name,
        taxonomy=sector.taxonomy,
        window_start=baseline,
        window_end=target,
        sector_flow_delta_cny=flow_delta,
        stock_return_pct=stock_return,
        correlation=correlation,
        directional_agreement_ratio=agreement,
        matched_interval_count=len(common) - 1,
        score=score,
    )


def _opening_flow_delta(sector: SectorFlowSeriesV1, trade_date: date) -> float | None:
    points = [
        point
        for point in sector.points
        if point.provider_as_of.date() == trade_date
        and time(9, 30) <= point.provider_as_of.time().replace(tzinfo=None) <= time(9, 35)
    ]
    if len(points) < 2:
        return None
    return points[-1].cumulative_cny - points[0].cumulative_cny


def _base_item(
    event: LimitUpEventV1 | LimitUpStatusV1,
    profile: StockSectorProfileV1 | None,
    relationship: StockRelationshipProfileV1 | None,
    stock: IntradayMinuteSeriesV1 | None,
    *,
    trade_date: date,
    tzinfo,
    flags: tuple[str, ...],
) -> dict:
    return {
        "instrument_id": event.instrument_id,
        "name": event.name,
        "board_count": event.board_count,
        "board_label": event.board_label,
        "first_sealed_at": _seal_datetime(event, trade_date, tzinfo),
        "limit_up_type": event.limit_up_type,
        "is_one_word_board": _is_one_word(event),
        "resealed": event.resealed,
        "order_amount_cny": getattr(event, "order_amount_cny", None),
        "source_reason": getattr(event, "reason", None),
        "profile_provider": profile.provider_variant if profile else None,
        "profile_industry": profile.industry if profile else None,
        "profile_concept_tags": profile.concept_tags if profile else (),
        "primary_business_key": relationship.primary_business_key if relationship else None,
        "primary_business_name": relationship.primary_business_name if relationship else None,
        "business_tags": relationship.business_tags if relationship else (),
        "statistical_industry_name": (
            relationship.statistical_industry.level3_name
            if relationship and relationship.statistical_industry
            else None
        ),
        "business_verification_status": (
            relationship.verification_status if relationship else None
        ),
        "stock_minute_provider": stock.metadata.provider if stock else None,
        "flags": flags,
    }


def attribute_limit_up_follow_pool(
    events: tuple[LimitUpEventV1 | LimitUpStatusV1, ...],
    profiles: Mapping[str, StockSectorProfileV1],
    minute_series: Mapping[str, IntradayMinuteSeriesV1],
    sectors: tuple[SectorFlowSeriesV1, ...],
    *,
    trade_date: date,
    tzinfo,
    relationships: Mapping[str, StockRelationshipProfileV1] | None = None,
) -> tuple[LimitUpFollowItemV1, ...]:
    """Attribute every event without treating editorial reasons as evidence."""

    relationship_map = relationships or {}
    candidate_map = {
        event.instrument_id: _candidate_sectors(
            profiles.get(event.instrument_id),
            sectors,
            relationship_map.get(event.instrument_id),
        )
        for event in events
    }
    items: list[LimitUpFollowItemV1] = []
    for event in events:
        profile = profiles.get(event.instrument_id)
        relationship = relationship_map.get(event.instrument_id)
        stock = minute_series.get(event.instrument_id)
        seal_at = _seal_datetime(event, trade_date, tzinfo)
        flags = []
        if profile is None:
            flags.append("profile_unavailable")
        if not candidate_map[event.instrument_id]:
            flags.append("candidate_sector_unavailable")
        if stock is None:
            flags.append("stock_minute_unavailable")
        if seal_at is None:
            flags.append("first_seal_time_unavailable")
        opening_only = bool(
            _is_one_word(event)
            or (seal_at is not None and seal_at.time().replace(tzinfo=None) <= OPENING_EVIDENCE_CUTOFF)
        )
        if opening_only:
            flags.append("opening_path_not_identifiable")
        evidence = []
        if stock is not None and seal_at is not None and not opening_only:
            evidence = [
                result
                for sector in candidate_map[event.instrument_id]
                if (result := _evaluate_follow_path(sector, stock, seal_at)) is not None
            ]
        if evidence:
            best = sorted(
                evidence,
                key=lambda item: (-item.score, -item.sector_flow_delta_cny, item.sector_key),
            )[0]
            items.append(LimitUpFollowItemV1(
                **_base_item(
                    event,
                    profile,
                    relationship,
                    stock,
                    trade_date=trade_date,
                    tzinfo=tzinfo,
                    flags=tuple(flags),
                ),
                follow_status="confirmed",
                confidence="high",
                attribution_method=PATH_METHOD,
                followed_sector_key=best.sector_key,
                followed_sector_name=best.sector_name,
                followed_sector_taxonomy=best.taxonomy,
                evidence=best,
            ))
        else:
            items.append(LimitUpFollowItemV1(
                **_base_item(
                    event,
                    profile,
                    relationship,
                    stock,
                    trade_date=trade_date,
                    tzinfo=tzinfo,
                    flags=tuple(flags or ["no_verified_path_match"]),
                ),
                follow_status="unresolved",
                confidence="unresolved",
                attribution_method=UNRESOLVED_METHOD,
            ))

    confirmed_peers = Counter(
        item.followed_sector_key
        for item in items
        if item.follow_status == "confirmed" and not item.is_one_word_board
    )
    by_id = {event.instrument_id: event for event in events}
    upgraded: list[LimitUpFollowItemV1] = []
    for item in items:
        seal_time = item.first_sealed_at.time().replace(tzinfo=None) if item.first_sealed_at else None
        opening_only = item.is_one_word_board or (
            seal_time is not None and seal_time <= OPENING_EVIDENCE_CUTOFF
        )
        if item.follow_status != "unresolved" or not opening_only:
            upgraded.append(item)
            continue
        options = []
        for sector in candidate_map[item.instrument_id]:
            peers = confirmed_peers[sector.sector_key]
            opening_flow = _opening_flow_delta(sector, trade_date)
            if (
                peers >= OPENING_COHORT_MIN_CONFIRMED_PEERS
                and opening_flow is not None
                and opening_flow > 0
            ):
                options.append((peers, opening_flow, sector.sector_key, sector))
        options.sort(key=lambda value: (-value[0], -value[1], value[2]))
        unique_dominant = bool(
            options
            and (len(options) == 1 or options[0][0] > options[1][0])
        )
        if not unique_dominant:
            upgraded.append(item)
            continue
        peers, _flow, _key, sector = options[0]
        event = by_id[item.instrument_id]
        profile = profiles.get(item.instrument_id)
        relationship = relationship_map.get(item.instrument_id)
        upgraded.append(LimitUpFollowItemV1(
            **_base_item(
                event,
                profile,
                relationship,
                minute_series.get(item.instrument_id),
                trade_date=trade_date,
                tzinfo=tzinfo,
                flags=tuple(dict.fromkeys((*item.flags, "opening_cohort_only_low_confidence"))),
            ),
            follow_status="provisional",
            confidence="low",
            attribution_method=OPENING_COHORT_METHOD,
            followed_sector_key=sector.sector_key,
            followed_sector_name=sector.name,
            followed_sector_taxonomy=sector.taxonomy,
            cohort_confirmed_peer_count=peers,
        ))
    return tuple(upgraded)


def _categories(
    items: tuple[LimitUpFollowItemV1, ...],
    *,
    unresolved_label: str = "待确认",
) -> tuple[LimitUpFollowCategoryV1, ...]:
    grouped: dict[str, list[LimitUpFollowItemV1]] = {}
    for item in items:
        key = item.followed_sector_key or "unresolved"
        grouped.setdefault(key, []).append(item)
    categories = []
    for key, members in grouped.items():
        statuses = {item.follow_status for item in members}
        confidence = (
            "unresolved"
            if key == "unresolved"
            else "high"
            if statuses == {"confirmed"}
            else "mixed"
        )
        categories.append(LimitUpFollowCategoryV1(
            sector_key=key,
            label=(
                unresolved_label
                if key == "unresolved"
                else str(members[0].followed_sector_name)
            ),
            count=len(members),
            confidence=confidence,
        ))
    categories.sort(key=lambda item: (item.sector_key == "unresolved", -item.count, item.label))
    return tuple(categories)


def _live_status_items(
    events: tuple[LimitUpStatusV1, ...],
    *,
    trade_date: date,
    tzinfo,
) -> tuple[LimitUpFollowItemV1, ...]:
    """Expose canonical limit status immediately without running attribution."""

    return tuple(
        LimitUpFollowItemV1(
            **_base_item(
                event,
                None,
                None,
                None,
                trade_date=trade_date,
                tzinfo=tzinfo,
                flags=("analysis_pending_midday_or_post_close",),
            ),
            follow_status="unresolved",
            confidence="unresolved",
            attribution_method=UNRESOLVED_METHOD,
        )
        for event in events
    )


def _business_categories(
    items: tuple[LimitUpFollowItemV1, ...],
) -> tuple[LimitUpFollowCategoryV1, ...]:
    grouped: dict[str, list[LimitUpFollowItemV1]] = {}
    for item in items:
        key = item.primary_business_key or "unresolved_business"
        grouped.setdefault(key, []).append(item)
    categories = []
    for key, members in grouped.items():
        statuses = {item.business_verification_status for item in members}
        confidence = (
            "unresolved"
            if key == "unresolved_business"
            else "high"
            if statuses == {"verified"}
            else "mixed"
        )
        categories.append(LimitUpFollowCategoryV1(
            sector_key=key,
            label=(
                "主营待核验"
                if key == "unresolved_business"
                else str(members[0].primary_business_name)
            ),
            count=len(members),
            confidence=confidence,
        ))
    categories.sort(
        key=lambda item: (
            item.sector_key == "unresolved_business",
            -item.count,
            item.label,
        )
    )
    return tuple(categories)


def _fetch_stock_minutes_in_batches(
    instruments: tuple[str, ...],
    fetcher: Callable[..., Mapping[str, IntradayMinuteSeriesV1]],
    *,
    generated_at: datetime,
) -> tuple[dict[str, IntradayMinuteSeriesV1], int]:
    results: dict[str, IntradayMinuteSeriesV1] = {}
    failed_batches = 0
    for offset in range(0, len(instruments), MAX_STOCK_MINUTE_BATCH_SIZE):
        batch = instruments[offset : offset + MAX_STOCK_MINUTE_BATCH_SIZE]
        try:
            results.update(fetcher(batch, now=generated_at))
        except Exception:
            failed_batches += 1
    return results, failed_batches


def build_limit_up_follow_pool(
    snapshot: MarketWatchSnapshotV1 | Mapping,
    *,
    source_snapshot_revision: str,
    generated_at: datetime,
    limit_event_fetcher: Callable[
        ..., LimitEventSeriesV1 | LimitUpStatusSeriesV1
    ] | None = None,
    profile_fetcher: Callable[..., StockSectorProfileSeriesV1] | None = None,
    minute_batch_fetcher: Callable[..., Mapping[str, IntradayMinuteSeriesV1]] | None = None,
    relationship_loader: Callable[
        [tuple[str, ...]], Mapping[str, StockRelationshipProfileV1]
    ] | None = None,
    analyze: bool = True,
) -> LimitUpFollowPoolV1:
    """Build live limit status or one scheduled attribution in Collector."""

    source_payload = (
        snapshot.model_dump(mode="json")
        if isinstance(snapshot, MarketWatchSnapshotV1)
        else snapshot
    )
    if stable_sha256(source_payload) != source_snapshot_revision:
        raise ValueError("source snapshot revision does not match its payload")
    canonical = MarketWatchSnapshotV1.model_validate(source_payload)
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must include a timezone")
    trade_date = canonical.as_of.date()

    if limit_event_fetcher is None:
        from tradex.data_gateway.limit_events import fetch_limit_up_status

        limit_event_fetcher = fetch_limit_up_status
    if profile_fetcher is None and analyze:
        from tradex.data_gateway.leadership import fetch_stock_sector_profiles

        profile_fetcher = fetch_stock_sector_profiles
    if minute_batch_fetcher is None and analyze:
        from tradex.data_gateway.intraday import (
            fetch_intraday_minute_series_batch_partial,
        )

        minute_batch_fetcher = fetch_intraday_minute_series_batch_partial

    events = limit_event_fetcher(trade_date.isoformat(), now=generated_at)
    if events.trading_date != trade_date:
        raise ValueError("limit-event pool does not match the accepted snapshot date")
    instruments = tuple(item.instrument_id for item in events.events)
    profiles: dict[str, StockSectorProfileV1] = {}
    relationships: Mapping[str, StockRelationshipProfileV1] = {}
    relationship_catalog_revision = None
    minutes: Mapping[str, IntradayMinuteSeriesV1] = {}
    quality_flags = list(events.metadata.quality_flags)
    if instruments and analyze:
        if relationship_loader is not None:
            try:
                relationships = relationship_loader(instruments)
                if len(relationships) != len(instruments):
                    quality_flags.append("stock_relationship_profiles_partial")
                from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader

                with InstrumentTaxonomyReader() as reader:
                    catalog_status = reader.status()
                relationship_catalog_revision = (
                    catalog_status.catalog_revision if catalog_status else None
                )
            except Exception:
                quality_flags.append("stock_relationship_catalog_unavailable")
        try:
            assert profile_fetcher is not None
            profile_series = profile_fetcher(
                [item[:6] for item in instruments],
                trade_date=trade_date.isoformat(),
                now=generated_at,
            )
            profiles = {item.instrument_id: item for item in profile_series.profiles}
            quality_flags.extend(profile_series.metadata.quality_flags)
        except Exception:
            quality_flags.append("stock_sector_profiles_unavailable")
        assert minute_batch_fetcher is not None
        minute_results, failed_minute_batches = _fetch_stock_minutes_in_batches(
            instruments,
            minute_batch_fetcher,
            generated_at=generated_at,
        )
        minutes = minute_results
        if failed_minute_batches and minutes:
            quality_flags.append("stock_minute_batch_partial")
        elif failed_minute_batches:
            quality_flags.append("stock_minute_batch_unavailable")

    sectors = tuple(
        item
        for trajectory in (
            canonical.sector_flow_trajectory,
            canonical.offense_sector_flow_trajectory,
        )
        if trajectory is not None
        for item in trajectory.sectors
    )
    items = (
        attribute_limit_up_follow_pool(
            events.events,
            profiles,
            minutes,
            sectors,
            trade_date=trade_date,
            tzinfo=canonical.as_of.tzinfo,
            relationships=relationships,
        )
        if analyze
        else _live_status_items(
            events.events,
            trade_date=trade_date,
            tzinfo=canonical.as_of.tzinfo,
        )
    )
    counts = Counter(item.follow_status for item in items)
    if not analyze:
        quality_flags.append("analysis_pending_midday_or_post_close")
    elif counts["unresolved"]:
        quality_flags.append("follow_attribution_partial")
    if analyze and counts["provisional"]:
        quality_flags.append("opening_cohort_low_confidence")
    return LimitUpFollowPoolV1.create(
        source_snapshot_revision=source_snapshot_revision,
        source_snapshot_id=canonical.snapshot_id,
        source_as_of=canonical.as_of,
        trade_date=trade_date,
        generated_at=generated_at,
        limit_event_provider=events.metadata.provider,
        quality="degraded" if counts["unresolved"] or counts["provisional"] else "accepted",
        quality_flags=tuple(dict.fromkeys(quality_flags)),
        pool_total=len(items),
        confirmed_count=counts["confirmed"],
        provisional_count=counts["provisional"],
        unresolved_count=counts["unresolved"],
        categories=_categories(
            items,
            unresolved_label=("待午盘/盘后归类" if not analyze else "待确认"),
        ),
        business_categories=_business_categories(items) if analyze else (),
        relationship_catalog_revision=relationship_catalog_revision,
        items=items,
    )


__all__ = [
    "LimitUpFollowCategoryV1",
    "LimitUpFollowEvidenceV1",
    "LimitUpFollowItemV1",
    "LimitUpFollowPoolV1",
    "_live_status_items",
    "attribute_limit_up_follow_pool",
    "build_limit_up_follow_pool",
]
