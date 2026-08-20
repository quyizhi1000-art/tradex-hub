from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from astock_signals.smart_router import SmartRouter
from tradex.data_gateway.contracts import QualityStatus
from tradex.data_gateway.limit_events import (
    fetch_limit_up_events,
    limit_event_series_to_component_metadata,
    limit_event_series_to_legacy_records,
)


_NOW = datetime(2026, 8, 19, 10, 31, tzinfo=ZoneInfo("Asia/Shanghai"))
_TRADE_STATUS = {"id": "closed", "name": "已收盘"}


def _frame(
    *,
    data_date: str = "20260819",
    reason: str = "复合肥+煤化工",
    board_label: object = "3天3板",
    trade_status: dict | None = None,
) -> pd.DataFrame:
    status = trade_status or _TRADE_STATUS
    frame = pd.DataFrame(
        [
            {
                "代码": "603395",
                "名称": "红四方",
                "价格": 12.34,
                "涨幅%": 10.01,
                "涨停原因": reason,
                "板型": "换手板",
                "封板成功率": 0.95,
                "炸板次数": 1,
                "封单额": 12_345_678,
                "连板": board_label,
                "首封时间": "09:31:02",
                "是否回封": 1,
                "数据日期": data_date,
                "交易状态": status,
            }
        ]
    )
    unknown = 0 if board_label == "3天3板" else 1
    frame.attrs.update(
        {
            "data_date": data_date,
            "trade_status": status,
            "pool_total": 1,
            "unique_total": 1,
            "reason_coverage": 1.0,
            "board_count_coverage": 1.0 - unknown,
            "unknown_board_count": unknown,
            "page_count": 1,
            "source_valid": True,
            "valid_empty": False,
        }
    )
    return frame


def test_limit_events_use_canonical_contract_and_preserve_dashboard_payload() -> None:
    router = SmartRouter()
    router.register("limit_events", "ths", lambda date: _frame(), priority=1)

    series = fetch_limit_up_events(
        "2026-08-19",
        router=router,
        now=_NOW,
    )

    assert series.metadata.contract == "limit_event.v1"
    assert series.metadata.provider == "ths"
    assert series.metadata.quality is QualityStatus.DEGRADED
    assert series.metadata.quality_flags == ("provider_timestamp_missing",)
    assert series.trade_status.code == "closed"
    assert series.events[0].instrument_id == "603395.SH"
    assert series.events[0].seal_success_pct == 95.0
    assert series.events[0].board_count == 3
    assert limit_event_series_to_legacy_records(series) == [
        {
            "代码": "603395",
            "名称": "红四方",
            "价格": 12.34,
            "涨幅%": 10.01,
            "涨停原因": "复合肥+煤化工",
            "板型": "换手板",
            "封板成功率": 0.95,
            "炸板次数": 1,
            "封单额": 12_345_678.0,
            "连板": "3天3板",
            "首封时间": "09:31:02",
            "是否回封": 1,
            "数据日期": "20260819",
            "交易状态": {"id": "closed", "name": "已收盘"},
        }
    ]
    assert limit_event_series_to_component_metadata(series) == {
        "contract": "limit_event.v1",
        "schema_version": 1,
        "provider_request_id": None,
        "provider_as_of": None,
        "quality": "degraded",
        "quality_flags": ["provider_timestamp_missing"],
        "partial": True,
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
    }


def test_valid_empty_limit_event_pool_is_not_a_provider_failure() -> None:
    frame = pd.DataFrame()
    frame.attrs.update(
        {
            "data_date": "20260819",
            "trade_status": {"id": 3, "name": "交易中"},
            "pool_total": 0,
            "unique_total": 0,
            "reason_coverage": 1.0,
            "board_count_coverage": 1.0,
            "unknown_board_count": 0,
            "page_count": 0,
            "source_valid": True,
            "valid_empty": True,
        }
    )
    router = SmartRouter()
    router.register("limit_events", "ths", lambda date: frame, priority=1)

    series = fetch_limit_up_events("2026-08-19", router=router, now=_NOW)

    assert series.events == ()
    assert series.valid_empty is True
    assert series.trade_status.code == "trading"
    assert limit_event_series_to_legacy_records(series) == []


def test_unknown_board_count_is_explicitly_degraded() -> None:
    router = SmartRouter()
    router.register(
        "limit_events",
        "ths",
        lambda date: _frame(board_label=None),
        priority=1,
    )

    series = fetch_limit_up_events("2026-08-19", router=router, now=_NOW)

    assert set(series.metadata.quality_flags) == {
        "board_count_partial",
        "provider_timestamp_missing",
    }
    assert series.unknown_board_count == 1
    assert series.board_count_coverage == 0.0


def test_optional_dataframe_na_is_degraded_instead_of_rejecting_the_source() -> None:
    frame = _frame()
    frame.loc[0, "封单额"] = pd.NA
    router = SmartRouter()
    router.register("limit_events", "ths", lambda date: frame, priority=1)

    series = fetch_limit_up_events("2026-08-19", router=router, now=_NOW)

    assert series.events[0].order_amount_cny is None
    assert "event_details_partial" in series.metadata.quality_flags


def test_preopen_label_overrides_provider_status_id_that_normally_means_trading() -> None:
    router = SmartRouter()
    router.register(
        "limit_events",
        "ths",
        lambda date: _frame(trade_status={"id": 3, "name": "集合竞价"}),
        priority=1,
    )

    series = fetch_limit_up_events("2026-08-19", router=router, now=_NOW)

    assert series.trade_status.code == "pre_open"
    assert series.trade_status.eligible_for_scoring is False


def test_wrong_trade_date_is_rejected_inside_route_and_falls_back() -> None:
    router = SmartRouter()
    router.register(
        "limit_events",
        "bad_primary",
        lambda date: _frame(data_date="20260818"),
        priority=1,
    )
    router.register(
        "limit_events",
        "ths",
        lambda date: _frame(),
        priority=2,
    )

    series = fetch_limit_up_events("2026-08-19", router=router, now=_NOW)

    assert series.metadata.provider == "ths"
    health = {item["source"]: item for item in router.get_health_report()}
    assert health["limit_events:bad_primary"]["fail_count"] == 1


def test_missing_required_reason_is_rejected_before_provider_success() -> None:
    router = SmartRouter()
    router.register(
        "limit_events",
        "bad_primary",
        lambda date: _frame(reason=""),
        priority=1,
    )
    router.register(
        "limit_events",
        "ths",
        lambda date: _frame(),
        priority=2,
    )

    series = fetch_limit_up_events("2026-08-19", router=router, now=_NOW)

    assert series.metadata.provider == "ths"
