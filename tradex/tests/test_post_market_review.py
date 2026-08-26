"""Focused fixture tests for review analysis, archive, and scheduling."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tradex.market_watch import (
    CellV3,
    DailyMarketReviewEvidenceV1,
    OutcomeVerdict,
    OutlookBias,
    ReviewTrigger,
    TableV3,
    TurnoverSnapshotV1,
    build_market_watch_snapshot,
    build_post_market_review,
    build_post_market_review_presentation,
    evaluate_review_outcome,
)
from tradex.market_watch.review_service import (
    PostMarketReviewService,
    ReviewSnapshotUnavailableError,
    ReviewTooEarlyError,
)
from tradex.market_watch.review import _same_opportunity_theme
from tradex.market_watch.review_store import PostMarketReviewStore


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _closing_snapshot(
    observed_at: datetime,
    *,
    direction: str = "up",
    snapshot_id: str = "closing-1",
    market_phase: str = "closed",
):
    positive = direction == "up"
    changes = (0.7, 0.8, 1.1, 1.2) if positive else (-0.8, -0.7, -1.2, -1.0)
    up_count, down_count = (3300, 1500) if positive else (1400, 3400)
    sector_change = 1.8 if positive else -1.8
    sector_breadth = 0.68 if positive else 0.32
    specs = (
        ("broad_market", "000001.SH", "上证指数", 3400.0),
        ("large_cap", "000300.SH", "沪深300", 4100.0),
        ("small_cap", "000852.SH", "中证1000", 6800.0),
        ("growth", "399006.SZ", "创业板指", 2250.0),
    )
    market = {
        "timestamp": observed_at.isoformat(),
        "provider_as_of": observed_at.isoformat(),
        "market_state": {
            "phase": market_phase,
            "is_open": market_phase == "trading",
        },
        "quality": "accepted",
        "indices": [
            {
                "role": role,
                "instrument_id": instrument_id,
                "name": name,
                "available": True,
                "level": level,
                "change_pct": change,
                "provider_as_of": observed_at.isoformat(),
                "quality": "accepted",
            }
            for (role, instrument_id, name, level), change in zip(specs, changes)
        ],
        "market_turnover": {
            "available": True,
            "today_date": observed_at.date().isoformat(),
            "previous_date": "2026-08-21" if observed_at.day == 24 else "2026-08-24",
            "as_of": (
                observed_at.strftime("%H:%M")
                if market_phase == "trading"
                else "15:00"
            ),
            "today_amount": 112_000_000_000.0 if positive else 92_000_000_000.0,
            "previous_same_time_amount": 100_000_000_000.0,
        },
    }
    risk = {
        "timestamp": observed_at.isoformat(),
        "breadth": {
            "up_count": up_count,
            "down_count": down_count,
            "flat_count": 100,
            "unclassified_count": 100,
            "total_count": 5000,
            "provider_as_of": observed_at.isoformat(),
            "quality": "accepted",
        },
        "rotation": {
            "sectors": [
                {
                    "sector_key": "industry:证券",
                    "name": "证券",
                    "tags": ["attack"],
                    "change_pct": sector_change,
                    "breadth_ratio": sector_breadth,
                    "main_net_inflow_cny": 5_000_000_000.0 if positive else -3_000_000_000.0,
                    "provider_as_of": observed_at.isoformat(),
                }
            ]
        },
    }
    return build_market_watch_snapshot(
        market,
        risk,
        as_of=observed_at,
        sequence=1,
        snapshot_id=snapshot_id,
    )


def _evidence(snapshot, generated_at: datetime, *, direction: str = "up"):
    positive = direction == "up"
    changes = [10.0, 4.0, 1.2, 0.4, -0.3, -1.0] if positive else [1.0, 0.2, -0.4, -1.2, -4.0, -10.0]
    up_count = sum(value > 0 for value in changes)
    down_count = sum(value < 0 for value in changes)
    buckets = [0] * 8
    for value in changes:
        index = 0 if value >= 9.5 else 1 if value >= 5 else 2 if value >= 2 else 3 if value > 0 else 4 if value == 0 else 5 if value > -2 else 6 if value > -5 else 7
        buckets[index] += 1
    status_rows = [
        {
            "component": name,
            "status": "accepted",
            "record_count": 1,
            "contract": "fixture.v1",
            "provider_as_of": snapshot.as_of.isoformat(),
        }
        for name in (
            "market_watch",
            "market_universe",
            "market_breadth_detail",
            "etfs",
            "industry_sectors",
            "concept_sectors",
            "limit_events",
            "stock_fund_flow",
            "dragon_tiger",
            "intraday_history",
        )
    ]
    sector_change = 2.4 if positive else -2.4
    sector_flow = 3_000_000_000.0 if positive else -3_000_000_000.0
    leader_change = 10.0 if positive else -8.0
    return DailyMarketReviewEvidenceV1.model_validate({
        "trade_date": snapshot.market_state.trading_date.isoformat(),
        "collected_at": generated_at.isoformat(),
        "market_watch": snapshot.model_dump(mode="json"),
        "universe": {
            "scanned_count": len(changes),
            "excluded_count": 1,
            "up_count": up_count,
            "down_count": down_count,
            "flat_count": 0,
            "mean_change_pct": sum(changes) / len(changes),
            "median_change_pct": 0.8 if positive else -0.8,
            "total_amount_cny": 1_200_000_000_000,
            "distribution": [
                {"bucket": key, "label": key, "count": count}
                for key, count in zip(
                    ("limit_up_zone", "strong_up", "up_2_5", "up_0_2", "flat", "down_0_2", "down_2_5", "down_5_plus"),
                    buckets,
                )
            ],
            "top_gainers": [{"instrument_id": "600001.SH", "name": "领涨股", "change_pct": changes[0], "amount_cny": 2_000_000_000}],
            "top_losers": [{"instrument_id": "000002.SZ", "name": "领跌股", "change_pct": changes[-1], "amount_cny": 1_000_000_000}],
            "most_traded": [{"instrument_id": "600001.SH", "name": "领涨股", "change_pct": changes[0], "amount_cny": 2_000_000_000}],
            "highest_turnover": [],
        },
        "etfs": {
            "scanned_count": 3,
            "up_count": 2 if positive else 1,
            "down_count": 1 if positive else 2,
            "flat_count": 0,
            "median_change_pct": 0.6 if positive else -0.6,
            "total_amount_cny": 90_000_000_000,
            "top_gainers": [{"instrument_id": "510300.SH", "name": "宽基ETF", "change_pct": 1.1 if positive else 0.1, "amount_cny": 5_000_000_000}],
            "top_losers": [],
            "most_traded": [],
        },
        "industry_sectors": {
            "sector_type": "industry",
            "scanned_count": 2,
            "up_count": 2 if positive else 0,
            "down_count": 0 if positive else 2,
            "flat_count": 0,
            "median_change_pct": 1.2 if positive else -1.2,
            "top_gainers": [{"sector_key": "industry:证券", "sector_type": "industry", "name": "证券", "change_pct": sector_change, "main_net_inflow_cny": sector_flow, "breadth_ratio": 0.75 if positive else 0.25, "leader_instrument_id": "600001.SH", "leader_name": "领涨股", "leader_change_pct": leader_change}],
            "top_losers": [],
            "top_inflows": [{"sector_key": "industry:证券", "sector_type": "industry", "name": "证券", "change_pct": sector_change, "main_net_inflow_cny": sector_flow, "breadth_ratio": 0.75 if positive else 0.25, "leader_instrument_id": "600001.SH", "leader_name": "领涨股", "leader_change_pct": leader_change}],
            "top_outflows": [],
        },
        "concept_sectors": {
            "sector_type": "concept",
            "scanned_count": 1,
            "up_count": 1 if positive else 0,
            "down_count": 0 if positive else 1,
            "flat_count": 0,
            "median_change_pct": 0.8 if positive else -0.8,
            "top_gainers": [{"sector_key": "concept:金融科技", "sector_type": "concept", "name": "金融科技", "change_pct": 1.5 if positive else -1.5, "main_net_inflow_cny": 1_000_000_000 if positive else -1_000_000_000}],
            "top_losers": [], "top_inflows": [], "top_outflows": [],
        },
        "limit_events": {
            "limit_up_count": 55 if positive else 12,
            "limit_down_count": 3 if positive else 45,
            "max_board_count": 5 if positive else 2,
            "board_heights": [{"board_count": 5 if positive else 2, "count": 1}],
            "top_reasons": [{"reason": "证券", "count": 6}],
            "representative_events": [{"instrument_id": "600001.SH", "name": "领涨股", "reason": "证券", "board_count": 5 if positive else 2}],
            "reason_coverage": 1,
            "board_count_coverage": 1,
        },
        "stock_fund_flow": {
            "scanned_count": 3,
            "positive_count": 2 if positive else 1,
            "negative_count": 1 if positive else 2,
            "flat_count": 0,
            "median_net_amount_cny": 200_000_000 if positive else -200_000_000,
            "total_net_amount_cny": 600_000_000 if positive else -600_000_000,
            "total_large_net_amount_cny": 400_000_000 if positive else -400_000_000,
            "top_inflows": [{"instrument_id": "600001.SH", "name": "领涨股", "net_amount_cny": 500_000_000, "large_net_amount_cny": 300_000_000, "extra_large_net_amount_cny": 200_000_000}],
            "top_outflows": [],
        },
        "dragon_tiger": {
            "listed_count": 1,
            "positive_net_count": 1 if positive else 0,
            "negative_net_count": 0 if positive else 1,
            "total_buy_amount_cny": 800_000_000,
            "total_sell_amount_cny": 300_000_000 if positive else 1_100_000_000,
            "total_net_amount_cny": 500_000_000 if positive else -300_000_000,
            "top_net_buys": [{"instrument_id": "600001.SH", "name": "领涨股", "change_pct": leader_change, "buy_amount_cny": 800_000_000, "sell_amount_cny": 300_000_000, "net_amount_cny": 500_000_000 if positive else -300_000_000, "reason": "日涨幅偏离"}],
            "top_net_sells": [],
        },
        "intraday": {
            "sample_count": 240,
            "coverage_ratio": 1,
            "first_as_of": snapshot.as_of.replace(hour=9, minute=31).isoformat(),
            "last_as_of": snapshot.as_of.isoformat(),
            "first_regime": "balanced",
            "last_regime": "attack" if positive else "defense",
            "regime_transitions": 2,
            "first_advance_ratio": 0.48 if positive else 0.52,
            "last_advance_ratio": 0.68 if positive else 0.32,
            "min_advance_ratio": 0.45 if positive else 0.32,
            "max_advance_ratio": 0.68 if positive else 0.55,
            "first_index_mean_change_pct": 0,
            "last_index_mean_change_pct": 0.9 if positive else -0.9,
            "trajectory_statement": "午后上涨覆盖持续扩散。" if positive else "午后上涨覆盖持续收窄。",
        },
        "components": status_rows,
        "quality_notes": [],
    })


def _service(snapshot, store, calls=None):
    return PostMarketReviewService(
        lambda: (calls.append(True) if calls is not None else None) or snapshot,
        store,
        evidence_loader=lambda value, now: _evidence(value, now),
    )


def test_opportunity_list_collapses_repeated_market_themes():
    selected = SimpleNamespace(name="煤炭", leader_instrument_id="600001.SH")

    assert _same_opportunity_theme(
        SimpleNamespace(name="动力煤", leader_instrument_id="600002.SH"),
        selected,
    )
    assert _same_opportunity_theme(
        SimpleNamespace(name="贵金属", leader_instrument_id="600001.SH"),
        selected,
    )
    assert not _same_opportunity_theme(
        SimpleNamespace(name="机器人", leader_instrument_id="300001.SZ"),
        selected,
    )


def test_manual_review_is_gated_at_1730_and_does_not_fetch_early(tmp_path: Path):
    calls = []
    store = PostMarketReviewStore(tmp_path / "review.sqlite3")
    service = PostMarketReviewService(lambda: calls.append(True) or {}, store)
    try:
        with pytest.raises(ReviewTooEarlyError, match="17:30"):
            service.generate(now=datetime(2026, 8, 24, 17, 29, tzinfo=SHANGHAI))
        assert calls == []
        assert store.list_dates() == []
    finally:
        store.close()


def test_manual_review_is_immutable_and_uses_cross_market_evidence(tmp_path: Path):
    observed = datetime(2026, 8, 24, 15, 1, tzinfo=SHANGHAI)
    snapshot = _closing_snapshot(observed)
    calls = []
    store = PostMarketReviewStore(tmp_path / "review.sqlite3")
    service = _service(snapshot, store, calls)
    now = datetime(2026, 8, 24, 20, 30, tzinfo=SHANGHAI)
    try:
        first = service.generate(now=now)
        second = service.generate(now=now.replace(hour=20, minute=45))
        review = first["review"]
        assert first["action"] == "inserted"
        assert second["action"] == "existing"
        assert first["presentation"]["contract"] == "post_market_review_presentation.v4"
        assert first["presentation"]["schema_version"] == 4
        assert first["presentation"]["review_id"] == review["review_id"]
        assert first["presentation"]["title"]
        assert first["presentation"]["standfirst"]
        assert first["presentation"]["day_character"] == "普涨进攻日"
        assert len(first["presentation"]["session_story"]) >= 2
        assert len(first["presentation"]["next_day_scenarios"]) == 3
        assert [item["scenario_id"] for item in first["presentation"]["next_day_scenarios"]] == [
            "base",
            "repair",
            "risk",
        ]
        assert first["presentation"]["money_making_effect"]
        assert first["presentation"]["loss_making_effect"]
        article_text = "".join(
            paragraph
            for section in first["presentation"]["sections"]
            for paragraph in section["paragraphs"]
        )
        assert "实打实的回撤" not in first["presentation"]["standfirst"]
        assert "普遍撤退" not in article_text
        assert "个股面并没有跟上" not in article_text
        assert "这里赚的是逆势抱团的钱" not in "".join(
            first["presentation"]["money_making_effect"]
        )
        assert "亏钱效应是主导项" not in "".join(
            first["presentation"]["loss_making_effect"]
        )
        sections = {
            section["section_id"]: section
            for section in first["presentation"]["sections"]
        }
        assert tuple(sections) == (
            "session",
            "mainline",
            "payoff",
            "flow",
            "tomorrow",
            "reconciliation",
        )
        assert all(section["paragraphs"] for section in sections.values())
        assert len(first["presentation"]["watch_items"]) >= 1
        appendix = {
            section["section_id"]: section
            for section in first["presentation"]["appendix_sections"]
        }
        assert tuple(appendix) == (
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
        for section in appendix.values():
            assert section["tables"]
            for table in section["tables"]:
                assert all(len(row) == len(table["columns"]) for row in table["rows"])
        index_table = appendix["overview"]["tables"][0]
        assert all(row[2]["tone"] == "rise" for row in index_table["rows"])
        assert len(calls) == 1
        assert review["quality"] == "ready"
        assert review["evidence"]["universe"]["scanned_count"] == 6
        assert "持仓体感" in review["recap"]["universe_statement"]
        assert "ETF整体" in review["recap"]["etf_statement"]
        assert "资金净流入" in review["recap"]["stock_fund_flow_statement"]
        assert "龙虎榜" in review["recap"]["dragon_tiger_statement"]
        assert len(review["recap"]["summary"]) < 260
        assert "扫描" not in review["recap"]["summary"]
        assert "角色指数" not in review["recap"]["summary"]
        assert len(review["recap"]["supporting_evidence"]) <= 3
        assert len(review["recap"]["risk_evidence"]) <= 4
        assert review["next_day_outlook"]["bias"] == "constructive"
        assert "明天先按" in review["next_day_outlook"]["thesis"]
        assert review["opportunity_sectors"][0]["name"] == "证券"
        assert len(review["opportunity_sectors"][0]["evidence_chain"]) >= 4
        assert "证券板块" in review["opportunity_sectors"][0]["next_day_confirmation"]
        history = service.history()
        assert history["schedule"]["manual_after"] == "17:30"
        assert history["presentation"]["review_id"] == review["review_id"]
    finally:
        store.close()


def test_automatic_backstop_generates_at_2100_only_when_missing(tmp_path: Path):
    snapshot = _closing_snapshot(datetime(2026, 8, 24, 15, 1, tzinfo=SHANGHAI))
    store = PostMarketReviewStore(tmp_path / "review.sqlite3")
    service = _service(snapshot, store)
    try:
        assert service.maybe_generate_automatic(now=datetime(2026, 8, 24, 20, 59, tzinfo=SHANGHAI)) == {"action": "not_due"}
        generated = service.maybe_generate_automatic(now=datetime(2026, 8, 24, 21, 0, tzinfo=SHANGHAI))
        repeated = service.maybe_generate_automatic(now=datetime(2026, 8, 24, 21, 1, tzinfo=SHANGHAI))
        assert generated["action"] == "inserted"
        assert generated["review"]["trigger"] == "automatic"
        assert repeated["action"] == "existing"
    finally:
        store.close()


def test_automatic_backstop_archives_incomplete_close_as_abstained(tmp_path: Path):
    snapshot = _closing_snapshot(
        datetime(2026, 8, 24, 14, 57, tzinfo=SHANGHAI),
        market_phase="trading",
    )
    store = PostMarketReviewStore(tmp_path / "review.sqlite3")
    service = _service(snapshot, store)
    try:
        generated = service.maybe_generate_automatic(
            now=datetime(2026, 8, 24, 21, 0, tzinfo=SHANGHAI)
        )

        assert generated["action"] == "inserted"
        assert generated["review"]["trigger"] == "automatic"
        assert generated["review"]["quality"] == "abstained"
        assert generated["review"]["source_snapshot_as_of"].startswith(
            "2026-08-24T14:57:"
        )
        assert (
            generated["review"]["evidence"]["market_watch"]["market_state"][
                "phase"
            ]
            == "trading"
        )
        assert generated["review"]["next_day_outlook"]["bias"] == "uncertain"
        assert generated["review"]["next_day_outlook"]["confidence"] == "abstain"
        assert generated["review"]["opportunity_sectors"] == []
        assert (
            "market_watch:degraded:closing_snapshot_incomplete"
            in generated["review"]["limitations"]
        )
    finally:
        store.close()


def test_archive_prefers_current_policy_without_hiding_older_dates(tmp_path: Path):
    generated_at = datetime(2026, 8, 24, 20, 30, tzinfo=SHANGHAI)
    snapshot = _closing_snapshot(datetime(2026, 8, 24, 15, 1, tzinfo=SHANGHAI))
    current = build_post_market_review(
        _evidence(snapshot, generated_at),
        generated_at=generated_at,
        trigger=ReviewTrigger.MANUAL,
    )
    older = current.model_copy(update={
        "config_version": "post-market-review-policy.v2",
        "review_id": f"{current.review_id}-v2",
    })
    store = PostMarketReviewStore(tmp_path / "review.sqlite3")
    try:
        assert store.record(older)[0] == "inserted"
        assert store.get(current.trade_date) is None
        assert store.get_current_or_latest(current.trade_date) == older
        assert store.list_dates()[0]["review_id"] == older.review_id

        assert store.record(current)[0] == "inserted"
        assert store.get(current.trade_date) == current
        assert store.get_current_or_latest(current.trade_date) == current
        assert len(store.list_dates()) == 1
        assert store.list_dates()[0]["review_id"] == current.review_id
    finally:
        store.close()


def test_automatic_backstop_has_bounded_ten_minute_retries(tmp_path: Path):
    calls = []

    def fail():
        calls.append(True)
        raise RuntimeError("upstream secret must not escape")

    store = PostMarketReviewStore(tmp_path / "review.sqlite3")
    service = PostMarketReviewService(fail, store)
    try:
        with pytest.raises(ReviewSnapshotUnavailableError):
            service.maybe_generate_automatic(now=datetime(2026, 8, 24, 21, 0, tzinfo=SHANGHAI))
        assert service.maybe_generate_automatic(now=datetime(2026, 8, 24, 21, 5, tzinfo=SHANGHAI)) == {"action": "retry_cooldown"}
        for minute in (10, 20):
            with pytest.raises(ReviewSnapshotUnavailableError):
                service.maybe_generate_automatic(now=datetime(2026, 8, 24, 21, minute, tzinfo=SHANGHAI))
        assert service.maybe_generate_automatic(now=datetime(2026, 8, 24, 21, 30, tzinfo=SHANGHAI)) == {"action": "retry_exhausted"}
        assert len(calls) == 3
    finally:
        store.close()


def test_next_trading_day_outcome_is_archived_and_calibrates_history(tmp_path: Path):
    monday_snapshot = _closing_snapshot(datetime(2026, 8, 24, 15, 1, tzinfo=SHANGHAI), snapshot_id="monday")
    tuesday_snapshot = _closing_snapshot(datetime(2026, 8, 25, 15, 1, tzinfo=SHANGHAI), snapshot_id="tuesday")
    monday = _evidence(monday_snapshot, datetime(2026, 8, 24, 20, 30, tzinfo=SHANGHAI))
    tuesday = _evidence(tuesday_snapshot, datetime(2026, 8, 25, 20, 30, tzinfo=SHANGHAI))
    review = build_post_market_review(monday, generated_at=datetime(2026, 8, 24, 20, 30, tzinfo=SHANGHAI), trigger=ReviewTrigger.MANUAL)
    outcome = evaluate_review_outcome(review, tuesday)
    assert review.next_day_outlook.bias == OutlookBias.CONSTRUCTIVE
    assert outcome.verdict == OutcomeVerdict.SUPPORTED

    store = PostMarketReviewStore(tmp_path / "review.sqlite3")
    try:
        assert store.record(review)[0] == "inserted"
        assert store.record_outcome(outcome)[0] == "inserted"
        assert store.record_outcome(outcome)[0] == "existing"
        learning = store.learning_summary()
        assert learning.supported_count == 1
        assert learning.support_ratio == 1.0
        assert store.list_dates()[0]["verdict"] == "supported"
    finally:
        store.close()


def test_editorial_presentation_compares_the_previous_same_policy_archive():
    monday_snapshot = _closing_snapshot(
        datetime(2026, 8, 24, 15, 1, tzinfo=SHANGHAI),
        direction="up",
        snapshot_id="monday-up",
    )
    tuesday_snapshot = _closing_snapshot(
        datetime(2026, 8, 25, 15, 1, tzinfo=SHANGHAI),
        direction="down",
        snapshot_id="tuesday-down",
    )
    monday = build_post_market_review(
        _evidence(monday_snapshot, datetime(2026, 8, 24, 20, 30, tzinfo=SHANGHAI), direction="up"),
        generated_at=datetime(2026, 8, 24, 20, 30, tzinfo=SHANGHAI),
        trigger=ReviewTrigger.MANUAL,
    )
    tuesday = build_post_market_review(
        _evidence(tuesday_snapshot, datetime(2026, 8, 25, 20, 30, tzinfo=SHANGHAI), direction="down"),
        generated_at=datetime(2026, 8, 25, 20, 30, tzinfo=SHANGHAI),
        trigger=ReviewTrigger.MANUAL,
    )

    presentation = build_post_market_review_presentation(tuesday, previous_review=monday)

    assert presentation.day_character == "退潮加速日"
    assert "和上一份同口径复盘相比" in presentation.comparison_statement
    assert "明显转弱" in presentation.comparison_statement
    reconciliation = next(item for item in presentation.appendix_sections if item.section_id == "reconciliation")
    assert "整体形势" in reconciliation.tables[0].rows[0][0].value
    assert "支持" in reconciliation.tables[0].rows[0][3].value or "不支持" in reconciliation.tables[0].rows[0][3].value
    overview = next(item for item in presentation.appendix_sections if item.section_id == "overview")
    assert all(row[2].tone.value == "fall" for row in overview.tables[0].rows)


def test_v3_table_rejects_rows_that_do_not_match_columns():
    with pytest.raises(ValueError, match="1 cells but 2 columns"):
        TableV3(
            table_id="invalid_shape",
            title="坏表",
            columns=("第一列", "第二列"),
            rows=((CellV3(value="只有一格"),),),
        )


def test_v3_discloses_unavailable_metrics_without_inventing_values():
    generated_at = datetime(2026, 8, 24, 20, 30, tzinfo=SHANGHAI)
    snapshot = _closing_snapshot(datetime(2026, 8, 24, 15, 1, tzinfo=SHANGHAI))
    snapshot = snapshot.model_copy(
        update={
            "turnover": TurnoverSnapshotV1(
                available=False,
                reason="fixture_missing_same_time_baseline",
            )
        }
    )
    review = build_post_market_review(
        _evidence(snapshot, generated_at),
        generated_at=generated_at,
        trigger=ReviewTrigger.MANUAL,
    )

    presentation = build_post_market_review_presentation(review)
    sections = {item.section_id: item for item in presentation.appendix_sections}
    sentiment_disclosure = {
        row[0].value: row[1].value
        for row in next(
            table for table in sections["sentiment"].tables if table.table_id == "limit_summary"
        ).rows
    }
    methodology_disclosure = {
        row[0].value: row[1].value
        for row in next(
            table
            for table in sections["methodology"].tables
            if table.table_id == "missing_disclosures"
        ).rows
    }
    catalyst_disclosure = {
        row[0].value: row[1].value
        for row in next(
            table for table in sections["tomorrow"].tables if table.table_id == "news_catalysts"
        ).rows
    }

    assert sentiment_disclosure["炸板率"] == "未取得"
    assert sentiment_disclosure["昨日涨停反馈"] == "未取得"
    assert methodology_disclosure["成交额环比"] == "未取得"
    assert catalyst_disclosure["可靠新闻催化"] == "未取得"
