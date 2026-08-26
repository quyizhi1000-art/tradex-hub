from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from tradex.data_gateway.contracts import (
    BoardLeaderV2,
    ContractMetadata,
    IntradayMinutePointV1,
    IntradayMinuteSeriesV1,
    QualityStatus,
)
from tradex.market_watch.contracts import SectorFlowSeriesV1
from tradex.market_watch.sector_resonance import evaluate_minute_resonance
from tradex.market_watch.sector_resonance_store import SectorResonanceStore
from tradex.market_watch.sector_resonance import SectorResonanceBatchV1


SHANGHAI = ZoneInfo("Asia/Shanghai")
TARGET = datetime(2026, 8, 26, 14, 57, tzinfo=SHANGHAI)


def _sector(offsets=(0, 1, 2, 3, 4, 5), increments=(10, 20, 30, 40, 50)):
    cumulative = [0.0]
    for value in increments:
        cumulative.append(cumulative[-1] + value * 1_000_000)
    points = []
    for offset, value in zip(offsets, cumulative, strict=True):
        observed = TARGET - timedelta(minutes=5 - offset)
        points.append({
            "sampled_at": observed,
            "provider_as_of": observed,
            "session_segment": "pm",
            "cumulative_cny": value,
        })
    return SectorFlowSeriesV1.model_validate({
        "sector_key": "insurance",
        "name": "保险",
        "category_key": "steady_defense",
        "category_name": "稳态防御",
        "taxonomy": "industry",
        "leader_board_code": "BK0474",
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
            "delta_5m_cny": cumulative[-1],
            "delta_5m_baseline_as_of": TARGET - timedelta(minutes=5),
            "current_strength": "strong",
            "fund_strength": "strong",
            "incremental_direction": "inflow",
        },
        "points": points,
    })


def _stock(
    returns=(0.10, 0.20, 0.30, 0.40, 0.50),
    offsets=(0, 1, 2, 3, 4, 5),
):
    prices = [100.0]
    for value in returns:
        prices.append(prices[-1] * (1.0 + value / 100.0))
    points = tuple(
        IntradayMinutePointV1(
            minute=(TARGET - timedelta(minutes=5 - offset)).time(),
            price=price,
            volume_shares=100.0,
        )
        for offset, price in zip(offsets, prices, strict=True)
    )
    return IntradayMinuteSeriesV1(
        metadata=ContractMetadata(
            contract="intraday_minute_series.v1",
            provider="fixture",
            provider_as_of=TARGET,
            fetched_at=TARGET,
            quality=QualityStatus.ACCEPTED,
        ),
        instrument_id="601628.SH",
        trading_date=date(2026, 8, 26),
        points=points,
    )


def _candidate():
    return BoardLeaderV2(
        instrument_id="601628.SH",
        name="中国人寿",
        price=101.0,
        change_pct=1.2,
        speed_pct=0.5,
        provider_as_of=TARGET,
        provider_variant="fixture",
    )


def test_high_shared_minute_correlation_and_fast_return_qualifies():
    leader = evaluate_minute_resonance(_sector(), _candidate(), _stock())

    assert leader is not None
    assert leader.instrument_id == "601628.SH"
    assert leader.speed_pct is not None and leader.speed_pct > 1.4
    assert leader.resonance_correlation == pytest.approx(1.0)
    assert leader.matched_interval_count == 5


def test_high_correlation_without_fast_five_minute_return_fails_closed():
    leader = evaluate_minute_resonance(
        _sector(),
        _candidate(),
        _stock(returns=(0.001, 0.002, 0.003, 0.004, 0.005)),
    )

    assert leader is None


def test_one_shared_missing_minute_uses_the_same_two_minute_interval():
    offsets = (0, 1, 2, 4, 5)
    increments = (10, 20, 70, 50)
    returns = (0.10, 0.20, 0.70, 0.50)

    leader = evaluate_minute_resonance(
        _sector(offsets=offsets, increments=increments),
        _candidate(),
        _stock(offsets=offsets, returns=returns),
    )

    assert leader is not None
    assert leader.resonance_correlation == pytest.approx(1.0)
    assert leader.matched_interval_count == 4


def test_more_than_two_minute_gap_fails_closed():
    offsets = (0, 1, 4, 5)
    leader = evaluate_minute_resonance(
        _sector(offsets=offsets, increments=(10, 90, 50)),
        _candidate(),
        _stock(offsets=offsets, returns=(0.10, 0.90, 0.50)),
    )

    assert leader is None


def test_resonance_store_round_trip_and_missing_read_only_store(tmp_path):
    path = tmp_path / "resonance.sqlite3"
    batch = SectorResonanceBatchV1.create(
        source_snapshot_revision="1" * 64,
        source_snapshot_id="snapshot-1",
        source_as_of=TARGET,
        generated_at=TARGET + timedelta(minutes=10),
        entries=(),
    )
    with SectorResonanceStore(path) as store:
        result = store.record(batch)
        assert result["action"] == "inserted"
    with SectorResonanceStore(path, read_only=True) as store:
        loaded = store.get_by_source_revision("1" * 64)
        assert loaded == batch
        assert store.get_latest_before(TARGET + timedelta(minutes=5)) == batch
        assert store.get_latest_before(TARGET + timedelta(minutes=7)) is None
    with SectorResonanceStore(tmp_path / "absent.sqlite3", read_only=True) as store:
        assert store.get_by_source_revision("2" * 64) is None
