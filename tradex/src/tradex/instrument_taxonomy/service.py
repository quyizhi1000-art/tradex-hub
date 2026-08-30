"""Single refresh owner for the immutable instrument relationship catalog."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .builder import build_stock_relationship_catalog
from .contracts import StockRelationshipCatalogStatusV1
from .store import InstrumentTaxonomyStore


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _require_publishable_source(source: Mapping[str, Any]) -> None:
    reporting_periods = tuple(
        str(item).strip()
        for item in source.get("reporting_periods", ())
        if str(item).strip()
    )
    if not reporting_periods:
        return
    latest_period = max(reporting_periods)
    unavailable_prefix = f"business_segments_unavailable:{latest_period}:"
    if any(
        str(flag).startswith(unavailable_prefix)
        for flag in source.get("flags", ())
    ):
        raise RuntimeError(
            "latest business segments are unavailable; preserving the prior catalog"
        )


class InstrumentTaxonomyService:
    def __init__(self, store: InstrumentTaxonomyStore | None = None) -> None:
        self.store = store or InstrumentTaxonomyStore()
        self._owns_store = store is None

    def refresh(
        self,
        *,
        as_of: date | str | None = None,
        now: datetime | None = None,
        source_loader: Callable[[date | str | None], Mapping[str, Any]] | None = None,
        official_evidence_path: str | Path | None = None,
    ) -> StockRelationshipCatalogStatusV1:
        generated_at = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
        if source_loader is None:
            from tradex.data_gateway.instrument_taxonomy import (
                fetch_stock_relationship_source_bundle,
            )

            source_loader = fetch_stock_relationship_source_bundle
        source = source_loader(as_of)
        _require_publishable_source(source)
        status, profiles = build_stock_relationship_catalog(
            source,
            generated_at=generated_at,
            official_evidence_path=official_evidence_path,
        )
        self.store.replace_catalog(status, profiles)
        return status

    def close(self) -> None:
        if self._owns_store:
            self.store.close()

    def __enter__(self) -> "InstrumentTaxonomyService":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


__all__ = ["InstrumentTaxonomyService"]
