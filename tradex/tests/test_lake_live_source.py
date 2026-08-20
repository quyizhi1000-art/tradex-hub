from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tradex.data_lake.live_source import LiveCaptureError, TradexLiveSource


SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_live_source_captures_the_inputs_used_by_risk_calculation():
    observed_at = datetime(2026, 8, 19, 10, 1, 17, tzinfo=SHANGHAI)

    def market_fetcher(*, force):
        assert force is True
        return {
            "source": "test-market",
            "provider_as_of": observed_at.isoformat(),
            "indices": [{"code": "000001", "change_pct": 1.0}],
        }

    def risk_fetcher(market_data, *, force, record_trajectory, capture_observer):
        assert force is True
        assert record_trajectory is True
        capture_observer({
            "trade_date": "2026-08-19",
            "observed_at": observed_at,
            "minute_bucket": observed_at.replace(second=0),
            "market_phase": "trading",
            "market_data": market_data,
            "values": {"market_breadth": [{"up_count": 3200}]},
            "statuses": {
                "market_breadth": {
                    "source": "eastmoney",
                    "provider_as_of": observed_at.isoformat(),
                }
            },
        })
        return {"version": "risk-v1", "emotion": {"key": "strong"}}

    result = TradexLiveSource(
        market_fetcher=market_fetcher,
        risk_fetcher=risk_fetcher,
    ).capture("capture-1")

    assert result.bundle.capture_id == "capture-1"
    assert result.bundle.minute_bucket.second == 0
    assert result.bundle.datasets["market_breadth"].records[0]["up_count"] == 3200
    assert result.bundle.datasets["market_overview"].source == "test-market"
    assert result.feature_payload["version"] == "risk-v1"


def test_live_source_rejects_fetcher_that_does_not_call_observer():
    source = TradexLiveSource(
        market_fetcher=lambda **kwargs: {},
        risk_fetcher=lambda *args, **kwargs: {},
    )
    with pytest.raises(LiveCaptureError, match="did not expose"):
        source.capture("capture-1")
