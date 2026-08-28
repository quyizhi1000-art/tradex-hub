"""Deterministic cross-sectional selector with fail-closed eligibility rules."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal
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
    LimitUpTendencyCandidateV1,
    LimitUpTendencyContributionV1,
    LimitUpTendencyScreenV1,
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

    config_version: str = "daily-stock-selection-balanced.v6"
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
LONG_UPPER_SHADOW_SCREEN_VERSION = "long-upper-shadow-main-board.v3"
LONG_UPPER_SHADOW_LOOKBACK = 10
LONG_UPPER_SHADOW_MIN_OCCURRENCES = 2
LONG_UPPER_SHADOW_MIN_PCT_OF_CLOSE = 3.0
LONG_UPPER_SHADOW_MIN_BODY_MULTIPLE = 2.0
LONG_UPPER_SHADOW_MIN_RANGE_RATIO = 0.5
LONG_UPPER_SHADOW_LIMIT_UP_EXCLUSION_LOOKBACK = 10
MAIN_BOARD_LIMIT_UP_MULTIPLIER = Decimal("1.10")
A_SHARE_PRICE_TICK = Decimal("0.01")
LIMIT_UP_TENDENCY_SCREEN_VERSION = "next-session-limit-up-tendency-main-board.v2"
LIMIT_UP_TENDENCY_TARGET_COUNT = 20
LIMIT_UP_TENDENCY_LOOKBACK = 15
LIMIT_UP_TENDENCY_MIN_LISTING_DAYS = 120
LIMIT_UP_TENDENCY_MIN_CLOSE = 3.0
LIMIT_UP_TENDENCY_MIN_AMOUNT_CNY = 50_000_000.0
LIMIT_UP_TENDENCY_MIN_FLOAT_MARKET_CAP_CNY = 2_000_000_000.0
LIMIT_UP_TENDENCY_MIN_CLOSE_POSITION = 0.55
LIMIT_UP_TENDENCY_WEIGHTS = {
    "daily_return": 0.14,
    "close_position": 0.12,
    "breakout_pressure": 0.16,
    "industry_breadth": 0.12,
    "amount_expansion": 0.12,
    "volume_ratio": 0.07,
    "turnover_heat": 0.10,
    "five_day_momentum": 0.08,
    "float_cap_elasticity": 0.06,
    "recent_limit_up": 0.03,
}
LIMIT_UP_TENDENCY_LABELS = {
    "daily_return": "当日涨幅",
    "close_position": "收盘强度",
    "breakout_pressure": "15 日突破位置",
    "industry_breadth": "行业上涨广度",
    "amount_expansion": "成交额放大",
    "volume_ratio": "量比",
    "turnover_heat": "换手热度",
    "five_day_momentum": "5 日动量",
    "float_cap_elasticity": "流通市值弹性",
    "recent_limit_up": "近期封板强度",
}


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


def _main_board_limit_price(bar: DailyStockCandlestickBarV1) -> float:
    return float(
        (
            Decimal(str(bar.previous_close)) * MAIN_BOARD_LIMIT_UP_MULTIPLIER
        ).quantize(A_SHARE_PRICE_TICK, rounding=ROUND_HALF_UP)
    )


def _closed_at_main_board_limit_up(bar: DailyStockCandlestickBarV1) -> bool:
    limit_price = _main_board_limit_price(bar)
    return math.isclose(bar.close, float(limit_price), rel_tol=0.0, abs_tol=1e-9)


def screen_long_upper_shadow_trials(
    snapshot: DailyStockFactorSnapshotV1,
) -> StockPatternScreenV1:
    """Screen a fully evidenced 10-session suspected trial pattern."""

    histories = {
        item.instrument_id: item for item in snapshot.candlestick_histories
    }
    required_dates = tuple(
        snapshot.candlestick_window_trade_dates[-LONG_UPPER_SHADOW_LOOKBACK:]
    )
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
        bars_by_date = (
            {item.trade_date: item for item in history.bars}
            if history is not None
            else {}
        )
        bars = tuple(
            bars_by_date[trade_date]
            for trade_date in required_dates
            if trade_date in bars_by_date
        )
        if (
            len(required_dates) != LONG_UPPER_SHADOW_LOOKBACK
            or len(bars) != LONG_UPPER_SHADOW_LOOKBACK
            or tuple(item.trade_date for item in bars) != required_dates
        ):
            excluded["incomplete_candlestick_window"] += 1
            continue
        evaluated_count += 1
        if any(_closed_at_main_board_limit_up(bar) for bar in bars):
            excluded["recent_limit_up"] += 1
            continue
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
                primary_business_name=row.primary_business_name,
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
        title="10 日长上影疑似试盘形态",
        screen_version=LONG_UPPER_SHADOW_SCREEN_VERSION,
        limit_up_exclusion_lookback_sessions=(
            LONG_UPPER_SHADOW_LIMIT_UP_EXCLUSION_LOOKBACK
        ),
        quality=quality,
        universe_count=len(snapshot.factors),
        board_eligible_count=board_eligible_count,
        evaluated_count=evaluated_count,
        matched_count=len(candidates),
        excluded_counts=dict(sorted(excluded.items())),
        candidates=tuple(candidates),
        methodology=(
            "观察窗口为信号日及此前 9 个已完成交易日，共 10 个交易日。",
            "单日上影长度须不低于收盘价 3%、不低于实体 2 倍，并占当日最高最低振幅至少 50%。",
            "10 日内至少命中 2 次长上影疑似试盘形态；仅保留规范化市场为主板且名称不含 ST 或退市标识的股票。",
            "同一个 10 日窗口内均不得收盘封涨停；主板涨停价按昨收的 110% 计算并四舍五入到 0.01 元。",
            "任一交易日 OHLC 或成交额证据缺失时，该股票不进入筛选结果。",
        ),
        limitations=(
            "长上影线只作为疑似试盘形态证据，不能据此断言任何资金主体的真实意图。",
            "近 10 日涨停排除只认收盘封板；盘中触及涨停但收盘未封板不会被排除。",
            "结果是收盘后历史形态筛选，不构成买入建议或收益预测。",
        ),
    )


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _turnover_heat_score(turnover_rate_pct: float) -> float:
    if turnover_rate_pct < 3.0:
        return _clip01(turnover_rate_pct / 4.0)
    if turnover_rate_pct <= 15.0:
        return 0.75 + (turnover_rate_pct - 3.0) / 48.0
    if turnover_rate_pct <= 25.0:
        return 1.0 - (turnover_rate_pct - 15.0) * 0.035
    return _clip01(0.65 - (turnover_rate_pct - 25.0) * 0.026)


def _pre_limit_daily_return_score(daily_return_pct: float) -> float:
    """Prefer an advancing setup without treating a near-limit close as free upside."""

    if daily_return_pct <= 0:
        return 0.0
    if daily_return_pct <= 4.0:
        return _clip01(daily_return_pct / 4.0)
    if daily_return_pct <= 7.0:
        return 1.0 - (daily_return_pct - 4.0) * 0.05
    return _clip01(0.85 - (daily_return_pct - 7.0) * 0.20)


def _five_day_momentum_score(five_day_return_pct: float) -> float:
    if five_day_return_pct <= -2.0:
        return 0.0
    if five_day_return_pct <= 12.0:
        return _clip01((five_day_return_pct + 2.0) / 14.0)
    return _clip01(1.0 - (five_day_return_pct - 12.0) / 20.0)


def _limit_up_opportunity_profile(
    *,
    closed_at_limit_up: bool,
    consecutive_limit_up_count: int,
    breakout_distance_pct: float,
    industry_positive_ratio: float | None,
    amount_expansion_ratio: float,
    turnover_rate_pct: float,
    daily_return_pct: float,
    five_day_return_pct: float,
) -> str:
    if closed_at_limit_up:
        board_stage = (
            "首板"
            if consecutive_limit_up_count == 1
            else f"{consecutive_limit_up_count} 连板"
        )
        return f"涨停延续观察（{board_stage}），不是普通的未涨停次日启动机会"
    if (
        breakout_distance_pct >= 0.0
        and industry_positive_ratio is not None
        and industry_positive_ratio >= 0.60
    ):
        return "板块共振突破（信号日未涨停）"
    if breakout_distance_pct >= -1.0 and amount_expansion_ratio >= 1.50:
        return "放量临界突破（信号日未涨停）"
    if (
        1.0 <= daily_return_pct <= 6.0
        and 2.0 <= five_day_return_pct <= 15.0
        and 4.0 <= turnover_rate_pct <= 15.0
    ):
        return "温和加速（信号日未涨停）"
    if industry_positive_ratio is not None and industry_positive_ratio >= 0.65:
        return "板块共振跟随（信号日未涨停）"
    if amount_expansion_ratio >= 1.50:
        return "放量换手（信号日未涨停）"
    return "高位承接观察（信号日未涨停）"


def _limit_up_tendency_reason(
    factor: str,
    *,
    daily_return_pct: float,
    close_position_ratio: float,
    amount_expansion_ratio: float,
    volume_ratio: float,
    turnover_rate_pct: float,
    five_day_return_pct: float,
    float_market_cap_cny: float,
    recent_limit_up_count: int,
    breakout_distance_pct: float,
    industry: str | None,
    industry_positive_ratio: float | None,
    industry_positive_count: int,
    industry_peer_count: int,
) -> str:
    if factor == "daily_return":
        return f"信号日上涨 {daily_return_pct:.2f}%，短线价格强度靠前"
    if factor == "close_position":
        return f"收盘位于当日振幅的 {close_position_ratio * 100:.0f}% 位置，尾盘承接较强"
    if factor == "breakout_pressure":
        relation = "高于" if breakout_distance_pct >= 0 else "低于"
        return (
            f"收盘{relation}此前 14 日最高价 {abs(breakout_distance_pct):.2f}%，"
            "用于识别突破或临界突破位置"
        )
    if factor == "industry_breadth":
        if industry_positive_ratio is None or industry_peer_count == 0:
            return "缺少可比较的同业样本，未把板块共振作为正向依据"
        return (
            f"{industry or '所属'}行业同日上涨 {industry_positive_count}/{industry_peer_count} 只"
            f"（{industry_positive_ratio * 100:.0f}%），板块广度提供同向证据"
        )
    if factor == "amount_expansion":
        return f"成交额为此前 5 日中位数的 {amount_expansion_ratio:.2f} 倍"
    if factor == "volume_ratio":
        return f"量比 {volume_ratio:.2f}，增量交易活跃"
    if factor == "turnover_heat":
        return f"换手率 {turnover_rate_pct:.2f}%，处于规则偏好的活跃区间"
    if factor == "five_day_momentum":
        return f"近 5 日累计涨幅 {five_day_return_pct:.2f}%，短线动量向上"
    if factor == "float_cap_elasticity":
        return f"流通市值约 {float_market_cap_cny / 100_000_000:.1f} 亿元，价格弹性因子占优"
    return f"信号日前 5 日有 {recent_limit_up_count} 次收盘封住主板涨停价"


def screen_next_session_limit_up_tendency(
    snapshot: DailyStockFactorSnapshotV1,
) -> LimitUpTendencyScreenV1:
    """Rank a fully evidenced main-board technical-strength pool for the next session."""

    required_dates = tuple(
        snapshot.candlestick_window_trade_dates[-LIMIT_UP_TENDENCY_LOOKBACK:]
    )
    complete_bars: dict[str, tuple[DailyStockCandlestickBarV1, ...]] = {}
    for history in snapshot.candlestick_histories:
        bars_by_date = {item.trade_date: item for item in history.bars}
        bars = tuple(
            bars_by_date[trade_date]
            for trade_date in required_dates
            if trade_date in bars_by_date
        )
        if (
            len(required_dates) == LIMIT_UP_TENDENCY_LOOKBACK
            and len(bars) == LIMIT_UP_TENDENCY_LOOKBACK
            and tuple(item.trade_date for item in bars) == required_dates
        ):
            complete_bars[history.instrument_id] = bars

    industry_daily_returns: defaultdict[str, list[float]] = defaultdict(list)
    for row in snapshot.factors:
        bars = complete_bars.get(row.instrument_id)
        if (
            bars is None
            or row.market != "主板"
            or _special_treatment_name(row.name)
            or not row.industry
        ):
            continue
        latest = bars[-1]
        industry_daily_returns[row.industry].append(
            latest.close / latest.previous_close * 100.0 - 100.0
        )

    excluded: Counter[str] = Counter()
    board_eligible_count = 0
    evaluated_count = 0
    scored: list[
        tuple[
            float,
            DailyStockFactorV1,
            dict[str, float | int | bool],
            tuple[LimitUpTendencyContributionV1, ...],
        ]
    ] = []

    for row in snapshot.factors:
        if row.market != "主板":
            excluded["not_main_board"] += 1
            continue
        if _special_treatment_name(row.name):
            excluded["special_treatment"] += 1
            continue
        if row.list_date is None:
            excluded["missing_listing_date"] += 1
            continue
        if (row.trade_date - row.list_date).days < LIMIT_UP_TENDENCY_MIN_LISTING_DAYS:
            excluded["recent_listing"] += 1
            continue
        if row.delist_date is not None and row.delist_date <= row.trade_date:
            excluded["delisted"] += 1
            continue
        if row.close < LIMIT_UP_TENDENCY_MIN_CLOSE:
            excluded["low_price"] += 1
            continue
        if row.amount_cny < LIMIT_UP_TENDENCY_MIN_AMOUNT_CNY:
            excluded["low_liquidity"] += 1
            continue
        if row.float_market_cap_cny is None:
            excluded["missing_float_market_cap"] += 1
            continue
        if row.float_market_cap_cny < LIMIT_UP_TENDENCY_MIN_FLOAT_MARKET_CAP_CNY:
            excluded["small_float_market_cap"] += 1
            continue
        board_eligible_count += 1
        if row.turnover_rate_pct is None or row.volume_ratio is None:
            excluded["missing_activity_metrics"] += 1
            continue

        bars = complete_bars.get(row.instrument_id)
        if bars is None:
            excluded["incomplete_candlestick_window"] += 1
            continue

        latest = bars[-1]
        daily_return_pct = latest.close / latest.previous_close * 100.0 - 100.0
        close_position_ratio = (latest.close - latest.low) / (latest.high - latest.low)
        five_day_return_pct = latest.close / bars[-6].close * 100.0 - 100.0
        previous_amount_median = statistics.median(
            item.amount_cny for item in bars[-6:-1]
        )
        amount_expansion_ratio = latest.amount_cny / previous_amount_median
        prior_limit_up_count = sum(
            _closed_at_main_board_limit_up(item) for item in bars[-6:-1]
        )
        closed_at_limit_up = _closed_at_main_board_limit_up(latest)
        opened_at_limit_up = math.isclose(
            latest.open,
            _main_board_limit_price(latest),
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        consecutive_limit_up_count = 0
        for item in reversed(bars):
            if not _closed_at_main_board_limit_up(item):
                break
            consecutive_limit_up_count += 1
        prior_high = max(item.high for item in bars[:-1])
        breakout_distance_pct = latest.close / prior_high * 100.0 - 100.0
        industry_returns = industry_daily_returns.get(row.industry or "", [])
        industry_peer_count = len(industry_returns)
        industry_positive_count = sum(value > 0 for value in industry_returns)
        industry_positive_ratio = (
            industry_positive_count / industry_peer_count
            if industry_peer_count
            else None
        )
        evaluated_count += 1
        if daily_return_pct <= 0:
            excluded["non_positive_session"] += 1
            continue
        if close_position_ratio < LIMIT_UP_TENDENCY_MIN_CLOSE_POSITION:
            excluded["weak_close"] += 1
            continue

        normalized = {
            "daily_return": _pre_limit_daily_return_score(daily_return_pct),
            "close_position": _clip01(
                (close_position_ratio - LIMIT_UP_TENDENCY_MIN_CLOSE_POSITION)
                / (1.0 - LIMIT_UP_TENDENCY_MIN_CLOSE_POSITION)
            ),
            "breakout_pressure": _clip01((breakout_distance_pct + 4.0) / 6.0),
            "industry_breadth": (
                _clip01((industry_positive_ratio - 0.40) / 0.35)
                if industry_positive_ratio is not None
                else 0.0
            ),
            "amount_expansion": _clip01((amount_expansion_ratio - 0.8) / 2.2),
            "volume_ratio": _clip01((row.volume_ratio - 0.8) / 2.2),
            "turnover_heat": _turnover_heat_score(row.turnover_rate_pct),
            "five_day_momentum": _five_day_momentum_score(five_day_return_pct),
            "float_cap_elasticity": _clip01(
                (
                    math.log(80_000_000_000.0)
                    - math.log(row.float_market_cap_cny)
                )
                / (
                    math.log(80_000_000_000.0)
                    - math.log(LIMIT_UP_TENDENCY_MIN_FLOAT_MARKET_CAP_CNY)
                )
            ),
            "recent_limit_up": _clip01(prior_limit_up_count / 2.0) * 0.50,
        }
        raw_values = {
            "daily_return": daily_return_pct,
            "close_position": close_position_ratio,
            "breakout_pressure": breakout_distance_pct,
            "industry_breadth": industry_positive_ratio or 0.0,
            "amount_expansion": amount_expansion_ratio,
            "volume_ratio": row.volume_ratio,
            "turnover_heat": row.turnover_rate_pct,
            "five_day_momentum": five_day_return_pct,
            "float_cap_elasticity": row.float_market_cap_cny,
            "recent_limit_up": float(prior_limit_up_count),
        }
        contributions = tuple(
            LimitUpTendencyContributionV1(
                factor=factor,
                label=LIMIT_UP_TENDENCY_LABELS[factor],
                raw_value=round(raw_values[factor], 6),
                normalized_score=round(normalized[factor], 6),
                weighted_points=round(
                    normalized[factor] * LIMIT_UP_TENDENCY_WEIGHTS[factor] * 100.0,
                    6,
                ),
            )
            for factor in LIMIT_UP_TENDENCY_WEIGHTS
        )
        entry_feasibility_factor = 0.85 if closed_at_limit_up else 1.0
        score = (
            sum(item.weighted_points for item in contributions)
            * entry_feasibility_factor
        )
        metrics: dict[str, float | int | bool] = {
            "daily_return_pct": daily_return_pct,
            "five_day_return_pct": five_day_return_pct,
            "close_position_ratio": close_position_ratio,
            "turnover_rate_pct": row.turnover_rate_pct,
            "volume_ratio": row.volume_ratio,
            "amount_expansion_ratio": amount_expansion_ratio,
            "recent_limit_up_count": prior_limit_up_count,
            "closed_at_limit_up": closed_at_limit_up,
            "opened_at_limit_up": opened_at_limit_up,
            "consecutive_limit_up_count": consecutive_limit_up_count,
            "breakout_distance_pct": breakout_distance_pct,
            "industry_positive_ratio": (
                industry_positive_ratio if industry_positive_ratio is not None else -1.0
            ),
            "industry_positive_count": industry_positive_count,
            "industry_peer_count": industry_peer_count,
            "entry_feasibility_factor": entry_feasibility_factor,
        }
        scored.append((score, row, metrics, contributions))

    scored.sort(key=lambda item: (-item[0], item[1].instrument_id))
    candidates: list[LimitUpTendencyCandidateV1] = []
    for rank, (score, row, metrics, contributions) in enumerate(
        scored[:LIMIT_UP_TENDENCY_TARGET_COUNT], start=1
    ):
        ordered_factors = [
            item.factor
            for item in sorted(
                contributions,
                key=lambda item: (-item.weighted_points, item.factor),
            )
        ]
        industry_positive_ratio = (
            None
            if float(metrics["industry_positive_ratio"]) < 0.0
            else float(metrics["industry_positive_ratio"])
        )
        reasons: list[str] = [
            "机会结构："
            + _limit_up_opportunity_profile(
                closed_at_limit_up=bool(metrics["closed_at_limit_up"]),
                consecutive_limit_up_count=int(
                    metrics["consecutive_limit_up_count"]
                ),
                breakout_distance_pct=float(metrics["breakout_distance_pct"]),
                industry_positive_ratio=industry_positive_ratio,
                amount_expansion_ratio=float(metrics["amount_expansion_ratio"]),
                turnover_rate_pct=float(metrics["turnover_rate_pct"]),
                daily_return_pct=float(metrics["daily_return_pct"]),
                five_day_return_pct=float(metrics["five_day_return_pct"]),
            )
        ]
        if bool(metrics["closed_at_limit_up"]):
            reasons.append(
                "信号日收盘封板；"
                f"换手率 {float(metrics['turnover_rate_pct']):.2f}%、"
                f"量比 {float(metrics['volume_ratio']):.2f}、"
                f"成交额为此前 5 日中位数的 {float(metrics['amount_expansion_ratio']):.2f} 倍"
            )
            reasons.append(
                _limit_up_tendency_reason(
                    "breakout_pressure",
                    daily_return_pct=float(metrics["daily_return_pct"]),
                    close_position_ratio=float(metrics["close_position_ratio"]),
                    amount_expansion_ratio=float(metrics["amount_expansion_ratio"]),
                    volume_ratio=float(metrics["volume_ratio"]),
                    turnover_rate_pct=float(metrics["turnover_rate_pct"]),
                    five_day_return_pct=float(metrics["five_day_return_pct"]),
                    float_market_cap_cny=float(row.float_market_cap_cny),
                    recent_limit_up_count=int(metrics["recent_limit_up_count"]),
                    breakout_distance_pct=float(metrics["breakout_distance_pct"]),
                    industry=row.industry,
                    industry_positive_ratio=industry_positive_ratio,
                    industry_positive_count=int(metrics["industry_positive_count"]),
                    industry_peer_count=int(metrics["industry_peer_count"]),
                )
            )
            ordered_factors = [
                factor
                for factor in ordered_factors
                if factor not in {"breakout_pressure", "recent_limit_up"}
            ]
        for factor in ordered_factors:
            reasons.append(
                _limit_up_tendency_reason(
                    factor,
                    daily_return_pct=float(metrics["daily_return_pct"]),
                    close_position_ratio=float(metrics["close_position_ratio"]),
                    amount_expansion_ratio=float(metrics["amount_expansion_ratio"]),
                    volume_ratio=float(metrics["volume_ratio"]),
                    turnover_rate_pct=float(metrics["turnover_rate_pct"]),
                    five_day_return_pct=float(metrics["five_day_return_pct"]),
                    float_market_cap_cny=float(row.float_market_cap_cny),
                    recent_limit_up_count=int(metrics["recent_limit_up_count"]),
                    breakout_distance_pct=float(metrics["breakout_distance_pct"]),
                    industry=row.industry,
                    industry_positive_ratio=industry_positive_ratio,
                    industry_positive_count=int(metrics["industry_positive_count"]),
                    industry_peer_count=int(metrics["industry_peer_count"]),
                )
            )
            if len(reasons) >= 4:
                break

        specific_risks: list[str] = []
        if bool(metrics["closed_at_limit_up"]):
            specific_risks.extend(
                [
                    "若下一交易日打板成交，按 T+1 最早只能再下一交易日卖出，收益要到第三个交易日才可兑现",
                    "当前档案没有首次封板时间、炸板次数、封单金额与题材事件，不能据此直接形成打板结论",
                ]
            )
            if bool(metrics["opened_at_limit_up"]):
                specific_risks.append(
                    "信号日开盘即触及涨停价，日线无法证明下一交易日存在可成交窗口"
                )
            if row.turnover_rate_pct < 1.0:
                specific_risks.append(
                    f"封板但换手率仅 {row.turnover_rate_pct:.2f}%，次日可能难以按可见价格成交"
                )
        if int(metrics["consecutive_limit_up_count"]) >= 2:
            specific_risks.append(
                f"已经连续 {int(metrics['consecutive_limit_up_count'])} 日收盘封板，延续与分歧风险同时升高"
            )
        if float(metrics["five_day_return_pct"]) > 25.0:
            specific_risks.append(
                f"近 5 日已上涨 {float(metrics['five_day_return_pct']):.2f}%，追高回撤风险较高"
            )
        if row.turnover_rate_pct > 20.0:
            specific_risks.append(
                f"换手率 {row.turnover_rate_pct:.2f}% 偏高，筹码分歧可能放大"
            )
        if row.volume_ratio > 4.0 or float(metrics["amount_expansion_ratio"]) > 4.0:
            specific_risks.append("量能处于规则高位，次日可能出现放量分歧")
        if float(metrics["close_position_ratio"]) < 0.75:
            specific_risks.append("收盘未处于日内高位区，尾盘强度有限")
        if industry_positive_ratio is not None and industry_positive_ratio < 0.45:
            specific_risks.append(
                f"所属行业同日上涨占比仅 {industry_positive_ratio * 100:.0f}%，板块共振偏弱"
            )
        risks = tuple(
            specific_risks[:3]
            + ["规则未纳入公告、题材持续性、封单结构与隔夜消息"]
        )
        candidates.append(
            LimitUpTendencyCandidateV1(
                rank=rank,
                instrument_id=row.instrument_id,
                name=row.name,
                industry=row.industry,
                primary_business_name=row.primary_business_name,
                market="主板",
                score=round(score, 4),
                reference_close=row.close,
                daily_return_pct=round(float(metrics["daily_return_pct"]), 4),
                five_day_return_pct=round(float(metrics["five_day_return_pct"]), 4),
                close_position_ratio=round(
                    float(metrics["close_position_ratio"]), 6
                ),
                turnover_rate_pct=row.turnover_rate_pct,
                volume_ratio=row.volume_ratio,
                amount_cny=row.amount_cny,
                amount_expansion_ratio=round(
                    float(metrics["amount_expansion_ratio"]), 6
                ),
                float_market_cap_cny=row.float_market_cap_cny,
                recent_limit_up_count=int(metrics["recent_limit_up_count"]),
                closed_at_limit_up=bool(metrics["closed_at_limit_up"]),
                opportunity_stage=(
                    "limit_up_continuation"
                    if bool(metrics["closed_at_limit_up"])
                    else "pre_limit_up"
                ),
                breakout_distance_pct=round(
                    float(metrics["breakout_distance_pct"]), 4
                ),
                industry_positive_ratio=(
                    round(industry_positive_ratio, 6)
                    if industry_positive_ratio is not None
                    else None
                ),
                industry_peer_count=int(metrics["industry_peer_count"]),
                consecutive_limit_up_count=int(
                    metrics["consecutive_limit_up_count"]
                ),
                opened_at_limit_up=bool(metrics["opened_at_limit_up"]),
                entry_feasibility_factor=float(
                    metrics["entry_feasibility_factor"]
                ),
                contributions=contributions,
                reasons=tuple(reasons),
                risks=risks,
            )
        )

    coverage = (
        evaluated_count / board_eligible_count if board_eligible_count else 0.0
    )
    quality = (
        "unavailable"
        if len(required_dates) != LIMIT_UP_TENDENCY_LOOKBACK or evaluated_count == 0
        else "accepted"
        if coverage >= 0.90
        else "degraded"
    )
    return LimitUpTendencyScreenV1(
        title="次日涨停机会 20 强",
        quality=quality,
        universe_count=len(snapshot.factors),
        board_eligible_count=board_eligible_count,
        evaluated_count=evaluated_count,
        selected_count=len(candidates),
        excluded_counts=dict(sorted(excluded.items())),
        candidates=tuple(candidates),
        methodology=(
            "仅保留主板、非 ST/退市、上市满 120 日、收盘价不低于 3 元、成交额不低于 5000 万元且流通市值不低于 20 亿元的股票。",
            "要求信号日及此前 14 个交易日的 OHLC、昨收和成交额完整，并具备换手率、量比与流通市值；缺失时直接排除，不做填补。",
            "候选须在信号日上涨且收盘位于日内振幅 55% 以上；排序同时消费当日强度、15 日突破位置、行业上涨广度、量价放大、换手区间、5 日动量、流通市值弹性和信号日前封板历史。",
            "信号日未涨停的启动机会与已经涨停的延续观察分开标识；后者不再因当日封板直接加分，并对次日成交及 T+1 持有期风险施加 15% 可执行性折减。",
            "普通主板收盘涨停按昨收乘以 110% 并四舍五入到 0.01 元识别；最终取折减后分数最高的 20 只，同分按规范化证券代码排序。",
        ),
        limitations=(
            "分数只表示同一交易日、同一证据集下的相对机会强弱，不代表可校准涨停概率或收益承诺。",
            "当前档案未消费首次封板时间、炸板次数、封单金额、公告新闻、题材持续性、龙虎榜和隔夜事件；缺少这些证据时不能据此形成打板结论。",
            "若下一交易日打板成交，按 T+1 最早只能再下一交易日卖出，真实收益要到第三个交易日才可兑现；一字板、跳空和快速回落还会造成成交偏差。",
            "结果是收盘后研究候选，不构成买入建议。",
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
                primary_business_name=row.primary_business_name,
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
    limit_up_tendency_screen = screen_next_session_limit_up_tendency(snapshot)
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
        "limit_up_tendency_screens": [
            {
                "screen_version": limit_up_tendency_screen.screen_version,
                "quality": limit_up_tendency_screen.quality,
                "candidates": [
                    [item.instrument_id, item.score]
                    for item in limit_up_tendency_screen.candidates
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
        limit_up_tendency_screens=(limit_up_tendency_screen,),
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
    "screen_next_session_limit_up_tendency",
    "select_daily_stocks",
]
