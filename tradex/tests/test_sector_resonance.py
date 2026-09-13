from __future__ import annotations

import threading
import time as time_module
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from tradex.data_gateway.contracts import (
    BoardLeaderSnapshotV2,
    BoardLeaderV2,
    ContractMetadata,
    IntradayMinutePointV1,
    IntradayMinuteSeriesV1,
    QualityStatus,
)
from tradex.market_watch.contracts import SectorFlowSeriesV1
from tradex.market_watch.sector_resonance import (
    _build_sector_resonance_entries,
    _build_sector_resonance_entries_batched,
    build_sector_resonance_snapshot,
    evaluate_minute_resonance,
    select_sector_resonance_candidates,
)
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
            "change_delta_5m_pct": (
                0.5 if cumulative[-1] > 0 else -0.5 if cumulative[-1] < 0 else 0.0
            ),
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
    assert leader.resonance_correlation > 0.999
    assert leader.matched_interval_count == 5


def test_high_correlation_without_fast_five_minute_return_fails_closed():
    leader = evaluate_minute_resonance(
        _sector(),
        _candidate(),
        _stock(returns=(0.001, 0.002, 0.003, 0.004, 0.005)),
    )

    assert leader is None


def test_downward_fund_outflow_is_not_reported_as_a_resonance_leader():
    leader = evaluate_minute_resonance(
        _sector(increments=(-10, -20, -30, -40, -50)),
        _candidate(),
        _stock(returns=(-0.10, -0.20, -0.30, -0.40, -0.50)),
    )

    assert leader is None


def test_same_direction_trend_uses_cumulative_path_not_noisy_increment_sizes():
    leader = evaluate_minute_resonance(
        _sector(increments=(100, 20, 180, 40, 160)),
        _candidate(),
        _stock(returns=(0.10, 0.40, 0.10, 0.40, 0.10)),
    )

    assert leader is not None
    assert leader.resonance_correlation is not None
    assert leader.resonance_correlation >= 0.60
    assert leader.directional_agreement_ratio == pytest.approx(1.0)


def test_opposite_five_minute_direction_is_not_resonance():
    leader = evaluate_minute_resonance(
        _sector(increments=(10, 20, 30, 40, 50)),
        _candidate(),
        _stock(returns=(-0.10, -0.20, -0.30, -0.40, -0.50)),
    )

    assert leader is None


def test_fund_inflow_can_resonate_when_sector_price_delta_is_negative():
    sector = _sector()
    sector = sector.model_copy(update={
        "latest": sector.latest.model_copy(update={
            "change_delta_5m_pct": -0.20,
        }),
    })

    leader = evaluate_minute_resonance(
        sector,
        _candidate(),
        _stock(),
    )

    assert leader is not None
    assert leader.speed_pct is not None and leader.speed_pct > 0


def test_three_exact_shared_intervals_are_sufficient_when_window_endpoints_match():
    offsets = (0, 2, 4, 5)
    leader = evaluate_minute_resonance(
        _sector(offsets=offsets, increments=(30, 70, 50)),
        _candidate(),
        _stock(offsets=offsets, returns=(0.30, 0.70, 0.50)),
    )

    assert leader is not None
    assert leader.matched_interval_count == 3


def test_downward_sector_does_not_request_or_label_a_leader():
    calls = []
    candidate = _candidate().model_copy(update={"speed_pct": -0.5})
    board = BoardLeaderSnapshotV2(
        metadata=ContractMetadata(
            contract="board_leader.v2",
            schema_version=2,
            provider="fixture",
            provider_as_of=TARGET,
            fetched_at=TARGET,
            quality=QualityStatus.ACCEPTED,
        ),
        board_code="BK0474",
        speed_order="asc",
        leaders=(candidate,),
    )

    def board_fetcher(board_code, **kwargs):
        calls.append((board_code, kwargs))
        return board

    result = build_sector_resonance_snapshot(
        _sector(increments=(-10, -20, -30, -40, -50)),
        board_fetcher=board_fetcher,
        minute_fetcher=lambda *_args, **_kwargs: _stock(
            returns=(-0.10, -0.20, -0.30, -0.40, -0.50)
        ),
        fetched_at=TARGET,
    )

    assert calls == []
    assert result.status == "no_match"
    assert result.resonance_direction is None
    assert result.status_label == "板块近5分钟未边际流入"


def test_sector_candidates_follow_dashboard_current_amount_order():
    base = _sector()
    low = base.model_copy(update={
        "sector_key": "low_flow",
        "name": "低资金",
        "latest": base.latest.model_copy(update={"cumulative_cny": 10.0}),
    })
    high = base.model_copy(update={
        "sector_key": "high_flow",
        "name": "高资金",
        "latest": base.latest.model_copy(update={"cumulative_cny": 30.0}),
    })
    middle = base.model_copy(update={
        "sector_key": "middle_flow",
        "name": "中资金",
        "latest": base.latest.model_copy(update={"cumulative_cny": 20.0}),
    })

    selected = select_sector_resonance_candidates(
        (low, high, middle),
        limit=2,
    )

    assert tuple(item.sector_key for item in selected) == (
        "high_flow",
        "middle_flow",
    )


def test_sector_candidates_exclude_current_outflows_even_when_cumulative_is_high():
    inflow = _sector().model_copy(update={
        "sector_key": "active_inflow",
        "name": "边际流入",
    })
    outflow = _sector().model_copy(update={
        "sector_key": "large_but_outflowing",
        "name": "累计很高但边际流出",
        "latest": _sector().latest.model_copy(update={
            "cumulative_cny": 9_999_000_000.0,
            "delta_5m_cny": -100_000_000.0,
        }),
    })

    selected = select_sector_resonance_candidates((outflow, inflow), limit=2)

    assert tuple(item.sector_key for item in selected) == ("active_inflow",)


def test_seconds_around_five_minute_cutoff_can_span_six_minute_buckets():
    sector = _sector(
        offsets=(-1, 0, 1, 2, 3, 4, 5),
        increments=(10, 20, 30, 40, 50, 60),
    )
    recorded_baseline = TARGET - timedelta(minutes=5, seconds=3)
    sector = sector.model_copy(update={
        "latest": sector.latest.model_copy(update={
            "delta_5m_baseline_as_of": recorded_baseline,
        }),
        "points": (
            sector.points[0].model_copy(update={
                "sampled_at": recorded_baseline,
                "provider_as_of": recorded_baseline,
            }),
            *sector.points[1:],
        ),
    })

    leader = evaluate_minute_resonance(
        sector,
        _candidate(),
        _stock(
            offsets=(-1, 0, 1, 2, 3, 4, 5),
            returns=(0.10, 0.20, 0.30, 0.40, 0.50, 0.60),
        ),
    )

    assert leader is not None
    assert leader.matched_interval_count == 6


def test_sector_entries_are_built_concurrently_in_stable_order():
    sectors = tuple(
        _sector().model_copy(update={
            "sector_key": f"sector_{index}",
            "name": f"板块{index}",
        })
        for index in range(4)
    )
    board = BoardLeaderSnapshotV2(
        metadata=ContractMetadata(
            contract="board_leader.v2",
            schema_version=2,
            provider="fixture",
            provider_as_of=TARGET,
            fetched_at=TARGET,
            quality=QualityStatus.ACCEPTED,
        ),
        board_code="BK0474",
        speed_order="desc",
        leaders=(_candidate(),),
    )
    lock = threading.Lock()
    release = threading.Event()
    active = 0
    maximum_active = 0
    minute_active = 0
    maximum_minute_active = 0

    def board_fetcher(*_args, **_kwargs):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            if active >= 2:
                release.set()
        assert release.wait(timeout=1)
        with lock:
            active -= 1
        return board

    def minute_fetcher(*_args, **_kwargs):
        nonlocal minute_active, maximum_minute_active
        with lock:
            minute_active += 1
            maximum_minute_active = max(maximum_minute_active, minute_active)
        time_module.sleep(0.02)
        with lock:
            minute_active -= 1
        return _stock()

    entries = _build_sector_resonance_entries(
        tuple(("offense", sector) for sector in sectors),
        board_fetcher=board_fetcher,
        minute_fetcher=minute_fetcher,
        candidate_limit=1,
        fetched_at=TARGET,
        max_workers=2,
    )

    assert maximum_active >= 2
    assert maximum_minute_active == 1
    assert tuple(item.sector_key for item in entries) == tuple(
        item.sector_key for item in sectors
    )


def test_sector_entries_fetch_all_candidate_minutes_in_one_batch():
    first = _sector()
    second = _sector().model_copy(update={
        "sector_key": "second_sector",
        "name": "第二板块",
        "leader_board_code": "BK0002",
    })
    second_candidate = _candidate().model_copy(update={
        "instrument_id": "600000.SH",
        "name": "浦发银行",
    })

    def board_fetcher(board_code, **_kwargs):
        candidate = _candidate() if board_code == "BK0474" else second_candidate
        return BoardLeaderSnapshotV2(
            metadata=ContractMetadata(
                contract="board_leader.v2",
                schema_version=2,
                provider="fixture",
                provider_as_of=TARGET,
                fetched_at=TARGET,
                quality=QualityStatus.ACCEPTED,
            ),
            board_code=board_code,
            speed_order="desc",
            leaders=(candidate,),
        )

    batch_calls = []

    def minute_batch_fetcher(symbols, **_kwargs):
        batch_calls.append(symbols)
        return {
            "601628.SH": _stock(),
            "600000.SH": _stock().model_copy(update={"instrument_id": "600000.SH"}),
        }

    entries = _build_sector_resonance_entries_batched(
        (("offense", first), ("offense", second)),
        board_fetcher=board_fetcher,
        minute_batch_fetcher=minute_batch_fetcher,
        candidate_limit=1,
        fetched_at=TARGET,
        max_workers=2,
    )

    assert batch_calls == [("600000.SH", "601628.SH")]
    assert [item.leader_snapshot.status for item in entries] == ["full", "full"]


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
    assert leader.resonance_correlation > 0.999
    assert leader.matched_interval_count == 4


def test_recorded_five_minute_baseline_is_used_when_minute_bucket_is_older():
    sector = _sector()
    recorded_baseline = TARGET - timedelta(minutes=5, seconds=3)
    shifted_points = (
        sector.points[0].model_copy(update={
            "sampled_at": recorded_baseline,
            "provider_as_of": recorded_baseline,
        }),
        *sector.points[1:],
    )
    sector = sector.model_copy(update={
        "latest": sector.latest.model_copy(update={
            "delta_5m_baseline_as_of": recorded_baseline,
        }),
        "points": shifted_points,
    })

    leader = evaluate_minute_resonance(
        sector,
        _candidate(),
        _stock(offsets=(-1, 1, 2, 3, 4, 5)),
    )

    assert leader is not None
    assert leader.matched_interval_count == 5


def test_recent_complete_window_is_used_when_stock_minutes_lag_one_minute():
    sector = _sector()
    fallback_baseline = TARGET - timedelta(minutes=6)
    older_point = sector.points[0].model_copy(update={
        "sampled_at": fallback_baseline,
        "provider_as_of": fallback_baseline,
        "cumulative_cny": -10_000_000.0,
    })
    fallback_target = sector.points[-2].model_copy(update={
        "delta_5m_cny": 110_000_000.0,
        "delta_5m_baseline_as_of": fallback_baseline,
    })
    sector = sector.model_copy(update={
        "points": (
            older_point,
            *sector.points[:-2],
            fallback_target,
            sector.points[-1],
        ),
    })

    leader = evaluate_minute_resonance(
        sector,
        _candidate(),
        _stock(
            offsets=(-1, 0, 1, 2, 3, 4),
            returns=(0.10, 0.20, 0.30, 0.40, 0.50),
        ),
    )

    assert leader is not None
    assert leader.provider_as_of == TARGET - timedelta(minutes=1)
    assert leader.matched_interval_count == 5


def test_minute_lag_beyond_two_minutes_is_unavailable_not_no_match():
    candidate = _candidate()
    board = BoardLeaderSnapshotV2(
        metadata=ContractMetadata(
            contract="board_leader.v2",
            schema_version=2,
            provider="fixture",
            provider_as_of=TARGET,
            fetched_at=TARGET,
            quality=QualityStatus.ACCEPTED,
        ),
        board_code="BK0474",
        speed_order="desc",
        leaders=(candidate,),
    )

    result = build_sector_resonance_snapshot(
        _sector(),
        board_fetcher=lambda *_args, **_kwargs: board,
        minute_fetcher=lambda *_args, **_kwargs: _stock(
            offsets=(0, 1, 2),
            returns=(0.10, 0.20),
        ),
        fetched_at=TARGET,
    )

    assert result.status == "unavailable"
    assert result.status_label == "分钟证据不足"


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


@pytest.mark.parametrize("fail_first", [False, True])
def test_large_candidate_set_keeps_every_instrument_and_isolates_failed_chunk(fail_first):
    jobs = tuple(("offense", _sector().model_copy(update={
        "sector_key": f"sector_{index}", "leader_board_code": f"BK{index:04d}",
    })) for index in range(9))
    def board_fetcher(board_code, **kwargs):
        index = int(board_code[2:])
        return BoardLeaderSnapshotV2(
            metadata=ContractMetadata(contract="board_leader.v2", schema_version=2,
                provider="fixture", provider_as_of=TARGET, fetched_at=TARGET,
                quality=QualityStatus.ACCEPTED),
            board_code=board_code, speed_order="desc",
            leaders=tuple(_candidate().model_copy(update={
                "instrument_id": f"{600000 + index * 5 + offset}.SH",
            }) for offset in range(5)),
        )
    calls = []
    def minutes(symbols, **kwargs):
        calls.append(symbols)
        if fail_first and len(calls) == 1:
            raise RuntimeError("first chunk unavailable")
        return {symbol: _stock().model_copy(update={"instrument_id": symbol}) for symbol in symbols}
    entries = _build_sector_resonance_entries_batched(jobs, board_fetcher=board_fetcher,
        minute_batch_fetcher=minutes, candidate_limit=5, fetched_at=TARGET, max_workers=1)
    assert [len(chunk) for chunk in calls] == [40, 5]
    assert len(set(symbol for chunk in calls for symbol in chunk)) == 45
    assert entries[-1].leader_snapshot.status == "full"
    assert entries[0].leader_snapshot.status == ("unavailable" if fail_first else "full")
