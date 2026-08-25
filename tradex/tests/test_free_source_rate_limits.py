from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from astock_signals.smart_router import SourceBusyError
from tradex.data_sources import (
    akshare_fetchers,
    http_fetchers,
    news_fetchers,
    ths_fetchers,
    wencai_fetchers,
)


@pytest.fixture(autouse=True)
def _isolated_shared_state(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "TRADEX_RATE_LIMIT_STATE_FILE",
        str(tmp_path / "provider-rate-limits.sqlite3"),
    )


def test_akshare_free_bucket_fails_over_instead_of_bursting(monkeypatch):
    fake_akshare = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)
    monkeypatch.setenv("AKSHARE_RATE_LIMIT_INTERVAL", "1")
    monkeypatch.setenv("AKSHARE_MAX_QUEUE_WAIT", "0")

    assert akshare_fetchers._ak() is fake_akshare
    with pytest.raises(SourceBusyError, match="AKShare request queue is busy"):
        akshare_fetchers._ak()


def test_tencent_free_bucket_fails_over_before_second_http_call(monkeypatch):
    calls = []
    sentinel = object()
    monkeypatch.setenv("TENCENT_RATE_LIMIT_INTERVAL", "1")
    monkeypatch.setenv("TENCENT_MAX_QUEUE_WAIT", "0")
    monkeypatch.setattr(
        http_fetchers._NO_PROXY_OPENER,
        "open",
        lambda *args, **kwargs: calls.append((args, kwargs)) or sentinel,
    )

    assert http_fetchers._urlopen_no_proxy("https://qt.gtimg.cn/q=sh000001") is sentinel
    with pytest.raises(SourceBusyError, match="Tencent request queue is busy"):
        http_fetchers._urlopen_no_proxy("https://qt.gtimg.cn/q=sz399001")
    assert len(calls) == 1


def test_ths_free_bucket_fails_over_before_second_http_call(monkeypatch):
    calls = []
    sentinel = object()
    monkeypatch.setenv("THS_FREE_RATE_LIMIT_INTERVAL", "1")
    monkeypatch.setenv("THS_FREE_MAX_QUEUE_WAIT", "0")
    monkeypatch.setattr(
        ths_fetchers,
        "_session",
        lambda: SimpleNamespace(
            get=lambda *args, **kwargs: calls.append((args, kwargs)) or sentinel
        ),
    )

    assert ths_fetchers._get("https://data.10jqka.com.cn/first") is sentinel
    with pytest.raises(SourceBusyError, match="THS free request queue is busy"):
        ths_fetchers._get("https://data.10jqka.com.cn/second")
    assert len(calls) == 1


def test_same_free_site_bucket_is_shared_across_fetcher_modules(monkeypatch):
    monkeypatch.setenv("THS_FREE_RATE_LIMIT_INTERVAL", "1")
    monkeypatch.setenv("THS_FREE_MAX_QUEUE_WAIT", "0")
    monkeypatch.setattr(
        ths_fetchers,
        "_session",
        lambda: SimpleNamespace(get=lambda *args, **kwargs: object()),
    )

    ths_fetchers._get("https://data.10jqka.com.cn/first")
    with pytest.raises(SourceBusyError, match="free:ths:web request queue is busy"):
        akshare_fetchers._wait_for_free_source(
            "free:ths:web",
            "THS_FREE_RATE_LIMIT_INTERVAL",
            0.5,
            "THS_FREE_MAX_QUEUE_WAIT",
            4.0,
        )


def test_cls_free_bucket_stops_second_transport_call(monkeypatch):
    calls = []
    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"data": {"roll_data": []}},
    )
    monkeypatch.setenv("CLS_FREE_RATE_LIMIT_INTERVAL", "1")
    monkeypatch.setenv("CLS_FREE_MAX_QUEUE_WAIT", "0")
    monkeypatch.setattr(
        news_fetchers.curl_requests,
        "get",
        lambda *args, **kwargs: calls.append((args, kwargs)) or response,
    )

    assert news_fetchers.fetch_cls_telegraph().empty
    assert news_fetchers.fetch_cls_telegraph().empty
    assert len(calls) == 1


def test_iwencai_free_bucket_stops_second_library_call(monkeypatch):
    calls = []
    fake_pywencai = SimpleNamespace(
        get=lambda **kwargs: calls.append(kwargs) or None
    )
    monkeypatch.setitem(sys.modules, "pywencai", fake_pywencai)
    monkeypatch.setenv("IWENCAI_FREE_RATE_LIMIT_INTERVAL", "1")
    monkeypatch.setenv("IWENCAI_FREE_MAX_QUEUE_WAIT", "0")

    assert wencai_fetchers.fetch_wencai_query("测试查询").empty
    assert wencai_fetchers.fetch_wencai_query("测试查询").empty
    assert len(calls) == 1
