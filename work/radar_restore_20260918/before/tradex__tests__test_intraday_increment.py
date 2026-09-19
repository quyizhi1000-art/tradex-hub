from copy import deepcopy
from datetime import date
from types import SimpleNamespace as NS

import pytest

from tradex.stock_selection import intraday_increment as module


def scan():
    candidates = [{"instrument_id": "600000.SH"}, {"instrument_id": "600001.SH"}]
    return {"screen_version": "macd-j-upturn-main-board.v2", "trade_date": "2026-09-18",
            "status": "monitoring", "message": "scan", "scan_candidates": candidates,
            "records": [{**c, "active": True} for c in candidates]}


def baseline(ids=("600000.SH",), *, evaluated=10):
    return set(ids), {"trade_date": "2026-09-17", "matched_count": len(ids),
                      "eligible_count": 10, "evaluated_count": evaluated}


def test_each_round_subtracts_same_previous_close_without_mutating_raw(monkeypatch):
    calls = []
    monkeypatch.setattr(module, "load_previous_close", lambda day: calls.append(day) or baseline())
    original = scan()
    before = deepcopy(original)
    cache = {}
    first = module.with_previous_close_difference(original, baselines=cache)
    second = module.with_previous_close_difference(original, baselines=cache)
    assert first == second
    assert original == before
    assert calls == [date(2026, 9, 18)]
    assert first["scan_candidates"] == [{"instrument_id": "600001.SH"}]
    assert [r["instrument_id"] for r in first["records"]] == ["600001.SH"]
    assert first["comparison"]["raw_matched_count"] == 2
    assert first["comparison"]["removed_count"] == first["comparison"]["new_count"] == 1
    # Withdrawal removes it this round; a later return is still new versus yesterday.
    withdrawn = {**original, "scan_candidates": original["scan_candidates"][:1]}
    assert module.with_previous_close_difference(withdrawn, baselines=cache)["scan_candidates"] == []
    assert module.with_previous_close_difference(original, baselines=cache)["scan_candidates"] == first["scan_candidates"]


def test_missing_baseline_is_unknown_not_zero_new(monkeypatch):
    monkeypatch.setattr(module, "load_previous_close", lambda _: (None, {"status": "unavailable"}))
    result = module.with_previous_close_difference(scan())
    assert result["status"] == "baseline_unavailable"
    assert result["records"] == result["scan_candidates"] == []
    assert result["comparison"]["new_count"] is None


def test_watch_and_history_project_same_difference_and_leave_archive_bytes(monkeypatch, tmp_path):
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from tradex.stock_selection.intraday_macd_j import read_watch, read_scan_history, archive_scan
    monkeypatch.setenv("TRADEX_DAILY_STOCK_SELECTION_DB", str(tmp_path/'missing.sqlite3'))
    monkeypatch.setattr(module, "load_previous_close", lambda _: baseline())
    now = datetime(2026,9,18,10,0,tzinfo=ZoneInfo('Asia/Shanghai'))
    original = {**scan(), "generated_at": now.isoformat(), "last_scan_slot": now.isoformat(),
                "next_scan_at": now.replace(minute=15).isoformat()}
    archive_scan(original, root=tmp_path)
    (tmp_path/'watch-2026-09-18.json').write_text(json.dumps(original),encoding='utf-8')
    before = {p.name:p.read_bytes() for p in tmp_path.glob('*.json')}
    watch = read_watch(now=now,root=tmp_path)
    history = read_scan_history(now=now,root=tmp_path)['scans'][0]
    assert watch == history
    assert len(watch['scan_candidates']) == 1
    assert before == {p.name:p.read_bytes() for p in tmp_path.glob('*.json')}


def test_valid_empty_and_partial_baselines_are_not_confused_with_missing(monkeypatch):
    monkeypatch.setattr(module, "load_previous_close", lambda _: baseline((), evaluated=9))
    result = module.with_previous_close_difference(scan())
    assert result["comparison"]["new_count"] == 2
    assert "未核验" in result["message"]


@pytest.mark.parametrize("change", [{"screen_version": "macd-j-upturn-main-board.v1"},
    {"scan_kind": "manual_close"}, {"scan_kind": "close_confirmation"}, {"status": "unavailable"}])
def test_legacy_and_close_and_failed_scans_are_preserved_without_baseline_calls(monkeypatch, change):
    def unexpected(_):
        raise AssertionError("must not load baseline")
    monkeypatch.setattr(module, "load_previous_close", unexpected)
    original = {**scan(), **change}
    assert module.with_previous_close_difference(original) == original


def test_exact_previous_trading_day_and_version_are_required(monkeypatch):
    calls = []
    def read(day, strategy_id, *, strategy_version):
        calls.append((day, strategy_version))
        return None
    monkeypatch.setattr(module, "read_archived_strategy_result", read)
    ids, info = module.load_previous_close(date(2026, 9, 21))
    assert ids is None
    assert info["trade_date"] == "2026-09-18"
    assert calls == [(date(2026, 9, 18), "v2")]


@pytest.mark.parametrize("quality,version", [("unavailable", "v2"), ("degraded", "v1")])
def test_unavailable_or_wrong_rule_archive_cannot_be_baseline(monkeypatch, quality, version):
    screen = NS(contract="stock_macd_j_screen.v1", quality=quality,
                screen_version=f"macd-j-upturn-main-board.{version}")
    monkeypatch.setattr(module, "read_archived_strategy_result", lambda *a, **k:
                        NS(trade_date=date(2026,9,17), quality=quality, payload=screen))
    assert module.load_previous_close(date(2026,9,18))[0] is None
