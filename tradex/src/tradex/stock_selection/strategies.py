"""Independent strategy registry, result identities, and outcome evaluation."""

from __future__ import annotations

import hashlib
import json
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TypeAlias

from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1

from .contracts import (
    BalancedStockSelectionResultV1,
    DailyStockSelectionV1,
    LimitUpTendencyScreenV1,
    StockPatternScreenV1,
    StockSelectionStrategyCatalogV1,
    StockSelectionStrategyDefinitionV1,
    StockSelectionStrategyOutcomeV1,
    StockSelectionStrategyResultV1,
    VolumeSurgeScreenV1,
    MacdJScreenV1,
)
from .volume_surge import screen_volume_surge
from .macd_j import screen_macd_j


ROUND_TRIP_COST_PCT = 0.15
_PRICE_TICK = Decimal("0.01")
_LIMIT_MULTIPLIER = Decimal("1.10")
StrategyPayload: TypeAlias = (
    BalancedStockSelectionResultV1
    | StockPatternScreenV1
    | LimitUpTendencyScreenV1
    | VolumeSurgeScreenV1
    | MacdJScreenV1
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def snapshot_revision(snapshot: DailyStockFactorSnapshotV1) -> str:
    """Hash the complete accepted point-in-time evidence used by every strategy."""

    # Optional new strategy evidence must not change older strategy identities.
    return _digest(snapshot.model_dump(mode="json", exclude={"technicals"}))


@dataclass(frozen=True, slots=True)
class RegisteredStockSelectionStrategy:
    strategy_id: str
    strategy_version: str
    title: str
    result_contract: str
    evaluation_policy: str
    display_order: int
    execute: Callable[
        [DailyStockFactorSnapshotV1, DailyStockSelectionV1],
        StrategyPayload,
    ]
    requires_legacy_selection: bool = True

    def definition(self) -> StockSelectionStrategyDefinitionV1:
        return StockSelectionStrategyDefinitionV1(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            title=self.title,
            result_contract=self.result_contract,
            evaluation_policy=self.evaluation_policy,
            display_order=self.display_order,
        )


def _balanced_payload(
    _snapshot: DailyStockFactorSnapshotV1,
    selection: DailyStockSelectionV1,
) -> BalancedStockSelectionResultV1:
    return BalancedStockSelectionResultV1(
        universe_count=selection.universe_count,
        eligible_count=selection.eligible_count,
        selected_count=selection.selected_count,
        excluded_counts=selection.excluded_counts,
        candidates=selection.candidates,
        methodology=selection.methodology,
        limitations=selection.limitations,
    )


def _pattern_payload(
    _snapshot: DailyStockFactorSnapshotV1,
    selection: DailyStockSelectionV1,
) -> StockPatternScreenV1:
    if len(selection.pattern_screens) != 1:
        raise ValueError("legacy selection must contain exactly one pattern strategy")
    return selection.pattern_screens[0]


def _limit_up_payload(
    _snapshot: DailyStockFactorSnapshotV1,
    selection: DailyStockSelectionV1,
) -> LimitUpTendencyScreenV1:
    if len(selection.limit_up_tendency_screens) != 1:
        raise ValueError("legacy selection must contain exactly one limit-up strategy")
    return selection.limit_up_tendency_screens[0]


REGISTERED_STOCK_SELECTION_STRATEGIES = (
    RegisteredStockSelectionStrategy(
        strategy_id="balanced-multifactor-a-share",
        strategy_version="v1",
        title="每日量化候选池",
        result_contract="balanced_stock_selection_result.v1",
        evaluation_policy="next_session_open_to_close_excess_return",
        display_order=10,
        execute=_balanced_payload,
    ),
    RegisteredStockSelectionStrategy(
        strategy_id="next-session-limit-up-tendency-main-board",
        strategy_version="v2",
        title="次日涨停机会 20 强",
        result_contract="stock_limit_up_tendency_screen.v1",
        evaluation_policy="next_session_limit_up",
        display_order=20,
        execute=_limit_up_payload,
    ),
    RegisteredStockSelectionStrategy(
        strategy_id="long-upper-shadow-main-board",
        strategy_version="v3",
        title="长上影疑似试盘形态",
        result_contract="stock_pattern_screen.v1",
        evaluation_policy="not_defined",
        display_order=30,
        execute=_pattern_payload,
    ),
    RegisteredStockSelectionStrategy(
        strategy_id="upward-volume-surge-main-board",
        strategy_version="v3",
        title="7 日向上放量",
        result_contract="stock_volume_surge_screen.v1",
        evaluation_policy="not_defined",
        display_order=40,
        execute=lambda snapshot, _selection: screen_volume_surge(snapshot),
        requires_legacy_selection=False,
    ),
    RegisteredStockSelectionStrategy(
        strategy_id="macd-j-upturn-main-board",
        strategy_version="v5",
        title="MACD 金叉 + J 线拐头",
        result_contract="stock_macd_j_screen.v1",
        evaluation_policy="not_defined",
        display_order=50,
        execute=lambda snapshot, _selection: screen_macd_j(snapshot),
        requires_legacy_selection=False,
    ),
)


def strategy_catalog(
    strategies: Sequence[RegisteredStockSelectionStrategy] = (
        REGISTERED_STOCK_SELECTION_STRATEGIES
    ),
) -> StockSelectionStrategyCatalogV1:
    definitions = tuple(
        sorted(
            (item.definition() for item in strategies),
            key=lambda item: (item.display_order, item.strategy_id),
        )
    )
    return StockSelectionStrategyCatalogV1(strategies=definitions)


def build_strategy_results(
    snapshot: DailyStockFactorSnapshotV1,
    selection: DailyStockSelectionV1,
    *,
    strategies: Sequence[RegisteredStockSelectionStrategy] = (
        REGISTERED_STOCK_SELECTION_STRATEGIES
    ),
) -> tuple[StockSelectionStrategyResultV1, ...]:
    if snapshot.trade_date != selection.trade_date:
        raise ValueError("strategy snapshot and legacy selection dates must match")
    revision = snapshot_revision(snapshot)
    results = []
    for strategy in sorted(
        strategies,
        key=lambda item: (item.display_order, item.strategy_id),
    ):
        definition = strategy.definition()
        result_revision = (
            _digest(snapshot.model_dump(mode="json"))
            if definition.result_contract == "stock_macd_j_screen.v1" and snapshot.technicals is not None else revision
        )
        payload = strategy.execute(snapshot, selection)
        payload_json = payload.model_dump(mode="json")
        if payload.contract != definition.result_contract:
            raise ValueError("registered strategy result contract does not match payload")
        quality = getattr(payload, "quality", selection.source_quality)
        identity = {
            "strategy_id": definition.strategy_id,
            "strategy_version": definition.strategy_version,
            "trade_date": selection.trade_date.isoformat(),
            "source_snapshot_revision": result_revision,
            "payload": payload_json,
        }
        results.append(
            StockSelectionStrategyResultV1(
                result_id=(
                    "stock-selection-strategy-result:"
                    f"{_digest(identity)[:24]}"
                ),
                strategy_id=definition.strategy_id,
                strategy_version=definition.strategy_version,
                title=definition.title,
                result_contract=definition.result_contract,
                trade_date=selection.trade_date,
                generated_at=selection.generated_at,
                source_contract=selection.source_contract,
                source_snapshot_revision=result_revision,
                source_quality=selection.source_quality,
                source_provider_as_of=selection.source_provider_as_of,
                quality=quality,
                payload=payload,
            )
        )
    return tuple(results)


def _outcome_id(payload: dict[str, object]) -> str:
    return f"stock-selection-strategy-outcome:{_digest(payload)[:24]}"


def _main_board_limit_price(previous_close: float) -> float:
    return float(
        (Decimal(str(previous_close)) * _LIMIT_MULTIPLIER).quantize(
            _PRICE_TICK,
            rounding=ROUND_HALF_UP,
        )
    )


def _evaluate_balanced_result(
    result: StockSelectionStrategyResultV1,
    snapshot: DailyStockFactorSnapshotV1,
) -> StockSelectionStrategyOutcomeV1:
    payload = BalancedStockSelectionResultV1.model_validate(result.payload)
    rows = {item.instrument_id: item for item in snapshot.factors}
    returns = []
    for candidate in payload.candidates:
        row = rows.get(candidate.instrument_id)
        if row is None or row.open <= 0:
            continue
        returns.append(
            (row.close / row.open - 1.0) * 100.0 - ROUND_TRIP_COST_PCT
        )
    coverage = len(returns) / payload.selected_count if payload.selected_count else 0.0
    evaluated = coverage >= 0.70 and bool(returns)
    portfolio = benchmark = excess = None
    verdict = "unverifiable"
    if evaluated:
        portfolio = statistics.mean(returns)
        benchmark = (
            snapshot.benchmark_close / snapshot.benchmark_open - 1.0
        ) * 100.0
        excess = portfolio - benchmark
        verdict = (
            "supported"
            if excess > 0.30
            else "not_supported"
            if excess < -0.30
            else "mixed"
        )
    identity = {
        "result_id": result.result_id,
        "evaluation_trade_date": snapshot.trade_date.isoformat(),
        "evaluated_count": len(returns),
        "portfolio_return_pct": portfolio,
        "benchmark_return_pct": benchmark,
        "excess_return_pct": excess,
    }
    return StockSelectionStrategyOutcomeV1(
        outcome_id=_outcome_id(identity),
        result_id=result.result_id,
        strategy_id=result.strategy_id,
        strategy_version=result.strategy_version,
        signal_trade_date=result.trade_date,
        evaluation_trade_date=snapshot.trade_date,
        evaluation_policy="next_session_open_to_close_excess_return",
        evaluation_status="evaluated" if evaluated else "unverifiable",
        evaluated_count=len(returns),
        selected_count=payload.selected_count,
        coverage=coverage,
        portfolio_return_pct=portfolio,
        benchmark_return_pct=benchmark,
        excess_return_pct=excess,
        verdict=verdict,
        limitations=("收益按下一交易日开盘至收盘并扣除 0.15% 双边成本。",),
    )


def _evaluate_limit_up_result(
    result: StockSelectionStrategyResultV1,
    snapshot: DailyStockFactorSnapshotV1,
) -> StockSelectionStrategyOutcomeV1:
    payload = LimitUpTendencyScreenV1.model_validate(result.payload)
    histories = {
        item.instrument_id: item for item in snapshot.candlestick_histories
    }
    evaluated_count = touched_count = closed_count = 0
    for candidate in payload.candidates:
        history = histories.get(candidate.instrument_id)
        if history is None or not history.bars:
            continue
        bar = history.bars[-1]
        if bar.trade_date != snapshot.trade_date or bar.previous_close <= 0:
            continue
        evaluated_count += 1
        limit_price = _main_board_limit_price(bar.previous_close)
        touched_count += int(bar.high + 1e-9 >= limit_price)
        closed_count += int(bar.close + 1e-9 >= limit_price)
    coverage = evaluated_count / payload.selected_count if payload.selected_count else 0.0
    evaluated = coverage >= 0.70 and evaluated_count > 0
    touched_rate = touched_count / evaluated_count if evaluated else None
    closed_rate = closed_count / evaluated_count if evaluated else None
    identity = {
        "result_id": result.result_id,
        "evaluation_trade_date": snapshot.trade_date.isoformat(),
        "evaluated_count": evaluated_count,
        "touched_limit_up_count": touched_count,
        "closed_limit_up_count": closed_count,
        "touched_limit_up_rate": touched_rate,
        "closed_limit_up_rate": closed_rate,
    }
    return StockSelectionStrategyOutcomeV1(
        outcome_id=_outcome_id(identity),
        result_id=result.result_id,
        strategy_id=result.strategy_id,
        strategy_version=result.strategy_version,
        signal_trade_date=result.trade_date,
        evaluation_trade_date=snapshot.trade_date,
        evaluation_policy="next_session_limit_up",
        evaluation_status="evaluated" if evaluated else "unverifiable",
        evaluated_count=evaluated_count,
        selected_count=payload.selected_count,
        coverage=coverage,
        touched_limit_up_count=touched_count if evaluated else None,
        closed_limit_up_count=closed_count if evaluated else None,
        touched_limit_up_rate=touched_rate,
        closed_limit_up_rate=closed_rate,
        limitations=(
            "触板和封板只按下一有效交易日的主板 10% 价格规则观察，不代表可成交收益。",
        ),
    )


def evaluate_strategy_result(
    result: StockSelectionStrategyResultV1,
    snapshot: DailyStockFactorSnapshotV1,
) -> StockSelectionStrategyOutcomeV1:
    if snapshot.trade_date <= result.trade_date:
        raise ValueError("strategy outcomes require a later evaluation date")
    definition = next(
        (
            item.definition()
            for item in REGISTERED_STOCK_SELECTION_STRATEGIES
            if item.strategy_id == result.strategy_id
            and item.strategy_version == result.strategy_version
        ),
        None,
    )
    if definition is None:
        raise ValueError("strategy result is not registered")
    if definition.evaluation_policy == "not_defined":
        raise ValueError("strategy does not define an outcome policy")
    if definition.evaluation_policy == "next_session_limit_up":
        return _evaluate_limit_up_result(result, snapshot)
    return _evaluate_balanced_result(result, snapshot)


__all__ = [
    "REGISTERED_STOCK_SELECTION_STRATEGIES",
    "RegisteredStockSelectionStrategy",
    "build_strategy_results",
    "evaluate_strategy_result",
    "snapshot_revision",
    "strategy_catalog",
]
