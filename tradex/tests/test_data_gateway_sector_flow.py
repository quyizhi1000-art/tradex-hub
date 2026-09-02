"""Canonical sector intraday fund-flow gateway tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from threading import Event
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import tradex.data_gateway.sector_flow as sector_flow_module

from tradex.data_gateway import (
    SectorFundFlowBackfillCache,
    SectorFundFlowStore,
    fetch_sector_intraday_fund_flow,
    fetch_sector_intraday_fund_flow_backfill,
    finalize_sector_intraday_fund_flow_backfill,
)
from tradex.data_gateway.sector_flow import (
    SectorFundFlowBackfillRefresher,
    read_sector_intraday_fund_flow_backfill,
    schedule_sector_intraday_fund_flow_backfill,
)
from tradex.data_sources.http_fetchers import (
    fetch_sector_intraday_fund_flow_eastmoney,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 8, 24)
NOW = datetime(2026, 8, 24, 10, 40, tzinfo=SHANGHAI)
TARGET = {
    "sector_key": "electric_power",
    "name": "电力",
    "taxonomy": "industry",
    "provider_sector_code": "BK0428",
}


def _frame(trading_date: date = TRADE_DATE) -> pd.DataFrame:
    start = datetime.combine(trading_date, datetime.min.time(), SHANGHAI).replace(
        hour=9,
        minute=31,
    )
    frame = pd.DataFrame(
        [
            {
                "provider_as_of": (start + timedelta(minutes=offset)).isoformat(),
                "main_net_inflow_cny": 100_000_000.0 + offset * 10_000_000.0,
            }
            for offset in range(6)
        ]
    )
    frame.attrs["provider_request_id"] = "fixture-request"
    return frame


class _Router:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail_first = fail_first

    def route_validated(self, data_type: str, validator, **kwargs: object):
        self.calls.append((data_type, kwargs))
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("fixture upstream unavailable")
        frame = _frame(kwargs["trade_date"])
        return validator(frame, "eastmoney"), "eastmoney"


class _Response:
    headers = {"x-request-id": "provider-fixture"}

    def __init__(self, klines: list[str]) -> None:
        self._klines = klines

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"data": {"klines": self._klines}}


def test_sector_flow_gateway_routes_and_validates_the_exact_contract():
    router = _Router()

    series = fetch_sector_intraday_fund_flow(
        **TARGET,
        trading_date=TRADE_DATE,
        now=NOW,
        router=router,
    )

    assert router.calls == [
        (
            "sector_intraday_fund_flow",
            {"provider_sector_code": "BK0428", "trade_date": TRADE_DATE},
        )
    ]
    assert series.metadata.contract == "sector_intraday_fund_flow.v1"
    assert series.metadata.provider == "eastmoney"
    assert series.metadata.provider_request_id == "fixture-request"
    assert series.trading_date == TRADE_DATE
    assert series.points[-1].cumulative_cny == 150_000_000.0


def test_eastmoney_fetcher_maps_exact_minutes_and_filters_other_dates(monkeypatch):
    captured = {}

    def em_get(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return _Response(
            [
                "2026-08-21 15:00,999,0,0,0,0,0",
                "2026-08-24 09:31,100000000,0,0,0,0,0",
                "2026-08-24 09:32,120000000,0,0,0,0,0",
            ]
        )

    monkeypatch.setattr("tradex.data_sources.em_client.em_get", em_get)

    frame = fetch_sector_intraday_fund_flow_eastmoney(
        provider_sector_code="BK0428",
        trade_date=TRADE_DATE,
    )

    assert captured["params"]["secid"] == "90.BK0428"
    assert captured["params"]["klt"] == 1
    assert frame["main_net_inflow_cny"].tolist() == [100_000_000.0, 120_000_000.0]
    assert frame.attrs["provider_sector_code"] == "BK0428"
    assert frame.attrs["provider_request_id"] == "provider-fixture"
    assert frame.attrs["provider_transport"] == "push2"


def test_eastmoney_fetcher_honors_low_priority_repair_budget(monkeypatch):
    captured = {}

    def em_get(_url, **kwargs):
        captured.update(kwargs)
        return _Response(["2026-08-24 09:31,100000000,0,0,0,0,0"])

    monkeypatch.setattr("tradex.data_sources.em_client.em_get", em_get)

    fetch_sector_intraday_fund_flow_eastmoney(
        provider_sector_code="BK0428",
        trade_date=TRADE_DATE,
        max_queue_wait=2.0,
        request_timeout=4,
    )

    assert captured["max_queue_wait"] == 2.0
    assert captured["timeout"] == 4


def test_eastmoney_fetcher_uses_delay_mirror_after_main_transport_failure(
    monkeypatch,
):
    calls = []

    def em_get(url, **_kwargs):
        calls.append(url)
        if "push2delay" not in url:
            raise RuntimeError("fixture main transport unavailable")
        return _Response(
            ["2026-08-24 09:31,100000000,0,0,0,0,0"]
        )

    monkeypatch.setattr("tradex.data_sources.em_client.em_get", em_get)

    frame = fetch_sector_intraday_fund_flow_eastmoney(
        provider_sector_code="BK0428",
        trade_date=TRADE_DATE,
    )

    assert calls == [
        "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
        "https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get",
    ]
    assert frame.attrs["provider_transport"] == "push2delay"


def test_eastmoney_fetcher_rejects_empty_main_before_using_delay_mirror(monkeypatch):
    calls = []

    def em_get(url, **_kwargs):
        calls.append(url)
        if "push2delay" not in url:
            return _Response(["2026-08-21 15:00,999,0,0,0,0,0"])
        return _Response(
            ["2026-08-24 09:31,100000000,0,0,0,0,0"]
        )

    monkeypatch.setattr("tradex.data_sources.em_client.em_get", em_get)

    frame = fetch_sector_intraday_fund_flow_eastmoney(
        provider_sector_code="BK0428",
        trade_date=TRADE_DATE,
    )

    assert len(calls) == 2
    assert frame.attrs["provider_transport"] == "push2delay"


def test_eastmoney_fetcher_rejects_semantically_empty_day(monkeypatch):
    monkeypatch.setattr(
        "tradex.data_sources.em_client.em_get",
        lambda *_args, **_kwargs: _Response(
            ["2026-08-21 15:00,999,0,0,0,0,0"]
        ),
    )

    with pytest.raises(RuntimeError, match="returned no sector minute fund flow"):
        fetch_sector_intraday_fund_flow_eastmoney(
            provider_sector_code="BK0428",
            trade_date=TRADE_DATE,
        )


def test_sector_flow_gateway_rejects_off_session_provider_points():
    frame = _frame()
    frame.loc[0, "provider_as_of"] = "2026-08-24T12:00:00+08:00"

    class Router:
        def route_validated(self, _data_type, validator, **_kwargs):
            return validator(frame, "eastmoney"), "eastmoney"

    with pytest.raises(ValueError, match="outside A-share sessions"):
        fetch_sector_intraday_fund_flow(
            **TARGET,
            trading_date=TRADE_DATE,
            now=NOW,
            router=Router(),
        )


def test_backfill_cache_is_success_only_and_read_paths_do_not_start_network_calls():
    cache = SectorFundFlowBackfillCache()
    router = _Router(fail_first=True)

    assert fetch_sector_intraday_fund_flow_backfill(
        (TARGET,),
        trading_date=TRADE_DATE,
        now=NOW,
        router=router,
        cache=cache,
        load_missing=False,
    ) == {}
    assert router.calls == []

    assert fetch_sector_intraday_fund_flow_backfill(
        (TARGET,),
        trading_date=TRADE_DATE,
        now=NOW,
        router=router,
        cache=cache,
        load_missing=True,
    ) == {}
    loaded = fetch_sector_intraday_fund_flow_backfill(
        (TARGET,),
        trading_date=TRADE_DATE,
        now=NOW,
        router=router,
        cache=cache,
        load_missing=True,
    )
    cached = fetch_sector_intraday_fund_flow_backfill(
        (TARGET,),
        trading_date=TRADE_DATE,
        now=NOW,
        router=router,
        cache=cache,
        load_missing=False,
    )

    assert len(router.calls) == 2
    assert loaded == cached
    assert len(loaded["electric_power"]) == 6
    assert loaded["electric_power"][0]["source_family"] == "eastmoney"


def test_backfill_refresher_is_non_blocking_single_flight_and_rotates_one_target():
    started = Event()
    release = Event()
    calls = []

    def refresh(targets, **kwargs):
        calls.append((tuple(targets), kwargs))
        if len(calls) == 1:
            started.set()
            assert release.wait(2)
        return {}

    refresher = SectorFundFlowBackfillRefresher(refresh=refresh)
    second = {**TARGET, "sector_key": "coal", "provider_sector_code": "BK0437"}

    try:
        assert refresher.request((TARGET,), trading_date=TRADE_DATE) is True
        assert started.wait(1)
        assert refresher.request((second,), trading_date=TRADE_DATE) is False
    finally:
        release.set()

    assert refresher.wait_for_idle(2)
    assert [target["sector_key"] for target in calls[0][0]] == ["electric_power"]
    assert [target["sector_key"] for target in calls[1][0]] == ["coal"]
    assert all(call[1]["load_missing"] for call in calls)
    assert all(call[1]["refresh_existing"] for call in calls)


def test_backfill_scheduler_keeps_persisted_target_missing_from_current_snapshot(
    monkeypatch,
):
    current = {**TARGET, "sector_key": "coal", "provider_sector_code": "BK0437"}
    captured = {}

    class _KnownTargetCache:
        remembered = ()

        @classmethod
        def remember_targets(cls, trading_date, targets):
            assert trading_date == TRADE_DATE
            cls.remembered = tuple(targets)

        @staticmethod
        def get_known_targets(trading_date):
            assert trading_date == TRADE_DATE
            return ({**TARGET, "source_family": "eastmoney"},)

    class _Refresher:
        @staticmethod
        def request(targets, *, trading_date):
            captured["targets"] = tuple(targets)
            captured["trading_date"] = trading_date
            return True

    monkeypatch.setattr(
        sector_flow_module,
        "_SECTOR_FLOW_BACKFILL_CACHE",
        _KnownTargetCache(),
    )
    monkeypatch.setattr(
        sector_flow_module,
        "_SECTOR_FLOW_BACKFILL_REFRESHER",
        _Refresher(),
    )

    assert schedule_sector_intraday_fund_flow_backfill(
        (current,),
        trading_date=TRADE_DATE,
    )
    assert captured["trading_date"] == TRADE_DATE
    assert {target["sector_key"] for target in captured["targets"]} == {
        "coal",
        "electric_power",
    }
    assert _KnownTargetCache.remembered == (current,)


def test_backfill_scheduler_persists_every_resolved_target_before_bounded_sweep(
    tmp_path,
    monkeypatch,
):
    second = {
        **TARGET,
        "sector_key": "coal",
        "name": "煤炭",
        "provider_sector_code": "BK0437",
    }
    captured = {}

    class _Refresher:
        @staticmethod
        def request(targets, *, trading_date):
            captured["targets"] = tuple(targets)
            captured["trading_date"] = trading_date
            return True

    with SectorFundFlowStore(tmp_path / "target-registry.sqlite3") as store:
        cache = SectorFundFlowBackfillCache(store=store)
        monkeypatch.setattr(
            sector_flow_module,
            "_SECTOR_FLOW_BACKFILL_CACHE",
            cache,
        )
        monkeypatch.setattr(
            sector_flow_module,
            "_SECTOR_FLOW_BACKFILL_REFRESHER",
            _Refresher(),
        )

        assert schedule_sector_intraday_fund_flow_backfill(
            (TARGET, second),
            trading_date=TRADE_DATE,
        )
        remembered = store.get_targets(TRADE_DATE)

    assert {item["sector_key"] for item in remembered} == {
        "coal",
        "electric_power",
    }
    assert captured["trading_date"] == TRADE_DATE
    assert {item["sector_key"] for item in captured["targets"]} == {
        "coal",
        "electric_power",
    }


def test_backfill_success_survives_process_cache_restart(tmp_path):
    db_path = tmp_path / "sector-flow.sqlite3"
    first_router = _Router()
    with SectorFundFlowStore(db_path) as first_store:
        first_cache = SectorFundFlowBackfillCache(store=first_store)
        first = fetch_sector_intraday_fund_flow_backfill(
            (TARGET,),
            trading_date=TRADE_DATE,
            now=NOW,
            router=first_router,
            cache=first_cache,
            load_missing=True,
        )

    second_router = _Router()
    with SectorFundFlowStore(db_path) as second_store:
        second_cache = SectorFundFlowBackfillCache(store=second_store)
        restored = fetch_sector_intraday_fund_flow_backfill(
            (TARGET,),
            trading_date=TRADE_DATE,
            now=NOW,
            router=second_router,
            cache=second_cache,
            load_missing=False,
        )

    assert len(first_router.calls) == 1
    assert second_router.calls == []
    assert restored == first


def test_intraday_repair_request_is_persisted_coalesced_and_audited(tmp_path):
    db_path = tmp_path / "intraday-repair.sqlite3"
    cutoff = NOW.replace(minute=39)
    with SectorFundFlowStore(db_path) as store:
        queued = store.request_intraday_repair(
            TRADE_DATE,
            required_through=cutoff,
            requested_at=NOW,
        )
        coalesced = store.request_intraday_repair(
            TRADE_DATE,
            required_through=NOW,
            requested_at=NOW + timedelta(seconds=5),
        )
        running = store.update_intraday_repair(
            TRADE_DATE,
            status="running",
            observed_at=NOW + timedelta(seconds=10),
            target_count=2,
            attempted_delta=1,
            improved_delta=1,
            remaining_targets=1,
            last_sector_key="electric_power",
        )
        finished = store.update_intraday_repair(
            TRADE_DATE,
            status="partial",
            observed_at=NOW + timedelta(seconds=20),
            failed_delta=1,
            remaining_targets=1,
            last_error="upstream incomplete",
        )

    assert queued["action"] == "queued"
    assert coalesced["action"] == "coalesced"
    assert coalesced["repair"]["required_through"] == NOW.isoformat()
    assert running["attempted_targets"] == 1
    assert running["improved_targets"] == 1
    assert running["last_sector_key"] == "electric_power"
    assert finished["status"] == "partial"
    assert finished["failed_targets"] == 1
    assert finished["remaining_targets"] == 1


def test_read_all_backfill_restores_curves_without_current_target_resolution(tmp_path):
    db_path = tmp_path / "sector-flow-all.sqlite3"
    with SectorFundFlowStore(db_path) as store:
        cache = SectorFundFlowBackfillCache(store=store)
        loaded = fetch_sector_intraday_fund_flow_backfill(
            (TARGET,),
            trading_date=TRADE_DATE,
            now=NOW,
            router=_Router(),
            cache=cache,
            load_missing=True,
        )

    with SectorFundFlowStore(db_path) as reopened:
        restored_targets = reopened.get_targets(TRADE_DATE)
        restored = read_sector_intraday_fund_flow_backfill(
            trading_date=TRADE_DATE,
            cache=SectorFundFlowBackfillCache(store=reopened),
        )

    assert restored == loaded
    assert restored_targets == (
        {
            **TARGET,
            "source_family": "eastmoney",
        },
    )
    assert restored["electric_power"][0]["name"] == "电力"
    assert restored["electric_power"][0]["taxonomy"] == "industry"


def test_persistent_backfill_store_never_replaces_a_longer_curve_with_shorter_data(
    tmp_path,
):
    full = fetch_sector_intraday_fund_flow(
        **TARGET,
        trading_date=TRADE_DATE,
        now=NOW,
        router=_Router(),
    )
    short_points = full.points[:2]
    shorter = full.model_copy(
        update={
            "metadata": full.metadata.model_copy(
                update={
                    "provider_as_of": short_points[-1].provider_as_of,
                    "fetched_at": NOW + timedelta(minutes=1),
                }
            ),
            "points": short_points,
        }
    )

    with SectorFundFlowStore(tmp_path / "preserve.sqlite3") as store:
        assert store.record(full)["action"] == "inserted"
        cache = SectorFundFlowBackfillCache(store=store)
        retained = cache.get_or_load(
            (TRADE_DATE, TARGET["sector_key"], TARGET["provider_sector_code"]),
            lambda: shorter,
            refresh_existing=True,
            refreshed_at=NOW + timedelta(minutes=1),
        )
        restored = store.get_best(TRADE_DATE, TARGET["sector_key"])

    assert retained.points == full.points
    assert restored is not None
    assert restored.points == full.points


def test_explicit_backfill_refresh_is_bounded_and_keeps_last_success_on_failure():
    cache = SectorFundFlowBackfillCache(refresh_min_interval_seconds=300)
    router = _Router()

    first = fetch_sector_intraday_fund_flow_backfill(
        (TARGET,),
        trading_date=TRADE_DATE,
        now=NOW,
        router=router,
        cache=cache,
        load_missing=True,
    )
    bounded = fetch_sector_intraday_fund_flow_backfill(
        (TARGET,),
        trading_date=TRADE_DATE,
        now=NOW + timedelta(minutes=1),
        router=router,
        cache=cache,
        refresh_existing=True,
    )
    router.fail_first = False
    original_route = router.route_validated

    def fail_refresh(*args, **kwargs):
        raise RuntimeError("refresh unavailable")

    router.route_validated = fail_refresh
    stale_success = fetch_sector_intraday_fund_flow_backfill(
        (TARGET,),
        trading_date=TRADE_DATE,
        now=NOW + timedelta(minutes=6),
        router=router,
        cache=cache,
        refresh_existing=True,
    )
    router.route_validated = original_route

    assert len(router.calls) == 1
    assert bounded == first
    assert stale_success == first


def test_collector_refresh_repairs_missing_curve_minutes_and_persists_them(
    tmp_path,
):
    class GrowingRouter:
        def __init__(self) -> None:
            self.calls = 0

        def route_validated(self, _data_type, validator, **kwargs):
            self.calls += 1
            frame = _frame(kwargs["trade_date"])
            if self.calls == 1:
                frame = frame.iloc[[0, 2]].reset_index(drop=True)
                frame.attrs["provider_request_id"] = "partial-curve"
            else:
                frame.attrs["provider_request_id"] = "repaired-curve"
            return validator(frame, "eastmoney"), "eastmoney"

    db_path = tmp_path / "gap-repair.sqlite3"
    router = GrowingRouter()
    with SectorFundFlowStore(db_path) as store:
        cache = SectorFundFlowBackfillCache(
            store=store,
            refresh_min_interval_seconds=0,
        )
        partial = fetch_sector_intraday_fund_flow_backfill(
            (TARGET,),
            trading_date=TRADE_DATE,
            now=NOW,
            router=router,
            cache=cache,
            load_missing=True,
        )
        repaired = fetch_sector_intraday_fund_flow_backfill(
            (TARGET,),
            trading_date=TRADE_DATE,
            now=NOW + timedelta(minutes=5),
            router=router,
            cache=cache,
            refresh_existing=True,
        )

    with SectorFundFlowStore(db_path) as reopened:
        restored = reopened.get_best(TRADE_DATE, TARGET["sector_key"])

    assert router.calls == 2
    assert len(partial[TARGET["sector_key"]]) == 2
    assert len(repaired[TARGET["sector_key"]]) == 6
    assert repaired[TARGET["sector_key"]][1]["provider_as_of"].minute == 32
    assert restored is not None
    assert len(restored.points) == 6


def test_post_close_finalization_refreshes_every_target_through_common_cutoff(
    tmp_path,
):
    class _ExtendedRouter(_Router):
        def route_validated(self, data_type: str, validator, **kwargs: object):
            self.calls.append((data_type, kwargs))
            frame = _frame(kwargs["trade_date"])
            final = frame.iloc[-1].copy()
            final["provider_as_of"] = (
                datetime.fromisoformat(str(final["provider_as_of"]))
                + timedelta(minutes=1)
            ).isoformat()
            final["main_net_inflow_cny"] = (
                float(final["main_net_inflow_cny"]) + 10_000_000.0
            )
            frame = pd.concat([frame, pd.DataFrame([final])], ignore_index=True)
            return validator(frame, "eastmoney"), "eastmoney"

    second_target = {
        **TARGET,
        "sector_key": "coal",
        "name": "煤炭",
        "provider_sector_code": "BK0437",
    }
    db_path = tmp_path / "finalize.sqlite3"
    progress = []
    with SectorFundFlowStore(db_path) as store:
        cache = SectorFundFlowBackfillCache(
            store=store,
            refresh_min_interval_seconds=300,
        )
        fetch_sector_intraday_fund_flow_backfill(
            (TARGET, second_target),
            trading_date=TRADE_DATE,
            now=NOW,
            router=_Router(),
            cache=cache,
            load_missing=True,
        )
        report = finalize_sector_intraday_fund_flow_backfill(
            trading_date=TRADE_DATE,
            required_through=NOW.replace(hour=9, minute=37),
            now=NOW + timedelta(minutes=1),
            router=_ExtendedRouter(),
            cache=cache,
            progress=lambda completed, total, sector_key: progress.append(
                (completed, total, sector_key)
            ),
        )

    assert report["contract"] == "sector_intraday_fund_flow_finalization.v1"
    assert report["target_count"] == 2
    assert report["refreshed_target_count"] == 2
    assert report["complete_count"] == 2
    assert report["min_point_count"] == 7
    assert report["max_point_count"] == 7
    assert progress == [(1, 2, "coal"), (2, 2, "electric_power")]


def test_post_close_finalization_does_not_refetch_a_curve_past_the_cutoff(
    tmp_path,
):
    router = _Router()
    with SectorFundFlowStore(tmp_path / "finalize-reuse.sqlite3") as store:
        cache = SectorFundFlowBackfillCache(
            store=store,
            refresh_min_interval_seconds=300,
        )
        fetch_sector_intraday_fund_flow_backfill(
            (TARGET,),
            trading_date=TRADE_DATE,
            now=NOW,
            router=router,
            cache=cache,
            load_missing=True,
        )
        report = finalize_sector_intraday_fund_flow_backfill(
            trading_date=TRADE_DATE,
            required_through=NOW.replace(hour=9, minute=35),
            now=NOW + timedelta(minutes=1),
            router=router,
            cache=cache,
        )

    assert len(router.calls) == 1
    assert report["complete"] is True
    assert report["refreshed_target_count"] == 0


def test_post_close_finalization_rejects_a_curve_with_rendered_intraday_gaps(
    tmp_path,
):
    class _GappedRouter(_Router):
        def route_validated(self, data_type: str, validator, **kwargs: object):
            self.calls.append((data_type, kwargs))
            frame = _frame(kwargs["trade_date"]).iloc[[0, 5]].copy()
            frame.iloc[1, frame.columns.get_loc("provider_as_of")] = (
                datetime.combine(kwargs["trade_date"], datetime.min.time(), SHANGHAI)
                .replace(hour=9, minute=37)
                .isoformat()
            )
            return validator(frame, "eastmoney"), "eastmoney"

    db_path = tmp_path / "finalize-gapped.sqlite3"
    with SectorFundFlowStore(db_path) as store:
        cache = SectorFundFlowBackfillCache(store=store)
        fetch_sector_intraday_fund_flow_backfill(
            (TARGET,),
            trading_date=TRADE_DATE,
            now=NOW,
            router=_GappedRouter(),
            cache=cache,
            load_missing=True,
        )
        report = finalize_sector_intraday_fund_flow_backfill(
            trading_date=TRADE_DATE,
            required_through=NOW.replace(hour=9, minute=37),
            now=NOW,
            router=_GappedRouter(),
            cache=cache,
        )

    assert report["complete"] is False
    assert report["complete_count"] == 0
    assert report["missing_target_keys"] == ("electric_power",)
