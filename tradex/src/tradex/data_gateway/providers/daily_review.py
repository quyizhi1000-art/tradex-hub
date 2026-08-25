"""Provider mappings for post-close stock flow and dragon-tiger evidence."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..contracts import DragonTigerTradeV1, StockFundFlowV1
from .market_overview import field, finite_number, parse_provider_time
from .securities import canonical_instrument_id, frame_request_id


def _records(frame: Any, *, allow_empty: bool = False) -> list[dict[str, Any]]:
    if frame is None or not hasattr(frame, "to_dict"):
        raise RuntimeError("daily-review provider returned an unsupported payload")
    records = frame.to_dict(orient="records")
    if not records and not allow_empty:
        raise RuntimeError("daily-review provider returned no records")
    return records


def _number(row: dict[str, Any], label: str, *aliases: str) -> float:
    value = finite_number(field(row, *aliases))
    if value is None:
        raise ValueError(f"daily-review row is missing {label}")
    return value


def _optional_number(row: dict[str, Any], *aliases: str) -> float | None:
    return finite_number(field(row, *aliases))


def _text(row: dict[str, Any], label: str, *aliases: str) -> str:
    value = str(field(row, *aliases) or "").strip()
    if not value:
        raise ValueError(f"daily-review row is missing {label}")
    return value


def _trade_date(value: Any, label: str = "trade_date") -> date:
    raw = str(value or "").strip().replace("-", "")
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc


def frame_trade_date(frame: Any) -> date:
    attrs = dict(getattr(frame, "attrs", {}) or {})
    return _trade_date(attrs.get("trade_date"), "frame trade_date")


def frame_provider_as_of(frame: Any):
    return parse_provider_time((getattr(frame, "attrs", {}) or {}).get("provider_as_of"))


def daily_review_request_id(frame: Any) -> str | None:
    return frame_request_id(frame)


def daily_review_units_verified(provider: str) -> bool:
    return provider in {"tushare", "ths_fuyao", "akshare_exact_day"}


def map_stock_fund_flow_frame(
    frame: Any,
    *,
    provider: str,
    requested_date: date,
) -> tuple[StockFundFlowV1, ...]:
    if frame_trade_date(frame) != requested_date:
        raise RuntimeError("stock fund-flow trading date does not match the request")
    multiplier = 10_000.0 if provider == "tushare" else 1.0
    flows: list[StockFundFlowV1] = []
    for row in _records(frame):
        row_date = field(row, "trade_date", "交易日期", "上榜日", "date")
        if row_date not in (None, "") and _trade_date(row_date) != requested_date:
            raise RuntimeError("stock fund-flow row has a mismatched trading date")
        flows.append(
            StockFundFlowV1(
                instrument_id=canonical_instrument_id(
                    _text(row, "instrument_id", "ts_code", "代码", "code", "symbol")
                ),
                net_amount_cny=_number(
                    row, "net_amount", "net_mf_amount", "net_amount_cny", "净流入额"
                )
                * multiplier,
                large_net_amount_cny=(
                    _number(row, "large_buy", "buy_lg_amount", "大单买入额")
                    - _number(row, "large_sell", "sell_lg_amount", "大单卖出额")
                )
                * multiplier,
                extra_large_net_amount_cny=(
                    _number(row, "extra_large_buy", "buy_elg_amount", "特大单买入额")
                    - _number(row, "extra_large_sell", "sell_elg_amount", "特大单卖出额")
                )
                * multiplier,
            )
        )
    flows.sort(key=lambda item: item.instrument_id)
    if len({item.instrument_id for item in flows}) != len(flows):
        raise RuntimeError("stock fund-flow provider returned duplicate instruments")
    return tuple(flows)


def map_dragon_tiger_frame(
    frame: Any,
    *,
    provider: str,
    requested_date: date,
) -> tuple[tuple[DragonTigerTradeV1, ...], bool]:
    if frame_trade_date(frame) != requested_date:
        raise RuntimeError("dragon-tiger trading date does not match the request")
    attrs = dict(getattr(frame, "attrs", {}) or {})
    records = _records(frame, allow_empty=True)
    if not records:
        if attrs.get("valid_empty") is not True:
            raise RuntimeError("dragon-tiger provider returned an unverified empty result")
        return (), True

    # top_list monetary fields are CNY (the provider sample's net_rate equals
    # net_amount / amount); unlike moneyflow, they are not ten-thousand CNY.
    multiplier = 1.0
    trades: list[DragonTigerTradeV1] = []
    for row in records:
        row_date = field(row, "trade_date", "交易日期", "上榜日", "date")
        if row_date not in (None, "") and _trade_date(row_date) != requested_date:
            raise RuntimeError("dragon-tiger row has a mismatched trading date")
        trades.append(
            DragonTigerTradeV1(
                instrument_id=canonical_instrument_id(
                    _text(row, "instrument_id", "ts_code", "代码", "code", "symbol")
                ),
                name=_text(row, "name", "name", "名称"),
                close=_optional_number(row, "close", "收盘价"),
                change_pct=_number(
                    row, "change_pct", "pct_change", "pct_chg", "涨跌幅"
                ),
                turnover_pct=_optional_number(row, "turnover_rate", "换手率"),
                market_amount_cny=(
                    value * multiplier
                    if (value := _optional_number(row, "amount", "market_amount_cny", "成交额"))
                    is not None
                    else None
                ),
                buy_amount_cny=_number(
                    row, "buy_amount", "l_buy", "buy_amount_cny", "龙虎榜买入额"
                )
                * multiplier,
                sell_amount_cny=_number(
                    row, "sell_amount", "l_sell", "sell_amount_cny", "龙虎榜卖出额"
                )
                * multiplier,
                net_amount_cny=_number(
                    row, "net_amount", "net_amount", "net_amount_cny", "龙虎榜净买额"
                )
                * multiplier,
                reason=(
                    reason
                    if (reason := str(field(row, "reason", "上榜原因", "解读") or "").strip())
                    else None
                ),
            )
        )
    trades.sort(
        key=lambda item: (-item.net_amount_cny, item.instrument_id, item.reason or "")
    )
    return tuple(trades), False


__all__ = [
    "daily_review_request_id",
    "daily_review_units_verified",
    "frame_provider_as_of",
    "map_dragon_tiger_frame",
    "map_stock_fund_flow_frame",
]
