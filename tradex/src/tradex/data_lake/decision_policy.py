"""Conservative, auditable shadow-allocation policy for lake features."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Mapping

from .contracts import DecisionDraft, FeatureSnapshot
from .serde import sha256_hex


POLICY_VERSION = "shadow-allocation-v1"
CRITICAL_COMPONENTS = (
    "market_breadth",
    "industry_quotes",
    "concept_quotes",
    "leadership_pool",
)
_WEIGHT_NAMES = ("offense", "defense", "cash")

# These are deliberately conservative candidates, not execution weights.  The
# cash floor remains meaningful even when both the broad market emotion and
# structure are positive.
_EMOTION_BASES: dict[str, dict[str, float]] = {
    "strong": {"offense": 0.42, "defense": 0.28, "cash": 0.30},
    "medium": {"offense": 0.28, "defense": 0.35, "cash": 0.37},
    "weak": {"offense": 0.12, "defense": 0.45, "cash": 0.43},
}
_STRUCTURE_DELTAS: dict[str, dict[str, float]] = {
    "positive": {"offense": 0.08, "defense": -0.04, "cash": -0.04},
    "neutral": {"offense": 0.00, "defense": 0.00, "cash": 0.00},
    "negative": {"offense": -0.06, "defense": 0.06, "cash": 0.00},
}
_QUALITY_CONFIDENCE_FACTOR = {"high": 0.75, "medium": 0.60}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _present(value: Any) -> bool:
    """Return whether a status value carries non-blank evidence."""

    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _record_count_evidence(status: Mapping[str, Any]) -> dict[str, int]:
    """Extract only explicit record-count evidence from a component status.

    ``risk_service`` does not currently embed records in every status, so an
    absent count must remain unknown rather than being treated as an empty
    response.  Sources that do expose records or a count can still be gated
    conservatively.
    """

    evidence: dict[str, int] = {}
    if "records" in status:
        records = status.get("records")
        if records is None:
            evidence["records"] = 0
        elif isinstance(records, Mapping):
            evidence["records"] = 1
        elif not isinstance(records, (str, bytes)):
            try:
                evidence["records"] = len(records)
            except TypeError:
                pass

    for key in ("record_count", "row_count", "returned_total", "pool_total"):
        value = status.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= 0
            and float(value).is_integer()
        ):
            evidence[key] = int(value)
    return evidence


def _coverage(data_quality: Mapping[str, Any]) -> float:
    """Read evidence coverage, falling back to the sector-coverage fields."""

    candidates = (
        (data_quality.get("evidence_available"), data_quality.get("evidence_total")),
        (data_quality.get("available"), data_quality.get("total")),
    )
    for available, total in candidates:
        if (
            isinstance(available, (int, float))
            and not isinstance(available, bool)
            and isinstance(total, (int, float))
            and not isinstance(total, bool)
            and math.isfinite(float(available))
            and math.isfinite(float(total))
            and float(total) > 0
        ):
            return min(1.0, max(0.0, float(available) / float(total)))
    return 0.0


def _normalize_weights(raw: Mapping[str, float]) -> dict[str, float]:
    total = sum(float(raw[name]) for name in _WEIGHT_NAMES)
    if not math.isfinite(total) or total <= 0:
        raise ValueError("candidate weights must have a finite positive sum")
    offense = round(float(raw["offense"]) / total, 12)
    defense = round(float(raw["defense"]) / total, 12)
    # Assign the rounding residual to cash so the persisted contract sums to 1.
    cash = round(1.0 - offense - defense, 12)
    normalized = {"offense": offense, "defense": defense, "cash": cash}
    if any(value < 0 for value in normalized.values()):
        raise ValueError("candidate weights must not be negative")
    return normalized


class ShadowPolicyV1:
    """Produce versioned, non-executable offense/defense/cash candidates."""

    policy_version = POLICY_VERSION

    def __init__(self, *, mode: str = "shadow") -> None:
        if mode != "shadow":
            raise ValueError("ShadowPolicyV1 mode must be 'shadow'")
        self.mode = mode

    @staticmethod
    def _decision_id(feature: FeatureSnapshot, effective_at: datetime) -> str:
        digest = sha256_hex({
            "feature_id": feature.feature_id,
            "policy_version": POLICY_VERSION,
            "effective_at": effective_at,
        })
        return f"shadow-{digest}"

    @staticmethod
    def _gate_reasons(payload: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
        reasons: list[str] = []
        quality = _mapping(payload.get("data_quality"))
        quality_key = str(quality.get("key") or "unknown").lower()
        emotion = _mapping(payload.get("emotion"))
        emotion_key = str(emotion.get("key") or "unknown").lower()
        structure = _mapping(payload.get("structure"))
        structure_tone = str(structure.get("tone") or "unknown").lower()

        if quality_key == "low":
            reasons.append("data_quality_low")
        elif quality_key not in _QUALITY_CONFIDENCE_FACTOR:
            reasons.append("data_quality_unknown")
        if bool(payload.get("stale")) or bool(quality.get("stale")):
            reasons.append("feature_stale")
        if bool(payload.get("opening_observation")):
            reasons.append("opening_observation")
        if emotion_key in {"unknown", "opening"}:
            reasons.append(f"emotion_{emotion_key}")
        elif emotion_key not in _EMOTION_BASES:
            reasons.append("emotion_unsupported")
        if structure_tone not in _STRUCTURE_DELTAS:
            reasons.append("structure_tone_unknown")

        components = _mapping(payload.get("components"))
        component_audit: dict[str, Any] = {}
        for name in CRITICAL_COMPONENTS:
            status = components.get(name)
            if not isinstance(status, Mapping):
                component_audit[name] = {"present": False}
                reasons.append(f"critical_component_missing:{name}")
                continue
            record_counts = _record_count_evidence(status)
            flags = {
                "present": True,
                "stale": bool(status.get("stale")),
                "partial": bool(status.get("partial")),
                "expired": bool(status.get("expired")),
                "error": _present(status.get("error")),
                "source_valid": status.get("source_valid"),
                "eligible_for_vote": status.get("eligible_for_vote"),
                "has_observation_time": (
                    _present(status.get("fetched_at"))
                    or _present(status.get("provider_as_of"))
                ),
                "valid_empty": status.get("valid_empty") is True,
                "record_counts": record_counts,
            }
            component_audit[name] = flags
            if flags["stale"] or flags["expired"]:
                reasons.append(f"critical_component_stale:{name}")
            if flags["partial"]:
                reasons.append(f"critical_component_partial:{name}")
            if flags["error"]:
                reasons.append(f"critical_component_error:{name}")
            if flags["source_valid"] is False:
                reasons.append(f"critical_component_source_invalid:{name}")
            if not flags["has_observation_time"]:
                reasons.append(f"critical_component_observation_time_missing:{name}")
            if flags["eligible_for_vote"] is False:
                reasons.append(f"critical_component_ineligible:{name}")
            if any(count == 0 for count in record_counts.values()) and not flags[
                "valid_empty"
            ]:
                reasons.append(f"critical_component_empty:{name}")

        # Preserve declaration order while preventing duplicated reason codes.
        reasons = list(dict.fromkeys(reasons))
        audit = {
            "quality_key": quality_key,
            "emotion_key": emotion_key,
            "structure_tone": structure_tone,
            "critical_components": component_audit,
        }
        return reasons, audit

    def decide(
        self,
        feature: FeatureSnapshot,
        effective_at: datetime,
    ) -> DecisionDraft:
        """Return a deterministic shadow candidate tied to one feature snapshot."""

        if not isinstance(feature, FeatureSnapshot):
            raise TypeError("feature must be a FeatureSnapshot")
        payload = feature.payload
        reasons, gate_audit = self._gate_reasons(payload)
        quality = _mapping(payload.get("data_quality"))
        coverage = _coverage(quality)
        quality_key = gate_audit["quality_key"]
        emotion_key = gate_audit["emotion_key"]
        structure_tone = gate_audit["structure_tone"]

        if reasons:
            normalized = {"offense": 0.0, "defense": 0.0, "cash": 1.0}
            contributions = {
                "candidate_kind": "shadow_only",
                "gates": {"passed": False, "reasons": reasons},
                "inputs": gate_audit,
                "confidence": {
                    "evidence_coverage": coverage,
                    "quality_key": quality_key,
                    "value": 0.0,
                    "cap": 0.75,
                },
                "weight_construction": {
                    "normalized": normalized,
                    "sum": 1.0,
                },
            }
            return DecisionDraft(
                decision_id=self._decision_id(feature, effective_at),
                feature_id=feature.feature_id,
                policy_version=self.policy_version,
                effective_at=effective_at,
                offense_weight=0.0,
                defense_weight=0.0,
                cash_weight=1.0,
                contributions=contributions,
                confidence=0.0,
                abstain_reason=";".join(reasons),
                mode=self.mode,
            )

        base = dict(_EMOTION_BASES[emotion_key])
        delta = dict(_STRUCTURE_DELTAS[structure_tone])
        raw = {name: base[name] + delta[name] for name in _WEIGHT_NAMES}
        normalized = _normalize_weights(raw)
        quality_factor = _QUALITY_CONFIDENCE_FACTOR[quality_key]
        confidence = round(min(0.75, coverage * quality_factor), 6)
        contributions = {
            "candidate_kind": "shadow_only",
            "gates": {"passed": True, "reasons": []},
            "inputs": gate_audit,
            "confidence": {
                "evidence_coverage": coverage,
                "quality_key": quality_key,
                "quality_factor": quality_factor,
                "value": confidence,
                "cap": 0.75,
            },
            "weight_construction": {
                "emotion_base": base,
                "structure_delta": delta,
                "raw": raw,
                "normalized": normalized,
                "sum": round(sum(normalized.values()), 12),
            },
        }
        return DecisionDraft(
            decision_id=self._decision_id(feature, effective_at),
            feature_id=feature.feature_id,
            policy_version=self.policy_version,
            effective_at=effective_at,
            offense_weight=normalized["offense"],
            defense_weight=normalized["defense"],
            cash_weight=normalized["cash"],
            contributions=contributions,
            confidence=confidence,
            abstain_reason=None,
            mode=self.mode,
        )


__all__ = ["CRITICAL_COMPONENTS", "POLICY_VERSION", "ShadowPolicyV1"]
