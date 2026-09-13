"""Compatibility imports for the shared Smart Sector Library.

New consumers should import :mod:`tradex.smart_sector_library` directly.
"""

from tradex.smart_sector_library.core import (
    MARKET_THEME_RULES,
    MarketThemeMatch,
    MarketThemeRule,
    ReviewedMarketAttributionV1,
    infer_current_market_category,
    load_reviewed_market_attributions,
)

__all__ = [
    "MARKET_THEME_RULES",
    "MarketThemeMatch",
    "MarketThemeRule",
    "ReviewedMarketAttributionV1",
    "infer_current_market_category",
    "load_reviewed_market_attributions",
]
