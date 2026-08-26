"""Daily A-share selection, archive and regression evaluation."""

from .backtest import walk_forward_backtest
from .contracts import (
    DailyStockSelectionOutcomeV1,
    DailyStockSelectionV1,
    SelectionBacktestReportV1,
    SelectionCandidateV1,
)
from .engine import DEFAULT_SELECTION_CONFIG, SelectionConfigV1, select_daily_stocks
from .service import DailyStockSelectionService
from .store import DailyStockSelectionStore

__all__ = [
    "DEFAULT_SELECTION_CONFIG",
    "DailyStockSelectionOutcomeV1",
    "DailyStockSelectionService",
    "DailyStockSelectionStore",
    "DailyStockSelectionV1",
    "SelectionBacktestReportV1",
    "SelectionCandidateV1",
    "SelectionConfigV1",
    "select_daily_stocks",
    "walk_forward_backtest",
]
