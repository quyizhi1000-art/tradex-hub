"""Current industry-block display, separate from immutable strategy evidence."""

from datetime import date
from typing import Literal

from pydantic import Field

from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader

from .contracts import SelectionModel


class SelectionIndustryDisplayV1(SelectionModel):
    contract: Literal["selection_industry_display.v1"] = "selection_industry_display.v1"
    schema_version: Literal[1] = 1
    basis: Literal["current_ths_industry"] = "current_ths_industry"
    catalog_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    as_of: date | None = None
    quality: Literal["accepted", "degraded", "unavailable"]
    names_by_instrument: dict[str, str] = Field(default_factory=dict)
    unclassified_instruments: tuple[str, ...] = ()


def load_selection_industry_display(instrument_ids) -> SelectionIndustryDisplayV1:
    """Read local market blocks only; never mix in another classification."""
    requested = tuple(sorted(set(instrument_ids)))
    with InstrumentTaxonomyReader() as reader:
        status = reader.status()
        if status is None:
            return SelectionIndustryDisplayV1(
                quality="unavailable", unclassified_instruments=requested,
            )
        profiles = reader.get_many(requested)
        # A concurrent catalog replacement must not mix revisions in one view.
        if reader.status() != status:
            return SelectionIndustryDisplayV1(
                quality="unavailable", unclassified_instruments=requested,
            )
    names = {}
    for instrument_id in requested:
        profile = profiles.get(instrument_id)
        path = profile.market_industry if profile else None
        if (path and path.taxonomy == "ths" and path.level1_name
                and profile.as_of == status.as_of
                and (path.effective_from is None or path.effective_from <= status.as_of)
                and (path.effective_to is None or path.effective_to >= status.as_of)):
            name = path.level1_name.strip()
            if name:
                names[instrument_id] = name
    missing = tuple(item for item in requested if item not in names)
    return SelectionIndustryDisplayV1(
        catalog_revision=status.catalog_revision, as_of=status.as_of,
        quality="accepted" if not missing else "degraded" if names else "unavailable",
        names_by_instrument=names, unclassified_instruments=missing,
    )
