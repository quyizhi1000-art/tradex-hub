"""Provider mapping for canonical daily limit-up events."""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time
from typing import Any

from ..contracts import LimitEventTradeStatusV1, LimitUpEventV1, LimitUpStatusV1
from .market_overview import field, parse_provider_time
from .securities import canonical_instrument_id


_DAY_BOARD_PATTERN = re.compile(r"^(?P<days>\d+)天(?P<boards>\d+)板$")
_STREAK_BOARD_PATTERN = re.compile(r"^(?P<boards>\d+)连板$")
_BOARD_PATTERN = re.compile(r"^(?P<boards>\d+)板$")


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        if bool(value != value):
            return None
    except (TypeError, ValueError):
        pass
    result = str(value).strip()
    if not result or result.lower() in {"nan", "nat", "<na>"}:
        return None
    return result


def _required_text(value: Any, label: str) -> str:
    result = _optional_text(value)
    if result is None:
        raise ValueError(f"{label} is required")
    return result


def _optional_number(value: Any, label: str) -> float | None:
    if _optional_text(value) is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _optional_integer(value: Any, label: str) -> int | None:
    if _optional_text(value) is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise ValueError(f"{label} must be an integer")
    return int(numeric)


def _optional_bool(value: Any, label: str) -> bool | None:
    if _optional_text(value) is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"0", "false", "no", "否"}:
        return False
    if text in {"1", "true", "yes", "是"}:
        return True
    raise ValueError(f"{label} must be boolean")


def _optional_time(value: Any) -> time | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        return time.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid first sealed time: {value!r}") from exc


def _eastmoney_time(value: Any) -> time | None:
    text = _optional_text(value)
    if text is None:
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) == 6:
        try:
            return time(int(digits[:2]), int(digits[2:4]), int(digits[4:]))
        except ValueError as exc:
            raise ValueError(f"invalid Eastmoney limit-up time: {value!r}") from exc
    return _optional_time(text)


def _eastmoney_board_label(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    match = re.fullmatch(r"(?P<days>\d+)\s*/\s*(?P<boards>\d+)", text)
    if match is None:
        raise ValueError(f"invalid Eastmoney limit-up statistic: {value!r}")
    days = int(match.group("days"))
    boards = int(match.group("boards"))
    if days < 1 or boards < 1 or boards > days:
        raise ValueError(f"invalid Eastmoney limit-up statistic: {value!r}")
    return f"{days}天{boards}板"


def _derived_trade_status(requested_date: date, fetched_at: datetime) -> dict[str, str]:
    if requested_date < fetched_at.date() or (
        requested_date == fetched_at.date() and fetched_at.time() >= time(15, 0)
    ):
        return {"id": "closed", "name": "已收盘"}
    if requested_date > fetched_at.date() or fetched_at.time() < time(9, 15):
        return {"id": "pre_open", "name": "盘前"}
    return {"id": "trading", "name": "交易中"}


def _normalise_date(value: Any, label: str) -> date:
    text = _required_text(value, label).replace("-", "")
    if re.fullmatch(r"\d{8}", text) is None:
        raise ValueError(f"{label} must use YYYYMMDD or YYYY-MM-DD")
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid date") from exc


def _record_code(row: dict[str, Any]) -> str:
    raw = _required_text(
        field(row, "代码", "证券代码", "code", "symbol", "ticker"),
        "limit-event instrument code",
    )
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", raw)
    if match is None:
        raise ValueError(f"invalid A-share instrument code: {raw!r}")
    return match.group(1)


def _board_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    label = "".join(str(value).split())
    if label == "首板":
        return 1
    day_match = _DAY_BOARD_PATTERN.fullmatch(label)
    if day_match is not None:
        days = int(day_match.group("days"))
        boards = int(day_match.group("boards"))
        return boards if days == boards and boards > 0 else None
    for pattern in (_STREAK_BOARD_PATTERN, _BOARD_PATTERN):
        match = pattern.fullmatch(label)
        if match is not None:
            count = int(match.group("boards"))
            return count if count > 0 else None
    return None


def _trade_status(value: Any) -> LimitEventTradeStatusV1:
    if not isinstance(value, dict):
        raise ValueError("limit-event trade_status must be an object")
    status_id = _required_text(value.get("id"), "limit-event trade_status.id")
    label = _required_text(value.get("name"), "limit-event trade_status.name")
    normalized_id = status_id.lower().replace("-", "_")
    combined = f"{normalized_id} {label.lower()}"

    if any(
        token in combined
        for token in (
            "盘前",
            "未开盘",
            "集合竞价",
            "pre_open",
            "preopen",
            "auction",
            "not_started",
        )
    ):
        code = "pre_open"
    elif any(
        token in combined
        for token in ("休市", "停盘", "未交易", "closed_day", "non_trading")
    ):
        code = "non_trading"
    elif normalized_id in {"closed", "close"} or any(
        token in combined for token in ("已收盘", "交易结束", "盘后")
    ):
        code = "closed"
    elif normalized_id in {"3", "open", "trade", "trading"} or any(
        token in combined for token in ("交易中", "连续交易")
    ):
        code = "trading"
    else:
        code = "unknown"
    return LimitEventTradeStatusV1(code=code, label=label)


def map_limit_event_frame(
    frame: Any,
    *,
    route_provider: str,
    requested_date: date,
    require_reason: bool = True,
) -> dict[str, Any]:
    """Map one provider frame and verify its declared pool completeness."""

    if frame is None or not hasattr(frame, "to_dict"):
        raise RuntimeError("limit-event provider returned an unsupported payload")
    attrs = dict(getattr(frame, "attrs", {}) or {})
    if attrs.get("source_valid") is not True:
        raise RuntimeError("涨停事件池未通过数据有效性校验")
    data_date = _normalise_date(
        attrs.get("data_date") or attrs.get("requested_date"),
        "limit-event data_date",
    )
    if data_date != requested_date:
        raise RuntimeError(
            "涨停事件池交易日不匹配: "
            f"expected={requested_date.isoformat()}, actual={data_date.isoformat()}"
        )
    trade_status = _trade_status(attrs.get("trade_status"))

    records = frame.to_dict(orient="records")
    pool_total = attrs.get("pool_total")
    unique_total = attrs.get("unique_total")
    if (
        not isinstance(pool_total, int)
        or isinstance(pool_total, bool)
        or pool_total < 0
        or unique_total != pool_total
        or len(records) != pool_total
    ):
        raise RuntimeError("涨停事件池 total/唯一股票数契约无效")
    valid_empty = attrs.get("valid_empty")
    if not isinstance(valid_empty, bool) or valid_empty != (pool_total == 0):
        raise RuntimeError("涨停事件池有效空池声明与 total 不一致")
    if require_reason and attrs.get("reason_coverage") != 1.0:
        raise RuntimeError("涨停事件池涨停原因覆盖不完整")

    events: list[LimitUpEventV1 | LimitUpStatusV1] = []
    seen: set[str] = set()
    for row in records:
        code = _record_code(row)
        if code in seen:
            raise RuntimeError(f"涨停事件池存在重复代码: {code}")
        seen.add(code)
        row_date = field(row, "数据日期", "trading_date", "data_date")
        if _optional_text(row_date) is not None and _normalise_date(
            row_date, "limit-event row date"
        ) != requested_date:
            raise RuntimeError(f"涨停事件 {code} 的交易日与请求不一致")
        row_status = field(row, "交易状态", "trade_status")
        if (
            _optional_text(row_status) is not None
            and _trade_status(row_status) != trade_status
        ):
            raise RuntimeError(f"涨停事件 {code} 的交易状态与池状态不一致")

        board_label = _optional_text(field(row, "连板", "board_label"))
        seal_success_pct = None
        if require_reason:
            seal_success_pct = _optional_number(
                field(row, "封板成功率", "seal_success_pct"),
                "seal success rate",
            )
            if seal_success_pct is not None and seal_success_pct <= 1:
                seal_success_pct *= 100
        common = {
            "instrument_id": canonical_instrument_id(code),
            "name": _required_text(field(row, "名称", "name"), "limit-event name"),
            "price_cny": _optional_number(
                field(row, "价格", "price", "price_cny"), "limit-event price"
            ),
            "change_pct": _optional_number(
                field(row, "涨幅%", "涨跌幅", "change_pct"),
                "limit-event change_pct",
            ),
            "limit_up_type": _optional_text(
                field(row, "板型", "limit_up_type", "board_type")
            ),
            "board_label": board_label,
            "board_count": _board_count(board_label),
            "first_sealed_at": _optional_time(
                field(row, "首封时间", "first_sealed_at", "first_limit_up_time")
            ),
            "resealed": _optional_bool(
                field(row, "是否回封", "resealed", "is_again_limit"),
                "limit-event resealed",
            ),
        }
        if require_reason:
            events.append(LimitUpEventV1(
                **common,
                reason=_required_text(
                    field(row, "涨停原因", "reason", "reason_type"),
                    "limit-event reason",
                ),
                seal_success_pct=seal_success_pct,
                open_count=_optional_integer(
                    field(row, "炸板次数", "open_count", "open_num"),
                    "limit-event open_count",
                ),
                order_amount_cny=_optional_number(
                    field(row, "封单额", "order_amount", "order_amount_cny"),
                    "limit-event order amount",
                ),
            ))
        else:
            events.append(LimitUpStatusV1(
                **common,
                reason=_optional_text(
                    field(row, "涨停原因", "reason", "reason_type")
                ),
            ))

    unknown_board_count = sum(item.board_count is None for item in events)
    declared_unknown = attrs.get("unknown_board_count")
    declared_coverage = attrs.get("board_count_coverage")
    expected_coverage = (
        (pool_total - unknown_board_count) / pool_total if pool_total else 1.0
    )
    if (
        declared_unknown != unknown_board_count
        or not isinstance(declared_coverage, (int, float))
        or isinstance(declared_coverage, bool)
        or not math.isclose(
            float(declared_coverage),
            expected_coverage,
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
    ):
        raise RuntimeError("涨停事件池连板高度覆盖契约无效")

    page_count = attrs.get("page_count")
    if page_count is not None and (
        not isinstance(page_count, int)
        or isinstance(page_count, bool)
        or page_count < 0
    ):
        raise RuntimeError("涨停事件池 provider page_count 无效")
    source = _optional_text(attrs.get("source")) or route_provider
    return {
        "provider": source,
        "provider_as_of": parse_provider_time(attrs.get("provider_as_of")),
        "trading_date": data_date,
        "trade_status": trade_status,
        "events": tuple(events),
        "pool_total": pool_total,
        "reason_coverage": 1.0,
        "board_count_coverage": expected_coverage,
        "unknown_board_count": unknown_board_count,
        "valid_empty": valid_empty,
        "provider_page_count": page_count,
    }


def map_eastmoney_limit_up_status_frame(
    frame: Any,
    *,
    route_provider: str,
    requested_date: date,
    fetched_at: datetime,
) -> dict[str, Any]:
    """Map Eastmoney's complete non-ST pool, including Beijing Exchange stocks."""

    if frame is None or not hasattr(frame, "to_dict"):
        raise RuntimeError("Eastmoney limit-up provider returned an unsupported payload")
    records = frame.to_dict(orient="records")
    if not records:
        raise RuntimeError("Eastmoney limit-up provider did not declare a valid empty pool")

    events: list[LimitUpStatusV1] = []
    seen: set[str] = set()
    for index, row in enumerate(records):
        code = _record_code(row)
        if code in seen:
            raise RuntimeError(f"Eastmoney limit-up pool duplicated instrument {code}")
        seen.add(code)
        name = "".join(
            _required_text(field(row, "名称", "name"), "limit-event name").split()
        )
        normalized_name = "".join(name.upper().split())
        if "ST" in normalized_name or "退" in normalized_name:
            raise RuntimeError("Eastmoney non-ST limit-up pool included ST or delisting stock")
        price = _optional_number(field(row, "最新价", "price"), "limit-event price")
        change_pct = _optional_number(
            field(row, "涨跌幅", "change_pct"),
            "limit-event change_pct",
        )
        first_sealed_at = _eastmoney_time(
            field(row, "首次封板时间", "first_sealed_at")
        )
        last_sealed_at = _eastmoney_time(
            field(row, "最后封板时间", "last_sealed_at")
        )
        board_label = _eastmoney_board_label(
            field(row, "涨停统计", "board_label")
        )
        open_count = _optional_integer(
            field(row, "炸板次数", "open_count"),
            "limit-event open_count",
        )
        if price is None or change_pct is None or first_sealed_at is None:
            raise RuntimeError(
                f"Eastmoney limit-up pool row {index} omitted required status fields"
            )
        events.append(LimitUpStatusV1(
            instrument_id=canonical_instrument_id(code),
            name=name,
            price_cny=price,
            change_pct=change_pct,
            limit_up_type=(
                "一字板"
                if first_sealed_at == time(9, 25)
                and last_sealed_at == first_sealed_at
                and open_count == 0
                else None
            ),
            board_label=board_label,
            board_count=_board_count(board_label),
            first_sealed_at=first_sealed_at,
            resealed=(open_count > 0 if open_count is not None else None),
            reason=None,
        ))

    unknown_board_count = sum(item.board_count is None for item in events)
    pool_total = len(events)
    return {
        "provider": route_provider,
        "provider_as_of": None,
        "trading_date": requested_date,
        "trade_status": _trade_status(
            _derived_trade_status(requested_date, fetched_at)
        ),
        "events": tuple(events),
        "pool_total": pool_total,
        "reason_coverage": 1.0,
        "board_count_coverage": (pool_total - unknown_board_count) / pool_total,
        "unknown_board_count": unknown_board_count,
        "valid_empty": False,
        "provider_page_count": None,
    }


def map_daily_limit_up_membership(
    raw: Any,
    *,
    route_provider: str,
    requested_date: date,
) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("source_valid") is not True:
        raise RuntimeError("daily limit-up membership source is not explicitly valid")
    trading_date = _normalise_date(
        raw.get("trade_date"),
        "daily limit-up membership trade_date",
    )
    if trading_date != requested_date:
        raise RuntimeError("daily limit-up membership date does not match request")
    members = raw.get("members")
    if not isinstance(members, (list, tuple)):
        raise RuntimeError("daily limit-up membership omitted members")

    instrument_ids: list[str] = []
    seen: set[str] = set()
    for row in members:
        if not isinstance(row, dict):
            raise RuntimeError("daily limit-up membership row must be an object")
        row_date = _normalise_date(
            row.get("trade_date"),
            "daily limit-up membership row trade_date",
        )
        if row_date != requested_date:
            raise RuntimeError("daily limit-up membership row date does not match request")
        instrument_id = canonical_instrument_id(
            _required_text(
                row.get("ts_code") or row.get("code"),
                "daily limit-up membership instrument",
            )
        )
        if instrument_id in seen:
            raise RuntimeError(
                f"daily limit-up membership duplicated instrument {instrument_id}"
            )
        seen.add(instrument_id)
        instrument_ids.append(instrument_id)

    return {
        "provider": route_provider,
        "provider_as_of": parse_provider_time(raw.get("provider_as_of")),
        "trading_date": trading_date,
        "instrument_ids": tuple(sorted(instrument_ids)),
    }


__all__ = [
    "map_daily_limit_up_membership",
    "map_eastmoney_limit_up_status_frame",
    "map_limit_event_frame",
]
