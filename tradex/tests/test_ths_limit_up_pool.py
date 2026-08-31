from __future__ import annotations

import requests
import pytest

from tradex.data_sources import ths_fetchers


TRADE_STATUS = {
    "id": "closed",
    "name": "已收盘",
    "start_time": "15:30",
    "end_time": "23:59:59.999999999",
}

EXPECTED_COLUMNS = [
    "代码",
    "名称",
    "价格",
    "涨幅%",
    "涨停原因",
    "板型",
    "封板成功率",
    "炸板次数",
    "封单额",
    "连板",
    "首封时间",
    "是否回封",
    "数据日期",
    "交易状态",
]


class _FakeResponse:
    def __init__(self, payload=None, http_error: Exception | None = None):
        self._payload = payload
        self._http_error = http_error

    def raise_for_status(self):
        if self._http_error is not None:
            raise self._http_error

    def json(self):
        return self._payload


def _row(
    code: str,
    *,
    reason: str = "复合肥+煤化工",
    high_days: object = "3天3板",
    change_tag: str = "LIMIT_BACK",
) -> dict:
    return {
        "code": code,
        "name": f"股票{code}",
        "latest": 12.34,
        "change_rate": 10.01,
        "reason_type": reason,
        "limit_up_type": "换手板",
        "limit_up_suc_rate": 0.95,
        "open_num": 1,
        "order_amount": 12345678,
        "high_days": high_days,
        "change_tag": change_tag,
        "first_limit_up_time": None,
        "is_again_limit": 1,
    }


def _payload(
    info: list[dict],
    *,
    total: int | None = None,
    count: int = 1,
    page: int = 1,
    limit: int = 200,
    data_date: str = "20260819",
    trade_status: object = TRADE_STATUS,
) -> dict:
    return {
        "status_code": 0,
        "status_msg": "success",
        "data": {
            "page": {
                "limit": limit,
                "total": len(info) if total is None else total,
                "count": count,
                "page": page,
            },
            "info": info,
            "date": data_date,
            "trade_status": trade_status,
        },
    }


def test_success_normalizes_date_and_preserves_raw_reason_and_board_text(monkeypatch):
    calls = []

    def fake_get(url, params=None, **kwargs):
        calls.append((url, dict(params), kwargs))
        return _FakeResponse(_payload([_row("603395")]))

    monkeypatch.setattr(ths_fetchers, "_get", fake_get)

    df = ths_fetchers.fetch_ths_limit_up_pool("2026-08-19")

    assert calls[0][1]["date"] == "20260819"
    assert list(df.columns) == EXPECTED_COLUMNS
    assert df.loc[0, "涨停原因"] == "复合肥+煤化工"
    assert df.loc[0, "连板"] == "3天3板"
    assert df.loc[0, "数据日期"] == "20260819"
    assert df.loc[0, "交易状态"] == TRADE_STATUS
    assert df.attrs == {
        "data_date": "20260819",
        "trade_status": TRADE_STATUS,
        "pool_total": 1,
        "unique_total": 1,
        "reason_coverage": 1.0,
        "board_count_coverage": 1.0,
        "unknown_board_count": 0,
        "page_count": 1,
        "source_valid": True,
        "valid_empty": False,
    }


@pytest.mark.parametrize(("page_count", "page_number"), [(0, 0), (1, 1)])
def test_total_zero_and_empty_info_is_a_valid_empty_pool(
    monkeypatch, page_count, page_number
):
    # 真实接口返回 0/0；同时兼容把空集表示为一个空页的版本。
    monkeypatch.setattr(
        ths_fetchers,
        "_get",
        lambda *args, **kwargs: _FakeResponse(
            _payload([], total=0, count=page_count, page=page_number)
        ),
    )

    df = ths_fetchers.fetch_ths_limit_up_pool("20260819")

    assert df.empty
    assert list(df.columns) == EXPECTED_COLUMNS
    assert df.attrs["pool_total"] == 0
    assert df.attrs["page_count"] == page_count
    assert df.attrs["source_valid"] is True
    assert df.attrs["valid_empty"] is True
    assert df.attrs["board_count_coverage"] == 1.0
    assert df.attrs["unknown_board_count"] == 0


def test_http_error_is_not_converted_to_an_empty_pool(monkeypatch):
    monkeypatch.setattr(
        ths_fetchers,
        "_get",
        lambda *args, **kwargs: _FakeResponse(
            http_error=requests.HTTPError("503 Service Unavailable")
        ),
    )

    with pytest.raises(RuntimeError, match="HTTP"):
        ths_fetchers.fetch_ths_limit_up_pool("20260819")


def test_root_status_error_is_not_converted_to_an_empty_pool(monkeypatch):
    payload = {
        "status_code": -1,
        "status_msg": "failed",
        "data": None,
    }
    monkeypatch.setattr(
        ths_fetchers, "_get", lambda *args, **kwargs: _FakeResponse(payload)
    )

    with pytest.raises(RuntimeError, match="status_code=-1"):
        ths_fetchers.fetch_ths_limit_up_pool("20260819")


@pytest.mark.parametrize(
    "payload",
    [
        {"status_code": 0, "status_msg": "success"},
        {
            "status_code": 0,
            "status_msg": "success",
            "data": {
                "page": {"limit": 200, "total": 0, "count": 0, "page": 0},
                "date": "20260819",
                "trade_status": TRADE_STATUS,
            },
        },
    ],
    ids=["missing-data", "missing-info"],
)
def test_missing_data_or_info_is_invalid(monkeypatch, payload):
    monkeypatch.setattr(
        ths_fetchers, "_get", lambda *args, **kwargs: _FakeResponse(payload)
    )

    with pytest.raises(RuntimeError):
        ths_fetchers.fetch_ths_limit_up_pool("20260819")


def test_wrong_data_date_is_rejected(monkeypatch):
    monkeypatch.setattr(
        ths_fetchers,
        "_get",
        lambda *args, **kwargs: _FakeResponse(
            _payload([_row("603395")], data_date="20260818")
        ),
    )

    with pytest.raises(RuntimeError, match="日期不匹配"):
        ths_fetchers.fetch_ths_limit_up_pool("20260819")


def test_paginates_and_preserves_unique_records(monkeypatch):
    first_page_rows = [_row(f"{index:06d}") for index in range(200)]
    second_page_rows = [_row("000200")]
    requested_pages = []

    def fake_get(url, params=None, **kwargs):
        requested_pages.append(params["page"])
        info = first_page_rows if params["page"] == 1 else second_page_rows
        return _FakeResponse(
            _payload(info, total=201, count=2, page=params["page"])
        )

    monkeypatch.setattr(ths_fetchers, "_get", fake_get)

    df = ths_fetchers.fetch_ths_limit_up_pool("20260819")

    assert requested_pages == [1, 2]
    assert len(df) == 201
    assert df["代码"].is_unique
    assert df["代码"].tolist() == [f"{index:06d}" for index in range(201)]
    assert df.attrs["pool_total"] == 201
    assert df.attrs["unique_total"] == 201
    assert df.attrs["page_count"] == 2


def test_duplicate_code_across_pages_invalidates_the_source(monkeypatch):
    first_page_rows = [_row(f"{index:06d}") for index in range(200)]

    def fake_get(url, params=None, **kwargs):
        info = first_page_rows if params["page"] == 1 else [_row("000199")]
        return _FakeResponse(
            _payload(info, total=201, count=2, page=params["page"])
        )

    monkeypatch.setattr(ths_fetchers, "_get", fake_get)

    with pytest.raises(RuntimeError, match="重复代码"):
        ths_fetchers.fetch_ths_limit_up_pool("20260819")


def test_record_count_must_match_declared_total(monkeypatch):
    monkeypatch.setattr(
        ths_fetchers,
        "_get",
        lambda *args, **kwargs: _FakeResponse(
            _payload([_row("000001")], total=2, count=1)
        ),
    )

    with pytest.raises(RuntimeError, match="total"):
        ths_fetchers.fetch_ths_limit_up_pool("20260819")


@pytest.mark.parametrize("field", ["code", "name", "reason_type"])
def test_nonempty_pool_requires_attribution_fields(monkeypatch, field):
    row = _row("603395")
    row[field] = ""
    monkeypatch.setattr(
        ths_fetchers,
        "_get",
        lambda *args, **kwargs: _FakeResponse(_payload([row])),
    )

    with pytest.raises(RuntimeError, match=field):
        ths_fetchers.fetch_ths_limit_up_pool("20260819")


def test_live_status_keeps_stock_when_reason_is_not_yet_available(monkeypatch):
    row = _row("603395")
    row.pop("reason_type")
    row.pop("high_days")
    monkeypatch.setattr(
        ths_fetchers,
        "_get",
        lambda *args, **kwargs: _FakeResponse(_payload([row])),
    )

    frame = ths_fetchers.fetch_ths_limit_up_status("20260819")

    assert frame.loc[0, "代码"] == "603395"
    assert frame.loc[0, "名称"] == "股票603395"
    assert frame.loc[0, "涨停原因"] == ""
    assert frame.attrs["reason_coverage"] == 0.0
    assert frame.attrs["unknown_board_count"] == 1


def test_nonempty_pool_rejects_a_malformed_stock_code(monkeypatch):
    monkeypatch.setattr(
        ths_fetchers,
        "_get",
        lambda *args, **kwargs: _FakeResponse(_payload([_row("ABC123")])),
    )

    with pytest.raises(RuntimeError, match="A 股代码"):
        ths_fetchers.fetch_ths_limit_up_pool("20260819")


def test_high_days_key_is_required_even_though_its_value_may_be_missing(monkeypatch):
    row = _row("603395")
    del row["high_days"]
    monkeypatch.setattr(
        ths_fetchers,
        "_get",
        lambda *args, **kwargs: _FakeResponse(_payload([row])),
    )

    with pytest.raises(RuntimeError, match="high_days"):
        ths_fetchers.fetch_ths_limit_up_pool("20260819")


def test_missing_high_days_uses_change_tag_without_guessing_limit_back(monkeypatch):
    rows = [
        _row("000001", high_days=None, change_tag="FIRST_LIMIT"),
        _row("000002", high_days=None, change_tag="LIMIT_BACK"),
        _row("000003", high_days="", change_tag="LIMIT_BACK"),
    ]
    monkeypatch.setattr(
        ths_fetchers,
        "_get",
        lambda *args, **kwargs: _FakeResponse(_payload(rows)),
    )

    df = ths_fetchers.fetch_ths_limit_up_pool("20260819")

    assert df.loc[0, "连板"] == "首板"
    assert df["连板"].isna().iloc[1]
    assert df.loc[2, "连板"] == ""
    assert df.attrs["board_count_coverage"] == pytest.approx(1 / 3)
    assert df.attrs["unknown_board_count"] == 2


def test_nonconsecutive_high_days_is_not_counted_as_streak_coverage(monkeypatch):
    monkeypatch.setattr(
        ths_fetchers,
        "_get",
        lambda *args, **kwargs: _FakeResponse(
            _payload([_row("002418", high_days="5天4板")])
        ),
    )

    frame = ths_fetchers.fetch_ths_limit_up_status("20260819")

    assert frame.loc[0, "连板"] == "5天4板"
    assert frame.attrs["board_count_coverage"] == 0.0
    assert frame.attrs["unknown_board_count"] == 1


@pytest.mark.parametrize(
    "trade_status",
    [None, "closed", {}, {"id": "closed"}, {"name": "已收盘"}],
)
def test_trade_status_requires_structured_id_and_name(monkeypatch, trade_status):
    monkeypatch.setattr(
        ths_fetchers,
        "_get",
        lambda *args, **kwargs: _FakeResponse(
            _payload([_row("603395")], trade_status=trade_status)
        ),
    )

    with pytest.raises(RuntimeError, match="trade_status"):
        ths_fetchers.fetch_ths_limit_up_pool("20260819")
