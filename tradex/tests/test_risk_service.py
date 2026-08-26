"""Risk-appetite dashboard aggregation tests."""

from __future__ import annotations

import copy
import threading
import time
from concurrent.futures import Future
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pandas as pd

from astock_signals.smart_router import SmartRouter
from tradex.data_gateway.contracts import (
    ContractMetadata,
    QualityStatus,
    StockSectorProfileSeriesV1,
    StockSectorProfileV1,
)
from tradex.data_gateway.limit_events import fetch_limit_up_events
from tradex.dashboard import risk_service
from tradex.market_watch.contracts import SectorFlowLeaderSnapshotV1


@pytest.fixture(autouse=True)
def _reset_service_cache(tmp_path):
    risk_service._trajectory_store = risk_service.RiskTrajectoryStore(
        tmp_path / "risk-trajectory.sqlite3"
    )
    risk_service._rotation_store = risk_service.RotationRadarStore(
        tmp_path / "rotation-radar.sqlite3"
    )
    risk_service.reset_risk_appetite_cache()
    yield
    risk_service.reset_risk_appetite_cache()
    risk_service.close_risk_trajectory_store()


def _background(prefix: str) -> list[dict]:
    return [
        {
            "板块名称": f"{prefix}{index}",
            "涨跌幅": change,
            "上涨家数": 50,
            "下跌家数": 50,
        }
        for index, change in enumerate((-4.0, -2.0, 0.0, 2.0, 4.0), start=1)
    ]


def _install_component_fakes(monkeypatch):
    industry = [
        *(
            {**row, "主力净流入-占比": flow_ratio}
            for row, flow_ratio in zip(
                _background("行业"),
                (-4.0, -2.0, 0.0, 2.0, 4.0),
            )
        ),
        {
            "板块名称": "证券",
            "最新点位": 1234.5,
            "涨跌幅": 8.0,
            "主力净流入-占比": 8.0,
            "上涨家数": 70,
            "下跌家数": 30,
            "领涨股票": "中信证券",
            "领涨股涨幅": 5.2,
        },
        {
            "板块名称": "银行",
            "涨跌幅": 1.0,
            "主力净流入-占比": 1.0,
            "上涨家数": 52,
            "下跌家数": 48,
        },
    ]
    concepts = [
        *(
            {**row, "主力净流入-占比": flow_ratio}
            for row, flow_ratio in zip(
                _background("概念"),
                (-4.0, -2.0, 0.0, 2.0, 4.0),
            )
        ),
        {
            "板块名称": "互联网金融",
            "涨跌幅": 9.0,
            "主力净流入-占比": 9.0,
            "上涨家数": 72,
            "下跌家数": 28,
        },
    ]

    monkeypatch.setattr(
        risk_service,
        "_fetch_market_breadth",
        lambda: ([{"上涨家数": 3300, "下跌家数": 1700}], "breadth"),
    )
    monkeypatch.setattr(
        risk_service,
        "_fetch_board_quotes",
        lambda board_type: (industry if board_type == "industry" else concepts, board_type),
    )
    monkeypatch.setattr(
        risk_service,
        "_fetch_etfs",
        lambda: ([{
            "etf_code": "512880",
            "name": "证券ETF",
            "change_pct": 2.5,
            "amount": 500,
        }], "etf"),
    )
    monkeypatch.setattr(
        risk_service,
        "_fetch_leaders",
        lambda: ([{
            "代码": "600030",
            "名称": "中信证券",
            "最新价": 30.0,
            "涨跌幅": 3.2,
        }], "tencent"),
    )
    monkeypatch.setattr(
        risk_service,
        "_fetch_leadership_pool",
        lambda trade_date: ([], "ths", {
            "contract": "limit_event.v1",
            "schema_version": 1,
            "quality": "degraded",
            "quality_flags": ["provider_timestamp_missing"],
            "source_valid": True,
            "data_date": trade_date.replace("-", ""),
            "trade_status": {"id": 3, "name": "交易中"},
            "pool_total": 0,
            "unique_total": 0,
            "reason_coverage": 1.0,
            "board_count_coverage": 1.0,
            "unknown_board_count": 0,
            "valid_empty": True,
            "page_count": 0,
        }),
    )


def _market_data() -> dict:
    return {
        "provider_as_of": "2026-08-19T10:30:00+08:00",
        "participation_indices": [
            {"名称": "上证指数", "代码": "sh000001", "涨跌幅": 0.5},
            {"名称": "深证成指", "代码": "sz399001", "涨跌幅": 0.7},
            {"名称": "创业板指", "代码": "sz399006", "涨跌幅": 1.5},
        ],
        "market_turnover": {
            "available": True,
            "direction": "expand",
            "difference": 100,
        },
    }


def test_sector_flow_components_are_derived_from_quote_components():
    values = {
        "industry_quotes": [{"板块名称": "证券", "主力净流入-占比": 8.0}],
        "concept_quotes": [{"板块名称": "互联网金融", "主力净流入-占比": 9.0}],
    }
    statuses = {
        "industry_quotes": {
            "source": "paid-provider",
            "contract": "sector_quote.v1",
            "provider_as_of": "2026-08-19T10:30:00+08:00",
        },
        "concept_quotes": {
            "source": "paid-provider",
            "contract": "sector_quote.v1",
            "provider_as_of": "2026-08-19T10:30:00+08:00",
        },
    }

    risk_service._derive_sector_flow_components(values, statuses)

    assert values["industry_flow"] == values["industry_quotes"]
    assert values["industry_flow"] is not values["industry_quotes"]
    assert values["industry_flow"][0] is not values["industry_quotes"][0]
    assert statuses["concept_flow"] == {
        **statuses["concept_quotes"],
        "derived_from": "concept_quotes",
    }
    assert statuses["concept_flow"] is not statuses["concept_quotes"]
    assert risk_service._CONTEXT_COMPONENTS == ("etfs", "leaders")
    assert set(risk_service._context_jobs()) == {"etfs", "leaders"}


def test_provider_sensitive_fast_components_are_refreshed_in_order(monkeypatch):
    _install_component_fakes(monkeypatch)
    active = 0
    overlapped = False
    calls: list[str] = []
    lock = threading.Lock()

    def fetched(name: str, records: list[dict], source: str):
        nonlocal active, overlapped
        with lock:
            overlapped = overlapped or active > 0
            active += 1
            calls.append(name)
        time.sleep(0.01)
        with lock:
            active -= 1
        return records, source

    monkeypatch.setattr(
        risk_service,
        "_fetch_market_breadth",
        lambda: fetched(
            "market_breadth",
            [{"上涨家数": 3300, "下跌家数": 1700}],
            "breadth",
        ),
    )
    monkeypatch.setattr(
        risk_service,
        "_fetch_board_quotes",
        lambda board_type: fetched(
            f"{board_type}_quotes",
            [{"板块名称": board_type, "涨跌幅": 1.0}],
            board_type,
        ),
    )

    risk_service.get_risk_appetite_data(_market_data(), force=True)

    assert calls == ["market_breadth", "industry_quotes", "concept_quotes"]
    assert overlapped is False


class _DeferredFuture(Future):
    """A deterministic future whose submitted work starts when it is awaited."""

    def __init__(self, task):
        super().__init__()
        self._task = task

    def result(self, timeout=None):
        if not self.done():
            try:
                value = self._task()
            except Exception as exc:  # pragma: no cover - Future parity for failures
                self.set_exception(exc)
            else:
                self.set_result(value)
        return super().result(timeout=timeout)


class _DeferredContextExecutor:
    def __init__(self):
        self.submissions: list[str] = []
        self.futures: dict[str, Future] = {}

    def submit(self, function, *args, **kwargs):
        name = str(args[0])
        self.submissions.append(name)
        future = _DeferredFuture(lambda: function(*args, **kwargs))
        self.futures[name] = future
        return future


def _offense_leader_result(core_count: int = 4, radar_count: int = 4) -> dict:
    core = [{
        "key": f"core-{index}",
        "label": f"常规方向{index}",
        "representative_board": {
            "taxonomy": "industry",
            "board_code": f"BK1{index:03d}",
            "name": f"常规板块{index}",
        },
    } for index in range(core_count)]
    radar = [{
        "key": f"concept:BK2{index:03d}",
        "label": f"轮动板块{index}",
        "taxonomy": "concept",
        "board_code": f"BK2{index:03d}",
    } for index in range(radar_count)]
    summary = [
        {
            "key": item["key"],
            "label": item["label"],
            "taxonomy": item["taxonomy"],
            "board_code": item["board_code"],
        }
        for item in radar[:2]
    ]
    return {
        "offense": {
            "core": {"items": core},
            "lists": {
                "attacking": radar,
                "rotating": [],
                "cooling": [],
                "unclassified": [],
            },
            "summary": {"current_leaders": summary},
        },
    }


def _ready_leader_snapshot(code: str, *, name: str = "成分领涨") -> dict:
    exchange = "SH" if code.startswith("6") else "SZ"
    return {
        "status": "ready",
        "source": "push2",
        "provider_as_of": "2026-08-19T10:30:00+08:00",
        "fetched_at": "2026-08-19T10:30:01+08:00",
        "stale": False,
        "method": "board_constituents",
        "items": [{
            "instrument_id": f"{code}.{exchange}",
            "code": code,
            "name": name,
            "price": 20.0,
            "change_pct": 8.0,
            "speed_pct": 1.2,
            "amount": 800_000_000,
            "turnover": 6.0,
            "flow_amount": 50_000_000,
            "flow_ratio": 5.0,
            "provider_as_of": "2026-08-19T10:30:00+08:00",
            "source": "push2",
        }],
    }


def _defense_flow_leader_result() -> dict:
    result = _offense_leader_result(core_count=5, radar_count=5)
    result["sector_flow_trajectory"] = {
        "sectors": [
            {
                "sector_key": f"defense-{index}",
                "name": f"防守板块{index}",
                "taxonomy": "industry",
                "leader_board_code": f"BK3{index:03d}",
                "latest": {
                    "provider_as_of": "2026-08-19T10:30:00+08:00",
                    "change_pct": 3.0 - index * 0.2,
                    "delta_5m_cny": 20_000_000,
                },
                "leader_snapshot": None,
            }
            for index in range(3)
        ],
    }
    return result


def _two_direction_flow_leader_result() -> dict:
    result = _defense_flow_leader_result()
    result["offense_sector_flow_trajectory"] = {
        "sectors": [{
            "sector_key": "advanced_packaging",
            "name": "先进封装",
            "taxonomy": "concept",
            "leader_board_code": "BK1101",
            "latest": {
                "provider_as_of": "2026-08-19T10:30:00+08:00",
                "change_pct": 4.2,
                "delta_5m_cny": 30_000_000,
            },
            "leader_snapshot": None,
        }],
    }
    return result


def test_service_builds_frontend_view_without_recounting_confirmations(monkeypatch):
    _install_component_fakes(monkeypatch)
    for name, fetcher in risk_service._context_jobs().items():
        risk_service._component(name, fetcher, force=False)

    result = risk_service.get_risk_appetite_data(_market_data())

    assert result["emotion"]["label"] == "积极"
    assert result["structure"]["label"] == "金融进攻"
    assert result["summary"]["financial_core"]["key"] == "strong"
    assert result["data_quality"]["available"] >= 2

    financial = next(group for group in result["groups"] if group["key"] == "financial")
    securities = next(item for item in financial["items"] if item["key"] == "securities")
    assert securities["state"]["key"] == "strong"
    assert securities["index_level"] == 1234.5
    assert securities["etf"] == {
        "code": "512880",
        "name": "证券ETF",
        "change_pct": 2.5,
        "amount": 500,
    }
    assert securities["leaders"][0]["change_pct"] == 3.2
    assert securities["current_leader"] == {
        "code": None,
        "name": "中信证券",
        "market": None,
        "change_pct": 5.2,
        "provider_as_of": None,
        "source": None,
    }
    assert securities["available_evidence"] == 4
    assert securities["core_state"]["key"] == "strong"
    assert securities["evidence"]["leadership"] == "medium"
    assert securities["leadership"]["vote_enabled"] is True
    assert result["attribution_version"] == "sector-attribution-v2"
    assert result["leadership_pool"]["status"]["eligible_for_vote"] is True
    assert result["components"]["leadership_pool"]["data_date"] == "20260819"


def test_capture_observer_receives_exact_component_inputs_only_when_opted_in(monkeypatch):
    _install_component_fakes(monkeypatch)
    for name, fetcher in risk_service._context_jobs().items():
        risk_service._component(name, fetcher, force=False)
    observed = []

    risk_service.get_risk_appetite_data(
        _market_data(),
        force=True,
        record_trajectory=True,
        capture_observer=observed.append,
    )

    assert len(observed) == 1
    capture = observed[0]
    assert capture["minute_bucket"].second == 0
    assert capture["values"]["market_breadth"][0]["上涨家数"] == 3300
    assert capture["statuses"]["leadership_pool"]["eligible_for_vote"] is True
    assert capture["market_data"]["market_turnover"]["direction"] == "expand"


def test_capture_observer_waits_for_cold_context_futures_without_refetch(monkeypatch):
    _install_component_fakes(monkeypatch)
    executor = _DeferredContextExecutor()
    monkeypatch.setattr(risk_service, "_context_executor", executor)
    observed = []

    risk_service.get_risk_appetite_data(
        _market_data(),
        force=True,
        record_trajectory=True,
        capture_observer=observed.append,
    )

    assert executor.submissions == list(risk_service._CONTEXT_COMPONENTS)
    assert set(executor.futures) == set(risk_service._CONTEXT_COMPONENTS)
    assert all(future.done() for future in executor.futures.values())
    assert risk_service._context_futures == {}

    capture = observed[0]
    for name in risk_service._CONTEXT_COMPONENTS:
        assert capture["values"][name]
        assert capture["statuses"][name]["refreshing"] is False
    assert capture["statuses"]["industry_flow"]["source"] == "industry"
    assert capture["statuses"]["industry_flow"]["derived_from"] == "industry_quotes"
    assert capture["statuses"]["etfs"]["source"] == "etf"
    assert capture["statuses"]["leaders"]["source"] == "tencent"


def test_regular_dashboard_keeps_cold_context_refresh_non_blocking(monkeypatch):
    _install_component_fakes(monkeypatch)
    executor = _DeferredContextExecutor()
    monkeypatch.setattr(risk_service, "_context_executor", executor)

    result = risk_service.get_risk_appetite_data(_market_data(), force=True)

    assert executor.submissions == list(risk_service._CONTEXT_COMPONENTS)
    assert all(not future.done() for future in executor.futures.values())
    assert all(name not in risk_service._component_cache for name in executor.futures)
    assert all(
        result["components"][name]["refreshing"] is True
        for name in risk_service._CONTEXT_COMPONENTS
    )

    # Finish the deterministic test futures so the global cache fixture has no
    # pending callbacks to carry into teardown. The assertions above prove the
    # ordinary request returned before any of this work ran.
    for future in executor.futures.values():
        future.result()


def test_industry_profile_degradation_marks_top_level_data_quality_partial(monkeypatch):
    _install_component_fakes(monkeypatch)
    original_build = risk_service.build_risk_appetite_snapshot

    def build_high_quality_snapshot(*args, **kwargs):
        snapshot = original_build(*args, **kwargs)
        snapshot["data_quality"]["level"] = "high"
        return snapshot

    monkeypatch.setattr(
        risk_service,
        "build_risk_appetite_snapshot",
        build_high_quality_snapshot,
    )
    monkeypatch.setattr(
        risk_service,
        "_fetch_leadership_pool",
        lambda trade_date: ([{
            "代码": "603395",
            "名称": "红四方",
            "涨停原因": "复合肥+央企",
            "连板": "首板",
        }], "ths", {
            "contract": "limit_event.v1",
            "schema_version": 1,
            "quality": "degraded",
            "quality_flags": ["provider_timestamp_missing"],
            "source_valid": True,
            "data_date": trade_date.replace("-", ""),
            "trade_status": {"id": 3, "name": "交易中"},
            "pool_total": 1,
            "unique_total": 1,
            "reason_coverage": 1.0,
            "board_count_coverage": 1.0,
            "unknown_board_count": 0,
            "valid_empty": False,
            "page_count": 1,
            "industry_profile_status": {
                "status": "degraded",
                "source_valid": False,
                "eligible_for_attribution": False,
                "requested_total": 1,
                "returned_total": 0,
                "coverage": 0.0,
                "provider_as_of": None,
                "error": "profile unavailable",
            },
        }),
    )
    for name, fetcher in risk_service._context_jobs().items():
        risk_service._component(name, fetcher, force=False)

    result = risk_service.get_risk_appetite_data(_market_data())

    assert result["data_quality"]["partial"] is True
    assert result["data_quality"]["key"] == "medium"
    assert result["data_quality"]["label"] == "部分数据"
    assert "industry_profile" in result["data_quality"]["missing_components"]


def test_rotation_failure_does_not_hide_existing_sentiment(monkeypatch):
    _install_component_fakes(monkeypatch)
    for name, fetcher in risk_service._context_jobs().items():
        risk_service._component(name, fetcher, force=False)

    def fail_rotation_store():
        raise RuntimeError("rotation unavailable")

    monkeypatch.setattr(risk_service, "_get_rotation_store", fail_rotation_store)
    result = risk_service.get_risk_appetite_data(_market_data())

    assert result["emotion"]["label"] == "积极"
    assert result["summary"]["financial_core"]["key"] == "strong"
    assert result["offense"]["status"] == "error"
    assert result["offense"]["status_label"] == "进攻雷达暂不可用"


def test_component_uses_stale_success_after_refresh_error(monkeypatch):
    risk_service._component_cache["industry_quotes"] = {
        "records": [{"板块名称": "银行"}],
        "source": "old-source",
        "fetched_at": "2026-08-19T10:00:00+08:00",
        "provider_as_of": "2026-08-19T09:59:59+08:00",
        "cached_at": 930.0,
        "last_error": None,
    }
    monkeypatch.setattr(risk_service.time, "monotonic", lambda: 1000.0)

    def fail():
        raise RuntimeError("upstream unavailable")

    records, status = risk_service._component("industry_quotes", fail, force=False)

    assert records == [{"板块名称": "银行"}]
    assert status["stale"] is True
    assert status["expired"] is False
    assert status["error"] == "upstream unavailable"


def test_component_discards_cache_after_hard_stale_limit(monkeypatch):
    risk_service._component_cache["industry_quotes"] = {
        "records": [{"板块名称": "银行"}],
        "source": "old-source",
        "fetched_at": "2026-08-19T10:00:00+08:00",
        "provider_as_of": "2026-08-19T09:59:59+08:00",
        "cached_at": 900.0,
        "last_error": None,
    }
    monkeypatch.setattr(risk_service.time, "monotonic", lambda: 1000.0)

    def fail():
        raise RuntimeError("upstream unavailable")

    records, status = risk_service._component("industry_quotes", fail, force=False)

    assert records == []
    assert status["stale"] is True
    assert status["expired"] is True


def test_closed_session_preserves_same_trade_date_quotes_after_hard_stale_limit(
    monkeypatch,
):
    risk_service._component_cache["industry_quotes"] = {
        "records": [{
            "板块名称": "种植业",
            "provider_as_of": "2026-08-19T15:39:32+08:00",
        }],
        "source": "old-source",
        "fetched_at": "2026-08-19T15:40:00+08:00",
        "provider_as_of": "2026-08-19T15:39:32+08:00",
        "cached_at": 800.0,
        "last_error": None,
    }
    monkeypatch.setattr(risk_service.time, "monotonic", lambda: 1000.0)

    records, status = risk_service._component(
        "industry_quotes",
        lambda: (_ for _ in ()).throw(RuntimeError("upstream unavailable")),
        force=False,
        closed_trade_date="2026-08-19",
    )

    assert records == [{
        "板块名称": "种植业",
        "provider_as_of": "2026-08-19T15:39:32+08:00",
    }]
    assert status["stale"] is True
    assert status["expired"] is False
    assert status["closed_session_fallback"] is True


def test_closed_session_never_preserves_quotes_from_a_previous_trade_date(monkeypatch):
    risk_service._component_cache["industry_quotes"] = {
        "records": [{"板块名称": "种植业"}],
        "source": "old-source",
        "fetched_at": "2026-08-19T15:40:00+08:00",
        "provider_as_of": "2026-08-19T15:39:32+08:00",
        "cached_at": 800.0,
        "last_error": None,
    }
    monkeypatch.setattr(risk_service.time, "monotonic", lambda: 1000.0)

    records, status = risk_service._component(
        "industry_quotes",
        lambda: (_ for _ in ()).throw(RuntimeError("upstream unavailable")),
        force=False,
        closed_trade_date="2026-08-20",
    )

    assert records == []
    assert status["expired"] is True
    assert status["closed_session_fallback"] is False


def test_closed_session_rejects_a_mixed_date_quote_universe(monkeypatch):
    risk_service._component_cache["industry_quotes"] = {
        "records": [
            {
                "板块名称": "种植业",
                "provider_as_of": "2026-08-19T15:39:32+08:00",
            },
            {
                "板块名称": "银行",
                "provider_as_of": "2026-08-18T15:39:32+08:00",
            },
        ],
        "source": "old-source",
        "fetched_at": "2026-08-19T15:40:00+08:00",
        "provider_as_of": "2026-08-19T15:39:32+08:00",
        "cached_at": 800.0,
        "last_error": None,
    }
    monkeypatch.setattr(risk_service.time, "monotonic", lambda: 1000.0)

    records, status = risk_service._component(
        "industry_quotes",
        lambda: (_ for _ in ()).throw(RuntimeError("upstream unavailable")),
        force=False,
        closed_trade_date="2026-08-19",
    )

    assert records == []
    assert status["expired"] is True
    assert status["closed_session_fallback"] is False


def test_component_keeps_failed_forced_refresh_marked_stale_inside_ttl(monkeypatch):
    risk_service._component_cache["leadership_pool"] = {
        "records": [{"代码": "603395"}],
        "source": "ths",
        "fetched_at": "2026-08-19T10:00:00+08:00",
        "provider_as_of": None,
        "cached_at": 965.0,
        "last_error": None,
        "cache_identity": "2026-08-19",
        "source_valid": True,
        "data_date": "20260819",
    }
    current = [1000.0]
    monkeypatch.setattr(risk_service.time, "monotonic", lambda: current[0])

    def fail():
        raise RuntimeError("temporary failure")

    _, failed = risk_service._component(
        "leadership_pool", fail, force=True, cache_identity="2026-08-19"
    )
    current[0] = 1001.0
    _, cached = risk_service._component(
        "leadership_pool", fail, force=False, cache_identity="2026-08-19"
    )

    assert failed["stale"] is True
    assert cached["stale"] is True
    assert cached["error"] == "temporary failure"


def test_leadership_component_never_reuses_a_previous_trade_date(monkeypatch):
    risk_service._component_cache["leadership_pool"] = {
        "records": [{"代码": "603395"}],
        "source": "ths",
        "fetched_at": "2026-08-18T15:00:00+08:00",
        "provider_as_of": None,
        "cached_at": 999.0,
        "last_error": None,
        "cache_identity": "2026-08-18",
        "source_valid": True,
        "data_date": "20260818",
    }
    monkeypatch.setattr(risk_service.time, "monotonic", lambda: 1000.0)

    def fail():
        raise RuntimeError("upstream unavailable")

    records, status = risk_service._component(
        "leadership_pool",
        fail,
        force=False,
        cache_identity="2026-08-19",
    )

    assert records == []
    assert status["fetched_at"] is None
    assert status["cache_identity"] == "2026-08-19"
    assert status["source_valid"] is False


def test_leadership_component_preserves_valid_empty_metadata():
    records, status = risk_service._component(
        "leadership_pool",
        lambda: ([], "ths", {
            "source_valid": True,
            "data_date": "20260819",
            "trade_status": {"id": 3, "name": "交易中"},
            "pool_total": 0,
            "unique_total": 0,
            "reason_coverage": 1.0,
            "board_count_coverage": 1.0,
            "unknown_board_count": 0,
            "valid_empty": True,
            "page_count": 0,
        }),
        force=False,
        cache_identity="2026-08-19",
    )

    assert records == []
    assert status["source_valid"] is True
    assert status["data_date"] == "20260819"
    assert status["valid_empty"] is True
    assert status["pool_total"] == 0
    assert status["unique_total"] == 0
    assert status["reason_coverage"] == 1.0
    assert status["board_count_coverage"] == 1.0
    assert status["unknown_board_count"] == 0


def _limit_up_frame_for_profile_test():
    frame = pd.DataFrame([{
        "代码": "600928",
        "名称": "西安银行",
        "涨停原因": "半年报增长+西安国资+养老金融+数字人民币",
        "连板": "首板",
    }])
    frame.attrs.update({
        "source_valid": True,
        "data_date": "20260819",
        "trade_status": {"id": "closed", "name": "已收盘"},
        "pool_total": 1,
        "unique_total": 1,
        "reason_coverage": 1.0,
        "board_count_coverage": 1.0,
        "unknown_board_count": 0,
        "valid_empty": False,
        "page_count": 1,
    })
    return frame


def _limit_event_series_for_profile_test(frame, trade_date="2026-08-19"):
    router = SmartRouter()
    router.register("limit_events", "ths", lambda date: frame, priority=1)
    return fetch_limit_up_events(
        trade_date,
        router=router,
        now=datetime(2026, 8, 19, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )


def _stock_sector_profile_series(rows):
    profiles = tuple(
        StockSectorProfileV1(
            instrument_id=(
                f"{row['代码']}.SH" if row["代码"].startswith("6")
                else f"{row['代码']}.SZ"
            ),
            name=row["名称"],
            industry=row["行业"],
            region=row["地域"],
            concept_tags=tuple(row["概念标签"]),
            provider_as_of=datetime.fromisoformat(row["provider_as_of"]),
            provider_variant=row["source"],
        )
        for row in rows
    )
    provider_as_of = max(item.provider_as_of for item in profiles)
    return StockSectorProfileSeriesV1(
        metadata=ContractMetadata(
            contract="stock_sector_profile.v1",
            provider="push2delay",
            provider_as_of=provider_as_of,
            fetched_at=datetime(2026, 8, 19, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            quality=QualityStatus.ACCEPTED,
        ),
        trade_date=date(2026, 8, 19),
        requested_instrument_ids=tuple(item.instrument_id for item in profiles),
        profiles=profiles,
    )


def test_leadership_fetch_adds_one_complete_batch_industry_profile(monkeypatch):
    frame = _limit_up_frame_for_profile_test()
    monkeypatch.setattr(
        risk_service,
        "fetch_limit_up_events",
        lambda trade_date: _limit_event_series_for_profile_test(frame, trade_date),
    )
    profiles = [{
        "代码": "600928",
        "名称": "西安银行",
        "行业": "银行Ⅱ",
        "地域": "陕西板块",
        "概念标签": ["互联网金融", "移动支付"],
        "provider_as_of": "2026-08-19T15:00:00+08:00",
        "source": "push2delay",
    }]
    monkeypatch.setattr(
        risk_service,
        "fetch_stock_sector_profiles",
        lambda codes, *, trade_date: _stock_sector_profile_series(profiles),
    )

    records, source, metadata = risk_service._fetch_leadership_pool("2026-08-19")

    assert source == "ths"
    assert records[0]["sector_profile"] == {
        "industry": "银行Ⅱ",
        "region": "陕西板块",
        "concept_tags": ["互联网金融", "移动支付"],
        "source": "push2delay",
        "provider_as_of": "2026-08-19T15:00:00+08:00",
        "fetched_at": "2026-08-19T15:00:00+08:00",
    }
    assert metadata["industry_profile_status"]["status"] == "ready"
    assert metadata["industry_profile_status"]["coverage"] == 1.0


def test_leadership_fetch_degrades_to_reason_only_when_profile_batch_fails(monkeypatch):
    frame = _limit_up_frame_for_profile_test()
    monkeypatch.setattr(
        risk_service,
        "fetch_limit_up_events",
        lambda trade_date: _limit_event_series_for_profile_test(frame, trade_date),
    )
    monkeypatch.setattr(
        risk_service,
        "fetch_stock_sector_profiles",
        lambda codes, *, trade_date: (_ for _ in ()).throw(
            RuntimeError("profile unavailable")
        ),
    )

    records, _, metadata = risk_service._fetch_leadership_pool("2026-08-19")

    assert "sector_profile" not in records[0]
    assert metadata["source_valid"] is True
    assert metadata["industry_profile_status"]["status"] == "degraded"
    assert metadata["industry_profile_status"]["eligible_for_attribution"] is False
    assert metadata["industry_profile_status"]["error"] == "profile unavailable"


def test_leadership_fetch_degrades_when_any_profile_row_has_wrong_trade_date(monkeypatch):
    frame = pd.concat(
        [
            _limit_up_frame_for_profile_test(),
            pd.DataFrame([{
                "代码": "002948",
                "名称": "青岛银行",
                "涨停原因": "业绩超预期+青岛国资",
                "连板": "首板",
            }]),
        ],
        ignore_index=True,
    )
    frame.attrs.update({
        "source_valid": True,
        "data_date": "20260819",
        "trade_status": {"id": "closed", "name": "已收盘"},
        "pool_total": 2,
        "unique_total": 2,
        "reason_coverage": 1.0,
        "board_count_coverage": 1.0,
        "unknown_board_count": 0,
        "valid_empty": False,
        "page_count": 1,
    })
    monkeypatch.setattr(
        risk_service,
        "fetch_limit_up_events",
        lambda trade_date: _limit_event_series_for_profile_test(frame, trade_date),
    )
    monkeypatch.setattr(
        risk_service,
        "fetch_stock_sector_profiles",
        lambda codes, *, trade_date: (_ for _ in ()).throw(
            RuntimeError("股票行业 profile 存在缺失或错误交易日")
        ),
    )

    records, _, metadata = risk_service._fetch_leadership_pool("2026-08-19")

    assert all("sector_profile" not in record for record in records)
    assert metadata["industry_profile_status"]["status"] == "degraded"
    assert "错误交易日" in metadata["industry_profile_status"]["error"]


def test_leadership_fetch_requires_explicit_source_valid(monkeypatch):
    frame = _limit_up_frame_for_profile_test()
    frame.attrs.pop("source_valid")
    monkeypatch.setattr(
        risk_service,
        "fetch_limit_up_events",
        lambda trade_date: _limit_event_series_for_profile_test(frame, trade_date),
    )

    with pytest.raises(RuntimeError, match="未通过数据有效性校验"):
        risk_service._fetch_leadership_pool("2026-08-19")


def test_effective_trade_date_prefers_provider_then_turnover_date():
    china = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 8, 20, 0, 1, tzinfo=china)

    assert risk_service._effective_trade_date(
        {
            "provider_as_of": "2026-08-19T15:00:00+08:00",
            "market_turnover": {"today_date": "2026-08-18"},
        },
        now,
    ) == "2026-08-19"
    assert risk_service._effective_trade_date(
        {"market_turnover": {"today_date": "2026-08-19"}},
        now,
    ) == "2026-08-19"
    assert risk_service._effective_trade_date({}, now) == "2026-08-20"


@pytest.mark.parametrize(
    "trade_status",
    [
        {"id": 3, "name": "交易中"},
        {"id": "closed", "name": "已收盘"},
        {"id": "trading", "name": "连续交易"},
    ],
)
def test_leadership_trade_status_allows_trading_and_closed(trade_status):
    assert risk_service._leadership_trade_status_eligible(trade_status) is True


@pytest.mark.parametrize(
    "trade_status",
    [
        None,
        {},
        {"id": "pre_open", "name": "盘前"},
        {"id": 3, "name": "集合竞价"},
        {"id": "closed_day", "name": "休市"},
        {"id": "mystery", "name": "未知"},
    ],
)
def test_leadership_trade_status_rejects_preopen_and_unknown(trade_status):
    assert risk_service._leadership_trade_status_eligible(trade_status) is False


def test_preopen_leadership_pool_is_display_only(monkeypatch):
    _install_component_fakes(monkeypatch)
    monkeypatch.setattr(
        risk_service,
        "_fetch_leadership_pool",
        lambda trade_date: ([
            {"代码": "1", "名称": "甲", "涨停原因": "证券", "连板": "首板"},
            {"代码": "2", "名称": "乙", "涨停原因": "证券", "连板": "2天2板"},
            {"代码": "3", "名称": "丙", "涨停原因": "证券", "连板": "首板"},
        ], "ths", {
            "contract": "limit_event.v1",
            "schema_version": 1,
            "quality": "degraded",
            "quality_flags": ["provider_timestamp_missing"],
            "source_valid": True,
            "data_date": trade_date.replace("-", ""),
            "trade_status": {"id": "pre_open", "name": "集合竞价"},
            "pool_total": 3,
            "unique_total": 3,
            "reason_coverage": 1.0,
            "board_count_coverage": 1.0,
            "unknown_board_count": 0,
            "valid_empty": False,
            "page_count": 1,
        }),
    )

    result = risk_service.get_risk_appetite_data(_market_data())
    financial = next(group for group in result["groups"] if group["key"] == "financial")
    securities = next(item for item in financial["items"] if item["key"] == "securities")

    assert result["components"]["leadership_pool"]["source_valid"] is True
    assert result["components"]["leadership_pool"]["eligible_for_vote"] is False
    assert result["components"]["leadership_pool"]["reason"] == "trade_status_not_eligible"
    assert securities["evidence"]["leadership"] is None


def test_static_membership_reuses_the_existing_sector_profile():
    snapshot = {
        "sectors": {
            "bank": {
                "leadership": {
                    "limit_up_leaders": [{
                        "code": "600928",
                        "sector_profile": {
                            "industry": "银行Ⅱ",
                            "region": "陕西板块",
                            "concept_tags": ["互联网金融", "移动支付"],
                            "source": "push2delay",
                            "provider_as_of": "2026-08-19T15:00:00+08:00",
                            "fetched_at": "2026-08-19T15:00:01+08:00",
                        },
                    }],
                },
            },
        },
    }

    risk_service._attach_static_memberships(snapshot)

    membership = snapshot["sectors"]["bank"]["leadership"][
        "limit_up_leaders"
    ][0]["static_membership"]
    assert membership["status"] == "ready"
    assert membership["derived_from"] == "stock_sector_profile.v1"
    assert membership["source"] == "push2delay"
    assert membership["board_names"] == [
        "银行Ⅱ",
        "陕西板块",
        "互联网金融",
        "移动支付",
    ]
    assert {item["sector_key"] for item in membership["attributions"]} == {
        "bank",
        "internet_finance",
    }


def test_static_membership_is_explicitly_unavailable_without_a_profile():
    snapshot = {
        "sectors": {
            "bank": {
                "leadership": {
                    "limit_up_leaders": [{"code": "600928", "sector_profile": None}],
                },
            },
        },
    }

    risk_service._attach_static_memberships(snapshot)

    membership = snapshot["sectors"]["bank"]["leadership"][
        "limit_up_leaders"
    ][0]["static_membership"]
    assert membership == {
        "status": "unavailable",
        "source": None,
        "provider_as_of": None,
        "fetched_at": None,
        "derived_from": "stock_sector_profile.v1",
        "reason": "sector_profile_unavailable",
    }


def test_provider_leader_fallback_reaches_core_radar_and_summary():
    result = _offense_leader_result(core_count=1, radar_count=1)
    records = [
        {
            "板块代码": "BK1000",
            "板块名称": "常规板块0",
            "领涨股代码": "600001",
            "领涨股市场": 1,
            "领涨股票": "常规领涨",
            "领涨股涨幅": 6.8,
            "更新时间": "2026-08-19T10:30:00+08:00",
            "source": "push2",
        },
        {
            "板块代码": "BK2000",
            "板块名称": "轮动板块0",
            "领涨股代码": "300001",
            "领涨股市场": 0,
            "领涨股票": "轮动领涨",
            "领涨股涨幅": 9.2,
            "更新时间": "2026-08-19T10:30:00+08:00",
            "source": "push2delay",
        },
    ]
    statuses = {
        "industry_quotes": {
            "source": "eastmoney",
            "fetched_at": "2026-08-19T10:30:01+08:00",
            "stale": False,
        },
        "concept_quotes": {
            "source": "eastmoney",
            "fetched_at": "2026-08-19T10:30:02+08:00",
            "stale": False,
        },
    }

    risk_service._attach_provider_leader_fallbacks(result, records, statuses)

    core = result["offense"]["core"]["items"][0]["leader_snapshot"]
    radar = result["offense"]["lists"]["attacking"][0]["leader_snapshot"]
    summary = result["offense"]["summary"]["current_leaders"][0]["leader_snapshot"]
    assert set(core) >= {
        "status", "source", "provider_as_of", "fetched_at", "stale", "method", "items"
    }
    assert core["method"] == "provider_leader"
    assert core["items"][0]["code"] == "600001"
    assert radar["source"] == "push2delay"
    assert radar["items"][0]["name"] == "轮动领涨"
    assert summary == radar
    assert risk_service._board_leader_source_hints(result) == {
        "BK1000": "push2",
        "BK2000": "push2delay",
    }


def test_static_provider_leader_cannot_masquerade_as_defense_resonance():
    result = _defense_flow_leader_result()
    records = [{
        "板块代码": "BK3000",
        "板块名称": "防守板块0",
        "leader_instrument_id": "600900.SH",
        "领涨股代码": "600900",
        "领涨股票": "长江电力",
        "领涨股涨幅": 2.8,
        "更新时间": "2026-08-19T10:30:00+08:00",
        "source": "push2",
    }]

    risk_service._attach_provider_leader_fallbacks(
        result,
        records,
        {"industry_quotes": {
            "source": "push2",
            "fetched_at": "2026-08-19T10:30:01+08:00",
            "stale": False,
        }},
    )

    snapshot = result["sector_flow_trajectory"]["sectors"][0]["leader_snapshot"]
    assert snapshot["status"] == "unavailable"
    assert snapshot["status_label"] == "共振条件暂缺"
    assert snapshot["leaders"] == []


def test_static_provider_leader_cannot_masquerade_as_offense_resonance():
    result = _two_direction_flow_leader_result()
    records = [{
        "板块代码": "BK1101",
        "板块名称": "先进封装",
        "leader_instrument_id": "688041.SH",
        "领涨股代码": "688041",
        "领涨股票": "海光信息",
        "领涨股涨幅": 8.6,
        "更新时间": "2026-08-19T10:30:00+08:00",
        "source": "push2",
    }]

    risk_service._attach_provider_leader_fallbacks(
        result,
        records,
        {"concept_quotes": {
            "source": "push2",
            "fetched_at": "2026-08-19T10:30:01+08:00",
            "stale": False,
        }},
    )

    snapshot = result["offense_sector_flow_trajectory"]["sectors"][0][
        "leader_snapshot"
    ]
    assert snapshot["status"] == "unavailable"
    assert snapshot["leaders"] == []
    assert "BK1101" in risk_service._board_leader_targets(result)


def test_sector_flow_resonance_requires_aligned_positive_funds_and_speed():
    latest = {
        "provider_as_of": "2026-08-19T10:30:00+08:00",
        "delta_5m_cny": 30_000_000,
    }
    slow = _ready_leader_snapshot("600001", name="较慢股")
    fast = _ready_leader_snapshot("600002", name="高共振股")
    slow["items"][0]["speed_pct"] = 0.4
    fast["items"][0]["speed_pct"] = 1.6
    snapshot = copy.deepcopy(slow)
    snapshot["items"].extend(fast["items"])

    resonance = risk_service._sector_flow_leader_snapshot(snapshot, latest)

    assert resonance["status"] == "full"
    assert resonance["selection_method"] == "sector_fund_flow_stock_speed.v1"
    assert resonance["marginal_window_minutes"] == 5
    assert resonance["leaders"] == [{
        "instrument_id": "600002.SH",
        "name": "高共振股",
        "change_pct": 8.0,
        "speed_pct": 1.6,
        "main_net_inflow_cny": 50_000_000.0,
        "resonance_strength": "high",
        "price": 20.0,
        "provider_as_of": "2026-08-19T10:30:00+08:00",
    }]


def test_sector_flow_resonance_abstains_without_simultaneous_evidence():
    snapshot = _ready_leader_snapshot("600001")
    no_inflow = risk_service._sector_flow_leader_snapshot(
        snapshot,
        {
            "provider_as_of": "2026-08-19T10:30:00+08:00",
            "delta_5m_cny": -1,
        },
    )
    misaligned = risk_service._sector_flow_leader_snapshot(
        snapshot,
        {
            "provider_as_of": "2026-08-19T10:35:00+08:00",
            "delta_5m_cny": 30_000_000,
        },
    )

    assert no_inflow["status"] == "no_match"
    assert no_inflow["leaders"] == []
    assert misaligned["status"] == "no_match"
    assert misaligned["leaders"] == []


def test_legacy_price_leader_snapshot_remains_readable():
    snapshot = SectorFlowLeaderSnapshotV1.model_validate({
        "status": "fallback",
        "status_label": "板块快照领涨",
        "source": "push2delay",
        "provider_as_of": "2026-08-19T10:30:00+08:00",
        "leaders": [{
            "instrument_id": "600900.SH",
            "name": "长江电力",
            "change_pct": 2.8,
            "price": None,
            "provider_as_of": "2026-08-19T10:30:00+08:00",
        }],
    })

    assert snapshot.selection_method == "price_leader.v1"
    assert snapshot.leaders[0].speed_pct is None


def test_board_leader_enrichment_caps_fourteen_distinct_single_flight_requests(monkeypatch):
    gate = threading.Event()
    started = []

    def blocking_fetch(board_code, _source_hint=None):
        started.append(board_code)
        gate.wait(timeout=5)
        return _ready_leader_snapshot("600001", name=board_code)

    monkeypatch.setattr(risk_service, "_fetch_board_leader_snapshot", blocking_fetch)
    result = _offense_leader_result(core_count=8, radar_count=8)

    first = risk_service._overlay_board_leaders(
        copy.deepcopy(result), "2026-08-19"
    )
    risk_service._overlay_board_leaders(copy.deepcopy(result), "2026-08-19")

    with risk_service._cache_lock:
        keys = list(risk_service._board_leader_futures)
        futures = list(risk_service._board_leader_futures.values())
    assert [key[1] for key in keys] == [
        "BK1000", "BK1001", "BK1002", "BK2000", "BK2001", "BK2002",
        "BK1003", "BK1004", "BK1005", "BK1006", "BK1007",
        "BK2003", "BK2004", "BK2005",
    ]
    assert len(set(keys)) == 14
    assert first["offense"]["core"]["items"][0]["leader_snapshot"]["status"] == "loading"

    gate.set()
    for future in futures:
        future.result(timeout=5)
    assert len(started) == 14


def test_main_snapshot_cache_gets_a_fresh_leader_overlay_without_rebuild(monkeypatch):
    # This test exercises cache overlay, not live-session lag. Keep it stable
    # when the suite runs on a later trading day during market hours.
    monkeypatch.setattr(risk_service, "_board_leader_market_open", lambda _now: False)
    trade_date = "2026-08-19"
    result = _offense_leader_result(core_count=1, radar_count=0)
    result["offense"]["core"]["items"][0]["leader_snapshot"] = {
        "status": "unavailable",
        "source": None,
        "provider_as_of": None,
        "fetched_at": None,
        "stale": False,
        "method": "provider_leader",
        "items": [],
    }
    risk_service._prepare_board_leader_trade_date(trade_date)
    now_mono = time.monotonic()
    with risk_service._cache_lock:
        risk_service._board_leader_cache[(trade_date, "BK1000")] = {
            **_ready_leader_snapshot("600099"),
            "cached_at": now_mono,
            "last_attempt_at": now_mono,
            "last_error": None,
        }
        risk_service._snapshot_cache = copy.deepcopy(result)
        risk_service._snapshot_cached_at = now_mono
        risk_service._snapshot_trade_date = trade_date

    response = risk_service.get_risk_appetite_data({
        "provider_as_of": "2026-08-19T10:31:00+08:00",
    })

    snapshot = response["offense"]["core"]["items"][0]["leader_snapshot"]
    assert snapshot["status"] == "full"
    assert snapshot["method"] == "board_constituents"
    assert snapshot["items"][0]["code"] == "600099"


def test_board_leader_failure_keeps_provider_fallback(monkeypatch):
    result = _offense_leader_result(core_count=1, radar_count=0)
    risk_service._attach_provider_leader_fallbacks(
        result,
        [{
            "板块代码": "BK1000",
            "板块名称": "常规板块0",
            "领涨股代码": "600001",
            "领涨股票": "即时领涨",
            "领涨股涨幅": 5.0,
            "更新时间": "2026-08-19T10:30:00+08:00",
            "source": "push2delay",
        }],
        {"industry_quotes": {
            "source": "eastmoney",
            "fetched_at": "2026-08-19T10:30:02+08:00",
            "stale": False,
        }},
    )

    gate = threading.Event()

    def fail(_board_code, _source_hint=None):
        gate.wait(timeout=5)
        raise RuntimeError("constituents unavailable")

    monkeypatch.setattr(risk_service, "_fetch_board_leader_snapshot", fail)
    pending = risk_service._overlay_board_leaders(
        copy.deepcopy(result), "2026-08-19"
    )
    with risk_service._cache_lock:
        future = next(iter(risk_service._board_leader_futures.values()))
    gate.set()
    with pytest.raises(RuntimeError, match="constituents unavailable"):
        future.result(timeout=5)
    fallback = risk_service._overlay_board_leaders(
        copy.deepcopy(result), "2026-08-19"
    )["offense"]["core"]["items"][0]["leader_snapshot"]

    assert pending["offense"]["core"]["items"][0]["leader_snapshot"]["status"] == "fallback"
    assert pending["offense"]["core"]["items"][0]["leader_snapshot"]["refreshing"] is True
    assert fallback["status"] == "fallback"
    assert fallback["method"] == "provider_leader"
    assert fallback["items"][0]["name"] == "即时领涨"
    assert fallback["error"] == "constituents unavailable"


def test_board_leader_stale_cache_survives_refresh_error(monkeypatch):
    trade_date = "2026-08-19"
    result = _offense_leader_result(core_count=1, radar_count=0)
    risk_service._prepare_board_leader_trade_date(trade_date)
    with risk_service._cache_lock:
        risk_service._board_leader_cache[(trade_date, "BK1000")] = {
            **_ready_leader_snapshot("600088"),
            "cached_at": 850.0,
            "last_attempt_at": 990.0,
            "last_error": "refresh failed",
        }
    monkeypatch.setattr(risk_service.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(
        risk_service,
        "_schedule_board_leader_fetch",
        lambda *args, **kwargs: False,
    )

    response = risk_service._overlay_board_leaders(result, trade_date)
    snapshot = response["offense"]["core"]["items"][0]["leader_snapshot"]

    assert snapshot["status"] == "stale"
    assert snapshot["stale"] is True
    assert snapshot["items"][0]["code"] == "600088"
    assert snapshot["error"] == "refresh failed"


def test_board_leader_cache_is_never_reused_across_trade_dates():
    risk_service._prepare_board_leader_trade_date("2026-08-18")
    with risk_service._cache_lock:
        risk_service._board_leader_cache[("2026-08-18", "BK1000")] = {
            **_ready_leader_snapshot("600001"),
            "cached_at": time.monotonic(),
            "last_attempt_at": time.monotonic(),
            "last_error": None,
        }

    risk_service._prepare_board_leader_trade_date("2026-08-19")

    with risk_service._cache_lock:
        assert risk_service._board_leader_trade_date == "2026-08-19"
        assert risk_service._board_leader_cache == {}
        assert risk_service._board_leader_futures == {}


def test_board_leader_targets_prioritise_strong_and_strengthening_core():
    result = _offense_leader_result(core_count=5, radar_count=3)
    core = result["offense"]["core"]["items"]
    core[0].update(level="weak", direction="weakening")
    core[1].update(level="medium", direction="flat")
    core[2].update(level="strong", direction="flat")
    core[3].update(level="weak", direction="strengthening")
    core[4].update(level="medium", direction="weakening")

    assert risk_service._board_leader_targets(result) == [
        "BK1002", "BK1003", "BK1001", "BK2000", "BK2001", "BK2002",
        "BK1004", "BK1000",
    ]


def test_board_leader_targets_put_rising_defense_cards_first_without_dropping_groups():
    result = _defense_flow_leader_result()

    assert risk_service._board_leader_targets(result) == [
        "BK3000", "BK3001", "BK3002",
        "BK1000", "BK1001", "BK1002",
        "BK2000", "BK2001", "BK2002",
        "BK1003", "BK1004", "BK2003", "BK2004",
    ]


def test_board_leader_provider_staleness_only_advances_during_trading():
    china = ZoneInfo("Asia/Shanghai")
    provider = "2026-08-19T10:30:00+08:00"

    assert risk_service._board_leader_provider_stale(
        provider,
        "2026-08-19",
        datetime(2026, 8, 19, 10, 36, tzinfo=china),
    )
    assert not risk_service._board_leader_provider_stale(
        provider,
        "2026-08-19",
        datetime(2026, 8, 19, 16, 0, tzinfo=china),
    )


def test_board_leader_cache_freezes_after_close_instead_of_expiring():
    trade_date = "2026-08-19"
    risk_service._prepare_board_leader_trade_date(trade_date)
    with risk_service._cache_lock:
        risk_service._board_leader_cache[(trade_date, "BK1000")] = {
            **_ready_leader_snapshot("600001"),
            "cached_at": 100.0,
            "last_attempt_at": 100.0,
            "last_error": None,
        }

    snapshot, error, pending = risk_service._leader_snapshot_from_cache(
        trade_date,
        "BK1000",
        1000.0,
        now=datetime(2026, 8, 19, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert error is None
    assert pending is False
    assert snapshot["status"] == "full"
    assert snapshot["items"][0]["code"] == "600001"


def test_headline_changes_only_after_two_consecutive_snapshots():
    positive = {
        "opening_observation": False,
        "emotion": {"key": "strong", "label": "积极", "tone": "positive"},
        "structure": {"key": "金融进攻", "label": "金融进攻", "reasons": []},
    }
    cautious = {
        "opening_observation": False,
        "emotion": {"key": "weak", "label": "谨慎", "tone": "negative"},
        "structure": {"key": "普遍退潮", "label": "普遍退潮", "reasons": []},
    }

    first = risk_service._stabilise_headline(copy.deepcopy(positive))
    pending = risk_service._stabilise_headline(copy.deepcopy(cautious))
    published = risk_service._stabilise_headline(copy.deepcopy(cautious))

    assert first["emotion"]["label"] == "积极"
    assert pending["emotion"]["label"] == "积极"
    assert pending["pending_headline"]["samples"] == 1
    assert published["emotion"]["label"] == "谨慎"


def test_headline_never_reuses_a_previous_trade_date():
    previous_day = {
        "version": "risk-appetite-v1.3",
        "trade_date": "2026-08-18",
        "opening_observation": False,
        "emotion": {"key": "strong", "label": "积极", "tone": "positive"},
        "structure": {"key": "金融进攻", "label": "金融进攻", "reasons": []},
    }
    current_day = {
        "version": "risk-appetite-v1.3",
        "trade_date": "2026-08-19",
        "opening_observation": False,
        "emotion": {"key": "weak", "label": "谨慎", "tone": "negative"},
        "structure": {"key": "普遍退潮", "label": "普遍退潮", "reasons": []},
    }

    risk_service._stabilise_headline(copy.deepcopy(previous_day))
    published = risk_service._stabilise_headline(copy.deepcopy(current_day))

    assert published["emotion"]["label"] == "谨慎"
    assert published["structure"]["label"] == "普遍退潮"
    assert "pending_headline" not in published


def test_trajectory_confirms_bank_relative_strength_after_two_fresh_minutes():
    china = ZoneInfo("Asia/Shanghai")

    def build(level: str, minute: int) -> dict:
        change, up, down = {
            "weak": (-8.0, 30, 70),
            "strong": (8.0, 70, 30),
        }[level]
        now = datetime(2026, 8, 19, 10, minute, tzinfo=china)
        snapshot = risk_service.build_risk_appetite_snapshot(
            industry_records=[
                *_background("行业"),
                {
                    "板块名称": "银行",
                    "涨跌幅": change,
                    "上涨家数": up,
                    "下跌家数": down,
                },
            ],
            concept_records=_background("概念"),
            index_records=[
                {"名称": "上证指数", "涨跌幅": 0.2},
                {"名称": "深证成指", "涨跌幅": 0.3},
                {"名称": "创业板指", "涨跌幅": 0.5},
            ],
            market_breadth={"上涨": 3000, "下跌": 2000},
            market_turnover={"available": True, "direction": "expand", "difference": 1},
            as_of=now,
        )
        provider_time = now.isoformat(timespec="seconds")
        status = {
            "source": "test",
            "fetched_at": provider_time,
            "as_of": provider_time,
            "provider_as_of": provider_time,
            "age_seconds": 0,
            "stale": False,
            "expired": False,
            "refreshing": False,
            "error": None,
        }
        statuses = {
            "market_breadth": {**status, "provider_as_of": None},
            "industry_quotes": status,
            "concept_quotes": status,
        }
        result = risk_service._view_model(snapshot, statuses)
        return risk_service._attach_trajectory(
            result,
            snapshot,
            statuses,
            {
                "provider_as_of": provider_time,
                "market_state": {"label": "盘中交易", "is_open": True},
            },
            now,
            record=True,
        )

    assert build("weak", 0)["dynamics"]["status"] == "collecting"
    assert build("weak", 1)["dynamics"]["status"] == "ready"
    assert build("strong", 2)["dynamics"]["axes"]["bank_support"]["pending_state"] == "strong"
    confirmed = build("strong", 3)

    bank = confirmed["dynamics"]["axes"]["bank_support"]
    assert bank["confirmed_state"] == "strong"
    assert bank["direction"] == "strengthening"
    assert [point["state"] for point in bank["path"]] == ["weak", "strong"]
    assert confirmed["dynamics"]["headline"] == "银行承接相对增强"
    bank_item = next(
        item
        for group in confirmed["groups"]
        for item in group["items"]
        if item["key"] == "bank"
    )
    assert bank_item["trend"]["confirmed_state"] == "strong"


def test_trajectory_requires_fresh_leadership_when_it_changed_sector_state():
    china = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 8, 19, 10, 30, tzinfo=china)
    snapshot = risk_service.build_risk_appetite_snapshot(
        industry_records=[
            *_background("行业"),
            {
                "板块名称": "种植业",
                "涨跌幅": 8.0,
                "上涨家数": 7,
                "下跌家数": 14,
                "主力净流入-占比": -1.0,
                "provider_as_of": now.isoformat(timespec="seconds"),
            },
        ],
        concept_records=_background("概念"),
        leadership_records=[
            {"代码": "1", "涨停原因": "粮食", "连板": "首板"},
            {"代码": "2", "涨停原因": "种业", "连板": "2天2板"},
            {"代码": "3", "涨停原因": "复合肥", "连板": "首板"},
        ],
        leadership_source_status={
            "source_valid": True,
            "eligible_for_vote": True,
            "data_date": "20260819",
        },
        trade_date="2026-08-19",
        as_of=now,
    )
    assert snapshot["sectors"]["agriculture"]["core_level"] == "medium"
    assert snapshot["sectors"]["agriculture"]["level"] == "strong"

    quote_status = {
        "source": "test",
        "fetched_at": now.isoformat(timespec="seconds"),
        "provider_as_of": now.isoformat(timespec="seconds"),
        "stale": False,
        "expired": False,
    }
    statuses = {
        "industry_quotes": quote_status,
        "concept_quotes": quote_status,
        "market_breadth": {**quote_status, "provider_as_of": None},
        "leadership_pool": {
            **quote_status,
            "provider_as_of": None,
            "data_date": "20260819",
            "stale": True,
        },
    }
    market_data = {"provider_as_of": now.isoformat(timespec="seconds")}

    stale_axes, _, _ = risk_service._trajectory_inputs(
        snapshot, statuses, market_data, "2026-08-19"
    )
    assert stale_axes["sector:agriculture"] == "unknown"

    statuses["leadership_pool"]["stale"] = False
    fresh_axes, metrics, providers = risk_service._trajectory_inputs(
        snapshot, statuses, market_data, "2026-08-19"
    )
    assert fresh_axes["sector:agriculture"] == "strong"
    assert metrics["sector:agriculture"]["leadership"] == {
        "matched_count": 3,
        "max_board_count": 2,
        "vote": "strong",
        "rank": 1,
    }
    assert providers["leadership_pool"] == "20260819"


def test_rotation_frontend_keeps_fund_direction_separate_from_amount_delta():
    current = {
        "schema_version": "rotation-radar-v1",
        "config_version": "rotation-radar-config-v1",
        "status": "ready",
        "status_label": "updated",
        "as_of": "2026-08-19T10:10:00+08:00",
        "sample_count": 8,
        "coverage": {
            "industry": {"total": 80, "effective": 78, "frozen": False},
            "concept": {"total": 100, "effective": 96, "frozen": False},
        },
        "core": {
            "schema_version": "core-offense-v1",
            "status": "ready",
            "items": [{
                "key": "semiconductor",
                "name": "半导体",
                "representative_board": {
                    "taxonomy": "industry",
                    "board_code": "BK1036",
                    "name": "半导体",
                },
            }],
        },
        "summary": {
            "strength": {"key": "medium", "label": "中"},
            "direction": {"key": "strengthening", "label": "相对增强"},
            "structure": {"key": "broadening", "label": "行业骨架扩散"},
            "counts": {"attacking": 1, "rotating": 0, "cooling": 0},
            "current_leaders": [],
        },
        "attacking": [{
            "id": "industry:BK1036",
            "taxonomy": "industry",
            "name": "半导体材料",
            "state": "attacking",
            "state_label": "正在进攻",
            "state_since": "2026-08-19T10:09:00+08:00",
            "direction": "stable",
            "strength": "strong",
            "family": {"key": "semiconductor", "label": "半导体材料与设备"},
            "role": {"key": "technology", "label": "科技进攻"},
            "flags": [],
            "provider_as_of": "2026-08-19T10:10:00+08:00",
            "source": "push2",
            "metrics": {
                "change_pct": 3.1,
                "price_percentile": 0.91,
                "breadth_ratio": 0.72,
                "flow_amount": 500_000_000,
                "flow_ratio": 5.2,
                "flow_rank": 4,
                "fund_state": "strong",
                "fund_delta": -10_000_000,
                "fund_direction": "stable",
                "previous_provider_as_of": "2026-08-19T10:09:00+08:00",
            },
        }],
        "rotating": [],
        "cooling": [],
        "unclassified": [],
        "events": [],
    }
    current["summary"]["current_leaders"] = [current["attacking"][0]]

    view = risk_service._rotation_frontend_view(
        current,
        phase="trading",
        market_direction="weakening",
    )

    item = view["lists"]["attacking"][0]
    assert item["funds"]["delta_amount"] == -10_000_000
    assert item["funds"]["direction"] == "flat"
    assert view["core"]["items"][0]["representative_board"]["board_code"] == "BK1036"
    assert view["summary"]["current_leaders"][0]["price"]["percentile"] == 0.91
    assert view["summary"]["current_leaders"][0]["breadth"]["ratio"] == 0.72
    assert view["summary"]["structure"]["key"] == "countertrend"
    assert view["summary"]["counts"] == {
        "attacking": 1,
        "rotating": 0,
        "divergent": 0,
        "cooling": 0,
    }

    midday = risk_service._rotation_frontend_view(
        current,
        phase="midday_break",
        market_direction="flat",
    )
    closed = risk_service._rotation_frontend_view(
        current,
        phase="closed",
        market_direction="flat",
    )
    assert midday["core"]["status"] == "partial"
    assert midday["core"]["status_label"] == "午休，常规进攻信号定格"
    assert closed["core"]["status"] == "closed"
    assert closed["core"]["status_label"] == "今日常规进攻已定格"


def test_rotation_sampler_records_while_ordinary_reads_do_not_advance():
    china = ZoneInfo("Asia/Shanghai")
    first = datetime(2026, 8, 19, 10, 0, tzinfo=china)

    def records(prefix: str, when: datetime, change: float) -> list[dict]:
        return [{
            "板块代码": f"{prefix}{index:03d}",
            "板块名称": "半导体材料" if index == 0 else f"{prefix}板块{index}",
            "涨跌幅": change if index == 0 else float(index),
            "上涨家数": 70 if index == 0 else 50,
            "下跌家数": 30 if index == 0 else 50,
            "主力净流入": 100_000_000 if index == 0 else 0,
            "主力净流入-占比": 5.0 if index == 0 else 0,
            "主力净流入排名": index + 1,
            "更新时间": when.isoformat(timespec="seconds"),
        } for index in range(8)]

    result = {"dynamics": {"axes": {}}}
    values = {
        "industry_quotes": records("I", first, 5.0),
        "concept_quotes": records("C", first, 5.0),
    }
    statuses = {
        "industry_quotes": {"source": "push2"},
        "concept_quotes": {"source": "push2"},
    }
    market = {
        "provider_as_of": first.isoformat(timespec="seconds"),
        "market_state": {"label": "盘中交易", "is_open": True},
    }
    recorded = risk_service._attach_rotation_radar(
        copy.deepcopy(result), values, statuses, market, first, record=True
    )
    second = first.replace(minute=1)
    values["industry_quotes"] = records("I", second, 6.0)
    values["concept_quotes"] = records("C", second, 6.0)
    read_only = risk_service._attach_rotation_radar(
        copy.deepcopy(result), values, statuses, market, second, record=False
    )

    stats = risk_service._rotation_store.get_snapshot_stats("2026-08-19")
    assert len(stats) == 1
    assert recorded["offense"]["sample_count"] == 1
    assert read_only["offense"]["sample_count"] == 1
    assert recorded["sector_flow_trajectory"]["contract"] == "sector_flow_trajectory.v1"
    assert recorded["sector_flow_trajectory"]["direction"] == "defense"
    assert recorded["offense_sector_flow_trajectory"]["direction"] == "offense"
    assert read_only["sector_flow_trajectory"]["schema_version"] == 1


def test_rotation_sampler_loads_backfill_while_reads_reuse_it(monkeypatch):
    china = ZoneInfo("Asia/Shanghai")
    observed = datetime(2026, 8, 19, 10, 40, tzinfo=china)

    def background(prefix: str) -> list[dict]:
        return [
            {
                "板块代码": f"{prefix}{index:03d}",
                "板块名称": f"{prefix}背景{index}",
                "涨跌幅": float(index - 3),
                "上涨家数": 50,
                "下跌家数": 50,
                "主力净流入": float(index) * 10_000_000,
                "主力净流入-占比": float(index),
                "主力净流入排名": index + 1,
                "更新时间": observed.isoformat(timespec="seconds"),
            }
            for index in range(7)
        ]

    industry = [
        *background("I"),
        {
            "板块代码": "BK0428",
            "板块名称": "电力",
            "涨跌幅": 5.0,
            "上涨家数": 80,
            "下跌家数": 20,
            "主力净流入": 1_100_000_000,
            "主力净流入-占比": 8.0,
            "主力净流入排名": 1,
            "更新时间": observed.isoformat(timespec="seconds"),
        },
    ]
    values = {"industry_quotes": industry, "concept_quotes": background("C")}
    statuses = {
        "industry_quotes": {"source": "push2delay"},
        "concept_quotes": {"source": "push2delay"},
    }
    calls = []
    supplemental = {
        "electric_power": tuple(
            {
                "provider_as_of": observed.replace(hour=10, minute=30) + timedelta(minutes=offset),
                "cumulative_cny": (1.0 + offset) * 100_000_000,
                "source_family": "eastmoney",
            }
            for offset in range(10)
        )
    }

    def backfill(targets, **kwargs):
        calls.append(
            (
                tuple(targets),
                kwargs["load_missing"],
                kwargs.get("refresh_existing", False),
            )
        )
        return supplemental

    monkeypatch.setattr(
        risk_service,
        "fetch_sector_intraday_fund_flow_backfill",
        backfill,
    )
    market = {
        "provider_as_of": observed.isoformat(timespec="seconds"),
        "market_state": {"label": "盘中交易", "is_open": True},
    }

    recorded = risk_service._attach_rotation_radar(
        {"dynamics": {"axes": {}}},
        values,
        statuses,
        market,
        observed,
        record=True,
    )
    read_only = risk_service._attach_rotation_radar(
        {"dynamics": {"axes": {}}},
        values,
        statuses,
        market,
        observed,
        record=False,
    )

    sector = next(
        item
        for item in recorded["sector_flow_trajectory"]["sectors"]
        if item["sector_key"] == "electric_power"
    )
    assert [
        (load_missing, refresh_existing)
        for _targets, load_missing, refresh_existing in calls
    ] == [(True, True), (False, False), (False, False)]
    assert calls[0][0][0]["provider_sector_code"] == "BK0428"
    assert sector["status"] == "ready"
    assert sector["latest"]["delta_5m_cny"] == 500_000_000
    assert read_only["sector_flow_trajectory"] == recorded["sector_flow_trajectory"]


def test_sampler_rotates_one_sector_backfill_target_per_minute() -> None:
    targets = tuple({"sector_key": f"sector-{index}"} for index in range(4))
    china = ZoneInfo("Asia/Shanghai")

    first = risk_service._sector_flow_backfill_load_targets(
        targets,
        datetime(2026, 8, 25, 10, 0, tzinfo=china),
    )
    second = risk_service._sector_flow_backfill_load_targets(
        targets,
        datetime(2026, 8, 25, 10, 1, tzinfo=china),
    )

    assert len(first) == 1
    assert len(second) == 1
    assert first != second


def test_rotation_flow_reads_the_effective_provider_trade_date(monkeypatch):
    requested = []

    class Store:
        def get_current(self, trade_date, config_version, **_kwargs):
            requested.append((trade_date, config_version))
            return risk_service.analyze_rotation_snapshots([])

    monkeypatch.setattr(risk_service, "_get_rotation_store", lambda: Store())
    observed = datetime(2026, 8, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    market = {
        "provider_as_of": "2026-08-21T15:00:00+08:00",
        "market_state": {"label": "今日收盘", "is_open": False},
    }

    result = risk_service._attach_rotation_radar(
        {"dynamics": {"axes": {}}},
        {"industry_quotes": [], "concept_quotes": []},
        {
            "industry_quotes": {"source": "push2"},
            "concept_quotes": {"source": "push2"},
        },
        market,
        observed,
        record=False,
    )

    assert requested == [("2026-08-21", risk_service.ROTATION_CONFIG_VERSION)]
    assert result["sector_flow_trajectory"]["market_phase"] == "closed"


def test_closed_rotation_read_never_cold_loads_exact_backfill(monkeypatch):
    calls = []

    class Store:
        def get_current(self, *_args, **_kwargs):
            return risk_service.analyze_rotation_snapshots([])

    def backfill(targets, **kwargs):
        calls.append((tuple(targets), kwargs["load_missing"]))
        return {}

    monkeypatch.setattr(risk_service, "_get_rotation_store", lambda: Store())
    monkeypatch.setattr(
        risk_service,
        "fetch_sector_intraday_fund_flow_backfill",
        backfill,
    )
    observed = datetime(2026, 8, 24, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai"))

    risk_service._attach_rotation_radar(
        {"dynamics": {"axes": {}}},
        {"industry_quotes": [], "concept_quotes": []},
        {"industry_quotes": {}, "concept_quotes": {}},
        {
            "provider_as_of": "2026-08-24T15:00:00+08:00",
            "market_state": {"label": "今日收盘", "is_open": False},
        },
        observed,
        record=False,
    )

    assert calls == [((), False)]
