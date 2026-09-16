from copy import deepcopy
from datetime import date, datetime
import threading
from zoneinfo import ZoneInfo

import pytest

from tradex.data_gateway.instrument_taxonomy import (
    INDUSTRY_COVERAGE_CHECKED, MARKET_INDUSTRY_CHECKED, _validate_bundle,
)
from tradex.data_sources import tushare_fetchers as fetchers
from tradex.instrument_taxonomy.service import InstrumentTaxonomyService
from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader, InstrumentTaxonomyStore


DAY = date(2026, 9, 16)
CODES = [f"{801000 + i}.SI" for i in range(31)]


def member(key="603267.SH", group=CODES[0], since="20210730"):
    return {"ts_code": key, "l1_code": group, "l1_name": "国防军工",
            "l2_code": "801745.SI", "l2_name": "军工电子Ⅱ",
            "l3_code": "857451.SI", "l3_name": "军工电子Ⅲ",
            "in_date": since, "out_date": None, "is_new": "Y"}


def install_source(monkeypatch, *, partitions=None, targeted=None):
    calls = []
    partitions = partitions or {}
    targeted = targeted or {}

    def paged(api, params, fields, **kwargs):
        calls.append((api, params))
        if api == "index_classify":
            assert params == {"level": "L1", "src": "SW2021"}
            return [{"index_code": code} for code in CODES], [(api, "catalog-request")]
        if "l1_code" in params:
            group = params["l1_code"]
            return partitions.get(group, [member("999999.SH", group)]), [(api, group)]
        return targeted.get(params["ts_code"], []), [(api, params["ts_code"])]

    monkeypatch.setattr(fetchers, "_paged_records", paged)
    return calls


def test_partitions_reconcile_a_stock_omitted_by_group_response(monkeypatch):
    calls = install_source(monkeypatch, targeted={"603267.SH": [member()]})
    rows, request_ids, missing = fetchers._taxonomy_sw_memberships(
        [{"ts_code": "603267.SH"}], DAY,
    )
    assert rows == [member()]
    assert missing == []
    assert len([p for api, p in calls if "l1_code" in p]) == 31
    assert any(p.get("ts_code") == "603267.SH" for api, p in calls)
    assert len(request_ids) == 33


def test_source_absence_remains_explicit_after_individual_lookup(monkeypatch):
    calls = install_source(monkeypatch)
    rows, _, missing = fetchers._taxonomy_sw_memberships([{"ts_code": "603267.SH"}], DAY)
    assert rows == [] and missing == ["603267.SH"]
    assert calls[-1][1]["ts_code"] == "603267.SH"


def test_large_hole_fails_before_unbounded_individual_requests(monkeypatch):
    calls = install_source(monkeypatch)
    with pytest.raises(RuntimeError, match="覆盖不足"):
        fetchers._taxonomy_sw_memberships(
            [{"ts_code": f"{600000+i}.SH"} for i in range(65)], DAY,
        )
    assert not any("ts_code" in params for _, params in calls)


def test_reclassification_uses_effective_date_not_partition_order(monkeypatch):
    latest = member(group=CODES[0], since="20260701")
    older = {**member(group=CODES[-1]), "l2_code": "801181.SI", "l2_name": "房地产开发"}
    install_source(monkeypatch, partitions={CODES[0]: [latest], CODES[-1]: [older]})
    rows, _, _ = fetchers._taxonomy_sw_memberships([{"ts_code": "603267.SH"}], DAY)
    assert rows == [latest]


def test_conflicting_same_date_memberships_fail_closed(monkeypatch):
    install_source(monkeypatch, partitions={
        CODES[0]: [member()], CODES[1]: [member(group=CODES[1])],
    })
    with pytest.raises(RuntimeError, match="归属冲突"):
        fetchers._taxonomy_sw_memberships([{"ts_code": "603267.SH"}], DAY)


def source(count=100):
    keys = [f"{600000+i}.SH" for i in range(count)]
    return {"contract": "instrument_taxonomy_source_bundle.v1", "schema_version": 1,
            "as_of": DAY.isoformat(), "stocks": [{"ts_code": key, "name": key} for key in keys],
            "companies": [], "sw_memberships": [member(key) for key in keys],
            "business_segments": [{"ts_code": keys[0], "end_date": "20260630",
                                   "bz_item": "电子元器件", "bz_sales": 1}],
            "industry_missing_instruments": [], "flags": []}


@pytest.mark.parametrize("defect", ["missing", "duplicate", "path", "future", "expired", "coverage"])
def test_gateway_rejects_incomplete_or_invalid_industry_data(defect):
    data = source()
    if defect in ("missing", "coverage"):
        removed = data["sw_memberships"].pop()
        if defect == "coverage":
            data["industry_missing_instruments"] = [removed["ts_code"], data["sw_memberships"].pop()["ts_code"]]
    elif defect == "duplicate":
        data["sw_memberships"].append(data["sw_memberships"][0])
    elif defect == "path":
        data["sw_memberships"][0]["l2_name"] = ""
    elif defect == "future":
        data["sw_memberships"][0]["in_date"] = "20270101"
    else:
        data["sw_memberships"][0]["out_date"] = "20260101"
    with pytest.raises(RuntimeError):
        _validate_bundle(data, "fixture", DAY)


def test_gateway_accepts_small_explicitly_confirmed_source_absence():
    data = source()
    data["industry_missing_instruments"] = [data["sw_memberships"].pop()["ts_code"]]
    assert _validate_bundle(data, "fixture", DAY)["industry_missing_instruments"] == ["600099.SH"]


def test_service_rejects_coverage_regression_without_replacing_catalog(tmp_path):
    data = source()
    with InstrumentTaxonomyStore(tmp_path / "taxonomy.sqlite3") as store:
        service = InstrumentTaxonomyService(store)
        before = service.refresh(as_of=DAY, source_loader=lambda _: data)
        broken = deepcopy(data)
        broken["industry_missing_instruments"] = [broken["sw_memberships"].pop()["ts_code"]]
        with pytest.raises(RuntimeError, match="coverage regressed"):
            service.refresh(as_of=DAY, source_loader=lambda _: broken)
        with InstrumentTaxonomyReader(store.db_path) as reader:
            assert reader.status() == before
            assert len(reader.all_profiles()) == 100
            assert reader.get("600099.SH").statistical_industry is not None
        assert INDUSTRY_COVERAGE_CHECKED in before.flags


@pytest.mark.parametrize("checked,expected", [(False, 1), (True, 0)])
def test_same_day_unchecked_catalog_refreshes_but_checked_catalog_does_not(monkeypatch, tmp_path, checked, expected):
    from tradex.analysis_worker import AnalysisRuntime
    db = tmp_path / "taxonomy.sqlite3"
    monkeypatch.setenv("TRADEX_INSTRUMENT_TAXONOMY_DB", str(db))
    with InstrumentTaxonomyStore(db) as store:
        status = InstrumentTaxonomyService(store).refresh(as_of=DAY, source_loader=lambda _: source())
        with InstrumentTaxonomyReader(db) as reader:
            profiles = reader.all_profiles()
        store.replace_catalog(status.model_copy(update={"flags": (INDUSTRY_COVERAGE_CHECKED, MARKET_INDUSTRY_CHECKED) if checked else ()}), profiles)
    runtime = AnalysisRuntime.__new__(AnalysisRuntime)
    runtime._enable_taxonomy_auto_refresh = True
    runtime._taxonomy_refresh_lock = threading.Lock()
    runtime._taxonomy_refresh_thread = None
    runtime._taxonomy_last_attempt_at = None
    called = []
    runtime._refresh_taxonomy = lambda observed: called.append(observed)
    now = datetime(2026, 9, 16, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    runtime._maybe_refresh_taxonomy(now)
    if runtime._taxonomy_refresh_thread:
        runtime._taxonomy_refresh_thread.join()
    runtime._maybe_refresh_taxonomy(now)  # Existing 30-minute retry throttle remains.
    assert len(called) == expected


def test_market_block_acquisition_excludes_subdivisions_and_concepts(monkeypatch):
    calls = []

    def paged(api, params, fields, **kwargs):
        calls.append((api, params))
        if api == "ths_index":
            return [{"ts_code": "881125.TI", "name": "汽车整车", "type": "I"},
                    {"ts_code": "884099.TI", "name": "乘用车", "type": "I"},
                    {"ts_code": "885001.TI", "name": "概念", "type": "N"}], []
        assert params == {"ts_code": "881125.TI"}
        return [{"ts_code": "881125.TI", "con_code": "600104.SH", "is_new": "Y"}], []

    monkeypatch.setattr(fetchers, "_paged_records", paged)
    result = fetchers._taxonomy_market_industries([{"ts_code": "600104.SH"}], DAY)
    assert result["memberships"] == [{"instrument_id": "600104.SH", "code": "881125.TI", "name": "汽车整车"}]
    assert len(calls) == 2


def market_source():
    data = source()
    return {"contract": "instrument_industry_blocks_source.v1", "schema_version": 1,
            "as_of": DAY.isoformat(), "taxonomy": "ths", "stocks": data["stocks"],
            "memberships": [{"instrument_id": row["ts_code"], "code": "881276.TI", "name": "军工电子"}
                            for row in data["stocks"]], "missing_instruments": []}


def test_market_conflict_is_rechecked_and_never_assigned_by_response_order(monkeypatch):
    calls = []

    def paged(api, params, fields, **kwargs):
        calls.append(params)
        if api == "ths_index":
            return [{"ts_code": "881121.TI", "name": "半导体", "type": "I"},
                    {"ts_code": "881124.TI", "name": "消费电子", "type": "I"}], []
        rows = [{"ts_code": key, "con_code": "688661.SH", "is_new": "Y"}
                for key in ("881121.TI", "881124.TI")]
        return ([row for row in rows if row["ts_code"] == params["ts_code"]]
                if "ts_code" in params else rows), []

    monkeypatch.setattr(fetchers, "_paged_records", paged)
    result = fetchers._taxonomy_market_industries([{"ts_code": "688661.SH"}], DAY)
    assert result["memberships"] == []
    assert result["missing_instruments"] == result["conflicting_instruments"] == ["688661.SH"]
    assert {"con_code": "688661.SH"} in calls


def test_market_overlay_preserves_statistical_and_business_evidence_and_rejects_loss(tmp_path):
    now = datetime(2026, 9, 16, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    with InstrumentTaxonomyStore(tmp_path / "taxonomy.sqlite3") as store:
        service = InstrumentTaxonomyService(store)
        service.refresh(as_of=DAY, source_loader=lambda _: source(), now=now)
        with InstrumentTaxonomyReader(store.db_path) as reader:
            original = reader.get("600000.SH").model_dump(exclude={"market_industry"})
        result = service.refresh_market_industries(source_loader=lambda _: market_source(), now=now)
        with InstrumentTaxonomyReader(store.db_path) as reader:
            profile = reader.get("600000.SH")
        assert profile.market_industry.level1_name == "军工电子"
        assert profile.model_dump(exclude={"market_industry"}) == original
        assert MARKET_INDUSTRY_CHECKED in result.flags
        broken = market_source()
        broken["missing_instruments"] = [broken["memberships"].pop()["instrument_id"]]
        with pytest.raises(RuntimeError, match="coverage regressed"):
            service.refresh_market_industries(source_loader=lambda _: broken, now=now)
        with InstrumentTaxonomyReader(store.db_path) as reader:
            assert reader.status() == result


@pytest.mark.parametrize("problem", ["missing", "duplicate", "date"])
def test_market_gateway_rejects_unverified_holes_conflicts_and_wrong_dates(problem):
    from tradex.data_gateway.instrument_taxonomy import validate_market_industries
    data = market_source()
    if problem == "missing":
        data["memberships"].pop()
    elif problem == "duplicate":
        data["memberships"].append({**data["memberships"][0], "code": "881125.TI"})
    else:
        data["as_of"] = "2026-09-15"
    with pytest.raises(RuntimeError):
        validate_market_industries(data, DAY)
