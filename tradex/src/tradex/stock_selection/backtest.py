"""Walk-forward regression using next-session opens, costs and exact snapshots."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1

from .contracts import SelectionBacktestReportV1, SelectionBacktestTradeV1
from .engine import DEFAULT_SELECTION_CONFIG, SelectionConfigV1, select_daily_stocks


def _factor_map(snapshot: DailyStockFactorSnapshotV1):
    return {item.instrument_id: item for item in snapshot.factors}


def _max_drawdown(returns_pct: Sequence[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns_pct:
        equity *= 1.0 + value / 100.0
        peak = max(peak, equity)
        worst = min(worst, (equity / peak - 1.0) * 100.0)
    return worst


def walk_forward_backtest(
    snapshots: Sequence[DailyStockFactorSnapshotV1],
    *,
    config: SelectionConfigV1 = DEFAULT_SELECTION_CONFIG,
    horizon_sessions: int = 5,
    round_trip_cost_pct: float = 0.15,
    minimum_coverage: float = 0.70,
) -> SelectionBacktestReportV1:
    if horizon_sessions < 1:
        raise ValueError("horizon_sessions must be positive")
    if round_trip_cost_pct < 0:
        raise ValueError("round_trip_cost_pct cannot be negative")
    ordered = sorted(snapshots, key=lambda item: item.trade_date)
    if len({item.trade_date for item in ordered}) != len(ordered):
        raise ValueError("backtest snapshots must have unique trade dates")

    trades: list[SelectionBacktestTradeV1] = []
    for signal_index in range(max(0, len(ordered) - horizon_sessions)):
        signal = ordered[signal_index]
        entry = ordered[signal_index + 1]
        exit_snapshot = ordered[signal_index + horizon_sessions]
        selection = select_daily_stocks(signal, config=config)
        if not selection.candidates:
            continue
        entry_rows = _factor_map(entry)
        exit_rows = _factor_map(exit_snapshot)
        returns: list[float] = []
        for candidate in selection.candidates:
            entry_row = entry_rows.get(candidate.instrument_id)
            exit_row = exit_rows.get(candidate.instrument_id)
            if entry_row is None or exit_row is None or entry_row.open <= 0:
                continue
            returns.append(
                (exit_row.close / entry_row.open - 1.0) * 100.0
                - round_trip_cost_pct
            )
        coverage = len(returns) / selection.selected_count
        if not returns or coverage < minimum_coverage:
            continue
        portfolio_return = statistics.mean(returns)
        benchmark_return = (
            exit_snapshot.benchmark_close / entry.benchmark_open - 1.0
        ) * 100.0
        trades.append(
            SelectionBacktestTradeV1(
                signal_trade_date=signal.trade_date,
                entry_trade_date=entry.trade_date,
                exit_trade_date=exit_snapshot.trade_date,
                selected_count=selection.selected_count,
                evaluated_count=len(returns),
                coverage=coverage,
                portfolio_return_pct=portfolio_return,
                benchmark_return_pct=benchmark_return,
                excess_return_pct=portfolio_return - benchmark_return,
            )
        )

    returns = [item.portfolio_return_pct for item in trades]
    excess = [item.excess_return_pct for item in trades]
    if returns:
        mean_return = statistics.mean(returns)
        mean_excess = statistics.mean(excess)
        hit_rate = sum(value > 0 for value in excess) / len(excess)
        deviation = statistics.pstdev(returns)
        sharpe = (
            mean_return / deviation * math.sqrt(252.0 / horizon_sessions)
            if deviation > 1e-12
            else None
        )
        drawdown = _max_drawdown(returns)
    else:
        mean_return = mean_excess = hit_rate = sharpe = drawdown = None
    return SelectionBacktestReportV1(
        config_version=config.config_version,
        horizon_sessions=horizon_sessions,
        round_trip_cost_pct=round_trip_cost_pct,
        signal_count=len(trades),
        average_return_pct=mean_return,
        average_excess_return_pct=mean_excess,
        hit_rate=hit_rate,
        sharpe_ratio=sharpe,
        max_drawdown_pct=drawdown,
        trades=tuple(trades),
    )


__all__ = ["walk_forward_backtest"]
