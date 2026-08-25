"""行情看板的数据标准化与交易时段测试。"""

from datetime import datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from tradex.dashboard import __main__ as dashboard_app
from tradex.dashboard.__main__ import (
    _build_market_turnover,
    _market_payload_is_current,
    _market_state,
    _normalise_indices,
    _normalise_participation_indices,
)


def test_get_html_reflects_file_changes(tmp_path, monkeypatch):
    watch_path = tmp_path / "watch"
    watch_path.mkdir()
    html_path = watch_path / "index.html"
    monkeypatch.setattr(dashboard_app, "__file__", str(tmp_path / "__main__.py"))

    html_path.write_text("version-1", encoding="utf-8")
    assert dashboard_app._get_html() == "version-1"

    html_path.write_text("version-2", encoding="utf-8")
    assert dashboard_app._get_html() == "version-2"


def test_html_response_disables_browser_cache(monkeypatch):
    body = "<main>当前版本</main>"
    statuses = []
    headers = []
    handler = dashboard_app.DashboardHandler.__new__(dashboard_app.DashboardHandler)
    handler.wfile = BytesIO()
    handler.send_response = statuses.append
    handler.send_header = lambda name, value: headers.append((name, value))
    handler.end_headers = lambda: None
    monkeypatch.setattr(dashboard_app, "_get_html", lambda: body)

    handler._handle_html()

    assert statuses == [200]
    assert ("Content-Type", "text/html; charset=utf-8") in headers
    assert ("Cache-Control", "no-store") in headers
    assert ("Content-Length", str(len(body.encode("utf-8")))) in headers
    assert handler.wfile.getvalue() == body.encode("utf-8")


def test_legacy_dashboard_entrypoint_is_removed():
    dashboard_dir = Path(dashboard_app.__file__).parent

    assert not (dashboard_dir / "index.html").exists()
    assert (dashboard_dir / "watch" / "index.html").is_file()


def test_normalise_indices_supports_primary_source_fields():
    records = [
        {
            "代码": "sh000001",
            "名称": "上证指数",
            "最新价": 3990.30,
            "涨跌额": 7.65,
            "涨跌幅": 0.19,
            "昨收": 3982.65,
            "今开": 3979.49,
            "最高": 3994.18,
            "最低": 3955.60,
            "成交额": 1135187666395,
        },
        {
            "代码": "sz399001",
            "名称": "深证成指",
            "最新价": 14622.50,
            "涨跌额": -81.77,
            "涨跌幅": -0.56,
            "昨收": 14704.27,
        },
    ]

    result = _normalise_indices(records, "akshare")

    assert [item["code"] for item in result] == [
        "sh000001",
        "sz399001",
        "sh000300",
        "sh000852",
        "sz399006",
    ]
    assert result[0]["price"] == 3990.30
    assert result[0]["amount"] == 1135187666395
    assert result[1]["change_pct"] == -0.56


def test_normalise_indices_derives_previous_close_and_converts_tencent_amount():
    records = [
        {
            "指数名称": "上证指数",
            "最新点位": 3990.30,
            "涨跌额": 7.65,
            "涨跌幅": 0.19,
            "成交额(元)": 113518767,
        },
        {
            "指数名称": "深证成指",
            "最新点位": 14622.50,
            "涨跌额": -81.77,
            "涨跌幅": -0.56,
            "成交额(元)": 126558702,
        },
    ]

    result = _normalise_indices(records, "tencent_http")

    assert result[0]["previous_close"] == 3982.65
    assert result[0]["amount"] == 1135187670000
    assert result[1]["amount"] == 1265587020000


def test_normalise_participation_indices_keeps_growth_indices():
    records = [
        {"代码": "sh000001", "名称": "上证指数", "涨跌幅": 0.2},
        {"代码": "sz399006", "名称": "创业板指", "涨跌幅": 1.4},
        {"代码": "missing", "名称": "无涨跌数据"},
    ]

    assert _normalise_participation_indices(records) == [
        {"名称": "上证指数", "代码": "sh000001", "涨跌幅": 0.2, "更新时间": None},
        {"名称": "创业板指", "代码": "sz399006", "涨跌幅": 1.4, "更新时间": None},
    ]


def test_market_state_covers_open_and_midday_break():
    china = ZoneInfo("Asia/Shanghai")

    assert _market_state(datetime(2026, 8, 18, 10, 30, tzinfo=china)) == {
        "label": "盘中交易",
        "is_open": True,
    }
    assert _market_state(datetime(2026, 8, 18, 12, 0, tzinfo=china)) == {
        "label": "午间休市",
        "is_open": False,
    }


def test_market_payload_requires_current_provider_or_turnover_date():
    china = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 8, 19, 10, 30, tzinfo=china)

    assert _market_payload_is_current(
        {"provider_as_of": "2026-08-19T10:29:00+08:00"}, now
    )
    assert not _market_payload_is_current(
        {"provider_as_of": "2026-08-18T15:00:00+08:00"}, now
    )
    assert _market_payload_is_current(
        {"market_turnover": {"available": True, "today_date": "2026-08-19"}},
        now,
    )
    assert not _market_payload_is_current(
        {"provider_as_of": "not-a-timestamp", "market_turnover": {
            "available": True,
            "today_date": "2026-08-19",
        }},
        now,
    )


def test_market_turnover_sums_all_a_market_at_the_same_minute():
    china = ZoneInfo("Asia/Shanghai")
    series_by_code = {
        "sh000001": [
            {"date": "2026-08-18", "time": "09:30", "amount": 8},
            {"date": "2026-08-18", "time": "09:31", "amount": 15},
            {"date": "2026-08-18", "time": "09:32", "amount": 99},
            {"date": "2026-08-19", "time": "09:30", "amount": 10},
            {"date": "2026-08-19", "time": "09:31", "amount": 20},
        ],
        "sz399001": [
            {"date": "2026-08-18", "time": "09:30", "amount": 4},
            {"date": "2026-08-18", "time": "09:31", "amount": 5},
            {"date": "2026-08-19", "time": "09:30", "amount": 5},
            {"date": "2026-08-19", "time": "09:31", "amount": 7},
            {"date": "2026-08-19", "time": "09:32", "amount": 100},
        ],
    }

    result = _build_market_turnover(
        series_by_code,
        datetime(2026, 8, 19, 14, 30, tzinfo=china),
    )

    assert result["scope"] == "all_a_shares"
    assert result["metric"] == "amount"
    assert result["as_of"] == "09:31"
    assert result["today_amount"] == 42
    assert result["previous_same_time_amount"] == 32
    assert result["difference"] == 10
    assert result["direction"] == "expand"
    assert result["label"] == "放量"
    assert "change_pct" not in result


def test_market_turnover_reports_negative_difference_as_contraction():
    china = ZoneInfo("Asia/Shanghai")
    series_by_code = {
        "sh000001": [
            {"date": "2026-08-18", "time": "10:00", "amount": 20},
            {"date": "2026-08-19", "time": "10:00", "amount": 10},
        ],
        "sz399001": [
            {"date": "2026-08-18", "time": "10:00", "amount": 10},
            {"date": "2026-08-19", "time": "10:00", "amount": 5},
        ],
    }

    result = _build_market_turnover(
        series_by_code,
        datetime(2026, 8, 19, 10, 30, tzinfo=china),
    )

    assert result["direction"] == "shrink"
    assert result["label"] == "缩量"
    assert result["difference"] == -15


def test_market_turnover_waits_for_today_instead_of_falling_back_to_volume():
    china = ZoneInfo("Asia/Shanghai")
    series_by_code = {
        "sh000001": [{"date": "2026-08-18", "time": "15:00", "amount": 20}],
        "sz399001": [{"date": "2026-08-18", "time": "15:00", "amount": 10}],
    }

    result = _build_market_turnover(
        series_by_code,
        datetime(2026, 8, 19, 10, 30, tzinfo=china),
    )

    assert result == {"available": False, "reason": "今日尚无成交额"}
