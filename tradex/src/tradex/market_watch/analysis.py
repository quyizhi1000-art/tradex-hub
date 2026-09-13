"""Pure analysis for ``market_watch.v1``.

No function in this module performs I/O or knows which provider supplied a
value.  Inputs are normalized dictionaries and outputs are strict canonical
models from :mod:`tradex.market_watch.contracts`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from tradex.market_calendar import a_share_session

from .contracts import (
    AlertV1,
    BreadthSnapshotV1,
    ChangeSummaryV1,
    ComponentQuality,
    ConclusionStrength,
    EvidenceStrength,
    FreshnessComponentV1,
    FreshnessStatus,
    FreshnessV1,
    GuardrailSeverity,
    GuardrailV1,
    IndexRole,
    IndexSnapshotV1,
    MarketPhase,
    MarketRegime,
    MarketStateV1,
    MarketWatchSnapshotV1,
    RotationDirection,
    RotationSnapshotV1,
    ScenarioV1,
    SectorFlowTrajectoryStatus,
    SectorFlowTrajectoryV1,
    SectorRotationV1,
    SectorTag,
    TurnoverDirection,
    TurnoverSnapshotV1,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_TURNOVER_FLAT_THRESHOLD_PCT = 3.0
SECTOR_MOVE_ALERT_THRESHOLDS_PCT = (1.2, 0.8, 0.5, 0.3)
MAX_SECTOR_MOVE_ALERTS = 12
_INTRADAY_MAX_AGE_SECONDS = {
    "indices": 120,
    "breadth": 120,
    "turnover": 180,
    "rotation": 120,
}

_ROLE_ORDER = (
    IndexRole.BROAD_MARKET,
    IndexRole.LARGE_CAP,
    IndexRole.SMALL_CAP,
    IndexRole.GROWTH,
)
_ROLE_DEFAULTS = {
    IndexRole.BROAD_MARKET: ("000001.SH", "上证指数"),
    IndexRole.LARGE_CAP: ("000300.SH", "沪深300"),
    IndexRole.SMALL_CAP: ("000852.SH", "中证1000"),
    IndexRole.GROWTH: ("399006.SZ", "创业板指"),
}
_INSTRUMENT_ROLES = {
    "000001.SH": IndexRole.BROAD_MARKET,
    "000300.SH": IndexRole.LARGE_CAP,
    "000852.SH": IndexRole.SMALL_CAP,
    "399852.SZ": IndexRole.SMALL_CAP,
    "399006.SZ": IndexRole.GROWTH,
}
_NAME_ROLES = {
    "上证指数": IndexRole.BROAD_MARKET,
    "上证综合指数": IndexRole.BROAD_MARKET,
    "沪深300": IndexRole.LARGE_CAP,
    "中证1000": IndexRole.SMALL_CAP,
    "创业板": IndexRole.GROWTH,
    "创业板指": IndexRole.GROWTH,
}
_LEGACY_INSTRUMENTS = {
    "sh000001": "000001.SH",
    "sh000300": "000300.SH",
    "sh000852": "000852.SH",
    "sz399852": "399852.SZ",
    "sz399006": "399006.SZ",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value.endswith("%"):
            value = value[:-1]
        if value in {"", "-", "--", "null", "None"}:
            return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _datetime(value: Any, *, assume_shanghai: bool = True) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            return None
    if result.tzinfo is None and assume_shanghai:
        result = result.replace(tzinfo=SHANGHAI)
    return result


def _resolve_as_of(value: datetime | str | None, market_data: Mapping[str, Any]) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must include a timezone")
        return value
    parsed = _datetime(value, assume_shanghai=False) if value is not None else None
    if parsed is not None:
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("as_of must include a timezone")
        return parsed
    raw = _first(market_data, "timestamp", "as_of", "fetched_at")
    parsed = _datetime(raw)
    return parsed or datetime.now(SHANGHAI)


def _canonical_instrument(value: Any) -> str | None:
    if value is None:
        return None
    code = str(value).strip()
    if not code:
        return None
    lowered = code.lower()
    if lowered in _LEGACY_INSTRUMENTS:
        return _LEGACY_INSTRUMENTS[lowered]
    upper = code.upper()
    if upper in _INSTRUMENT_ROLES:
        return upper
    if code.isdigit() and len(code) == 6:
        if code in {"000001", "000300", "000852"}:
            return f"{code}.SH"
        if code in {"399852", "399006"}:
            return f"{code}.SZ"
    return None


def _component_quality(value: Any, *, available: bool) -> ComponentQuality:
    text = str(value or "").lower()
    if text in {item.value for item in ComponentQuality}:
        return ComponentQuality(text)
    return ComponentQuality.ACCEPTED if available else ComponentQuality.UNAVAILABLE


def _freshness_status(value: Any, *, default: FreshnessStatus) -> FreshnessStatus:
    text = str(value or "").lower()
    return FreshnessStatus(text) if text in {item.value for item in FreshnessStatus} else default


def _overall_freshness_status(
    components: Sequence[FreshnessComponentV1],
) -> FreshnessStatus:
    statuses = {item.status for item in components}
    return (
        FreshnessStatus.STALE
        if FreshnessStatus.STALE in statuses
        else FreshnessStatus.UNAVAILABLE
        if statuses == {FreshnessStatus.UNAVAILABLE}
        else FreshnessStatus.DEGRADED
        if statuses - {FreshnessStatus.FRESH}
        else FreshnessStatus.FRESH
    )


def _infer_phase(as_of: datetime, claimed_open: bool, raw_phase: Any) -> MarketPhase:
    # Provider phase labels are advisory.  The verified exchange calendar owns
    # the session boundary so a paid/free source cannot turn a holiday into an
    # open market.  A provider claiming closed during a scheduled open session
    # remains unknown because the conflict may represent a market-wide halt.
    del raw_phase
    scheduled = a_share_session(as_of)
    phase = MarketPhase(scheduled.phase.value)
    if scheduled.is_open and not claimed_open:
        return MarketPhase.UNKNOWN
    return phase


def _normalize_indices(
    market_data: Mapping[str, Any], as_of: datetime
) -> tuple[IndexSnapshotV1, ...]:
    raw_records = [
        *_sequence(market_data.get("indices")),
        *_sequence(market_data.get("participation_indices")),
    ]
    by_role: dict[IndexRole, IndexSnapshotV1] = {}
    top_provider_time = _datetime(market_data.get("provider_as_of"))
    for raw_value in raw_records:
        raw = _mapping(raw_value)
        instrument_id = _canonical_instrument(
            _first(raw, "instrument_id", "code", "代码")
        )
        name = str(_first(raw, "name", "名称") or "").strip()
        role_value = raw.get("role")
        try:
            role = IndexRole(str(role_value)) if role_value is not None else None
        except ValueError:
            role = None
        role = role or _INSTRUMENT_ROLES.get(instrument_id or "") or _NAME_ROLES.get(name)
        if role is None:
            continue
        default_instrument, default_name = _ROLE_DEFAULTS[role]
        instrument_id = instrument_id or default_instrument
        level = _number(_first(raw, "level", "price", "value", "最新点位", "最新价"))
        change_pct = _number(_first(raw, "change_pct", "涨跌幅"))
        available = bool(raw.get("available", level is not None or change_pct is not None))
        if not available or (level is None and change_pct is None):
            available = False
            level = None
            change_pct = None
        partial = available and (level is None or change_pct is None)
        raw_flags = [str(item) for item in _sequence(raw.get("quality_flags"))]
        if partial:
            raw_flags.append("index_quote_partial")
        candidate = IndexSnapshotV1(
            role=role,
            instrument_id=instrument_id,
            name=name or default_name,
            available=available,
            level=level,
            change_pct=change_pct,
            provider_as_of=_datetime(_first(raw, "provider_as_of", "更新时间")) or top_provider_time,
            quality=(
                ComponentQuality.UNAVAILABLE
                if not available
                else ComponentQuality.DEGRADED
                if partial
                else _component_quality(
                    _first(raw, "quality"),
                    available=available,
                )
            ),
            quality_flags=tuple(dict.fromkeys(raw_flags)),
        )
        existing = by_role.get(role)
        candidate_score = (
            int(candidate.available),
            int(candidate.level is not None) + int(candidate.change_pct is not None),
        )
        existing_score = (
            int(existing.available),
            int(existing.level is not None) + int(existing.change_pct is not None),
        ) if existing is not None else (-1, -1)
        if candidate_score > existing_score:
            by_role[role] = candidate
    result: list[IndexSnapshotV1] = []
    for role in _ROLE_ORDER:
        if role in by_role:
            result.append(by_role[role])
            continue
        instrument_id, name = _ROLE_DEFAULTS[role]
        result.append(IndexSnapshotV1(
            role=role,
            instrument_id=instrument_id,
            name=name,
            available=False,
            quality=ComponentQuality.UNAVAILABLE,
            quality_flags=("index_missing",),
        ))
    return tuple(result)


def _normalize_breadth(risk_data: Mapping[str, Any]) -> BreadthSnapshotV1:
    raw = _mapping(risk_data.get("breadth"))
    if not raw:
        raw = _mapping(
            _mapping(_mapping(risk_data.get("market_participation")).get("metrics")).get(
                "market_breadth"
            )
        )
    up = _integer(_first(raw, "up_count", "上涨家数", "上涨"))
    down = _integer(_first(raw, "down_count", "下跌家数", "下跌"))
    if up is None or down is None:
        return BreadthSnapshotV1(
            available=False,
            quality=ComponentQuality.UNAVAILABLE,
            reason=str(raw.get("reason") or "market_breadth_unavailable"),
            quality_flags=("breadth_missing",),
        )
    flat = _integer(_first(raw, "flat_count", "平盘家数", "平盘")) or 0
    unclassified = _integer(_first(raw, "unclassified_count", "未分类")) or 0
    total = _integer(raw.get("total_count")) or up + down + flat + unclassified
    directional = up + down
    ratio = up / directional if directional else None
    return BreadthSnapshotV1(
        available=True,
        up_count=up,
        down_count=down,
        flat_count=flat,
        unclassified_count=unclassified,
        total_count=total,
        advance_ratio=ratio,
        provider_as_of=_datetime(_first(raw, "provider_as_of", "更新时间")),
        quality=_component_quality(raw.get("quality"), available=True),
        quality_flags=tuple(str(item) for item in _sequence(raw.get("quality_flags"))),
    )


def classify_turnover(
    *,
    today_amount_cny: float | None,
    previous_same_time_amount_cny: float | None,
    today_date: date | str | None,
    previous_date: date | str | None,
    as_of: str | None,
    flat_threshold_pct: float = DEFAULT_TURNOVER_FLAT_THRESHOLD_PCT,
    unavailable_reason: str = "same_time_turnover_unavailable",
) -> TurnoverSnapshotV1:
    """Classify same-time turnover with a symmetric no-change noise band."""

    threshold = float(flat_threshold_pct) / 100.0
    if not 0 <= threshold <= 1:
        raise ValueError("flat_threshold_pct must be between 0 and 100")
    today = _number(today_amount_cny)
    previous = _number(previous_same_time_amount_cny)
    current_date = _date(today_date)
    prior_date = _date(previous_date)
    if (
        today is None
        or today < 0
        or previous is None
        or previous <= 0
        or current_date is None
        or prior_date is None
        or as_of is None
    ):
        return TurnoverSnapshotV1(
            available=False,
            neutral_band_ratio=threshold,
            reason=unavailable_reason,
        )
    difference = today - previous
    ratio = difference / previous
    direction = (
        TurnoverDirection.EXPAND
        if ratio > threshold
        else TurnoverDirection.SHRINK
        if ratio < -threshold
        else TurnoverDirection.FLAT
    )
    return TurnoverSnapshotV1(
        available=True,
        today_date=current_date,
        previous_date=prior_date,
        as_of=str(as_of),
        today_amount_cny=today,
        previous_same_time_amount_cny=previous,
        difference_cny=difference,
        difference_ratio=ratio,
        neutral_band_ratio=threshold,
        direction=direction,
    )


def _normalize_turnover(
    market_data: Mapping[str, Any], *, flat_threshold_pct: float
) -> TurnoverSnapshotV1:
    raw = _mapping(market_data.get("turnover")) or _mapping(
        market_data.get("market_turnover")
    )
    if raw.get("available") is False:
        return classify_turnover(
            today_amount_cny=None,
            previous_same_time_amount_cny=None,
            today_date=None,
            previous_date=None,
            as_of=None,
            flat_threshold_pct=flat_threshold_pct,
            unavailable_reason=str(raw.get("reason") or "same_time_turnover_unavailable"),
        )
    return classify_turnover(
        today_amount_cny=_first(raw, "today_amount_cny", "today_amount"),
        previous_same_time_amount_cny=_first(
            raw,
            "previous_same_time_amount_cny",
            "previous_same_time_amount",
        ),
        today_date=raw.get("today_date"),
        previous_date=raw.get("previous_date"),
        as_of=raw.get("as_of"),
        flat_threshold_pct=flat_threshold_pct,
        unavailable_reason=str(raw.get("reason") or "same_time_turnover_unavailable"),
    )


def _sector_tags(raw: Mapping[str, Any]) -> tuple[SectorTag, ...]:
    values = _sequence(raw.get("tags"))
    tags: list[SectorTag] = []
    for value in values:
        try:
            tag = SectorTag(str(value))
        except ValueError:
            continue
        if tag not in tags:
            tags.append(tag)
    if tags:
        return tuple(tags)
    # Existing provider-neutral risk views expose stable keys alongside display
    # labels.  Combine all canonical identity hints so a key such as
    # ``securities`` does not hide its Chinese label ``证券``.
    combined = " ".join(
        str(raw.get(field) or "").strip().lower()
        for field in (
            "sector_key",
            "key",
            "id",
            "name",
            "label",
            "role",
            "family",
        )
    )
    inferred: list[SectorTag] = []
    if any(word in combined for word in (
        "证券", "券商", "互联网金融", "securities", "internet_finance",
        "financial_core", "technology", "growth",
    )):
        inferred.append(SectorTag.ATTACK)
    if any(word in combined for word in (
        "电力", "白酒", "零售", "银行", "electric_power", "baijiu",
        "retail", "bank", "cashflow_defense", "consumer_stability",
        "defense", "防御",
    )):
        inferred.append(SectorTag.DEFENSE)
    if any(word in combined for word in (
        "黄金", "贵金属", "precious_metals", "safe_haven", "避险",
    )):
        inferred.extend((SectorTag.SAFE_HAVEN, SectorTag.EVENT))
    if any(word in combined for word in (
        "油", "农业", "oil_gas", "agriculture", "event", "事件",
    )):
        inferred.append(SectorTag.EVENT)
    if any(word in combined for word in (
        "有色", "稀土", "nonferrous", "rare_earth", "cyclical", "周期",
    )):
        inferred.append(SectorTag.CYCLICAL)
    if any(word in combined for word in ("银行", "bank", "weight", "权重")):
        inferred.append(SectorTag.WEIGHT_SUPPORT)
    return tuple(dict.fromkeys(inferred)) or (SectorTag.MIXED,)


def _sector_metrics(raw: Mapping[str, Any]) -> dict[str, Any]:
    price = _mapping(raw.get("price"))
    breadth = _mapping(raw.get("breadth"))
    funds = _mapping(raw.get("funds"))
    metrics = _mapping(raw.get("metrics"))
    return {
        "sector_key": str(_first(raw, "sector_key", "key", "id") or "").strip(),
        "name": str(_first(raw, "name", "label") or "").strip(),
        "tags": _sector_tags(raw),
        "change_pct": _number(
            _first(raw, "change_pct")
            if raw.get("change_pct") is not None
            else _first(price, "change_pct")
            if price
            else _first(metrics, "change_pct")
        ),
        "breadth_ratio": _number(
            _first(raw, "breadth_ratio")
            if raw.get("breadth_ratio") is not None
            else _first(breadth, "ratio")
            if breadth
            else _first(metrics, "breadth_ratio")
        ),
        "main_net_inflow_cny": _number(
            _first(raw, "main_net_inflow_cny", "flow_amount")
            if _first(raw, "main_net_inflow_cny", "flow_amount") is not None
            else _first(funds, "current_amount")
            if funds
            else _first(metrics, "flow_amount")
        ),
        "main_net_inflow_ratio": _number(
            _first(raw, "main_net_inflow_ratio", "flow_ratio")
            if _first(raw, "main_net_inflow_ratio", "flow_ratio") is not None
            else _first(funds, "current_ratio")
            if funds
            else _first(metrics, "flow_ratio")
        ),
        "provider_as_of": _datetime(
            _first(raw, "provider_as_of") or _first(funds, "as_of")
        ),
    }


def _classify_sector(raw: Mapping[str, Any]) -> SectorRotationV1 | None:
    values = _sector_metrics(raw)
    if not values["sector_key"] or not values["name"]:
        return None
    change = values["change_pct"]
    breadth = values["breadth_ratio"]
    flow = values["main_net_inflow_cny"]
    supporting: list[str] = []
    counter: list[str] = []
    direction = RotationDirection.UNKNOWN
    strength = EvidenceStrength.UNKNOWN
    if change is not None and breadth is not None:
        if change >= 0.8 and breadth >= 0.60:
            direction = RotationDirection.STRENGTHENING
            strength = EvidenceStrength.STRONG
        elif change <= -0.8 and breadth <= 0.40:
            direction = RotationDirection.WEAKENING
            strength = EvidenceStrength.STRONG
        elif change >= 0.2 and breadth >= 0.52:
            direction = RotationDirection.STRENGTHENING
            strength = EvidenceStrength.MODERATE
        elif change <= -0.2 and breadth <= 0.48:
            direction = RotationDirection.WEAKENING
            strength = EvidenceStrength.MODERATE
        else:
            direction = RotationDirection.STABLE
            strength = EvidenceStrength.WEAK
        if change > 0:
            supporting.append(f"板块价格上涨{change:.2f}个百分点")
        elif change < 0:
            counter.append(f"板块价格下跌{abs(change):.2f}个百分点")
        if breadth >= 0.5:
            supporting.append(f"内部上涨占比{breadth:.0%}")
        else:
            counter.append(f"内部上涨占比仅{breadth:.0%}")
    elif change is not None or breadth is not None:
        direction = RotationDirection.UNKNOWN
        strength = EvidenceStrength.WEAK
        counter.append("价格或内部广度缺一，方向仅供观察")
    if flow is not None:
        if (direction == RotationDirection.STRENGTHENING and flow > 0) or (
            direction == RotationDirection.WEAKENING and flow < 0
        ):
            supporting.append("资金流方向与价格和广度一致（辅助证据）")
        elif flow != 0:
            counter.append("资金流与价格或广度不一致（辅助证据）")
        else:
            counter.append("资金流未形成方向（辅助证据）")
        if strength == EvidenceStrength.UNKNOWN:
            strength = EvidenceStrength.WEAK
            counter.append("仅有资金流，不能形成强结论")
    return SectorRotationV1(
        **values,
        direction=direction,
        evidence_strength=strength,
        supporting_evidence=tuple(supporting),
        counter_evidence=tuple(counter),
    )


def classify_rotation(sectors: Sequence[Mapping[str, Any]]) -> RotationSnapshotV1:
    """Classify multi-label sector leadership; fund flow never upgrades strength."""

    # Legacy dashboard groups can briefly repeat a sector while it moves from
    # one lifecycle list to another.  Preserve the first (highest-priority)
    # occurrence so the canonical snapshot remains unique and one duplicate
    # presentation row cannot invalidate the entire market-watch refresh.
    unique: dict[str, SectorRotationV1] = {}
    for raw in sectors:
        item = _classify_sector(_mapping(raw))
        if item is not None and item.sector_key not in unique:
            unique[item.sector_key] = item
    classified = tuple(unique.values())
    leaders = [
        item
        for item in classified
        if item.direction == RotationDirection.STRENGTHENING
        and item.evidence_strength in {EvidenceStrength.STRONG, EvidenceStrength.MODERATE}
    ]
    attack_score = sum(
        2 if item.evidence_strength == EvidenceStrength.STRONG else 1
        for item in leaders
        if SectorTag.ATTACK in item.tags
    )
    defense_score = sum(
        2 if item.evidence_strength == EvidenceStrength.STRONG else 1
        for item in leaders
        if SectorTag.DEFENSE in item.tags or SectorTag.SAFE_HAVEN in item.tags
    )
    leading_tags = tuple(dict.fromkeys(tag for item in leaders for tag in item.tags))
    if attack_score and defense_score:
        regime = MarketRegime.MIXED
        summary = "进攻与防御标签同时增强，盘面处于多线并存。"
    elif attack_score:
        regime = MarketRegime.ATTACK
        summary = "价格与内部广度共同指向进攻类板块增强。"
    elif defense_score:
        regime = MarketRegime.DEFENSE
        summary = "价格与内部广度共同指向防御或避险方向增强。"
    elif leaders:
        regime = MarketRegime.MIXED
        summary = "周期、事件或权重方向增强，暂不强行归入进攻或防御。"
    else:
        regime = MarketRegime.UNCERTAIN
        summary = "尚无板块同时获得价格与内部广度确认。"
    return RotationSnapshotV1(
        regime=regime,
        sectors=classified,
        leading_tags=leading_tags,
        summary=summary,
    )


def _rotation_inputs(risk_data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Share direction membership with the canonical trajectory catalogs."""
    directions: dict[str, set[str]] = {}
    for field, tag in (("sector_flow_trajectory", "defense"),
                       ("offense_sector_flow_trajectory", "attack")):
        for item in _sequence(_mapping(risk_data.get(field)).get("sectors")):
            item = _mapping(item)
            for identity in (item.get("sector_key"), item.get("name")):
                if identity:
                    directions.setdefault(str(identity), set()).add(tag)
    rows = []
    for raw in _raw_rotation_inputs(risk_data):
        if _sequence(raw.get("tags")):
            rows.append(raw)
            continue
        tags = set()
        for field in ("sector_key", "key", "id", "name", "label"):
            tags.update(directions.get(str(raw.get(field) or ""), ()))
        if tags:
            tags.update(tag.value for tag in _sector_tags(raw)
                        if tag not in {SectorTag.MIXED, SectorTag.ATTACK, SectorTag.DEFENSE})
        rows.append({**raw, "tags": sorted(tags)} if tags else raw)
    return rows


def _raw_rotation_inputs(risk_data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    direct = _mapping(risk_data.get("rotation"))
    direct_sectors = _sequence(direct.get("sectors"))
    if direct_sectors:
        return [_mapping(item) for item in direct_sectors]
    if _sequence(risk_data.get("sectors")):
        return [_mapping(item) for item in _sequence(risk_data.get("sectors"))]
    offense_lists = _mapping(_mapping(risk_data.get("offense")).get("lists"))
    values: list[Mapping[str, Any]] = []
    for key in ("attacking", "rotating", "cooling", "unclassified"):
        values.extend(_mapping(item) for item in _sequence(offense_lists.get(key)))
    if values:
        return values
    for group in _sequence(risk_data.get("groups")):
        values.extend(_mapping(item) for item in _sequence(_mapping(group).get("items")))
    return values


def _normalize_sector_flow_trajectory(
    risk_data: Mapping[str, Any],
    *,
    field: str = "sector_flow_trajectory",
    direction: str = "defense",
) -> SectorFlowTrajectoryV1 | None:
    """Validate the optional trajectory without letting it alter macro conclusions."""

    raw = _mapping(risk_data.get(field))
    if not raw and direction == "defense":
        raw = _mapping(_mapping(risk_data.get("offense")).get("sector_flow_trajectory"))
    elif not raw and direction == "offense":
        raw = _mapping(
            _mapping(risk_data.get("offense")).get("offense_sector_flow_trajectory")
        )
    if not raw:
        return None
    try:
        result = SectorFlowTrajectoryV1.model_validate(raw)
        if result.direction != direction:
            raise ValueError("sector flow trajectory direction mismatch")
        return result
    except (TypeError, ValueError):
        # The trajectory is an auxiliary observation surface.  Malformed data
        # fails closed inside that surface rather than taking down the existing
        # indices/breadth/turnover guardrail.
        return SectorFlowTrajectoryV1(
            direction=direction,
            status=SectorFlowTrajectoryStatus.UNAVAILABLE,
            market_phase=MarketPhase.UNKNOWN,
            reason="invalid_sector_flow_trajectory",
            flags=("invalid_sector_flow_trajectory",),
        )


def _explicit_freshness(
    market_data: Mapping[str, Any], risk_data: Mapping[str, Any]
) -> FreshnessV1 | None:
    raw = _mapping(risk_data.get("freshness")) or _mapping(market_data.get("freshness"))
    return FreshnessV1.model_validate(raw) if raw else None


def _derive_freshness(
    market_data: Mapping[str, Any],
    risk_data: Mapping[str, Any],
    *,
    as_of: datetime,
    indices: tuple[IndexSnapshotV1, ...],
    breadth: BreadthSnapshotV1,
    turnover: TurnoverSnapshotV1,
    rotation: RotationSnapshotV1,
) -> FreshnessV1:
    top_fetch = _datetime(_first(market_data, "timestamp", "fetched_at"))
    risk_fetch = _datetime(_first(risk_data, "timestamp", "as_of")) or top_fetch
    component_statuses = _mapping(risk_data.get("components"))
    has_usable_component_details = any(
        bool(_mapping(component_statuses.get(name)))
        for name in (
            "breadth",
            "market_breadth",
            "rotation",
            "industry_quotes",
            "concept_quotes",
        )
    )
    components: list[FreshnessComponentV1] = []

    available_indices = [item for item in indices if item.available]
    index_times = [item.provider_as_of for item in available_indices if item.provider_as_of]
    index_old = any(item.date() != as_of.date() for item in index_times)
    complete_index_coverage = len(available_indices) == 4 and all(
        item.level is not None and item.change_pct is not None
        for item in available_indices
    )
    accepted_index_quality = complete_index_coverage and all(
        item.quality == ComponentQuality.ACCEPTED for item in available_indices
    )
    complete_provider_timestamps = bool(available_indices) and (
        len(index_times) == len(available_indices)
    )
    index_status = (
        FreshnessStatus.UNAVAILABLE
        if not available_indices
        else FreshnessStatus.STALE
        if index_old
        else FreshnessStatus.FRESH
        if complete_index_coverage
        and accepted_index_quality
        and complete_provider_timestamps
        and top_fetch is not None
        else FreshnessStatus.DEGRADED
    )
    components.append(FreshnessComponentV1(
        component="indices",
        status=index_status,
        quality=(
            ComponentQuality.ACCEPTED
            if index_status == FreshnessStatus.FRESH
            else ComponentQuality.DEGRADED
            if index_status in {FreshnessStatus.DEGRADED, FreshnessStatus.STALE}
            else ComponentQuality.UNAVAILABLE
        ),
        provider_as_of=max(index_times) if index_times else None,
        fetched_at=top_fetch,
        flags=tuple(
            flag
            for flag, active in (
                ("index_coverage_partial", not complete_index_coverage),
                (
                    "provider_timestamp_missing",
                    bool(available_indices) and not complete_provider_timestamps,
                ),
                ("provider_date_mismatch", index_old),
                ("fetch_time_missing", top_fetch is None),
            )
            if active
        ),
    ))

    def risk_component(
        name: str,
        *,
        available: bool,
        provider_as_of: datetime | None,
        status_names: Sequence[str] = (),
    ) -> FreshnessComponentV1:
        raw_statuses = [
            raw
            for key in (status_names or (name,))
            if (raw := _mapping(component_statuses.get(key)))
        ]
        fetched_times = [
            value
            for raw in raw_statuses
            if (value := _datetime(_first(raw, "fetched_at", "as_of")))
        ]
        provider_times = [
            value
            for raw in raw_statuses
            if (value := _datetime(raw.get("provider_as_of")))
        ]
        # A composite component is only as current as its oldest contributing
        # source.  Using the newest watermark would hide one frozen branch.
        fetched = min(fetched_times, default=None) or risk_fetch
        provider_time = min(
            (
                *provider_times,
                *fetched_times,
                *((provider_as_of,) if provider_as_of is not None else ()),
            ),
            default=None,
        )
        stale = any(
            bool(raw.get("stale") or raw.get("expired")) for raw in raw_statuses
        ) or (
            provider_time is not None and provider_time.date() != as_of.date()
        )
        unavailable = not available or bool(raw_statuses) and all(
            bool(raw.get("expired")) for raw in raw_statuses
        )
        degraded = any(
            bool(raw.get("partial") or raw.get("error") or raw.get("expired"))
            for raw in raw_statuses
        ) or fetched is None
        status = (
            FreshnessStatus.UNAVAILABLE
            if unavailable
            else FreshnessStatus.STALE
            if stale
            else FreshnessStatus.DEGRADED
            if degraded
            else FreshnessStatus.FRESH
        )
        quality = (
            ComponentQuality.ACCEPTED
            if status == FreshnessStatus.FRESH
            else ComponentQuality.DEGRADED
            if status in {FreshnessStatus.DEGRADED, FreshnessStatus.STALE}
            else ComponentQuality.UNAVAILABLE
        )
        flags = [
            str(item)
            for raw in raw_statuses
            for item in _sequence(raw.get("quality_flags"))
        ]
        if any(raw.get("error") for raw in raw_statuses):
            flags.append("component_error")
        if stale:
            flags.append("component_stale")
        if fetched is None:
            flags.append("fetch_time_missing")
        return FreshnessComponentV1(
            component=name,
            status=status,
            quality=quality,
            provider_as_of=provider_time,
            fetched_at=fetched,
            flags=tuple(dict.fromkeys(flags)),
        )

    components.append(risk_component(
        "breadth",
        available=breadth.available,
        provider_as_of=breadth.provider_as_of,
        status_names=("breadth", "market_breadth"),
    ))
    components.append(risk_component(
        "turnover",
        available=turnover.available,
        provider_as_of=(
            datetime.combine(
                turnover.today_date,
                time.fromisoformat(turnover.as_of),
                tzinfo=SHANGHAI,
            )
            if turnover.available and turnover.today_date and turnover.as_of
            else None
        ),
    ))
    rotation_time = min(
        (item.provider_as_of for item in rotation.sectors if item.provider_as_of),
        default=None,
    )
    components.append(risk_component(
        "rotation",
        available=bool(rotation.sectors),
        provider_as_of=rotation_time,
        status_names=("rotation", "industry_quotes", "concept_quotes"),
    ))

    flags: list[str] = []
    quality = _mapping(risk_data.get("data_quality"))
    # Modern risk responses carry component-level watermarks.  The legacy
    # aggregate bit is only authoritative when those details are absent.
    if bool(risk_data.get("stale")) and not has_usable_component_details:
        flags.append("legacy_snapshot_stale")
        components = [
            item.model_copy(update={
                "status": FreshnessStatus.STALE,
                "quality": ComponentQuality.DEGRADED,
                "flags": tuple(dict.fromkeys((*item.flags, "legacy_snapshot_stale"))),
            })
            if item.component in {"breadth", "rotation"}
            and item.status != FreshnessStatus.UNAVAILABLE
            else item
            for item in components
        ]
    if quality.get("partial"):
        flags.append("partial_component_coverage")
        components = [
            item.model_copy(update={
                "status": FreshnessStatus.DEGRADED,
                "quality": ComponentQuality.DEGRADED,
                "flags": tuple(dict.fromkeys((*item.flags, "partial_component_coverage"))),
            })
            if item.component == "rotation" and item.status == FreshnessStatus.FRESH
            else item
            for item in components
        ]

    overall = _overall_freshness_status(components)
    return FreshnessV1(
        status=overall,
        components=tuple(components),
        flags=tuple(flags),
    )


def _apply_session_safety(
    freshness: FreshnessV1,
    *,
    market_data: Mapping[str, Any],
    as_of: datetime,
    claimed_open: bool,
    indices: tuple[IndexSnapshotV1, ...],
) -> tuple[FreshnessV1, bool]:
    if not claimed_open:
        return freshness, True
    provider_times = [
        value
        for value in (
            _datetime(market_data.get("provider_as_of")),
            *(item.provider_as_of for item in indices if item.available),
            *(
                item.provider_as_of or item.fetched_at
                for item in freshness.components
            ),
        )
        if value is not None
    ]
    confirmed = bool(provider_times) and all(value.date() == as_of.date() for value in provider_times)
    if confirmed:
        component_times: dict[str, tuple[datetime, ...]] = {
            "indices": tuple(
                item.provider_as_of
                for item in indices
                if item.available and item.provider_as_of is not None
            ),
        }
        for item in freshness.components:
            if not component_times.get(item.component):
                component_times[item.component] = tuple(
                    value
                    for value in (item.provider_as_of or item.fetched_at,)
                    if value is not None
                )
        delayed = {
            component
            for component, observations in component_times.items()
            if observations
            and any(
                (
                    as_of.astimezone(SHANGHAI)
                    - observed.astimezone(SHANGHAI)
                ).total_seconds() > _INTRADAY_MAX_AGE_SECONDS[component]
                for observed in observations
            )
        }
        if not delayed:
            return freshness, True
        components = tuple(
            item.model_copy(update={
                "status": FreshnessStatus.STALE,
                "quality": ComponentQuality.DEGRADED,
                "flags": tuple(dict.fromkeys((*item.flags, "intraday_data_delayed"))),
            })
            if item.component in delayed
            and item.status != FreshnessStatus.UNAVAILABLE
            else item
            for item in freshness.components
        )
        updated = freshness.model_copy(update={
            "status": _overall_freshness_status(components),
            "components": components,
            "flags": tuple(dict.fromkeys((*freshness.flags, "intraday_data_delayed"))),
        })
        return FreshnessV1.model_validate(updated.model_dump()), True
    components = tuple(
        item.model_copy(update={
            "status": FreshnessStatus.STALE,
            "quality": ComponentQuality.DEGRADED,
            "flags": tuple(dict.fromkeys((*item.flags, "trading_session_unconfirmed"))),
        })
        if item.component == "indices" and item.status != FreshnessStatus.UNAVAILABLE
        else item
        for item in freshness.components
    )
    updated = freshness.model_copy(update={
        "status": _overall_freshness_status(components),
        "components": components,
        "flags": tuple(dict.fromkeys((*freshness.flags, "trading_session_unconfirmed"))),
    })
    return FreshnessV1.model_validate(updated.model_dump()), False


def build_guardrail(
    *,
    indices: Sequence[IndexSnapshotV1],
    breadth: BreadthSnapshotV1,
    turnover: TurnoverSnapshotV1,
    rotation: RotationSnapshotV1,
    freshness: FreshnessV1,
) -> GuardrailV1:
    """Build a market-level brake; current state and objections stay separate."""

    if freshness.status in {FreshnessStatus.STALE, FreshnessStatus.UNAVAILABLE}:
        return GuardrailV1(
            regime=MarketRegime.UNCERTAIN,
            severity=GuardrailSeverity.STOP,
            conclusion_strength=ConclusionStrength.ABSTAIN,
            current_state="关键盘面数据陈旧或不可用，当前结构不具备判断条件。",
            supporting_evidence=("数据质量闸门已触发",),
            counter_evidence=tuple(
                f"{item.component}:{item.status.value}"
                for item in freshness.components
                if item.status != FreshnessStatus.FRESH
            ) or ("缺少可核验的新鲜组件",),
            behavioral_constraint="暂停依据本页面形成新的方向判断，等待关键组件刷新并核对交易状态。",
        )

    changes = {
        item.role: item.change_pct
        for item in indices
        if item.available and item.change_pct is not None
    }
    broad = changes.get(IndexRole.BROAD_MARKET)
    large = changes.get(IndexRole.LARGE_CAP)
    small = changes.get(IndexRole.SMALL_CAP)
    growth = changes.get(IndexRole.GROWTH)
    breadth_ratio = breadth.advance_ratio if breadth.available else None
    support: list[str] = []
    counter: list[str] = []

    broad_positive = any(value is not None and value > 0.2 for value in (broad, large))
    elastic_weak = all(value is not None and value < 0 for value in (small, growth))
    breadth_narrow = breadth_ratio is not None and breadth_ratio < 0.48
    divergence = broad_positive and (elastic_weak or breadth_narrow)

    if broad_positive:
        support.append("宽基或大盘指数保持上涨")
    if small is not None and growth is not None and small > 0 and growth > 0:
        support.append("小盘与成长指数同步上涨")
    elif elastic_weak:
        counter.append("小盘与成长指数同步走弱")
    if breadth_ratio is not None:
        (support if breadth_ratio >= 0.52 else counter).append(
            f"全市场上涨占比{breadth_ratio:.0%}"
        )
    else:
        counter.append("市场广度不可用")
    if turnover.available:
        if turnover.direction == TurnoverDirection.EXPAND:
            support.append(f"成交额较昨日同期增加{turnover.difference_ratio:.1%}")
        elif turnover.direction == TurnoverDirection.SHRINK:
            counter.append(f"成交额较昨日同期减少{abs(turnover.difference_ratio):.1%}")
        else:
            counter.append(
                f"成交额处于±{turnover.neutral_band_ratio:.0%}噪声区间，未见显著变化"
            )
    else:
        counter.append("昨日同期成交额比较不可用")

    if divergence:
        regime = MarketRegime.MIXED
        severity = GuardrailSeverity.CAUTION
        strength = ConclusionStrength.MODERATE
        current = "指数表面偏强，但内部参与收窄，属于需要刹车的结构分化。"
        behavior = "先核对广度与小盘成长是否修复，避免仅凭指数红盘作出冲动决策。"
    elif (
        rotation.regime == MarketRegime.ATTACK
        and breadth_ratio is not None
        and breadth_ratio >= 0.52
        and any(value is not None and value > 0 for value in (small, growth))
    ):
        regime = MarketRegime.ATTACK
        severity = GuardrailSeverity.CALM
        strength = (
            ConclusionStrength.STRONG
            if breadth_ratio >= 0.55
            and turnover.available
            and turnover.direction == TurnoverDirection.EXPAND
            else ConclusionStrength.MODERATE
        )
        current = "进攻标签、指数弹性与市场广度相互确认。"
        behavior = "保持原有纪律，以广度转弱或进攻板块失去内部确认作为重新评估条件。"
    elif rotation.regime == MarketRegime.DEFENSE or (
        breadth_ratio is not None and breadth_ratio < 0.45 and broad_positive
    ):
        regime = MarketRegime.DEFENSE
        severity = GuardrailSeverity.CAUTION
        strength = ConclusionStrength.MODERATE
        current = "防御或避险方向相对占优，整体风险偏好没有形成扩散。"
        behavior = "降低对指数单点上涨的依赖，等待广度回到中性区间后再重新判断风险偏好。"
    elif rotation.regime == MarketRegime.MIXED:
        regime = MarketRegime.MIXED
        severity = GuardrailSeverity.CAUTION
        strength = ConclusionStrength.MODERATE
        current = "多类标签并存，盘面更接近轮动而非单一主线。"
        behavior = "把当前信息视作轮动观察，避免把单个板块的瞬时强势外推为全市场趋势。"
    else:
        regime = MarketRegime.UNCERTAIN
        severity = GuardrailSeverity.CAUTION
        strength = ConclusionStrength.WEAK
        current = "有效证据不足或相互矛盾，暂不能归类为进攻或防御。"
        behavior = "保留判断，等待价格与内部广度在同一方向形成确认。"

    if rotation.regime != MarketRegime.UNCERTAIN:
        support.append(rotation.summary)
    else:
        counter.append(rotation.summary)
    if freshness.status == FreshnessStatus.DEGRADED:
        severity = GuardrailSeverity.CAUTION
        strength = ConclusionStrength.WEAK
        counter.append("部分组件降级，结论强度已自动下调")
    return GuardrailV1(
        regime=regime,
        severity=severity,
        conclusion_strength=strength,
        current_state=current,
        supporting_evidence=tuple(dict.fromkeys(support)),
        counter_evidence=tuple(dict.fromkeys(counter)),
        behavioral_constraint=behavior,
    )


def build_scenarios(
    *,
    guardrail: GuardrailV1,
    indices: Sequence[IndexSnapshotV1],
    breadth: BreadthSnapshotV1,
    turnover: TurnoverSnapshotV1,
    rotation: RotationSnapshotV1,
) -> tuple[ScenarioV1, ...]:
    """Return at most two 5-15 minute conditional scenarios without odds."""

    if guardrail.severity == GuardrailSeverity.STOP:
        return ()
    ratio = breadth.advance_ratio if breadth.available else None
    if guardrail.regime == MarketRegime.ATTACK:
        return (ScenarioV1(
            scenario_id="attack_holds",
            horizon_minutes=10,
            if_condition="若上涨占比维持在52%以上，且进攻类板块继续获得价格与内部广度确认",
            then_expectation="则当前进攻结构可视为延续，而不是仅由资金流单项推动",
            invalidation="上涨占比跌破50%，或成长与小盘指数同时转弱",
            evidence=tuple(guardrail.supporting_evidence[:3]),
        ),)
    if guardrail.regime == MarketRegime.DEFENSE:
        return (ScenarioV1(
            scenario_id="defense_holds",
            horizon_minutes=10,
            if_condition="若防御或避险板块继续获得价格和内部广度确认，且全市场上涨占比仍低于45%",
            then_expectation="则盘面仍应按防御占优理解",
            invalidation="进攻标签板块转强且全市场上涨占比回到52%以上",
            evidence=tuple(guardrail.supporting_evidence[:3]),
        ),)
    if guardrail.regime == MarketRegime.MIXED:
        return (
            ScenarioV1(
                scenario_id="breadth_repairs",
                horizon_minutes=10,
                if_condition="若全市场上涨占比回到52%以上，且小盘与成长指数不再弱于大盘",
                then_expectation="则当前分化可能转为更广泛的参与",
                invalidation="上涨占比再次跌破48%或弹性指数继续走弱",
                evidence=(f"当前上涨占比{ratio:.0%}" if ratio is not None else "当前广度待确认",),
            ),
            ScenarioV1(
                scenario_id="divergence_widens",
                horizon_minutes=10,
                if_condition="若宽基指数维持红盘但上涨占比继续低于48%",
                then_expectation="则指数与内部参与的分化仍在扩大",
                invalidation="上涨占比回到52%以上并连续获得弹性指数确认",
                evidence=tuple(guardrail.counter_evidence[:3]),
            ),
        )
    return (ScenarioV1(
        scenario_id="evidence_confirms",
        horizon_minutes=10,
        if_condition="若价格与内部广度在同一标签方向形成同步确认",
        then_expectation="则盘面才具备从不确定转入明确结构的条件",
        invalidation="价格与广度继续背离或关键组件转为陈旧",
        evidence=tuple(guardrail.counter_evidence[:3]),
    ),)


def _market_state_alert(
    guardrail: GuardrailV1, freshness: FreshnessV1
) -> tuple[AlertV1, ...]:
    if guardrail.severity == GuardrailSeverity.CALM:
        return ()
    if freshness.status == FreshnessStatus.UNAVAILABLE:
        code, title = "data_unavailable", "关键盘面数据不可用"
    elif freshness.status == FreshnessStatus.STALE:
        code, title = "data_stale", "关键盘面数据已陈旧"
    elif guardrail.regime == MarketRegime.MIXED:
        code, title = "market_divergence", "指数与内部结构存在分化"
    elif guardrail.regime == MarketRegime.DEFENSE:
        code, title = "defense_dominant", "防御结构占优"
    else:
        code, title = "market_caution", "盘面证据不足"
    dedupe_key = (
        f"market_watch:{code}"
        if code in {"data_stale", "data_unavailable"}
        else "market_watch:market_state"
    )
    return (AlertV1(
        kind="data_quality" if code.startswith("data_") else "market_state",
        code=code,
        severity=guardrail.severity,
        title=title,
        message=guardrail.behavioral_constraint,
        dedupe_key=dedupe_key,
    ),)


def _sector_move_alerts(
    *,
    freshness: FreshnessV1,
    market_state: MarketStateV1,
    trajectories: Sequence[SectorFlowTrajectoryV1 | None],
) -> tuple[AlertV1, ...]:
    if freshness.status in {
        FreshnessStatus.STALE,
        FreshnessStatus.UNAVAILABLE,
    } or not market_state.is_open:
        return ()

    candidates: list[tuple[float, AlertV1]] = []
    for trajectory in trajectories:
        if trajectory is None or trajectory.status not in {
            SectorFlowTrajectoryStatus.READY,
            SectorFlowTrajectoryStatus.PARTIAL,
        }:
            continue
        for sector in trajectory.sectors:
            latest = sector.latest
            if latest is None or latest.change_delta_5m_pct is None:
                continue
            magnitude = abs(latest.change_delta_5m_pct)
            threshold = next(
                (
                    value
                    for value in SECTOR_MOVE_ALERT_THRESHOLDS_PCT
                    if magnitude >= value
                ),
                None,
            )
            if threshold is None:
                continue
            move_direction = (
                "strengthening" if latest.change_delta_5m_pct > 0 else "weakening"
            )
            code = "sector_move_up" if move_direction == "strengthening" else "sector_move_down"
            move_label = "突然增强" if move_direction == "strengthening" else "突然走弱"
            delta_label = f"{latest.change_delta_5m_pct:+.2f}个百分点"
            flow_label = (
                f"，近5分钟资金变化{latest.delta_5m_cny / 100_000_000:+.1f}亿元"
                if latest.delta_5m_cny is not None
                else ""
            )
            threshold_key = str(threshold).replace(".", "_")
            leaders = (
                sector.leader_snapshot.leaders
                if sector.leader_snapshot is not None
                else ()
            )
            candidates.append((
                magnitude,
                AlertV1(
                    kind="sector_move",
                    code=code,
                    severity=GuardrailSeverity.CAUTION,
                    title=f"{sector.name}{move_label}",
                    message=f"5分钟板块涨幅变化{delta_label}{flow_label}。",
                    dedupe_key=(
                        f"market_watch:sector_move:{trajectory.direction}:"
                        f"{sector.sector_key}:{move_direction}:{threshold_key}"
                    ),
                    sector_key=sector.sector_key,
                    sector_label=sector.name,
                    sector_direction=trajectory.direction,
                    move_direction=move_direction,
                    trigger_threshold_pct=threshold,
                    change_delta_5m_pct=latest.change_delta_5m_pct,
                    change_pct=latest.change_pct,
                    flow_delta_5m_cny=latest.delta_5m_cny,
                    provider_as_of=latest.provider_as_of,
                    leaders=leaders,
                ),
            ))
    candidates.sort(key=lambda item: (-item[0], item[1].dedupe_key))
    return tuple(item[1] for item in candidates[:MAX_SECTOR_MOVE_ALERTS])


def _candidate_alerts(
    *,
    guardrail: GuardrailV1,
    freshness: FreshnessV1,
    market_state: MarketStateV1,
    sector_flow_trajectory: SectorFlowTrajectoryV1 | None,
    offense_sector_flow_trajectory: SectorFlowTrajectoryV1 | None,
) -> tuple[AlertV1, ...]:
    return (
        *_market_state_alert(guardrail, freshness),
        *_sector_move_alerts(
            freshness=freshness,
            market_state=market_state,
            trajectories=(
                sector_flow_trajectory,
                offense_sector_flow_trajectory,
            ),
        ),
    )


def _normalize_change(
    market_data: Mapping[str, Any], risk_data: Mapping[str, Any]
) -> ChangeSummaryV1:
    raw = _mapping(risk_data.get("change")) or _mapping(market_data.get("change"))
    if raw:
        return ChangeSummaryV1.model_validate(raw)
    events = _sequence(_mapping(risk_data.get("offense")).get("events"))
    if events:
        event = _mapping(events[-1])
        event_at = str(event.get("at") or "").strip()
        event_id = str(_first(event, "id", "key") or "market").strip()
        previous_state = str(event.get("from") or "").strip()
        current_state = str(event.get("to") or "").strip()
        if event_at and previous_state and current_state and previous_state != current_state:
            name = str(event.get("name") or "盘面方向").strip()
            summary = str(event.get("label") or "").strip() or (
                f"{name}由{previous_state}转为{current_state}"
            )
            return ChangeSummaryV1(
                available=True,
                previous_snapshot_id=(
                    f"event-before:{event_id}:{previous_state}:{event_at}"
                ),
                summary=summary,
                changed_fields=(f"rotation.sectors.{event_id}.lifecycle",),
            )
    dynamics = _mapping(risk_data.get("dynamics"))
    confirmed_at = str(dynamics.get("last_confirmed_at") or "").strip()
    headline = str(dynamics.get("headline") or "").strip()
    generic_headlines = {
        "主要结构相对稳定",
        "板块状态出现变化，等待确认",
        "动态样本积累中",
        "动态数据暂不可用",
    }
    if (
        confirmed_at
        and headline
        and headline not in generic_headlines
        and str(dynamics.get("status") or "") in {"ready", "closed"}
    ):
        return ChangeSummaryV1(
            available=True,
            previous_snapshot_id=f"trajectory-before:{confirmed_at}",
            summary=headline,
            changed_fields=("dynamics",),
        )
    return ChangeSummaryV1(
        available=False,
        reason="no_confirmed_change_evidence",
    )


def normalize_market_watch_input(
    market_data: Mapping[str, Any],
    risk_data: Mapping[str, Any] | None = None,
    *,
    as_of: datetime | str | None = None,
    turnover_flat_threshold_pct: float = DEFAULT_TURNOVER_FLAT_THRESHOLD_PCT,
) -> dict[str, Any]:
    """Normalize two legacy/provider-neutral dictionaries without dashboard imports."""

    market = _mapping(market_data)
    risk = _mapping(risk_data)
    observed_at = _resolve_as_of(as_of, market)
    indices = _normalize_indices(market, observed_at)
    breadth = _normalize_breadth(risk)
    turnover = _normalize_turnover(
        market, flat_threshold_pct=turnover_flat_threshold_pct
    )
    rotation = classify_rotation(_rotation_inputs(risk))
    sector_flow_trajectory = _normalize_sector_flow_trajectory(risk)
    offense_sector_flow_trajectory = _normalize_sector_flow_trajectory(
        risk,
        field="offense_sector_flow_trajectory",
        direction="offense",
    )
    freshness = _explicit_freshness(market, risk) or _derive_freshness(
        market,
        risk,
        as_of=observed_at,
        indices=indices,
        breadth=breadth,
        turnover=turnover,
        rotation=rotation,
    )
    raw_state = _mapping(market.get("market_state"))
    claimed_open = bool(raw_state.get("is_open", False))
    freshness, session_confirmed = _apply_session_safety(
        freshness,
        market_data=market,
        as_of=observed_at,
        claimed_open=claimed_open,
        indices=indices,
    )
    phase = (
        _infer_phase(observed_at, claimed_open, risk.get("phase") or raw_state.get("phase"))
        if session_confirmed
        else MarketPhase.UNKNOWN
    )
    market_state = MarketStateV1(
        phase=phase,
        is_open=(
            session_confirmed
            and phase in {MarketPhase.OPENING_OBSERVATION, MarketPhase.TRADING}
        ),
        trading_date=observed_at.astimezone(SHANGHAI).date(),
    )
    return {
        "as_of": observed_at,
        "market_state": market_state,
        "freshness": freshness,
        "indices": indices,
        "breadth": breadth,
        "turnover": turnover,
        "rotation": rotation,
        "sector_flow_trajectory": sector_flow_trajectory,
        "offense_sector_flow_trajectory": offense_sector_flow_trajectory,
        "change": _normalize_change(market, risk),
    }


def build_market_watch_snapshot(
    market_data: Mapping[str, Any],
    risk_data: Mapping[str, Any] | None = None,
    *,
    as_of: datetime | str | None = None,
    sequence: int = 0,
    snapshot_id: str | None = None,
    turnover_flat_threshold_pct: float = DEFAULT_TURNOVER_FLAT_THRESHOLD_PCT,
) -> MarketWatchSnapshotV1:
    """Build one ``market_watch.v1`` snapshot from two ordinary dictionaries."""

    normalized = normalize_market_watch_input(
        market_data,
        risk_data,
        as_of=as_of,
        turnover_flat_threshold_pct=turnover_flat_threshold_pct,
    )
    guardrail = build_guardrail(
        indices=normalized["indices"],
        breadth=normalized["breadth"],
        turnover=normalized["turnover"],
        rotation=normalized["rotation"],
        freshness=normalized["freshness"],
    )
    scenarios = build_scenarios(
        guardrail=guardrail,
        indices=normalized["indices"],
        breadth=normalized["breadth"],
        turnover=normalized["turnover"],
        rotation=normalized["rotation"],
    )
    return MarketWatchSnapshotV1(
        snapshot_id=snapshot_id or f"mw-{sequence}-{uuid4().hex}",
        sequence=sequence,
        as_of=normalized["as_of"],
        market_state=normalized["market_state"],
        freshness=normalized["freshness"],
        guardrail=guardrail,
        indices=normalized["indices"],
        breadth=normalized["breadth"],
        turnover=normalized["turnover"],
        rotation=normalized["rotation"],
        sector_flow_trajectory=normalized["sector_flow_trajectory"],
        offense_sector_flow_trajectory=normalized[
            "offense_sector_flow_trajectory"
        ],
        scenarios=scenarios,
        change=normalized["change"],
        alerts=_candidate_alerts(
            guardrail=guardrail,
            freshness=normalized["freshness"],
            market_state=normalized["market_state"],
            sector_flow_trajectory=normalized["sector_flow_trajectory"],
            offense_sector_flow_trajectory=normalized[
                "offense_sector_flow_trajectory"
            ],
        ),
    )


__all__ = [
    "DEFAULT_TURNOVER_FLAT_THRESHOLD_PCT",
    "SHANGHAI",
    "build_guardrail",
    "build_market_watch_snapshot",
    "build_scenarios",
    "classify_rotation",
    "classify_turnover",
    "normalize_market_watch_input",
]
