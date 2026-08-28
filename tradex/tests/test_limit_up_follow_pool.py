from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tradex.data_gateway.contracts import (
    ContractMetadata,
    IntradayMinutePointV1,
    IntradayMinuteSeriesV1,
    LimitUpEventV1,
    QualityStatus,
    StockSectorProfileV1,
)
from tradex.market_watch.contracts import SectorFlowSeriesV1
from tradex.market_watch.limit_up_pool import (
    LimitUpFollowPoolV1,
    _fetch_stock_minutes_in_batches,
    _live_status_items,
    attribute_limit_up_follow_pool,
)
from tradex.market_watch.limit_up_pool_store import LimitUpFollowPoolStore


SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 8, 27)
TARGET = datetime(2026, 8, 27, 14, 57, tzinfo=SHANGHAI)


def _sector(
    key: str,
    name: str,
    *,
    increments: tuple[float, ...] = (10, 20, 30, 40, 50),
    include_opening: bool = False,
) -> SectorFlowSeriesV1:
    points = []
    if include_opening:
        for offset, value in enumerate((0.0, 5.0, 12.0, 25.0, 40.0, 60.0)):
            observed = datetime(2026, 8, 27, 9, 30, tzinfo=SHANGHAI) + timedelta(
                minutes=offset
            )
            points.append({
                "sampled_at": observed,
                "provider_as_of": observed,
                "session_segment": "am",
                "cumulative_cny": value * 1_000_000,
                **(
                    {
                        "delta_5m_cny": 60_000_000.0,
                        "delta_5m_baseline_as_of": observed - timedelta(minutes=5),
                    }
                    if offset == 5
                    else {}
                ),
            })
    cumulative = [100_000_000.0]
    for value in increments:
        cumulative.append(cumulative[-1] + value * 1_000_000)
    for offset, value in enumerate(cumulative):
        observed = TARGET - timedelta(minutes=5 - offset)
        points.append({
            "sampled_at": observed,
            "provider_as_of": observed,
            "session_segment": "pm",
            "cumulative_cny": value,
            **(
                {
                    "delta_5m_cny": cumulative[-1] - cumulative[0],
                    "delta_5m_baseline_as_of": TARGET - timedelta(minutes=5),
                }
                if offset == 5
                else {}
            ),
        })
    return SectorFlowSeriesV1.model_validate({
        "sector_key": key,
        "name": name,
        "category_key": "test_category",
        "category_name": "测试",
        "taxonomy": "industry",
        "status": "ready",
        "follow_eligible": True,
        "eligible_for_rank": True,
        "observation_rank": 1,
        "rank_total": 1,
        "observation_tier": "confirmed_strengthening",
        "tier_label": "确认增强",
        "latest": {
            "provider_as_of": TARGET,
            "change_pct": 1.0,
            "cumulative_cny": cumulative[-1],
            "delta_5m_cny": cumulative[-1] - cumulative[0],
            "delta_5m_baseline_as_of": TARGET - timedelta(minutes=5),
            "change_delta_5m_pct": 0.5,
            "current_strength": "strong",
            "fund_strength": "strong",
            "incremental_direction": "inflow",
        },
        "points": points,
    })


def _stock(instrument_id: str) -> IntradayMinuteSeriesV1:
    prices = [100.0]
    for value in (0.10, 0.20, 0.30, 0.40, 0.50):
        prices.append(prices[-1] * (1 + value / 100))
    return IntradayMinuteSeriesV1(
        metadata=ContractMetadata(
            contract="intraday_minute_series.v1",
            provider="fixture",
            provider_as_of=TARGET,
            fetched_at=TARGET,
            quality=QualityStatus.ACCEPTED,
        ),
        instrument_id=instrument_id,
        trading_date=TRADE_DATE,
        points=tuple(
            IntradayMinutePointV1(
                minute=(TARGET - timedelta(minutes=5 - offset)).time(),
                price=price,
                volume_shares=100,
            )
            for offset, price in enumerate(prices)
        ),
    )


def _event(
    instrument_id: str,
    name: str,
    *,
    sealed_at: time = time(14, 57),
    limit_up_type: str = "换手板",
    reason: str = "供应商自报原因",
    board_count: int = 2,
) -> LimitUpEventV1:
    return LimitUpEventV1(
        instrument_id=instrument_id,
        name=name,
        reason=reason,
        limit_up_type=limit_up_type,
        board_label=f"{board_count}连板",
        board_count=board_count,
        first_sealed_at=sealed_at,
    )


def _profile(
    instrument_id: str,
    name: str,
    *,
    industry: str,
    concepts: tuple[str, ...] = (),
) -> StockSectorProfileV1:
    return StockSectorProfileV1(
        instrument_id=instrument_id,
        name=name,
        industry=industry,
        concept_tags=concepts,
        provider_as_of=TARGET,
        provider_variant="fixture",
    )


def test_editorial_reason_does_not_select_the_followed_sector() -> None:
    event = _event("600001.SH", "测试一", reason="医药+并购")
    profile = _profile("600001.SH", "测试一", industry="保险Ⅱ")

    items = attribute_limit_up_follow_pool(
        (event,),
        {profile.instrument_id: profile},
        {event.instrument_id: _stock(event.instrument_id)},
        (_sector("insurance", "保险"),),
        trade_date=TRADE_DATE,
        tzinfo=SHANGHAI,
    )

    assert items[0].follow_status == "confirmed"
    assert items[0].followed_sector_name == "保险"
    assert items[0].source_reason == "医药+并购"
    assert items[0].evidence is not None
    assert items[0].evidence.window_end == TARGET


def test_cross_concept_stock_selects_the_curve_it_actually_followed() -> None:
    event = _event("600002.SH", "测试二")
    profile = _profile(
        event.instrument_id,
        event.name,
        industry="其他",
        concepts=("保险", "证券"),
    )
    # The securities curve ends positive but its path is deliberately noisy and
    # does not meet the path correlation threshold.
    sectors = (
        _sector("securities", "证券", increments=(80, -70, 80, -70, 80)),
        _sector("insurance", "保险"),
    )

    item = attribute_limit_up_follow_pool(
        (event,),
        {profile.instrument_id: profile},
        {event.instrument_id: _stock(event.instrument_id)},
        sectors,
        trade_date=TRADE_DATE,
        tzinfo=SHANGHAI,
    )[0]

    assert item.follow_status == "confirmed"
    assert item.followed_sector_key == "insurance"


def test_one_word_board_never_claims_path_confirmation_without_price_discovery() -> None:
    event = _event(
        "600003.SH",
        "测试三",
        sealed_at=time(9, 30),
        limit_up_type="一字板",
    )
    profile = _profile(event.instrument_id, event.name, industry="保险")

    item = attribute_limit_up_follow_pool(
        (event,),
        {profile.instrument_id: profile},
        {},
        (_sector("insurance", "保险", include_opening=True),),
        trade_date=TRADE_DATE,
        tzinfo=SHANGHAI,
    )[0]

    assert item.follow_status == "unresolved"
    assert item.followed_sector_name is None
    assert "opening_path_not_identifiable" in item.flags


def test_one_word_board_can_only_receive_low_confidence_cohort_confirmation() -> None:
    events = (
        _event("600011.SH", "测试甲"),
        _event("600012.SH", "测试乙"),
        _event(
            "600013.SH",
            "测试丙",
            sealed_at=time(9, 30),
            limit_up_type="一字板",
        ),
    )
    profiles = {
        event.instrument_id: _profile(event.instrument_id, event.name, industry="保险")
        for event in events
    }
    minutes = {
        event.instrument_id: _stock(event.instrument_id)
        for event in events[:2]
    }

    items = attribute_limit_up_follow_pool(
        events,
        profiles,
        minutes,
        (_sector("insurance", "保险", include_opening=True),),
        trade_date=TRADE_DATE,
        tzinfo=SHANGHAI,
    )

    one_word = items[2]
    assert one_word.follow_status == "provisional"
    assert one_word.confidence == "low"
    assert one_word.followed_sector_name == "保险"
    assert one_word.cohort_confirmed_peer_count == 2
    assert one_word.evidence is None


def test_board_height_and_first_seal_time_are_preserved_for_grouping() -> None:
    event = _event("600021.SH", "测试高度", board_count=4, sealed_at=time(10, 9, 5))
    profile = _profile(event.instrument_id, event.name, industry="未知行业")

    item = attribute_limit_up_follow_pool(
        (event,),
        {profile.instrument_id: profile},
        {},
        (),
        trade_date=TRADE_DATE,
        tzinfo=SHANGHAI,
    )[0]

    assert item.board_count == 4
    assert item.first_sealed_at == datetime(2026, 8, 27, 10, 9, 5, tzinfo=SHANGHAI)
    assert item.follow_status == "unresolved"


def test_live_status_exposes_limit_event_without_waiting_for_attribution() -> None:
    event = _event("600024.SH", "盘中先出现", board_count=3, sealed_at=time(9, 41))

    item = _live_status_items(
        (event,),
        trade_date=TRADE_DATE,
        tzinfo=SHANGHAI,
    )[0]

    assert item.name == "盘中先出现"
    assert item.board_count == 3
    assert item.first_sealed_at == datetime(2026, 8, 27, 9, 41, tzinfo=SHANGHAI)
    assert item.follow_status == "unresolved"
    assert item.profile_provider is None
    assert item.stock_minute_provider is None
    assert item.flags == ("analysis_pending_midday_or_post_close",)


def test_one_missing_stock_curve_does_not_discard_other_complete_curves() -> None:
    events = (
        _event("600022.SH", "分钟完整"),
        _event("600023.SH", "分钟缺失"),
    )
    profiles = {
        event.instrument_id: _profile(event.instrument_id, event.name, industry="保险")
        for event in events
    }

    items = attribute_limit_up_follow_pool(
        events,
        profiles,
        {events[0].instrument_id: _stock(events[0].instrument_id)},
        (_sector("insurance", "保险"),),
        trade_date=TRADE_DATE,
        tzinfo=SHANGHAI,
    )

    assert items[0].follow_status == "confirmed"
    assert items[1].follow_status == "unresolved"
    assert "stock_minute_unavailable" in items[1].flags


def test_stock_minutes_are_split_at_the_gateway_batch_limit() -> None:
    instruments = tuple(f"{index:06d}.SH" for index in range(1, 77))
    calls = []

    def fetcher(batch, *, now):
        calls.append((tuple(batch), now))
        return {}

    results, failed_batches = _fetch_stock_minutes_in_batches(
        instruments,
        fetcher,
        generated_at=TARGET,
    )

    assert results == {}
    assert failed_batches == 0
    assert [len(batch) for batch, _now in calls] == [40, 36]
    assert all(now == TARGET for _batch, now in calls)


def test_limit_up_follow_pool_store_round_trips_exact_revision(tmp_path) -> None:
    event = _event("600031.SH", "测试存储")
    profile = _profile(event.instrument_id, event.name, industry="保险")
    item = attribute_limit_up_follow_pool(
        (event,),
        {profile.instrument_id: profile},
        {event.instrument_id: _stock(event.instrument_id)},
        (_sector("insurance", "保险"),),
        trade_date=TRADE_DATE,
        tzinfo=SHANGHAI,
    )[0]
    pool = LimitUpFollowPoolV1.create(
        source_snapshot_revision="a" * 64,
        source_snapshot_id="mw-20260827-1457",
        source_as_of=TARGET,
        trade_date=TRADE_DATE,
        generated_at=TARGET + timedelta(seconds=5),
        limit_event_provider="fixture",
        quality="accepted",
        pool_total=1,
        confirmed_count=1,
        provisional_count=0,
        unresolved_count=0,
        items=(item,),
    )
    path = tmp_path / "limit-up.sqlite3"

    with LimitUpFollowPoolStore(path) as store:
        assert store.record(pool)["action"] == "inserted"
        assert store.record(pool)["action"] == "unchanged"
    with LimitUpFollowPoolStore(path, read_only=True) as store:
        loaded = store.get_by_source_revision("a" * 64)

    assert loaded == pool
