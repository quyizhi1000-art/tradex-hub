"""Pure rotation radar tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tradex.dashboard.rotation_radar import (
    CORE_OFFENSE_CONFIG_VERSION,
    CORE_OFFENSE_DEFINITIONS,
    analyze_rotation_snapshots,
    analyze_sector_flow_snapshots,
    canonical_family,
    normalize_rotation_snapshot,
    sector_flow_backfill_targets,
)
from tradex.market_watch import SectorFlowTrajectoryV1


SHANGHAI = ZoneInfo("Asia/Shanghai")
START = datetime(2026, 8, 19, 10, 0, tzinfo=SHANGHAI)


def _board(
    taxonomy: str,
    code: str,
    name: str,
    minute: datetime,
    *,
    change: float,
    breadth: float = 0.5,
    flow: float = 0.0,
    provider: datetime | None = None,
) -> dict:
    total = 100
    up = round(total * breadth)
    return {
        "板块代码": code,
        "板块名称": name,
        "涨跌幅": change,
        "上涨家数": up,
        "下跌家数": total - up,
        "主力净流入": flow * 100_000_000,
        "主力净流入-占比": flow,
        "主力净流入排名": int(50 - flow),
        "更新时间": (provider or minute).isoformat(timespec="seconds"),
        "source": f"{taxonomy}-source",
    }


def _background(taxonomy: str, minute: datetime, prefix: str) -> list[dict]:
    return [
        _board(
            taxonomy,
            f"{prefix}{index:03d}",
            f"{prefix}背景{index}",
            minute,
            change=change,
        )
        for index, change in enumerate((-2.0, -1.0, 0.0, 1.0, 2.0, 3.0, 4.0), start=1)
    ]


def _snapshot(
    minute: datetime,
    *,
    target_taxonomy: str = "industry",
    target_name: str = "半导体材料",
    target_code: str = "T001",
    target_change: float = -3.0,
    target_breadth: float = 0.3,
    target_flow: float = 0.0,
    include_target: bool = True,
    target_provider: datetime | None = None,
    phase: str = "trading",
) -> dict:
    industry = _background("industry", minute, "I")
    concept = _background("concept", minute, "C")
    if include_target:
        target = _board(
            target_taxonomy,
            target_code,
            target_name,
            minute,
            change=target_change,
            breadth=target_breadth,
            flow=target_flow,
            provider=target_provider,
        )
        (industry if target_taxonomy == "industry" else concept).append(target)
    return normalize_rotation_snapshot(
        industry,
        concept,
        minute_bucket=minute,
        market_phase=phase,
    )


def _rotation_sequence(*, taxonomy: str = "industry", name: str = "半导体材料") -> list[dict]:
    snapshots = [
        _snapshot(START + timedelta(minutes=offset), target_taxonomy=taxonomy, target_name=name)
        for offset in range(5)
    ]
    snapshots.extend([
        _snapshot(
            START + timedelta(minutes=5),
            target_taxonomy=taxonomy,
            target_name=name,
            target_change=1.5,
            target_breadth=0.52,
        ),
        _snapshot(
            START + timedelta(minutes=6),
            target_taxonomy=taxonomy,
            target_name=name,
            target_change=1.6,
            target_breadth=0.53,
        ),
    ])
    return snapshots


def _attack_sequence(*, taxonomy: str = "industry", name: str = "半导体材料") -> list[dict]:
    snapshots = _rotation_sequence(taxonomy=taxonomy, name=name)
    snapshots.extend([
        _snapshot(
            START + timedelta(minutes=7),
            target_taxonomy=taxonomy,
            target_name=name,
            target_change=5.0,
            target_breadth=0.8,
            target_flow=10.0,
        ),
        _snapshot(
            START + timedelta(minutes=8),
            target_taxonomy=taxonomy,
            target_name=name,
            target_change=5.2,
            target_breadth=0.82,
            target_flow=11.0,
        ),
    ])
    return snapshots


def _all_candidates(view: dict) -> list[dict]:
    return view["attacking"] + view["rotating"] + view["cooling"] + view["unclassified"]


def _core_item(view: dict, key: str) -> dict:
    return next(item for item in view["core"]["items"] if item["key"] == key)


def _core_snapshot(
    minute: datetime,
    *,
    code: str = "BK1036",
    name: str = "半导体",
    change: float = 0.0,
    breadth: float = 0.5,
    flow: float = 0.0,
    source: str = "industry-source",
) -> dict:
    anchor = _board(
        "industry",
        code,
        name,
        minute,
        change=change,
        breadth=breadth,
        flow=flow,
    )
    anchor["source"] = source
    return normalize_rotation_snapshot(
        [*_background("industry", minute, "I"), anchor],
        _background("concept", minute, "C"),
        minute_bucket=minute,
    )


def _defense_flow_snapshot(
    minute: datetime,
    *,
    flow: float,
    change: float = 5.0,
    board_code: str = "POWER",
    provider: datetime | None = None,
    source: str = "industry-source",
) -> dict:
    electric_power = _board(
        "industry",
        board_code,
        "电力",
        minute,
        change=change,
        breadth=0.80,
        flow=flow,
        provider=provider,
    )
    electric_power["source"] = source
    return normalize_rotation_snapshot(
        [*_background("industry", minute, "I"), electric_power],
        _background("concept", minute, "C"),
        minute_bucket=minute,
    )


def test_normalization_is_order_independent_and_taxonomies_rank_separately():
    minute = START
    industry = _background("industry", minute, "I")
    concept = _background("concept", minute, "C")
    concept.append(_board("concept", "CT", "脑机接口", minute, change=0.5, breadth=0.8, flow=10))

    first = normalize_rotation_snapshot(industry, concept, minute_bucket=minute)
    second = normalize_rotation_snapshot(reversed(industry), reversed(concept), minute_bucket=minute)

    assert first == second
    assert first["coverage"]["industry"]["total"] == 7
    assert first["coverage"]["concept"]["total"] == 8

    concept_view = analyze_rotation_snapshots(_attack_sequence(taxonomy="concept", name="脑机接口"))
    assert any(item["taxonomy"] == "concept" for item in concept_view["attacking"])


def test_same_day_provider_snapshot_freezes_after_close_but_not_during_trading():
    provider = datetime(2026, 8, 19, 15, 0, tzinfo=SHANGHAI)
    observed = datetime(2026, 8, 19, 16, 30, tzinfo=SHANGHAI)
    board = _board(
        "industry",
        "BK1036",
        "半导体",
        observed,
        provider=provider,
        change=2.0,
        breadth=0.7,
        flow=3.0,
    )

    trading = normalize_rotation_snapshot(
        [board], [], minute_bucket=observed, market_phase="trading"
    )
    closed = normalize_rotation_snapshot(
        [board], [], minute_bucket=observed, market_phase="closed"
    )

    assert trading["boards"][0]["provider_fresh"] is False
    assert closed["boards"][0]["provider_fresh"] is True
    assert analyze_rotation_snapshots([closed])["core"]["as_of"] == provider.isoformat(
        timespec="seconds"
    )


def test_passive_percentile_improvement_does_not_create_rotation():
    snapshots = []
    for offset in range(5):
        minute = START + timedelta(minutes=offset)
        industry = [
            _board("industry", "PASSIVE", "半导体材料", minute, change=0.5),
            *[
                _board("industry", f"P{i}", f"行业{i}", minute, change=float(i + 1))
                for i in range(7)
            ],
        ]
        snapshots.append(normalize_rotation_snapshot(industry, _background("concept", minute, "C"), minute_bucket=minute))
    for offset in (5, 6):
        minute = START + timedelta(minutes=offset)
        industry = [
            _board("industry", "PASSIVE", "半导体材料", minute, change=0.5),
            *[
                _board("industry", f"P{i}", f"行业{i}", minute, change=float(-i - 1))
                for i in range(7)
            ],
        ]
        snapshots.append(normalize_rotation_snapshot(industry, _background("concept", minute, "C"), minute_bucket=minute))

    view = analyze_rotation_snapshots(snapshots)
    assert not any(item["board_code"] == "PASSIVE" for item in view["rotating"])


def test_two_fresh_watermarks_drive_mutually_exclusive_lifecycle():
    rotating = analyze_rotation_snapshots(_rotation_sequence())
    assert [item["state"] for item in rotating["rotating"]] == ["rotating"]
    assert rotating["attacking"] == []

    attacking = analyze_rotation_snapshots(_attack_sequence())
    assert [item["state"] for item in attacking["attacking"]] == ["attacking"]
    assert attacking["rotating"] == []
    assert attacking["attacking"][0]["role"]["key"] == "technology_offense"

    minute9 = START + timedelta(minutes=9)
    minute10 = START + timedelta(minutes=10)
    cooling = analyze_rotation_snapshots([
        *_attack_sequence(),
        _snapshot(minute9, target_change=-4, target_breadth=0.2, target_flow=-10),
        _snapshot(minute10, target_change=-5, target_breadth=0.1, target_flow=-11),
    ])
    assert [item["state"] for item in cooling["cooling"]] == ["cooling"]
    assert cooling["attacking"] == []


def test_same_or_older_board_watermark_never_advances_confirmation():
    snapshots = _rotation_sequence()[:-1]
    minute5 = START + timedelta(minutes=5)
    snapshots.extend([
        _snapshot(
            START + timedelta(minutes=6),
            target_change=1.6,
            target_breadth=0.53,
            target_provider=minute5,
        ),
        _snapshot(
            START + timedelta(minutes=7),
            target_change=1.7,
            target_breadth=0.54,
            target_provider=minute5 - timedelta(minutes=1),
        ),
    ])

    view = analyze_rotation_snapshots(snapshots)
    assert view["rotating"] == []
    assert view["events"] == []


def test_source_switch_resets_history_before_a_state_exists():
    snapshots = [
        _snapshot(START + timedelta(minutes=offset))
        for offset in range(5)
    ]
    for offset in (5, 6):
        minute = START + timedelta(minutes=offset)
        industry = [
            *_background("industry", minute, "I"),
            _board(
                "industry",
                "T001",
                "半导体材料",
                minute,
                change=1.5 + (offset - 5) * 0.1,
                breadth=0.52,
            ),
        ]
        industry[-1]["source"] = "fallback-source"
        snapshots.append(normalize_rotation_snapshot(
            industry,
            _background("concept", minute, "C"),
            minute_bucket=minute,
        ))

    view = analyze_rotation_snapshots(snapshots)
    assert view["rotating"] == []
    assert view["events"] == []


def test_fund_direction_uses_state_change_between_distinct_watermarks():
    snapshots = _rotation_sequence()
    minute = START + timedelta(minutes=7)
    snapshots.append(_snapshot(
        minute,
        target_change=5,
        target_breadth=0.8,
        target_flow=10,
    ))
    view = analyze_rotation_snapshots(snapshots)
    candidate = view["rotating"][0]

    assert candidate["metrics"]["fund_direction"] == "strengthening"
    assert candidate["metrics"]["previous_provider_as_of"].endswith("10:06:00+08:00")
    assert candidate["metrics"]["fund_delta"] == 1_000_000_000
    assert view["summary"]["current_leaders"][0]["direction"] == "strengthening"


def test_missing_board_does_not_exit_but_two_explicit_failures_do():
    snapshots = _attack_sequence()
    snapshots.extend([
        _snapshot(START + timedelta(minutes=9), include_target=False),
        _snapshot(START + timedelta(minutes=10), include_target=False),
    ])
    missing = analyze_rotation_snapshots(snapshots)
    assert missing["attacking"][0]["current"] is False

    snapshots.extend([
        _snapshot(START + timedelta(minutes=11), target_change=-4, target_breadth=0.2, target_flow=-10),
        _snapshot(START + timedelta(minutes=12), target_change=-5, target_breadth=0.1, target_flow=-11),
    ])
    failed = analyze_rotation_snapshots(snapshots)
    assert failed["attacking"] == []
    assert failed["cooling"][0]["state"] == "cooling"


def test_opening_collects_raw_baseline_without_lifecycle_events():
    snapshots = [
        _snapshot(
            datetime(2026, 8, 19, 9, 30 + offset, tzinfo=SHANGHAI),
            target_change=5,
            target_breadth=0.8,
            target_flow=10,
            phase="opening_observation",
        )
        for offset in range(3)
    ]
    view = analyze_rotation_snapshots(snapshots)
    assert view["status"] == "collecting"
    assert view["attacking"] == []
    assert view["rotating"] == []
    assert view["events"] == []


def test_lunch_resets_confirmation_and_temporal_baseline():
    morning_start = datetime(2026, 8, 19, 11, 24, tzinfo=SHANGHAI)
    morning = [
        _snapshot(morning_start + timedelta(minutes=offset))
        for offset in range(5)
    ]
    morning.append(_snapshot(
        datetime(2026, 8, 19, 11, 29, tzinfo=SHANGHAI),
        target_change=1.5,
        target_breadth=0.52,
    ))
    afternoon = _snapshot(
        datetime(2026, 8, 19, 13, 0, tzinfo=SHANGHAI),
        target_change=1.6,
        target_breadth=0.53,
    )

    view = analyze_rotation_snapshots([*morning, afternoon])
    assert view["rotating"] == []
    assert view["events"] == []


def test_sector_flow_trajectory_uses_a_same_session_five_minute_baseline():
    snapshots = [
        _defense_flow_snapshot(
            START + timedelta(minutes=offset),
            flow=1.0 + offset,
            change=4.0 + 0.2 * offset,
        )
        for offset in range(6)
    ]

    trajectory = analyze_sector_flow_snapshots(snapshots)
    canonical = SectorFlowTrajectoryV1.model_validate(trajectory)
    electric_power = next(
        item for item in canonical.sectors if item.sector_key == "electric_power"
    )

    assert electric_power.latest is not None
    assert electric_power.latest.cumulative_cny == 600_000_000
    assert electric_power.latest.delta_5m_cny == 500_000_000
    assert electric_power.latest.delta_5m_baseline_as_of == START
    assert electric_power.latest.change_delta_5m_pct == 1.0
    assert electric_power.observation_tier.value == "confirmed_strengthening"
    assert electric_power.observation_rank == 1
    assert len(canonical.sectors) == 39


def test_defense_sector_flow_trajectory_adds_industry_and_concept_children():
    snapshots = []
    for offset in range(6):
        minute = START + timedelta(minutes=offset)
        environmental = _board(
            "industry", "BK_ENV", "环保", minute,
            change=1.0 + offset * 0.1, breadth=0.62, flow=2.0 + offset,
        )
        energy_conservation = _board(
            "concept", "BK_GREEN", "节能环保", minute,
            change=1.5 + offset * 0.2, breadth=0.68, flow=3.0 + offset,
        )
        for item in (environmental, energy_conservation):
            item["source"] = "push2delay"
        snapshots.append(normalize_rotation_snapshot(
            [*_background("industry", minute, "I"), environmental],
            [*_background("concept", minute, "C"), energy_conservation],
            minute_bucket=minute,
        ))

    trajectory = analyze_sector_flow_snapshots(snapshots, direction="defense")
    canonical = SectorFlowTrajectoryV1.model_validate(trajectory)
    anchors = [item for item in canonical.sectors if item.layer == "anchor"]
    concepts = [item for item in canonical.sectors if item.layer == "concept"]
    environmental = next(
        item for item in anchors if item.sector_key == "environmental_protection"
    )
    energy_conservation = next(
        item
        for item in concepts
        if item.sector_key == "energy_conservation_environmental_protection"
    )

    assert canonical.direction == "defense"
    assert len(anchors) == 20
    assert len(concepts) == 19
    assert {
        "prepared_food",
        "air_source_heat_pump",
        "internet_healthcare",
        "agriculture_planting",
        "sponge_city",
        "underground_pipe_network",
        "ecological_agriculture",
        "pumped_storage",
        "food_safety",
    }.isdisjoint({item.sector_key for item in canonical.sectors})
    assert environmental.taxonomy == "industry"
    assert environmental.latest is not None
    assert energy_conservation.taxonomy == "concept"
    assert energy_conservation.parent_sector_key == "environmental_protection"
    assert energy_conservation.parent_name == "环保"
    assert energy_conservation.latest is not None
    assert energy_conservation.latest.delta_5m_cny == 500_000_000


def test_expanded_sector_flow_pool_requests_exact_backfill_for_resolved_boards():
    minute = START
    electric_power = _board(
        "industry", "BK0428", "电力", minute, change=1.0, breadth=0.6, flow=1.0
    )
    coal = _board(
        "industry", "BK0437", "煤炭", minute, change=2.0, breadth=0.7, flow=2.0
    )
    electric_power["source"] = "push2delay"
    coal["source"] = "push2delay"
    industry = [*_background("industry", minute, "I"), electric_power, coal]
    concept = _background("concept", minute, "C")

    trajectory = analyze_sector_flow_snapshots([
        normalize_rotation_snapshot(industry, concept, minute_bucket=minute)
    ])
    coal_series = next(
        item for item in trajectory["sectors"] if item["sector_key"] == "coal"
    )
    targets = sector_flow_backfill_targets(
        industry,
        concept,
        minute_bucket=minute,
        sources={"industry": "push2delay", "concept": "push2delay"},
    )

    assert coal_series["latest"]["change_pct"] == 2.0
    assert "coal" in {item["sector_key"] for item in targets}


def test_sector_flow_trajectory_exposes_only_an_explicit_bk_leader_identity():
    with_bk = analyze_sector_flow_snapshots([
        _defense_flow_snapshot(START, flow=1.0, board_code="BK0428"),
    ])
    without_bk = analyze_sector_flow_snapshots([
        _defense_flow_snapshot(START, flow=1.0, board_code="POWER"),
    ])

    assert next(
        item for item in with_bk["sectors"] if item["sector_key"] == "electric_power"
    )["leader_board_code"] == "BK0428"
    assert next(
        item for item in without_bk["sectors"] if item["sector_key"] == "electric_power"
    )["leader_board_code"] is None


def test_sector_flow_trajectory_does_not_bridge_the_midday_break():
    morning = [
        _defense_flow_snapshot(
            datetime(2026, 8, 19, 11, 25, tzinfo=SHANGHAI) + timedelta(minutes=offset),
            flow=1.0 + offset,
        )
        for offset in range(6)
    ]
    afternoon = _defense_flow_snapshot(
        datetime(2026, 8, 19, 13, 0, tzinfo=SHANGHAI),
        flow=9.0,
    )

    trajectory = analyze_sector_flow_snapshots([*morning, afternoon])
    electric_power = next(
        item for item in trajectory["sectors"] if item["sector_key"] == "electric_power"
    )

    assert electric_power["latest"]["delta_5m_cny"] is None
    assert electric_power["latest"]["delta_10m_cny"] is None
    assert electric_power["points"][-1]["session_segment"] == "pm"
    assert electric_power["status"] == "collecting"


def test_sector_flow_trajectory_ignores_repeated_watermarks_and_resets_source():
    same_provider = [
        _defense_flow_snapshot(START, flow=1.0),
        _defense_flow_snapshot(
            START + timedelta(minutes=1),
            flow=3.0,
            provider=START,
        ),
    ]
    repeated = analyze_sector_flow_snapshots(same_provider)
    electric_power = next(
        item for item in repeated["sectors"] if item["sector_key"] == "electric_power"
    )
    assert len(electric_power["points"]) == 1
    assert "repeated_provider_time_ignored" in electric_power["flags"]

    switched = [
        *[
            _defense_flow_snapshot(START + timedelta(minutes=offset), flow=1.0 + offset)
            for offset in range(6)
        ],
        _defense_flow_snapshot(
            START + timedelta(minutes=6),
            flow=8.0,
            source="industry-fallback",
        ),
    ]
    reset = analyze_sector_flow_snapshots(switched)
    electric_power = next(
        item for item in reset["sectors"] if item["sector_key"] == "electric_power"
    )
    assert len(electric_power["points"]) == 1
    assert electric_power["latest"]["delta_5m_cny"] is None
    assert "source_changed_baseline_reset" in electric_power["flags"]


def test_sector_flow_trajectory_is_deterministic_for_reversed_snapshot_input():
    snapshots = [
        _defense_flow_snapshot(START + timedelta(minutes=offset), flow=1.0 + offset)
        for offset in range(6)
    ]

    assert analyze_sector_flow_snapshots(snapshots) == analyze_sector_flow_snapshots(
        reversed(snapshots)
    )


def test_offense_trajectory_keeps_core_anchors_and_adds_resource_observation():
    snapshots = [
        _core_snapshot(
            START + timedelta(minutes=offset),
            change=1.0 + offset * 0.1,
            breadth=0.62 + offset * 0.01,
            flow=2.0 + offset,
            source="push2delay",
        )
        for offset in range(6)
    ]

    trajectory = analyze_sector_flow_snapshots(snapshots, direction="offense")
    canonical = SectorFlowTrajectoryV1.model_validate(trajectory)
    semiconductor = next(
        item for item in trajectory["sectors"] if item["sector_key"] == "semiconductor"
    )

    assert canonical.direction == "offense"
    assert {
        item["sector_key"]
        for item in trajectory["sectors"]
        if item["layer"] == "anchor"
    } == {
        *(definition["key"] for definition in CORE_OFFENSE_DEFINITIONS),
        "innovative_drugs",
        "minor_metals",
    }
    assert semiconductor["latest"]["delta_5m_cny"] == 500_000_000
    assert semiconductor["category_name"] == "科技成长"


def test_offense_trajectory_places_rare_earth_under_minor_metals():
    snapshots = []
    latest_industry = []
    latest_concept = []
    for offset in range(6):
        minute = START + timedelta(minutes=offset)
        minor_metals = _board(
            "industry", "BK1027", "小金属", minute,
            change=1.0 + offset * 0.1, breadth=0.62, flow=2.0 + offset,
        )
        rare_earth = _board(
            "concept", "BK0578", "稀土永磁", minute,
            change=1.5 + offset * 0.2, breadth=0.68, flow=3.0 + offset,
        )
        for item in (minor_metals, rare_earth):
            item["source"] = "push2delay"
        latest_industry = [*_background("industry", minute, "I"), minor_metals]
        latest_concept = [*_background("concept", minute, "C"), rare_earth]
        snapshots.append(normalize_rotation_snapshot(
            latest_industry,
            latest_concept,
            minute_bucket=minute,
        ))

    trajectory = analyze_sector_flow_snapshots(snapshots, direction="offense")
    canonical = SectorFlowTrajectoryV1.model_validate(trajectory)
    minor_metals = next(
        item for item in canonical.sectors if item.sector_key == "minor_metals"
    )
    rare_earth = next(
        item
        for item in canonical.sectors
        if item.sector_key == "rare_earth_permanent_magnet"
    )
    targets = sector_flow_backfill_targets(
        latest_industry,
        latest_concept,
        minute_bucket=START + timedelta(minutes=5),
        sources={"industry": "push2delay", "concept": "push2delay"},
    )

    assert len(canonical.sectors) == 48
    assert minor_metals.category_name == "资源周期"
    assert minor_metals.taxonomy == "industry"
    assert minor_metals.latest is not None
    assert rare_earth.category_name == "资源周期"
    assert rare_earth.taxonomy == "concept"
    assert rare_earth.parent_sector_key == "minor_metals"
    assert rare_earth.parent_name == "小金属"
    assert rare_earth.latest is not None
    assert rare_earth.latest.delta_5m_cny == 500_000_000
    assert "minor_metals" in {item["sector_key"] for item in targets}


def test_offense_trajectory_adds_innovative_drugs_as_medical_growth():
    snapshots = []
    latest_industry = []
    latest_concept = []
    for offset in range(6):
        minute = START + timedelta(minutes=offset)
        innovative_drugs = _board(
            "concept", "BK1106", "创新药", minute,
            change=1.5 + offset * 0.2, breadth=0.68, flow=3.0 + offset,
        )
        innovative_drugs["source"] = "push2delay"
        latest_industry = _background("industry", minute, "I")
        latest_concept = [*_background("concept", minute, "C"), innovative_drugs]
        snapshots.append(normalize_rotation_snapshot(
            latest_industry,
            latest_concept,
            minute_bucket=minute,
        ))

    trajectory = analyze_sector_flow_snapshots(snapshots, direction="offense")
    canonical = SectorFlowTrajectoryV1.model_validate(trajectory)
    innovative_drugs = next(
        item for item in canonical.sectors if item.sector_key == "innovative_drugs"
    )
    targets = sector_flow_backfill_targets(
        latest_industry,
        latest_concept,
        minute_bucket=START + timedelta(minutes=5),
        sources={"industry": "push2delay", "concept": "push2delay"},
    )

    assert len(canonical.sectors) == 48
    assert innovative_drugs.name == "创新药"
    assert innovative_drugs.category_name == "医药成长"
    assert innovative_drugs.taxonomy == "concept"
    assert innovative_drugs.parent_sector_key is None
    assert innovative_drugs.latest is not None
    assert innovative_drugs.latest.delta_5m_cny == 500_000_000
    assert "innovative_drugs" in {item["sector_key"] for item in targets}


def test_offense_sector_flow_trajectory_adds_hierarchical_concept_children():
    snapshots = []
    for offset in range(6):
        minute = START + timedelta(minutes=offset)
        semiconductor = _board(
            "industry", "BK1036", "半导体", minute,
            change=1.0 + offset * 0.1, breadth=0.62, flow=2.0 + offset,
        )
        advanced_packaging = _board(
            "concept", "BK1101", "先进封装", minute,
            change=1.5 + offset * 0.2, breadth=0.68, flow=3.0 + offset,
        )
        for item in (semiconductor, advanced_packaging):
            item["source"] = "push2delay"
        snapshots.append(normalize_rotation_snapshot(
            [*_background("industry", minute, "I"), semiconductor],
            [*_background("concept", minute, "C"), advanced_packaging],
            minute_bucket=minute,
        ))

    trajectory = analyze_sector_flow_snapshots(snapshots, direction="offense")
    canonical = SectorFlowTrajectoryV1.model_validate(trajectory)
    concept = next(
        item
        for item in trajectory["sectors"]
        if item["sector_key"] == "advanced_packaging"
    )

    assert canonical.direction == "offense"
    assert len(trajectory["sectors"]) > len(CORE_OFFENSE_DEFINITIONS) * 4
    assert concept["layer"] == "concept"
    assert concept["parent_sector_key"] == "semiconductor"
    assert concept["parent_name"] == "半导体"
    assert concept["taxonomy"] == "concept"
    assert concept["latest"]["delta_5m_cny"] == 500_000_000


def test_sector_flow_backfill_targets_cover_defense_and_core_offense_without_duplicates():
    minute = START
    electric_power = _board(
        "industry", "BK0428", "电力", minute,
        change=1.0, breadth=0.6, flow=1.0,
    )
    semiconductor = _board(
        "industry", "BK1036", "半导体", minute,
        change=2.0, breadth=0.7, flow=2.0,
    )
    for item in (electric_power, semiconductor):
        item["source"] = "push2delay"

    targets = sector_flow_backfill_targets(
        [*_background("industry", minute, "I"), electric_power, semiconductor],
        _background("concept", minute, "C"),
        minute_bucket=minute,
        sources={"industry": "push2delay", "concept": "push2delay"},
    )

    assert [(item["sector_key"], item["provider_sector_code"]) for item in targets] == [
        ("electric_power", "BK0428"),
        ("semiconductor", "BK1036"),
    ]


def test_sector_flow_backfill_merges_with_push2delay_live_points():
    live_minute = START + timedelta(minutes=10)
    live = _defense_flow_snapshot(
        live_minute,
        flow=11.0,
        source="push2delay",
    )
    supplements = {
        "electric_power": [
            {
                "provider_as_of": START + timedelta(minutes=offset),
                "cumulative_cny": (1.0 + offset) * 100_000_000,
                "source_family": "eastmoney",
            }
            for offset in range(10)
        ]
    }

    trajectory = analyze_sector_flow_snapshots([live], supplements)
    electric_power = next(
        item for item in trajectory["sectors"] if item["sector_key"] == "electric_power"
    )

    assert trajectory["status"] in {"ready", "partial"}
    assert electric_power["status"] == "ready"
    assert electric_power["latest"]["provider_as_of"] == live_minute
    assert electric_power["latest"]["delta_5m_cny"] == 500_000_000
    assert electric_power["points"][0]["provider_as_of"] == START
    assert "intraday_history_backfilled" in electric_power["flags"]


def test_sector_flow_backfill_prefers_live_sample_at_the_same_minute():
    first_live_minute = START + timedelta(minutes=10, seconds=18)
    latest_live_minute = START + timedelta(minutes=10, seconds=57)
    first_live = _defense_flow_snapshot(
        first_live_minute,
        flow=11.0,
        source="push2delay",
    )
    latest_live = _defense_flow_snapshot(
        latest_live_minute,
        flow=12.0,
        source="push2delay",
    )
    supplements = {
        "electric_power": [
            {
                "provider_as_of": START + timedelta(minutes=offset),
                "cumulative_cny": (1.0 + offset) * 100_000_000,
                "source_family": "eastmoney",
            }
            for offset in range(11)
        ]
    }

    trajectory = analyze_sector_flow_snapshots(
        [first_live, latest_live],
        supplements,
    )
    electric_power = next(
        item
        for item in trajectory["sectors"]
        if item["sector_key"] == "electric_power"
    )
    join_minute = latest_live_minute.replace(second=0, microsecond=0)
    joined = [
        point
        for point in electric_power["points"]
        if point["provider_as_of"].replace(second=0, microsecond=0) == join_minute
    ]

    assert len(joined) == 1
    assert joined[0]["provider_as_of"] == latest_live_minute
    assert joined[0]["cumulative_cny"] == 1_200_000_000
    assert len(electric_power["points"]) == 11
    assert "same_minute_provider_time_replaced" in electric_power["flags"]


def test_sector_flow_backfill_repairs_gaps_after_live_collection_started():
    live_offsets = (0, 1, 4, 5)
    live = [
        _defense_flow_snapshot(
            START + timedelta(minutes=offset, seconds=18),
            flow=1.0 + offset,
            source="push2delay",
        )
        for offset in live_offsets
    ]
    supplements = {
        "electric_power": [
            {
                "provider_as_of": START + timedelta(minutes=offset),
                "cumulative_cny": (1.0 + offset) * 100_000_000,
                "source_family": "eastmoney",
            }
            for offset in range(7)
        ]
    }

    trajectory = analyze_sector_flow_snapshots(live, supplements)
    electric_power = next(
        item
        for item in trajectory["sectors"]
        if item["sector_key"] == "electric_power"
    )
    points = electric_power["points"]

    assert len(points) == 6
    assert [point["provider_as_of"].minute for point in points] == list(range(6))
    assert points[0]["provider_as_of"].second == 18
    assert points[1]["provider_as_of"].second == 18
    assert points[2]["provider_as_of"].second == 0
    assert points[3]["provider_as_of"].second == 0
    assert points[4]["provider_as_of"].second == 18
    assert points[5]["provider_as_of"].second == 18
    assert electric_power["latest"]["delta_5m_cny"] == 500_000_000
    assert points[-1]["provider_as_of"].replace(second=0, microsecond=0) <= (
        START + timedelta(minutes=5)
    )
    assert "intraday_history_backfilled" in electric_power["flags"]


def test_sector_flow_can_display_exact_backfill_without_a_live_flow_amount():
    minute = START + timedelta(minutes=10)
    electric_power = _board(
        "industry",
        "BK0428",
        "电力",
        minute,
        change=2.0,
        breadth=0.70,
        flow=0.0,
    )
    electric_power.pop("主力净流入")
    electric_power.pop("主力净流入-占比")
    electric_power["source"] = "push2delay"
    snapshot = normalize_rotation_snapshot(
        [*_background("industry", minute, "I"), electric_power],
        _background("concept", minute, "C"),
        minute_bucket=minute,
    )
    supplements = {
        "electric_power": [
            {
                "provider_as_of": START + timedelta(minutes=offset),
                "cumulative_cny": (1.0 + offset) * 100_000_000,
                "source_family": "eastmoney",
            }
            for offset in range(11)
        ]
    }

    trajectory = analyze_sector_flow_snapshots([snapshot], supplements)
    result = next(
        item for item in trajectory["sectors"] if item["sector_key"] == "electric_power"
    )

    assert result["status"] == "partial"
    assert result["latest"]["cumulative_cny"] == 1_100_000_000
    assert result["latest"]["delta_5m_cny"] == 500_000_000
    assert result["reason"] is None


def test_sector_flow_keeps_exact_curve_when_current_bulk_snapshot_omits_sector():
    minute = START + timedelta(minutes=5)
    snapshot = normalize_rotation_snapshot(
        _background("industry", minute, "I"),
        _background("concept", minute, "C"),
        minute_bucket=minute,
    )
    supplements = {
        "electric_power": [
            {
                "provider_as_of": START + timedelta(minutes=offset),
                "cumulative_cny": (1.0 + offset) * 100_000_000,
                "source_family": "eastmoney",
                "name": "电力",
                "taxonomy": "industry",
            }
            for offset in range(6)
        ]
    }

    trajectory = analyze_sector_flow_snapshots([snapshot], supplements)
    result = next(
        item for item in trajectory["sectors"] if item["sector_key"] == "electric_power"
    )

    assert trajectory["status"] == "partial"
    assert result["status"] == "partial"
    assert result["taxonomy"] == "industry"
    assert len(result["points"]) == 6
    assert result["latest"]["cumulative_cny"] == 600_000_000
    assert result["reason"] is None
    assert "current_sector_snapshot_missing" in result["flags"]


def test_sector_flow_backfill_targets_accept_only_matching_eastmoney_board_ids():
    minute = START
    eastmoney = _board(
        "industry", "BK0428", "电力", minute, change=1.0, breadth=0.6, flow=1.0
    )
    eastmoney["source"] = "push2delay"
    foreign = _board(
        "industry", "BK0732", "贵金属", minute, change=1.0, breadth=0.6, flow=1.0
    )
    foreign["source"] = "ths_fuyao"

    targets = sector_flow_backfill_targets(
        [*_background("industry", minute, "I"), eastmoney, foreign],
        _background("concept", minute, "C"),
        minute_bucket=minute,
        sources={"industry": "push2delay", "concept": "push2delay"},
    )

    assert [item["sector_key"] for item in targets] == ["electric_power"]
    assert targets[0]["provider_sector_code"] == "BK0428"
    assert targets[0]["source_family"] == "eastmoney"


def test_cross_day_history_is_not_used_for_speed_or_confirmation():
    yesterday = [
        _snapshot(START + timedelta(minutes=offset))
        for offset in range(5)
    ]
    today_minute = START + timedelta(days=1)
    today = _snapshot(
        today_minute,
        target_change=1.5,
        target_breadth=0.52,
    )

    view = analyze_rotation_snapshots([*yesterday, today])
    assert view["sample_count"] == 1
    assert view["rotating"] == []
    assert view["events"] == []


def test_family_dedupes_across_taxonomy_and_keeps_confirmations():
    snapshots = []
    for offset in range(9):
        minute = START + timedelta(minutes=offset)
        if offset < 5:
            change, breadth, flow = -3.0, 0.3, 0.0
        elif offset < 7:
            change, breadth, flow = 1.5 + (offset - 5) * 0.1, 0.52, 0.0
        else:
            change, breadth, flow = 5.0 + (offset - 7) * 0.1, 0.8, 10.0
        industry = [
            *_background("industry", minute, "I"),
            _board("industry", "SEMI-I", "半导体设备Ⅱ", minute, change=change, breadth=breadth, flow=flow),
        ]
        concept = [
            *_background("concept", minute, "C"),
            _board("concept", "SEMI-C", "半导体材料", minute, change=change, breadth=breadth, flow=flow),
        ]
        snapshots.append(normalize_rotation_snapshot(industry, concept, minute_bucket=minute))

    view = analyze_rotation_snapshots(snapshots)
    assert len(view["attacking"]) == 1
    primary = view["attacking"][0]
    assert primary["family"]["key"] == "semiconductor_materials"
    assert len(primary["confirmations"]) == 1
    assert primary["expansion_eligible"] is True
    assert view["summary"]["counts"]["expanding_families"] == 1


def test_unclassified_strong_candidates_are_bounded_and_do_not_expand():
    snapshots = []
    for offset in range(7):
        minute = START + timedelta(minutes=offset)
        targets = []
        for index in range(4):
            change = -3.0 if offset < 5 else 5.0
            breadth = 0.3 if offset < 5 else 0.8
            flow = 0.0 if offset < 5 else 10.0
            targets.append(_board(
                "industry",
                f"U{index}",
                f"未知新题材{index}",
                minute,
                change=change,
                breadth=breadth,
                flow=flow,
            ))
        industry = [*_background("industry", minute, "I"), *targets]
        snapshots.append(normalize_rotation_snapshot(industry, _background("concept", minute, "C"), minute_bucket=minute))

    view = analyze_rotation_snapshots(snapshots)
    assert len(view["unclassified"]) == 3
    assert view["summary"]["counts"]["expanding_families"] == 0
    assert view["summary"]["structure"]["key"] != "broadening"


def test_coverage_collapse_freezes_entry_and_marks_partial():
    snapshots = _rotation_sequence()[:-1]
    minute = START + timedelta(minutes=6)
    snapshots.append(normalize_rotation_snapshot(
        [_board("industry", "T001", "半导体材料", minute, change=2, breadth=0.7, flow=10)],
        [_board("concept", "ONLY", "人工智能", minute, change=2, breadth=0.7, flow=10)],
        minute_bucket=minute,
    ))

    view = analyze_rotation_snapshots(snapshots)
    assert view["status"] == "partial"
    assert view["coverage"]["industry"]["frozen"] is True
    assert view["rotating"] == []


def test_critical_field_coverage_collapse_freezes_lifecycle():
    snapshots = _attack_sequence()
    for offset in (9, 10):
        minute = START + timedelta(minutes=offset)
        industry, concept = [], []
        for taxonomy, target in (
            ("industry", industry),
            ("concept", concept),
        ):
            records = _background(taxonomy, minute, taxonomy[0].upper())
            records.append(_board(
                taxonomy,
                "T001" if taxonomy == "industry" else "OTHER",
                "半导体材料" if taxonomy == "industry" else "概念背景",
                minute,
                change=-5,
                breadth=0.1,
                flow=-10,
            ))
            for record in records:
                record.pop("主力净流入")
                record.pop("主力净流入-占比")
            target.extend(records)
        snapshots.append(normalize_rotation_snapshot(industry, concept, minute_bucket=minute))

    view = analyze_rotation_snapshots(snapshots)
    assert view["status"] == "partial"
    assert view["coverage"]["industry"]["frozen"] is True
    assert view["attacking"][0]["board_code"] == "T001"
    assert view["cooling"] == []


def test_family_mapping_covers_requested_examples_and_known_styles():
    expected = {
        "AI应用": "ai_applications",
        "无人驾驶": "smart_driving",
        "脑机接口": "brain_computer",
        "半导体材料": "semiconductor_materials",
        "人形机器人": "humanoid_robotics",
        "证券Ⅱ": "financial",
        "油气开采": "resource_energy",
        "电力行业": "power_utilities",
    }
    assert {name: canonical_family(name)["key"] for name in expected} == expected


def test_core_offense_is_versioned_and_always_returns_eight_anchors():
    core = analyze_rotation_snapshots([])["core"]

    assert core["config_version"] == CORE_OFFENSE_CONFIG_VERSION
    assert core["status"] == "collecting"
    assert core["coverage"] == {
        "available": 0,
        "total": 8,
        "ratio": 0.0,
        "partial": True,
        "missing": [definition["key"] for definition in CORE_OFFENSE_DEFINITIONS],
    }
    assert [item["name"] for item in core["items"]] == [
        "半导体", "软件开发", "通信设备", "自动化设备",
        "消费电子", "电池", "光伏设备", "国防军工",
    ]
    assert all(item["level"] == "unknown" for item in core["items"])
    defense = core["items"][-1]
    assert defense["conditional"] is True
    assert defense["anchor_mode"] == "conditional_event"


def test_core_anchor_prefers_code_and_uses_exact_name_only_as_fallback():
    minute = START
    preferred = _board(
        "industry", "BK1036", "芯片行业更名", minute,
        change=-5, breadth=0.2, flow=-10,
    )
    same_name = _board(
        "industry", "ALT-SEMI", "半导体", minute,
        change=6, breadth=0.8, flow=10,
    )
    view = analyze_rotation_snapshots([
        normalize_rotation_snapshot(
            [*_background("industry", minute, "I"), preferred, same_name],
            _background("concept", minute, "C"),
            minute_bucket=minute,
        )
    ])
    semiconductor = _core_item(view, "semiconductor")
    assert semiconductor["representative_board"]["board_code"] == "BK1036"
    assert semiconductor["level"] == "weak"

    fallback = analyze_rotation_snapshots([
        _core_snapshot(
            minute,
            code="ALT-SEMI",
            name="半导体",
            change=6,
            breadth=0.8,
            flow=10,
        )
    ])
    semiconductor = _core_item(fallback, "semiconductor")
    assert semiconductor["representative_board"]["board_code"] == "ALT-SEMI"
    assert semiconductor["level"] == "strong"


def test_core_industry_level_is_independent_from_concept_confirmation_and_lifecycle():
    minute = START
    software = _board(
        "industry", "BK0737", "软件开发", minute,
        change=6, breadth=0.8, flow=10,
    )
    ai_application = _board(
        "concept", "BK1629", "AI应用", minute,
        change=-5, breadth=0.2, flow=-10,
    )
    view = analyze_rotation_snapshots([
        normalize_rotation_snapshot(
            [*_background("industry", minute, "I"), software],
            [*_background("concept", minute, "C"), ai_application],
            minute_bucket=minute,
        )
    ])

    core = _core_item(view, "software_development")
    assert core["level"] == "strong"
    assert core["evidence_count"] == core["evidence_total"] == 3
    assert core["representative_board"] == {
        "taxonomy": "industry",
        "board_code": "BK0737",
        "name": "软件开发",
        "provider_as_of": minute.isoformat(timespec="seconds"),
        "source": "industry-source",
    }
    assert core["confirmations"][0]["alignment"] == "divergent"
    assert core["confirmations"][0]["level"] == "weak"
    assert view["attacking"] == []
    assert view["rotating"] == []


def test_core_direction_reuses_ten_minute_history_but_resets_on_source_switch():
    snapshots = [
        _core_snapshot(
            START + timedelta(minutes=offset),
            change=-4 if offset < 10 else 6,
            breadth=0.2 if offset < 10 else 0.8,
            flow=-10 if offset < 10 else 10,
        )
        for offset in range(11)
    ]
    strengthened = _core_item(
        analyze_rotation_snapshots(snapshots),
        "semiconductor",
    )
    assert strengthened["level"] == "strong"
    assert strengthened["direction"] == "strengthening"
    assert strengthened["metrics"]["direction_window_minutes"] == 10
    assert strengthened["metrics"]["rank_delta_10m"] > 0

    switched_last = _core_snapshot(
        START + timedelta(minutes=10),
        change=6,
        breadth=0.8,
        flow=10,
        source="industry-fallback",
    )
    reset = _core_item(
        analyze_rotation_snapshots([*snapshots[:-1], switched_last]),
        "semiconductor",
    )
    assert reset["level"] == "strong"
    assert reset["direction"] == "insufficient"
    assert reset["metrics"]["direction_window_minutes"] is None


def test_core_does_not_publish_strength_from_a_collapsed_cross_section():
    baseline = _core_snapshot(
        START,
        change=-4,
        breadth=0.2,
        flow=-10,
    )
    minute = START + timedelta(minutes=1)
    collapsed = normalize_rotation_snapshot(
        [
            _board(
                "industry", "BK1036", "半导体", minute,
                change=8, breadth=0.9, flow=20,
            )
        ],
        [
            _board(
                "concept", "ONLY", "单一概念", minute,
                change=8, breadth=0.9, flow=20,
            )
        ],
        minute_bucket=minute,
    )

    semiconductor = _core_item(
        analyze_rotation_snapshots([baseline, collapsed]),
        "semiconductor",
    )
    assert semiconductor["representative_board"]["board_code"] == "BK1036"
    assert semiconductor["level"] == "unknown"
    assert semiconductor["evidence_count"] == 0
    assert semiconductor["coverage"]["status"] == "stale"


def test_result_lists_and_events_are_bounded():
    view = analyze_rotation_snapshots(_attack_sequence())
    assert len(view["attacking"]) <= 8
    assert len(view["rotating"]) <= 8
    assert len(view["cooling"]) <= 8
    assert len(view["unclassified"]) <= 3
    assert len(view["events"]) <= 12
    assert set(view["summary"]) == {
        "strength", "direction", "structure", "counts", "current_leaders"
    }


def test_cooling_expires_after_thirty_effective_market_steps():
    snapshots = _attack_sequence()
    snapshots.extend([
        _snapshot(START + timedelta(minutes=9), target_change=-4, target_breadth=0.2, target_flow=-10),
        _snapshot(START + timedelta(minutes=10), target_change=-5, target_breadth=0.1, target_flow=-11),
    ])
    for offset in range(11, 41):
        snapshots.append(_snapshot(START + timedelta(minutes=offset), include_target=False))

    view = analyze_rotation_snapshots(snapshots)
    assert view["cooling"] == []
    assert any(event["to"] == "expired" for event in view["events"])
