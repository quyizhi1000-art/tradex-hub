"""Single current market membership, separate from immutable strategy evidence."""

from datetime import date
from typing import Literal

from pydantic import Field

from tradex.smart_sector_library.catalog import SmartSectorCatalog

from .contracts import SelectionModel


class SelectionIndustryDisplayV1(SelectionModel):
    contract: Literal["selection_industry_display.v1"] = "selection_industry_display.v1"
    schema_version: Literal[1] = 1
    basis: Literal["smart_sector_library"] = "smart_sector_library"
    catalog_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    as_of: date | None = None
    quality: Literal["accepted", "degraded", "unavailable"]
    names_by_instrument: dict[str, str] = Field(default_factory=dict)
    unclassified_instruments: tuple[str, ...] = ()


def load_selection_industry_display(instrument_ids) -> SelectionIndustryDisplayV1:
    """Only verified library membership can become a display/grouping label."""
    requested = tuple(sorted(set(instrument_ids)))
    with SmartSectorCatalog() as reader:
        memberships = reader.get_many(requested)
        revision, as_of = reader.revision, reader.as_of
    names = {key: item.primary_sector_name for key, item in memberships.items()
             if item.status == "verified" and item.primary_sector_name}
    missing = tuple(item for item in requested if item not in names)
    return SelectionIndustryDisplayV1(
        catalog_revision=revision, as_of=as_of,
        quality="accepted" if not missing else "degraded" if names else "unavailable",
        names_by_instrument=names, unclassified_instruments=missing,
    )
