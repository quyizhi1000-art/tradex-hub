"""Deterministic cross-sectional selector with fail-closed eligibility rules."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradex.data_gateway.stock_selection_contracts import (
    DailyStockCandlestickBarV1,
    DailyStockFactorSnapshotV1,
    DailyStockFactorV1,
)

from .contracts import (
    DailyStockSelectionV1,
    FactorContributionV1,
    SelectionCandidateV1,
    StockPatternCandidateV1,
    StockPatternEvidenceV1,
    StockPatternScreenV1,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
FACTOR_LABELS = {
    "value_pe": "估值 PE",
    "value_pb": "估值 PB",
    "dividend": "股息率",
    "quality_roe": "ROE",
    "quality_margin": "毛利率",
    "quality_debt": "资产负债率",
    "growth_revenue": "营收增长",
    "growth_profit": "利润增长",
    "momentum_20d": "20 日动量",
    "momentum_60d": "60 日动量",
}


class SelectionConfigV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    config_version: str = "daily-stock-selection-balanced.v2"
    top_n: int = Field(default=20, ge=1, le=100)
    min_listing_days: int = Field(default=120, ge=0)
    min_close: float = Field(default=3.0, gt=0)
    min_amount_cny: float = Field(default=50_000_000.0, ge=0)
    min_market_cap_cny: float = Field(default=2_000_000_000.0, ge=0)
    min_factor_count: int = Field(default=6, ge=1, le=10)
    industry_group_minimum: int = Field(default=8, ge=3)
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "value_pe": 0.11,
            "value_pb": 0.09,
            "dividend": 0.06,
            "quality_roe": 0.14,
            "quality_margin": 0.08,
            "quality_debt": 0.10,
            "growth_revenue": 0.11,
            "growth_profit": 0.11,
            "momentum_20d": 0.10,
            "momentum_60d": 0.10,
        }
    )

    @model_validator(mode="after")
    def validate_weights(self) -> "SelectionConfigV1":
        if set(self.weights) != set(FACTOR_LABELS):
            raise ValueError("selection weights must cover the versioned factor set")
        if any(not math.isfinite(value) or value <= 0 for value in self.weights.values()):
            raise ValueError("selection weights must be finite and positive")
        if not math.isclose(sum(self.weights.values()), 1.0, abs_tol=1e-9):
            raise ValueError("selection weights must sum to one")
        return self


DEFAULT_SELECTION_CONFIG = SelectionConfigV1()
LONG_UPPER_SHADOW_SCREEN_VERSION = "long-upper-shadow-main-board.v1"
LONG_UPPER_SHADOW_LOOKBACK = 15
LONG_UPPER_SHADOW_MIN_OCCURRENCES = 2
LONG_UPPER_SHADOW_MIN_PCT_OF_CLOSE = 3.0
LONG_UPPER_SHADOW_MIN_BODY_MULTIPLE = 2.0
LONG_UPPER_SHADOW_MIN_RANGE_RATIO = 0.5


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _factor_values(row: DailyStockFactorV1) -> dict[str, float | None]:
    pe = _finite(row.pe_ttm)
    pb = _finite(row.pb)
    return {
        "value_pe": -math.log(pe) if pe is not None and 0 < pe <= 200 else None,
        "value_pb": -math.log(pb) if pb is not None and 0 < pb <= 30 else None,
        "dividend": math.log1p(row.dividend_yield_pct)
        if row.dividend_yield_pct is not None
        else None,
        "quality_roe": _finite(row.roe_pct),
        "quality_margin": _finite(row.gross_margin_pct),
        "quality_debt": -row.debt_to_assets_pct
        if row.debt_to_assets_pct is not None
        else None,
        "growth_revenue": _finite(row.revenue_yoy_pct),
        "growth_profit": _finite(row.net_profit_yoy_pct),
        "momentum_20d": _finite(row.momentum_20d_pct),
        "momentum_60d": _finite(row.momentum_60d_pct),
    }


def _exclusion_reason(row: DailyStockFactorV1, config: SelectionConfigV1) -> str | None:
    upper_name = row.name.upper()
    if "ST" in upper_name or "退" in row.name:
        return "special_treatment"
    if row.list_date is None:
        return "missing_listing_date"
    if (row.trade_date - row.list_date).days < config.min_listing_days:
        return "recent_listing"
    if row.delist_date is not None and row.delist_date <= row.trade_date:
        return "delisted"
    if row.close < config.min_close:
        return "low_price"
    if row.amount_cny < config.min_amount_cny:
        return "low_liquidity"
    if row.total_market_cap_cny is None:
        return "missing_market_cap"
    if row.total_market_cap_cny < config.min_market_cap_cny:
        return "small_market_cap"
    if sum(value is not None for value in _factor_values(row).values()) < config.min_factor_count:
        return "insufficient_factor_coverage"
    return None


def _special_treatment_name(name: str) -> bool:
    return "ST" in name.upper() or "退" in name


def _long_upper_shadow_evidence(
    bar: DailyStockCandlestickBarV1,
) -> StockPatternEvidenceV1 | None:
    upper_shadow = bar.high - max(bar.open, bar.close)
    body = abs(bar.close - bar.open)
    daily_range = bar.high - bar.low
    pct_of_close = upper_shadow / bar.close * 100.0
    body_multiple = upper_shadow / body if body > 1e-12 else None
    range_ratio = upper_shadow / daily_range
    if pct_of_close < LONG_UPPER_SHADOW_MIN_PCT_OF_CLOSE:
        return None
    if body_multiple is not None and body_multiple < LONG_UPPER_SHADOW_MIN_BODY_MULTIPLE:
        return None
    if range_ratio < LONG_UPPER_SHADOW_MIN_RANGE_RATIO:
        return None
    return StockPatternEvidenceV1(
        trade_date=bar.trade_date,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        upper_shadow_pct_of_close=round(pct_of_close, 4),
        upper_shadow_body_multiple=(
            round(body_multiple, 4) if body_multiple is not None else None
        ),
        upper_shadow_range_ratio=round(range_ratio, 6),
    )


def screen_long_upper_shadow_trials(
    snapshot: DailyStockFactorSnapshotV1,
) -> StockPatternScreenV1:
    """Screen a fully evidenced 15-session long-upper-shadow proxy."""

    histories = {
        item.instrument_id: item for item in snapshot.candlestick_histories
    }
    required_dates = tuple(snapshot.candlestick_window_trade_dates)
    excluded: Counter[str] = Counter()
    candidates: list[StockPatternCandidateV1] = []
    board_eligible_count = 0
    evaluated_count = 0
    for row in snapshot.factors:
        if row.market != "主板":
            excluded["not_main_board"] += 1
            continue
        if _special_treatment_name(row.name):
            excluded["special_treatment"] += 1
            continue
        board_eligible_count += 1
        history = histories.get(row.instrument_id)
        bars = tuple(history.bars) if history is not None else ()
        if (
            len(required_dates) != LONG_UPPER_SHADOW_LOOKBACK
            or len(bars) != LONG_UPPER_SHADOW_LOOKBACK
            or tuple(item.trade_date for item in bars) != required_dates
        ):
            excluded["incomplete_candlestick_window"] += 1
            continue
        evaluated_count += 1
        evidence = tuple(
            item
            for bar in bars
            if (item := _long_upper_shadow_evidence(bar)) is not None
        )
        if len(evidence) < LONG_UPPER_SHADOW_MIN_OCCURRENCES:
            excluded["insufficient_occurrences"] += 1
            continue
        candidates.append(
            StockPatternCandidateV1(
                instrument_id=row.instrument_id,
                name=row.name,
                industry=row.industry,
                market="主板",
                reference_close=row.close,
                occurrence_count=len(evidence),
                latest_occurrence_date=evidence[-1].trade_date,
                evidence=evidence,
            )
        )
    candidates.sort(
        key=lambda item: (
            -item.occurrence_count,
            -item.latest_occurrence_date.toordinal(),
            item.instrument_id,
        )
    )
    coverage = (
        evaluated_count / board_eligible_count if board_eligible_count else 0.0
    )
    quality = (
        "unavailable"
        if len(required_dates) != LONG_UPPER_SHADOW_LOOKBACK or evaluated_count == 0
        else "accepted"
        if coverage >= 0.90
        else "degraded"
    )
    return StockPatternScreenV1(
        title="15 日长上影试盘形态",
        quality=quality,
        universe_count=len(snapshot.factors),
        board_eligible_count=board_eligible_count,
        evaluated_count=evaluated_count,
        matched_count=len(candidates),
        excluded_counts=dict(sorted(excluded.items())),
        candidates=tuple(candidates),
        methodology=(
            "观察窗口为信号日及此前 14 个已完成交易日，共 15 个交易日。",
            "单日上影长度须不低于收盘价 3%、不低于实体 2 倍，并占当日最高最低振幅至少 50%。",
            "15 日内至少命中 2 次；仅保留规范化市场为主板且名称不含 ST 或退市标识的股票。",
            "任一交易日 OHLC 或成交额证据缺失时，该股票不进入筛选结果。",
        ),
        limitations=(
            "长上影线只作为试盘形态代理，不能据此断言主力资金的真实意图。",
            "结果是收盘后历史形态筛选，不构成买入建议或收益预测。",
        ),
    )


def _robust_z(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    raw = list(values.values())
    median = statistics.median(raw)
    mad = statistics.median(abs(value - median) for value in raw)
    if mad > 0:
        lower, upper = median - 5 * mad, median + 5 * mad
        clipped = {key: min(upper, max(lower, value)) for key, value in values.items()}
        center = statistics.mean(clipped.values())
        scale = statistics.pstdev(clipped.values())
    else:
        clipped = values
        center = statistics.mean(raw)
        scale = statistics.pstdev(raw)
    if scale <= 1e-12:
        return {key: 0.0 for key in values}
    return {key: (value - center) / scale for key, value in clipped.items()}


def _factor_zscores(
    rows: list[DailyStockFactorV1],
    values: dict[str, dict[str, float | None]],
    config: SelectionConfigV1,
) -> dict[str, dict[str, float]]:
    result = {row.instrument_id: {} for row in rows}
    for factor in FACTOR_LABELS:
        global_values = {
            row.instrument_id: value
            for row in rows
            if (value := values[row.instrument_id][factor]) is not None
        }
        global_z = _robust_z(global_values)
        industries: dict[str, dict[str, float]] = defaultdict(dict)
        for row in rows:
            value = values[row.instrument_id][factor]
            if value is not None and row.industry:
                industries[row.industry][row.instrument_id] = value
        industry_z = {
            industry: _robust_z(group)
            for industry, group in industries.items()
            if len(group) >= config.industry_group_minimum
        }
        for row in rows:
            if values[row.instrument_id][factor] is None:
                continue
            result[row.instrument_id][factor] = industry_z.get(row.industry or "", global_z).get(
                row.instrument_id,
                global_z.get(row.instrument_id, 0.0),
            )
    return result


def _selection_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"daily-stock-selection:{hashlib.sha256(encoded).hexdigest()[:24]}"


def select_daily_stocks(
    snapshot: DailyStockFactorSnapshotV1,
    *,
    config: SelectionConfigV1 = DEFAULT_SELECTION_CONFIG,
    generated_at: datetime | None = None,
) -> DailyStockSelectionV1:
    generated = generated_at or datetime.now(SHANGHAI)
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ValueError("selection generated_at must include a timezone")
    generated = generated.astimezone(SHANGHAI)

    excluded: Counter[str] = Counter()
    eligible: list[DailyStockFactorV1] = []
    for row in snapshot.factors:
        reason = _exclusion_reason(row, config)
        if reason is None:
            eligible.append(row)
        else:
            excluded[reason] += 1
    factor_values = {row.instrument_id: _factor_values(row) for row in eligible}
    zscores = _factor_zscores(eligible, factor_values, config)

    scored: list[tuple[float, DailyStockFactorV1, tuple[FactorContributionV1, ...]]] = []
    for row in eligible:
        contributions: list[FactorContributionV1] = []
        weighted_sum = 0.0
        present = 0
        for factor, weight in config.weights.items():
            raw_value = factor_values[row.instrument_id][factor]
            if raw_value is None:
                continue
            present += 1
            z_score = max(-4.0, min(4.0, zscores[row.instrument_id][factor]))
            weighted = z_score * weight
            weighted_sum += weighted
            contributions.append(
                FactorContributionV1(
                    factor=factor,
                    label=FACTOR_LABELS[factor],
                    raw_value=raw_value,
                    z_score=z_score,
                    weighted_contribution=weighted,
                )
            )
        missing_penalty = (len(config.weights) - present) * 1.25
        score = max(0.0, min(100.0, 50.0 + 12.0 * weighted_sum - missing_penalty))
        scored.append(
            (
                score,
                row,
                tuple(sorted(contributions, key=lambda item: item.factor)),
            )
        )
    scored.sort(key=lambda item: (-item[0], item[1].instrument_id))

    candidates: list[SelectionCandidateV1] = []
    for rank, (score, row, contributions) in enumerate(scored[: config.top_n], start=1):
        positive = sorted(
            (item for item in contributions if item.weighted_contribution > 0),
            key=lambda item: (-item.weighted_contribution, item.factor),
        )
        negative = sorted(
            (item for item in contributions if item.weighted_contribution < 0),
            key=lambda item: (item.weighted_contribution, item.factor),
        )
        present = {item.factor for item in contributions}
        missing = [FACTOR_LABELS[key] for key in FACTOR_LABELS if key not in present]
        reasons = tuple(
            f"{item.label}在行业/全市场横截面相对占优" for item in positive[:3]
        ) or ("综合因子得分进入当日候选池",)
        risks = tuple(
            [
                *(f"{item.label}相对偏弱" for item in negative[:2]),
                *(f"缺少{label}" for label in missing[:2]),
            ][:3]
        ) or ("因子模型不能替代个股基本面核查",)
        candidates.append(
            SelectionCandidateV1(
                rank=rank,
                instrument_id=row.instrument_id,
                name=row.name,
                industry=row.industry,
                score=round(score, 4),
                factor_coverage=len(contributions) / len(config.weights),
                reference_close=row.close,
                amount_cny=row.amount_cny,
                total_market_cap_cny=row.total_market_cap_cny,
                contributions=contributions,
                reasons=reasons,
                risks=risks,
            )
        )

    pattern_screen = screen_long_upper_shadow_trials(snapshot)
    identity_payload = {
        "trade_date": snapshot.trade_date.isoformat(),
        "config_version": config.config_version,
        "candidates": [
            [item.instrument_id, item.score, item.factor_coverage] for item in candidates
        ],
        "pattern_screens": [
            {
                "screen_version": pattern_screen.screen_version,
                "quality": pattern_screen.quality,
                "candidates": [
                    [
                        item.instrument_id,
                        [evidence.trade_date.isoformat() for evidence in item.evidence],
                    ]
                    for item in pattern_screen.candidates
                ],
            }
        ],
    }
    limitations = [
        "候选结果是可复现的量化排序，不构成买入建议。",
        "信号在收盘数据完成后生成，成交与收益验证从下一交易日开盘开始。",
    ]
    if snapshot.metadata.quality.value == "degraded":
        limitations.append("财务指标覆盖不足，结果已标记为降级。")
    return DailyStockSelectionV1(
        config_version=config.config_version,
        selection_id=_selection_id(identity_payload),
        trade_date=snapshot.trade_date,
        generated_at=generated,
        source_contract=snapshot.metadata.contract,
        source_quality=snapshot.metadata.quality.value,
        source_provider_as_of=snapshot.metadata.provider_as_of,
        universe_count=len(snapshot.factors),
        eligible_count=len(eligible),
        selected_count=len(candidates),
        excluded_counts=dict(sorted(excluded.items())),
        candidates=tuple(candidates),
        pattern_screens=(pattern_screen,),
        methodology=(
            "ST/退市、上市不足、低流动性、低市值和因子缺失先做硬性剔除。",
            "连续因子做 MAD 去极值和行业内标准化，行业样本不足时使用全市场。",
            "缺失因子不按零值加分，并按缺失数量扣减综合分。",
            "同分时按规范化证券代码排序，保证同输入得到同输出。",
        ),
        limitations=tuple(limitations),
    )


__all__ = [
    "DEFAULT_SELECTION_CONFIG",
    "SelectionConfigV1",
    "screen_long_upper_shadow_trials",
    "select_daily_stocks",
]
