from datetime import date
from types import SimpleNamespace
from tradex.stock_selection import industry_display


def test_display_only_publishes_one_verified_primary_and_no_fallback(monkeypatch):
    rows = {
        "002050.SZ": SimpleNamespace(status="verified", primary_sector_name="人形机器人", tags=("液冷",)),
        "002851.SZ": SimpleNamespace(status="verified", primary_sector_name="AI算力", tags=("英伟达",)),
        "600001.SH": SimpleNamespace(status="unresolved", primary_sector_name=None, provider_industry="银行"),
        "600002.SH": SimpleNamespace(status="stale", primary_sector_name=None),
    }
    class Reader:
        revision = "a" * 64
        as_of = date(2026, 9, 18)
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def get_many(self, ids): return {key: rows[key] for key in ids}
    monkeypatch.setattr(industry_display, "SmartSectorCatalog", Reader)
    result = industry_display.load_selection_industry_display(rows)
    assert result.basis == "smart_sector_library"
    assert result.names_by_instrument == {"002050.SZ": "人形机器人", "002851.SZ": "AI算力"}
    assert result.unclassified_instruments == ("600001.SH", "600002.SH")
    assert result.quality == "degraded"
    assert result.catalog_revision == "a" * 64
