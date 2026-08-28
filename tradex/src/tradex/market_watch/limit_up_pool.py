"""Revision-bound live limit-up pool enriched by the stock relationship catalog.

The Collector owns refresh and persistence. The pool keeps canonical live
limit-up identity/status fields and joins the immutable instrument taxonomy
catalog by ``instrument_id``. It intentionally performs no price-path,
sector-flow, cohort, or provider-editorial attribution.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from tradex.data_gateway.contracts import (
    LimitEventSeriesV1,
    LimitUpEventV1,
    LimitUpStatusSeriesV1,
    LimitUpStatusV1,
)
from tradex.instrument_taxonomy.contracts import (
    StockRelationshipCatalogStatusV1,
    StockRelationshipProfileV1,
)

from .contracts import ContractModel, MarketWatchSnapshotV1
from .integrity import REVISION_PATTERN, stable_sha256


class LimitUpPoolItemV2(ContractModel):
    """One live limit-up stock with a typed relationship-catalog match."""

    instrument_id: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    name: str = Field(min_length=1)
    board_count: int | None = Field(default=None, ge=1)
    board_label: str | None = None
    first_sealed_at: datetime | None = None
    limit_up_type: str | None = None
    is_one_word_board: bool = False
    resealed: bool | None = None
    order_amount_cny: float | None = Field(default=None, ge=0)
    relationship_match_status: Literal["matched", "unmatched"]
    primary_business_key: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    primary_business_name: str | None = None
    business_tags: tuple[str, ...] = ()
    statistical_industry_name: str | None = None
    relationship_verification_status: Literal[
        "verified",
        "corroborated",
        "provider_only",
        "disputed",
        "stale",
        "unresolved",
    ] | None = None
    relationship_flags: tuple[str, ...] = ()

    @field_validator("first_sealed_at")
    @classmethod
    def require_aware_seal_time(cls, value: datetime | None):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("first_sealed_at must include a timezone")
        return value

    @field_validator("business_tags", "relationship_flags")
    @classmethod
    def require_unique_non_empty_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("relationship tags and flags cannot contain empty values")
        if len(value) != len(set(value)):
            raise ValueError("relationship tags and flags cannot contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_relationship_match(self) -> "LimitUpPoolItemV2":
        if bool(self.primary_business_key) != bool(self.primary_business_name):
            raise ValueError("primary business key and name must be present together")
        relationship_fields = (
            self.primary_business_key,
            self.primary_business_name,
            self.business_tags,
            self.statistical_industry_name,
            self.relationship_verification_status,
        )
        if self.relationship_match_status == "unmatched":
            if any(relationship_fields):
                raise ValueError("unmatched stock cannot carry catalog relationship fields")
            if "stock_relationship_unavailable" not in self.relationship_flags:
                raise ValueError("unmatched stock must explain the missing relationship")
        elif self.relationship_verification_status is None:
            raise ValueError("matched stock requires a catalog verification status")
        return self


class LimitUpPoolCategoryV2(ContractModel):
    business_key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    count: int = Field(ge=1)


class LimitUpPoolV2(ContractModel):
    """Live limit-up status joined only to ``stock_relationship_profile.v1``."""

    contract: Literal["limit_up_pool.v2"] = "limit_up_pool.v2"
    schema_version: Literal[2] = 2
    source_snapshot_revision: str = Field(pattern=REVISION_PATTERN)
    source_snapshot_id: str = Field(min_length=1)
    source_as_of: datetime
    trade_date: date
    generated_at: datetime
    limit_event_provider: str = Field(min_length=1)
    relationship_catalog_revision: str | None = Field(
        default=None,
        pattern=REVISION_PATTERN,
    )
    quality: Literal["accepted", "degraded"]
    quality_flags: tuple[str, ...] = ()
    pool_total: int = Field(ge=0, le=300)
    catalog_matched_count: int = Field(ge=0)
    business_classified_count: int = Field(ge=0)
    unmatched_count: int = Field(ge=0)
    categories: tuple[LimitUpPoolCategoryV2, ...] = ()
    items: tuple[LimitUpPoolItemV2, ...] = Field(default=(), max_length=300)
    pool_revision: str = Field(pattern=REVISION_PATTERN)

    @field_validator("source_as_of", "generated_at")
    @classmethod
    def require_aware_pool_time(cls, value: datetime, info):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_pool(self) -> "LimitUpPoolV2":
        if self.trade_date != self.source_as_of.date():
            raise ValueError("limit-up pool trade date must match the source snapshot")
        if self.pool_total != len(self.items):
            raise ValueError("limit-up pool total must equal item count")
        counts = Counter(item.relationship_match_status for item in self.items)
        if self.catalog_matched_count != counts["matched"]:
            raise ValueError("limit-up pool catalog matched count is inconsistent")
        if self.unmatched_count != counts["unmatched"]:
            raise ValueError("limit-up pool unmatched count is inconsistent")
        if self.pool_total != self.catalog_matched_count + self.unmatched_count:
            raise ValueError("limit-up pool catalog counts do not cover the pool")
        classified_count = sum(bool(item.primary_business_key) for item in self.items)
        if self.business_classified_count != classified_count:
            raise ValueError("limit-up pool business classified count is inconsistent")
        category_keys = [item.business_key for item in self.categories]
        if len(category_keys) != len(set(category_keys)):
            raise ValueError("limit-up pool business categories must be unique")
        if sum(item.count for item in self.categories) != self.pool_total:
            raise ValueError("limit-up pool business categories do not cover the pool")
        expected_quality = "degraded" if self.quality_flags else "accepted"
        if self.quality != expected_quality:
            raise ValueError("limit-up pool quality does not match its quality flags")
        expected_revision = stable_sha256(
            self.model_dump(mode="json", exclude={"pool_revision"})
        )
        if self.pool_revision != expected_revision:
            raise ValueError("limit-up pool revision does not match its payload")
        return self

    @classmethod
    def create(cls, **values) -> "LimitUpPoolV2":
        items = tuple(values.get("items", ()))
        payload = {
            "contract": "limit_up_pool.v2",
            "schema_version": 2,
            "quality_flags": (),
            "categories": _business_categories(items),
            "items": items,
            **values,
        }
        return cls(**payload, pool_revision=stable_sha256(payload))


RelationshipCatalogReader = Callable[
    [tuple[str, ...]],
    tuple[
        StockRelationshipCatalogStatusV1 | None,
        Mapping[str, StockRelationshipProfileV1],
    ],
]


def _read_relationship_catalog(
    instrument_ids: tuple[str, ...],
) -> tuple[
    StockRelationshipCatalogStatusV1 | None,
    Mapping[str, StockRelationshipProfileV1],
]:
    from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader

    with InstrumentTaxonomyReader() as reader:
        return reader.status(), reader.get_many(instrument_ids)


def _seal_datetime(
    event: LimitUpEventV1 | LimitUpStatusV1,
    trade_date: date,
    tzinfo,
) -> datetime | None:
    if event.first_sealed_at is None:
        return None
    return datetime.combine(trade_date, event.first_sealed_at, tzinfo=tzinfo)


def _is_one_word(event: LimitUpEventV1 | LimitUpStatusV1) -> bool:
    return "一字" in "".join(str(event.limit_up_type or "").split()).casefold()


def _pool_items(
    events: tuple[LimitUpEventV1 | LimitUpStatusV1, ...],
    relationships: Mapping[str, StockRelationshipProfileV1],
    *,
    trade_date: date,
    tzinfo,
) -> tuple[LimitUpPoolItemV2, ...]:
    items = []
    for event in events:
        relationship = relationships.get(event.instrument_id)
        statistical_industry = (
            relationship.statistical_industry.level3_name
            if relationship is not None and relationship.statistical_industry is not None
            else None
        )
        items.append(LimitUpPoolItemV2(
            instrument_id=event.instrument_id,
            name=event.name,
            board_count=event.board_count,
            board_label=event.board_label,
            first_sealed_at=_seal_datetime(event, trade_date, tzinfo),
            limit_up_type=event.limit_up_type,
            is_one_word_board=_is_one_word(event),
            resealed=event.resealed,
            order_amount_cny=getattr(event, "order_amount_cny", None),
            relationship_match_status="matched" if relationship else "unmatched",
            primary_business_key=(relationship.primary_business_key if relationship else None),
            primary_business_name=(relationship.primary_business_name if relationship else None),
            business_tags=(relationship.business_tags if relationship else ()),
            statistical_industry_name=statistical_industry,
            relationship_verification_status=(
                relationship.verification_status if relationship else None
            ),
            relationship_flags=(
                relationship.flags
                if relationship
                else ("stock_relationship_unavailable",)
            ),
        ))
    return tuple(items)


def _business_categories(
    items: tuple[LimitUpPoolItemV2, ...],
) -> tuple[LimitUpPoolCategoryV2, ...]:
    grouped: dict[str, list[LimitUpPoolItemV2]] = {}
    for item in items:
        key = item.primary_business_key or "unresolved_business"
        grouped.setdefault(key, []).append(item)
    categories = [
        LimitUpPoolCategoryV2(
            business_key=key,
            label=(
                "主营待核验"
                if key == "unresolved_business"
                else str(members[0].primary_business_name)
            ),
            count=len(members),
        )
        for key, members in grouped.items()
    ]
    categories.sort(key=lambda item: (
        item.business_key == "unresolved_business",
        -item.count,
        item.label,
    ))
    return tuple(categories)


def build_limit_up_pool(
    snapshot: MarketWatchSnapshotV1 | Mapping,
    *,
    source_snapshot_revision: str,
    generated_at: datetime,
    limit_event_fetcher: Callable[
        ..., LimitEventSeriesV1 | LimitUpStatusSeriesV1
    ] | None = None,
    relationship_reader: RelationshipCatalogReader | None = None,
) -> LimitUpPoolV2:
    """Join one canonical live limit-up pool to one catalog revision."""

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
    events = limit_event_fetcher(trade_date.isoformat(), now=generated_at)
    if events.trading_date != trade_date:
        raise ValueError("limit-event pool does not match the accepted snapshot date")

    instruments = tuple(item.instrument_id for item in events.events)
    relationship_reader = relationship_reader or _read_relationship_catalog
    catalog_status = None
    relationships: Mapping[str, StockRelationshipProfileV1] = {}
    quality_flags = list(events.metadata.quality_flags)
    if getattr(events.metadata.quality, "value", events.metadata.quality) != "accepted":
        quality_flags.append("limit_up_status_degraded")
    try:
        catalog_status, relationships = relationship_reader(instruments)
    except Exception:
        quality_flags.append("stock_relationship_catalog_unavailable")
    if catalog_status is None:
        quality_flags.append("stock_relationship_catalog_unavailable")
    if len(relationships) != len(instruments):
        quality_flags.append("stock_relationship_profiles_partial")

    items = _pool_items(
        events.events,
        relationships,
        trade_date=trade_date,
        tzinfo=canonical.as_of.tzinfo,
    )
    classified_count = sum(bool(item.primary_business_key) for item in items)
    if classified_count != len(items):
        quality_flags.append("stock_business_classification_partial")
    quality_flags = list(dict.fromkeys(quality_flags))
    return LimitUpPoolV2.create(
        source_snapshot_revision=source_snapshot_revision,
        source_snapshot_id=canonical.snapshot_id,
        source_as_of=canonical.as_of,
        trade_date=trade_date,
        generated_at=generated_at,
        limit_event_provider=events.metadata.provider,
        relationship_catalog_revision=(
            catalog_status.catalog_revision if catalog_status else None
        ),
        quality="degraded" if quality_flags else "accepted",
        quality_flags=tuple(quality_flags),
        pool_total=len(items),
        catalog_matched_count=sum(
            item.relationship_match_status == "matched" for item in items
        ),
        business_classified_count=classified_count,
        unmatched_count=sum(
            item.relationship_match_status == "unmatched" for item in items
        ),
        items=items,
    )


__all__ = [
    "LimitUpPoolCategoryV2",
    "LimitUpPoolItemV2",
    "LimitUpPoolV2",
    "build_limit_up_pool",
]
