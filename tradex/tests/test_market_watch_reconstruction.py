from __future__ import annotations

from datetime import date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tradex.market_watch import reconstruction
from tradex.market_watch.reconstruction import (
    HistoricalTrajectoryNotPublished,
    HistoricalTrajectoryUnavailable,
    SameDayPostCloseReconstructor,
    _assert_rotation_trajectory_current,
    _prepare_historical_rotation,
)
from tradex.market_watch.recovery_source_cache import RecoverySourceMatrixStore


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _trajectory_sector(key: str, provider_as_of: datetime) -> dict:
    return {
        "sector_key": key,
        "name": key,
        "latest": {
            "provider_as_of": provider_as_of.isoformat(),
            "cumulative_cny": 100.0,
        },
    }


def test_rotation_preflight_checks_only_the_prepared_backfill_scope() -> None:
    target = datetime(2026, 8, 31, 13, 30, tzinfo=SHANGHAI)
    risk = {
        "sector_flow_trajectory": {
            "sectors": [
                _trajectory_sector("required", target),
                _trajectory_sector("optional", target.replace(minute=28)),
            ]
        }
    }

    _assert_rotation_trajectory_current(
        risk,
        target,
        required_sector_keys={"required"},
    )
    component = _prepare_historical_rotation(
        risk,
        target,
        required_sector_keys={"required"},
    )

    assert component["quality"] == "degraded"
    assert [item["sector_key"] for item in risk["rotation"]["sectors"]] == [
        "required"
    ]

    with pytest.raises(HistoricalTrajectoryUnavailable, match="required"):
        _assert_rotation_trajectory_current(
            risk,
            target,
            required_sector_keys={"required", "missing_required"},
        )


def test_rotation_preflight_does_not_require_an_unrelated_gap_minute() -> None:
    unavailable = datetime(2026, 9, 2, 9, 30, tzinfo=SHANGHAI)
    target = datetime(2026, 9, 2, 9, 35, tzinfo=SHANGHAI)
    prepared_minutes = []

    def prepare_curves(*, required_minutes, **_kwargs):
        prepared_minutes.append(tuple(required_minutes))
        requested = set(required_minutes)
        return {
            "complete": requested == {target},
            "known_targets": 1,
            "ready_target_keys": ("required",),
            "missing_targets": (() if requested == {target} else ("required",)),
        }

    owner = SameDayPostCloseReconstructor(
        history=_History(),
        target_minutes=lambda _date: (unavailable, target),
        rotation_loader=lambda _target: (_ for _ in ()).throw(
            RuntimeError("target-only rotation load reached")
        ),
        rotation_curve_preparer=prepare_curves,
        clock=lambda: target.replace(hour=16),
    )

    with pytest.raises(RuntimeError, match="target-only rotation load reached"):
        owner.reconstruct(target, lambda *_args: None)

    assert prepared_minutes == [(target,)]


def test_rotation_preflight_marks_an_unpublished_boundary_minute_terminal() -> None:
    target = datetime(2026, 9, 2, 9, 30, tzinfo=SHANGHAI)
    rotation_loaded = False

    def load_rotation(_target):
        nonlocal rotation_loaded
        rotation_loaded = True
        return {}

    owner = SameDayPostCloseReconstructor(
        history=_History(),
        target_minutes=lambda _date: (target,),
        rotation_loader=load_rotation,
        rotation_curve_preparer=lambda **_kwargs: {
            "complete": False,
            "known_targets": 1,
            "ready_target_keys": (),
            "missing_targets": ("required",),
            "unavailable_minutes": (target,),
        },
        clock=lambda: target.replace(hour=16),
    )

    with pytest.raises(HistoricalTrajectoryNotPublished, match="09:30"):
        owner.reconstruct(target, lambda *_args: None)

    assert rotation_loaded is False


def test_reconstructor_reuses_persisted_same_day_source_matrix(
    tmp_path,
    monkeypatch,
) -> None:
    target = datetime(2026, 8, 31, 13, 30, tzinfo=SHANGHAI)
    cache = RecoverySourceMatrixStore(tmp_path / "recovery-source.sqlite3")
    cache.record(
        trade_date=target.date(),
        provider_as_of=target.replace(hour=15, minute=0),
        previous_close={"000001.SZ": 10.0},
        stock_prices={target.time(): {"000001.SZ": 10.2}},
        index_points={
            instrument_id: {
                target.time(): {"close": 100.0, "amount_cny": 10.0}
            }
            for instrument_id in (
                "000001.SH",
                "000300.SH",
                "000852.SH",
                "399006.SZ",
                "399001.SZ",
            )
        },
        index_previous_close={
            instrument_id: (instrument_id, 99.0)
            for instrument_id in (
                "000001.SH",
                "000300.SH",
                "000852.SH",
                "399006.SZ",
            )
        },
    )
    monkeypatch.setattr(
        reconstruction,
        "fetch_a_share_universe_snapshot",
        lambda **_kwargs: pytest.fail("a valid same-day cache must avoid provider reload"),
    )
    progress = []
    owner = SameDayPostCloseReconstructor(
        history=_History(),
        target_minutes=lambda _date: (target,),
        rotation_loader=lambda _target: {},
        clock=lambda: target.replace(hour=16),
        source_cache=cache,
    )

    owner._ensure_loaded(
        target,
        lambda done, total, stage, message=None: progress.append(
            (done, total, stage, message)
        ),
    )

    assert owner._stock_prices[target.time()] == {"000001.SZ": 10.2}
    assert owner._index_points["000001.SH"][target.time()].close == 100.0
    assert progress[-1][2] == "source_cache"


def test_reconstructor_bulk_load_skips_minutes_not_published_by_curve_contracts(
    monkeypatch,
) -> None:
    target = datetime(2026, 9, 4, 11, 21, tzinfo=SHANGHAI)
    lunch_boundary = target.replace(hour=13, minute=0)
    later_gap = target.replace(hour=13, minute=1)
    auction = target.replace(hour=9, minute=25)
    included_times = {target.time(), later_gap.time()}
    universe = SimpleNamespace(
        metadata=SimpleNamespace(provider_as_of=target.replace(hour=15, minute=0)),
        quotes=(SimpleNamespace(instrument_id="000001.SZ", previous_close=10.0),),
        excluded_row_count=0,
    )
    stock_series = SimpleNamespace(
        trading_date=target.date(),
        points=tuple(
            SimpleNamespace(minute=minute, price=10.2)
            for minute in included_times
        ),
    )

    monkeypatch.setattr(
        reconstruction,
        "fetch_a_share_universe_snapshot",
        lambda **_kwargs: universe,
    )
    monkeypatch.setattr(
        reconstruction,
        "fetch_intraday_minute_series_batch_partial",
        lambda _batch, **_kwargs: {"000001.SZ": stock_series},
    )
    monkeypatch.setattr(
        reconstruction,
        "fetch_market_overview",
        lambda **_kwargs: SimpleNamespace(
            indices=tuple(
                SimpleNamespace(
                    instrument_id=instrument_id,
                    name=instrument_id,
                    previous_close=99.0,
                    available=True,
                )
                for instrument_id in (
                    "000001.SH",
                    "000300.SH",
                    "000852.SH",
                    "399006.SZ",
                )
            )
        ),
    )
    monkeypatch.setattr(
        reconstruction,
        "fetch_index_intraday_series",
        lambda _instrument_id, **_kwargs: SimpleNamespace(
            points=tuple(
                SimpleNamespace(
                    trading_date=target.date(),
                    minute=minute,
                    close=100.0,
                    amount_cny=10.0,
                )
                for minute in included_times
            )
        ),
    )
    owner = SameDayPostCloseReconstructor(
        history=_History(),
        target_minutes=lambda _date: (
            auction,
            target,
            lunch_boundary,
            later_gap,
        ),
        rotation_loader=lambda _target: {},
        clock=lambda: target.replace(hour=16),
        batch_concurrency=1,
    )

    owner._ensure_loaded(target, lambda *_args: None)

    assert set(owner._stock_prices) == included_times
    assert set(owner._index_points["000001.SH"]) == included_times


def test_reconstructor_retries_only_stock_batches_still_missing(
    monkeypatch,
) -> None:
    target = datetime(2026, 8, 31, 13, 30, tzinfo=SHANGHAI)
    codes = tuple(f"{number:06d}.SZ" for number in range(1, 42))
    universe = SimpleNamespace(
        metadata=SimpleNamespace(provider_as_of=target.replace(hour=15, minute=0)),
        quotes=tuple(
            SimpleNamespace(instrument_id=code, previous_close=10.0)
            for code in codes
        ),
        excluded_row_count=0,
    )
    batch_sizes = []

    def load_batch(batch, **_kwargs):
        batch_sizes.append(len(batch))
        if len(batch_sizes) == 2:
            raise RuntimeError("fixture transient batch failure")
        return {
            code: SimpleNamespace(
                trading_date=target.date(),
                points=(SimpleNamespace(minute=target.time(), price=10.2),),
            )
            for code in batch
        }

    monkeypatch.setattr(
        reconstruction,
        "fetch_a_share_universe_snapshot",
        lambda **_kwargs: universe,
    )
    monkeypatch.setattr(
        reconstruction,
        "fetch_intraday_minute_series_batch_partial",
        load_batch,
    )
    monkeypatch.setattr(
        reconstruction,
        "fetch_market_overview",
        lambda **_kwargs: SimpleNamespace(
            indices=tuple(
                SimpleNamespace(
                    instrument_id=instrument_id,
                    name=instrument_id,
                    previous_close=99.0,
                    available=True,
                )
                for instrument_id in (
                    "000001.SH",
                    "000300.SH",
                    "000852.SH",
                    "399006.SZ",
                )
            )
        ),
    )
    monkeypatch.setattr(
        reconstruction,
        "fetch_index_intraday_series",
        lambda _instrument_id, **_kwargs: SimpleNamespace(
            points=(
                SimpleNamespace(
                    trading_date=target.date(),
                    minute=target.time(),
                    close=100.0,
                    amount_cny=10.0,
                ),
            )
        ),
    )
    owner = SameDayPostCloseReconstructor(
        history=_History(),
        target_minutes=lambda _date: (target,),
        rotation_loader=lambda _target: {},
        clock=lambda: target.replace(hour=16),
        batch_concurrency=1,
    )

    with pytest.raises(RuntimeError, match="transient batch failure"):
        owner._ensure_loaded(target, lambda *_args: None)
    owner._ensure_loaded(target, lambda *_args: None)

    assert batch_sizes == [40, 1, 1]
    assert len(owner._stock_prices[target.time()]) == 41


class _History:
    def list_dates(self, limit=8):
        return [{"trade_date": "2026-08-26"}]

    def get_collection_records(self, trade_date):
        return [
            {
                "minute_bucket": "2026-08-26T14:53:00+08:00",
                "record_kind": "accepted_real",
            }
        ]

    def get_timeline(self, trade_date):
        return [
            {
                "minute_bucket": "2026-08-26T14:53:00+08:00",
                "payload": {
                    "turnover": {
                        "available": True,
                        "today_date": "2026-08-26",
                        "as_of": "14:53",
                        "today_amount_cny": 250.0,
                    }
                },
            }
        ]


@pytest.mark.parametrize("target_time", [time(9, 35), time(14, 53)])
def test_same_day_post_close_reconstruction_uses_exact_target_facts(
    monkeypatch,
    target_time,
):
    target = datetime.combine(date(2026, 8, 27), target_time, SHANGHAI)
    observed = datetime(2026, 8, 27, 16, 0, tzinfo=SHANGHAI)
    universe = SimpleNamespace(
        metadata=SimpleNamespace(
            provider_as_of=datetime(2026, 8, 27, 15, 0, tzinfo=SHANGHAI)
        ),
        quotes=(
            SimpleNamespace(instrument_id="000001.SZ", previous_close=10.0),
            SimpleNamespace(instrument_id="600000.SH", previous_close=12.0),
        ),
        excluded_row_count=0,
    )
    stock_series = {
        "000001.SZ": SimpleNamespace(
            trading_date=target.date(),
            points=(SimpleNamespace(minute=target.time(), price=10.2),),
        ),
        "600000.SH": SimpleNamespace(
            trading_date=target.date(),
            points=(SimpleNamespace(minute=target.time(), price=11.8),),
        ),
    }
    previous = {
        "000001.SH": 3900.0,
        "000300.SH": 4600.0,
        "000852.SH": 7600.0,
        "399006.SZ": 3400.0,
    }
    overview = SimpleNamespace(
        indices=tuple(
            SimpleNamespace(
                instrument_id=instrument_id,
                name=instrument_id,
                previous_close=value,
                available=True,
            )
            for instrument_id, value in previous.items()
        )
    )
    index_values = {
        "000001.SH": (3950.0, 100.0),
        "000300.SH": (4650.0, 20.0),
        "000852.SH": (7700.0, 30.0),
        "399006.SZ": (3450.0, 40.0),
        "399001.SZ": (12500.0, 200.0),
    }

    monkeypatch.setattr(
        reconstruction,
        "fetch_a_share_universe_snapshot",
        lambda **_kwargs: universe,
    )
    monkeypatch.setattr(
        reconstruction,
        "fetch_intraday_minute_series_batch_partial",
        lambda _batch, **_kwargs: stock_series,
    )
    monkeypatch.setattr(
        reconstruction,
        "fetch_market_overview",
        lambda **_kwargs: overview,
    )
    monkeypatch.setattr(
        reconstruction,
        "fetch_index_intraday_series",
        lambda instrument_id, **_kwargs: SimpleNamespace(
            points=(
                SimpleNamespace(
                    trading_date=target.date(),
                    minute=target.time(),
                    open=index_values[instrument_id][0],
                    close=index_values[instrument_id][0],
                    high=index_values[instrument_id][0],
                    low=index_values[instrument_id][0],
                    amount_cny=index_values[instrument_id][1],
                ),
            )
        ),
    )
    progress = []
    owner = SameDayPostCloseReconstructor(
        history=_History(),
        target_minutes=lambda _date: (target,),
        rotation_loader=lambda _target: {
            "offense": {
                "lists": {
                    "attacking": [
                        {
                            "key": "securities",
                            "name": "证券",
                            "change_pct": 1.2,
                            "breadth": {"ratio": 0.7},
                            "provider_as_of": target.isoformat(),
                        }
                    ],
                    "rotating": [],
                    "cooling": [],
                    "unclassified": [],
                },
                "events": [],
            }
        },
        clock=lambda: observed,
        sleep=lambda _seconds: None,
        batch_pause_seconds=0,
    )
    if target_time == time(9, 35):
        owner._previous_turnover = lambda _target: (date(2026, 8, 26), 250.0)

    snapshot = owner.reconstruct(
        target,
        lambda done, total, stage, message=None: progress.append(
            (done, total, stage, message)
        ),
    )

    assert snapshot.as_of == target
    assert snapshot.breadth.up_count == 1
    assert snapshot.breadth.down_count == 1
    assert snapshot.breadth.unclassified_count == 0
    assert snapshot.turnover.today_amount_cny == 300.0
    assert snapshot.turnover.previous_same_time_amount_cny == 250.0
    assert len(snapshot.indices) == 4
    assert snapshot.rotation.sectors
    assert progress[-1][:3] == (6, 6, "contract")


def test_cross_day_reconstruction_fails_before_provider_calls() -> None:
    target = datetime(2026, 8, 26, 14, 53, tzinfo=SHANGHAI)
    owner = SameDayPostCloseReconstructor(
        history=_History(),
        target_minutes=lambda _date: (target,),
        rotation_loader=lambda _target: {},
        clock=lambda: datetime(2026, 8, 27, 16, 0, tzinfo=SHANGHAI),
    )

    try:
        owner.reconstruct(target, lambda *_args: None)
    except RuntimeError as exc:
        assert "before midnight" in str(exc)
    else:
        raise AssertionError("cross-day reconstruction must fail closed")


def test_cross_day_0925_reconstruction_uses_exact_auction_aggregate(monkeypatch) -> None:
    target = datetime(2026, 8, 28, 9, 25, tzinfo=SHANGHAI)
    observed = datetime(2026, 8, 29, 0, 20, tzinfo=SHANGHAI)
    auction = {
        date(2026, 8, 27): SimpleNamespace(
            trading_date=date(2026, 8, 27),
            instrument_count=5_502,
            provider_row_count=5_502,
            excluded_row_count=0,
            up_count=1_654,
            down_count=3_105,
            flat_count=743,
            total_amount_cny=17_148_572_585.0,
            metadata=SimpleNamespace(quality=SimpleNamespace(value="accepted")),
        ),
        date(2026, 8, 28): SimpleNamespace(
            trading_date=date(2026, 8, 28),
            instrument_count=5_507,
            provider_row_count=5_507,
            excluded_row_count=0,
            up_count=1_873,
            down_count=2_785,
            flat_count=849,
            total_amount_cny=20_565_135_915.0,
            metadata=SimpleNamespace(quality=SimpleNamespace(value="accepted")),
        ),
    }
    previous = {
        "000001.SH": 3_956.57,
        "000300.SH": 4_630.28,
        "000852.SH": 7_732.945,
        "399006.SZ": 3_473.36,
    }
    opens = {
        "000001.SH": 3_950.24,
        "000300.SH": 4_615.84,
        "000852.SH": 7_734.19,
        "399006.SZ": 3_453.73,
    }
    monkeypatch.setattr(
        reconstruction,
        "fetch_opening_auction_market",
        lambda trading_date, **_kwargs: auction[trading_date],
    )
    monkeypatch.setattr(
        reconstruction,
        "fetch_market_overview",
        lambda **_kwargs: SimpleNamespace(
            indices=tuple(
                SimpleNamespace(
                    instrument_id=instrument_id,
                    name=instrument_id,
                    previous_close=value,
                    available=True,
                )
                for instrument_id, value in previous.items()
            )
        ),
    )
    monkeypatch.setattr(
        reconstruction,
        "fetch_index_intraday_series",
        lambda instrument_id, **_kwargs: SimpleNamespace(
            points=(
                SimpleNamespace(
                    trading_date=target.date(),
                    minute=time(9, 30),
                    open=opens[instrument_id],
                ),
            )
        ),
    )
    progress = []
    owner = SameDayPostCloseReconstructor(
        history=_History(),
        target_minutes=lambda _date: (target,),
        rotation_loader=lambda _target: {},
        clock=lambda: observed,
    )

    snapshot = owner.reconstruct_opening_auction(
        target,
        lambda done, total, stage, message=None: progress.append(
            (done, total, stage, message)
        ),
    )

    assert snapshot.market_state.phase.value == "pre_open"
    assert snapshot.market_state.is_open is False
    assert snapshot.breadth.total_count == 5_507
    assert snapshot.breadth.up_count == 1_873
    assert snapshot.turnover.today_amount_cny == 20_565_135_915.0
    assert snapshot.turnover.previous_same_time_amount_cny == 17_148_572_585.0
    assert snapshot.turnover.as_of == "09:25"
    assert snapshot.freshness.status.value == "degraded"
    assert snapshot.rotation.sectors == ()
    assert snapshot.sector_flow_trajectory is None
    assert progress[-1][:3] == (5, 5, "auction_contract")
