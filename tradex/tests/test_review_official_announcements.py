from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from astock_signals.smart_router import SmartRouter
from tradex.data_gateway.review_announcement_contracts import (
    ReviewAnnouncementCandidateManifestV1,
    ReviewAnnouncementCandidateV1,
)
from tradex.data_gateway.review_announcements import (
    fetch_review_official_announcements,
)
from tradex.data_sources import news_fetchers
from tradex.market_watch.review_announcement_candidates import (
    NoReviewAnnouncementCandidates,
    manifest_from_review_artifact,
)
from tradex.market_watch.review_announcement_store import (
    ReviewOfficialAnnouncementStore,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 29, 8, 30, tzinfo=SHANGHAI)


def _manifest() -> ReviewAnnouncementCandidateManifestV1:
    return ReviewAnnouncementCandidateManifestV1(
        trade_date=date(2026, 8, 28),
        review_id="pmr-2026-08-28-fixture",
        review_generated_at=datetime(2026, 8, 28, 21, 5, tzinfo=SHANGHAI),
        source_artifact_revision="a" * 64,
        manifest_revision="b" * 64,
        candidates=(
            ReviewAnnouncementCandidateV1(
                instrument_id="002466.SZ",
                name="天齐锂业",
                watch_item_rank=5,
                watch_item_title="主板条件观察标的",
            ),
        ),
    )


def _provider_payload():
    return {
        "provider": "cninfo",
        "start_date": "2026-08-28",
        "end_date": "2026-08-29",
        "candidates": [item.model_dump(mode="json") for item in _manifest().candidates],
        "announcements": [
            {
                "announcement_id": "1225522279",
                "instrument_id": "002466.SZ",
                "name": "天齐锂业",
                "title": "关于半年度报告的公告",
                "published_at": "2026-08-28T00:00:00+08:00",
                "publication_precision": "date",
                "announcement_type": None,
                "source_url": "https://static.cninfo.com.cn/finalpage/2026-08-28/1225522279.PDF",
                "source": "cninfo",
            }
        ],
    }


def test_manifest_uses_only_materialized_review_watch_stocks():
    artifact = {
        "contract": "tradex_analysis_artifact.v1",
        "source_revision": "c" * 64,
        "payload": {
            "review": {
                "trade_date": "2026-08-28",
                "review_id": "pmr-2026-08-28-fixture",
                "generated_at": "2026-08-28T21:05:00+08:00",
            },
            "presentation": {
                "watch_items": [
                    {"rank": 1, "title": "大盘", "stocks": []},
                    {
                        "rank": 5,
                        "title": "主板条件观察标的",
                        "stocks": [
                            {"instrument_id": "002466.SZ", "name": "天齐锂业"},
                            {"instrument_id": "002466.SZ", "name": "重复项"},
                        ],
                    },
                ]
            },
        },
    }

    manifest = manifest_from_review_artifact(artifact)

    assert manifest.trade_date == date(2026, 8, 28)
    assert manifest.source_artifact_revision == "c" * 64
    assert [item.instrument_id for item in manifest.candidates] == ["002466.SZ"]
    assert len(manifest.manifest_revision) == 64


def test_manifest_reports_valid_review_without_stock_candidates():
    artifact = {
        "contract": "tradex_analysis_artifact.v1",
        "source_revision": "c" * 64,
        "payload": {
            "review": {
                "trade_date": "2026-08-28",
                "review_id": "pmr-2026-08-28-fixture",
                "generated_at": "2026-08-28T21:05:00+08:00",
            },
            "presentation": {"watch_items": []},
        },
    }

    with pytest.raises(NoReviewAnnouncementCandidates):
        manifest_from_review_artifact(artifact)


def test_cninfo_fetcher_is_candidate_and_date_bounded(monkeypatch):
    calls = []
    monkeypatch.setattr(news_fetchers, "_resolve_org_id_strict", lambda code: "org-1")
    monkeypatch.setattr(news_fetchers, "wait_for_free_source", lambda *args, **kwargs: None)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "totalpages": 1,
                "announcements": [
                    {
                        "announcementId": "1225522279",
                        "announcementTitle": "<em>半年度报告</em>",
                        "announcementTime": 1787846400000,
                        "announcementTypeName": None,
                        "adjunctUrl": "finalpage/2026-08-28/1225522279.PDF",
                        "secCode": "002466",
                        "secName": "天齐锂业",
                    },
                    {
                        "announcementId": "ignored",
                        "announcementTitle": "错误股票",
                        "announcementTime": 1787846400000,
                        "adjunctUrl": "ignored.pdf",
                        "secCode": "000001",
                        "secName": "错误股票",
                    },
                ],
            }

    def fake_post(url, *, data, headers, timeout):
        calls.append((url, dict(data)))
        return Response()

    monkeypatch.setattr(news_fetchers.curl_requests, "post", fake_post)

    payload = news_fetchers.fetch_cninfo_candidate_announcements(
        candidates=[item.model_dump(mode="json") for item in _manifest().candidates],
        start_date="2026-08-28",
        end_date="2026-08-29",
    )

    assert len(calls) == 1
    assert calls[0][1]["stock"] == "002466,org-1"
    assert calls[0][1]["seDate"] == "2026-08-28~2026-08-29"
    assert [item["announcement_id"] for item in payload["announcements"]] == [
        "1225522279"
    ]
    assert payload["announcements"][0]["title"] == "半年度报告"


def test_cninfo_fetcher_accepts_explicit_zero_result(monkeypatch):
    monkeypatch.setattr(news_fetchers, "_resolve_org_id_strict", lambda code: "org-1")
    monkeypatch.setattr(news_fetchers, "wait_for_free_source", lambda *args, **kwargs: None)

    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {
            "totalAnnouncement": 0,
            "totalpages": 0,
            "announcements": None,
        },
    )
    monkeypatch.setattr(
        news_fetchers.curl_requests,
        "post",
        lambda *args, **kwargs: response,
    )

    payload = news_fetchers.fetch_cninfo_candidate_announcements(
        candidates=[item.model_dump(mode="json") for item in _manifest().candidates],
        start_date="2026-08-28",
        end_date="2026-08-29",
    )

    assert payload["announcements"] == []


def test_gateway_and_store_keep_official_archive_immutable(tmp_path: Path):
    router = SmartRouter()
    router.register(
        "review_candidate_announcements",
        "cninfo",
        lambda **kwargs: _provider_payload(),
        priority=1,
    )
    archive = fetch_review_official_announcements(
        _manifest(),
        window_end=date(2026, 8, 29),
        router=router,
        now=NOW,
    )
    db_path = tmp_path / "official.sqlite3"
    reader = ReviewOfficialAnnouncementStore(db_path, read_only=True)
    assert reader.get("2026-08-28") is None
    with ReviewOfficialAnnouncementStore(db_path) as writer:
        first = writer.record(archive)
        second = writer.record(archive)

    assert first["action"] == "inserted"
    assert second["action"] == "existing"
    loaded = reader.get("2026-08-28")
    reader.close()
    assert loaded is not None
    assert loaded.metadata.provider == "cninfo"
    assert loaded.announcements[0].title == "关于半年度报告的公告"
    assert loaded.source_revision == archive.source_revision


def test_gateway_rejects_non_candidate_announcement():
    payload = _provider_payload()
    payload["announcements"][0]["instrument_id"] = "000001.SZ"
    router = SmartRouter()
    router.register(
        "review_candidate_announcements",
        "cninfo",
        lambda **kwargs: payload,
        priority=1,
    )

    with pytest.raises(Exception, match="non-candidate"):
        fetch_review_official_announcements(
            _manifest(),
            window_end=date(2026, 8, 29),
            router=router,
            now=NOW,
        )
