"""Read-only intraday membership difference against the exact previous close."""
from datetime import date, timedelta

from tradex.market_calendar import CalendarDayStatus, calendar_day_status
from .store import read_archived_strategy_result

CONTRACT = "intraday_macd_j_increment.v1"


def load_previous_close(day, *, version="v4"):
    previous = day - timedelta(days=1)
    for _ in range(30):
        status = calendar_day_status(previous)
        if status is CalendarDayStatus.UNVERIFIED:
            return None, {"status": "unavailable", "reason": "calendar_unverified", "trade_date": None}
        if status is CalendarDayStatus.VERIFIED_TRADING_DAY:
            break
        previous -= timedelta(days=1)
    else:
        return None, {"status": "unavailable", "reason": "calendar_unverified", "trade_date": None}
    metadata = {"status": "unavailable", "trade_date": previous.isoformat()}
    result = read_archived_strategy_result(previous, "macd-j-upturn-main-board", strategy_version=version)
    if result is None:
        return None, {**metadata, "reason": "previous_close_missing"}
    screen = result.payload
    if (result.trade_date != previous or result.quality == "unavailable"
            or screen.contract != "stock_macd_j_screen.v1" or screen.quality == "unavailable"
            or screen.screen_version != f"macd-j-upturn-main-board.{version}"):
        return None, {**metadata, "reason": "previous_close_unavailable"}
    return {row.instrument_id for row in screen.candidates}, {
        "status": "available", "trade_date": previous.isoformat(),
        "result_id": result.result_id, "source_snapshot_revision": result.source_snapshot_revision,
        "strategy_version": result.strategy_version, "quality": screen.quality,
        "matched_count": screen.matched_count, "evaluated_count": screen.evaluated_count,
        "eligible_count": screen.board_eligible_count,
    }


def with_previous_close_difference(scan, *, baselines=None):
    """Project display/alerts only. Raw scan archives and close results stay intact."""
    if (scan.get("screen_version") not in {"macd-j-upturn-main-board.v1", "macd-j-upturn-main-board.v2", "macd-j-upturn-main-board.v3", "macd-j-upturn-main-board.v4"}
            or scan.get("scan_kind") in {"manual_close", "close_confirmation"}
            or scan.get("status") not in {"monitoring", "partial"}
            or not isinstance(scan.get("scan_candidates"), list)):
        return scan
    day = date.fromisoformat(scan["trade_date"])
    baselines = {} if baselines is None else baselines
    version = scan["screen_version"].rsplit(".", 1)[1]
    key = (day, version)
    if key not in baselines:
        baselines[key] = load_previous_close(day, version=version)
    old_ids, baseline = baselines[key]
    raw = scan["scan_candidates"]
    comparison = {"contract": CONTRACT, "baseline": baseline, "raw_matched_count": len(raw)}
    result = {**scan, "comparison": comparison}
    if old_ids is None:
        comparison.update(status="unavailable", removed_count=None, new_count=None)
        result.update(status="baseline_unavailable", scan_candidates=[], records=[],
                      message="上一交易日同版本 MACD＋KDJ 存档不可用，暂时无法判断新增；原始扫描已保留。")
        return result
    candidates = [row for row in raw if row["instrument_id"] not in old_ids]
    comparison.update(status="available", removed_count=len(raw)-len(candidates), new_count=len(candidates))
    result["scan_candidates"] = candidates
    result["matched_count"] = len(candidates)
    result["records"] = [row for row in scan.get("records", []) if row["instrument_id"] not in old_ids]
    description = (f"新增基准：{baseline['trade_date']} 盘后存档 {baseline['matched_count']} 只；"
                   f"本轮命中 {len(raw)} 只，剔除昨日已入选 {len(raw)-len(candidates)} 只，"
                   f"本轮新增 {len(candidates)} 只。")
    if baseline["evaluated_count"] < baseline["eligible_count"]:
        description += "昨日存档含未核验股票；新增仅指相对该存档的名单差集。"
    result["message"] = f"{scan.get('message', '')} {description}".strip()
    return result
