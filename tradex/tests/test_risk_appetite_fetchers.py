from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from astock_signals import anti_ban_client
from tradex.data_sources import akshare_fetchers, em_client, http_fetchers


class _FakeResponse:
    def __init__(self, *, payload=None, body: bytes = b""):
        self._payload = payload
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload

    def read(self):
        return self._body


def _quote_line(
    prefix: str,
    code: str,
    name: str,
    price: str,
    pct: str,
    provider_time: str = "",
) -> str:
    fields = [""] * 50
    fields[1] = name
    fields[2] = code
    fields[3] = price
    fields[30] = provider_time
    fields[32] = pct
    fields[37] = "123456.7"
    fields[44] = "987.65"
    return f'v_{prefix}{code}="{"~".join(fields)}";'


@pytest.mark.parametrize(
    ("board_type", "expected_filter"),
    [("industry", "m:90 t:2 f:!50"), ("concept", "m:90 t:3 f:!50")],
)
def test_fetch_industry_quotes_maps_board_type_and_fields(
    monkeypatch, board_type, expected_filter
):
    calls = []
    provider_time = datetime(
        2026, 8, 19, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    payload = {
        "data": {
            "diff": [
                {
                    "f12": "BK0475",
                    "f14": "银行",
                    "f2": 1234.56,
                    "f3": 1.25,
                    "f6": 987654321.0,
                    "f62": 12345678.0,
                    "f184": 3.75,
                    "f204": 7,
                    "f104": 31,
                    "f105": 8,
                    "f128": "招商银行",
                    "f140": "600036",
                    "f141": 1,
                    "f136": 2.4,
                    "f124": int(provider_time.timestamp()),
                }
            ]
        }
    }

    def fake_em_get(url, params, timeout):
        calls.append((url, params, timeout))
        return _FakeResponse(payload=payload)

    monkeypatch.setattr(em_client, "em_get", fake_em_get)

    result = http_fetchers.fetch_industry_quotes(board_type=board_type)

    assert len(calls) == 1
    assert calls[0][1]["fs"] == expected_filter
    assert calls[0][1]["fields"] == (
        "f12,f14,f2,f3,f6,f62,f184,f204,f104,f105,"
        "f128,f140,f141,f136,f124"
    )
    assert result.to_dict("records") == [
        {
            "板块代码": "BK0475",
            "最新点位": 1234.56,
            "板块名称": "银行",
            "涨跌幅": 1.25,
            "成交额": 987654321.0,
            "主力净流入": 12345678.0,
            "主力净流入-占比": 3.75,
            "主力净流入排名": 7,
            "上涨家数": 31,
            "下跌家数": 8,
            "领涨股代码": "600036",
            "领涨股市场": 1,
            "领涨股票": "招商银行",
            "领涨股涨幅": 2.4,
            "更新时间": "2026-08-19T10:30:00+08:00",
            "source": "push2",
        }
    ]


def test_fetch_industry_quotes_exact_uses_equality(monkeypatch):
    payload = {
        "data": {
            "diff": [
                {"f12": "1", "f14": "银行", "f2": 10, "f3": 1},
                {"f12": "2", "f14": "银行概念", "f2": 20, "f3": 2},
                {"f12": "3", "f14": "证券", "f2": 30, "f3": 3},
            ]
        }
    }
    monkeypatch.setattr(
        em_client,
        "em_get",
        lambda *args, **kwargs: _FakeResponse(payload=payload),
    )

    result = http_fetchers.fetch_industry_quotes(exact=["银行", "证券"])

    assert result["板块名称"].tolist() == ["证券", "银行"]
    assert "银行概念" not in result["板块名称"].tolist()
    optional_columns = [
        "成交额", "主力净流入", "主力净流入-占比", "主力净流入排名",
        "领涨股代码", "领涨股市场", "领涨股票", "领涨股涨幅", "更新时间",
    ]
    for record in result.to_dict("records"):
        assert all(record[column] is None for column in optional_columns)


def test_fetch_industry_quotes_preserves_real_zero_fund_values(monkeypatch):
    payload = {
        "data": {
            "diff": [{
                "f12": "BK0475",
                "f14": "银行",
                "f2": 1000,
                "f3": 0,
                "f6": 0,
                "f62": 0,
                "f184": 0,
                "f204": 0,
            }]
        }
    }
    calls = []

    def fake_em_get(url, params, timeout):
        calls.append((url, params, timeout))
        return _FakeResponse(payload=payload)

    monkeypatch.setattr(em_client, "em_get", fake_em_get)

    record = http_fetchers.fetch_industry_quotes().to_dict("records")[0]

    assert len(calls) == 1
    assert record["成交额"] == 0
    assert record["主力净流入"] == 0
    assert record["主力净流入-占比"] == 0
    assert record["主力净流入排名"] == 0
    assert record["更新时间"] is None
    assert record["source"] == "push2"


def test_fetch_industry_quotes_marks_the_actual_delay_source(monkeypatch):
    calls = []

    def fake_em_get(url, params, timeout):
        calls.append(url)
        if "push2delay" not in url:
            raise RuntimeError("primary unavailable")
        return _FakeResponse(payload={
            "data": {"diff": [{
                "f12": "BK0475",
                "f14": "银行",
                "f2": 1000,
                "f3": 1,
                "f128": "招商银行",
                "f140": "600036",
                "f141": 1,
                "f136": 2.4,
            }]},
        })

    monkeypatch.setattr(em_client, "em_get", fake_em_get)

    record = http_fetchers.fetch_industry_quotes().to_dict("records")[0]

    assert len(calls) == 2
    assert record["source"] == "push2delay"
    assert record["领涨股代码"] == "600036"


def test_fetch_board_leaders_uses_a_bounded_first_page_and_preserves_missing(monkeypatch):
    provider_time = datetime(
        2026, 8, 19, 10, 35, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    calls = []
    payload = {
        "data": {
            "diff": [
                {
                    "f12": "600001",
                    "f14": "领涨一号",
                    "f2": 12.3,
                    "f3": 9.9,
                    "f22": 1.2,
                    "f6": 800_000_000,
                    "f8": 7.1,
                    "f62": 50_000_000,
                    "f184": 6.2,
                    "f124": int(provider_time.timestamp()),
                },
                {"f12": "600002", "f14": "领涨二号", "f3": 6.5},
            ],
        },
    }

    def fake_em_get(url, params, timeout):
        calls.append((url, params, timeout))
        return _FakeResponse(payload=payload)

    monkeypatch.setattr(em_client, "em_get", fake_em_get)

    result = http_fetchers.fetch_board_leaders("bk1036", limit=2)
    records = result.to_dict("records")

    assert len(calls) == 1
    assert calls[0][1]["pn"] == "1"
    assert calls[0][1]["pz"] == "2"
    assert calls[0][1]["po"] == "1"
    assert calls[0][1]["fid"] == "f22"
    assert calls[0][1]["fs"] == "b:BK1036 f:!50"
    assert calls[0][1]["fields"] == "f12,f14,f2,f3,f22,f6,f8,f62,f184,f124"
    assert records[0] == {
        "code": "600001",
        "name": "领涨一号",
        "price": 12.3,
        "change_pct": 9.9,
        "speed_pct": 1.2,
        "amount": 800_000_000.0,
        "turnover": 7.1,
        "flow_amount": 50_000_000.0,
        "flow_ratio": 6.2,
        "provider_as_of": "2026-08-19T10:35:00+08:00",
        "source": "push2",
    }
    assert records[1]["price"] is None
    assert records[1]["speed_pct"] is None
    assert records[1]["amount"] is None
    assert records[1]["provider_as_of"] is None
    assert result.attrs["source"] == "push2"


def test_fetch_board_leaders_can_request_falling_speed_order(monkeypatch):
    calls = []

    def fake_em_get(url, params, timeout):
        calls.append((url, params, timeout))
        return _FakeResponse(payload={
            "data": {
                "diff": [{
                    "f12": "600001",
                    "f14": "下跌样本",
                    "f2": 10.0,
                    "f3": -2.0,
                    "f22": -1.0,
                    "f6": 1000,
                    "f8": 1.0,
                    "f62": -100,
                    "f184": -1.0,
                    "f124": 1787796000,
                }]
            }
        })

    monkeypatch.setattr(em_client, "em_get", fake_em_get)

    result = http_fetchers.fetch_board_leaders(
        "BK1036",
        limit=1,
        speed_order="asc",
    )

    assert calls[0][1]["po"] == "0"
    assert result.iloc[0]["speed_pct"] == -1.0


def test_fetch_board_leaders_falls_back_to_delay_and_validates_bounds(monkeypatch):
    calls = []

    def fake_em_get(url, params, timeout):
        calls.append(url)
        if "push2delay" not in url:
            raise RuntimeError("primary unavailable")
        return _FakeResponse(payload={
            "data": {"diff": [{"f12": "600001", "f14": "样本", "f3": 1.0}]},
        })

    monkeypatch.setattr(em_client, "em_get", fake_em_get)

    record = http_fetchers.fetch_board_leaders("BK1036").to_dict("records")[0]

    assert len(calls) == 2
    assert record["source"] == "push2delay"
    with pytest.raises(ValueError, match="board_code"):
        http_fetchers.fetch_board_leaders("1036")
    with pytest.raises(ValueError, match="limit"):
        http_fetchers.fetch_board_leaders("BK1036", limit=11)


def test_fetch_board_leaders_reuses_the_known_board_quote_source(monkeypatch):
    calls = []

    def fake_em_get(url, params, timeout):
        calls.append(url)
        return _FakeResponse(payload={
            "data": {"diff": [{"f12": "600001", "f14": "样本", "f3": 1.0}]},
        })

    monkeypatch.setattr(em_client, "em_get", fake_em_get)

    result = http_fetchers.fetch_board_leaders(
        "BK1036",
        source_hint="push2delay",
    )

    assert calls == ["https://push2delay.eastmoney.com/api/qt/clist/get"]
    assert result.attrs["source"] == "push2delay"


def test_fetch_stock_sector_profiles_batches_dedupes_and_preserves_order(monkeypatch):
    provider_time = datetime(
        2026, 8, 19, 10, 35, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    calls = []
    payload = {
        "rc": 0,
        "data": {
            "total": 3,
            # Provider order is deliberately different from request order.
            "diff": [
                {
                    "f12": "920128",
                    "f14": "胜业电气",
                    "f100": "元件",
                    "f102": "广东板块",
                    "f103": "光伏概念,风能",
                    "f124": int(provider_time.timestamp()),
                },
                {
                    "f12": "002948",
                    "f14": "青岛银行",
                    "f100": "银行Ⅱ",
                    "f102": "山东板块",
                    "f103": "互联网金融，跨境支付,互联网金融",
                    "f124": int(provider_time.timestamp()),
                },
                {
                    "f12": "600928",
                    "f14": "西安银行",
                    "f100": "银行Ⅱ",
                    "f102": "陕西板块",
                    "f103": "移动支付,互联网金融",
                    "f124": int(provider_time.timestamp()),
                },
            ],
        },
    }

    def fake_em_get(url, params, timeout):
        calls.append((url, params, timeout))
        return _FakeResponse(payload=payload)

    monkeypatch.setattr(em_client, "em_get", fake_em_get)

    result = http_fetchers.fetch_stock_sector_profiles(
        ["600928", "002948", "600928", "920128"]
    )

    assert len(calls) == 1
    assert calls[0][0] == "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
    assert calls[0][1]["secids"] == "1.600928,0.002948,0.920128"
    assert calls[0][1]["fields"] == "f12,f14,f100,f102,f103,f124"
    assert calls[0][2] == 15
    assert result["代码"].tolist() == ["600928", "002948", "920128"]
    assert result.loc[0, "概念标签"] == ["移动支付", "互联网金融"]
    assert result.loc[1, "概念标签"] == ["互联网金融", "跨境支付"]
    assert result.loc[2, "provider_as_of"] == "2026-08-19T10:35:00+08:00"
    assert set(result.columns) == {
        "代码", "名称", "行业", "地域", "概念标签", "provider_as_of", "source"
    }
    assert result.attrs == {
        "source": "push2delay",
        "profile_total": 3,
        "source_valid": True,
    }


def test_fetch_stock_sector_profiles_rejects_a_missing_returned_code(monkeypatch):
    monkeypatch.setattr(
        em_client,
        "em_get",
        lambda *args, **kwargs: _FakeResponse(payload={
            "rc": 0,
            "data": {
                "total": 2,
                "diff": [{
                    "f12": "600928",
                    "f14": "西安银行",
                    "f100": "银行Ⅱ",
                    "f102": "陕西板块",
                    "f103": "互联网金融",
                }],
            },
        }),
    )

    with pytest.raises(RuntimeError, match="incomplete code set"):
        http_fetchers.fetch_stock_sector_profiles(["600928", "002948"])


def test_fetch_stock_sector_profiles_rejects_duplicate_returned_codes(monkeypatch):
    row = {
        "f12": "600928",
        "f14": "西安银行",
        "f100": "银行Ⅱ",
        "f102": "陕西板块",
        "f103": "互联网金融",
    }
    monkeypatch.setattr(
        em_client,
        "em_get",
        lambda *args, **kwargs: _FakeResponse(payload={
            "rc": 0,
            "data": {"total": 2, "diff": [row, dict(row)]},
        }),
    )

    with pytest.raises(RuntimeError, match="duplicate code"):
        http_fetchers.fetch_stock_sector_profiles(["600928", "002948"])


def test_fetch_stock_sector_profiles_requires_nonempty_industry(monkeypatch):
    monkeypatch.setattr(
        em_client,
        "em_get",
        lambda *args, **kwargs: _FakeResponse(payload={
            "rc": 0,
            "data": {
                "total": 1,
                "diff": [{
                    "f12": "600928",
                    "f14": "西安银行",
                    "f100": "-",
                    "f102": "陕西板块",
                    "f103": "互联网金融",
                }],
            },
        }),
    )

    with pytest.raises(RuntimeError, match="code, name, and industry"):
        http_fetchers.fetch_stock_sector_profiles(["600928"])


@pytest.mark.parametrize("codes", [[], [""], ["60092"], ["712345"]])
def test_fetch_stock_sector_profiles_rejects_invalid_codes_without_request(
    monkeypatch, codes
):
    calls = []
    monkeypatch.setattr(em_client, "em_get", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(ValueError):
        http_fetchers.fetch_stock_sector_profiles(codes)

    assert calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"rc": -1, "data": None},
        {"data": {"total": 1, "diff": []}},
        {"rc": False, "data": {"total": 1, "diff": []}},
        {"rc": 0},
        {"rc": 0, "data": {"total": 1}},
        {"rc": 0, "data": {"total": 1, "diff": None}},
    ],
)
def test_fetch_stock_sector_profiles_rejects_invalid_response_shape(
    monkeypatch, payload
):
    monkeypatch.setattr(
        em_client,
        "em_get",
        lambda *args, **kwargs: _FakeResponse(payload=payload),
    )

    with pytest.raises(RuntimeError):
        http_fetchers.fetch_stock_sector_profiles(["600928"])


def test_em_get_uses_worker_local_sessions_without_serialising_network(monkeypatch):
    guard = threading.Lock()
    barrier = threading.Barrier(2)
    active = 0
    max_active = 0
    session_ids = set()

    class FakeSession:
        def __init__(self):
            self.session_id = id(self)

        def get(self, *args, **kwargs):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
                session_ids.add(self.session_id)
            barrier.wait(timeout=1)
            with guard:
                active -= 1
            return _FakeResponse(payload={})

    monkeypatch.setattr(em_client._rq, "Session", FakeSession)
    monkeypatch.setattr(em_client, "_EM_THREAD_LOCAL", threading.local())
    monkeypatch.setattr(anti_ban_client, "_em_next_slot", [0.0])
    monkeypatch.setattr(anti_ban_client, "_EM_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(anti_ban_client, "_EM_MAX_QUEUE_WAIT", 1.0)
    monkeypatch.setattr(anti_ban_client.random, "uniform", lambda *_: 0.0)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(em_client.em_get, f"https://example.test/{index}")
            for index in range(2)
        ]
        for future in futures:
            future.result(timeout=2)

    assert max_active == 2
    assert len(session_ids) == 2


def test_em_get_fails_fast_when_the_provider_queue_is_busy(
    monkeypatch, tmp_path
):
    from astock_signals.smart_router import SourceBusyError

    monkeypatch.setenv(
        "TRADEX_RATE_LIMIT_STATE_FILE",
        str(tmp_path / "provider-rate-limits.sqlite3"),
    )
    monkeypatch.setattr(anti_ban_client, "_EM_MIN_INTERVAL", 1.0)
    monkeypatch.setattr(anti_ban_client, "_EM_MAX_QUEUE_WAIT", 0.01)
    monkeypatch.setattr(anti_ban_client.random, "uniform", lambda *_: 0.0)
    anti_ban_client.reserve_em_request_slot()

    with pytest.raises(SourceBusyError, match="queue is busy"):
        em_client.em_get("https://example.test/busy")


@pytest.mark.parametrize(
    "fetch",
    [
        lambda: http_fetchers.fetch_industry_quotes(),
        lambda: http_fetchers.fetch_board_leaders("BK0475"),
    ],
)
def test_http_eastmoney_mirrors_propagate_busy_without_retry(monkeypatch, fetch):
    from astock_signals.smart_router import SourceBusyError

    calls = []

    def busy(*args, **kwargs):
        calls.append(args[0])
        raise SourceBusyError("busy-test")

    monkeypatch.setattr(em_client, "em_get", busy)

    with pytest.raises(SourceBusyError, match="busy-test"):
        fetch()
    assert len(calls) == 1


def test_akshare_fund_flow_preserves_gateway_busy(monkeypatch):
    from astock_signals.smart_router import SourceBusyError

    monkeypatch.setattr(
        em_client,
        "em_get",
        lambda *args, **kwargs: (_ for _ in ()).throw(SourceBusyError("busy-test")),
    )

    with pytest.raises(SourceBusyError, match="busy-test"):
        akshare_fetchers.fetch_fund_flow(code="000001")


def test_eastmoney_news_preserves_gateway_busy(monkeypatch):
    from astock_signals.smart_router import SourceBusyError
    from tradex.data_sources import news_fetchers

    monkeypatch.setattr(
        em_client,
        "em_get",
        lambda *args, **kwargs: (_ for _ in ()).throw(SourceBusyError("busy-test")),
    )

    with pytest.raises(SourceBusyError, match="busy-test"):
        news_fetchers.fetch_em_news_direct(symbol="000001")


def test_fetch_industry_quotes_paginates_until_reported_total(monkeypatch):
    calls = []

    def fake_em_get(url, params, timeout):
        calls.append(params["pn"])
        if params["pn"] == "1":
            items = [
                {"f12": str(index), "f14": f"样本{index}", "f2": index, "f3": index}
                for index in range(100)
            ]
        else:
            items = [{"f12": "100", "f14": "目标板块", "f2": 100, "f3": -5}]
        return _FakeResponse(payload={"data": {"total": 101, "diff": items}})

    monkeypatch.setattr(em_client, "em_get", fake_em_get)

    result = http_fetchers.fetch_industry_quotes(exact="目标板块")

    assert calls == ["1", "2"]
    assert result["板块名称"].tolist() == ["目标板块"]


def test_fetch_industry_quotes_rejects_unknown_board_type():
    with pytest.raises(ValueError, match="board_type"):
        http_fetchers.fetch_industry_quotes(board_type="theme")


def test_fetch_sector_fund_flow_normalises_provider_time(monkeypatch):
    provider_time = datetime(
        2026, 8, 19, 10, 31, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    payload = {
        "data": {
            "total": 2,
            "diff": [
                {
                    "f12": "BK0475",
                    "f14": "银行",
                    "f62": 0,
                    "f184": 0,
                    "f66": 0,
                    "f69": 0,
                    "f72": 0,
                    "f75": 0,
                    "f78": 0,
                    "f81": 0,
                    "f84": 0,
                    "f87": 0,
                    "f204": 0,
                    "f205": 0,
                    "f124": int(provider_time.timestamp()),
                },
                {"f12": "BK0473", "f14": "证券"},
            ],
        }
    }

    class FakeSession:
        def __init__(self):
            self.headers = {}

        def get(self, *args, **kwargs):
            return _FakeResponse(payload=payload)

    monkeypatch.setattr(akshare_fetchers, "_ak", lambda: object())
    monkeypatch.setattr(em_client, "em_get", FakeSession().get)

    result = akshare_fetchers.fetch_industry_data(endpoint="sector_fund_flow_rank")

    flow_columns = [
        "主力净流入", "主力净流入-占比",
        "超大单净流入", "超大单净流入-占比",
        "大单净流入", "大单净流入-占比",
        "中单净流入", "中单净流入-占比",
        "小单净流入", "小单净流入-占比",
        "主力净流入排名", "涨跌股数比",
    ]
    assert result.loc[0, "更新时间"] == "2026-08-19T10:31:00+08:00"
    assert result.loc[1, "更新时间"] is None
    assert all(result.loc[0, column] == 0 for column in flow_columns)
    assert all(result.loc[1, column] is None for column in flow_columns)


def test_fetch_market_overview_tencent_preserves_provider_times(monkeypatch):
    body = "".join(
        [
            _quote_line(
                "sh", "000001", "上证指数", "3700", "0.80", "20260819103205"
            ),
            _quote_line("sz", "399001", "深证成指", "12000", "1.10"),
        ]
    ).encode("gbk")
    monkeypatch.setattr(
        http_fetchers,
        "_urlopen_no_proxy",
        lambda *args, **kwargs: _FakeResponse(body=body),
    )

    result = http_fetchers.fetch_market_overview_tencent()

    assert result.loc[0, "更新时间"] == "2026-08-19T10:32:05+08:00"
    assert result.loc[1, "更新时间"] is None
    assert {
        "指数名称", "最新点位", "涨跌额", "涨跌幅", "成交量(手)", "成交额(元)"
    }.issubset(result.columns)


def test_fetch_realtime_quotes_tencent_batches_markets_and_preserves_order(monkeypatch):
    calls = []
    body = "".join(
        [
            _quote_line("sh", "600000", "浦发银行", "10.25", "1.20"),
            _quote_line("sz", "000001", "平安银行", "12.30", "-0.50"),
            _quote_line("bj", "920001", "北交样本", "25.80", "3.40"),
        ]
    ).encode("gbk")

    def fake_urlopen(url, timeout):
        calls.append((url, timeout))
        return _FakeResponse(body=body)

    monkeypatch.setattr(http_fetchers, "_urlopen_no_proxy", fake_urlopen)

    result = http_fetchers.fetch_realtime_quotes_tencent(
        ["600000", "000001", "920001", "600000"]
    )

    assert calls == [
        (
            "https://qt.gtimg.cn/q=sh600000,sz000001,bj920001",
            10,
        )
    ]
    assert result["代码"].tolist() == ["600000", "000001", "920001"]
    assert result.columns.tolist() == [
        "代码", "名称", "最新价", "涨跌幅", "成交额", "流通市值"
    ]
    assert result.loc[0, "名称"] == "浦发银行"
    assert result.loc[1, "涨跌幅"] == -0.5
    assert result.loc[2, "流通市值"] == 987.65


@pytest.mark.parametrize("symbols", [[], ["12345"], ["A00001"], ["500001"]])
def test_fetch_realtime_quotes_tencent_rejects_invalid_codes(symbols):
    with pytest.raises(ValueError):
        http_fetchers.fetch_realtime_quotes_tencent(symbols)
