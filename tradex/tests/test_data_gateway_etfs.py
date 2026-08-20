from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from astock_signals.smart_router import SmartRouter
from tradex.dashboard import risk_service
from tradex.data_gateway.contracts import QualityStatus
from tradex.data_gateway.etfs import fetch_etf_quotes, etf_quotes_to_legacy_records
from tradex.data_gateway.market_structure import metadata_to_component_status


_NOW = datetime(2026, 8, 19, 10, 31, tzinfo=ZoneInfo("Asia/Shanghai"))


def _payload(*, provider_as_of: str | None = "2026-08-19T10:30:00+08:00"):
    rows = [
        {
            "etf_code": "513500",
            "name": "标普500ETF",
            "price": 1.85,
            "change_pct": 0.55,
            "amount": 9_250_000,
            "provider_as_of": provider_as_of,
        },
        {
            "etf_code": "510300",
            "name": "沪深300ETF",
            "price": 4.2,
            "change_pct": 0.12,
            "amount": 33_600_000,
            "provider_as_of": provider_as_of,
        },
    ]
    return {
        "source": "AKShare (东财)",
        "data_type": "etf_realtime",
        "timestamp": "2026-08-19T10:30:01+08:00",
        "etfs": rows,
    }


def _router(provider: str, payload) -> SmartRouter:
    router = SmartRouter()
    router.register("etf_data", provider, lambda **_kwargs: payload, priority=1)
    return router


def test_astock_etf_payload_maps_to_sorted_contract_and_legacy_view() -> None:
    series = fetch_etf_quotes(
        limit=2,
        router=_router("astock_signals", _payload()),
        now=_NOW,
    )

    assert series.metadata.contract == "etf_quote.v1"
    assert series.metadata.quality is QualityStatus.ACCEPTED
    assert [item.instrument_id for item in series.quotes] == [
        "510300.SH",
        "513500.SH",
    ]
    assert [item.amount_cny for item in series.quotes] == [33_600_000, 9_250_000]
    legacy = etf_quotes_to_legacy_records(series)
    assert legacy[0] == {
        "etf_code": "510300",
        "name": "沪深300ETF",
        "price": 4.2,
        "change_pct": 0.12,
        "amount": 33_600_000,
        "provider_as_of": "2026-08-19T10:30:00+08:00",
        "source": "astock_signals",
    }


def test_etf_payload_without_provider_timestamp_is_explicitly_degraded() -> None:
    series = fetch_etf_quotes(
        router=_router("astock_signals", _payload(provider_as_of=None)),
        now=_NOW,
    )

    assert series.metadata.provider_as_of is None
    assert series.metadata.quality is QualityStatus.DEGRADED
    assert series.metadata.quality_flags == ("provider_timestamp_missing",)


def test_etf_invalid_primary_payload_falls_back_before_recording_success() -> None:
    bad_payload = _payload()
    bad_payload["etfs"][0]["amount"] = -1
    good_payload = {
        "etfs": [
            {
                "代码": "159915",
                "名称": "创业板ETF",
                "最新价": 2.1,
                "涨跌幅": -0.3,
                "成交额": 6_300_000,
                "更新时间": "2026-08-19T10:30:00+08:00",
            }
        ]
    }
    router = SmartRouter()
    router.register("etf_data", "bad_paid", lambda **_kwargs: bad_payload, priority=1)
    router.register(
        "etf_data", "astock_signals", lambda **_kwargs: good_payload, priority=2
    )

    series = fetch_etf_quotes(router=router, now=_NOW)

    assert series.metadata.provider == "astock_signals"
    assert series.quotes[0].instrument_id == "159915.SZ"
    health = {item["source"]: item for item in router.get_health_report()}
    assert health["etf_data:bad_paid"]["fail_count"] == 1
    assert health["etf_data:bad_paid"]["success_rate"] == 0.0


def test_etf_contract_rejects_duplicate_instruments() -> None:
    payload = _payload()
    payload["etfs"][1]["etf_code"] = "513500"

    with pytest.raises(RuntimeError, match="All sources for 'etf_data' failed"):
        fetch_etf_quotes(router=_router("astock_signals", payload), now=_NOW)


def test_dashboard_etf_fetcher_exposes_gateway_quality(monkeypatch) -> None:
    series = fetch_etf_quotes(
        router=_router("astock_signals", _payload(provider_as_of=None)),
        now=_NOW,
    )
    monkeypatch.setattr(risk_service, "fetch_etf_quotes", lambda: series)

    records, source, metadata = risk_service._fetch_etfs()

    assert records[0]["name"] == "沪深300ETF"
    assert source == "astock_signals"
    assert metadata == metadata_to_component_status(series.metadata)
    assert metadata["partial"] is True
