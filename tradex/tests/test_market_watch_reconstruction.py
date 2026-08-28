from __future__ import annotations

from datetime import date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from tradex.market_watch import reconstruction
from tradex.market_watch.reconstruction import SameDayPostCloseReconstructor


SHANGHAI = ZoneInfo("Asia/Shanghai")


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


def test_same_day_post_close_reconstruction_uses_exact_target_facts(monkeypatch):
    target = datetime(2026, 8, 27, 14, 53, tzinfo=SHANGHAI)
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
