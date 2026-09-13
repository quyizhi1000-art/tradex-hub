"""Provider-neutral public service for the Smart Sector Library."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date

from tradex.instrument_taxonomy.contracts import (
    BusinessSegmentV1,
    StockRelationshipProfileV1,
)

from .contracts import (
    SmartSectorCandidateV1,
    SmartSectorDecisionV1,
    SmartSectorPolicyV1,
)
from .core import (
    MarketThemeMatch,
    ReviewedMarketAttributionV1,
    infer_business_categories,
    infer_current_market_categories,
    infer_current_market_category,
    load_reviewed_market_attributions,
    market_context_has_known_theme,
)


class SmartSectorLibrary:
    """Resolve one stock's market-facing sector for one exact trade date.

    The service owns no provider calls, cache, or stable-taxonomy writes.  A
    caller supplies market context and disclosed business evidence; the
    library either returns a reviewed/rule-supported decision or abstains.
    """

    policy = SmartSectorPolicyV1()

    _STAGE_SCORE = {
        "operating_output": 60,
        "revenue": 55,
        "orders": 50,
        "delivery": 48,
        "project": 40,
        "product": 35,
        "pilot": 20,
    }
    _MATERIALITY_SCORE = {
        "dominant": 45,
        "meaningful": 25,
        "emerging": 5,
        "unknown": 0,
    }
    _MARKET_SCORE = {
        "leading_cluster": 35,
        "co_movement": 25,
        "reason_only": 20,
        "none": 0,
    }
    _RELATION_SCORE = {
        "direct_product_or_service": 8,
        "system_function": 10,
        "operating_asset_or_output": 10,
        "downstream_demand": 5,
        "material_domain": 0,
        "commercialized_new_business": 25,
    }
    _EVIDENCE_QUALITY_SCORE = {
        "verified": 12,
        "corroborated": 10,
        "provider_only": 0,
        "unverified": 0,
    }
    _VERIFIED_MARKET_CLUSTER_BONUS = 45
    _REJECTED_RELATIONS = {"capital_relation", "production_method", "statistical_industry"}
    _REJECTED_STAGES = {"plan", "investment", "rumor"}

    def __init__(
        self,
        effective_on: date,
        *,
        reviewed_attributions: Mapping[str, ReviewedMarketAttributionV1] | None = None,
    ) -> None:
        self.effective_on = effective_on
        self._reviewed = dict(
            load_reviewed_market_attributions(effective_on)
            if reviewed_attributions is None
            else reviewed_attributions
        )
        if any(item.effective_on != effective_on for item in self._reviewed.values()):
            raise ValueError("smart sector reviews must match the effective trade date")

    def resolve(
        self,
        instrument_id: str,
        *,
        market_context: str | None,
        business_evidence: Iterable[str],
    ) -> SmartSectorDecisionV1:
        """Return an accepted exact-date category or an explicit abstention."""

        reviewed = self._reviewed.get(instrument_id)
        if reviewed is not None and reviewed.review_basis == "human_review_override":
            return SmartSectorDecisionV1(
                instrument_id=instrument_id,
                effective_on=self.effective_on,
                category_key=reviewed.category_key,
                category_name=reviewed.category_name,
                basis="manual_market_review",
                rule_id=reviewed.rule_id,
                review_basis=reviewed.review_basis,
                logic_type="human_exception",
                quality="accepted",
            )

        normalized_evidence = tuple(
            str(item or "").strip()
            for item in business_evidence
            if str(item or "").strip()
        )
        candidates = (
            self.build_candidates(
                market_context=market_context,
                business_evidence=normalized_evidence,
            )
            if str(market_context or "").strip() and normalized_evidence
            else ()
        )
        if candidates:
            return self.resolve_candidates(instrument_id, candidates)

        if reviewed is not None and reviewed.review_basis == "rule_supported":
            inferred = infer_current_market_category(
                reviewed.reason_text,
                reviewed.business_evidence,
            )
            if inferred is not None:
                return SmartSectorDecisionV1(
                    instrument_id=instrument_id,
                    effective_on=self.effective_on,
                    category_key=inferred.category_key,
                    category_name=inferred.category_name,
                    basis="event_business_crosscheck",
                    rule_id=inferred.rule_id,
                    logic_type=inferred.logic_type,
                    quality="accepted",
                )

        flags = []
        if not str(market_context or "").strip():
            flags.append("market_context_missing")
        if not normalized_evidence:
            flags.append("business_evidence_missing")
        if not flags:
            flags.append("no_supported_market_business_crosscheck")
        return SmartSectorDecisionV1(
            instrument_id=instrument_id,
            effective_on=self.effective_on,
            basis="unresolved",
            quality="degraded",
            quality_flags=tuple(flags),
        )

    def build_candidates(
        self,
        *,
        market_context: str | None,
        business_evidence: Iterable[str],
        business_segments: Iterable[BusinessSegmentV1] = (),
        source_evidence_ids: Iterable[str] = (),
        evidence_quality: str = "unverified",
    ) -> tuple[SmartSectorCandidateV1, ...]:
        """Build market-aligned and operating-baseline candidates from evidence."""

        normalized = tuple(
            str(item or "").strip()
            for item in business_evidence
            if str(item or "").strip()
        )
        if not str(market_context or "").strip() or not normalized:
            return ()
        segments = tuple(BusinessSegmentV1.model_validate(item) for item in business_segments)
        source_ids = tuple(
            dict.fromkeys(str(item).strip() for item in source_evidence_ids if str(item).strip())
        )
        candidates: list[SmartSectorCandidateV1] = []
        for match in infer_current_market_categories(market_context, normalized):
            candidates.append(self._candidate_from_match(
                match,
                segments=segments,
                market_alignment="reason_only",
                source_evidence_ids=source_ids,
                evidence_quality=evidence_quality,
            ))
        if market_context_has_known_theme(market_context):
            for match in infer_business_categories(normalized):
                candidates.append(self._candidate_from_match(
                    match,
                    segments=segments,
                    market_alignment="none",
                    source_evidence_ids=source_ids,
                    evidence_quality=evidence_quality,
                ))
        unique: dict[tuple[object, ...], SmartSectorCandidateV1] = {}
        for candidate in candidates:
            identity = (
                candidate.category_key,
                candidate.relation_type,
                candidate.evidence_stage,
                candidate.materiality,
                candidate.market_alignment,
                candidate.granularity,
                candidate.evidence_quality,
            )
            unique.setdefault(identity, candidate)
        return tuple(unique.values())

    def candidates_from_relationship(
        self,
        *,
        market_context: str | None,
        relationship: StockRelationshipProfileV1,
    ) -> tuple[SmartSectorCandidateV1, ...]:
        business_evidence = (
            relationship.primary_business_name or "",
            relationship.directory_category_name or "",
            relationship.business_summary or "",
            *relationship.business_tags,
            *(segment.name for segment in relationship.business_segments),
        )
        return self.build_candidates(
            market_context=market_context,
            business_evidence=business_evidence,
            business_segments=relationship.business_segments,
            source_evidence_ids=(item.evidence_id for item in relationship.evidence),
            evidence_quality=(
                relationship.verification_status
                if relationship.verification_status
                in {"verified", "corroborated", "provider_only"}
                else "unverified"
            ),
        )

    def resolve_relationships(
        self,
        observations: Iterable[
            tuple[str, str | None, StockRelationshipProfileV1 | None]
        ],
    ) -> dict[str, SmartSectorDecisionV1]:
        """Resolve a same-snapshot pool and derive cluster alignment once."""

        prepared = tuple(observations)
        if len({item[0] for item in prepared}) != len(prepared):
            raise ValueError("smart sector observations contain duplicate instruments")
        candidates_by_id: dict[str, tuple[SmartSectorCandidateV1, ...]] = {}
        market_category_counts: Counter[str] = Counter()
        reasons: dict[str, str | None] = {}
        for instrument_id, market_context, relationship in prepared:
            reasons[instrument_id] = market_context
            candidates = (
                self.candidates_from_relationship(
                    market_context=market_context,
                    relationship=relationship,
                )
                if relationship is not None
                else ()
            )
            reviewed = self._reviewed.get(instrument_id)
            if reviewed is not None and reviewed.review_basis == "rule_supported":
                reviewed_candidates = self.build_candidates(
                    market_context=reviewed.reason_text,
                    business_evidence=reviewed.business_evidence,
                    source_evidence_ids=(item.evidence_id for item in reviewed.evidence),
                    evidence_quality="corroborated",
                )
                candidates = tuple((*candidates, *(
                    candidate
                    for candidate in reviewed_candidates
                    if (
                        candidate.category_key,
                        candidate.category_name,
                    ) == (reviewed.category_key, reviewed.category_name)
                )))
            candidates_by_id[instrument_id] = candidates
            market_category_counts.update({
                candidate.category_key
                for candidate in candidates
                if candidate.market_alignment == "reason_only"
            })

        clustered = [count for count in market_category_counts.values() if count >= 2]
        leading_count = max(clustered, default=0)
        decisions: dict[str, SmartSectorDecisionV1] = {}
        for instrument_id, _market_context, relationship in prepared:
            if not str(reasons[instrument_id] or "").strip():
                decisions[instrument_id] = self._unresolved_candidate_decision(
                    instrument_id,
                    "market_context_missing",
                )
                continue
            if relationship is None:
                decisions[instrument_id] = self._unresolved_candidate_decision(
                    instrument_id,
                    "business_evidence_missing",
                )
                continue
            adjusted = []
            for candidate in candidates_by_id[instrument_id]:
                count = market_category_counts[candidate.category_key]
                alignment = candidate.market_alignment
                if alignment == "reason_only" and count >= 2:
                    alignment = (
                        "leading_cluster" if count == leading_count else "co_movement"
                    )
                adjusted.append(candidate.model_copy(update={"market_alignment": alignment}))
            decisions[instrument_id] = self.resolve_candidates(instrument_id, adjusted)
        return decisions

    @staticmethod
    def _segment_materiality(
        match: MarketThemeMatch,
        segments: tuple[BusinessSegmentV1, ...],
    ) -> tuple[str, str]:
        matched = []
        for segment in segments:
            if any(
                value == segment.name
                or value in segment.name
                or segment.name in value
                for value in match.matched_business_values
            ):
                matched.append(segment)
        shares = [segment.revenue_share for segment in matched if segment.revenue_share is not None]
        if not shares:
            return match.evidence_stage, match.materiality
        share = min(sum(shares), 1.0)
        materiality = (
            "dominant"
            if share >= 0.5
            else "meaningful"
            if share >= 0.05
            else "emerging"
        )
        has_revenue = any(segment.revenue_cny is not None for segment in matched)
        return ("revenue" if has_revenue else match.evidence_stage), materiality

    def _candidate_from_match(
        self,
        match: MarketThemeMatch,
        *,
        segments: tuple[BusinessSegmentV1, ...],
        market_alignment: str,
        source_evidence_ids: tuple[str, ...],
        evidence_quality: str,
    ) -> SmartSectorCandidateV1:
        evidence_stage, materiality = self._segment_materiality(match, segments)
        refs = tuple(dict.fromkeys((
            *source_evidence_ids,
            *(f"business:{value}" for value in match.matched_business_values),
            *(
                (f"market:{match.rule_id}",)
                if market_alignment != "none"
                else ()
            ),
        )))
        return SmartSectorCandidateV1(
            category_key=match.category_key,
            category_name=match.category_name,
            relation_type=match.logic_type,
            evidence_stage=evidence_stage,
            materiality=materiality,
            market_alignment=market_alignment,
            granularity=match.granularity,
            direct_business_evidence=True,
            evidence_quality=evidence_quality,
            evidence_refs=refs or (f"business_rule:{match.rule_id}",),
        )

    def resolve_candidates(
        self,
        instrument_id: str,
        candidates: Iterable[SmartSectorCandidateV1],
    ) -> SmartSectorDecisionV1:
        """Rank structured evidence without assuming one universal value-chain direction.

        Capital links, production methods, statistical industries, plans and rumors
        cannot win.  Among real business candidates, commercial maturity,
        materiality, same-day market alignment and useful granularity all contribute.
        This lets an operating new business outrank an untraded legacy business while
        still falling back to the dominant real business when a hot concept is weak.
        """

        reviewed = self._reviewed.get(instrument_id)
        if reviewed is not None and reviewed.review_basis == "human_review_override":
            return SmartSectorDecisionV1(
                instrument_id=instrument_id,
                effective_on=self.effective_on,
                category_key=reviewed.category_key,
                category_name=reviewed.category_name,
                basis="manual_market_review",
                rule_id=reviewed.rule_id,
                review_basis=reviewed.review_basis,
                logic_type="human_exception",
                quality="accepted",
            )

        accepted: list[tuple[int, SmartSectorCandidateV1]] = []
        for candidate in candidates:
            if not candidate.direct_business_evidence:
                continue
            if candidate.relation_type in self._REJECTED_RELATIONS:
                continue
            if candidate.evidence_stage in self._REJECTED_STAGES:
                continue
            score = (
                self._STAGE_SCORE[candidate.evidence_stage]
                + self._MATERIALITY_SCORE[candidate.materiality]
                + self._MARKET_SCORE[candidate.market_alignment]
                + self._RELATION_SCORE[candidate.relation_type]
                + self._EVIDENCE_QUALITY_SCORE[candidate.evidence_quality]
                + (5 if candidate.granularity == "specific" else 0)
            )
            if (
                candidate.evidence_quality in {"verified", "corroborated"}
                and candidate.market_alignment in {"leading_cluster", "co_movement"}
            ):
                score += self._VERIFIED_MARKET_CLUSTER_BONUS
            accepted.append((score, candidate))

        if not accepted:
            return self._unresolved_candidate_decision(
                instrument_id,
                "no_eligible_evidence_candidate",
            )

        best_score = max(score for score, _ in accepted)
        winners = [candidate for score, candidate in accepted if score == best_score]
        winning_categories = {
            (candidate.category_key, candidate.category_name) for candidate in winners
        }
        if len(winning_categories) != 1:
            return self._unresolved_candidate_decision(
                instrument_id,
                "ambiguous_top_evidence_candidates",
            )

        winner = winners[0]
        return SmartSectorDecisionV1(
            instrument_id=instrument_id,
            effective_on=self.effective_on,
            category_key=winner.category_key,
            category_name=winner.category_name,
            basis="evidence_candidate_ranking",
            rule_id="smart_sector_candidate_ranking_v1",
            logic_type=winner.relation_type,
            quality="accepted",
        )

    def _unresolved_candidate_decision(
        self,
        instrument_id: str,
        flag: str,
    ) -> SmartSectorDecisionV1:
        return SmartSectorDecisionV1(
            instrument_id=instrument_id,
            effective_on=self.effective_on,
            basis="unresolved",
            quality="degraded",
            quality_flags=(flag,),
        )


__all__ = ["SmartSectorLibrary"]
