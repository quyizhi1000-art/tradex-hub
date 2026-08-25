"""Mappings from provider payloads into canonical Tradex contracts."""

from .auctions import map_opening_auction_frame
from .daily_review import map_dragon_tiger_frame, map_stock_fund_flow_frame
from .leadership import (
    map_board_leader_frame,
    map_leader_quote_frame,
    map_stock_sector_profile_frame,
)
from .limit_events import map_limit_event_frame
from .market_overview import map_indices, map_participation_indices
from .market_universe import map_a_share_universe_payload
from .market_structure import map_market_breadth_frame, map_sector_quote_frame
from .etfs import map_etf_quote_payload
from .intraday import map_intraday_minute_frame
from .securities import map_ohlcv_frame, map_quote_frame
from .sector_flow import map_sector_intraday_fund_flow

__all__ = [
    "map_board_leader_frame",
    "map_dragon_tiger_frame",
    "map_a_share_universe_payload",
    "map_etf_quote_payload",
    "map_indices",
    "map_intraday_minute_frame",
    "map_leader_quote_frame",
    "map_limit_event_frame",
    "map_market_breadth_frame",
    "map_opening_auction_frame",
    "map_ohlcv_frame",
    "map_participation_indices",
    "map_quote_frame",
    "map_sector_quote_frame",
    "map_sector_intraday_fund_flow",
    "map_stock_sector_profile_frame",
    "map_stock_fund_flow_frame",
]
