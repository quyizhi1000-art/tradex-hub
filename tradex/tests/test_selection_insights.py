from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tradex.data_gateway.contracts import ContractMetadata, DailyLimitUpMembershipV1, QualityStatus
from tradex.stock_selection.insights import STRATEGY_FIELDS, build_insights, compare_result, trading_window
from tradex.stock_selection.service import DailyStockSelectionService
from tradex.stock_selection.store import DailyStockSelectionStore

STRATEGY = "long-upper-shadow-main-board"
NOW = datetime(2026, 9, 17, 20, tzinfo=ZoneInfo("Asia/Shanghai"))


class Candidate:
    def __init__(self, code="600001.SH", **kwargs):
        self.instrument_id = code
        self.name = "样本"
        self.data = dict(instrument_id=code, name=self.name, reference_close=10,
                         occurrence_count=2, latest_occurrence_date="2026-09-11", evidence=[], **kwargs)

    def model_dump(self, **_kwargs):
        return dict(self.data)


def result(day, candidates, version="v3", quality="accepted"):
    return SimpleNamespace(strategy_id=STRATEGY, strategy_version=version, quality=quality,
                           result_id=f"{day}:{version}", generated_at=NOW,
                           payload=SimpleNamespace(candidates=candidates))


def membership(day, codes=()):
    return DailyLimitUpMembershipV1(
        trading_date=date.fromisoformat(day), instrument_ids=tuple(sorted(codes)),
        metadata=ContractMetadata(contract="daily_limit_up_membership.v1", provider="fixture",
                                  fetched_at=NOW, quality=QualityStatus.ACCEPTED),
    )


class Store:
    def __init__(self, entries, memberships):
        self.entries, self.memberships = entries, memberships

    def list_strategy_results(self, day):
        return self.entries.get(str(day), [])

    def get_strategy_result(self, day, strategy_id, strategy_version):
        return next((r for r in self.list_strategy_results(day)
                     if r.strategy_id == strategy_id and r.strategy_version == strategy_version), None)

    def get_limit_up_membership(self, day):
        return self.memberships.get(str(day))


def test_comparison_ignores_quotes_and_distinguishes_changed_new_unknown():
    prior = Candidate()
    quoted = Candidate()
    quoted.data["reference_close"] = 12
    previous = result("2026-09-16", [prior])
    current = result("2026-09-17", [quoted, Candidate("600002.SH")])
    comparison = compare_result(current, previous, date(2026, 9, 16))
    assert comparison["counts"] == dict(new=1, changed=0, unchanged=1, unknown=0)
    quoted.data["occurrence_count"] = 3
    changed = compare_result(current, previous, date(2026, 9, 16))
    assert changed["rows"]["600001.SH"]["changes"] == [{"field": "occurrence_count", "before": 2, "after": 3}]
    assert compare_result(current, None, date(2026, 9, 16))["counts"]["unknown"] == 2


def test_calendar_counts_five_sessions_including_target_and_holidays():
    assert [str(d) for d in trading_window(date(2026, 9, 17))] == [
        "2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17"]
    assert [str(d) for d in trading_window(date(2026, 10, 9))] == [
        "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09"]
    assert trading_window(date(2027, 1, 5)) == []


@pytest.mark.parametrize("strategy_id,fields", STRATEGY_FIELDS.items())
def test_all_five_strategies_compare_their_own_fields_only(strategy_id, fields):
    before, after = Candidate(), Candidate()
    prior, current = result("2026-09-16", [before]), result("2026-09-17", [after])
    prior.strategy_id = current.strategy_id = strategy_id
    after.data.update(reference_close=99, daily_return_pct=8, amount_cny=1000,
                      rank=99, score=98, factor_coverage=0.1, total_market_cap_cny=999,
                      contributions=[{"factor": "roe", "raw_value": 30, "weighted_contribution": 9}])
    assert compare_result(current, prior, date(2026, 9, 16))["counts"]["unchanged"] == 1
    before.data[fields[0]], after.data[fields[0]] = "old", "new"
    assert compare_result(current, prior, date(2026, 9, 16))["counts"]["changed"] == 1


def test_volume_compares_displayed_floor_and_events_not_hidden_ohlc():
    before, after = Candidate(), Candidate()
    prior, current = result("2026-09-16", [before]), result("2026-09-17", [after])
    prior.strategy_id = current.strategy_id = "upward-volume-surge-main-board"
    evidence = dict(trade_date="2026-09-09", change_pct=4.18, volume_multiple=2.27,
                    volume_shares=103320503, prior_5d_average_volume_shares=45522765.8,
                    close=12.45, previous_close=11.95, low=11.29)
    for candidate in (before, after):
        candidate.data.update(anchor_trade_date="2026-09-09", anchor_low=11.29,
                              minimum_subsequent_close=12.92, evidence=[dict(evidence)])
    after.data["evidence"][0].update(close=13, previous_close=12, low=11.30)
    assert compare_result(current, prior, date(2026, 9, 16))["counts"]["unchanged"] == 1
    after.data["minimum_subsequent_close"] = 12.80
    changes = compare_result(current, prior, date(2026, 9, 16))["rows"]["600001.SH"]["changes"]
    assert changes == [{"field": "minimum_subsequent_close", "before": 12.92, "after": 12.80}]
    after.data["evidence"].append(dict(evidence, trade_date="2026-09-17"))
    changes = compare_result(current, prior, date(2026, 9, 16))["rows"]["600001.SH"]["changes"]
    assert [c["field"] for c in changes] == ["minimum_subsequent_close", "evidence"]
    assert "close" not in changes[1]["after"][0]


def test_only_rendered_reasons_and_risks_are_compared():
    before, after = Candidate(), Candidate()
    prior, current = result("2026-09-16", [before]), result("2026-09-17", [after])
    prior.strategy_id = current.strategy_id = "balanced-multifactor-a-share"
    before.data.update(reasons=["主要依据", "隐藏次要依据"], risks=["主要风险"])
    after.data.update(reasons=["主要依据", "隐藏依据改变"], risks=["主要风险"])
    assert compare_result(current, prior, date(2026, 9, 16))["counts"]["unchanged"] == 1
    after.data["reasons"][0] = "主要依据改变"
    assert compare_result(current, prior, date(2026, 9, 16))["counts"]["changed"] == 1


def test_each_entry_and_limit_day_is_preserved_without_cartesian_duplicates():
    dates = [str(d) for d in trading_window(date(2026, 9, 17))]
    entries = {day: [result(day, [])] for day in dates}
    entries["2026-09-14"] = [result("2026-09-14", [Candidate()])]
    entries["2026-09-16"] = [result("2026-09-16", [Candidate()])]
    # Today's row is absent; an earlier post-entry limit-up still qualifies.
    pools = {day: membership(day) for day in dates}
    for day in ("2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16"):
        pools[day] = membership(day, ["600001.SH"])
    store = Store(entries, pools)
    info = build_insights(store, date(2026, 9, 17), [SimpleNamespace(strategy_id=STRATEGY, strategy_version="v3")])
    followup = info["strategies"][STRATEGY]["followup"]
    assert followup["quality"] == "complete"
    assert len(followup["rows"]) == 1
    row = followup["rows"][0]
    assert [r["trade_date"] for r in row["archive_records"]] == ["2026-09-14", "2026-09-16"]
    assert [r["trade_date"] for r in row["limit_up_records"]] == ["2026-09-14", "2026-09-15", "2026-09-16"]
    assert row["limit_up_records"][-1]["archive_dates"] == ["2026-09-14", "2026-09-16"]


def test_missing_day_and_version_are_not_replaced_with_older_archives():
    store = Store({"2026-09-17": [result("2026-09-17", [Candidate()])],
                   "2026-09-16": [result("2026-09-16", [Candidate()], version="v2")]}, {})
    info = build_insights(store, date(2026, 9, 17), [SimpleNamespace(strategy_id=STRATEGY, strategy_version="v3")])
    strategy = info["strategies"][STRATEGY]
    assert strategy["comparison"]["status"] == "unavailable"
    assert strategy["comparison"]["counts"]["new"] == 0
    assert strategy["followup"]["quality"] == "partial"
    assert "2026-09-16" in strategy["followup"]["missing_archive_dates"]
    assert len(strategy["followup"]["missing_limit_up_dates"]) == 5


def test_membership_cache_persists_checks_digest_and_distinguishes_valid_empty(tmp_path):
    store = DailyStockSelectionStore(tmp_path / "selections.sqlite3")
    assert store.get_limit_up_membership(date(2026, 9, 17)) is None
    store.record_limit_up_membership(membership("2026-09-17"))
    revision = store.limit_up_membership_revision()
    store.close()
    store = DailyStockSelectionStore(tmp_path / "selections.sqlite3")
    assert store.get_limit_up_membership(date(2026, 9, 17)).instrument_ids == ()
    assert store.limit_up_membership_revision() == revision
    store._connection.execute("UPDATE stock_selection_limit_up_memberships SET payload_digest = 'invalid'")
    with pytest.raises(RuntimeError, match="digest"):
        store.get_limit_up_membership(date(2026, 9, 17))
    store.close()


def test_refresh_is_bounded_cached_and_wrong_dates_fail_closed(tmp_path):
    store = DailyStockSelectionStore(tmp_path / "selections.sqlite3")
    store.list_strategy_dates = lambda **kwargs: [{"trade_date": "2026-09-17"}]
    requests = []

    def load(day, **kwargs):
        requests.append(day)
        return membership(day)

    service = DailyStockSelectionService(store, membership_loader=load, clock=lambda: NOW)
    service.refresh_limit_up_memberships(max_requests=2)
    assert len(requests) == 2
    service.refresh_limit_up_memberships(max_requests=5)
    assert len(requests) == 5
    service.refresh_limit_up_memberships()
    assert len(requests) == 5
    store.close()
    store = DailyStockSelectionStore(":memory:")
    store.list_strategy_dates = lambda **kwargs: [{"trade_date": "2026-09-17"}]
    service = DailyStockSelectionService(store, membership_loader=lambda *a, **kw: membership("2026-09-10"), clock=lambda: NOW)
    service.refresh_limit_up_memberships(max_requests=1)
    assert store.get_limit_up_membership(date(2026, 9, 17)) is None
    store.close()
