from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from tradex.market_watch import (
    EvidenceStrength,
    FreshnessStatus,
    GuardrailSeverity,
    IndexRole,
    MarketPhase,
    MarketRegime,
    MarketWatchSnapshotV1,
    SectorFlowTrajectoryV1,
    TurnoverDirection,
    build_market_watch_snapshot,
    classify_rotation,
    classify_turnover,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 24, 10, 30, tzinfo=SHANGHAI)


def _indices(
    changes: tuple[float, float, float, float],
    *,
    provider_as_of: datetime = NOW,
    small_cap_id: str = "000852.SH",
) -> list[dict]:
    specs = (
        ("broad_market", "000001.SH", "上证指数", 3400.0),
        ("large_cap", "000300.SH", "沪深300", 4100.0),
        ("small_cap", small_cap_id, "中证1000", 6800.0),
        ("growth", "399006.SZ", "创业板指", 2250.0),
    )
    return [
        {
            "role": role,
            "instrument_id": instrument_id,
            "name": name,
            "available": True,
            "level": level,
            "change_pct": change,
            "provider_as_of": provider_as_of.isoformat(),
            "quality": "accepted",
        }
        for (role, instrument_id, name, level), change in zip(specs, changes, strict=True)
    ]


def _counts(ratio: float, total: int = 5000) -> tuple[int, int]:
    up = round(total * ratio)
    return up, total - up


def _inputs(
    *,
    changes: tuple[float, float, float, float],
    breadth_ratio: float,
    today_amount: float,
    previous_amount: float = 100_000_000_000.0,
    sectors: list[dict],
    provider_as_of: datetime = NOW,
    small_cap_id: str = "000852.SH",
) -> tuple[dict, dict]:
    up, down = _counts(breadth_ratio)
    market = {
        "timestamp": NOW.isoformat(),
        "provider_as_of": provider_as_of.isoformat(),
        "market_state": {"phase": "trading", "is_open": True},
        "quality": "accepted",
        "indices": _indices(
            changes,
            provider_as_of=provider_as_of,
            small_cap_id=small_cap_id,
        ),
        "market_turnover": {
            "available": True,
            "today_date": "2026-08-24",
            "previous_date": "2026-08-21",
            "as_of": "10:30",
            "today_amount": today_amount,
            "previous_same_time_amount": previous_amount,
        },
    }
    risk = {
        "timestamp": NOW.isoformat(),
        "breadth": {
            "up_count": up,
            "down_count": down,
            "flat_count": 0,
            "unclassified_count": 0,
            "total_count": up + down,
            "provider_as_of": provider_as_of.isoformat(),
            "quality": "accepted",
        },
        "rotation": {"sectors": sectors},
    }
    return market, risk


def _sector(
    key: str,
    name: str,
    tags: list[str],
    change_pct: float,
    breadth_ratio: float,
    flow: float,
    *,
    provider_as_of: datetime = NOW,
) -> dict:
    return {
        "sector_key": key,
        "name": name,
        "tags": tags,
        "change_pct": change_pct,
        "breadth_ratio": breadth_ratio,
        "main_net_inflow_cny": flow,
        "provider_as_of": provider_as_of.isoformat(),
    }


def _sector_flow_trajectory(*, direction: str = "defense") -> dict:
    point = {
        "sampled_at": NOW.isoformat(),
        "provider_as_of": NOW.isoformat(),
        "session_segment": "am",
        "cumulative_cny": 600_000_000.0,
        "delta_5m_cny": None,
        "delta_5m_baseline_as_of": None,
    }
    return {
        "contract": "sector_flow_trajectory.v1",
        "schema_version": 1,
        "direction": direction,
        "status": "collecting",
        "trade_date": NOW.date().isoformat(),
        "as_of": NOW.isoformat(),
        "market_phase": "trading",
        "trajectory_scope": "trading_session_to_as_of",
        "marginal_window_minutes": 5,
        "sectors": [{
            "sector_key": "electric_power" if direction == "defense" else "semiconductor",
            "name": "电力" if direction == "defense" else "半导体",
            "category_key": "steady_defense" if direction == "defense" else "technology_growth",
            "category_name": "稳态防御" if direction == "defense" else "科技成长",
            "taxonomy": "industry",
            "leader_board_code": "BK0428",
            "status": "collecting",
            "follow_eligible": True,
            "eligible_for_rank": True,
            "observation_rank": 1,
            "rank_total": 1,
            "observation_tier": "strong_pending",
            "tier_label": "强势待确认",
            "latest": {
                "provider_as_of": NOW.isoformat(),
                "change_pct": 1.2,
                "breadth_ratio": 0.68,
                "cumulative_cny": 600_000_000.0,
                "main_net_inflow_pct": 2.4,
                "price_percentile": 0.88,
                "flow_percentile": 0.82,
                "delta_5m_cny": None,
                "delta_10m_cny": None,
                "delta_5m_baseline_as_of": None,
                "delta_10m_baseline_as_of": None,
                "current_strength": "strong",
                "fund_strength": "strong",
                "incremental_direction": "unknown",
            },
            "leader_snapshot": {
                "status": "full",
                "status_label": "领涨股已更新",
                "source": "push2",
                "provider_as_of": NOW.isoformat(),
                "stale": False,
                "refreshing": False,
                "leaders": [
                    {
                        "instrument_id": "600900.SH",
                        "name": "长江电力",
                        "change_pct": 2.8,
                        "price": 30.0,
                        "provider_as_of": NOW.isoformat(),
                    },
                    {
                        "instrument_id": "600011.SH",
                        "name": "华能国际",
                        "change_pct": 2.2,
                        "price": 8.0,
                        "provider_as_of": NOW.isoformat(),
                    },
                ],
            },
            "points": [point],
            "supporting_evidence": ["当日累计估算净流入"],
            "counter_evidence": ["近5分钟同源基线仍在积累"],
            "flags": ["five_minute_baseline_collecting"],
            "reason": None,
        }],
        "flags": ["provider_estimated_flow"],
        "reason": None,
    }


def _build(market: dict, risk: dict, *, snapshot_id: str = "fixture-1"):
    return build_market_watch_snapshot(
        market,
        risk,
        as_of=NOW,
        sequence=1,
        snapshot_id=snapshot_id,
    )


def test_replay_broad_rally_with_expanding_turnover_is_confirmed_attack() -> None:
    market, risk = _inputs(
        changes=(0.9, 1.0, 1.6, 1.8),
        breadth_ratio=0.68,
        today_amount=112_000_000_000.0,
        sectors=[_sector("industry:证券", "证券", ["attack"], 2.4, 0.74, 8e9)],
    )

    snapshot = _build(market, risk)

    assert snapshot.freshness.status == FreshnessStatus.FRESH
    assert snapshot.guardrail.regime == MarketRegime.ATTACK
    assert snapshot.guardrail.severity == GuardrailSeverity.CALM
    assert snapshot.turnover.direction == TurnoverDirection.EXPAND
    assert snapshot.rotation.sectors[0].evidence_strength == EvidenceStrength.STRONG


def test_replay_index_up_while_breadth_narrows_triggers_divergence_brake() -> None:
    market, risk = _inputs(
        changes=(0.8, 1.1, -0.6, -0.9),
        breadth_ratio=0.43,
        today_amount=101_000_000_000.0,
        sectors=[
            _sector("industry:银行", "银行", ["weight_support"], 1.1, 0.66, 5e9)
        ],
    )

    snapshot = _build(market, risk)

    assert snapshot.guardrail.regime == MarketRegime.MIXED
    assert snapshot.guardrail.severity == GuardrailSeverity.CAUTION
    assert "结构分化" in snapshot.guardrail.current_state
    assert snapshot.turnover.direction == TurnoverDirection.FLAT


def test_replay_shrinking_turnover_and_defense_leadership_is_defensive() -> None:
    market, risk = _inputs(
        changes=(-0.2, 0.1, -1.2, -1.5),
        breadth_ratio=0.37,
        today_amount=88_000_000_000.0,
        sectors=[
            _sector("industry:电力", "电力", ["defense"], 1.6, 0.72, 4e9),
            _sector("concept:黄金", "黄金", ["safe_haven", "event"], 1.3, 0.68, 3e9),
        ],
    )

    snapshot = _build(market, risk)

    assert snapshot.guardrail.regime == MarketRegime.DEFENSE
    assert snapshot.guardrail.severity == GuardrailSeverity.CAUTION
    assert snapshot.turnover.direction == TurnoverDirection.SHRINK
    assert any("成交额" in item for item in snapshot.guardrail.counter_evidence)


def test_replay_sector_rotation_keeps_multi_labels_and_mixed_regime() -> None:
    market, risk = _inputs(
        changes=(0.3, 0.4, 0.2, 0.3),
        breadth_ratio=0.54,
        today_amount=104_000_000_000.0,
        sectors=[
            _sector("industry:证券", "证券", ["attack"], 1.5, 0.67, 5e9),
            _sector(
                "concept:贵金属",
                "贵金属",
                ["safe_haven", "event"],
                1.2,
                0.65,
                4e9,
            ),
            _sector("industry:有色", "有色", ["cyclical"], 0.9, 0.61, 2e9),
        ],
    )

    snapshot = _build(market, risk)

    assert snapshot.rotation.regime == MarketRegime.MIXED
    assert snapshot.guardrail.regime == MarketRegime.MIXED
    assert {tag.value for tag in snapshot.rotation.sectors[1].tags} == {
        "safe_haven",
        "event",
    }
    assert len(snapshot.scenarios) == 2


def test_existing_risk_view_keys_map_to_multi_label_taxonomy() -> None:
    rotation = classify_rotation([
        {
            "key": "securities",
            "label": "证券",
            "change_pct": 1.2,
            "breadth_ratio": 0.63,
        },
        {
            "key": "bank",
            "label": "银行",
            "change_pct": 0.9,
            "breadth_ratio": 0.61,
        },
        {
            "key": "precious_metals",
            "label": "贵金属",
            "change_pct": 1.1,
            "breadth_ratio": 0.66,
        },
    ])

    tags = {item.sector_key: {tag.value for tag in item.tags} for item in rotation.sectors}
    assert tags["securities"] == {"attack"}
    assert tags["bank"] == {"defense", "weight_support"}
    assert tags["precious_metals"] == {"safe_haven", "event"}


def test_rotation_deduplicates_sector_during_lifecycle_transition() -> None:
    rotation = classify_rotation([
        {
            "key": "advanced_packaging",
            "label": "先进封装",
            "change_pct": 1.2,
            "breadth_ratio": 0.63,
        },
        {
            "key": "advanced_packaging",
            "label": "先进封装（旧分组）",
            "change_pct": -0.5,
            "breadth_ratio": 0.42,
        },
    ])

    assert len(rotation.sectors) == 1
    assert rotation.sectors[0].name == "先进封装"
    assert rotation.sectors[0].direction.value == "strengthening"


def test_existing_dashboard_group_shape_normalizes_before_the_v1_contract() -> None:
    market, risk = _inputs(
        changes=(0.5, 0.6, 0.9, 1.0),
        breadth_ratio=0.61,
        today_amount=108_000_000_000.0,
        sectors=[],
    )
    risk.pop("rotation")
    risk["groups"] = [{
        "key": "financial",
        "label": "金融板块",
        "items": [{
            "key": "securities",
            "label": "证券",
            "change_pct": 1.3,
            "breadth_ratio": 0.66,
            "flow_pct": 0.08,
        }],
    }]

    snapshot = _build(market, risk)

    assert snapshot.rotation.sectors[0].sector_key == "securities"
    assert {tag.value for tag in snapshot.rotation.sectors[0].tags} == {"attack"}
    # The legacy flow-percent display field is intentionally not interpreted
    # as a canonical CNY amount.
    assert snapshot.rotation.sectors[0].main_net_inflow_cny is None


def test_replay_old_provider_dates_force_unknown_stop_and_unconfirmed_session() -> None:
    old = datetime(2026, 8, 21, 15, 0, tzinfo=SHANGHAI)
    market, risk = _inputs(
        changes=(0.5, 0.6, 0.8, 0.9),
        breadth_ratio=0.60,
        today_amount=110_000_000_000.0,
        sectors=[
            _sector(
                "industry:证券",
                "证券",
                ["attack"],
                1.2,
                0.65,
                5e9,
                provider_as_of=old,
            )
        ],
        provider_as_of=old,
    )
    risk["breadth"]["provider_as_of"] = old.isoformat()

    snapshot = _build(market, risk)

    assert snapshot.freshness.status == FreshnessStatus.STALE
    assert "trading_session_unconfirmed" in snapshot.freshness.flags
    assert snapshot.market_state.phase == MarketPhase.UNKNOWN
    assert snapshot.market_state.is_open is False
    assert snapshot.guardrail.regime == MarketRegime.UNCERTAIN
    assert snapshot.guardrail.severity == GuardrailSeverity.STOP
    assert snapshot.scenarios == ()
    assert snapshot.alerts[0].code == "data_stale"


def test_legacy_partial_quality_degrades_a_component_and_keeps_contract_valid() -> None:
    market, risk = _inputs(
        changes=(0.6, 0.7, 1.0, 1.1),
        breadth_ratio=0.62,
        today_amount=108_000_000_000.0,
        sectors=[_sector("industry:securities", "证券", ["attack"], 1.3, 0.66, 4e9)],
    )
    risk["data_quality"] = {"partial": True}

    snapshot = _build(market, risk)

    assert snapshot.freshness.status == FreshnessStatus.DEGRADED
    rotation_freshness = next(
        item for item in snapshot.freshness.components if item.component == "rotation"
    )
    assert rotation_freshness.status == FreshnessStatus.DEGRADED
    assert "partial_component_coverage" in rotation_freshness.flags
    assert snapshot.guardrail.severity == GuardrailSeverity.CAUTION


def test_legacy_stale_flag_marks_risk_components_stale_and_stops_scenarios() -> None:
    market, risk = _inputs(
        changes=(0.6, 0.7, 1.0, 1.1),
        breadth_ratio=0.62,
        today_amount=108_000_000_000.0,
        sectors=[_sector("industry:securities", "证券", ["attack"], 1.3, 0.66, 4e9)],
    )
    risk["stale"] = True

    snapshot = _build(market, risk)

    assert snapshot.freshness.status == FreshnessStatus.STALE
    statuses = {
        item.component: item.status for item in snapshot.freshness.components
    }
    assert statuses["breadth"] == FreshnessStatus.STALE
    assert statuses["rotation"] == FreshnessStatus.STALE
    assert snapshot.guardrail.severity == GuardrailSeverity.STOP
    assert snapshot.scenarios == ()


def test_component_freshness_overrides_legacy_top_level_stale_flag() -> None:
    market, risk = _inputs(
        changes=(0.6, 0.7, 1.0, 1.1),
        breadth_ratio=0.62,
        today_amount=108_000_000_000.0,
        sectors=[_sector("industry:securities", "证券", ["attack"], 1.3, 0.66, 4e9)],
    )
    fresh_component = {
        "fetched_at": NOW.isoformat(),
        "provider_as_of": NOW.isoformat(),
        "stale": False,
        "expired": False,
        "partial": False,
    }
    risk["stale"] = True
    risk["components"] = {
        "market_breadth": dict(fresh_component),
        "industry_quotes": dict(fresh_component),
        "concept_quotes": dict(fresh_component),
    }

    snapshot = _build(market, risk)

    statuses = {
        item.component: item.status for item in snapshot.freshness.components
    }
    assert snapshot.freshness.status == FreshnessStatus.FRESH
    assert statuses["breadth"] == FreshnessStatus.FRESH
    assert statuses["rotation"] == FreshnessStatus.FRESH
    assert "legacy_snapshot_stale" not in snapshot.freshness.flags


def test_explicit_stale_rotation_still_stops_with_component_details() -> None:
    market, risk = _inputs(
        changes=(0.6, 0.7, 1.0, 1.1),
        breadth_ratio=0.62,
        today_amount=108_000_000_000.0,
        sectors=[_sector("industry:securities", "证券", ["attack"], 1.3, 0.66, 4e9)],
    )
    fresh_component = {
        "fetched_at": NOW.isoformat(),
        "provider_as_of": NOW.isoformat(),
        "stale": False,
        "expired": False,
        "partial": False,
    }
    risk["stale"] = True
    risk["components"] = {
        "market_breadth": dict(fresh_component),
        "industry_quotes": {**fresh_component, "stale": True},
        "concept_quotes": dict(fresh_component),
    }

    snapshot = _build(market, risk)

    freshness = {
        item.component: item for item in snapshot.freshness.components
    }
    assert freshness["breadth"].status == FreshnessStatus.FRESH
    assert freshness["rotation"].status == FreshnessStatus.STALE
    assert "component_stale" in freshness["rotation"].flags
    assert snapshot.freshness.status == FreshnessStatus.STALE
    assert "legacy_snapshot_stale" not in snapshot.freshness.flags
    assert snapshot.guardrail.severity == GuardrailSeverity.STOP
    assert snapshot.scenarios == ()


@pytest.mark.parametrize("index_quality", ["accepted", "degraded"])
def test_complete_index_quotes_without_provider_timestamps_are_degraded_not_partial(
    index_quality: str,
) -> None:
    market, risk = _inputs(
        changes=(0.6, 0.7, 1.0, 1.1),
        breadth_ratio=0.62,
        today_amount=108_000_000_000.0,
        sectors=[_sector("industry:securities", "证券", ["attack"], 1.3, 0.66, 4e9)],
    )
    market.pop("provider_as_of")
    market["quality"] = index_quality
    for index in market["indices"]:
        index.pop("provider_as_of")
        index["quality"] = index_quality

    snapshot = _build(market, risk)

    indices_freshness = next(
        item for item in snapshot.freshness.components if item.component == "indices"
    )
    assert all(
        item.available and item.level is not None and item.change_pct is not None
        for item in snapshot.indices
    )
    assert indices_freshness.status == FreshnessStatus.DEGRADED
    assert "provider_timestamp_missing" in indices_freshness.flags
    assert "index_coverage_partial" not in indices_freshness.flags


def test_same_day_but_old_intraday_components_trigger_the_safety_brake() -> None:
    observed_at = datetime(2026, 8, 24, 14, 0, tzinfo=SHANGHAI)
    old = datetime(2026, 8, 24, 9, 31, tzinfo=SHANGHAI)
    market, risk = _inputs(
        changes=(0.9, 1.0, 1.5, 1.7),
        breadth_ratio=0.68,
        today_amount=112_000_000_000.0,
        sectors=[
            _sector(
                "industry:securities",
                "证券",
                ["attack"],
                2.0,
                0.72,
                6e9,
                provider_as_of=old,
            )
        ],
        provider_as_of=old,
    )
    market["timestamp"] = old.isoformat()
    risk["timestamp"] = old.isoformat()

    snapshot = build_market_watch_snapshot(
        market,
        risk,
        as_of=observed_at,
        sequence=1,
        snapshot_id="same-day-old",
    )

    assert snapshot.market_state.phase == MarketPhase.TRADING
    assert snapshot.market_state.is_open is True
    assert snapshot.freshness.status == FreshnessStatus.STALE
    assert "intraday_data_delayed" in snapshot.freshness.flags
    assert snapshot.guardrail.regime == MarketRegime.UNCERTAIN
    assert snapshot.guardrail.severity == GuardrailSeverity.STOP


def test_old_same_day_turnover_minute_is_not_treated_as_fresh() -> None:
    observed_at = datetime(2026, 8, 24, 14, 0, tzinfo=SHANGHAI)
    market, risk = _inputs(
        changes=(0.5, 0.6, 0.8, 0.9),
        breadth_ratio=0.60,
        today_amount=110_000_000_000.0,
        sectors=[
            _sector(
                "industry:securities",
                "证券",
                ["attack"],
                1.2,
                0.65,
                5e9,
                provider_as_of=observed_at,
            )
        ],
        provider_as_of=observed_at,
    )
    market["timestamp"] = observed_at.isoformat()
    market["market_turnover"]["as_of"] = "09:31"
    risk["timestamp"] = observed_at.isoformat()

    snapshot = build_market_watch_snapshot(
        market,
        risk,
        as_of=observed_at,
        sequence=1,
        snapshot_id="old-turnover-minute",
    )

    turnover_freshness = next(
        item for item in snapshot.freshness.components if item.component == "turnover"
    )
    assert turnover_freshness.status == FreshnessStatus.STALE
    assert snapshot.guardrail.severity == GuardrailSeverity.STOP


def test_one_frozen_index_is_not_hidden_by_a_newer_top_level_watermark() -> None:
    observed_at = datetime(2026, 8, 24, 14, 0, tzinfo=SHANGHAI)
    frozen_at = datetime(2026, 8, 24, 9, 31, tzinfo=SHANGHAI)
    market, risk = _inputs(
        changes=(0.5, 0.6, 0.8, 0.9),
        breadth_ratio=0.60,
        today_amount=110_000_000_000.0,
        sectors=[
            _sector(
                "industry:securities",
                "证券",
                ["attack"],
                1.2,
                0.65,
                5e9,
                provider_as_of=observed_at,
            )
        ],
        provider_as_of=observed_at,
    )
    market["timestamp"] = observed_at.isoformat()
    market["indices"][2]["provider_as_of"] = frozen_at.isoformat()
    market["market_turnover"]["as_of"] = "13:59"
    risk["timestamp"] = observed_at.isoformat()

    snapshot = build_market_watch_snapshot(
        market,
        risk,
        as_of=observed_at,
        sequence=1,
        snapshot_id="one-index-frozen",
    )

    indices_freshness = next(
        item for item in snapshot.freshness.components if item.component == "indices"
    )
    assert indices_freshness.status == FreshnessStatus.STALE
    assert snapshot.guardrail.severity == GuardrailSeverity.STOP


def test_contract_is_strict_immutable_timezone_aware_and_has_four_roles() -> None:
    market, risk = _inputs(
        changes=(0.5, 0.6, 0.7, 0.8),
        breadth_ratio=0.60,
        today_amount=106_000_000_000.0,
        sectors=[_sector("industry:证券", "证券", ["attack"], 1.0, 0.62, 2e9)],
        small_cap_id="399852.SZ",
    )
    snapshot = _build(market, risk)
    assert tuple(item.role for item in snapshot.indices) == (
        IndexRole.BROAD_MARKET,
        IndexRole.LARGE_CAP,
        IndexRole.SMALL_CAP,
        IndexRole.GROWTH,
    )
    assert snapshot.indices[2].instrument_id == "399852.SZ"

    payload = snapshot.model_dump(mode="json")
    assert tuple(payload) == (
        "contract",
        "schema_version",
        "snapshot_id",
        "sequence",
        "as_of",
        "market_state",
        "freshness",
        "guardrail",
        "indices",
        "breadth",
        "turnover",
        "rotation",
        "sector_flow_trajectory",
        "offense_sector_flow_trajectory",
        "scenarios",
        "change",
        "alerts",
    )
    with pytest.raises(ValidationError):
        MarketWatchSnapshotV1.model_validate({**payload, "provider": "leaked-provider"})
    with pytest.raises(ValidationError):
        MarketWatchSnapshotV1.model_validate({**payload, "as_of": "2026-08-24T10:30:00"})
    with pytest.raises(ValidationError):
        MarketWatchSnapshotV1.model_validate({**payload, "indices": payload["indices"][:-1]})
    with pytest.raises(ValidationError):
        snapshot.sequence = 2


def test_optional_sector_flow_trajectory_is_strict_and_does_not_change_guardrail():
    market, risk = _inputs(
        changes=(-0.2, 0.1, -1.2, -1.5),
        breadth_ratio=0.37,
        today_amount=88_000_000_000.0,
        sectors=[_sector("industry:电力", "电力", ["defense"], 1.6, 0.72, 4e9)],
    )
    without_flow = _build(market, deepcopy(risk), snapshot_id="without-flow")
    risk["sector_flow_trajectory"] = _sector_flow_trajectory()
    with_flow = _build(market, risk, snapshot_id="with-flow")

    assert with_flow.sector_flow_trajectory is not None
    assert with_flow.sector_flow_trajectory.contract == "sector_flow_trajectory.v1"
    assert with_flow.guardrail == without_flow.guardrail
    assert with_flow.rotation == without_flow.rotation
    assert with_flow.scenarios == without_flow.scenarios

    payload = with_flow.model_dump(mode="json")
    legacy = dict(payload)
    legacy.pop("sector_flow_trajectory")
    assert MarketWatchSnapshotV1.model_validate(legacy).sector_flow_trajectory is None
    assert SectorFlowTrajectoryV1.model_validate(
        payload["sector_flow_trajectory"]
    ).model_dump(mode="json") == payload["sector_flow_trajectory"]

    invalid = deepcopy(payload["sector_flow_trajectory"])
    invalid["provider"] = "leaked-provider"
    with pytest.raises(ValidationError):
        SectorFlowTrajectoryV1.model_validate(invalid)

    invalid = deepcopy(payload["sector_flow_trajectory"])
    invalid["sectors"][0]["points"][0]["provider_as_of"] = "2026-08-24T10:30:00"
    with pytest.raises(ValidationError):
        SectorFlowTrajectoryV1.model_validate(invalid)


def test_offense_sector_flow_trajectory_is_independent_and_direction_checked():
    market, risk = _inputs(
        changes=(0.5, 0.6, 0.7, 0.8),
        breadth_ratio=0.60,
        today_amount=106_000_000_000.0,
        sectors=[_sector("industry:半导体", "半导体", ["attack"], 1.8, 0.68, 4e9)],
    )
    risk["sector_flow_trajectory"] = _sector_flow_trajectory(direction="defense")
    risk["offense_sector_flow_trajectory"] = _sector_flow_trajectory(direction="offense")

    snapshot = _build(market, risk, snapshot_id="two-flow-directions")

    assert snapshot.sector_flow_trajectory is not None
    assert snapshot.sector_flow_trajectory.direction == "defense"
    assert snapshot.offense_sector_flow_trajectory is not None
    assert snapshot.offense_sector_flow_trajectory.direction == "offense"

    invalid = snapshot.model_dump(mode="json")
    invalid["offense_sector_flow_trajectory"]["direction"] = "defense"
    with pytest.raises(ValidationError):
        MarketWatchSnapshotV1.model_validate(invalid)

    concept = deepcopy(snapshot.offense_sector_flow_trajectory.model_dump(mode="json"))
    concept["sectors"][0].update({
        "layer": "concept",
        "parent_sector_key": None,
        "parent_name": None,
        "taxonomy": "concept",
    })
    with pytest.raises(ValidationError):
        SectorFlowTrajectoryV1.model_validate(concept)

def test_invalid_sector_flow_trajectory_fails_closed_inside_the_auxiliary_surface():
    market, risk = _inputs(
        changes=(0.5, 0.6, 0.7, 0.8),
        breadth_ratio=0.60,
        today_amount=106_000_000_000.0,
        sectors=[_sector("industry:证券", "证券", ["attack"], 1.0, 0.62, 2e9)],
    )
    baseline = _build(market, deepcopy(risk), snapshot_id="baseline")
    risk["sector_flow_trajectory"] = {
        **_sector_flow_trajectory(),
        "contract": "provider_specific_flow.v9",
    }

    snapshot = _build(market, risk, snapshot_id="invalid-flow")

    assert snapshot.sector_flow_trajectory is not None
    assert snapshot.sector_flow_trajectory.status.value == "unavailable"
    assert snapshot.sector_flow_trajectory.reason == "invalid_sector_flow_trajectory"
    assert snapshot.guardrail == baseline.guardrail


def test_turnover_uses_symmetric_three_percent_noise_band() -> None:
    common = {
        "previous_same_time_amount_cny": 100.0,
        "today_date": "2026-08-24",
        "previous_date": "2026-08-21",
        "as_of": "10:30",
    }

    assert classify_turnover(today_amount_cny=102.99, **common).direction == TurnoverDirection.FLAT
    assert classify_turnover(today_amount_cny=103.01, **common).direction == TurnoverDirection.EXPAND
    assert classify_turnover(today_amount_cny=97.01, **common).direction == TurnoverDirection.FLAT
    assert classify_turnover(today_amount_cny=96.99, **common).direction == TurnoverDirection.SHRINK


def test_fund_flow_alone_cannot_form_a_strong_sector_conclusion() -> None:
    rotation = classify_rotation([
        {
            "sector_key": "concept:资金流孤证",
            "name": "资金流孤证",
            "tags": ["attack"],
            "main_net_inflow_cny": 20_000_000_000.0,
            "provider_as_of": NOW.isoformat(),
        }
    ])

    assert rotation.regime == MarketRegime.UNCERTAIN
    assert rotation.sectors[0].evidence_strength == EvidenceStrength.WEAK
    assert any("不能形成强结论" in item for item in rotation.sectors[0].counter_evidence)


def test_snapshot_contains_no_probability_or_return_forecast_and_at_most_two_scenarios() -> None:
    market, risk = _inputs(
        changes=(0.8, 1.0, -0.3, -0.4),
        breadth_ratio=0.47,
        today_amount=100_000_000_000.0,
        sectors=[
            _sector("industry:证券", "证券", ["attack"], 1.0, 0.62, 3e9),
            _sector("industry:电力", "电力", ["defense"], 1.1, 0.64, 2e9),
        ],
    )
    snapshot = _build(market, risk)
    payload = snapshot.model_dump(mode="json")
    rendered = json.dumps(payload, ensure_ascii=False)

    assert len(snapshot.scenarios) <= 2
    assert all(
        forbidden not in rendered
        for forbidden in (
            "probability",
            "confidence",
            "概率",
            "胜率",
            "收益预测",
            "目标价",
            "买入",
            "卖出",
            "加仓",
            "减仓",
        )
    )

    invalid = deepcopy(payload)
    invalid["scenarios"] = [payload["scenarios"][0]] * 3
    with pytest.raises(ValidationError):
        MarketWatchSnapshotV1.model_validate(invalid)


def test_change_uses_only_an_explicitly_confirmed_rotation_transition() -> None:
    market, risk = _inputs(
        changes=(0.4, 0.5, 0.7, 0.8),
        breadth_ratio=0.58,
        today_amount=106_000_000_000.0,
        sectors=[_sector("industry:证券", "证券", ["attack"], 1.1, 0.64, 3e9)],
    )
    risk["offense"] = {
        "events": [
            {
                "at": NOW.isoformat(),
                "id": "industry:证券",
                "name": "证券",
                "from": "candidate",
                "to": "attacking",
                "label": "候选转为进攻",
            }
        ]
    }

    snapshot = _build(market, risk)

    assert snapshot.change.available is True
    assert snapshot.change.summary == "候选转为进攻"
    assert snapshot.change.previous_snapshot_id.startswith("event-before:")
    assert snapshot.change.changed_fields == (
        "rotation.sectors.industry:证券.lifecycle",
    )


def test_static_headline_without_confirmed_event_is_not_reported_as_change() -> None:
    market, risk = _inputs(
        changes=(0.1, 0.2, 0.1, 0.2),
        breadth_ratio=0.51,
        today_amount=100_000_000_000.0,
        sectors=[],
    )
    risk["structure"] = {"label": "金融进攻"}
    risk["dynamics"] = {
        "status": "collecting",
        "headline": "板块状态出现变化，等待确认",
        "last_confirmed_at": None,
    }

    snapshot = _build(market, risk)

    assert snapshot.change.available is False
    assert snapshot.change.changed_fields == ()
    assert snapshot.change.reason == "no_confirmed_change_evidence"
