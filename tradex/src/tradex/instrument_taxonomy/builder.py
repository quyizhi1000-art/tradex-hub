"""Build strict relationship profiles from provider-normalized source rows."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from .contracts import (
    BusinessSegmentV1,
    EvidenceRefV1,
    IndustryPathV1,
    StockRelationshipCatalogStatusV1,
    StockRelationshipProfileV1,
)
from .normalization import (
    business_domain_by_business_key,
    clean_segment_name,
    matched_business_rules,
    reviewed_directory_category,
)


TUSHARE_STOCK_COMPANY_DOC = "https://tushare.pro/document/2?doc_id=112"
TUSHARE_MAINBZ_DOC = "https://tushare.pro/document/2?doc_id=81"
TUSHARE_SW_MEMBER_DOC = "https://tushare.pro/document/2?doc_id=335"
_SEGMENT_REQUIRED_BUSINESS_KEYS = frozenset({
    "textiles",
    "fluorochemicals",
    "traditional_chinese_medicine",
})
_BROAD_ROLLUP_BUSINESS_KEYS = frozenset({
    "textiles",
    "traditional_chinese_medicine",
})
_MIN_BROAD_ROLLUP_REVENUE_SHARE = 0.20
_MIN_PRIMARY_RULE_REVENUE_SHARE = 0.20


def _text(value: Any) -> str | None:
    result = str(value or "").strip()
    return result or None


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _date(value: Any) -> date | None:
    raw = str(value or "").strip().replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _official_evidence_path() -> Path:
    return Path(str(files(__package__).joinpath("official_evidence.v1.json")))


def load_official_evidence(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    target = Path(path) if path is not None else _official_evidence_path()
    payload = json.loads(target.read_text(encoding="utf-8"))
    if (
        payload.get("contract") != "instrument_taxonomy_official_evidence.v1"
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("profiles"), list)
    ):
        raise ValueError("official taxonomy evidence contract is invalid")
    result: dict[str, dict[str, Any]] = {}
    for item in payload["profiles"]:
        if not isinstance(item, dict):
            raise ValueError("official taxonomy evidence contains a non-object profile")
        instrument_id = _text(item.get("instrument_id"))
        if instrument_id is None or instrument_id in result:
            raise ValueError("official taxonomy evidence contains an invalid instrument")
        # Validate evidence at load time so an invalid manual entry can never
        # silently enter the accepted catalog.
        evidence = tuple(EvidenceRefV1.model_validate(row) for row in item.get("evidence", ()))
        result[instrument_id] = {**item, "evidence": evidence}
    return result


def _structured_evidence(
    instrument_id: str,
    *,
    generated_at: datetime,
    business_summary: str | None,
    segments: tuple[BusinessSegmentV1, ...],
) -> tuple[EvidenceRefV1, ...]:
    evidence: list[EvidenceRefV1] = []
    if business_summary:
        evidence.append(EvidenceRefV1(
            evidence_id=f"tushare-stock-company-{instrument_id.lower()}",
            source_kind="structured_provider",
            publisher="TuShare Pro",
            title="上市公司基本信息-主营业务",
            source_url=TUSHARE_STOCK_COMPANY_DOC,
            retrieved_at=generated_at,
            assertions=("公司主营描述可用",),
        ))
    if segments:
        latest = max(item.report_period for item in segments)
        evidence.append(EvidenceRefV1(
            evidence_id=f"tushare-fina-mainbz-{instrument_id.lower()}-{latest:%Y%m%d}",
            source_kind="structured_provider",
            publisher="TuShare Pro",
            title=f"主营业务构成-{latest.isoformat()}",
            source_url=TUSHARE_MAINBZ_DOC,
            published_at=latest,
            retrieved_at=generated_at,
            assertions=tuple(f"产品构成:{item.name}" for item in segments[:8]),
        ))
    return tuple(evidence)


def _business_segments(rows: Iterable[Mapping[str, Any]]) -> tuple[BusinessSegmentV1, ...]:
    prepared: list[tuple[date, str, float | None, float | None, float | None]] = []
    for row in rows:
        period = _date(row.get("end_date"))
        name = clean_segment_name(str(row.get("bz_item") or ""))
        if period is None or name is None:
            continue
        prepared.append((
            period,
            name,
            _float(row.get("bz_sales")),
            _float(row.get("bz_profit")),
            _float(row.get("bz_cost")),
        ))
    if not prepared:
        return ()
    latest = max(item[0] for item in prepared)
    latest_rows = [item for item in prepared if item[0] == latest]
    deduped: dict[str, tuple[date, str, float | None, float | None, float | None]] = {}
    for item in latest_rows:
        current = deduped.get(item[1])
        if current is None or (item[2] or 0.0) > (current[2] or 0.0):
            deduped[item[1]] = item
    ordered = sorted(deduped.values(), key=lambda item: (-(item[2] or 0.0), item[1]))
    revenue_total = sum(max(item[2] or 0.0, 0.0) for item in ordered)
    return tuple(BusinessSegmentV1(
        name=item[1],
        report_period=item[0],
        revenue_cny=item[2],
        profit_cny=item[3],
        cost_cny=item[4],
        revenue_share=(
            max(item[2], 0.0) / revenue_total
            if item[2] is not None and revenue_total > 0
            else None
        ),
        source="tushare:fina_mainbz_vip",
    ) for item in ordered[:12])


def _business_identity(
    summary: str | None,
    segments: tuple[BusinessSegmentV1, ...],
    official: Mapping[str, Any] | None,
) -> tuple[str | None, str | None, tuple[str, ...]]:
    if official is not None:
        key = _text(official.get("primary_business_key"))
        name = _text(official.get("primary_business_name"))
        tags = tuple(dict.fromkeys(
            str(item).strip() for item in official.get("business_tags", ()) if str(item).strip()
        ))
        return key, name, tags

    revenue_by_rule: defaultdict[tuple[str, str], float] = defaultdict(float)
    segment_supported_rule_keys: set[str] = set()
    segment_rule_matches: list[tuple[BusinessSegmentV1, tuple[Any, ...]]] = []
    ordered_rule_names: list[tuple[str, str]] = []
    for segment in segments:
        rules = matched_business_rules((segment.name,))
        segment_rule_matches.append((segment, rules))
        segment_supported_rule_keys.update(rule.key for rule in rules)
        for rule in rules:
            pair = (rule.key, rule.name)
            if pair not in ordered_rule_names:
                ordered_rule_names.append(pair)
        if rules:
            revenue_by_rule[(rules[0].key, rules[0].name)] += max(
                segment.revenue_cny or 0.0,
                0.0,
            )
    total_segment_revenue = sum(max(segment.revenue_cny or 0.0, 0.0) for segment in segments)
    weak_rollups = {
        pair
        for pair, revenue in revenue_by_rule.items()
        if pair[0] in _BROAD_ROLLUP_BUSINESS_KEYS
        and total_segment_revenue > 0
        and (
            revenue / total_segment_revenue < _MIN_BROAD_ROLLUP_REVENUE_SHARE
            or revenue < max(
                (
                    max(segment.revenue_cny or 0.0, 0.0)
                    for segment, rules in segment_rule_matches
                    if not any(
                        rule.key == pair[0]
                        for rule in rules
                    )
                ),
                default=0.0,
            )
        )
    }
    if weak_rollups:
        revenue_by_rule.clear()
        for segment, rules in segment_rule_matches:
            fallback = next(
                (
                    rule
                    for rule in rules
                    if (rule.key, rule.name) not in weak_rollups
                ),
                None,
            )
            if fallback is not None:
                revenue_by_rule[(fallback.key, fallback.name)] += max(
                    segment.revenue_cny or 0.0,
                    0.0,
                )
    for pair in weak_rollups:
        segment_supported_rule_keys.discard(pair[0])
    ordered_rule_names = [pair for pair in ordered_rule_names if pair not in weak_rollups]
    summary_rules = matched_business_rules((summary or "",))
    for rule in summary_rules:
        if (
            rule.key in _SEGMENT_REQUIRED_BUSINESS_KEYS
            and rule.key not in segment_supported_rule_keys
        ):
            continue
        pair = (rule.key, rule.name)
        if pair not in ordered_rule_names:
            ordered_rule_names.append(pair)

    primary_pair: tuple[str, str] | None = None
    if revenue_by_rule:
        primary_pair = sorted(
            revenue_by_rule,
            key=lambda pair: (-revenue_by_rule[pair], pair[1]),
        )[0]
        primary_revenue = revenue_by_rule[primary_pair]
        largest_competing_segment = max(
            (
                max(segment.revenue_cny or 0.0, 0.0)
                for segment, rules in segment_rule_matches
                if not any(rule.key == primary_pair[0] for rule in rules)
            ),
            default=0.0,
        )
        if total_segment_revenue > 0 and (
            primary_revenue / total_segment_revenue < _MIN_PRIMARY_RULE_REVENUE_SHARE
            or primary_revenue < largest_competing_segment
        ):
            leading_segment = segments[0]
            leading_rules = matched_business_rules((leading_segment.name,))
            primary_pair = (
                (leading_rules[0].key, leading_rules[0].name)
                if leading_rules
                else (
                    f"disclosed_{hashlib.sha1(leading_segment.name.encode('utf-8')).hexdigest()[:12]}",
                    leading_segment.name,
                )
            )
    elif ordered_rule_names:
        primary_pair = ordered_rule_names[0]
    elif segments:
        name = segments[0].name
        primary_pair = (f"disclosed_{hashlib.sha1(name.encode('utf-8')).hexdigest()[:12]}", name)

    tags: list[str] = []
    if primary_pair:
        tags.append(primary_pair[1])
    tags.extend(pair[1] for pair in ordered_rule_names)
    tags.extend(segment.name for segment in segments[:6])
    unique_tags = tuple(dict.fromkeys(item for item in tags if item))[:12]
    if primary_pair is None:
        return None, None, unique_tags
    return primary_pair[0], primary_pair[1], unique_tags


def _directory_category(
    business_key: str | None,
    business_name: str | None,
    official: Mapping[str, Any] | None,
    evidence_texts: Iterable[str],
) -> tuple[str | None, str | None]:
    """Choose the reviewed category shown in the market directory.

    The category may be a broad domain (for example ``芯片``) or a material,
    evidence-backed business theme (for example ``创新药``).  The exact
    primary-business leaf remains separate and is never overwritten.
    """

    if official is not None:
        key = _text(official.get("directory_category_key"))
        name = _text(official.get("directory_category_name"))
        if bool(key) != bool(name):
            raise ValueError("official directory category key and name must appear together")
        if key and name:
            return key, name
    learned = reviewed_directory_category(business_key, evidence_texts)
    if learned is not None:
        return learned
    return business_key, business_name


def _business_support(
    summary: str | None,
    segments: tuple[BusinessSegmentV1, ...],
    business_key: str | None,
    business_name: str | None,
) -> tuple[bool, bool]:
    """Require semantic agreement, not merely two non-empty provider responses."""

    if business_key is None or business_name is None:
        return False, False
    summary_keys = {item.key for item in matched_business_rules((summary or "",))}
    segment_keys = {
        item.key
        for segment in segments
        for item in matched_business_rules((segment.name,))
    }
    if business_key.startswith("disclosed_"):
        normalized_summary = "".join((summary or "").split())
        normalized_name = "".join(business_name.split())
        return (
            bool(normalized_name and normalized_name in normalized_summary),
            any(segment.name == business_name for segment in segments),
        )
    return business_key in summary_keys, business_key in segment_keys


def build_stock_relationship_catalog(
    source: Mapping[str, Any],
    *,
    generated_at: datetime,
    official_evidence_path: str | Path | None = None,
) -> tuple[StockRelationshipCatalogStatusV1, tuple[StockRelationshipProfileV1, ...]]:
    """Build a full-market catalog from one validated source bundle."""

    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must include a timezone")
    if source.get("contract") != "instrument_taxonomy_source_bundle.v1":
        raise ValueError("instrument taxonomy source contract is invalid")
    as_of = date.fromisoformat(str(source.get("as_of")))
    stocks = source.get("stocks")
    companies = source.get("companies")
    sw_memberships = source.get("sw_memberships")
    segment_rows = source.get("business_segments")
    if not all(isinstance(value, (list, tuple)) for value in (stocks, companies, sw_memberships, segment_rows)):
        raise ValueError("instrument taxonomy source bundle contains invalid row collections")

    companies_by_id = {
        str(item.get("ts_code") or "").strip(): item
        for item in companies
        if isinstance(item, Mapping) and _text(item.get("ts_code"))
    }
    sw_by_id = {
        str(item.get("ts_code") or "").strip(): item
        for item in sw_memberships
        if isinstance(item, Mapping) and _text(item.get("ts_code"))
    }
    segments_by_id: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in segment_rows:
        if isinstance(item, Mapping) and (instrument_id := _text(item.get("ts_code"))):
            segments_by_id[instrument_id].append(item)
    official_by_id = load_official_evidence(official_evidence_path)

    profiles: list[StockRelationshipProfileV1] = []
    for stock in stocks:
        if not isinstance(stock, Mapping):
            raise ValueError("instrument taxonomy stock master contains a non-object row")
        instrument_id = _text(stock.get("ts_code"))
        name = _text(stock.get("name"))
        if instrument_id is None or name is None:
            raise ValueError("instrument taxonomy stock master contains an incomplete identity")
        company = companies_by_id.get(instrument_id, {})
        summary = _text(company.get("main_business")) or _text(company.get("introduction"))
        segments = _business_segments(segments_by_id.get(instrument_id, ()))
        official = official_by_id.get(instrument_id)
        business_key, business_name, business_tags = _business_identity(summary, segments, official)
        business_domain = business_domain_by_business_key(business_key)
        directory_key, directory_name = _directory_category(
            business_key,
            business_name,
            official,
            (
                summary or "",
                business_name or "",
                *(segment.name for segment in segments),
                *business_tags,
            ),
        )
        summary_support, segment_support = _business_support(
            summary,
            segments,
            business_key,
            business_name,
        )

        sw = sw_by_id.get(instrument_id)
        statistical = None
        if sw is not None and _text(sw.get("l3_name")):
            statistical = IndustryPathV1(
                taxonomy="sw",
                taxonomy_version="sw-2021-current",
                level1_code=_text(sw.get("l1_code")),
                level1_name=_text(sw.get("l1_name")),
                level2_code=_text(sw.get("l2_code")),
                level2_name=_text(sw.get("l2_name")),
                level3_code=_text(sw.get("l3_code")),
                level3_name=_text(sw.get("l3_name")),
                effective_from=_date(sw.get("in_date")),
                effective_to=_date(sw.get("out_date")),
                source="tushare:index_member_all",
            )

        structured_evidence = _structured_evidence(
            instrument_id,
            generated_at=generated_at,
            business_summary=summary,
            segments=segments,
        )
        official_refs = tuple(official.get("evidence", ())) if official else ()
        if official_refs and business_name:
            verification_status = "verified"
        elif summary_support and segment_support:
            verification_status = "corroborated"
        elif business_name:
            verification_status = "provider_only"
        else:
            verification_status = "unresolved"

        flags: list[str] = ["regulatory_industry_unavailable"]
        if statistical is None:
            flags.append("statistical_industry_unavailable")
        if business_name is None:
            flags.append("primary_business_unresolved")
        if official_refs:
            flags.append("official_web_evidence_applied")
        elif business_name and verification_status != "corroborated":
            flags.append("business_cross_source_unconfirmed")
        if instrument_id == "002916.SZ":
            flags.append("abf_unverified")

        profiles.append(StockRelationshipProfileV1(
            instrument_id=instrument_id,
            name=name,
            as_of=as_of,
            regulatory_industry=None,
            statistical_industry=statistical,
            provider_industry=_text(stock.get("industry")),
            business_domain_key=(business_domain.key if business_domain else None),
            business_domain_name=(business_domain.name if business_domain else None),
            directory_category_key=directory_key,
            directory_category_name=directory_name,
            primary_business_key=business_key,
            primary_business_name=business_name,
            business_tags=business_tags,
            business_summary=summary,
            business_segments=segments,
            verification_status=verification_status,
            evidence=tuple((*official_refs, *structured_evidence)),
            flags=tuple(dict.fromkeys(flags)),
        ))

    profiles.sort(key=lambda item: item.instrument_id)
    if len(profiles) != len({item.instrument_id for item in profiles}):
        raise ValueError("instrument taxonomy catalog contains duplicate instruments")
    serialised = [item.model_dump(mode="json") for item in profiles]
    revision = _sha256({"as_of": as_of.isoformat(), "profiles": serialised})
    counts = defaultdict(int)
    for item in profiles:
        counts[item.verification_status] += 1
    status = StockRelationshipCatalogStatusV1(
        catalog_revision=revision,
        as_of=as_of,
        generated_at=generated_at,
        profile_total=len(profiles),
        verified_total=counts["verified"],
        corroborated_total=counts["corroborated"],
        provider_only_total=counts["provider_only"] + counts["disputed"] + counts["stale"],
        unresolved_total=counts["unresolved"],
        source_providers=tuple(dict.fromkeys(str(item) for item in source.get("source_providers", ()) if str(item))),
        source_request_ids=tuple(dict.fromkeys(str(item) for item in source.get("source_request_ids", ()) if str(item))),
        flags=tuple(dict.fromkeys(str(item) for item in source.get("flags", ()) if str(item))),
    )
    return status, tuple(profiles)


def apply_market_industries_to_catalog(status, profiles, source, *, generated_at):
    """Attach the current market block without replacing statistical/business evidence."""
    from tradex.data_gateway.instrument_taxonomy import MARKET_INDUSTRY_CHECKED, validate_market_industries

    validate_market_industries(source, status.as_of)
    if {item.instrument_id for item in profiles} != {row["ts_code"] for row in source["stocks"]}:
        raise RuntimeError("market industry universe differs from the accepted catalog")
    by_id = {row["instrument_id"]: row for row in source["memberships"]}
    updated = []
    for profile in profiles:
        row = by_id.get(profile.instrument_id)
        if row is None and profile.market_industry is not None:
            raise RuntimeError("market industry coverage regressed; preserving prior catalog")
        path = IndustryPathV1(
            taxonomy="ths", taxonomy_version="ths-market-industry-current",
            level1_code=row["code"], level1_name=row["name"], source="tushare:ths_member",
        ) if row else None
        updated.append(profile.model_copy(update={"market_industry": path}))
    revision = _sha256({"as_of": status.as_of.isoformat(),
                        "profiles": [item.model_dump(mode="json") for item in updated]})
    flags = [flag for flag in status.flags if not flag.startswith((
        "market_industry_source_missing:", "market_industry_source_conflict:",
    ))]
    updated_status = status.model_copy(update={
        "catalog_revision": revision, "generated_at": generated_at,
        "source_providers": tuple(dict.fromkeys((*status.source_providers, *source.get("source_providers", ())))),
        "source_request_ids": tuple(dict.fromkeys((*status.source_request_ids, *source.get("source_request_ids", ())))),
        "flags": tuple(dict.fromkeys((*flags, MARKET_INDUSTRY_CHECKED,
                    *(f"market_industry_source_missing:{key}" for key in source["missing_instruments"]),
                    *(f"market_industry_source_conflict:{key}" for key in source.get("conflicting_instruments", ()))))),
    })
    return updated_status, tuple(updated)


def apply_official_evidence_to_catalog(
    status: StockRelationshipCatalogStatusV1,
    profiles: Iterable[StockRelationshipProfileV1],
    *,
    generated_at: datetime,
    official_evidence_path: str | Path | None = None,
) -> tuple[StockRelationshipCatalogStatusV1, tuple[StockRelationshipProfileV1, ...]]:
    """Overlay reviewed official evidence without reconstructing provider rows.

    This path preserves the accepted provider snapshot byte-for-byte for stocks
    without reviewed evidence changes.  It is intentionally narrower than a
    provider refresh and remains safe when the provider is temporarily
    unavailable.
    """

    canonical_status = StockRelationshipCatalogStatusV1.model_validate(status)
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must include a timezone")
    official_by_id = load_official_evidence(official_evidence_path)
    refreshed: list[StockRelationshipProfileV1] = []
    for raw_profile in profiles:
        profile = StockRelationshipProfileV1.model_validate(raw_profile)
        official = official_by_id.get(profile.instrument_id)
        if official is None:
            refreshed.append(profile)
            continue
        business_key, business_name, business_tags = _business_identity(
            profile.business_summary,
            profile.business_segments,
            official,
        )
        business_domain = business_domain_by_business_key(business_key)
        directory_key, directory_name = _directory_category(
            business_key,
            business_name,
            official,
            (
                profile.business_summary or "",
                business_name or "",
                *(segment.name for segment in profile.business_segments),
                *business_tags,
            ),
        )
        official_refs = tuple(official.get("evidence", ()))
        retained_evidence = tuple(
            item
            for item in profile.evidence
            if item.source_kind
            not in {"official_filing", "official_company", "web_secondary"}
        )
        flags = [
            item
            for item in profile.flags
            if item
            not in {
                "business_cross_source_unconfirmed",
                "primary_business_unresolved",
            }
        ]
        if business_name is None:
            flags.append("primary_business_unresolved")
        if official_refs and "official_web_evidence_applied" not in flags:
            flags.append("official_web_evidence_applied")
        refreshed.append(StockRelationshipProfileV1.model_validate({
            **profile.model_dump(mode="json"),
            "business_domain_key": business_domain.key if business_domain else None,
            "business_domain_name": business_domain.name if business_domain else None,
            "directory_category_key": directory_key,
            "directory_category_name": directory_name,
            "primary_business_key": business_key,
            "primary_business_name": business_name,
            "business_tags": business_tags,
            "verification_status": "verified" if official_refs and business_name else profile.verification_status,
            "evidence": (*official_refs, *retained_evidence),
            "flags": tuple(dict.fromkeys(flags)),
        }))

    refreshed.sort(key=lambda item: item.instrument_id)
    if len(refreshed) != canonical_status.profile_total:
        raise ValueError("accepted catalog profile count changed during evidence overlay")
    serialised = [item.model_dump(mode="json") for item in refreshed]
    revision = _sha256({
        "as_of": canonical_status.as_of.isoformat(),
        "profiles": serialised,
    })
    counts = defaultdict(int)
    for item in refreshed:
        counts[item.verification_status] += 1
    updated_status = StockRelationshipCatalogStatusV1(
        catalog_revision=revision,
        as_of=canonical_status.as_of,
        generated_at=generated_at,
        profile_total=len(refreshed),
        verified_total=counts["verified"],
        corroborated_total=counts["corroborated"],
        provider_only_total=(
            counts["provider_only"] + counts["disputed"] + counts["stale"]
        ),
        unresolved_total=counts["unresolved"],
        source_providers=canonical_status.source_providers,
        source_request_ids=canonical_status.source_request_ids,
        flags=tuple(dict.fromkeys((
            *canonical_status.flags,
            "official_evidence_refresh_from_accepted_catalog",
        ))),
    )
    return updated_status, tuple(refreshed)


__all__ = [
    "apply_official_evidence_to_catalog",
    "build_stock_relationship_catalog",
    "load_official_evidence",
]
