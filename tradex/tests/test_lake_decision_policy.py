"""Focused tests for the conservative shadow allocation policy."""

from __future__ import annotations

import copy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tradex.data_lake.contracts import FeatureSnapshot
from tradex.data_lake.decision_policy import (
    CRITICAL_COMPONENTS,
    POLICY_VERSION,
    ShadowPolicyV1,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
EFFECTIVE_AT = datetime(2026, 8, 19, 10, 5, tzinfo=SHANGHAI)


def _component_status(name: str) -> dict:
    """Mirror the status shape emitted by risk_service._component_status."""

    status = {
        "source": "fixture",
        "fetched_at": "2026-08-19T10:05:00+08:00",
        "as_of": "2026-08-19T10:05:00+08:00",
        "provider_as_of": "2026-08-19T10:05:00+08:00",
        "cache_identity": None,
        "source_valid": True,
        "data_date": None,
        "trade_status": None,
        "pool_total": None,
        "unique_total": None,
        "reason_coverage": None,
        "board_count_coverage": None,
        "unknown_board_count": None,
        "industry_profile_status": None,
        "valid_empty": False,
        "page_count": None,
        "age_seconds": 0.0,
        "stale": False,
        "expired": False,
        "refreshing": False,
        "closed_session_fallback": False,
        "error": None,
    }
    if name == "leadership_pool":
        status.update({
            "data_date": "20260819",
            "trade_status": {"id": "3", "name": "交易中"},
            "pool_total": 3,
            "unique_total": 3,
            "reason_coverage": 1.0,
            "board_count_coverage": 1.0,
            "unknown_board_count": 0,
            "page_count": 1,
            "eligible_for_vote": True,
        })
    return status


def _payload(
    *,
    emotion: str = "strong",
    tone: str = "positive",
    quality: str = "high",
    evidence_available: int = 47,
    evidence_total: int = 47,
) -> dict:
    return {
        "opening_observation": False,
        "stale": False,
        "emotion": {"key": emotion},
        "structure": {"tone": tone},
        "data_quality": {
            "key": quality,
            "stale": False,
            "evidence_available": evidence_available,
            "evidence_total": evidence_total,
        },
        "components": {
            name: _component_status(name) for name in CRITICAL_COMPONENTS
        },
    }


def _feature(payload: dict, *, feature_id: str = "feature-1") -> FeatureSnapshot:
    return FeatureSnapshot(
        feature_id=feature_id,
        capture_id="capture-1",
        name="risk_appetite",
        schema_version="risk-feature-v1",
        config_version="risk-appetite-v1.3",
        payload=payload,
        input_artifact_ids=("artifact-1",),
        code_sha="deadbeef",
        created_at=EFFECTIVE_AT,
    )


def test_policy_rejects_every_non_shadow_mode():
    assert ShadowPolicyV1().mode == "shadow"
    with pytest.raises(ValueError, match="must be 'shadow'"):
        ShadowPolicyV1(mode="execute")


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda value: value["data_quality"].update(key="low"), "data_quality_low"),
        (lambda value: value.update(stale=True), "feature_stale"),
        (
            lambda value: value["components"]["market_breadth"].update(stale=True),
            "critical_component_stale:market_breadth",
        ),
        (
            lambda value: value["components"]["industry_quotes"].update(partial=True),
            "critical_component_partial:industry_quotes",
        ),
        (
            lambda value: value["components"]["concept_quotes"].update(
                error="provider disconnected"
            ),
            "critical_component_error:concept_quotes",
        ),
        (
            lambda value: value["components"]["market_breadth"].update(
                source_valid=False
            ),
            "critical_component_source_invalid:market_breadth",
        ),
        (
            lambda value: value["components"]["industry_quotes"].update(
                fetched_at=None,
                provider_as_of=" ",
            ),
            "critical_component_observation_time_missing:industry_quotes",
        ),
        (
            lambda value: value["components"]["leadership_pool"].update(
                eligible_for_vote=False
            ),
            "critical_component_ineligible:leadership_pool",
        ),
        (
            lambda value: value["components"]["leadership_pool"].update(
                pool_total=0,
                unique_total=0,
                valid_empty=False,
            ),
            "critical_component_empty:leadership_pool",
        ),
        (
            lambda value: value["components"]["concept_quotes"].update(records=[]),
            "critical_component_empty:concept_quotes",
        ),
        (
            lambda value: value.update(opening_observation=True),
            "opening_observation",
        ),
        (lambda value: value["emotion"].update(key="unknown"), "emotion_unknown"),
        (lambda value: value["emotion"].update(key="opening"), "emotion_opening"),
    ],
)
def test_hard_gates_abstain_to_cash_and_record_reason(mutate, reason):
    payload = _payload()
    mutate(payload)

    decision = ShadowPolicyV1().decide(_feature(payload), EFFECTIVE_AT)

    assert decision.mode == "shadow"
    assert decision.policy_version == POLICY_VERSION
    assert (
        decision.offense_weight,
        decision.defense_weight,
        decision.cash_weight,
    ) == (0.0, 0.0, 1.0)
    assert decision.confidence == 0.0
    assert reason in decision.abstain_reason.split(";")
    assert reason in decision.contributions["gates"]["reasons"]
    assert decision.contributions["candidate_kind"] == "shadow_only"


def test_explicit_valid_empty_leadership_pool_remains_eligible():
    payload = _payload(emotion="medium", tone="neutral")
    payload["components"]["leadership_pool"].update({
        "provider_as_of": None,
        "pool_total": 0,
        "unique_total": 0,
        "valid_empty": True,
        "page_count": 0,
    })

    decision = ShadowPolicyV1().decide(_feature(payload), EFFECTIVE_AT)

    assert decision.abstain_reason is None
    assert (
        decision.offense_weight,
        decision.defense_weight,
        decision.cash_weight,
    ) == (0.28, 0.35, 0.37)
    leadership_audit = decision.contributions["inputs"]["critical_components"][
        "leadership_pool"
    ]
    assert leadership_audit["valid_empty"] is True
    assert leadership_audit["record_counts"]["pool_total"] == 0


@pytest.mark.parametrize(
    ("emotion", "tone", "expected"),
    [
        ("strong", "positive", (0.50, 0.24, 0.26)),
        ("strong", "neutral", (0.42, 0.28, 0.30)),
        ("strong", "negative", (0.36, 0.34, 0.30)),
        ("medium", "positive", (0.36, 0.31, 0.33)),
        ("medium", "neutral", (0.28, 0.35, 0.37)),
        ("medium", "negative", (0.22, 0.41, 0.37)),
        ("weak", "positive", (0.20, 0.41, 0.39)),
        ("weak", "neutral", (0.12, 0.45, 0.43)),
        ("weak", "negative", (0.06, 0.51, 0.43)),
    ],
)
def test_all_supported_states_produce_conservative_normalized_candidates(
    emotion, tone, expected
):
    decision = ShadowPolicyV1().decide(
        _feature(_payload(emotion=emotion, tone=tone)),
        EFFECTIVE_AT,
    )

    assert decision.abstain_reason is None
    assert (
        decision.offense_weight,
        decision.defense_weight,
        decision.cash_weight,
    ) == expected
    assert sum(expected) == pytest.approx(1.0)
    construction = decision.contributions["weight_construction"]
    assert construction["sum"] == 1.0
    assert construction["normalized"] == {
        "offense": expected[0],
        "defense": expected[1],
        "cash": expected[2],
    }
    assert decision.contributions["gates"] == {"passed": True, "reasons": []}


def test_confidence_uses_evidence_coverage_quality_and_never_exceeds_point_75():
    policy = ShadowPolicyV1()
    full_high = policy.decide(_feature(_payload()), EFFECTIVE_AT)
    half_high = policy.decide(
        _feature(_payload(evidence_available=20, evidence_total=40), feature_id="half"),
        EFFECTIVE_AT,
    )
    full_medium = policy.decide(
        _feature(_payload(quality="medium"), feature_id="medium"),
        EFFECTIVE_AT,
    )

    assert full_high.confidence == 0.75
    assert half_high.confidence == 0.375
    assert full_medium.confidence == 0.60
    assert all(item.confidence <= 0.75 for item in (full_high, half_high, full_medium))


def test_same_feature_and_effective_time_are_fully_deterministic():
    payload = _payload(emotion="medium", tone="neutral")
    feature = _feature(payload)
    policy = ShadowPolicyV1()

    first = policy.decide(feature, EFFECTIVE_AT)
    second = policy.decide(feature, EFFECTIVE_AT)

    assert first == second
    assert first.decision_id.startswith("shadow-")
    assert len(first.decision_id) == len("shadow-") + 64
    assert first.contributions["candidate_kind"] == "shadow_only"

    changed_payload = copy.deepcopy(payload)
    changed_payload["emotion"]["key"] = "weak"
    # Identity is intentionally feature-version based: callers must issue a new
    # feature_id when its immutable payload changes.
    changed = policy.decide(_feature(changed_payload, feature_id="feature-2"), EFFECTIVE_AT)
    assert changed.decision_id != first.decision_id
