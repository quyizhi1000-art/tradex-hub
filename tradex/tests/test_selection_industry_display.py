from datetime import date
from types import SimpleNamespace

from tradex.stock_selection import industry_display


def profile(leaf, block="小金属", **path_fields):
    return SimpleNamespace(as_of=date(2026, 9, 16), market_industry=SimpleNamespace(
        taxonomy="ths", level1_name=block, level3_name=leaf,
        effective_from=path_fields.get("effective_from"),
        effective_to=path_fields.get("effective_to"),
    ))


def reader_fixture(monkeypatch, profiles, *, available=True, changes_revision=False):
    status = SimpleNamespace(as_of=date(2026, 9, 16), catalog_revision="a" * 64)

    class Reader:
        calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def status(self):
            self.calls += 1
            return (status if self.calls == 1 or not changes_revision else None) if available else None

        def get_many(self, ids):
            return {key: profiles[key] for key in ids if key in profiles}

    monkeypatch.setattr(industry_display, "InstrumentTaxonomyReader", Reader)


def test_both_user_examples_use_ths_blocks_without_changing_leaf_evidence(monkeypatch):
    profiles = {"002378.SZ": profile("钨"), "001280.SZ": profile("稀土")}
    reader_fixture(monkeypatch, profiles)
    result = industry_display.load_selection_industry_display(profiles)
    assert result.names_by_instrument == {"001280.SZ": "小金属", "002378.SZ": "小金属"}
    assert result.quality == "accepted"
    assert result.as_of == date(2026, 9, 16)
    assert profiles["002378.SZ"].market_industry.level3_name == "钨"


def test_saic_uses_market_block_instead_of_statistical_subdivision(monkeypatch):
    saic = profile("综合乘用车", "汽车整车")
    saic.statistical_industry = SimpleNamespace(taxonomy="sw", level2_name="乘用车")
    reader_fixture(monkeypatch, {"600104.SH": saic})
    result = industry_display.load_selection_industry_display(["600104.SH"])
    assert result.basis == "current_ths_industry"
    assert result.names_by_instrument == {"600104.SH": "汽车整车"}


def test_missing_or_inactive_l2_does_not_fall_back_to_fine_industry(monkeypatch):
    profiles = {"002378.SZ": profile("钨"), "001280.SZ": profile("稀土", None),
                "600001.SH": profile("稀土", effective_to=date(2026, 9, 15)),
                "600002.SH": profile("稀土", effective_from=date(2026, 9, 17))}
    reader_fixture(monkeypatch, profiles)
    result = industry_display.load_selection_industry_display([*profiles, "600003.SH"])
    assert result.names_by_instrument == {"002378.SZ": "小金属"}
    assert len(result.unclassified_instruments) == 4
    assert result.quality == "degraded"


def test_absent_or_concurrently_changed_catalog_fails_closed(monkeypatch):
    for kwargs in ({"available": False}, {"changes_revision": True}):
        reader_fixture(monkeypatch, {"002378.SZ": profile("钨")}, **kwargs)
        result = industry_display.load_selection_industry_display(["002378.SZ"])
        assert result.quality == "unavailable"
        assert result.names_by_instrument == {}
        assert result.unclassified_instruments == ("002378.SZ",)
