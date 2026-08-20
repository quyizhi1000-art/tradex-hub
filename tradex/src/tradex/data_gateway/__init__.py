"""Provider-neutral financial data contracts and gateways.

The data gateway is the anti-corruption boundary between vendor payloads and
Tradex features.  Provider adapters may change independently; callers consume
versioned canonical models instead of DataFrames or vendor field names.
"""

from .contracts import (
    BoardLeaderSnapshotV1,
    BoardLeaderV1,
    ContractMetadata,
    EtfQuoteSeriesV1,
    EtfQuoteV1,
    IndexQuoteV1,
    LeaderQuoteSeriesV1,
    LeaderQuoteV1,
    LimitEventSeriesV1,
    LimitEventTradeStatusV1,
    LimitUpEventV1,
    MarketOverviewV1,
    MarketBreadthV1,
    MarketStateV1,
    MarketTurnoverV1,
    OHLCVBarV1,
    OHLCVSeriesV1,
    ParticipationIndexV1,
    QualityStatus,
    QuoteSnapshotV1,
    SectorQuoteSeriesV1,
    SectorQuoteV1,
    StockSectorProfileSeriesV1,
    StockSectorProfileV1,
)
from .etfs import etf_quotes_to_legacy_records, fetch_etf_quotes
from .leadership import (
    board_leader_snapshot_to_legacy_payload,
    fetch_board_leader_snapshot,
    fetch_leader_quotes,
    fetch_stock_sector_profiles,
    leader_quotes_to_legacy_records,
    stock_sector_profiles_to_legacy_records,
)
from .limit_events import (
    fetch_limit_up_events,
    limit_event_series_to_component_metadata,
    limit_event_series_to_legacy_records,
)
from .market import fetch_market_overview, market_overview_to_legacy_payload
from .market_structure import (
    fetch_market_breadth_snapshot,
    fetch_sector_quotes,
    market_breadth_to_legacy_records,
    metadata_to_component_status,
    sector_quotes_to_legacy_records,
)
from .securities import (
    fetch_ohlcv_series,
    fetch_quote_snapshot,
    ohlcv_series_to_legacy_records,
    quote_snapshot_to_legacy_records,
)

__all__ = [
    "BoardLeaderSnapshotV1",
    "BoardLeaderV1",
    "ContractMetadata",
    "EtfQuoteSeriesV1",
    "EtfQuoteV1",
    "IndexQuoteV1",
    "LeaderQuoteSeriesV1",
    "LeaderQuoteV1",
    "LimitEventSeriesV1",
    "LimitEventTradeStatusV1",
    "LimitUpEventV1",
    "MarketOverviewV1",
    "MarketBreadthV1",
    "MarketStateV1",
    "MarketTurnoverV1",
    "OHLCVBarV1",
    "OHLCVSeriesV1",
    "ParticipationIndexV1",
    "QualityStatus",
    "QuoteSnapshotV1",
    "SectorQuoteSeriesV1",
    "SectorQuoteV1",
    "StockSectorProfileSeriesV1",
    "StockSectorProfileV1",
    "board_leader_snapshot_to_legacy_payload",
    "fetch_board_leader_snapshot",
    "fetch_etf_quotes",
    "fetch_leader_quotes",
    "fetch_limit_up_events",
    "fetch_market_breadth_snapshot",
    "fetch_market_overview",
    "fetch_ohlcv_series",
    "fetch_quote_snapshot",
    "fetch_sector_quotes",
    "fetch_stock_sector_profiles",
    "leader_quotes_to_legacy_records",
    "etf_quotes_to_legacy_records",
    "limit_event_series_to_component_metadata",
    "limit_event_series_to_legacy_records",
    "market_breadth_to_legacy_records",
    "market_overview_to_legacy_payload",
    "metadata_to_component_status",
    "ohlcv_series_to_legacy_records",
    "quote_snapshot_to_legacy_records",
    "sector_quotes_to_legacy_records",
    "stock_sector_profiles_to_legacy_records",
]
