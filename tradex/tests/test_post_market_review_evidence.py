"""Fixture-only coverage for whole-market review evidence collection."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from tradex.data_gateway.contracts import (
    AShareUniverseQuoteV1,
    AShareUniverseSnapshotV1,
    ContractMetadata,
    DragonTigerSeriesV1,
    DragonTigerTradeV1,
    EtfQuoteSeriesV1,
    EtfQuoteV1,
    LimitEventSeriesV1,
    LimitEventTradeStatusV1,
    LimitUpEventV1,
    MarketBreadthV1,
    QualityStatus,
    SectorQuoteSeriesV1,
    SectorQuoteV1,
    StockFundFlowSeriesV1,
    StockFundFlowV1,
)
from tradex.market_watch.review_evidence import (
    EVIDENCE_COMPONENT_ORDER,
    EvidenceStatus,
    collect_daily_market_review_evidence,
)
from tradex.market_watch.review import ReviewTrigger, build_post_market_review

from test_post_market_review import _closing_snapshot


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 24, 20, 30, tzinfo=SHANGHAI)
AS_OF = datetime(2026, 8, 24, 15, 1, tzinfo=SHANGHAI)
TRADE_DATE = date(2026, 8, 24)


def _metadata(contract: str, *, quality: QualityStatus = QualityStatus.ACCEPTED):
    return ContractMetadata(
        contract=contract,
        provider="fixture",
        provider_as_of=AS_OF,
        fetched_at=NOW,
        quality=quality,
    )


def _loaders(*, fail_etf: bool = False, fail_dragon: bool = False):
    universe_quotes = (
        AShareUniverseQuoteV1(instrument_id="000001.SZ", name="平安银行", last=11, change_pct=-1, amount_cny=900_000_000, turnover_pct=1.2),
        AShareUniverseQuoteV1(instrument_id="300001.SZ", name="科技股", last=20, change_pct=4, amount_cny=1_500_000_000, turnover_pct=8.0),
        AShareUniverseQuoteV1(instrument_id="600001.SH", name="领涨股", last=15, change_pct=10, amount_cny=2_000_000_000, turnover_pct=12.0),
    )
    universe = AShareUniverseSnapshotV1(
        metadata=_metadata("a_share_universe_quote.v1"),
        provider_row_count=4,
        active_quote_count=3,
        excluded_row_count=1,
        quotes=universe_quotes,
    )
    breadth = MarketBreadthV1(
        metadata=_metadata("market_breadth.v1"),
        up_count=3400,
        down_count=1400,
        flat_count=100,
        unclassified_count=100,
        limit_up_count=55,
        limit_down_count=3,
        total_count=5000,
    )
    etfs = EtfQuoteSeriesV1(
        metadata=_metadata("etf_quote.v1"),
        requested_limit=5000,
        quotes=(
            EtfQuoteV1(instrument_id="510300.SH", name="沪深300ETF", last=4, change_pct=0.8, amount_cny=8_000_000_000, provider_variant="fixture"),
            EtfQuoteV1(instrument_id="159915.SZ", name="创业板ETF", last=2, change_pct=1.5, amount_cny=5_000_000_000, provider_variant="fixture"),
            EtfQuoteV1(instrument_id="512000.SH", name="证券ETF", last=1, change_pct=2.5, amount_cny=3_000_000_000, provider_variant="fixture"),
        ),
    )
    industry = SectorQuoteSeriesV1(
        metadata=_metadata("sector_quote.v1"),
        sector_type="industry",
        quotes=(
            SectorQuoteV1(sector_key="industry:证券", sector_type="industry", name="证券", change_pct=2.6, main_net_inflow_cny=3_000_000_000, up_count=36, down_count=4, leader_instrument_id="600001.SH", leader_name="领涨股", leader_change_pct=10),
            SectorQuoteV1(sector_key="industry:银行", sector_type="industry", name="银行", change_pct=0.2, main_net_inflow_cny=-500_000_000, up_count=15, down_count=25),
        ),
    )
    concept = SectorQuoteSeriesV1(
        metadata=_metadata("sector_quote.v1"),
        sector_type="concept",
        quotes=(
            SectorQuoteV1(sector_key="concept:金融科技", sector_type="concept", name="金融科技", change_pct=1.8, main_net_inflow_cny=1_000_000_000, up_count=30, down_count=10, leader_instrument_id="300001.SZ", leader_name="科技股", leader_change_pct=4),
        ),
    )
    limits = LimitEventSeriesV1(
        metadata=_metadata("limit_event.v1"),
        trading_date=TRADE_DATE,
        trade_status=LimitEventTradeStatusV1(code="closed", label="已收盘"),
        events=(
            LimitUpEventV1(instrument_id="600001.SH", name="领涨股", reason="证券", board_count=5, first_sealed_at=time(9, 35), order_amount_cny=300_000_000),
            LimitUpEventV1(instrument_id="300001.SZ", name="科技股", reason="金融科技", board_count=2, first_sealed_at=time(10, 5), order_amount_cny=100_000_000),
        ),
        pool_total=2,
        reason_coverage=1,
        board_count_coverage=1,
        unknown_board_count=0,
        valid_empty=False,
    )
    funds = StockFundFlowSeriesV1(
        metadata=_metadata("stock_fund_flow_day.v1"),
        trade_date=TRADE_DATE,
        flows=(
            StockFundFlowV1(instrument_id="000001.SZ", net_amount_cny=-300_000_000, large_net_amount_cny=-200_000_000, extra_large_net_amount_cny=-100_000_000),
            StockFundFlowV1(instrument_id="300001.SZ", net_amount_cny=200_000_000, large_net_amount_cny=120_000_000, extra_large_net_amount_cny=80_000_000),
            StockFundFlowV1(instrument_id="600001.SH", net_amount_cny=600_000_000, large_net_amount_cny=400_000_000, extra_large_net_amount_cny=200_000_000),
        ),
    )
    dragon = DragonTigerSeriesV1(
        metadata=_metadata("dragon_tiger_market_day.v1"),
        trade_date=TRADE_DATE,
        trades=(
            DragonTigerTradeV1(instrument_id="600001.SH", name="领涨股", close=15, change_pct=10, turnover_pct=12, market_amount_cny=2_000_000_000, buy_amount_cny=800_000_000, sell_amount_cny=300_000_000, net_amount_cny=500_000_000, reason="日涨幅偏离"),
        ),
    )

    def failed_etf():
        raise TimeoutError("fixture secret must never be archived")

    def failed_dragon():
        raise RuntimeError("paid source detail must not escape")

    return {
        "market_universe": lambda: universe,
        "market_breadth_detail": lambda: breadth,
        "etfs": failed_etf if fail_etf else lambda: etfs,
        "industry_sectors": lambda: industry,
        "concept_sectors": lambda: concept,
        "limit_events": lambda: limits,
        "stock_fund_flow": lambda: funds,
        "dragon_tiger": failed_dragon if fail_dragon else lambda: dragon,
    }


def test_collector_scans_all_surfaces_and_keeps_compact_ranked_evidence():
    closing = _closing_snapshot(AS_OF)
    history = [
        closing.model_copy(update={"as_of": AS_OF.replace(hour=9, minute=29)}).model_dump(mode="json"),
        closing.model_copy(update={"as_of": AS_OF.replace(hour=9, minute=31)}).model_dump(mode="json"),
        closing.model_copy(update={"as_of": AS_OF.replace(hour=9, minute=31, second=30)}).model_dump(mode="json"),
        closing.model_copy(update={"as_of": AS_OF.replace(hour=15, minute=0)}).model_dump(mode="json"),
        closing.model_copy(update={"as_of": AS_OF.replace(hour=20, minute=30)}).model_dump(mode="json"),
    ]
    evidence = collect_daily_market_review_evidence(
        closing,
        history_samples=history,
        collected_at=NOW,
        loaders=_loaders(),
    )
    assert tuple(item.component for item in evidence.components) == EVIDENCE_COMPONENT_ORDER
    assert all(item.status == EvidenceStatus.ACCEPTED for item in evidence.components[:-1])
    assert evidence.components[0].provider == "tradex_market_watch"
    assert evidence.components[1].provider == "fixture"
    assert evidence.components[-1].provider == "tradex_history"
    assert evidence.universe.scanned_count == 3
    assert evidence.universe.median_change_pct == 4
    assert evidence.universe.total_amount_cny == 4_400_000_000
    assert evidence.universe.highest_turnover[0].name == "领涨股"
    assert evidence.etfs.top_gainers[0].name == "证券ETF"
    assert evidence.etfs.most_traded[0].name == "沪深300ETF"
    assert evidence.industry_sectors.top_inflows[0].name == "证券"
    assert evidence.limit_events.max_board_count == 5
    assert evidence.stock_fund_flow.top_inflows[0].name == "领涨股"
    assert evidence.stock_fund_flow.top_outflows[0].instrument_id == "000001.SZ"
    assert evidence.dragon_tiger.total_net_amount_cny == 500_000_000
    assert evidence.intraday.sample_count == 2
    assert evidence.intraday.first_as_of.time() == time(9, 31, 30)
    assert evidence.intraday.last_as_of.time() == time(15, 0)
    assert "盘中" in evidence.intraday.trajectory_statement or "上涨占比" in evidence.intraday.trajectory_statement


def test_late_components_degrade_independently_without_leaking_error_details():
    evidence = collect_daily_market_review_evidence(
        _closing_snapshot(AS_OF),
        collected_at=NOW,
        loaders=_loaders(fail_etf=True, fail_dragon=True),
    )
    by_name = {item.component: item for item in evidence.components}
    assert evidence.universe is not None
    assert evidence.industry_sectors is not None
    assert evidence.stock_fund_flow is not None
    assert evidence.etfs is None
    assert evidence.dragon_tiger is None
    assert by_name["etfs"].error_code == "TimeoutError"
    assert by_name["dragon_tiger"].error_code == "RuntimeError"
    assert "secret" not in str(evidence.model_dump(mode="json"))
    assert "paid source" not in str(evidence.model_dump(mode="json"))
    assert "etfs:unavailable:TimeoutError" in evidence.quality_notes

    review = build_post_market_review(
        evidence,
        generated_at=NOW,
        trigger=ReviewTrigger.MANUAL,
    )
    assert review.quality.value == "degraded"
    assert review.next_day_outlook.bias.value == "constructive"
    assert review.opportunity_sectors[0].name == "证券"
    assert "etfs:unavailable:TimeoutError" in review.limitations
