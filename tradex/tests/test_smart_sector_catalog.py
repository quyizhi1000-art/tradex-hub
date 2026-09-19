from datetime import date, timedelta
import json
from pathlib import Path

import pytest

from tradex.smart_sector_library.catalog import MarketSectorDossierV2, SmartSectorCatalog, decide_membership
from tradex.data_gateway.sector_evidence import map_sector_evidence

EVIDENCE = Path(__file__).parents[1] / "src/tradex/smart_sector_library/market_membership_evidence.v2.json"

def dossier(index=0):
    return MarketSectorDossierV2.model_validate(json.loads(EVIDENCE.read_text(encoding="utf-8"))["entries"][index])

@pytest.mark.parametrize("index,expected,chain", [(0,"人形机器人","机电执行器"),(1,"AI算力","AI服务器电源"),(2,"人形机器人","精密减速器")])
def test_market_examples_and_generic_stock_identity(index, expected, chain):
    d = dossier(index)
    original = decide_membership(d, date(2026,9,18))
    renamed = decide_membership(d.model_copy(update={"instrument_id":"600999.SH","name":"同行样例"}),date(2026,9,18))
    assert original.primary_sector_name == renamed.primary_sector_name == expected
    assert chain in original.chain
    assert original.status == "verified"

def test_investment_and_list_order_cannot_become_primary():
    d=dossier(1)
    investment=d.candidates[0].model_copy(update={"sector_key":"chips","sector_name":"第三代半导体","relation":"investment"})
    result=decide_membership(d.model_copy(update={"candidates":(investment,*d.candidates)}),d.reviewed_on)
    assert result.primary_sector_name == "AI算力"


def test_reviewed_investment_primary_requires_official_evidence_and_explicit_review():
    d = dossier()
    c = d.candidates[0].model_copy(update={"relation": "investment", "investment_reviewed": True})
    reviewed = d.model_copy(update={"candidates": (c,)})
    result = decide_membership(reviewed, d.reviewed_on)
    assert result.status == "verified"
    assert "investment_association" in result.flags
    no_review = c.model_copy(update={"investment_reviewed": False})
    assert decide_membership(d.model_copy(update={"candidates": (no_review,)}), d.reviewed_on).status == "unresolved"
    secondary_only = tuple(e.model_copy(update={"source_kind": "structured_provider"})
                           if e.source_kind in {"official_filing", "official_company"} else e for e in d.evidence)
    assert decide_membership(reviewed.model_copy(update={"evidence": secondary_only}), d.reviewed_on).status == "unresolved"
    for relation in ("plan", "rumor"):
        unsupported = c.model_copy(update={"relation": relation})
        assert decide_membership(d.model_copy(update={"candidates": (unsupported,)}), d.reviewed_on).status == "unresolved"

def test_no_market_evidence_and_conflicting_primaries_abstain():
    d=dossier()
    missing=d.model_copy(update={"candidates":tuple(c.model_copy(update={"market_evidence_ids":()}) for c in d.candidates)})
    assert decide_membership(missing,d.reviewed_on).primary_sector_name is None
    conflict=d.model_copy(update={"candidates":(d.candidates[0],d.candidates[1].model_copy(update={"market_role":"primary"}))})
    result=decide_membership(conflict,d.reviewed_on)
    assert result.status == "disputed"
    assert result.primary_sector_name is None


def test_crosschecked_sources_need_two_publishers_and_specific_business_review():
    d=dossier()
    business=d.evidence[0].model_copy(update={"source_kind":"structured_provider"})
    refs=(business,*d.evidence[1:])
    c=d.candidates[0].model_copy(update={"business_review_basis":"crosschecked_sources",
          "business_evidence_ids":(refs[0].evidence_id,refs[1].evidence_id)})
    reviewed=d.model_copy(update={"evidence":refs,"candidates":(c,)})
    result=decide_membership(reviewed,d.reviewed_on)
    assert result.status == "verified"
    assert result.business_review_basis == "crosschecked_sources"
    single=c.model_copy(update={"business_evidence_ids":(refs[1].evidence_id,)})
    assert decide_membership(reviewed.model_copy(update={"candidates":(single,)}),d.reviewed_on).status == "unresolved"

def test_no_future_review_or_expired_assignment_leaks():
    d=dossier()
    before=decide_membership(d,d.reviewed_on-timedelta(days=1))
    after=decide_membership(d,d.review_due+timedelta(days=1))
    assert before.primary_sector_name is after.primary_sector_name is None
    assert before.evidence == ()
    assert after.status == "stale"

def test_invalid_evidence_and_missing_catalog_do_not_fall_back(tmp_path):
    invalid=tmp_path/"broken.json"
    invalid.write_text('{"contract":"wrong"}',encoding="utf-8")
    with SmartSectorCatalog(taxonomy_path=tmp_path/"missing.db",evidence_path=invalid,as_of=date(2026,9,18)) as catalog:
        result=catalog.get("002050.SZ")
        assert result.status == "unresolved"
        assert "evidence_store_invalid" in result.flags
    assert not (tmp_path/"missing.db").exists()

def test_gateway_preserves_provenance_and_excludes_industry_outlook():
    def row(kind,text):
        return {"SECUCODE":"002851.SZ","SECURITY_NAME_ABBR":"麦格米特","KEY_CLASSIF":kind,"KEYWORD":kind,"MAINPOINT_CONTENT":text}
    raw={"payload":{"ssbk":[{"SECUCODE":"002851.SZ","SECURITY_NAME_ABBR":"麦格米特","BOARD_NAME":"英伟达概念"}],
                    "hxtc":[row("主营业务","服务器电源"),row("行业背景","全行业人工智能增长")]},
         "source_url":"https://example.test/source","fetched_at":"2026-09-18T10:00:00+08:00"}
    result=map_sector_evidence(raw,"fixture","002851.SZ")
    assert [s.text for s in result.business_sections] == ["服务器电源"]
    assert result.provider_as_of is None
    assert "secondary_business_digest" in result.quality_flags
    with pytest.raises(ValueError,match="identity"):
        map_sector_evidence(raw,"fixture","002050.SZ")

def test_every_market_attribution_consumer_uses_shared_library():
    root=Path(__file__).parents[1]/"src/tradex"
    for consumer in ["stock_selection/industry_display.py","market_watch/limit_up_pool.py","analysis_worker.py",
                     "dashboard/risk_service.py","dashboard/__main__.py","tools/company_info.py",
                     "tools/stock_screening.py","tools/composite_analysis.py","tools/signal_data_base.py",
                     "tools/signal_data_flow.py","market_watch/review_service.py"]:
        assert "tradex.smart_sector_library" in (root/consumer).read_text(encoding="utf-8"), consumer
    display=(root/"stock_selection/industry_display.py").read_text(encoding="utf-8")
    assert "InstrumentTaxonomyReader" not in display and "market_industry" not in display
    assert "current_ths_industry" not in (root/"dashboard/watch/app.js").read_text(encoding="utf-8")


def test_ths_parser_requires_exact_stock_page_and_excludes_other_leaders():
    from tradex.smart_sector_library.research import ths_concepts
    row = {"instrument_id":"002851.SZ", "status":"read", "acquisition":"page",
           "source_url":"https://basic.10jqka.com.cn/002851/concept.html",
           "body":"https://basic.10jqka.com.cn/002851/concept.html\nL26: # 002851\nL71: 1 | 英伟达概念 | 其他龙头甲 乙 | 产品资料\nL72: 公司解释"}
    assert ths_concepts(row) == ["英伟达概念"]
    assert ths_concepts({**row, "acquisition":"search_extract"}) == []
    assert ths_concepts({**row, "instrument_id":"002050.SZ"}) == []


def test_research_publication_cannot_assign_a_primary(tmp_path):
    from tradex.smart_sector_library.research import publish
    root=tmp_path/"research"
    (root/"full_market").mkdir(parents=True)
    (root/"full_market/progress.json").write_text(json.dumps({"instrument_ids":["000001.SZ"]}),encoding="utf-8")
    (root/"full_market/000001.SZ.json").write_text(json.dumps({"collection_status":"collected", "source":{
        "memberships":["银行","互联网金融"], "source_url":"https://example.test/stock", "fetched_at":"2026-09-18T20:00:00+08:00"}}),encoding="utf-8")
    output=tmp_path/"published.json"
    result=publish(root,output)
    assert result["collected"] == 1
    row=json.loads(output.read_text(encoding="utf-8"))["items"]["000001.SZ"]
    assert row["review_status"] == "pending"
    assert "primary_sector_name" not in row
    assert all(c["verified"] is False for c in row["candidate_concepts"])
    (root/"ths-progress.json").write_text(json.dumps({"status":"paused_after_three_unavailable"}),encoding="utf-8")
    paused=publish(root,output)
    assert paused["stage"] == "acquisition_partial_source_paused"
    assert paused["source_states"]["ths"] == "paused_after_three_unavailable"


@pytest.mark.asyncio
async def test_concept_tool_keeps_primary_when_board_quotes_fail(monkeypatch):
    from types import SimpleNamespace
    from mcp.server.fastmcp import FastMCP
    from tradex.tools import signal_data_base
    from tradex.smart_sector_library import catalog
    expected={"status":"unresolved", "instrument_id":"920001.BJ", "primary_sector_name":None}
    class Reader:
        revision="b"*64
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def get(self,key):
            assert key == "920001.BJ"
            return SimpleNamespace(model_dump=lambda **_: expected)
    monkeypatch.setattr(catalog,"SmartSectorCatalog",Reader)
    monkeypatch.setattr(signal_data_base,"cache",SimpleNamespace(get=lambda *_:None,set=lambda *_:None))
    def unavailable(*args,**kwargs): raise RuntimeError("provider offline")
    monkeypatch.setattr(signal_data_base,"_router",SimpleNamespace(route=unavailable))
    mcp=FastMCP("membership-test")
    signal_data_base.register(mcp)
    result=json.loads(await mcp._tool_manager._tools["get_concept_attribution"].fn(symbol="920001"))
    assert result["market_membership"] == expected
    assert result["provider_board_quotes"] is None
    assert result["board_quote_error"] == "RuntimeError"


def test_atomic_research_write_keeps_old_snapshot_during_reader_lock(tmp_path,monkeypatch):
    from tradex.smart_sector_library import storage
    target=tmp_path/"progress.json"
    target.write_text('{"processed":1}',encoding="utf-8")
    replace=storage.os.replace
    attempts=[]
    def locked(source,destination):
        attempts.append(1)
        if len(attempts)<3:
            assert json.loads(target.read_text())["processed"] == 1
            raise PermissionError("reader briefly holds target")
        replace(source,destination)
    monkeypatch.setattr(storage.os,"replace",locked)
    monkeypatch.setattr(storage.time,"sleep",lambda _:None)
    storage.write_json(target,{"processed":2})
    assert len(attempts) == 3
    assert json.loads(target.read_text())["processed"] == 2
