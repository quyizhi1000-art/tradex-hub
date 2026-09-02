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
from tradex.manual_portfolio.intraday_analysis import (
    build_manual_portfolio_intraday_analysis,
)
from tradex.manual_portfolio.outlook import build_manual_portfolio_outlook
from tradex.manual_portfolio.readiness import portfolio_outlook_readiness
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


def test_outlook_request_persists_until_collector_dispatch(tmp_path):
    path = tmp_path / "portfolio.sqlite3"
    with ManualPortfolioStore(path) as store:
        revision = "a" * 64
        request = store.request_outlook(revision, requested_at=NOW)

        assert request["state"] == "waiting_for_market"
        assert request["job_id"] is None
        dispatched = store.mark_outlook_request_dispatched(
            revision,
            job_id="manual_portfolio_outlook:fixture",
            dispatched_at=NOW.replace(minute=3),
        )

    assert dispatched["state"] == "dispatched"
    assert dispatched["job_id"] == "manual_portfolio_outlook:fixture"
    with ManualPortfolioReader(path) as reader:
        assert reader.outlook_request(revision) == dispatched


def test_outlook_readiness_services_a_post_close_request_immediately():
    observed = datetime(2026, 9, 1, 23, 30, tzinfo=SHANGHAI)
    readiness = portfolio_outlook_readiness(
        portfolio_revision="a" * 64,
        enabled_count=2,
        snapshot=None,
        now=observed,
        automatic_generation_requested=True,
    )

    assert readiness.state == "waiting_for_market"
    assert readiness.next_collection_at == observed
    assert readiness.automatic_generation_requested is True


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

    outlook = build_manual_portfolio_outlook(
        snapshot,
        generated_at=NOW,
        market_context={
            "source_trade_date": "2026-08-31",
            "bias": "balanced",
            "confidence": "weak",
            "thesis": "指数与个股分化，次日先按震荡轮动观察。",
            "expected_shape": "机会更可能集中在局部方向。",
            "confirmation": "指数、广度、成交至少三项连续同向。",
            "invalidation": "价格、广度与资金持续背离。",
            "risk_control": "等待盘中条件确认。",
        },
    )

    assert outlook.contract == "manual_portfolio_outlook.v1"
    assert outlook.source_trading_date == date(2026, 8, 31)
    assert outlook.deterministic_price_prediction is False
    by_id = {item.instrument_id: item for item in outlook.items}
    assert by_id["000001.SZ"].status == "conditional"
    assert by_id["000001.SZ"].confirmation_conditions
    assert by_id["600000.SH"].status == "abstain"
    assert "不形成方向性前瞻" in by_id["600000.SH"].next_session


def test_outlook_uses_complete_price_path_without_claiming_missing_amount_evidence(
    tmp_path,
):
    with ManualPortfolioStore(tmp_path / "portfolio.sqlite3") as store:
        _service(store).add("000001", now=NOW)
        snapshot = refresh_manual_portfolio_market(
            store,
            now=NOW,
            fetcher=lambda *_args, **_kwargs: {
                "000001.SZ": _series(
                    quality=QualityStatus.DEGRADED,
                    prices=(10.0, 10.2),
                ).model_copy(
                    update={
                        "metadata": _series().metadata.model_copy(
                            update={
                                "quality": QualityStatus.DEGRADED,
                                "quality_flags": (
                                    "amount_partial",
                                    "cumulative_average_partial",
                                ),
                            }
                        )
                    }
                )
            },
        )

    outlook = build_manual_portfolio_outlook(
        snapshot,
        generated_at=NOW,
        market_context={
            "source_trade_date": "2026-08-31",
            "bias": "balanced",
            "confidence": "weak",
            "thesis": "指数与个股分化，次日先按震荡轮动观察。",
            "expected_shape": "机会更可能集中在局部方向。",
            "confirmation": "指数、广度、成交至少三项连续同向。",
            "invalidation": "价格、广度与资金持续背离。",
            "risk_control": "等待盘中条件确认。",
        },
    )

    assert outlook.items[0].status == "conditional"
    assert outlook.items[0].evidence_status == "degraded"
    assert "偏强收尾" in outlook.items[0].next_session
    assert any("不作量价判断" in item for item in outlook.items[0].limitations)
    assert any("价格样本" in item for item in outlook.items[0].confirmation_conditions)
    assert any("价格路径" in item for item in outlook.items[0].invalidation_conditions)
    assert outlook.market_context.bias == "balanced"
    assert outlook.items[0].price_plan is not None
    assert outlook.items[0].price_plan.deterministic_target is False
    assert outlook.items[0].price_plan.pullback_observation_zone == (10.1, 10.13)
    assert outlook.items[0].price_plan.pressure_observation_zone == (10.16, 10.2)
    assert len(outlook.items[0].opening_scenarios) == 3
    assert len(outlook.items[0].market_scenarios) == 3


def test_intraday_analysis_is_separate_from_next_session_outlook(tmp_path):
    with ManualPortfolioStore(tmp_path / "portfolio.sqlite3") as store:
        _service(store).add("000001", now=NOW)
        snapshot = refresh_manual_portfolio_market(
            store,
            now=NOW,
            fetcher=lambda *_args, **_kwargs: {
                "000001.SZ": _series(prices=(10.0, 10.2))
            },
        )

    analysis = build_manual_portfolio_intraday_analysis(snapshot, generated_at=NOW)

    assert analysis.contract == "manual_portfolio_intraday_analysis.v1"
    assert analysis.analysis_scope == "current_session"
    assert analysis.items[0].status == "conditional"
    assert "盘中" in analysis.items[0].current_observation
    assert any("不替代昨晚" in item for item in analysis.items[0].limitations)
