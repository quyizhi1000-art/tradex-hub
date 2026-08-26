"""Deterministic analysis over a whole-market post-close evidence bundle."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta
from enum import Enum
from statistics import mean
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from tradex.market_calendar import (
    CalendarDayStatus,
    TradingSessionPhase,
    a_share_session,
    calendar_day_status,
)

from .contracts import (
    ConclusionStrength,
    ContractModel,
    FreshnessStatus,
    MarketPhase,
    MarketWatchSnapshotV1,
    SectorFlowObservationTier,
    SectorTag,
    TurnoverDirection,
)
from .review_evidence import (
    DailyMarketReviewEvidenceV1,
    EvidenceStatus,
    SectorReviewItemV1,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
# Policy v3 keeps the immutable evidence contract but accepts a complete,
# same-session 15:00 turnover snapshot recovered after the close as degraded
# evidence.  Older policy rows remain readable and are never overwritten.
REVIEW_CONFIG_VERSION = "post-market-review-policy.v3"


class ReviewTrigger(str, Enum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"


class ReviewQuality(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    ABSTAINED = "abstained"


class OutlookBias(str, Enum):
    CONSTRUCTIVE = "constructive"
    BALANCED = "balanced"
    DEFENSIVE = "defensive"
    UNCERTAIN = "uncertain"


class OpportunityConviction(str, Enum):
    CONFIRMED = "confirmed"
    WATCH = "watch"


class OutcomeVerdict(str, Enum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    NOT_SUPPORTED = "not_supported"
    UNVERIFIABLE = "unverifiable"


class ReviewIndexV1(ContractModel):
    role: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    change_pct: float

    @field_validator("change_pct")
    @classmethod
    def finite_change(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("index change_pct must be finite")
        return value


class MarketRecapV1(ContractModel):
    headline: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    indices: tuple[ReviewIndexV1, ...] = Field(default=(), max_length=4)
    breadth_statement: str = Field(min_length=1)
    universe_statement: str = Field(min_length=1)
    turnover_statement: str = Field(min_length=1)
    intraday_statement: str = Field(min_length=1)
    etf_statement: str = Field(min_length=1)
    rotation_statement: str = Field(min_length=1)
    sentiment_statement: str = Field(min_length=1)
    stock_fund_flow_statement: str = Field(min_length=1)
    dragon_tiger_statement: str = Field(min_length=1)
    supporting_evidence: tuple[str, ...] = ()
    risk_evidence: tuple[str, ...] = ()


class NextDayOutlookV1(ContractModel):
    bias: OutlookBias
    confidence: ConclusionStrength
    thesis: str = Field(min_length=1)
    expected_shape: str = Field(min_length=1)
    confirmation: str = Field(min_length=1)
    invalidation: str = Field(min_length=1)
    risk_control: str = Field(min_length=1)
    supporting_evidence: tuple[str, ...] = ()
    counter_evidence: tuple[str, ...] = ()

    @field_validator("thesis", "expected_shape", "confirmation", "invalidation", "risk_control")
    @classmethod
    def reject_false_precision(cls, value: str) -> str:
        if any(token in value for token in ("概率", "胜率", "目标价", "收益预测", "%概率")):
            raise ValueError("outlook cannot contain probability or return forecasts")
        return value


class OpportunitySectorV1(ContractModel):
    sector_key: str = Field(min_length=1)
    sector_type: Literal["industry", "concept"]
    name: str = Field(min_length=1)
    conviction: OpportunityConviction
    rank_score: int = Field(ge=0)
    tags: tuple[SectorTag, ...] = Field(min_length=1)
    change_pct: float | None = None
    breadth_ratio: float | None = Field(default=None, ge=0, le=1)
    main_net_inflow_cny: float | None = None
    observation_tier: SectorFlowObservationTier | None = None
    leader_instrument_id: str | None = Field(default=None, pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    leader_name: str | None = None
    evidence_chain: tuple[str, ...] = Field(min_length=2)
    rationale: str = Field(min_length=1)
    next_day_confirmation: str = Field(min_length=1)
    invalidation: str = Field(min_length=1)
    risk_note: str = Field(min_length=1)


class ReviewLearningV1(ContractModel):
    evaluated_count: int = Field(default=0, ge=0)
    supported_count: int = Field(default=0, ge=0)
    partial_count: int = Field(default=0, ge=0)
    not_supported_count: int = Field(default=0, ge=0)
    unverifiable_count: int = Field(default=0, ge=0)
    support_ratio: float | None = Field(default=None, ge=0, le=1)
    calibration_note: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_counts(self) -> "ReviewLearningV1":
        total = self.supported_count + self.partial_count + self.not_supported_count + self.unverifiable_count
        if total != self.evaluated_count:
            raise ValueError("learning counts must sum to evaluated_count")
        verifiable = self.evaluated_count - self.unverifiable_count
        if (self.support_ratio is None) != (verifiable == 0):
            raise ValueError("support_ratio must exist exactly when outcomes are verifiable")
        if self.support_ratio is not None:
            expected = (self.supported_count + 0.5 * self.partial_count) / verifiable
            if not math.isclose(self.support_ratio, expected, abs_tol=1e-9):
                raise ValueError("support_ratio does not match outcome counts")
        return self


class PostMarketReviewV1(ContractModel):
    contract: Literal["post_market_review.v1"] = "post_market_review.v1"
    schema_version: Literal[1] = 1
    config_version: str = Field(default=REVIEW_CONFIG_VERSION, min_length=1)
    review_id: str = Field(min_length=1)
    trade_date: date
    generated_at: datetime
    trigger: ReviewTrigger
    quality: ReviewQuality
    source_snapshot_id: str = Field(min_length=1)
    source_snapshot_as_of: datetime
    source_freshness: FreshnessStatus
    evidence: DailyMarketReviewEvidenceV1
    recap: MarketRecapV1
    next_day_outlook: NextDayOutlookV1
    opportunity_sectors: tuple[OpportunitySectorV1, ...] = Field(default=(), max_length=5)
    learning: ReviewLearningV1
    limitations: tuple[str, ...] = ()

    @field_validator("generated_at", "source_snapshot_as_of")
    @classmethod
    def aware_times(cls, value: datetime, info):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_review(self) -> "PostMarketReviewV1":
        if self.evidence.trade_date != self.trade_date:
            raise ValueError("review evidence must belong to trade_date")
        if self.source_snapshot_as_of.astimezone(SHANGHAI).date() != self.trade_date:
            raise ValueError("source snapshot must belong to review trade_date")
        if self.generated_at.astimezone(SHANGHAI).date() != self.trade_date:
            raise ValueError("review must be generated on its Shanghai trade_date")
        if self.quality == ReviewQuality.ABSTAINED:
            if self.next_day_outlook.bias != OutlookBias.UNCERTAIN:
                raise ValueError("an abstained review must use an uncertain outlook")
            if self.next_day_outlook.confidence != ConclusionStrength.ABSTAIN:
                raise ValueError("an abstained review must abstain on confidence")
            if self.opportunity_sectors:
                raise ValueError("an abstained review cannot name opportunity sectors")
        keys = [item.sector_key for item in self.opportunity_sectors]
        if len(keys) != len(set(keys)):
            raise ValueError("opportunity sectors must be unique")
        return self


class ReviewThemeV2(ContractModel):
    role: Literal["当日主线", "轮动支线", "亏钱集中区"]
    name: str = Field(min_length=1)
    analysis: str = Field(min_length=1)
    representatives: tuple[str, ...] = Field(default=(), max_length=5)


class NextDayScenarioV2(ContractModel):
    scenario_id: Literal["base", "repair", "risk"]
    label: str = Field(min_length=1)
    trigger: str = Field(min_length=1)
    interpretation: str = Field(min_length=1)
    response: str = Field(min_length=1)


class PostMarketReviewPresentationV2(ContractModel):
    """Current editorial rendering of one immutable archived evidence bundle."""

    contract: Literal["post_market_review_presentation.v2"] = "post_market_review_presentation.v2"
    schema_version: Literal[2] = 2
    review_id: str = Field(min_length=1)
    day_character: str = Field(min_length=1)
    core_conclusion: str = Field(min_length=1)
    session_story: tuple[str, ...] = Field(min_length=2, max_length=4)
    comparison_statement: str = Field(min_length=1)
    themes: tuple[ReviewThemeV2, ...] = Field(default=(), max_length=3)
    money_making_effect: tuple[str, ...] = Field(min_length=1, max_length=4)
    loss_making_effect: tuple[str, ...] = Field(min_length=1, max_length=4)
    next_day_scenarios: tuple[NextDayScenarioV2, ...] = Field(min_length=3, max_length=3)
    recap: MarketRecapV1
    next_day_outlook: NextDayOutlookV1
    opportunity_sectors: tuple[OpportunitySectorV1, ...] = Field(default=(), max_length=5)


class CellToneV3(str, Enum):
    """Semantic presentation tone; the consumer owns the actual colour palette."""

    RISE = "rise"
    FALL = "fall"
    FLAT = "flat"
    NEUTRAL = "neutral"
    MUTED = "muted"
    WARNING = "warning"


class CellV3(ContractModel):
    value: str = Field(min_length=1)
    tone: CellToneV3 = CellToneV3.NEUTRAL


class TableV3(ContractModel):
    table_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1)
    columns: tuple[str, ...] = Field(min_length=1)
    rows: tuple[tuple[CellV3, ...], ...] = ()
    empty_state: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_shape(self) -> "TableV3":
        if len(self.columns) != len(set(self.columns)):
            raise ValueError("table columns must be unique")
        for index, row in enumerate(self.rows):
            if len(row) != len(self.columns):
                raise ValueError(
                    f"table row {index} has {len(row)} cells but {len(self.columns)} columns"
                )
        if not self.rows and self.empty_state is None:
            raise ValueError("an empty table requires empty_state")
        return self


class SectionV3(ContractModel):
    section_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    paragraphs: tuple[str, ...] = ()
    tables: tuple[TableV3, ...] = Field(min_length=1)
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_tables(self) -> "SectionV3":
        table_ids = [item.table_id for item in self.tables]
        if len(table_ids) != len(set(table_ids)):
            raise ValueError("section table ids must be unique")
        return self


PRESENTATION_V3_REQUIRED_SECTIONS = (
    "overview",
    "reconciliation",
    "sentiment",
    "sectors",
    "stocks",
    "etfs",
    "flows",
    "tomorrow",
    "methodology",
)


class PostMarketReviewPresentationV3(ContractModel):
    """Structured rendering over an immutable ``post_market_review.v1`` row."""

    contract: Literal["post_market_review_presentation.v3"] = "post_market_review_presentation.v3"
    schema_version: Literal[3] = 3
    review_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    deck: str = Field(min_length=1)
    day_character: str = Field(min_length=1)
    core_conclusion: str = Field(min_length=1)
    session_story: tuple[str, ...] = Field(min_length=2, max_length=4)
    comparison_statement: str = Field(min_length=1)
    themes: tuple[ReviewThemeV2, ...] = Field(default=(), max_length=3)
    money_making_effect: tuple[str, ...] = Field(min_length=1, max_length=4)
    loss_making_effect: tuple[str, ...] = Field(min_length=1, max_length=4)
    next_day_scenarios: tuple[NextDayScenarioV2, ...] = Field(min_length=3, max_length=3)
    sections: tuple[SectionV3, ...] = Field(min_length=8)
    # Compatibility fields retained for current service and UI consumers.
    recap: MarketRecapV1
    next_day_outlook: NextDayOutlookV1
    opportunity_sectors: tuple[OpportunitySectorV1, ...] = Field(default=(), max_length=5)

    @model_validator(mode="after")
    def validate_sections(self) -> "PostMarketReviewPresentationV3":
        section_ids = [item.section_id for item in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("presentation section ids must be unique")
        missing = [
            section_id
            for section_id in PRESENTATION_V3_REQUIRED_SECTIONS
            if section_id not in section_ids
        ]
        if missing:
            raise ValueError(f"presentation is missing required sections: {', '.join(missing)}")
        return self


# Short generic name for callers that do not need the domain-specific prefix.
PresentationV3 = PostMarketReviewPresentationV3


class ArticleSectionV4(ContractModel):
    """One readable chapter in the editorial review body."""

    section_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1)
    paragraphs: tuple[str, ...] = Field(min_length=1, max_length=4)


class WatchItemV4(ContractModel):
    """A next-session question with explicit confirmation and failure signals."""

    rank: int = Field(ge=1, le=5)
    title: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)
    confirmation: str = Field(min_length=1)
    invalidation: str = Field(min_length=1)


class PostMarketReviewPresentationV4(ContractModel):
    """Editorial article over an immutable ``post_market_review.v1`` archive."""

    contract: Literal["post_market_review_presentation.v4"] = "post_market_review_presentation.v4"
    schema_version: Literal[4] = 4
    review_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    standfirst: str = Field(min_length=1)
    sections: tuple[ArticleSectionV4, ...] = Field(min_length=5, max_length=7)
    watch_items: tuple[WatchItemV4, ...] = Field(min_length=1, max_length=5)
    appendix_sections: tuple[SectionV3, ...] = Field(min_length=8)
    # Compatibility fields retained for archive/API consumers.
    day_character: str = Field(min_length=1)
    core_conclusion: str = Field(min_length=1)
    session_story: tuple[str, ...] = Field(min_length=2, max_length=4)
    comparison_statement: str = Field(min_length=1)
    themes: tuple[ReviewThemeV2, ...] = Field(default=(), max_length=3)
    money_making_effect: tuple[str, ...] = Field(min_length=1, max_length=4)
    loss_making_effect: tuple[str, ...] = Field(min_length=1, max_length=4)
    next_day_scenarios: tuple[NextDayScenarioV2, ...] = Field(min_length=3, max_length=3)
    recap: MarketRecapV1
    next_day_outlook: NextDayOutlookV1
    opportunity_sectors: tuple[OpportunitySectorV1, ...] = Field(default=(), max_length=5)

    @model_validator(mode="after")
    def validate_article(self) -> "PostMarketReviewPresentationV4":
        section_ids = [item.section_id for item in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("article section ids must be unique")
        ranks = [item.rank for item in self.watch_items]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("watch item ranks must be consecutive and start at one")
        return self


PresentationV4 = PostMarketReviewPresentationV4


class ReviewOutcomeV1(ContractModel):
    contract: Literal["post_market_review_outcome.v1"] = "post_market_review_outcome.v1"
    schema_version: Literal[1] = 1
    review_id: str = Field(min_length=1)
    forecast_trade_date: date
    evaluated_on: date
    forecast_bias: OutlookBias
    realized_bias: OutlookBias
    verdict: OutcomeVerdict
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def chronological(self) -> "ReviewOutcomeV1":
        if self.evaluated_on <= self.forecast_trade_date:
            raise ValueError("review outcome must be evaluated on a later date")
        return self


def empty_learning() -> ReviewLearningV1:
    return ReviewLearningV1(calibration_note="尚无可核验的历史日复盘，先以低置信度积累样本。")


def _watch_signals(snapshot: MarketWatchSnapshotV1) -> tuple[float | None, float | None]:
    index_changes = [item.change_pct for item in snapshot.indices if item.available and item.change_pct is not None]
    return (
        mean(index_changes) if index_changes else None,
        snapshot.breadth.advance_ratio if snapshot.breadth.available else None,
    )


def _quality(evidence: DailyMarketReviewEvidenceV1) -> ReviewQuality:
    snapshot = evidence.market_watch
    if (
        snapshot.market_state.phase != MarketPhase.CLOSED
        or snapshot.freshness.status
        in {FreshnessStatus.STALE, FreshnessStatus.UNAVAILABLE}
        or evidence.universe is None
        or sum(item.available for item in snapshot.indices) < 2
    ):
        return ReviewQuality.ABSTAINED
    if any(item.status != EvidenceStatus.ACCEPTED for item in evidence.components):
        return ReviewQuality.DEGRADED
    return ReviewQuality.READY


def _cny(value: float) -> str:
    absolute = abs(value)
    if absolute >= 100_000_000:
        return f"{value / 100_000_000:+.1f}亿元"
    if absolute >= 10_000:
        return f"{value / 10_000:+.0f}万元"
    return f"{value:+.0f}元"


def _plain_net_flow(value: float) -> str:
    if value > 0:
        return f"净买入{_cny(value).lstrip('+')}"
    if value < 0:
        return f"净卖出{_cny(abs(value)).lstrip('+')}"
    return "净额基本持平"


def _realized_bias(value: DailyMarketReviewEvidenceV1 | MarketWatchSnapshotV1) -> OutlookBias:
    if isinstance(value, DailyMarketReviewEvidenceV1):
        index_mean, fallback_ratio = _watch_signals(value.market_watch)
        ratio = value.universe.advance_ratio if value.universe is not None else fallback_ratio
        median_change = value.universe.median_change_pct if value.universe is not None else None
    else:
        index_mean, ratio = _watch_signals(value)
        median_change = None
    if index_mean is None:
        return OutlookBias.UNCERTAIN
    positive_cross_section = ratio is None or ratio >= 0.52
    negative_cross_section = ratio is None or ratio <= 0.48
    if index_mean >= 0.35 and positive_cross_section and (median_change is None or median_change >= 0):
        return OutlookBias.CONSTRUCTIVE
    if index_mean <= -0.35 and negative_cross_section and (median_change is None or median_change <= 0):
        return OutlookBias.DEFENSIVE
    return OutlookBias.BALANCED


def _breadth_plain_language(ratio: float) -> str:
    if ratio <= 0.30:
        return "大约每4只股票里只有1只上涨"
    if ratio <= 0.45:
        return "下跌股票明显多于上涨股票"
    if ratio < 0.55:
        return "上涨和下跌家数大致相当"
    if ratio < 0.70:
        return "上涨股票明显更多"
    return "大约每4只股票里有3只上涨"


def _theme_family(name: str) -> str:
    """Collapse provider taxonomies into market themes people actually discuss."""

    normalized = name.strip()
    families = (
        (("白银", "黄金", "贵金属"), "贵金属"),
        (("动力煤", "焦炭", "煤炭", "焦煤"), "煤炭"),
        (("种子", "粮食", "转基因", "种业"), "种业"),
        (("工业金属", "铜", "钼", "铝", "锌", "有色"), "有色金属"),
        (("CRO", "医疗研发", "生物", "疫苗", "创新药", "单抗", "医药"), "医药"),
        (("印制电路", "PCB", "被动元件", "元件"), "电子元件"),
        (("算力", "CPO", "光模块", "人工智能", "AI"), "AI硬件"),
        (("半导体", "芯片", "通信", "电子", "计算机"), "科技成长"),
        (("视频媒体", "文字媒体", "传媒", "游戏"), "传媒"),
        (("红利", "银行", "保险", "公用事业"), "红利防守"),
    )
    for needles, family in families:
        if any(token in normalized for token in needles):
            return family
    return normalized


def _recap(
    evidence: DailyMarketReviewEvidenceV1,
    quality: ReviewQuality,
    opportunities: tuple[OpportunitySectorV1, ...],
) -> MarketRecapV1:
    snapshot = evidence.market_watch
    index_mean, watch_ratio = _watch_signals(snapshot)
    indices = tuple(
        ReviewIndexV1(role=item.role.value, name=item.name, change_pct=item.change_pct)
        for item in snapshot.indices
        if item.available and item.change_pct is not None
    )
    universe = evidence.universe
    ratio = universe.advance_ratio if universe is not None else watch_ratio
    median_change = universe.median_change_pct if universe is not None else None
    opportunity_names = "、".join(item.name for item in opportunities[:3])
    if quality == ReviewQuality.ABSTAINED:
        headline = "今天的数据不够完整，先不勉强判断明天方向"
    elif index_mean is not None and index_mean >= 0.35 and (ratio or 0) >= 0.52 and (median_change or 0) >= 0:
        headline = "今天多数股票上涨，市场整体偏强"
    elif index_mean is not None and index_mean <= -0.35 and (ratio or 1) <= 0.48 and (median_change or 0) <= 0:
        headline = (
            f"今天是普跌行情，{opportunity_names}逆势活跃"
            if opportunity_names
            else "今天是普跌行情，整体应以防守为主"
        )
    else:
        headline = (
            f"今天指数和个股表现分化，机会集中在{opportunity_names}"
            if opportunity_names
            else "今天指数和个股表现分化，赚钱效应比较零散"
        )

    index_text = (
        f"主要指数平均{index_mean:+.2f}%。"
        if index_mean is not None
        else "主要指数数据不足，不能只凭个别指数下结论。"
    )
    if universe is not None:
        breadth_plain = _breadth_plain_language(universe.advance_ratio)
        breadth_text = (
            f"{breadth_plain}：上涨{universe.up_count}只，下跌{universe.down_count}只"
            f"（上涨占比{universe.advance_ratio * 100:.1f}%）。"
        )
        universe_text = (
            f"多数人的持仓体感约为{universe.median_change_pct:+.2f}%（全A中位数）；"
            f"两市成交约{universe.total_amount_cny / 100_000_000:.0f}亿元"
            f"，最活跃的是{universe.most_traded[0].name if universe.most_traded else '暂无可靠样本'}。"
        )
    else:
        breadth_plain = "全A涨跌家数缺失"
        breadth_text = "没有拿到完整的全A涨跌家数，不能用几个指数代替大多数股票的真实表现。"
        universe_text = "全A中位数和个股成交分布缺失，今天的持仓体感无法可靠估计。"
    turnover = snapshot.turnover
    if turnover.available:
        direction = {TurnoverDirection.EXPAND: "放量", TurnoverDirection.SHRINK: "缩量", TurnoverDirection.FLAT: "基本持平"}[turnover.direction]
        turnover_text = f"和上一交易日同一时点相比，今天两市成交额{direction}{abs((turnover.difference_ratio or 0) * 100):.1f}%。"
    else:
        turnover_text = "缺少上一交易日同一时点的可比数据，今天无法可靠判断是放量还是缩量。"
    first_ratio = evidence.intraday.first_advance_ratio
    last_ratio = evidence.intraday.last_advance_ratio
    if first_ratio is not None and last_ratio is not None:
        change = last_ratio - first_ratio
        direction = "越走越强" if change >= 0.08 else "越走越弱" if change <= -0.08 else "前后变化不大"
        intraday_text = (
            f"上涨占比从早盘{first_ratio * 100:.1f}%变到收盘{last_ratio * 100:.1f}%，"
            f"盘面整体{direction}。"
        )
    elif last_ratio is not None:
        intraday_text = (
            f"早盘部分轨迹缺失；收盘时上涨股票占{last_ratio * 100:.1f}%，"
            "只能确认收盘强弱，不能可靠还原全天节奏。"
        )
    else:
        intraday_text = "盘中涨跌覆盖轨迹不完整，今天不对早盘到收盘的演变强行下结论。"
    if evidence.etfs is not None:
        leaders = "、".join(item.name for item in evidence.etfs.top_gainers[:3]) or "无"
        etf_text = f"ETF整体中位数{evidence.etfs.median_change_pct:+.2f}%；逆势靠前的是{leaders}。"
    else:
        etf_text = "ETF行情缺失，今天无法用ETF确认资金偏好的方向。"
    sector_parts = []
    for summary, label in ((evidence.industry_sectors, "行业"), (evidence.concept_sectors, "概念")):
        if summary is not None:
            leaders = "、".join(item.name for item in summary.top_gainers[:3]) or "无"
            inflows = "、".join(item.name for item in summary.top_inflows[:3]) or "资金缺失"
            sector_parts.append(f"{label}领涨的是{leaders}，资金流入靠前的是{inflows}")
    rotation_text = "；".join(sector_parts) + "。" if sector_parts else "行业和概念板块行情缺失，今天无法判断主线。"
    if evidence.limit_events is not None:
        reason = "、".join(item.reason for item in evidence.limit_events.top_reasons[:3]) or "原因覆盖不足"
        sentiment_text = (
            f"短线还有{evidence.limit_events.limit_up_count}只涨停、"
            f"{evidence.limit_events.limit_down_count if evidence.limit_events.limit_down_count is not None else '未知'}只跌停，"
            f"最高{evidence.limit_events.max_board_count or 1}板；活跃题材主要是{reason}。"
        )
    else:
        sentiment_text = "涨停、跌停和连板数据缺失，今天无法可靠判断短线情绪。"
    if evidence.stock_fund_flow is not None:
        top = evidence.stock_fund_flow.top_inflows[0] if evidence.stock_fund_flow.top_inflows else None
        top_name = (top.name or top.instrument_id) if top else "无"
        flow_ratio = evidence.stock_fund_flow.positive_count / evidence.stock_fund_flow.scanned_count
        flow_text = (
            f"只有{flow_ratio * 100:.1f}%的股票录得资金净流入；"
            f"单只股票资金净额的中位数是{_cny(evidence.stock_fund_flow.median_net_amount_cny)}，"
            f"净流入最多的是{top_name}。"
        )
    else:
        flow_ratio = None
        flow_text = "个股资金流数据缺失，今天不判断资金是在普遍流入还是流出。"
    if evidence.dragon_tiger is not None:
        top = evidence.dragon_tiger.top_net_buys[0].name if evidence.dragon_tiger.top_net_buys else "无有效上榜记录"
        dragon_text = (
            f"龙虎榜共{evidence.dragon_tiger.listed_count}条，合计{_plain_net_flow(evidence.dragon_tiger.total_net_amount_cny)}；"
            f"净买入最多的是{top}。"
        )
    else:
        dragon_text = "龙虎榜数据缺失，今天无法确认活跃资金的净买卖方向。"

    if universe is not None and index_mean is not None:
        if index_mean <= -0.35 and universe.median_change_pct <= 0:
            opening = (
                f"今天不是只跌指数，而是多数股票一起走弱：主要指数平均{index_mean:+.2f}%，"
                f"全A中位数{universe.median_change_pct:+.2f}%，{breadth_plain}。"
            )
        elif index_mean >= 0.35 and universe.median_change_pct >= 0:
            opening = (
                f"今天的上涨有个股配合：主要指数平均{index_mean:+.2f}%，"
                f"全A中位数{universe.median_change_pct:+.2f}%，{breadth_plain}。"
            )
        else:
            opening = (
                f"今天指数和个股没有完全同步：主要指数平均{index_mean:+.2f}%，"
                f"全A中位数{universe.median_change_pct:+.2f}%，{breadth_plain}。"
            )
    else:
        opening = "今天的全市场核心数据不完整，下面只陈述能够确认的事实。"
    if opportunity_names:
        rotation_summary = f"资金和强势股主要集中在{opportunity_names}，赚钱效应并不分散。"
    else:
        rotation_summary = "没有板块同时通过价格、广度和资金的多重确认，主线仍不清楚。"
    if evidence.limit_events is not None and flow_ratio is not None:
        temperature_summary = (
            f"短线仍有{evidence.limit_events.limit_up_count}只涨停和{evidence.limit_events.max_board_count or 1}板高度，"
            f"但资金净流入股票只占{flow_ratio * 100:.1f}%，说明机会有、容错不高。"
        )
    else:
        temperature_summary = "短线情绪或资金数据不完整，不能把局部强势当成全面回暖。"

    highlights: list[str] = []
    if opportunity_names:
        highlights.append(f"相对强势方向集中在{opportunity_names}，至少有两类证据相互确认。")
    if evidence.limit_events is not None and evidence.limit_events.limit_up_count >= 30:
        highlights.append(
            f"短线情绪没有完全熄火：{evidence.limit_events.limit_up_count}只涨停，最高{evidence.limit_events.max_board_count or 1}板。"
        )
    if evidence.dragon_tiger is not None and evidence.dragon_tiger.total_net_amount_cny > 0:
        highlights.append(
            f"龙虎榜合计{_plain_net_flow(evidence.dragon_tiger.total_net_amount_cny)}，活跃资金仍在局部做多。"
        )
    if not highlights:
        highlights.append("今天没有足够明确的盘面亮点，保持谨慎比勉强找机会更重要。")

    risks: list[str] = []
    if universe is not None and universe.advance_ratio < 0.45:
        risks.append(f"市场广度偏弱：上涨股票只占{universe.advance_ratio * 100:.1f}%。")
    if universe is not None and universe.median_change_pct < 0:
        risks.append(f"全A中位数{universe.median_change_pct:+.2f}%，多数持仓的实际体感偏差。")
    if flow_ratio is not None and flow_ratio < 0.45:
        risks.append(f"资金净流入股票只占{flow_ratio * 100:.1f}%，资金面没有形成普遍回流。")
    if quality == ReviewQuality.DEGRADED:
        risks.append("部分数据仍是降级状态，因此明日判断只给低置信度。")
    return MarketRecapV1(
        headline=headline,
        summary=" ".join((opening, rotation_summary, temperature_summary)),
        indices=indices,
        breadth_statement=breadth_text,
        universe_statement=universe_text,
        turnover_statement=turnover_text,
        intraday_statement=intraday_text,
        etf_statement=etf_text,
        rotation_statement=rotation_text,
        sentiment_statement=sentiment_text,
        stock_fund_flow_statement=flow_text,
        dragon_tiger_statement=dragon_text,
        supporting_evidence=tuple(dict.fromkeys(highlights)),
        risk_evidence=tuple(dict.fromkeys(risks)),
    )


def _outlook_score(evidence: DailyMarketReviewEvidenceV1) -> tuple[int, list[str], list[str]]:
    snapshot = evidence.market_watch
    index_mean, watch_ratio = _watch_signals(snapshot)
    score = 0
    support: list[str] = []
    counter: list[str] = []

    def signal(value: float | None, positive: float, negative: float, label: str, weight: int = 1) -> None:
        nonlocal score
        if value is None:
            return
        if value >= positive:
            score += weight
            support.append(f"{label}{value:+.2f}")
        elif value <= negative:
            score -= weight
            counter.append(f"{label}{value:+.2f}")

    signal(index_mean, 0.35, -0.35, "角色指数均值", 2)
    universe = evidence.universe
    ratio = universe.advance_ratio if universe is not None else watch_ratio
    if ratio is not None:
        if ratio >= 0.56:
            score += 2
            support.append(f"上涨占比{ratio * 100:.1f}%")
        elif ratio <= 0.44:
            score -= 2
            counter.append(f"上涨占比仅{ratio * 100:.1f}%")
    if universe is not None:
        signal(universe.median_change_pct, 0.2, -0.2, "全A中位数", 2)
    if evidence.etfs is not None:
        signal(evidence.etfs.median_change_pct, 0.15, -0.15, "ETF中位数")
    sector_medians = [item.median_change_pct for item in (evidence.industry_sectors, evidence.concept_sectors) if item is not None]
    signal(mean(sector_medians) if sector_medians else None, 0.2, -0.2, "板块中位数")
    if snapshot.turnover.available:
        if snapshot.turnover.direction == TurnoverDirection.EXPAND:
            score += 1
            support.append("成交额同比同刻放大")
        elif snapshot.turnover.direction == TurnoverDirection.SHRINK:
            score -= 1
            counter.append("成交额同比同刻收缩")
    if evidence.limit_events is not None:
        if (evidence.limit_events.max_board_count or 0) >= 4 and evidence.limit_events.limit_up_count >= 40:
            score += 1
            support.append("涨停梯队具备高度与数量")
        if evidence.limit_events.limit_down_count is not None and evidence.limit_events.limit_down_count >= evidence.limit_events.limit_up_count:
            score -= 1
            counter.append("跌停数量不低于涨停数量")
    if evidence.stock_fund_flow is not None:
        flow_ratio = evidence.stock_fund_flow.positive_count / evidence.stock_fund_flow.scanned_count
        if flow_ratio >= 0.55:
            score += 1
            support.append(f"个股资金净流入覆盖{flow_ratio * 100:.1f}%")
        elif flow_ratio <= 0.45:
            score -= 1
            counter.append(f"个股资金净流入覆盖仅{flow_ratio * 100:.1f}%")
    if evidence.dragon_tiger is not None and evidence.dragon_tiger.listed_count:
        if evidence.dragon_tiger.total_net_amount_cny > 0:
            score += 1
            support.append("龙虎榜合计净买入")
        elif evidence.dragon_tiger.total_net_amount_cny < 0:
            score -= 1
            counter.append("龙虎榜合计净卖出")
    first_ratio = evidence.intraday.first_advance_ratio
    last_ratio = evidence.intraday.last_advance_ratio
    if first_ratio is not None and last_ratio is not None:
        if last_ratio - first_ratio >= 0.08:
            score += 1
            support.append("盘中参与度向收盘扩散")
        elif last_ratio - first_ratio <= -0.08:
            score -= 1
            counter.append("盘中参与度向收盘收窄")
    return score, support, counter


def _outlook(
    evidence: DailyMarketReviewEvidenceV1,
    quality: ReviewQuality,
    learning: ReviewLearningV1,
    opportunities: tuple[OpportunitySectorV1, ...],
) -> NextDayOutlookV1:
    if quality == ReviewQuality.ABSTAINED:
        return NextDayOutlookV1(
            bias=OutlookBias.UNCERTAIN,
            confidence=ConclusionStrength.ABSTAIN,
            thesis="今天缺了关键数据，我不想用几个局部现象硬猜明天涨跌。",
            expected_shape="明天先观察，不预设方向；等指数、上涨家数、成交和板块资金重新给出一致信号。",
            confirmation="主要指数和大多数股票同向，且成交与板块资金跟上，才开始形成可用判断。",
            invalidation="单个指数、ETF或热点突然上涨，都不足以证明市场整体转强。",
            risk_control="关键数据恢复前，不根据这份复盘增加方向性仓位。",
            counter_evidence=evidence.quality_notes,
        )
    score, support, counter = _outlook_score(evidence)
    bias = OutlookBias.CONSTRUCTIVE if score >= 4 else OutlookBias.DEFENSIVE if score <= -4 else OutlookBias.BALANCED
    confidence = ConclusionStrength.MODERATE if quality == ReviewQuality.READY and abs(score) >= 6 else ConclusionStrength.WEAK
    if learning.evaluated_count >= 5 and learning.support_ratio is not None and learning.support_ratio < 0.45:
        confidence = ConclusionStrength.WEAK
        counter.append("近期历史结论支持度偏低")
    opportunity_names = "、".join(item.name for item in opportunities[:3])
    if bias == OutlookBias.CONSTRUCTIVE:
        thesis = "明天先按偏强行情准备，但不把高开直接当成继续上涨。"
        expected = (
            f"更可能震荡向上，{opportunity_names}继续活跃；如果只有指数涨、个股不跟，行情会转成分化。"
            if opportunity_names
            else "更可能震荡向上；如果只有指数涨、个股不跟，行情会转成分化。"
        )
        confirmation = "主要指数保持红盘，上涨股票稳定超过一半，而且强势板块的资金没有转为流出。"
        invalidation = "主要指数转弱、全A中位数翻绿、上涨股票重新少于一半，偏强判断就不再成立。"
        control = "只看价格、上涨家数和资金同时变强的板块；如果只是领涨股冲高，不追。"
    elif bias == OutlookBias.DEFENSIVE:
        thesis = "明天先按弱势延续来准备，不提前赌全面反弹。"
        expected = (
            f"大盘更可能弱势震荡，{opportunity_names}这些逆势方向也会明显分化；指数单独反弹还不算反转。"
            if opportunity_names
            else "大盘更可能弱势震荡或先反弹后分化；指数单独反弹还不算反转。"
        )
        confirmation = "上午如果多数股票继续下跌、全A中位数仍为负，而且今天的强势板块开始补跌，说明弱势仍在延续。"
        invalidation = "如果主要指数翻红、上涨股票稳定超过一半，并且多个行业同时获得资金流入，防守判断作废。"
        control = "不要追今天已经大涨的唯一强势方向；先等大多数股票和资金面一起改善，再提高进攻性。"
    else:
        thesis = "今天没有形成一致方向，明天先按震荡和板块轮动来看。"
        expected = (
            f"指数可能反复，机会更可能留在{opportunity_names}等局部方向，而不是全面普涨。"
            if opportunity_names
            else "指数可能反复，机会更可能来自盘中新出现的局部方向，而不是全面普涨。"
        )
        confirmation = "指数、上涨家数、全A中位数和成交中至少三项连续同向，才说明市场选出了方向。"
        invalidation = "如果价格、上涨家数和资金继续各走各的，就维持震荡判断，不追单点异动。"
        control = "少做预判，等盘中条件真正出现后再行动。"
    return NextDayOutlookV1(
        bias=bias,
        confidence=confidence,
        thesis=thesis,
        expected_shape=expected,
        confirmation=confirmation,
        invalidation=invalidation,
        risk_control=control,
        supporting_evidence=tuple(support[:8]),
        counter_evidence=tuple((counter + list(evidence.quality_notes))[:8]),
    )


def _same_opportunity_theme(
    candidate: SectorReviewItemV1,
    selected: OpportunitySectorV1,
) -> bool:
    if (
        candidate.leader_instrument_id
        and candidate.leader_instrument_id == selected.leader_instrument_id
    ):
        return True
    if _theme_family(candidate.name) == _theme_family(selected.name):
        return True
    if {_theme_family(candidate.name), _theme_family(selected.name)} <= {"贵金属", "有色金属"}:
        return True
    left = candidate.name.strip()
    right = selected.name.strip()
    shorter = left if len(left) <= len(right) else right
    longer = right if shorter == left else left
    for width in range(len(shorter), 1, -1):
        if any(shorter[start : start + width] in longer for start in range(len(shorter) - width + 1)):
            return True
    return False


def _opportunities(evidence: DailyMarketReviewEvidenceV1, quality: ReviewQuality) -> tuple[OpportunitySectorV1, ...]:
    if quality == ReviewQuality.ABSTAINED:
        return ()
    snapshot = evidence.market_watch
    rotation = {item.sector_key: item for item in snapshot.rotation.sectors}
    trajectory = {
        item.sector_key: item
        for item in (snapshot.sector_flow_trajectory.sectors if snapshot.sector_flow_trajectory is not None else ())
    }
    stock_flow_ids = {item.instrument_id for item in (evidence.stock_fund_flow.top_inflows[:20] if evidence.stock_fund_flow else ())}
    dragon_net_by_instrument: dict[str, float] = {}
    if evidence.dragon_tiger is not None:
        for trade in (*evidence.dragon_tiger.top_net_buys, *evidence.dragon_tiger.top_net_sells):
            dragon_net_by_instrument[trade.instrument_id] = (
                dragon_net_by_instrument.get(trade.instrument_id, 0.0) + trade.net_amount_cny
            )
    dragon_ids = {
        instrument_id
        for instrument_id, net_amount in dragon_net_by_instrument.items()
        if net_amount > 0
    }
    limit_ids = {item.instrument_id for item in (evidence.limit_events.representative_events if evidence.limit_events else ())}
    limit_reason_by_id = {
        item.instrument_id: item.reason
        for item in (evidence.limit_events.representative_events if evidence.limit_events else ())
    }
    limit_reasons = [item.reason for item in (evidence.limit_events.top_reasons if evidence.limit_events else ())]
    candidates: dict[str, SectorReviewItemV1] = {}
    inflow_keys: set[str] = set()
    for summary in (evidence.industry_sectors, evidence.concept_sectors):
        if summary is None:
            continue
        for item in summary.top_gainers:
            candidates[item.sector_key] = item
        for item in summary.top_inflows:
            candidates[item.sector_key] = item
            if (item.main_net_inflow_cny or 0) > 0:
                inflow_keys.add(item.sector_key)

    ranked: list[tuple[int, SectorReviewItemV1, tuple[str, ...]]] = []
    for item in candidates.values():
        if item.change_pct <= 0:
            continue
        score = 1 if item.change_pct < 1.5 else 2
        chain = [f"板块收涨{item.change_pct:+.2f}%"]
        if item.breadth_ratio is not None:
            if item.breadth_ratio >= 0.55:
                score += 2
                chain.append(f"上涨覆盖{item.breadth_ratio * 100:.1f}%")
            else:
                chain.append(f"上涨覆盖仅{item.breadth_ratio * 100:.1f}%")
        if item.sector_key in inflow_keys and (item.main_net_inflow_cny or 0) > 0:
            score += 2
            chain.append(f"主力净流入{_cny(item.main_net_inflow_cny or 0)}")
        leader_id = item.leader_instrument_id
        if leader_id and leader_id in stock_flow_ids:
            score += 1
            chain.append("领涨股进入个股资金净流入前列")
        if leader_id and leader_id in dragon_ids:
            score += 1
            chain.append("领涨股获龙虎榜净买入确认")
        if leader_id and leader_id in limit_ids:
            score += 1
            chain.append("领涨股进入高辨识度涨停样本")
        if any(item.name in reason or reason in item.name for reason in limit_reasons):
            score += 1
            chain.append("涨停原因聚类与板块名称相互印证")
        if score >= 3 and len(chain) >= 2:
            ranked.append((score, item, tuple(chain)))
    ranked.sort(key=lambda row: (-row[0], -row[1].change_pct, row[1].sector_key))
    results = []
    for score, item, chain in ranked:
        if any(_same_opportunity_theme(item, selected) for selected in results):
            continue
        rotation_item = rotation.get(item.sector_key)
        flow_item = trajectory.get(item.sector_key)
        tags = rotation_item.tags if rotation_item is not None else (SectorTag.MIXED,)
        tier = flow_item.observation_tier if flow_item is not None else None
        confirmed = score >= 6 and (item.main_net_inflow_cny or 0) > 0
        family = _theme_family(item.name)
        price_detail = f"{item.name}板块收涨{item.change_pct:+.2f}%"
        breadth_detail = (
            f"、{item.breadth_ratio * 100:.1f}%的成分股上涨"
            if item.breadth_ratio is not None
            else ""
        )
        flow_detail = (
            f"、主力净流入{_cny(item.main_net_inflow_cny).lstrip('+')}"
            if (item.main_net_inflow_cny or 0) > 0
            else ""
        )
        leader_detail = (
            f"，领涨股{item.leader_name}{item.leader_change_pct:+.2f}%"
            if item.leader_name and item.leader_change_pct is not None
            else f"，领涨股是{item.leader_name}"
            if item.leader_name
            else ""
        )
        catalyst_detail = (
            f"；涨停池给出的盘面归因是“{limit_reason_by_id[item.leader_instrument_id]}”"
            if item.leader_instrument_id in limit_reason_by_id
            else ""
        )
        watch_target = item.leader_name or family
        results.append(OpportunitySectorV1(
            sector_key=item.sector_key,
            sector_type=item.sector_type,
            name=family,
            conviction=OpportunityConviction.CONFIRMED if confirmed else OpportunityConviction.WATCH,
            rank_score=score,
            tags=tags,
            change_pct=item.change_pct,
            breadth_ratio=item.breadth_ratio,
            main_net_inflow_cny=item.main_net_inflow_cny,
            observation_tier=tier,
            leader_instrument_id=item.leader_instrument_id,
            leader_name=item.leader_name,
            evidence_chain=chain,
            rationale=(
                f"这不是一只股票单独异动：{price_detail}{breadth_detail}{flow_detail}{leader_detail}"
                f"{catalyst_detail}。"
            ),
            next_day_confirmation=(
                f"先看{watch_target}能否扛住第一次分歧，同时{family}板块仍有一半以上成分股上涨，"
                "且资金没有由流入转为流出。"
            ),
            invalidation=(
                f"如果{family}板块翻绿、上涨覆盖跌到一半以下，"
                f"或{watch_target}走弱并伴随资金流出，就从观察名单移除。"
            ),
            risk_note=(
                f"{item.name}今天已经上涨{item.change_pct:+.2f}%，次日高开不等于机会；"
                "这张卡只列跟踪条件，不建议看到强势就追。"
            ),
        ))
        if len(results) == 3:
            break
    return tuple(results)


def _distribution_count(evidence: DailyMarketReviewEvidenceV1, bucket: str) -> int:
    if evidence.universe is None:
        return 0
    return next((item.count for item in evidence.universe.distribution if item.bucket == bucket), 0)


def _index_changes(evidence: DailyMarketReviewEvidenceV1) -> dict[str, tuple[str, float]]:
    return {
        item.role.value: (item.name, item.change_pct)
        for item in evidence.market_watch.indices
        if item.available and item.change_pct is not None
    }


def _clock_label(value: datetime | None) -> str | None:
    return value.astimezone(SHANGHAI).strftime("%H:%M") if value is not None else None


def _downside_families(evidence: DailyMarketReviewEvidenceV1, *, limit: int = 3) -> tuple[str, ...]:
    candidates: list[SectorReviewItemV1] = []
    if evidence.industry_sectors is not None:
        candidates.extend(evidence.industry_sectors.top_outflows)
        candidates.extend(evidence.industry_sectors.top_losers)
    candidates.sort(key=lambda item: (item.main_net_inflow_cny or 0, item.change_pct))
    ignored = {"历史新高"}
    result: list[str] = []
    for item in candidates:
        if item.name in ignored or item.change_pct >= 0:
            continue
        family = _theme_family(item.name)
        if family not in result:
            result.append(family)
        if len(result) == limit:
            break
    return tuple(result)


def _day_character(
    evidence: DailyMarketReviewEvidenceV1,
    previous: PostMarketReviewV1 | None,
) -> str:
    index_mean, fallback_ratio = _watch_signals(evidence.market_watch)
    universe = evidence.universe
    ratio = universe.advance_ratio if universe is not None else fallback_ratio
    median_change = universe.median_change_pct if universe is not None else None
    weak = (
        index_mean is not None
        and index_mean <= -0.5
        and ratio is not None
        and ratio <= 0.35
        and (median_change is None or median_change <= -0.5)
    )
    strong = (
        index_mean is not None
        and index_mean >= 0.5
        and ratio is not None
        and ratio >= 0.65
        and (median_change is None or median_change >= 0.5)
    )
    if weak:
        if previous is not None and previous.evidence.universe is not None and universe is not None:
            prior = previous.evidence.universe
            if ratio <= prior.advance_ratio - 0.10 and median_change <= prior.median_change_pct - 0.5:
                return "退潮加速日"
            if prior.advance_ratio <= 0.35 and prior.median_change_pct <= -0.5:
                return "弱势延续日"
        return "普跌分化日"
    if strong:
        if previous is not None and previous.evidence.universe is not None and universe is not None:
            prior = previous.evidence.universe
            if ratio >= prior.advance_ratio + 0.10 and median_change >= prior.median_change_pct + 0.5:
                return "普涨修复日"
        return "普涨进攻日"
    if index_mean is not None and index_mean >= 0.25 and ratio is not None and ratio < 0.48:
        return "指数强、个股弱的分化日"
    if index_mean is not None and index_mean <= -0.25 and ratio is not None and ratio > 0.52:
        return "指数弱、个股有修复的分化日"
    return "震荡轮动日"


def _session_story(evidence: DailyMarketReviewEvidenceV1) -> tuple[str, ...]:
    universe = evidence.universe
    intraday = evidence.intraday
    story: list[str] = []
    strongest_time = _clock_label(intraday.strongest_as_of)
    weakest_time = _clock_label(intraday.weakest_as_of)
    last_ratio = intraday.last_advance_ratio
    if (
        intraday.coverage_ratio >= 0.8
        and intraday.max_advance_ratio is not None
        and intraday.morning_close_advance_ratio is not None
        and intraday.min_advance_ratio is not None
        and last_ratio is not None
    ):
        improved_from_low = (
            intraday.weakest_as_of is not None
            and intraday.strongest_as_of is not None
            and intraday.weakest_as_of < intraday.strongest_as_of
            and last_ratio >= 0.55
        )
        if improved_from_low:
            story.append(
                f"盘中节奏上，{weakest_time or '开盘'}上涨股票最低只有{intraday.min_advance_ratio * 100:.1f}%，"
                f"午盘已经回到{intraday.morning_close_advance_ratio * 100:.1f}%；"
                f"{strongest_time or '午后'}一度升到{intraday.max_advance_ratio * 100:.1f}%，"
                f"收盘仍有{last_ratio * 100:.1f}%。个股参与度从低开后的低点持续扩散。"
            )
        else:
            ending = (
                "尾盘有回拉，但没有扭转弱势。"
                if last_ratio >= intraday.min_advance_ratio + 0.05 and last_ratio < 0.45
                else "弱势一直延续到收盘。"
                if last_ratio < 0.35
                else "收盘参与度重新回到中性区域。"
            )
            story.append(
                f"盘中节奏上，{strongest_time or '早盘'}上涨股票一度占{intraday.max_advance_ratio * 100:.1f}%，"
                f"午盘只剩{intraday.morning_close_advance_ratio * 100:.1f}%；"
                f"{weakest_time or '午后'}降到全天最低{intraday.min_advance_ratio * 100:.1f}%，"
                f"收盘回到{last_ratio * 100:.1f}%。{ending}"
            )
    elif intraday.first_advance_ratio is not None and last_ratio is not None:
        movement = last_ratio - intraday.first_advance_ratio
        shape = "越走越强" if movement >= 0.08 else "越走越弱" if movement <= -0.08 else "以震荡为主"
        story.append(
            f"从早盘到收盘，上涨占比由{intraday.first_advance_ratio * 100:.1f}%变为"
            f"{last_ratio * 100:.1f}%，全天{shape}。"
        )
    else:
        story.append("盘中轨迹缺少关键节点，只能判断收盘结果，不能把全天走势编成完整故事。")

    changes = _index_changes(evidence)
    available = list(changes.values())
    weakest = min(available, key=lambda item: item[1]) if available else None
    strongest = max(available, key=lambda item: item[1]) if available else None
    if universe is not None:
        comparison = ""
        if weakest is not None and strongest is not None and weakest[0] != strongest[0]:
            comparison = f"其中{weakest[0]}{weakest[1]:+.2f}%最弱，{strongest[0]}{strongest[1]:+.2f}%相对抗跌。"
        turnover_note = f"两市股票成交约{universe.total_amount_cny / 1_000_000_000_000:.2f}万亿元。"
        if not evidence.market_watch.turnover.available:
            turnover_note += "由于上一交易日同口径数据缺失，今天不判断放量还是缩量。"
        cross_section = (
            "收盘的核心是个股普遍修复"
            if universe.advance_ratio >= 0.6 and universe.median_change_pct > 0
            else "收盘不是指数单独难看"
        )
        story.append(
            f"{cross_section}：{universe.down_count}只下跌、{universe.up_count}只上涨，"
            f"全A中位数{universe.median_change_pct:+.2f}%。{comparison}{turnover_note}"
        )
    else:
        story.append("没有完整全A分布，指数与多数个股是否同向无法确认。")

    if evidence.stock_fund_flow is not None:
        flow = evidence.stock_fund_flow
        ratio = flow.positive_count / flow.scanned_count
        outflow_names = "、".join(
            (item.name or item.instrument_id) for item in flow.top_outflows[:4]
        ) or "未形成清晰集中方向"
        if ratio >= 0.55 and flow.total_net_amount_cny > 0:
            story.append(
                f"按个股资金流口径，{ratio * 100:.1f}%的股票录得净流入，"
                f"合计{_plain_net_flow(flow.total_net_amount_cny)}；净流出靠前的是{outflow_names}。"
                "资金已经回到多数股票，但高成交核心仍有明显分化。"
            )
        else:
            story.append(
                f"按个股资金流口径，只有{ratio * 100:.1f}%的股票录得净流入，"
                f"合计{_plain_net_flow(flow.total_net_amount_cny)}；净流出靠前的是{outflow_names}。"
                "这说明资金更像在撤退和换仓，而不是普遍回流。"
            )
    else:
        story.append("个股资金流缺失，今天只判断价格强弱，不给资金行为强行归因。")
    return tuple(story[:4])


def _comparison_statement(
    evidence: DailyMarketReviewEvidenceV1,
    previous: PostMarketReviewV1 | None,
) -> str:
    current = evidence.universe
    prior = previous.evidence.universe if previous is not None else None
    if current is None or prior is None:
        return "没有上一交易日的同口径存档可比，因此今天只做日内定性，不把单日涨跌硬说成周期拐点。"
    breadth_delta = (current.advance_ratio - prior.advance_ratio) * 100
    median_delta = current.median_change_pct - prior.median_change_pct
    current_limits = evidence.limit_events
    prior_limits = previous.evidence.limit_events if previous is not None else None
    limit_text = ""
    if current_limits is not None and prior_limits is not None:
        limit_text = (
            f"；涨停由{prior_limits.limit_up_count}只变为{current_limits.limit_up_count}只，"
            f"最高板由{prior_limits.max_board_count or 0}板变为{current_limits.max_board_count or 0}板"
        )
    direction = "明显改善" if breadth_delta >= 10 and median_delta >= 0.5 else "明显转弱" if breadth_delta <= -10 and median_delta <= -0.5 else "变化不大"
    return (
        f"和上一份同口径复盘相比，上涨占比变化{breadth_delta:+.1f}个百分点，"
        f"全A中位数变化{median_delta:+.2f}个百分点{limit_text}，整体属于{direction}。"
    )


def _theme_layers(
    evidence: DailyMarketReviewEvidenceV1,
    opportunities: tuple[OpportunitySectorV1, ...],
) -> tuple[ReviewThemeV2, ...]:
    themes: list[ReviewThemeV2] = []
    if opportunities:
        main = opportunities[0]
        representatives = [main.leader_name] if main.leader_name else []
        if evidence.etfs is not None:
            representatives.extend(
                item.name
                for item in evidence.etfs.top_gainers
                if _theme_family(item.name) == main.name
            )
        themes.append(ReviewThemeV2(
            role="当日主线",
            name=main.name,
            analysis=main.rationale,
            representatives=tuple(dict.fromkeys(representatives))[:5],
        ))
    if len(opportunities) > 1:
        secondary = opportunities[1:3]
        themes.append(ReviewThemeV2(
            role="轮动支线",
            name="、".join(item.name for item in secondary),
            analysis=(
                "这些方向也有价格、上涨覆盖或资金流的交叉确认，但强度和辨识度低于当日最强线，"
                "更适合看承接，不适合把同日上涨直接外推成连续主升。"
            ),
            representatives=tuple(
                item.leader_name for item in secondary if item.leader_name
            ),
        ))
    downside = _downside_families(evidence)
    if downside:
        themes.append(ReviewThemeV2(
            role="亏钱集中区",
            name="、".join(downside),
            analysis=(
                "这些方向同时出现在板块资金净流出前列，且价格收跌，说明不是普通轮动，"
                "而是当天最需要回避的抛压来源。"
            ),
            representatives=tuple(
                (item.name or item.instrument_id)
                for item in (evidence.stock_fund_flow.top_outflows[:4] if evidence.stock_fund_flow else ())
            ),
        ))
    return tuple(themes[:3])


def _money_making_effect(
    evidence: DailyMarketReviewEvidenceV1,
    opportunities: tuple[OpportunitySectorV1, ...],
) -> tuple[str, ...]:
    result: list[str] = []
    universe = evidence.universe
    broad_repair = bool(
        universe is not None
        and universe.advance_ratio >= 0.6
        and universe.median_change_pct > 0
    )
    if opportunities:
        names = "、".join(item.name for item in opportunities[:3])
        leaders = "、".join(item.leader_name for item in opportunities[:3] if item.leader_name)
        if broad_repair:
            result.append(
                f"板块主线集中在{names}，代表股是{leaders or '板块前排'}；"
                f"同时全A有{universe.up_count}只上涨，赚钱效应已经从前排扩散到多数股票。"
            )
        else:
            result.append(
                f"板块赚钱效应集中在{names}，代表股是{leaders or '板块前排'}；"
                "这里赚的是逆势抱团的钱，不是普涨的钱。"
            )
    if evidence.limit_events is not None:
        limits = evidence.limit_events
        suffix = (
            "首板和个股广度同步扩散，短线活跃度与全市场修复方向一致。"
            if broad_repair
            else "但高度由少数前排维持，不能据此判断大多数股票已经转强。"
        )
        result.append(
            f"按当前涨停池，短线高度还在：{limits.limit_up_count}只涨停、最高{limits.max_board_count or 1}板。"
            + suffix
        )
    if evidence.etfs is not None and evidence.etfs.top_gainers:
        opportunity_names = {item.name for item in opportunities}
        matched = [
            item.name
            for item in evidence.etfs.top_gainers
            if _theme_family(item.name) in opportunity_names or "红利" in item.name
        ][:4]
        if matched:
            result.append(
                f"ETF端的相对强势集中在{'、'.join(matched)}，与股票板块的防守抱团方向基本一致。"
            )
    return tuple(result or ["今天没有形成可确认的集中赚钱模式，最好的结论就是不勉强找主线。"])[:4]


def _loss_making_effect(evidence: DailyMarketReviewEvidenceV1) -> tuple[str, ...]:
    result: list[str] = []
    universe = evidence.universe
    if universe is not None:
        deep_loss = _distribution_count(evidence, "down_5_plus")
        if universe.advance_ratio >= 0.6 and universe.median_change_pct > 0:
            result.append(
                f"普涨中仍有局部亏钱区：{universe.down_count}只下跌，其中{deep_loss}只跌幅超过5%，"
                f"但全A中位数{universe.median_change_pct:+.2f}%，亏钱效应不是当天主导。"
            )
        else:
            result.append(
                f"个股亏钱效应是主导项：{universe.down_count}只下跌，其中{deep_loss}只跌幅超过5%，"
                f"全A中位数{universe.median_change_pct:+.2f}%。"
            )
        crowded = [item for item in universe.most_traded if item.change_pct < 0][:4]
        if crowded:
            result.append(
                "高成交品种也在释放亏钱效应："
                + "、".join(f"{item.name}{item.change_pct:+.2f}%" for item in crowded)
                + "。这类拥挤方向若次日继续补跌，市场很难出现有质量的修复。"
            )
    downside = _downside_families(evidence)
    if downside:
        result.append(f"板块层面的主要抛压集中在{'、'.join(downside)}，不是随机个股下跌。")
    if evidence.stock_fund_flow is not None:
        flow = evidence.stock_fund_flow
        ratio = flow.positive_count / flow.scanned_count
        if ratio >= 0.55 and flow.total_net_amount_cny > 0:
            result.append(
                f"资金净流入覆盖达到{ratio * 100:.1f}%，多数股票已有主动承接；"
                "需要防的是高成交核心内部仍在分化。"
            )
        else:
            result.append(
                f"资金净流入覆盖只有{ratio * 100:.1f}%，说明多数股票缺少主动承接，"
                "反弹时也要先看资金是否回流。"
            )
    return tuple(result or ["亏钱效应数据不完整，无法可靠判断风险主要来自哪里。"])[:4]


def _next_day_scenarios(
    outlook: NextDayOutlookV1,
    opportunities: tuple[OpportunitySectorV1, ...],
) -> tuple[NextDayScenarioV2, ...]:
    themes = "、".join(item.name for item in opportunities[:2]) or "今天的强势方向"
    if outlook.bias == OutlookBias.DEFENSIVE:
        return (
            NextDayScenarioV2(
                scenario_id="base",
                label=f"基准剧本：弱势震荡，{themes}内部淘汰",
                trigger=(
                    "10:30前上涨股票仍不足四成、全A中位数没有翻红，"
                    f"而{themes}的前排冲高后出现明显分化。"
                ),
                interpretation="指数即使反抽，也更像超跌后的喘息；存量资金仍在少数防守方向里抱团。",
                response="不追一致高开，先看前排能否承接分歧；其他弱势方向只按反弹看，不急着抄底。",
            ),
            NextDayScenarioV2(
                scenario_id="repair",
                label="修复剧本：成长止跌，个股面真正回暖",
                trigger=(
                    "上涨股票稳定超过一半，创业板不再明显弱于上证，"
                    "并且今天大额净流出的高成交品种止跌。"
                ),
                interpretation="这才说明修复从指数扩散到个股，防守抱团可能让位给更广泛的风险偏好回升。",
                response="等修复条件同时出现后再提高进攻性，优先看有板块配合的核心品种，不追单只脉冲。",
            ),
            NextDayScenarioV2(
                scenario_id="risk",
                label="风险剧本：强势线补跌，亏钱效应继续扩散",
                trigger=(
                    f"上涨股票再次跌到四分之一附近，跌停和大跌家数增加，且{themes}也由强转弱。"
                ),
                interpretation="最后的抱团方向开始松动，市场从普跌分化走向更完整的退潮。",
                response="减少试错，先处理走弱品种；没有新的价格、广度和资金共振前，以防守为主。",
            ),
        )
    if outlook.bias == OutlookBias.CONSTRUCTIVE:
        return (
            NextDayScenarioV2(
                scenario_id="base",
                label=f"基准剧本：强势延续，{themes}接受第一次分歧",
                trigger="上涨股票保持过半、全A中位数红盘，强势板块回落时仍有资金承接。",
                interpretation="赚钱效应仍在扩散，主线从一致上涨转入有承接的良性分歧。",
                response="不追开盘加速，等第一次分歧后的承接确认；只做板块与个股同向的机会。",
            ),
            NextDayScenarioV2(
                scenario_id="repair",
                label="增强剧本：量价齐升，轮动升级为普涨",
                trigger="主要指数同步走强、上涨股票超过六成，成交放大且多个行业获得净流入。",
                interpretation="增量资金接力，行情不再只依赖少数主线。",
                response="可以扩大观察面，但仍避免在情绪高潮处追最一致的品种。",
            ),
            NextDayScenarioV2(
                scenario_id="risk",
                label="风险剧本：指数红、个股绿，强势日证伪",
                trigger="上涨股票跌破一半、全A中位数翻绿，强势板块资金由流入转为流出。",
                interpretation="前一日的强势没有延续，市场重新回到权重支撑下的分化。",
                response="撤回进攻假设，降低追涨频率，等待新的主线和广度重新共振。",
            ),
        )
    return (
        NextDayScenarioV2(
            scenario_id="base",
            label=f"基准剧本：继续轮动，机会仍在{themes}",
            trigger="指数反复、上涨股票在四到六成之间摆动，板块资金快速切换。",
            interpretation="市场没有选出持续方向，局部机会与整体赚钱效应继续分离。",
            response="降低出手频率，只跟踪有价格、广度和资金三者确认的方向。",
        ),
        NextDayScenarioV2(
            scenario_id="repair",
            label="向上剧本：广度扩散，轮动转成进攻",
            trigger="上涨股票稳定超过六成、全A中位数走强，成交和板块净流入同步改善。",
            interpretation="市场从存量轮动进入更有持续性的风险偏好回升。",
            response="顺着最先完成共振的板块观察核心品种，不追脱离板块的单只异动。",
        ),
        NextDayScenarioV2(
            scenario_id="risk",
            label="向下剧本：广度转弱，轮动失败",
            trigger="上涨股票跌破四成、全A中位数持续为负，强势方向也失去资金承接。",
            interpretation="震荡平衡被打破，亏钱效应开始从局部扩散。",
            response="停止把回落当低吸机会，先等广度和资金重新稳定。",
        ),
    )


def _core_conclusion(
    evidence: DailyMarketReviewEvidenceV1,
    opportunities: tuple[OpportunitySectorV1, ...],
) -> str:
    universe = evidence.universe
    changes = _index_changes(evidence)
    weakest = min(changes.values(), key=lambda item: item[1]) if changes else None
    themes = "、".join(item.name for item in opportunities[:3])
    if universe is None:
        return "全A分布缺失，今天只能确认局部强弱，不能把指数涨跌当成完整市场结论。"
    broad_repair = universe.advance_ratio >= 0.6 and universe.median_change_pct > 0
    if broad_repair:
        opening = (
            f"今天最关键的是{universe.up_count}只股票收涨、全A中位数"
            f"{universe.median_change_pct:+.2f}%，个股面完成了明显修复"
        )
        if weakest is not None and weakest[1] < 0:
            opening += f"；虽然{weakest[0]}{weakest[1]:+.2f}%拖累指数，仍没有打断广度扩散"
        if themes:
            opening += f"。{themes}形成了价格、广度和资金相互确认的当日主线"
        else:
            opening += "。市场普遍回暖，但尚未形成经过多类证据确认的集中主线"
    else:
        opening = (
            f"今天最关键的不是某一个指数涨跌，而是{universe.down_count}只股票收跌、"
            f"全A中位数{universe.median_change_pct:+.2f}%"
        )
        if weakest is not None:
            opening += f"，{weakest[0]}{weakest[1]:+.2f}%是主要拖累"
        if themes:
            opening += f"。{themes}撑住了局部赚钱效应，但没有改变全市场偏弱的事实"
        else:
            opening += "。市场没有形成经过多类证据确认的强势主线"
    if evidence.limit_events is not None:
        opening += (
            f"；按当前涨停池，{evidence.limit_events.limit_up_count}只涨停和"
            f"{evidence.limit_events.max_board_count or 1}板高度说明短线资金仍在"
        )
        opening += "，并与普涨广度相互印证" if broad_repair else "，但高度在、广度不在"
    return opening + "。"


def _presentation_headline(
    evidence: DailyMarketReviewEvidenceV1,
    day_character: str,
    opportunities: tuple[OpportunitySectorV1, ...],
) -> str:
    changes = _index_changes(evidence)
    weakest = min(changes.values(), key=lambda item: item[1]) if changes else None
    themes = "、".join(item.name for item in opportunities[:2])
    if "普跌" in day_character or "退潮" in day_character or "弱势" in day_character:
        if weakest is not None and themes:
            return f"{day_character}：{weakest[0]}领跌，{themes}逆势抱团"
        if themes:
            return f"{day_character}：多数股票走弱，{themes}逆势活跃"
        return f"{day_character}：亏钱效应占上风，暂时没有可靠主线"
    if "普涨" in day_character:
        return f"{day_character}：个股普遍回暖" + (f"，{themes}领涨" if themes else "")
    return f"{day_character}：局部有机会，但市场还没有走出一致方向"


def _cell(value: object, tone: CellToneV3 = CellToneV3.NEUTRAL) -> CellV3:
    return CellV3(value=str(value), tone=tone)


def _missing_cell(subject: str | None = None) -> CellV3:
    value = f"{subject}：未取得" if subject else "未取得"
    return _cell(value, CellToneV3.MUTED)


def _number_tone(value: float | None) -> CellToneV3:
    if value is None:
        return CellToneV3.MUTED
    if value > 0:
        return CellToneV3.RISE
    if value < 0:
        return CellToneV3.FALL
    return CellToneV3.FLAT


def _pct_cell(value: float | None) -> CellV3:
    if value is None:
        return _missing_cell()
    return _cell(f"{value:+.2f}%", _number_tone(value))


def _ratio_cell(value: float | None) -> CellV3:
    if value is None:
        return _missing_cell()
    return _cell(f"{value * 100:.1f}%", _number_tone(value - 0.5))


def _cny_cell(value: float | None, *, signed: bool = True) -> CellV3:
    if value is None:
        return _missing_cell()
    formatted = _cny(value)
    if not signed:
        formatted = formatted.lstrip("+")
    return _cell(formatted, _number_tone(value) if signed else CellToneV3.NEUTRAL)


def _overview_section(evidence: DailyMarketReviewEvidenceV1, recap: MarketRecapV1) -> SectionV3:
    snapshot = evidence.market_watch
    index_rows = tuple(
        (
            _cell(item.name),
            _cell(f"{item.level:.2f}" if item.level is not None else "未取得", CellToneV3.MUTED if item.level is None else CellToneV3.NEUTRAL),
            _pct_cell(item.change_pct),
        )
        for item in snapshot.indices
        if item.available
    )
    if not index_rows:
        index_rows = ((_cell("主要指数"), _missing_cell(), _missing_cell()),)

    universe = evidence.universe
    turnover = snapshot.turnover
    if turnover.available and turnover.difference_ratio is not None:
        comparison = _cell(
            f"较上一交易日同一时点{turnover.difference_ratio * 100:+.1f}%",
            _number_tone(turnover.difference_ratio),
        )
    else:
        comparison = _missing_cell("成交额环比")
    turnover_rows = (
        (
            _cell("全A成交额"),
            _cny_cell(universe.total_amount_cny, signed=False) if universe is not None else _missing_cell(),
            comparison,
        ),
        (
            _cell("全A平均涨跌"),
            _pct_cell(universe.mean_change_pct) if universe is not None else _missing_cell(),
            _cell("全A个股等权均值" if universe is not None else "未取得", CellToneV3.MUTED),
        ),
        (
            _cell("全A中位涨跌"),
            _pct_cell(universe.median_change_pct) if universe is not None else _missing_cell(),
            _cell("持仓体感参考" if universe is not None else "未取得", CellToneV3.MUTED),
        ),
    )
    return SectionV3(
        section_id="overview",
        title="市场总览与全天路径",
        summary=recap.summary,
        paragraphs=_session_story(evidence),
        tables=(
            TableV3(
                table_id="index_performance",
                title="主要指数",
                columns=("指数", "收盘", "涨跌幅"),
                rows=index_rows,
            ),
            TableV3(
                table_id="market_turnover",
                title="全A表现与成交",
                columns=("指标", "当前值", "对比或口径"),
                rows=turnover_rows,
            ),
        ),
        notes=(recap.turnover_statement,),
    )


def _reconciliation_section(
    review: PostMarketReviewV1,
    previous: PostMarketReviewV1 | None,
) -> SectionV3:
    if previous is None:
        rows = (
            (
                _cell("上一交易日复盘"),
                _missing_cell(),
                _missing_cell(),
                _cell("没有上一交易日的同口径存档，今天不做事后对账。", CellToneV3.MUTED),
            ),
        )
        summary = "这是当前口径下的首份可比报告，昨日判断与观察池均未取得。"
        notes = ("从下一交易日起，本栏会固定回看前一日的整体判断；板块条件仍需逐项保存后才能自动验收。",)
    else:
        outcome = evaluate_review_outcome(previous, review.evidence)
        verdict_labels = {
            OutcomeVerdict.SUPPORTED: "支持",
            OutcomeVerdict.PARTIAL: "部分支持",
            OutcomeVerdict.NOT_SUPPORTED: "不支持",
            OutcomeVerdict.UNVERIFIABLE: "无法验证",
        }
        rows_list: list[tuple[CellV3, ...]] = [
            (
                _cell("整体形势"),
                _cell(previous.next_day_outlook.thesis),
                _cell(previous.next_day_outlook.confirmation),
                _cell(f"{verdict_labels[outcome.verdict]}：{outcome.summary}"),
            )
        ]
        for item in previous.opportunity_sectors:
            rows_list.append(
                (
                    _cell(item.name),
                    _cell(item.rationale),
                    _cell(item.next_day_confirmation),
                    _cell("未自动核验：当前归档尚未保存逐项触发结果。", CellToneV3.MUTED),
                )
            )
        rows = tuple(rows_list)
        summary = _comparison_statement(review.evidence, previous)
        notes = ("整体方向按下一交易日收盘证据回看；板块观察不会只凭同名板块当日上涨就判定命中。",)
    return SectionV3(
        section_id="reconciliation",
        title="昨日判断回看",
        summary=summary,
        tables=(
            TableV3(
                table_id="previous_plan_check",
                title="前一日结论与本日结果",
                columns=("项目", "昨日判断", "昨日确认条件", "今日验收"),
                rows=rows,
            ),
        ),
        notes=notes,
    )


def _sentiment_section(evidence: DailyMarketReviewEvidenceV1, recap: MarketRecapV1) -> SectionV3:
    universe = evidence.universe
    width_rows = (
        (
            _cell("上涨 / 下跌 / 平盘"),
            _cell(
                f"{universe.up_count} / {universe.down_count} / {universe.flat_count}"
                if universe is not None
                else "未取得",
                CellToneV3.MUTED if universe is None else CellToneV3.NEUTRAL,
            ),
        ),
        (
            _cell("上涨占比"),
            _ratio_cell(universe.advance_ratio) if universe is not None else _missing_cell(),
        ),
        (
            _cell("样本覆盖"),
            _cell(str(universe.scanned_count)) if universe is not None else _missing_cell(),
        ),
    )
    limits = evidence.limit_events
    limit_rows = (
        (
            _cell("涨停家数"),
            _cell(str(limits.limit_up_count)) if limits is not None else _missing_cell(),
        ),
        (
            _cell("跌停家数"),
            _cell(str(limits.limit_down_count))
            if limits is not None and limits.limit_down_count is not None
            else _missing_cell(),
        ),
        (
            _cell("最高连板"),
            _cell(f"{limits.max_board_count}板")
            if limits is not None and limits.max_board_count is not None
            else _missing_cell(),
        ),
        (_cell("炸板率"), _missing_cell()),
        (_cell("昨日涨停反馈"), _missing_cell()),
    )
    ladder_rows = tuple(
        (_cell(f"{item.board_count}板"), _cell(str(item.count)))
        for item in (limits.board_heights if limits is not None else ())
    )
    if not ladder_rows:
        ladder_rows = ((_cell("连板梯队"), _missing_cell()),)
    distribution_rows = tuple(
        (_cell(item.label), _cell(str(item.count)))
        for item in (universe.distribution if universe is not None else ())
    )
    if not distribution_rows:
        distribution_rows = ((_cell("涨跌分布"), _missing_cell()),)
    return SectionV3(
        section_id="sentiment",
        title="市场宽度与短线情绪",
        summary=recap.sentiment_statement,
        paragraphs=(_money_making_effect(evidence, ())[0], _loss_making_effect(evidence)[0]),
        tables=(
            TableV3(
                table_id="market_breadth",
                title="市场宽度",
                columns=("指标", "值"),
                rows=width_rows,
            ),
            TableV3(
                table_id="limit_summary",
                title="涨跌停概况",
                columns=("指标", "值"),
                rows=limit_rows,
            ),
            TableV3(
                table_id="limit_ladder",
                title="连板梯队",
                columns=("高度", "家数"),
                rows=ladder_rows,
            ),
            TableV3(
                table_id="change_distribution",
                title="全A涨跌分布",
                columns=("区间", "家数"),
                rows=distribution_rows,
            ),
        ),
        notes=("炸板率和昨日涨停反馈不在现有 evidence 合同中，均明确标记为未取得。",),
    )


def _sector_table(
    summary: object | None,
    *,
    table_id: str,
    title: str,
    sector_label: str,
) -> TableV3:
    rows: list[tuple[CellV3, ...]] = []
    seen: set[tuple[str, str]] = set()
    groups = (
        ("领涨", getattr(summary, "top_gainers", ())),
        ("领跌", getattr(summary, "top_losers", ())),
        ("资金流入", getattr(summary, "top_inflows", ())),
        ("资金流出", getattr(summary, "top_outflows", ())),
    )
    for role, items in groups:
        for item in items[:5]:
            identity = (role, item.sector_key)
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(
                (
                    _cell(role),
                    _cell(item.name),
                    _pct_cell(item.change_pct),
                    _cny_cell(item.main_net_inflow_cny),
                    _ratio_cell(item.breadth_ratio),
                    _cell(item.leader_name or "未取得", CellToneV3.MUTED if not item.leader_name else CellToneV3.NEUTRAL),
                    _pct_cell(item.leader_change_pct),
                )
            )
    if not rows:
        rows.append(
            (
                _cell(sector_label),
                _missing_cell(),
                _missing_cell(),
                _missing_cell(),
                _missing_cell(),
                _missing_cell(),
                _missing_cell(),
            )
        )
    return TableV3(
        table_id=table_id,
        title=title,
        columns=("角色", "板块", "涨跌幅", "主力净额", "上涨覆盖", "龙头", "龙头涨跌"),
        rows=tuple(rows),
    )


def _sectors_section(evidence: DailyMarketReviewEvidenceV1, recap: MarketRecapV1) -> SectionV3:
    return SectionV3(
        section_id="sectors",
        title="行业与概念",
        summary=recap.rotation_statement,
        tables=(
            _sector_table(
                evidence.industry_sectors,
                table_id="industry_sectors",
                title="行业前后排",
                sector_label="行业",
            ),
            _sector_table(
                evidence.concept_sectors,
                table_id="concept_sectors",
                title="概念前后排",
                sector_label="概念",
            ),
        ),
        notes=("板块主力净额沿用现有 evidence 的供应商口径，不等同于账户资金净申购。",),
    )


def _stocks_section(evidence: DailyMarketReviewEvidenceV1) -> SectionV3:
    universe = evidence.universe
    hot_rows = tuple(
        (
            _cell(item.instrument_id),
            _cell(item.name),
            _pct_cell(item.change_pct),
            _cny_cell(item.amount_cny, signed=False),
            _pct_cell(item.turnover_pct),
        )
        for item in (universe.most_traded if universe is not None else ())
    )
    if not hot_rows:
        hot_rows = (
            (_cell("热门成交股"), _missing_cell(), _missing_cell(), _missing_cell(), _missing_cell()),
        )

    mover_rows: list[tuple[CellV3, ...]] = []
    if universe is not None:
        for role, items in (("涨幅前排", universe.top_gainers), ("跌幅前排", universe.top_losers)):
            for item in items[:8]:
                mover_rows.append(
                    (
                        _cell(role),
                        _cell(item.instrument_id),
                        _cell(item.name),
                        _pct_cell(item.change_pct),
                        _cny_cell(item.amount_cny, signed=False),
                        _pct_cell(item.turnover_pct),
                    )
                )
    if not mover_rows:
        mover_rows.append(
            (_cell("涨跌幅前排"), _missing_cell(), _missing_cell(), _missing_cell(), _missing_cell(), _missing_cell())
        )

    limits = evidence.limit_events
    event_rows = tuple(
        (
            _cell(item.instrument_id),
            _cell(item.name),
            _cell(f"{item.board_count}板" if item.board_count is not None else "未取得", CellToneV3.MUTED if item.board_count is None else CellToneV3.NEUTRAL),
            _cell(item.reason),
            _cell(item.first_sealed_at or "未取得", CellToneV3.MUTED if not item.first_sealed_at else CellToneV3.NEUTRAL),
            _cell(str(item.open_count)) if item.open_count is not None else _missing_cell(),
            _cny_cell(item.order_amount_cny, signed=False),
        )
        for item in (limits.representative_events if limits is not None else ())
    )
    if not event_rows:
        event_rows = (
            (
                _cell("涨停代表股"),
                _missing_cell(),
                _missing_cell(),
                _missing_cell(),
                _missing_cell(),
                _missing_cell(),
                _missing_cell(),
            ),
        )
    return SectionV3(
        section_id="stocks",
        title="成交热门股与涨停代表",
        summary="按全A成交额和现有涨停池展示代表个股，不把局部样本扩写成完整市场名单。",
        tables=(
            TableV3(
                table_id="most_traded_stocks",
                title="成交额热门股",
                columns=("代码", "名称", "涨跌幅", "成交额", "换手率"),
                rows=hot_rows,
            ),
            TableV3(
                table_id="stock_movers",
                title="涨跌幅前排",
                columns=("角色", "代码", "名称", "涨跌幅", "成交额", "换手率"),
                rows=tuple(mover_rows),
            ),
            TableV3(
                table_id="representative_limit_events",
                title="涨停池代表股",
                columns=("代码", "名称", "连板", "原因", "首次封板", "开板次数", "封单额"),
                rows=event_rows,
            ),
        ),
    )


def _etfs_section(evidence: DailyMarketReviewEvidenceV1, recap: MarketRecapV1) -> SectionV3:
    etfs = evidence.etfs
    summary_rows = (
        (
            _cell("上涨 / 下跌 / 平盘"),
            _cell(f"{etfs.up_count} / {etfs.down_count} / {etfs.flat_count}")
            if etfs is not None
            else _missing_cell(),
        ),
        (
            _cell("ETF中位涨跌"),
            _pct_cell(etfs.median_change_pct) if etfs is not None else _missing_cell(),
        ),
        (
            _cell("ETF成交额"),
            _cny_cell(etfs.total_amount_cny, signed=False) if etfs is not None else _missing_cell(),
        ),
        (_cell("ETF净申购 / 赎回"), _missing_cell()),
        (_cell("ETF折溢价"), _missing_cell()),
    )
    leaders: list[tuple[str, object]] = []
    seen: set[str] = set()
    if etfs is not None:
        for role, items in (("领涨", etfs.top_gainers), ("成交活跃", etfs.most_traded), ("领跌", etfs.top_losers)):
            for item in items[:6]:
                identity = f"{role}:{item.instrument_id}"
                if identity in seen:
                    continue
                seen.add(identity)
                leaders.append((role, item))
    leader_rows = tuple(
        (
            _cell(role),
            _cell(item.instrument_id),
            _cell(item.name),
            _pct_cell(item.change_pct),
            _cny_cell(item.amount_cny, signed=False),
            _pct_cell(item.turnover_pct),
        )
        for role, item in leaders
    )
    if not leader_rows:
        leader_rows = (
            (_cell("ETF代表"), _missing_cell(), _missing_cell(), _missing_cell(), _missing_cell(), _missing_cell()),
        )
    return SectionV3(
        section_id="etfs",
        title="ETF表现",
        summary=recap.etf_statement,
        tables=(
            TableV3(
                table_id="etf_summary",
                title="ETF整体",
                columns=("指标", "值"),
                rows=summary_rows,
            ),
            TableV3(
                table_id="etf_leaders",
                title="ETF代表",
                columns=("角色", "代码", "名称", "涨跌幅", "成交额", "换手率"),
                rows=leader_rows,
            ),
        ),
        notes=("现有 evidence 只有 ETF 行情摘要，净申购/赎回和折溢价均标记为未取得。",),
    )


def _flows_section(evidence: DailyMarketReviewEvidenceV1, recap: MarketRecapV1) -> SectionV3:
    flows = evidence.stock_fund_flow
    flow_summary_rows = (
        (
            _cell("净流入 / 净流出 / 持平"),
            _cell(f"{flows.positive_count} / {flows.negative_count} / {flows.flat_count}")
            if flows is not None
            else _missing_cell(),
        ),
        (
            _cell("个股资金净额合计"),
            _cny_cell(flows.total_net_amount_cny) if flows is not None else _missing_cell(),
        ),
        (
            _cell("大单资金净额合计"),
            _cny_cell(flows.total_large_net_amount_cny) if flows is not None else _missing_cell(),
        ),
    )
    mover_rows: list[tuple[CellV3, ...]] = []
    if flows is not None:
        for direction, items in (("流入", flows.top_inflows), ("流出", flows.top_outflows)):
            for item in items[:10]:
                mover_rows.append(
                    (
                        _cell(direction),
                        _cell(item.instrument_id),
                        _cell(item.name or "未取得", CellToneV3.MUTED if not item.name else CellToneV3.NEUTRAL),
                        _cny_cell(item.net_amount_cny),
                        _cny_cell(item.large_net_amount_cny),
                        _cny_cell(item.extra_large_net_amount_cny),
                    )
                )
    if not mover_rows:
        mover_rows.append(
            (_cell("个股资金"), _missing_cell(), _missing_cell(), _missing_cell(), _missing_cell(), _missing_cell())
        )

    dragon = evidence.dragon_tiger
    dragon_rows: list[tuple[CellV3, ...]] = []
    if dragon is not None:
        aggregated: dict[str, dict[str, object]] = {}
        seen_trades: set[tuple[str, float, float, str | None]] = set()
        for item in (*dragon.top_net_buys, *dragon.top_net_sells):
            identity = (item.instrument_id, item.buy_amount_cny, item.sell_amount_cny, item.reason)
            if identity in seen_trades:
                continue
            seen_trades.add(identity)
            record = aggregated.setdefault(
                item.instrument_id,
                {
                    "name": item.name,
                    "change_pct": item.change_pct,
                    "buy": 0.0,
                    "sell": 0.0,
                    "reasons": [],
                },
            )
            record["buy"] = float(record["buy"]) + item.buy_amount_cny
            record["sell"] = float(record["sell"]) + item.sell_amount_cny
            if item.reason and item.reason not in record["reasons"]:
                record["reasons"].append(item.reason)
        ranked_dragon = sorted(
            aggregated.items(),
            key=lambda pair: abs(float(pair[1]["buy"]) - float(pair[1]["sell"])),
            reverse=True,
        )[:15]
        for instrument_id, record in ranked_dragon:
            buy = float(record["buy"])
            sell = float(record["sell"])
            net = buy - sell
            reasons = "；".join(str(value) for value in record["reasons"][:2])
            dragon_rows.append(
                (
                    _cell("净买入" if net > 0 else "净卖出" if net < 0 else "持平"),
                    _cell(instrument_id),
                    _cell(str(record["name"])),
                    _pct_cell(float(record["change_pct"])),
                    _cny_cell(net),
                    _cny_cell(buy, signed=False),
                    _cny_cell(sell, signed=False),
                    _cell(reasons or "未取得", CellToneV3.MUTED if not reasons else CellToneV3.NEUTRAL),
                )
            )
    if not dragon_rows:
        dragon_rows.append(
            (
                _cell("龙虎榜"),
                _missing_cell(),
                _missing_cell(),
                _missing_cell(),
                _missing_cell(),
                _missing_cell(),
                _missing_cell(),
                _missing_cell(),
            )
        )
    return SectionV3(
        section_id="flows",
        title="资金流与龙虎榜",
        summary=f"{recap.stock_fund_flow_statement}{recap.dragon_tiger_statement}",
        tables=(
            TableV3(
                table_id="stock_flow_summary",
                title="个股资金概况",
                columns=("指标", "值"),
                rows=flow_summary_rows,
            ),
            TableV3(
                table_id="stock_flow_movers",
                title="个股资金前排",
                columns=("方向", "代码", "名称", "净额", "大单净额", "超大单净额"),
                rows=tuple(mover_rows),
            ),
            TableV3(
                table_id="dragon_tiger",
                title="龙虎榜",
                columns=("方向", "代码", "名称", "涨跌幅", "净额", "买入", "卖出", "上榜原因"),
                rows=tuple(dragon_rows),
            ),
        ),
        notes=(
            "个股主力资金与龙虎榜分别保留现有数据口径，不互相替代。",
            "龙虎榜按代码合并重复席位记录后展示；它只覆盖达到披露条件的交易，不代表全市场机构持仓。",
        ),
    )


def _tomorrow_section(
    evidence: DailyMarketReviewEvidenceV1,
    outlook: NextDayOutlookV1,
    scenarios: tuple[NextDayScenarioV2, ...],
    opportunities: tuple[OpportunitySectorV1, ...],
) -> SectionV3:
    scenario_rows = tuple(
        (
            _cell(item.label),
            _cell(item.trigger),
            _cell(item.interpretation),
            _cell(item.response),
        )
        for item in scenarios
    )
    opportunity_rows = tuple(
        (
            _cell(item.name),
            _cell("多证据确认" if item.conviction == OpportunityConviction.CONFIRMED else "观察"),
            _cell("；".join(item.evidence_chain)),
            _cell(item.next_day_confirmation),
            _cell(item.invalidation),
        )
        for item in opportunities
    )
    if not opportunity_rows:
        opportunity_rows = (
            (_cell("机会方向"), _missing_cell(), _missing_cell(), _missing_cell(), _missing_cell()),
        )
    avoid_rows: list[tuple[CellV3, ...]] = []
    if evidence.industry_sectors is not None:
        seen_avoid: set[str] = set()
        candidates = (*evidence.industry_sectors.top_outflows, *evidence.industry_sectors.top_losers)
        for item in candidates:
            if item.name in seen_avoid or item.change_pct >= 0:
                continue
            seen_avoid.add(item.name)
            avoid_rows.append(
                (
                    _cell(item.name),
                    _pct_cell(item.change_pct),
                    _cny_cell(item.main_net_inflow_cny),
                    _cell("当天价格与资金同时偏弱，不把一次反抽直接当成反转。"),
                    _cell("板块翻红、上涨覆盖回到一半以上，并出现资金净流入。"),
                )
            )
            if len(avoid_rows) >= 5:
                break
    if not avoid_rows:
        avoid_rows.append(
            (_cell("回避方向"), _missing_cell(), _missing_cell(), _missing_cell(), _missing_cell())
        )
    catalyst_rows = ((_cell("可靠新闻催化"), _missing_cell()),)
    return SectionV3(
        section_id="tomorrow",
        title="明日观察",
        summary=outlook.thesis,
        tables=(
            TableV3(
                table_id="next_day_scenarios",
                title="条件化剧本",
                columns=("情景", "确认条件", "含义", "应对"),
                rows=scenario_rows,
            ),
            TableV3(
                table_id="opportunity_watch",
                title="机会方向观察",
                columns=("方向", "级别", "今日证据", "明日确认", "失效条件"),
                rows=opportunity_rows,
            ),
            TableV3(
                table_id="avoid_watch",
                title="次日回避清单",
                columns=("方向", "今日涨跌", "今日主力净额", "回避理由", "解除条件"),
                rows=tuple(avoid_rows),
            ),
            TableV3(
                table_id="news_catalysts",
                title="催化证据",
                columns=("项目", "状态"),
                rows=catalyst_rows,
            ),
        ),
        notes=("明日部分只给确认和失效条件；现有 evidence 没有可靠新闻源，不能补写新闻催化。",),
    )


def _methodology_section(review: PostMarketReviewV1) -> SectionV3:
    evidence = review.evidence
    component_rows = tuple(
        (
            _cell(item.component),
            _cell(item.status.value),
            _cell(str(item.record_count)),
            _cell(item.provider or "未取得", CellToneV3.MUTED if not item.provider else CellToneV3.NEUTRAL),
            _cell(
                item.provider_as_of.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")
                if item.provider_as_of is not None
                else "未取得",
                CellToneV3.MUTED if item.provider_as_of is None else CellToneV3.NEUTRAL,
            ),
            _cell("、".join(item.flags) if item.flags else "无", CellToneV3.MUTED),
        )
        for item in evidence.components
    )
    turnover = evidence.market_watch.turnover
    turnover_disclosure = (
        f"已取得：较上一交易日同一时点{turnover.difference_ratio * 100:+.1f}%"
        if turnover.available and turnover.difference_ratio is not None
        else "未取得"
    )
    disclosure_rows = (
        (_cell("炸板率"), _missing_cell()),
        (_cell("昨日涨停反馈"), _missing_cell()),
        (
            _cell("成交额环比"),
            _cell(
                turnover_disclosure,
                _number_tone(turnover.difference_ratio)
                if turnover.available and turnover.difference_ratio is not None
                else CellToneV3.MUTED,
            ),
        ),
        (_cell("可靠新闻催化"), _missing_cell()),
    )
    notes = tuple(
        dict.fromkeys(
            (
                f"不可变事实合同：{review.contract}；文章展示合同：post_market_review_presentation.v4。",
                f"源快照：{review.source_snapshot_id}，截至{review.source_snapshot_as_of.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M')}。",
                "所有未由现有 evidence 提供的指标都写明未取得，不以推断值填充。",
                *review.limitations,
            )
        )
    )
    return SectionV3(
        section_id="methodology",
        title="口径与数据质量",
        summary="展示层只重排不可变 review.v1 证据，不修改归档事实，也不补造缺失字段。",
        tables=(
            TableV3(
                table_id="evidence_components",
                title="证据组件",
                columns=("组件", "状态", "记录数", "来源", "数据截至", "质量标记"),
                rows=component_rows,
            ),
            TableV3(
                table_id="missing_disclosures",
                title="关键字段披露",
                columns=("字段", "状态或口径"),
                rows=disclosure_rows,
            ),
        ),
        notes=notes,
    )


def _presentation_sections_v3(
    review: PostMarketReviewV1,
    *,
    previous: PostMarketReviewV1 | None,
    recap: MarketRecapV1,
    outlook: NextDayOutlookV1,
    scenarios: tuple[NextDayScenarioV2, ...],
    opportunities: tuple[OpportunitySectorV1, ...],
) -> tuple[SectionV3, ...]:
    evidence = review.evidence
    return (
        _overview_section(evidence, recap),
        _reconciliation_section(review, previous),
        _sentiment_section(evidence, recap),
        _sectors_section(evidence, recap),
        _stocks_section(evidence),
        _etfs_section(evidence, recap),
        _flows_section(evidence, recap),
        _tomorrow_section(evidence, outlook, scenarios, opportunities),
        _methodology_section(review),
    )


def _article_clock(value: datetime | None) -> str:
    if value is None:
        return "盘中"
    return value.astimezone(SHANGHAI).strftime("%H:%M")


def _article_broad_repair(evidence: DailyMarketReviewEvidenceV1) -> bool:
    universe = evidence.universe
    return bool(
        universe is not None
        and universe.advance_ratio >= 0.6
        and universe.median_change_pct > 0
    )


def _article_title(
    evidence: DailyMarketReviewEvidenceV1,
    opportunities: tuple[OpportunitySectorV1, ...],
    quality: ReviewQuality,
) -> str:
    if quality == ReviewQuality.ABSTAINED:
        return "今天的数据不够完整，这份复盘先不下方向结论"
    intraday = evidence.intraday
    weakest = min(_index_changes(evidence).values(), key=lambda item: item[1], default=None)
    themes = "、".join(item.name for item in opportunities[:2])
    if (
        intraday is not None
        and intraday.max_advance_ratio is not None
        and intraday.min_advance_ratio is not None
        and intraday.max_advance_ratio - intraday.min_advance_ratio >= 0.25
    ):
        improved_from_low = (
            intraday.weakest_as_of is not None
            and intraday.strongest_as_of is not None
            and intraday.weakest_as_of < intraday.strongest_as_of
        )
        ending = ""
        if weakest is not None and weakest[1] < 0:
            ending = f"，{weakest[0]}跌{abs(weakest[1]):.2f}%"
        if themes:
            ending += f"，{themes}{'领涨' if improved_from_low else '成了避风港'}"
        if improved_from_low:
            return (
                f"开盘仅{intraday.min_advance_ratio * 100:.0f}%个股上涨，"
                f"午后扩散到{intraday.max_advance_ratio * 100:.0f}%{ending}"
            )
        return (
            f"早盘还有{intraday.max_advance_ratio * 100:.0f}%个股上涨，"
            f"午后最低只剩{intraday.min_advance_ratio * 100:.0f}%{ending}"
        )
    character = _day_character(evidence, None)
    return _presentation_headline(evidence, character, opportunities)


def _article_standfirst(
    evidence: DailyMarketReviewEvidenceV1,
    opportunities: tuple[OpportunitySectorV1, ...],
    quality: ReviewQuality,
) -> str:
    if quality == ReviewQuality.ABSTAINED:
        return "这份档案仍会保留已经取得的盘面事实，但关键证据没有达到完整复盘门槛。正文可以用来了解今天发生了什么，明日方向暂不据此下结论。"
    universe = evidence.universe
    themes = "、".join(item.name for item in opportunities[:3])
    if universe is None:
        return "今天的全市场个股分布没有完整取得，正文只写能够被现有证据确认的盘面变化，不拿指数替代个股体感。"
    if _article_broad_repair(evidence):
        text = (
            f"今天不能只看指数涨跌不一。收盘有{universe.up_count}只股票上涨，"
            f"全A中位数{universe.median_change_pct:+.2f}%，多数持仓体感是明显回血。"
        )
        if themes:
            text += f"{themes}成为资金最集中的进攻方向，但成交缩量意味着明天仍要接受第一次分歧检验。"
    else:
        text = (
            f"今天不是“指数跌了多少”这么简单。收盘有{universe.down_count}只股票下跌，"
            f"全A中位数{universe.median_change_pct:+.2f}%，说明多数持仓承受的是一场实打实的回撤。"
        )
        if themes:
            text += f"{themes}守住了局部赚钱效应，但它们更像资金撤退时寻找的落脚点，还不是新一轮普涨的起点。"
    return text


def _article_session_section(evidence: DailyMarketReviewEvidenceV1) -> ArticleSectionV4:
    paragraphs: list[str] = []
    intraday = evidence.intraday
    broad_repair = _article_broad_repair(evidence)
    if (
        intraday is not None
        and intraday.max_advance_ratio is not None
        and intraday.min_advance_ratio is not None
        and intraday.last_advance_ratio is not None
    ):
        improved_from_low = (
            intraday.weakest_as_of is not None
            and intraday.strongest_as_of is not None
            and intraday.weakest_as_of < intraday.strongest_as_of
            and intraday.last_advance_ratio >= 0.55
        )
        if improved_from_low:
            path = (
                f"{_article_clock(intraday.weakest_as_of)}上涨股票最低只有{intraday.min_advance_ratio * 100:.1f}%，"
            )
            if intraday.morning_close_advance_ratio is not None:
                path += f"到了午间已经回到{intraday.morning_close_advance_ratio * 100:.1f}%；"
            path += (
                f"{_article_clock(intraday.strongest_as_of)}最高升到{intraday.max_advance_ratio * 100:.1f}%，"
                f"收盘仍有{intraday.last_advance_ratio * 100:.1f}%。"
                "这不是指数硬撑出来的假反弹，而是个股参与度从开盘低点一路扩散。"
            )
        else:
            path = (
                f"{_article_clock(intraday.strongest_as_of)}是全天最强的一刻，"
                f"一度有{intraday.max_advance_ratio * 100:.1f}%的股票上涨。"
            )
            if intraday.morning_close_advance_ratio is not None:
                path += f"到了午间，上涨占比已经降到{intraday.morning_close_advance_ratio * 100:.1f}%；"
            path += (
                f"{_article_clock(intraday.weakest_as_of)}最低只剩{intraday.min_advance_ratio * 100:.1f}%，"
                f"收盘也只有{intraday.last_advance_ratio * 100:.1f}%。"
                + (
                    "盘中虽然反复，收盘仍是多数股票上涨，参与度没有被局部回落打散。"
                    if broad_repair
                    else "全天最值得记住的不是尾盘有没有反抽，而是早盘的普遍尝试最终变成了午后的普遍撤退。"
                )
            )
        paragraphs.append(path)
    else:
        paragraphs.append(
            "盘中上涨家数轨迹没有完整取得，因此今天不编造早盘、午后和尾盘的细节；只能从收盘证据判断市场最终偏弱还是偏强。"
        )

    changes = list(_index_changes(evidence).values())
    index_text = "、".join(f"{name}{change:+.2f}%" for name, change in changes)
    universe = evidence.universe
    close = f"收盘看，{index_text}。" if index_text else "主要指数收盘数据没有完整取得。"
    if universe is not None:
        close += (
            f"全A成交约{universe.total_amount_cny / 100_000_000:.0f}亿元，"
            f"{universe.up_count}只上涨、{universe.down_count}只下跌、{universe.flat_count}只平盘。"
        )
    turnover = evidence.market_watch.turnover
    if not turnover.available:
        close += "上一交易日同一时点的成交额没有取到，所以不能把今天说成放量杀跌或缩量回调。"
    paragraphs.append(close)
    return ArticleSectionV4(
        section_id="session",
        title="今天是怎么走强的" if broad_repair else "今天是怎么走坏的",
        paragraphs=tuple(paragraphs),
    )


def _article_mainline_section(
    evidence: DailyMarketReviewEvidenceV1,
    opportunities: tuple[OpportunitySectorV1, ...],
) -> ArticleSectionV4:
    paragraphs: list[str] = []
    broad_repair = _article_broad_repair(evidence)
    if opportunities:
        parts: list[str] = []
        for item in opportunities[:3]:
            detail = item.name
            if item.change_pct is not None:
                detail += f"涨{item.change_pct:.2f}%"
            if item.breadth_ratio is not None:
                detail += f"，{item.breadth_ratio * 100:.0f}%的成分股收红"
            if item.main_net_inflow_cny is not None:
                detail += f"，资金净流入{abs(item.main_net_inflow_cny) / 100_000_000:.1f}亿元"
            if item.leader_name:
                detail += f"，前排是{item.leader_name}"
            parts.append(detail)
        if broad_repair:
            paragraphs.append(
                "今天的强势不是靠一两只股票硬拉，而是几条能够由板块涨幅、内部广度和资金互相印证的进攻线："
                + "；".join(parts)
                + "。它们和全A普遍上涨同时出现，说明赚钱效应已经扩散；创业板仍弱、成交缩量，则说明这还不是毫无分歧的全面进攻。"
            )
        else:
            paragraphs.append(
                "逆势最硬的不是一个含糊的“防守”标签，而是几条能够由板块涨幅、内部广度和资金互相印证的线："
                + "；".join(parts)
                + "。它们解释了今天的钱往哪里躲，却还不能把整个市场带强——因为个股面并没有跟上。"
            )
    else:
        paragraphs.append("今天没有出现能被板块涨幅、内部广度和资金同时确认的主线，硬找一个热门题材只会把偶然脉冲当成趋势。")

    limits = evidence.limit_events
    if limits is not None and limits.representative_events:
        representatives = "、".join(
            f"{item.name}{item.board_count}板" if item.board_count else item.name
            for item in limits.representative_events[:5]
        )
        if broad_repair:
            paragraphs.append(
                f"短线前排还有{representatives}。高标分散在不同题材，首板数量同时增加，"
                "说明活跃资金不仅维护高度，也开始向更多方向试错；明天要看这种扩散能否承受分歧。"
            )
        else:
            paragraphs.append(
                f"短线这边并没有完全熄火，前排还有{representatives}。"
                "但这些高标分散在不同题材里，说明活跃资金仍愿意做少数辨识度品种，并不等于主线已经带动全市场。"
            )
    paragraphs.append(
        "本档案能确认的是盘面结构，不能确认当天上涨背后的全部新闻原因；没有进入可靠新闻证据的催化，不会在复盘里补故事。"
    )
    return ArticleSectionV4(
        section_id="mainline",
        title="主线开始扩散，但指数仍有分化" if broad_repair else "逆势主线有，但不是全面进攻",
        paragraphs=tuple(paragraphs[:4]),
    )


def _article_payoff_section(evidence: DailyMarketReviewEvidenceV1) -> ArticleSectionV4:
    paragraphs: list[str] = []
    limits = evidence.limit_events
    universe = evidence.universe
    broad_repair = _article_broad_repair(evidence)
    if limits is not None:
        first_boards = next(
            (item.count for item in limits.board_heights if item.board_count == 1),
            None,
        )
        ladder = (
            f"{limits.limit_up_count}只涨停里有{first_boards}只是首板，二板以上仍集中在少数前排"
            if first_boards is not None
            else "涨停数量不少，但真正站上连板梯队的仍是少数前排"
        )
        if broad_repair:
            paragraphs.append(
                f"今天的赚钱效应不只在高标股。涨停{limits.limit_up_count}只、"
                f"跌停{limits.limit_down_count if limits.limit_down_count is not None else '未取得'}只、最高{limits.max_board_count or 1}板；"
                f"{ladder}。首板扩散与多数个股上涨同时出现，说明风险偏好确实回暖。"
            )
        else:
            paragraphs.append(
                f"今天能赚钱的人，主要集中在逆势板块和少数高标股。涨停{limits.limit_up_count}只、"
                f"跌停{limits.limit_down_count if limits.limit_down_count is not None else '未取得'}只、最高{limits.max_board_count or 1}板，"
                f"说明短线高度还在；但{ladder}，赚钱效应没有大面积铺开。"
            )
    if universe is not None:
        deep_loss = _distribution_count(evidence, "down_5_plus")
        crowded = [item for item in universe.most_traded if item.change_pct < 0][:5]
        names = "、".join(f"{item.name}{item.change_pct:+.2f}%" for item in crowded)
        paragraph = (
            f"普涨并不等于没有亏钱区：{universe.down_count}只股票收跌，其中{deep_loss}只跌幅超过5%。"
            if broad_repair
            else f"更普遍的体感在另一边：{universe.down_count}只股票收跌，其中{deep_loss}只跌幅超过5%。"
        )
        if names:
            paragraph += f"成交最拥挤的一批股票里，{names}都在下跌，亏钱效应集中在原本最热门的成长和科技方向。"
        paragraphs.append(paragraph)
    if evidence.etfs is not None and evidence.etfs.top_losers:
        etf_losers = evidence.etfs.top_losers[:4]
        losers = "、".join(f"{item.name}{item.change_pct:+.2f}%" for item in etf_losers)
        loss_families = "、".join(dict.fromkeys(_theme_family(item.name) for item in etf_losers))
        paragraphs.append(
            f"ETF把这种分化照得更清楚：{losers}排在跌幅前列。"
            f"也就是说，今天的主要亏钱区集中在{loss_families or '这些方向'}，并不是所有板块都跟着个股普涨。"
        )
    return ArticleSectionV4(
        section_id="payoff",
        title="谁赚到了钱，谁最难受",
        paragraphs=tuple(paragraphs or ["个股、涨停和ETF证据不完整，今天无法可靠拆分赚钱与亏钱效应。"]),
    )


def _article_flow_section(
    evidence: DailyMarketReviewEvidenceV1,
    opportunities: tuple[OpportunitySectorV1, ...],
) -> ArticleSectionV4:
    paragraphs: list[str] = []
    flow = evidence.stock_fund_flow
    if flow is not None:
        ratio = flow.positive_count / flow.scanned_count
        outflows = "、".join((item.name or item.instrument_id) for item in flow.top_outflows[:5])
        prefix = "" if ratio >= 0.55 and flow.total_net_amount_cny > 0 else "只有"
        conclusion = (
            "资金已经回到多数股票；净流出榜仍集中在部分高成交核心，说明普涨内部还存在明显换仓。"
            if ratio >= 0.55 and flow.total_net_amount_cny > 0
            else "这不是资金普遍回流，而是从高拥挤方向撤出后，向少数权重和防守资产重新落位。"
        )
        paragraphs.append(
            f"个股资金流给出的答案很直接：{prefix}{ratio * 100:.1f}%的股票录得净流入，"
            f"全市场合计{_plain_net_flow(flow.total_net_amount_cny)}。"
            + (f"净流出最集中的包括{outflows}。" if outflows else "")
            + conclusion
        )
    etfs = evidence.etfs
    if etfs is not None and etfs.top_gainers:
        opportunity_families = {item.name for item in opportunities}
        if "贵金属" in opportunity_families:
            opportunity_families.add("有色金属")
        matched = [
            item
            for item in etfs.top_gainers
            if _theme_family(item.name) in opportunity_families
        ][:5]
        matched_opportunity = bool(matched)
        if not matched:
            matched = list(etfs.top_gainers[:3])
        leaders = "、".join(
            f"{item.name}{item.change_pct:+.2f}%" for item in matched
        )
        paragraphs.append(
            (
                f"ETF端的相对强势也对得上：{leaders}。这些方向和股票板块的强势线能够互相印证，"
                "不是单一数据源制造的假象。"
                if matched_opportunity
                else f"ETF端走强的是另一条线：{leaders}。它和股票主线并不完全重合，说明资金仍在快速轮动，"
                "还没有收敛成唯一共识。"
            )
        )
    dragon = evidence.dragon_tiger
    if dragon is not None:
        names = "、".join(item.name for item in dragon.top_net_buys[:4])
        broad_inflow = bool(flow is not None and flow.positive_count / flow.scanned_count >= 0.55 and flow.total_net_amount_cny > 0)
        paragraphs.append(
            f"龙虎榜共有{dragon.listed_count}条，合计{_plain_net_flow(dragon.total_net_amount_cny)}；"
            f"净买入靠前的是{names or '未形成集中方向'}。"
            + (
                "龙虎榜和全市场资金流同向转正，为当天修复增加了一层确认。"
                if broad_inflow
                else "局部游资和机构席位仍在出手，但这只能说明少数票还有交易热度，不能抵消全市场资金面偏弱。"
            )
        )
    return ArticleSectionV4(
        section_id="flow",
        title="资金去哪儿了",
        paragraphs=tuple(paragraphs or ["资金流、ETF和龙虎榜证据没有完整取得，今天不对资金去向作强判断。"]),
    )


def _article_tomorrow_section(
    outlook: NextDayOutlookV1,
    scenarios: tuple[NextDayScenarioV2, ...],
) -> ArticleSectionV4:
    scenario_map = {item.scenario_id: item for item in scenarios}
    repair = scenario_map["repair"]
    risk = scenario_map["risk"]
    constructive = outlook.bias == OutlookBias.CONSTRUCTIVE
    return ArticleSectionV4(
        section_id="tomorrow",
        title="明天怎么看",
        paragraphs=(
            f"我的基准判断是：{outlook.thesis}{outlook.expected_shape}{outlook.risk_control}",
            f"{'什么情况会让强势进一步升级？' if constructive else '什么情况才算真正修复？'}{repair.trigger}{repair.interpretation}{repair.response}",
            f"{'什么情况说明今天的强势被证伪？' if constructive else '什么情况说明风险还没出清？'}{risk.trigger}{risk.interpretation}{risk.response}",
        ),
    )


def _article_reconciliation_section(
    review: PostMarketReviewV1,
    previous: PostMarketReviewV1 | None,
) -> ArticleSectionV4:
    if previous is None:
        paragraphs = (
            "这是当前口径下的第一份可比存档，没有上一交易日的原始判断可以核对。今天不做事后解释；从下一份开始，先把昨天的判断摆出来，再看今天到底支持、部分支持还是证伪。",
        )
    else:
        outcome = evaluate_review_outcome(previous, review.evidence)
        labels = {
            OutcomeVerdict.SUPPORTED: "基本兑现",
            OutcomeVerdict.PARTIAL: "只兑现了一部分",
            OutcomeVerdict.NOT_SUPPORTED: "没有兑现",
            OutcomeVerdict.UNVERIFIABLE: "现有证据无法验收",
        }
        paragraphs = (
            f"先对账，不改口。上一份复盘的核心判断是“{previous.next_day_outlook.thesis}”今天的结果是：{labels[outcome.verdict]}。{outcome.summary}",
            _comparison_statement(review.evidence, previous),
        )
    return ArticleSectionV4(
        section_id="reconciliation",
        title="昨天说对了什么",
        paragraphs=paragraphs,
    )


def _article_watch_items(
    evidence: DailyMarketReviewEvidenceV1,
    scenarios: tuple[NextDayScenarioV2, ...],
    opportunities: tuple[OpportunitySectorV1, ...],
) -> tuple[WatchItemV4, ...]:
    scenario_map = {item.scenario_id: item for item in scenarios}
    repair = scenario_map["repair"]
    risk = scenario_map["risk"]
    items: list[WatchItemV4] = [
        WatchItemV4(
            rank=1,
            title="先看个股面有没有真修复",
            why_it_matters="指数翻红不够。今天的问题出在多数股票和高成交核心一起走弱，明天也必须先由它们止跌来修复。",
            confirmation=repair.trigger,
            invalidation=risk.trigger,
        )
    ]
    for opportunity in opportunities[:3]:
        detail = opportunity.name
        if opportunity.change_pct is not None:
            detail += f"今天涨{opportunity.change_pct:.2f}%"
        if opportunity.breadth_ratio is not None:
            detail += f"，上涨覆盖{opportunity.breadth_ratio * 100:.0f}%"
        if opportunity.leader_name:
            detail += f"，前排是{opportunity.leader_name}"
        items.append(WatchItemV4(
            rank=len(items) + 1,
            title=f"{opportunity.name}能不能扛住第一次分歧",
            why_it_matters=detail + "。今天的证据足够把它列入观察，但次日高开本身不是买点。",
            confirmation=opportunity.next_day_confirmation,
            invalidation=opportunity.invalidation,
        ))
    return tuple(items)


def _article_sections_v4(
    review: PostMarketReviewV1,
    *,
    previous: PostMarketReviewV1 | None,
    outlook: NextDayOutlookV1,
    scenarios: tuple[NextDayScenarioV2, ...],
    opportunities: tuple[OpportunitySectorV1, ...],
) -> tuple[ArticleSectionV4, ...]:
    evidence = review.evidence
    return (
        _article_session_section(evidence),
        _article_mainline_section(evidence, opportunities),
        _article_payoff_section(evidence),
        _article_flow_section(evidence, opportunities),
        _article_tomorrow_section(outlook, scenarios),
        _article_reconciliation_section(review, previous),
    )


def build_post_market_review(
    evidence: DailyMarketReviewEvidenceV1,
    *,
    generated_at: datetime,
    trigger: ReviewTrigger,
    learning: ReviewLearningV1 | None = None,
) -> PostMarketReviewV1:
    canonical = DailyMarketReviewEvidenceV1.model_validate(evidence)
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must include a timezone")
    local_generated = generated_at.astimezone(SHANGHAI)
    snapshot = canonical.market_watch
    if (
        snapshot.market_state.phase != MarketPhase.CLOSED
        and a_share_session(local_generated).phase != TradingSessionPhase.CLOSED
    ):
        raise ValueError("post-market review requires a closed-market snapshot")
    quality = _quality(canonical)
    learning_context = learning or empty_learning()
    opportunities = _opportunities(canonical, quality)
    limitations = list(canonical.quality_notes)
    if not opportunities:
        limitations.append("no_sector_met_multi_evidence_threshold")
    digest_payload = {
        "trade_date": canonical.trade_date.isoformat(),
        "snapshot_id": snapshot.snapshot_id,
        "evidence": canonical.model_dump(mode="json"),
        "config_version": REVIEW_CONFIG_VERSION,
    }
    digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:20]
    return PostMarketReviewV1(
        review_id=f"pmr-{canonical.trade_date.isoformat()}-{digest}",
        trade_date=canonical.trade_date,
        generated_at=local_generated,
        trigger=trigger,
        quality=quality,
        source_snapshot_id=snapshot.snapshot_id,
        source_snapshot_as_of=snapshot.as_of,
        source_freshness=snapshot.freshness.status,
        evidence=canonical,
        recap=_recap(canonical, quality, opportunities),
        next_day_outlook=_outlook(canonical, quality, learning_context, opportunities),
        opportunity_sectors=opportunities,
        learning=learning_context,
        limitations=tuple(dict.fromkeys(limitations)),
    )


def build_post_market_review_presentation(
    review: PostMarketReviewV1,
    previous_review: PostMarketReviewV1 | None = None,
) -> PostMarketReviewPresentationV4:
    """Render a readable V4 article without changing the immutable evidence row."""

    canonical = PostMarketReviewV1.model_validate(review)
    previous = (
        PostMarketReviewV1.model_validate(previous_review)
        if previous_review is not None and previous_review.trade_date < canonical.trade_date
        else None
    )
    opportunities = _opportunities(canonical.evidence, canonical.quality)
    outlook = _outlook(
        canonical.evidence,
        canonical.quality,
        canonical.learning,
        opportunities,
    )
    character = _day_character(canonical.evidence, previous)
    conclusion = _core_conclusion(canonical.evidence, opportunities)
    recap = _recap(canonical.evidence, canonical.quality, opportunities).model_copy(update={
        "headline": _presentation_headline(canonical.evidence, character, opportunities),
        "summary": conclusion,
    })
    scenarios = _next_day_scenarios(outlook, opportunities)
    appendix_sections = _presentation_sections_v3(
        canonical,
        previous=previous,
        recap=recap,
        outlook=outlook,
        scenarios=scenarios,
        opportunities=opportunities,
    )
    return PostMarketReviewPresentationV4(
        review_id=canonical.review_id,
        title=_article_title(canonical.evidence, opportunities, canonical.quality),
        standfirst=_article_standfirst(canonical.evidence, opportunities, canonical.quality),
        sections=_article_sections_v4(
            canonical,
            previous=previous,
            outlook=outlook,
            scenarios=scenarios,
            opportunities=opportunities,
        ),
        watch_items=_article_watch_items(canonical.evidence, scenarios, opportunities),
        appendix_sections=appendix_sections,
        day_character=character,
        core_conclusion=conclusion,
        session_story=_session_story(canonical.evidence),
        comparison_statement=_comparison_statement(canonical.evidence, previous),
        themes=_theme_layers(canonical.evidence, opportunities),
        money_making_effect=_money_making_effect(canonical.evidence, opportunities),
        loss_making_effect=_loss_making_effect(canonical.evidence),
        next_day_scenarios=scenarios,
        recap=recap,
        next_day_outlook=outlook,
        opportunity_sectors=opportunities,
    )


def next_verified_trading_day(value: date) -> date | None:
    for offset in range(1, 15):
        candidate = value + timedelta(days=offset)
        status = calendar_day_status(candidate)
        if status == CalendarDayStatus.VERIFIED_TRADING_DAY:
            return candidate
        if status == CalendarDayStatus.UNVERIFIED:
            return None
    return None


def evaluate_review_outcome(
    review: PostMarketReviewV1,
    realized_evidence: DailyMarketReviewEvidenceV1 | MarketWatchSnapshotV1,
) -> ReviewOutcomeV1:
    prior = PostMarketReviewV1.model_validate(review)
    if isinstance(realized_evidence, DailyMarketReviewEvidenceV1):
        realized: DailyMarketReviewEvidenceV1 | MarketWatchSnapshotV1 = DailyMarketReviewEvidenceV1.model_validate(realized_evidence)
        evaluated_on = realized.trade_date
    else:
        realized = MarketWatchSnapshotV1.model_validate(realized_evidence)
        evaluated_on = realized.market_state.trading_date
    expected_date = next_verified_trading_day(prior.trade_date)
    actual_bias = _realized_bias(realized)
    forecast_bias = prior.next_day_outlook.bias
    if expected_date is None or evaluated_on != expected_date:
        verdict = OutcomeVerdict.UNVERIFIABLE
        summary = "缺少紧接着的已核验交易日收盘证据，本次预测不计入支持度。"
    elif forecast_bias == OutlookBias.UNCERTAIN or actual_bias == OutlookBias.UNCERTAIN:
        verdict = OutcomeVerdict.UNVERIFIABLE
        summary = "预测或实际收盘证据不足，本次结果不作方向评价。"
    elif forecast_bias == actual_bias:
        verdict = OutcomeVerdict.SUPPORTED
        summary = "次日全市场收盘形态与前一日基准判断一致。"
    elif OutlookBias.BALANCED in {forecast_bias, actual_bias}:
        verdict = OutcomeVerdict.PARTIAL
        summary = "次日全市场收盘只部分符合基准判断，方向强度未完全兑现。"
    else:
        verdict = OutcomeVerdict.NOT_SUPPORTED
        summary = "次日全市场收盘方向与前一日基准判断相反。"
    return ReviewOutcomeV1(
        review_id=prior.review_id,
        forecast_trade_date=prior.trade_date,
        evaluated_on=evaluated_on,
        forecast_bias=forecast_bias,
        realized_bias=actual_bias,
        verdict=verdict,
        summary=summary,
    )


__all__ = [
    "CellToneV3",
    "CellV3",
    "MarketRecapV1",
    "NextDayOutlookV1",
    "OpportunityConviction",
    "OpportunitySectorV1",
    "OutcomeVerdict",
    "OutlookBias",
    "PostMarketReviewV1",
    "NextDayScenarioV2",
    "PostMarketReviewPresentationV2",
    "PostMarketReviewPresentationV3",
    "PresentationV3",
    "PRESENTATION_V3_REQUIRED_SECTIONS",
    "ReviewThemeV2",
    "REVIEW_CONFIG_VERSION",
    "ReviewLearningV1",
    "ReviewOutcomeV1",
    "ReviewQuality",
    "ReviewTrigger",
    "SectionV3",
    "TableV3",
    "build_post_market_review",
    "build_post_market_review_presentation",
    "empty_learning",
    "evaluate_review_outcome",
    "next_verified_trading_day",
]
