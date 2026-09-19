from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tradex.data_gateway.intraday_scan_quotes import fetch_intraday_scan_quotes, quote_time_is_current

NOW = datetime(2026, 9, 18, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def row(code, *, stamp=NOW, last=10):
    return {"code": code, "name": "stock", "last": last, "change_pct": 0,
            "amount": 1000000, "成交额": 100, "open": 10, "high": 11, "low": 9,
            "previous_close": 10, "provider_as_of": stamp.isoformat() if stamp else None}


@pytest.mark.parametrize("failure", ["missing", "stale", "no_time", "invalid", "unavailable"])
def test_paid_primary_only_supplements_unusable_stocks_and_retains_row_provenance(failure):
    class Router:
        calls = []
        def route_validated(self, capability, validate, **kwargs):
            self.calls.append((capability, kwargs.get("symbols")))
            if capability == "intraday_scan_universe":
                if failure == "unavailable":
                    raise RuntimeError("primary unavailable")
                rows = [row("600000.SH")]
                if failure != "missing":
                    rows.append(row("600001.SH", stamp=NOW-timedelta(minutes=5) if failure == "stale"
                                    else None if failure == "no_time" else NOW,
                                    last=12 if failure == "invalid" else 10))
                return validate(rows, "tushare"), "tushare"
            assert kwargs["symbols"] == (["600000", "600001"] if failure == "unavailable" else ["600001"])
            return validate([row(c) for c in kwargs["symbols"]], "tencent_http"), "tencent_http"
    router = Router()
    result = fetch_intraday_scan_quotes(["600000.SH", "600001.SH"], now=NOW, router=router)
    assert not result.missing_instrument_ids
    assert len(router.calls) == 2
    assert result.quote_providers["600001.SH"] == "tencent_http"
    assert result.quote_providers["600000.SH"] == ("tencent_http" if failure == "unavailable" else "tushare")
    assert all(q.amount_cny == 1000000 for q in result.quotes)


def test_complete_paid_quote_batch_never_calls_backup():
    class Router:
        def route_validated(self, capability, validate, **kwargs):
            assert capability == "intraday_scan_universe"
            # An unrelated whole-market row is filtered before the scan.
            return validate([row("600000.SH"), row("300001.SZ")], "tushare"), "tushare"
    result = fetch_intraday_scan_quotes(["600000.SH"], now=NOW, router=Router())
    assert result.metadata.provider == "tushare"
    assert len(result.quotes) == 1
    assert not result.missing_instrument_ids


def test_both_sources_stale_never_become_accepted_quotes():
    class Router:
        def route_validated(self, capability, validate, **kwargs):
            return validate([row("600000.SH", stamp=NOW-timedelta(minutes=5))], "tushare"), "tushare"
    result = fetch_intraday_scan_quotes(["600000.SH"], now=NOW, router=Router())
    assert not result.quotes
    assert result.missing_instrument_ids == ("600000.SH",)
    assert result.metadata.quality == "degraded"


def test_midday_snapshot_gate_keeps_morning_close_and_rejects_older_or_afternoon_reuse():
    noon = NOW.replace(hour=12)
    morning_close = NOW.replace(hour=11, minute=30)
    assert quote_time_is_current(morning_close, noon)
    assert not quote_time_is_current(morning_close-timedelta(minutes=2), noon)
    assert not quote_time_is_current(morning_close, NOW.replace(hour=13))
    assert not quote_time_is_current(noon+timedelta(seconds=1), noon)
