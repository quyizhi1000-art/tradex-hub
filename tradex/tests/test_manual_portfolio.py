from __future__ import annotations

from datetime import date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tradex.data_gateway.contracts import (
    ContractMetadata,
    IntradayMinutePointV1,
    IntradayMinuteSeriesV1,
    QualityStatus,
)
from tradex.data_gateway.intraday import MAX_INTRADAY_BATCH_INSTRUMENTS
from tradex.manual_portfolio.contracts import MAX_ENABLED_INSTRUMENTS
from tradex.manual_portfolio.market import refresh_manual_portfolio_market
from tradex.manual_portfolio.outlook import build_manual_portfolio_outlook
from tradex.manual_portfolio.service import ManualPortfolioService
from tradex.manual_portfolio.store import ManualPortfolioReader, ManualPortfolioStore


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 31, 10, 2, tzinfo=SHANGHAI)


def test_enabled_limit_matches_the_single_audited_intraday_batch():
    assert MAX_ENABLED_INSTRUMENTS == MAX_INTRADAY_BATCH_INSTRUMENTS == 40


class _TaxonomyReader:
    def __init__(self, profiles=None):
        self.profiles = profiles or {}

    def get(self, instrument_id):
        return self.profiles.get(instrument_id)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


def _service(store, profiles=None):
    return ManualPortfolioService(
        store,
        taxonomy_reader_factory=lambda: _TaxonomyReader(profiles),
    )


def _series(
    instrument_id="000001.SZ",
    *,
    quality=QualityStatus.ACCEPTED,
    provider_as_of=NOW,
    prices=(10.0, 10.1),
):
    return IntradayMinuteSeriesV1(
        metadata=ContractMetadata(
            contract="intraday_minute_series.v1",
            provider="fixture",
            provider_request_id="request-1",
            provider_as_of=provider_as_of,
            fetched_at=NOW,
            quality=quality,
            quality_flags=() if quality is QualityStatus.ACCEPTED else ("amount_partial",),
        ),
        instrument_id=instrument_id,
        trading_date=date(2026, 8, 31),
        points=tuple(
            IntradayMinutePointV1(
                minute=time(10, index),
                price=price,
                cumulative_average_price=price,
                volume_shares=1000,
                amount_cny=price * 1000,
                open=price,
                high=price,
                low=price,
            )
            for index, price in enumerate(prices)
        ),
    )


def test_manual_portfolio_normalizes_deduplicates_and_keeps_attribution_separate(tmp_path):
    profile = SimpleNamespace(name="平安银行", verification_status="provider_only")
    with ManualPortfolioStore(tmp_path / "portfolio.sqlite3") as store:
        service = _service(store, {"000001.SZ": profile})
        added = service.add("sz000001", note="观察", now=NOW)

        assert added.instrument_id == "000001.SZ"
        assert added.display_name == "平安银行"
        assert added.code_validation_status == "catalog_verified"
        assert added.attribution_status == "provider_only"
        with pytest.raises(ValueError, match="已在持仓观察"):
            service.add("000001.SZ", now=NOW)
        with pytest.raises(ValueError, match="交易所后缀"):
            service.add("600000.SZ", now=NOW)

        unclassified = service.add("600000", display_name="浦发银行", now=NOW)
        assert unclassified.code_validation_status == "format_valid_unverified"
        assert unclassified.attribution_status == "not_available"


def test_manual_portfolio_enforces_40_enabled_but_allows_disabled_entries(tmp_path):
    with ManualPortfolioStore(tmp_path / "portfolio.sqlite3") as store:
        service = _service(store)
        for number in range(1, 41):
            service.add(f"{number:06d}.SZ", now=NOW)
        with pytest.raises(ValueError, match="40"):
            service.add("600000.SH", now=NOW)
        disabled = service.add("600001.SH", enabled=False, now=NOW)
        assert disabled.enabled is False
        with pytest.raises(ValueError, match="40"):
            service.update(disabled.instrument_id, enabled=True, now=NOW)


def test_update_and_delete_require_an_existing_explicit_instrument(tmp_path):
    path = tmp_path / "portfolio.sqlite3"
    with ManualPortfolioStore(path) as store:
        service = _service(store)
        original = service.add("000001", note="旧备注", now=NOW)
        updated = service.update("000001.SZ", note="新备注", enabled=False, now=NOW)
        assert updated.added_at == original.added_at
        assert updated.note == "新备注"
        assert updated.enabled is False
        with pytest.raises(LookupError):
            service.update("600000", note="不存在", now=NOW)
        assert service.delete("000001") is True
        assert service.delete("000001") is False

    with ManualPortfolioReader(path) as reader:
        assert reader.list_entries() == ()


def test_long_running_reader_attaches_after_first_manual_write(tmp_path):
    path = tmp_path / "portfolio.sqlite3"
    reader = ManualPortfolioReader(path)
    assert reader.available is False

    with ManualPortfolioStore(path) as store:
        _service(store).add("000001", now=NOW)

    try:
        assert reader.available is True
        assert [item.instrument_id for item in reader.list_entries()] == ["000001.SZ"]
    finally:
        reader.close()


def test_partial_batch_materializes_missing_item_without_deleting_it(tmp_path):
    with ManualPortfolioStore(tmp_path / "portfolio.sqlite3") as store:
        service = _service(store)
        service.add("000001", now=NOW)
        service.add("600000", now=NOW)

        snapshot = refresh_manual_portfolio_market(
            store,
            now=NOW,
            fetcher=lambda symbols, **_kwargs: {"000001.SZ": _series()},
        )

        assert snapshot.item_count == 2
        by_id = {item.instrument_id: item for item in snapshot.items}
        assert by_id["000001.SZ"].status == "accepted"
        assert by_id["600000.SH"].status == "unavailable"
        assert "未返回" in by_id["600000.SH"].reason
        assert len(store.list_entries()) == 2


def test_alert_requires_two_fresh_accepted_samples_and_honors_cooldown(tmp_path):
    with ManualPortfolioStore(tmp_path / "portfolio.sqlite3") as store:
        _service(store).add("000001", now=NOW)
        fetcher = lambda *_args, **_kwargs: {"000001.SZ": _series(prices=(10.0, 10.2))}

        first = refresh_manual_portfolio_market(store, now=NOW, fetcher=fetcher)
        second = refresh_manual_portfolio_market(
            store, now=NOW.replace(minute=3), fetcher=fetcher
        )

        assert len(first.alerts) == 1
        assert first.alerts[0].observation_only is True
        assert len(first.alerts[0].evidence) == 2
        assert first.alerts[0].invalidation_condition
        assert second.alerts == ()


@pytest.mark.parametrize("quality", [QualityStatus.DEGRADED])
def test_degraded_or_stale_quality_suppresses_directional_alerts(tmp_path, quality):
    with ManualPortfolioStore(tmp_path / "portfolio.sqlite3") as store:
        _service(store).add("000001", now=NOW)
        degraded = refresh_manual_portfolio_market(
            store,
            now=NOW,
            fetcher=lambda *_args, **_kwargs: {
                "000001.SZ": _series(quality=quality, prices=(10.0, 10.2))
            },
        )
        stale = refresh_manual_portfolio_market(
            store,
            now=NOW,
            fetcher=lambda *_args, **_kwargs: {
                "000001.SZ": _series(
                    provider_as_of=NOW.replace(hour=9, minute=40), prices=(10.0, 10.2)
                )
            },
        )

        assert degraded.items[0].status == "degraded"
        assert degraded.alerts == ()
        assert stale.items[0].status == "stale"
        assert stale.alerts == ()


def test_fresh_provider_metadata_cannot_hide_stale_price_samples(tmp_path):
    later = NOW.replace(minute=10)
    with ManualPortfolioStore(tmp_path / "portfolio.sqlite3") as store:
        _service(store).add("000001", now=NOW)
        snapshot = refresh_manual_portfolio_market(
            store,
            now=later,
            fetcher=lambda *_args, **_kwargs: {
                "000001.SZ": _series(provider_as_of=later, prices=(10.0, 10.2))
            },
        )

    assert snapshot.items[0].status == "stale"
    assert "sample_timestamp_stale" in snapshot.items[0].quality_flags
    assert snapshot.alerts == ()


def test_outlook_is_conditional_and_abstains_on_unavailable_evidence(tmp_path):
    with ManualPortfolioStore(tmp_path / "portfolio.sqlite3") as store:
        service = _service(store)
        service.add("000001", now=NOW)
        service.add("600000", now=NOW)
        snapshot = refresh_manual_portfolio_market(
            store,
            now=NOW,
            fetcher=lambda *_args, **_kwargs: {"000001.SZ": _series(prices=(10.0, 10.2))},
        )

    outlook = build_manual_portfolio_outlook(snapshot, generated_at=NOW)

    assert outlook.contract == "manual_portfolio_outlook.v1"
    assert outlook.source_trading_date == date(2026, 8, 31)
    assert outlook.deterministic_price_prediction is False
    by_id = {item.instrument_id: item for item in outlook.items}
    assert by_id["000001.SZ"].status == "conditional"
    assert by_id["000001.SZ"].confirmation_conditions
    assert by_id["600000.SH"].status == "abstain"
    assert "不形成方向性前瞻" in by_id["600000.SH"].next_session
