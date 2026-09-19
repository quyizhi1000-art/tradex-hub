"""聪明板块库: shared, evidence-gated, date-bound sector attribution."""

from .contracts import (
    SMART_SECTOR_LIBRARY_NAME,
    SmartSectorCandidateV1,
    SmartSectorDecisionV1,
    SmartSectorPolicyV1,
)
from .core import (
    BUSINESS_SECTOR_RULES,
    MARKET_THEME_RULES,
    BusinessSectorRule,
    MarketThemeMatch,
    MarketThemeRule,
    ReviewedMarketAttributionV1,
    infer_business_categories,
    infer_current_market_categories,
    infer_current_market_category,
    load_reviewed_market_attributions,
)
from .service import SmartSectorLibrary
from .catalog import SmartSectorCatalog, MarketMembershipV2, read_market_memberships

__all__ = [
    "BUSINESS_SECTOR_RULES",
    "MARKET_THEME_RULES",
    "SMART_SECTOR_LIBRARY_NAME",
    "BusinessSectorRule",
    "MarketThemeMatch",
    "MarketThemeRule",
    "ReviewedMarketAttributionV1",
    "SmartSectorCandidateV1",
    "SmartSectorDecisionV1",
    "SmartSectorLibrary",
    "SmartSectorCatalog",
    "MarketMembershipV2",
    "read_market_memberships",
    "SmartSectorPolicyV1",
    "infer_business_categories",
    "infer_current_market_categories",
    "infer_current_market_category",
    "load_reviewed_market_attributions",
]
