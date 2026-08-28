"""Daily A-share selection, archive and regression evaluation."""

from .backtest import walk_forward_backtest
from .contracts import (
    BalancedStockSelectionResultV1,
    DailyStockSelectionOutcomeV1,
    DailyStockSelectionV1,
    LimitUpTendencyCandidateV1,
    LimitUpTendencyScreenV1,
    SelectionBacktestReportV1,
    SelectionCandidateV1,
    StockSelectionStrategyCatalogV1,
    StockSelectionStrategyDefinitionV1,
    StockSelectionStrategyOutcomeV1,
    StockSelectionStrategyResultV1,
)
from .engine import (
    DEFAULT_SELECTION_CONFIG,
    SelectionConfigV1,
    screen_next_session_limit_up_tendency,
    select_daily_stocks,
)
from .service import DailyStockSelectionService
from .strategies import (
    REGISTERED_STOCK_SELECTION_STRATEGIES,
    build_strategy_results,
    evaluate_strategy_result,
    strategy_catalog,
)
from .store import DailyStockSelectionStore

__all__ = [
    "BalancedStockSelectionResultV1",
    "DEFAULT_SELECTION_CONFIG",
    "DailyStockSelectionOutcomeV1",
    "DailyStockSelectionService",
    "DailyStockSelectionStore",
    "DailyStockSelectionV1",
    "LimitUpTendencyCandidateV1",
    "LimitUpTendencyScreenV1",
    "REGISTERED_STOCK_SELECTION_STRATEGIES",
    "SelectionBacktestReportV1",
    "SelectionCandidateV1",
    "SelectionConfigV1",
    "StockSelectionStrategyCatalogV1",
    "StockSelectionStrategyDefinitionV1",
    "StockSelectionStrategyOutcomeV1",
    "StockSelectionStrategyResultV1",
    "build_strategy_results",
    "evaluate_strategy_result",
    "screen_next_session_limit_up_tendency",
    "select_daily_stocks",
    "strategy_catalog",
    "walk_forward_backtest",
]
