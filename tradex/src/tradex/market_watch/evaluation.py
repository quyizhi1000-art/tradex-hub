"""Deterministic replay and operational evaluation for ``market_watch.v1``.

The evaluator consumes only canonical snapshots and emitted alert events.  It
never fetches market data, changes runtime thresholds, estimates investment
returns, or produces stock-level advice.  Session coverage follows the two
mainland continuous-auction windows (09:30-11:30 and 13:00-15:00, Shanghai
time); exchange-holiday eligibility remains the responsibility of the caller's
trading-calendar boundary.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import Any, Iterable, Literal, Mapping, Sequence
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import (
    AlertV1,
    FreshnessStatus,
    GuardrailSeverity,
    MarketRegime,
    MarketWatchSnapshotV1,
)
from .policy import DEFAULT_MARKET_WATCH_POLICY


SHANGHAI = ZoneInfo("Asia/Shanghai")
_SESSION_SECONDS = 4 * 60 * 60
_SESSION_MINUTES = 240


class EvaluationModel(BaseModel):
    """Immutable, JSON-serializable evaluation contract fragment."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EvaluationVerdict(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    INSUFFICIENT = "insufficient"


class CheckStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    INSUFFICIENT = "insufficient"
    NOT_APPLICABLE = "not_applicable"


class EvaluationConfigV1(EvaluationModel):
    """Versioned, read-only replay thresholds.

    A calibration hint can recommend a *separate* replay with different
    values, but this object and the live policy are never mutated.
    """

    config_version: str = Field(
        default=DEFAULT_MARKET_WATCH_POLICY.config_version, min_length=1
    )
    expected_interval_seconds: float = Field(
        default=DEFAULT_MARKET_WATCH_POLICY.trading_refresh_interval_seconds,
        gt=0,
        le=300,
    )
    continuity_gap_seconds: float = Field(
        default=DEFAULT_MARKET_WATCH_POLICY.continuity_gap_seconds,
        gt=0,
        le=900,
    )
    confirmation_samples: int = Field(
        default=DEFAULT_MARKET_WATCH_POLICY.alert_confirmation_samples,
        ge=1,
        le=10,
    )
    rapid_reversal_window_seconds: float = Field(
        default=DEFAULT_MARKET_WATCH_POLICY.rapid_reversal_window_seconds,
        gt=0,
        le=3600,
    )
    minimum_samples_per_session: int = Field(
        default=DEFAULT_MARKET_WATCH_POLICY.minimum_samples_per_session,
        ge=1,
    )
    minimum_trading_minute_coverage_ratio: float = Field(
        default=(
            DEFAULT_MARKET_WATCH_POLICY.minimum_trading_minute_coverage_ratio
        ),
        ge=0,
        le=1,
    )
    minimum_fresh_ratio: float = Field(
        default=DEFAULT_MARKET_WATCH_POLICY.minimum_fresh_ratio,
        ge=0,
        le=1,
    )
    maximum_degraded_ratio: float = Field(
        default=DEFAULT_MARKET_WATCH_POLICY.maximum_degraded_ratio,
        ge=0,
        le=1,
    )
    maximum_stale_unavailable_ratio: float = Field(
        default=DEFAULT_MARKET_WATCH_POLICY.maximum_stale_unavailable_ratio,
        ge=0,
        le=1,
    )
    maximum_data_gap_seconds: float = Field(
        default=DEFAULT_MARKET_WATCH_POLICY.maximum_data_gap_seconds,
        gt=0,
        le=3600,
    )
    maximum_rapid_reversal_ratio: float = Field(
        default=DEFAULT_MARKET_WATCH_POLICY.maximum_rapid_reversal_ratio,
        ge=0,
        le=1,
    )
    minimum_sessions_for_multi_day: int = Field(
        default=DEFAULT_MARKET_WATCH_POLICY.minimum_sessions_for_multi_day,
        ge=1,
        le=30,
    )

    @model_validator(mode="after")
    def validate_threshold_relationships(self) -> "EvaluationConfigV1":
        if self.continuity_gap_seconds < self.expected_interval_seconds:
            raise ValueError(
                "continuity_gap_seconds cannot be shorter than expected_interval_seconds"
            )
        if self.maximum_data_gap_seconds < self.continuity_gap_seconds:
            raise ValueError(
                "maximum_data_gap_seconds cannot be shorter than continuity_gap_seconds"
            )
        return self


class ValueCountV1(EvaluationModel):
    value: str = Field(min_length=1)
    count: int = Field(ge=0)
    ratio: float = Field(ge=0, le=1)


class DwellMetricV1(EvaluationModel):
    value: str = Field(min_length=1)
    sample_count: int = Field(ge=0)
    sample_ratio: float = Field(ge=0, le=1)
    covered_trading_minutes: int = Field(ge=0)
    covered_minute_ratio: float = Field(ge=0, le=1)


class TransitionV1(EvaluationModel):
    from_value: str = Field(min_length=1)
    to_value: str = Field(min_length=1)
    count: int = Field(ge=1)


class TransitionMetricsV1(EvaluationModel):
    transition_count: int = Field(ge=0)
    transitions: tuple[TransitionV1, ...] = ()


class CoverageMetricsV1(EvaluationModel):
    sample_count: int = Field(ge=0)
    trading_sample_count: int = Field(ge=0)
    expected_sample_count: int = Field(ge=0)
    sample_coverage_ratio: float = Field(ge=0, le=1)
    expected_trading_minutes: int = Field(ge=0)
    covered_trading_minutes: int = Field(ge=0)
    trading_minute_coverage_ratio: float = Field(ge=0, le=1)
    longest_data_gap_seconds: float = Field(ge=0)
    observed_start: datetime | None = None
    observed_end: datetime | None = None


class FreshnessMetricsV1(EvaluationModel):
    evaluated_sample_count: int = Field(ge=0)
    by_status: tuple[ValueCountV1, ...]
    fresh_ratio: float = Field(ge=0, le=1)
    degraded_ratio: float = Field(ge=0, le=1)
    stale_ratio: float = Field(ge=0, le=1)
    unavailable_ratio: float = Field(ge=0, le=1)


class AlertMetricsV1(EvaluationModel):
    source: Literal["snapshot", "explicit"]
    emitted_alert_count: int = Field(ge=0)
    by_code: tuple[ValueCountV1, ...]
    by_severity: tuple[ValueCountV1, ...]
    candidate_run_count: int = Field(ge=0)
    confirmed_candidate_run_count: int = Field(ge=0)
    unconfirmed_candidate_run_count: int = Field(ge=0)
    confirmation_observability_ratio: float | None = Field(default=None, ge=0, le=1)


class NoiseMetricsV1(EvaluationModel):
    regime_rapid_reversal_count: int = Field(ge=0)
    severity_rapid_reversal_count: int = Field(ge=0)
    total_rapid_reversal_count: int = Field(ge=0)
    total_state_transition_count: int = Field(ge=0)
    rapid_reversal_ratio: float = Field(ge=0, le=1)
    rapid_reversals_per_trading_hour: float = Field(ge=0)


class AcceptanceCheckV1(EvaluationModel):
    code: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    status: CheckStatus
    observed: str = Field(min_length=1)
    requirement: str = Field(min_length=1)


class AcceptanceV1(EvaluationModel):
    verdict: EvaluationVerdict
    checks: tuple[AcceptanceCheckV1, ...]
    reasons: tuple[str, ...]


class CalibrationHintV1(EvaluationModel):
    code: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    metric: str = Field(min_length=1)
    observation: str = Field(min_length=1)
    action: str = Field(min_length=1)
    requires_manual_review: Literal[True] = True
    auto_apply: Literal[False] = False


class EvaluationMetricsV1(EvaluationModel):
    coverage: CoverageMetricsV1
    freshness: FreshnessMetricsV1
    regime_dwell: tuple[DwellMetricV1, ...]
    severity_dwell: tuple[DwellMetricV1, ...]
    regime_transitions: TransitionMetricsV1
    severity_transitions: TransitionMetricsV1
    alerts: AlertMetricsV1
    noise: NoiseMetricsV1


class MarketWatchSessionEvaluationV1(EvaluationModel):
    contract: Literal["market_watch_evaluation.v1"] = "market_watch_evaluation.v1"
    schema_version: Literal[1] = 1
    config_version: str = Field(min_length=1)
    scope: Literal["session"] = "session"
    trade_date: date
    config: EvaluationConfigV1
    metrics: EvaluationMetricsV1
    acceptance: AcceptanceV1
    calibration_hints: tuple[CalibrationHintV1, ...]


class SessionVerdictV1(EvaluationModel):
    trade_date: date
    verdict: EvaluationVerdict
    sample_count: int = Field(ge=0)
    trading_minute_coverage_ratio: float = Field(ge=0, le=1)
    fresh_ratio: float = Field(ge=0, le=1)
    rapid_reversal_ratio: float = Field(ge=0, le=1)


class MarketWatchMultiDayEvaluationV1(EvaluationModel):
    contract: Literal["market_watch_evaluation.v1"] = "market_watch_evaluation.v1"
    schema_version: Literal[1] = 1
    config_version: str = Field(min_length=1)
    scope: Literal["multi_day"] = "multi_day"
    date_from: date
    date_to: date
    session_count: int = Field(ge=1)
    config: EvaluationConfigV1
    metrics: EvaluationMetricsV1
    acceptance: AcceptanceV1
    session_verdicts: tuple[SessionVerdictV1, ...]
    calibration_hints: tuple[CalibrationHintV1, ...]


@dataclass(frozen=True)
class _AlertObservation:
    alert: AlertV1
    trade_date: date | None


@dataclass(frozen=True)
class _StateRun:
    value: str
    trade_date: date
    group: int
    start_offset: float
    last_offset: float
    sample_count: int


def _ratio(numerator: int | float, denominator: int | float) -> float:
    if denominator <= 0:
        return 0.0
    return min(1.0, max(0.0, float(numerator) / float(denominator)))


def _enum_value(value: Any) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def _parse_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("alert timestamps must include a timezone")
    return parsed


def _coerce_config(
    config: EvaluationConfigV1 | Mapping[str, Any] | None,
) -> EvaluationConfigV1:
    if config is None:
        return EvaluationConfigV1()
    if isinstance(config, EvaluationConfigV1):
        return config
    return EvaluationConfigV1.model_validate(config)


def _coerce_snapshots(
    samples: Iterable[MarketWatchSnapshotV1 | Mapping[str, Any]],
) -> tuple[MarketWatchSnapshotV1, ...]:
    snapshots = tuple(
        MarketWatchSnapshotV1.model_validate(
            item.model_dump(mode="python")
            if isinstance(item, MarketWatchSnapshotV1)
            else item
        )
        for item in samples
    )
    seen_ids: set[str] = set()
    previous: tuple[datetime, int] | None = None
    for snapshot in snapshots:
        if snapshot.snapshot_id in seen_ids:
            raise ValueError(f"duplicate market-watch snapshot_id: {snapshot.snapshot_id}")
        seen_ids.add(snapshot.snapshot_id)
        current = (snapshot.as_of, snapshot.sequence)
        if previous is not None and current <= previous:
            raise ValueError(
                "market-watch samples must be strictly ordered by (as_of, sequence)"
            )
        previous = current
    return snapshots


def _session_offset_seconds(snapshot: MarketWatchSnapshotV1) -> float | None:
    if not snapshot.market_state.is_open:
        return None
    local = snapshot.as_of.astimezone(SHANGHAI)
    seconds = local.hour * 3600 + local.minute * 60 + local.second
    morning_start = 9 * 3600 + 30 * 60
    morning_end = 11 * 3600 + 30 * 60
    afternoon_start = 13 * 3600
    afternoon_end = 15 * 3600
    if morning_start <= seconds < morning_end:
        return float(seconds - morning_start)
    if afternoon_start <= seconds < afternoon_end:
        return float((morning_end - morning_start) + seconds - afternoon_start)
    return None


def _active_samples(
    snapshots: Sequence[MarketWatchSnapshotV1],
) -> tuple[tuple[MarketWatchSnapshotV1, float], ...]:
    active: list[tuple[MarketWatchSnapshotV1, float]] = []
    for snapshot in snapshots:
        offset = _session_offset_seconds(snapshot)
        if offset is not None:
            active.append((snapshot, offset))
    return tuple(active)


def _coverage_metrics(
    snapshots: Sequence[MarketWatchSnapshotV1],
    session_dates: Sequence[date],
    config: EvaluationConfigV1,
) -> CoverageMetricsV1:
    active = _active_samples(snapshots)
    expected_minutes = _SESSION_MINUTES * len(session_dates)
    expected_samples = math.ceil(
        _SESSION_SECONDS / config.expected_interval_seconds
    ) * len(session_dates)
    covered = {
        (snapshot.market_state.trading_date, int(offset // 60))
        for snapshot, offset in active
    }
    longest_gap = 0.0
    offsets_by_date: dict[date, list[float]] = {item: [] for item in session_dates}
    for snapshot, offset in active:
        offsets_by_date.setdefault(snapshot.market_state.trading_date, []).append(offset)
    for values in offsets_by_date.values():
        points = (0.0, *sorted(set(values)), float(_SESSION_SECONDS))
        longest_gap = max(
            longest_gap,
            max((right - left for left, right in zip(points, points[1:])), default=0.0),
        )
    return CoverageMetricsV1(
        sample_count=len(snapshots),
        trading_sample_count=len(active),
        expected_sample_count=expected_samples,
        sample_coverage_ratio=_ratio(len(active), expected_samples),
        expected_trading_minutes=expected_minutes,
        covered_trading_minutes=len(covered),
        trading_minute_coverage_ratio=_ratio(len(covered), expected_minutes),
        longest_data_gap_seconds=longest_gap,
        observed_start=snapshots[0].as_of if snapshots else None,
        observed_end=snapshots[-1].as_of if snapshots else None,
    )


def _value_counts(
    values: Sequence[str], ordered_values: Sequence[str] | None = None
) -> tuple[ValueCountV1, ...]:
    counts = Counter(values)
    total = len(values)
    order = tuple(ordered_values) if ordered_values is not None else tuple(sorted(counts))
    return tuple(
        ValueCountV1(value=value, count=counts[value], ratio=_ratio(counts[value], total))
        for value in order
    )


def _freshness_metrics(
    active: Sequence[tuple[MarketWatchSnapshotV1, float]],
) -> FreshnessMetricsV1:
    ordered = tuple(item.value for item in FreshnessStatus)
    values = tuple(snapshot.freshness.status.value for snapshot, _ in active)
    counts = Counter(values)
    total = len(values)
    return FreshnessMetricsV1(
        evaluated_sample_count=total,
        by_status=_value_counts(values, ordered),
        fresh_ratio=_ratio(counts[FreshnessStatus.FRESH.value], total),
        degraded_ratio=_ratio(counts[FreshnessStatus.DEGRADED.value], total),
        stale_ratio=_ratio(counts[FreshnessStatus.STALE.value], total),
        unavailable_ratio=_ratio(counts[FreshnessStatus.UNAVAILABLE.value], total),
    )


def _dwell_metrics(
    active: Sequence[tuple[MarketWatchSnapshotV1, float]],
    *,
    field: Literal["regime", "severity"],
) -> tuple[DwellMetricV1, ...]:
    if field == "regime":
        ordered = tuple(item.value for item in MarketRegime)
        get_value = lambda snapshot: snapshot.guardrail.regime.value
    else:
        ordered = tuple(item.value for item in GuardrailSeverity)
        get_value = lambda snapshot: snapshot.guardrail.severity.value
    sample_values = tuple(get_value(snapshot) for snapshot, _ in active)
    sample_counts = Counter(sample_values)
    minute_values: dict[tuple[date, int], str] = {}
    for snapshot, offset in active:
        minute_values[(snapshot.market_state.trading_date, int(offset // 60))] = get_value(
            snapshot
        )
    minute_counts = Counter(minute_values.values())
    return tuple(
        DwellMetricV1(
            value=value,
            sample_count=sample_counts[value],
            sample_ratio=_ratio(sample_counts[value], len(sample_values)),
            covered_trading_minutes=minute_counts[value],
            covered_minute_ratio=_ratio(minute_counts[value], len(minute_values)),
        )
        for value in ordered
    )


def _state_runs(
    active: Sequence[tuple[MarketWatchSnapshotV1, float]],
    *,
    field: Literal["regime", "severity"],
    continuity_gap_seconds: float,
) -> tuple[_StateRun, ...]:
    runs: list[_StateRun] = []
    group = 0
    previous_date: date | None = None
    previous_offset: float | None = None
    for snapshot, offset in active:
        trade_date = snapshot.market_state.trading_date
        value = (
            snapshot.guardrail.regime.value
            if field == "regime"
            else snapshot.guardrail.severity.value
        )
        continuous = (
            previous_date == trade_date
            and previous_offset is not None
            and 0 <= offset - previous_offset <= continuity_gap_seconds
        )
        if not continuous:
            group += 1
        if runs and continuous and runs[-1].value == value and runs[-1].group == group:
            prior = runs[-1]
            runs[-1] = _StateRun(
                value=value,
                trade_date=trade_date,
                group=group,
                start_offset=prior.start_offset,
                last_offset=offset,
                sample_count=prior.sample_count + 1,
            )
        else:
            runs.append(
                _StateRun(
                    value=value,
                    trade_date=trade_date,
                    group=group,
                    start_offset=offset,
                    last_offset=offset,
                    sample_count=1,
                )
            )
        previous_date = trade_date
        previous_offset = offset
    return tuple(runs)


def _transition_metrics(runs: Sequence[_StateRun]) -> TransitionMetricsV1:
    pairs = Counter(
        (left.value, right.value)
        for left, right in zip(runs, runs[1:])
        if left.group == right.group and left.value != right.value
    )
    transitions = tuple(
        TransitionV1(from_value=left, to_value=right, count=count)
        for (left, right), count in sorted(pairs.items())
    )
    return TransitionMetricsV1(
        transition_count=sum(item.count for item in transitions),
        transitions=transitions,
    )


def _rapid_reversal_count(
    runs: Sequence[_StateRun], window_seconds: float
) -> int:
    count = 0
    for first, middle, last in zip(runs, runs[1:], runs[2:]):
        if (
            first.group == middle.group == last.group
            and first.value == last.value
            and first.value != middle.value
            and last.start_offset - first.last_offset <= window_seconds
        ):
            count += 1
    return count


def _candidate_code(snapshot: MarketWatchSnapshotV1) -> str | None:
    if snapshot.freshness.status != FreshnessStatus.FRESH:
        return None
    if snapshot.guardrail.severity == GuardrailSeverity.CALM:
        return None
    if snapshot.guardrail.regime == MarketRegime.MIXED:
        return "market_divergence"
    if snapshot.guardrail.regime == MarketRegime.DEFENSE:
        return "defense_dominant"
    return "market_caution"


def _confirmation_runs(
    active: Sequence[tuple[MarketWatchSnapshotV1, float]],
    config: EvaluationConfigV1,
) -> tuple[int, int]:
    runs: list[int] = []
    current_code: str | None = None
    current_count = 0
    previous_date: date | None = None
    previous_offset: float | None = None
    for snapshot, offset in active:
        trade_date = snapshot.market_state.trading_date
        code = _candidate_code(snapshot)
        continuous = (
            previous_date == trade_date
            and previous_offset is not None
            and 0 <= offset - previous_offset <= config.continuity_gap_seconds
        )
        if code is not None and continuous and code == current_code:
            current_count += 1
        else:
            if current_code is not None:
                runs.append(current_count)
            current_code = code
            current_count = 1 if code is not None else 0
        previous_date = trade_date
        previous_offset = offset
    if current_code is not None:
        runs.append(current_count)
    confirmed = sum(count >= config.confirmation_samples for count in runs)
    return len(runs), confirmed


def _coerce_alert_observation(value: Any) -> _AlertObservation:
    if isinstance(value, AlertV1):
        return _AlertObservation(alert=value, trade_date=None)
    if not isinstance(value, Mapping):
        if all(hasattr(value, field) for field in AlertV1.model_fields):
            payload = {field: getattr(value, field) for field in AlertV1.model_fields}
            return _AlertObservation(alert=AlertV1.model_validate(payload), trade_date=None)
        raise TypeError("alerts must contain canonical AlertV1 values or mappings")

    metadata_keys = {
        "trade_date",
        "emitted_at",
        "as_of",
        "timestamp",
        "snapshot_id",
    }
    if "alert" in value:
        unknown_envelope = set(value) - metadata_keys - {"alert"}
        if unknown_envelope:
            raise ValueError(
                "unknown explicit-alert envelope fields: "
                + ", ".join(sorted(str(item) for item in unknown_envelope))
            )
        raw_alert = value["alert"]
    else:
        raw_alert = {key: item for key, item in value.items() if key not in metadata_keys}
    if isinstance(raw_alert, AlertV1):
        alert = raw_alert
    elif isinstance(raw_alert, Mapping):
        alert = AlertV1.model_validate(raw_alert)
    else:
        raise TypeError("explicit alert envelope must contain a canonical AlertV1 mapping")
    raw_date = value.get("trade_date")
    timestamp = None
    for key in ("emitted_at", "as_of", "timestamp"):
        if value.get(key) is not None:
            timestamp = _parse_datetime(value[key])
            break
    event_date = _parse_date(raw_date) if raw_date is not None else None
    if timestamp is not None:
        timestamp_date = timestamp.astimezone(SHANGHAI).date()
        if event_date is not None and event_date != timestamp_date:
            raise ValueError("alert trade_date does not match its timestamp")
        event_date = timestamp_date
    return _AlertObservation(alert=alert, trade_date=event_date)


def _alert_metrics(
    snapshots: Sequence[MarketWatchSnapshotV1],
    active: Sequence[tuple[MarketWatchSnapshotV1, float]],
    explicit_alerts: Sequence[_AlertObservation],
    config: EvaluationConfigV1,
    *,
    explicit_alert_source: bool,
) -> AlertMetricsV1:
    if explicit_alert_source:
        source: Literal["snapshot", "explicit"] = "explicit"
        alerts = tuple(item.alert for item in explicit_alerts)
    else:
        source = "snapshot"
        alerts = tuple(alert for snapshot in snapshots for alert in snapshot.alerts)
    codes = tuple(alert.code for alert in alerts)
    severities = tuple(_enum_value(alert.severity) for alert in alerts)
    run_count, confirmed_count = _confirmation_runs(active, config)
    return AlertMetricsV1(
        source=source,
        emitted_alert_count=len(alerts),
        by_code=_value_counts(codes),
        by_severity=_value_counts(
            severities,
            (GuardrailSeverity.CAUTION.value, GuardrailSeverity.STOP.value),
        ),
        candidate_run_count=run_count,
        confirmed_candidate_run_count=confirmed_count,
        unconfirmed_candidate_run_count=run_count - confirmed_count,
        confirmation_observability_ratio=(
            _ratio(confirmed_count, run_count) if run_count else None
        ),
    )


def _build_metrics(
    snapshots: Sequence[MarketWatchSnapshotV1],
    session_dates: Sequence[date],
    alerts: Sequence[_AlertObservation],
    config: EvaluationConfigV1,
    *,
    explicit_alert_source: bool = False,
) -> EvaluationMetricsV1:
    active = _active_samples(snapshots)
    regime_runs = _state_runs(
        active, field="regime", continuity_gap_seconds=config.continuity_gap_seconds
    )
    severity_runs = _state_runs(
        active, field="severity", continuity_gap_seconds=config.continuity_gap_seconds
    )
    regime_transitions = _transition_metrics(regime_runs)
    severity_transitions = _transition_metrics(severity_runs)
    regime_reversals = _rapid_reversal_count(
        regime_runs, config.rapid_reversal_window_seconds
    )
    severity_reversals = _rapid_reversal_count(
        severity_runs, config.rapid_reversal_window_seconds
    )
    total_reversals = regime_reversals + severity_reversals
    total_transitions = (
        regime_transitions.transition_count + severity_transitions.transition_count
    )
    covered_hours = len(
        {
            (snapshot.market_state.trading_date, int(offset // 60))
            for snapshot, offset in active
        }
    ) / 60.0
    return EvaluationMetricsV1(
        coverage=_coverage_metrics(snapshots, session_dates, config),
        freshness=_freshness_metrics(active),
        regime_dwell=_dwell_metrics(active, field="regime"),
        severity_dwell=_dwell_metrics(active, field="severity"),
        regime_transitions=regime_transitions,
        severity_transitions=severity_transitions,
        alerts=_alert_metrics(
            snapshots,
            active,
            alerts,
            config,
            explicit_alert_source=explicit_alert_source,
        ),
        noise=NoiseMetricsV1(
            regime_rapid_reversal_count=regime_reversals,
            severity_rapid_reversal_count=severity_reversals,
            total_rapid_reversal_count=total_reversals,
            total_state_transition_count=total_transitions,
            rapid_reversal_ratio=_ratio(total_reversals, total_transitions),
            rapid_reversals_per_trading_hour=(
                total_reversals / covered_hours if covered_hours else 0.0
            ),
        ),
    )


def _acceptance(
    metrics: EvaluationMetricsV1,
    config: EvaluationConfigV1,
    *,
    session_count: int,
    require_multi_day: bool,
    child_verdicts: Sequence[EvaluationVerdict] = (),
) -> AcceptanceV1:
    coverage = metrics.coverage
    freshness = metrics.freshness
    noise = metrics.noise
    required_samples = config.minimum_samples_per_session * session_count
    stale_unavailable = freshness.stale_ratio + freshness.unavailable_ratio
    checks: list[AcceptanceCheckV1] = []

    def add(
        code: str, status: CheckStatus, observed: str, requirement: str
    ) -> None:
        checks.append(
            AcceptanceCheckV1(
                code=code,
                status=status,
                observed=observed,
                requirement=requirement,
            )
        )

    add(
        "sample_count",
        CheckStatus.PASSED
        if coverage.trading_sample_count >= required_samples
        else CheckStatus.INSUFFICIENT,
        str(coverage.trading_sample_count),
        f">={required_samples}",
    )
    add(
        "trading_minute_coverage",
        CheckStatus.PASSED
        if coverage.trading_minute_coverage_ratio
        >= config.minimum_trading_minute_coverage_ratio
        else CheckStatus.INSUFFICIENT,
        f"{coverage.trading_minute_coverage_ratio:.4f}",
        f">={config.minimum_trading_minute_coverage_ratio:.4f}",
    )
    add(
        "fresh_ratio",
        CheckStatus.PASSED
        if freshness.fresh_ratio >= config.minimum_fresh_ratio
        else CheckStatus.FAILED,
        f"{freshness.fresh_ratio:.4f}",
        f">={config.minimum_fresh_ratio:.4f}",
    )
    add(
        "degraded_ratio",
        CheckStatus.PASSED
        if freshness.degraded_ratio <= config.maximum_degraded_ratio
        else CheckStatus.FAILED,
        f"{freshness.degraded_ratio:.4f}",
        f"<={config.maximum_degraded_ratio:.4f}",
    )
    add(
        "stale_unavailable_ratio",
        CheckStatus.PASSED
        if stale_unavailable <= config.maximum_stale_unavailable_ratio
        else CheckStatus.FAILED,
        f"{stale_unavailable:.4f}",
        f"<={config.maximum_stale_unavailable_ratio:.4f}",
    )
    add(
        "longest_data_gap",
        CheckStatus.PASSED
        if coverage.longest_data_gap_seconds <= config.maximum_data_gap_seconds
        else CheckStatus.FAILED,
        f"{coverage.longest_data_gap_seconds:.1f}s",
        f"<={config.maximum_data_gap_seconds:.1f}s",
    )
    add(
        "rapid_reversal_ratio",
        CheckStatus.PASSED
        if noise.rapid_reversal_ratio <= config.maximum_rapid_reversal_ratio
        else CheckStatus.FAILED,
        f"{noise.rapid_reversal_ratio:.4f}",
        f"<={config.maximum_rapid_reversal_ratio:.4f}",
    )
    if metrics.alerts.candidate_run_count:
        add(
            "confirmation_observability",
            CheckStatus.PASSED
            if metrics.alerts.confirmed_candidate_run_count
            == metrics.alerts.candidate_run_count
            else CheckStatus.FAILED,
            (
                f"{metrics.alerts.confirmed_candidate_run_count}/"
                f"{metrics.alerts.candidate_run_count} candidate runs"
            ),
            f"each run has >={config.confirmation_samples} consecutive samples",
        )
    else:
        add(
            "confirmation_observability",
            CheckStatus.NOT_APPLICABLE,
            "no alert-bearing fresh state observed",
            "evaluate when a fresh caution/stop state occurs",
        )
    if require_multi_day:
        add(
            "multi_day_session_count",
            CheckStatus.PASSED
            if session_count >= config.minimum_sessions_for_multi_day
            else CheckStatus.INSUFFICIENT,
            str(session_count),
            f">={config.minimum_sessions_for_multi_day}",
        )
        if child_verdicts and any(
            verdict == EvaluationVerdict.FAILED for verdict in child_verdicts
        ):
            add(
                "session_consistency",
                CheckStatus.FAILED,
                "at least one session failed",
                "no failed complete session",
            )
        else:
            add(
                "session_consistency",
                CheckStatus.PASSED,
                "no failed complete session",
                "no failed complete session",
            )

    insufficient = tuple(
        item.code for item in checks if item.status == CheckStatus.INSUFFICIENT
    )
    failed = tuple(item.code for item in checks if item.status == CheckStatus.FAILED)
    if insufficient:
        verdict = EvaluationVerdict.INSUFFICIENT
        reasons = tuple(f"insufficient:{code}" for code in insufficient) + tuple(
            f"failed_observation:{code}" for code in failed
        )
    elif failed:
        verdict = EvaluationVerdict.FAILED
        reasons = tuple(f"failed:{code}" for code in failed)
    else:
        verdict = EvaluationVerdict.PASSED
        reasons = ("all_applicable_acceptance_checks_passed",)
    return AcceptanceV1(verdict=verdict, checks=tuple(checks), reasons=reasons)


def _calibration_hints(
    metrics: EvaluationMetricsV1,
    acceptance: AcceptanceV1,
    config: EvaluationConfigV1,
) -> tuple[CalibrationHintV1, ...]:
    hints: list[CalibrationHintV1] = []
    coverage = metrics.coverage
    freshness = metrics.freshness
    alerts = metrics.alerts
    noise = metrics.noise
    if acceptance.verdict == EvaluationVerdict.INSUFFICIENT:
        hints.append(
            CalibrationHintV1(
                code="collect_more_session_data",
                metric="trading_minute_coverage_ratio",
                observation=f"coverage={coverage.trading_minute_coverage_ratio:.4f}",
                action=(
                    "继续记录完整交易时段，达到当前报告所列样本数和交易分钟覆盖门槛后再作验收。"
                ),
            )
        )
    if (
        freshness.fresh_ratio < config.minimum_fresh_ratio
        or freshness.stale_ratio + freshness.unavailable_ratio
        > config.maximum_stale_unavailable_ratio
    ):
        hints.append(
            CalibrationHintV1(
                code="repair_data_quality_before_tuning",
                metric="freshness_ratios",
                observation=(
                    f"fresh={freshness.fresh_ratio:.4f}, "
                    f"stale_or_unavailable="
                    f"{freshness.stale_ratio + freshness.unavailable_ratio:.4f}"
                ),
                action=(
                    "先排查采集、刷新和缓存链路；数据质量恢复前不要通过放宽阈值消除告警。"
                ),
            )
        )
    if noise.rapid_reversal_ratio > config.maximum_rapid_reversal_ratio:
        proposed = min(config.confirmation_samples + 1, 10)
        hints.append(
            CalibrationHintV1(
                code="replay_longer_confirmation",
                metric="rapid_reversal_ratio",
                observation=f"ratio={noise.rapid_reversal_ratio:.4f}",
                action=(
                    f"复制当前历史样本离线比较 confirmation_samples="
                    f"{config.confirmation_samples} 与 {proposed}；至少覆盖"
                    f"{config.minimum_sessions_for_multi_day}个完整交易日后人工决定是否修改策略。"
                ),
            )
        )
    elif (
        alerts.candidate_run_count
        and alerts.unconfirmed_candidate_run_count > alerts.confirmed_candidate_run_count
    ):
        hints.append(
            CalibrationHintV1(
                code="review_short_candidate_runs",
                metric="unconfirmed_candidate_run_count",
                observation=(
                    f"unconfirmed={alerts.unconfirmed_candidate_run_count}, "
                    f"confirmed={alerts.confirmed_candidate_run_count}"
                ),
                action=(
                    "检查短候选状态是否集中在数据缺口附近；分别回放修复数据缺口前后结果，再人工评估连续确认参数。"
                ),
            )
        )
    if not hints:
        hints.append(
            CalibrationHintV1(
                code="retain_current_policy",
                metric="acceptance",
                observation=f"verdict={acceptance.verdict.value}",
                action=(
                    "保持当前阈值；累计新的完整交易日后重复同版本回放，仅在证据持续偏离时提出人工变更。"
                ),
            )
        )
    return tuple(hints)


def evaluate_market_watch_session(
    samples: Iterable[MarketWatchSnapshotV1 | Mapping[str, Any]],
    alerts: Iterable[AlertV1 | Mapping[str, Any]] = (),
    trade_date: date | str | None = None,
    *,
    config: EvaluationConfigV1 | Mapping[str, Any] | None = None,
) -> MarketWatchSessionEvaluationV1:
    """Evaluate one A-share trading session without any I/O.

    ``samples`` may be a complete history timeline when ``trade_date`` is
    supplied.  A non-empty ``alerts`` iterable is treated as the authoritative
    emitted-event stream; otherwise each snapshot's canonical ``alerts`` field
    is used.  A partial session is reported as ``insufficient``, never passed.
    """

    policy = _coerce_config(config)
    all_snapshots = _coerce_snapshots(samples)
    requested_date = _parse_date(trade_date) if trade_date is not None else None
    dates = tuple(sorted({item.market_state.trading_date for item in all_snapshots}))
    if requested_date is None:
        if not dates:
            raise ValueError("trade_date is required when samples are empty")
        if len(dates) != 1:
            raise ValueError(
                "session evaluation received multiple trading dates; pass trade_date "
                "or use evaluate_market_watch_history"
            )
        requested_date = dates[0]
    selected = tuple(
        item
        for item in all_snapshots
        if item.market_state.trading_date == requested_date
    )
    observations = tuple(_coerce_alert_observation(item) for item in alerts)
    selected_alerts = tuple(
        item
        for item in observations
        if item.trade_date is None or item.trade_date == requested_date
    )
    metrics = _build_metrics(
        selected,
        (requested_date,),
        selected_alerts,
        policy,
        explicit_alert_source=bool(observations),
    )
    acceptance = _acceptance(
        metrics, policy, session_count=1, require_multi_day=False
    )
    return MarketWatchSessionEvaluationV1(
        config_version=policy.config_version,
        trade_date=requested_date,
        config=policy,
        metrics=metrics,
        acceptance=acceptance,
        calibration_hints=_calibration_hints(metrics, acceptance, policy),
    )


def evaluate_market_watch_history(
    samples: Iterable[MarketWatchSnapshotV1 | Mapping[str, Any]],
    alerts: Iterable[AlertV1 | Mapping[str, Any]] = (),
    *,
    config: EvaluationConfigV1 | Mapping[str, Any] | None = None,
) -> MarketWatchMultiDayEvaluationV1:
    """Aggregate an ordered canonical timeline across one or more sessions."""

    policy = _coerce_config(config)
    snapshots = _coerce_snapshots(samples)
    if not snapshots:
        raise ValueError("multi-day evaluation requires at least one snapshot")
    dates = tuple(sorted({item.market_state.trading_date for item in snapshots}))
    observations = tuple(_coerce_alert_observation(item) for item in alerts)
    if observations and any(item.trade_date is None for item in observations):
        raise ValueError(
            "explicit alerts in multi-day evaluation require trade_date or an aware timestamp"
        )
    sessions: list[MarketWatchSessionEvaluationV1] = []
    for item_date in dates:
        day_samples = tuple(
            item for item in snapshots if item.market_state.trading_date == item_date
        )
        day_alerts = tuple(item for item in observations if item.trade_date == item_date)
        metrics = _build_metrics(
            day_samples,
            (item_date,),
            day_alerts,
            policy,
            explicit_alert_source=bool(observations),
        )
        acceptance = _acceptance(
            metrics, policy, session_count=1, require_multi_day=False
        )
        sessions.append(
            MarketWatchSessionEvaluationV1(
                config_version=policy.config_version,
                trade_date=item_date,
                config=policy,
                metrics=metrics,
                acceptance=acceptance,
                calibration_hints=_calibration_hints(metrics, acceptance, policy),
            )
        )
    metrics = _build_metrics(
        snapshots,
        dates,
        observations,
        policy,
        explicit_alert_source=bool(observations),
    )
    acceptance = _acceptance(
        metrics,
        policy,
        session_count=len(dates),
        require_multi_day=True,
        child_verdicts=tuple(item.acceptance.verdict for item in sessions),
    )
    return MarketWatchMultiDayEvaluationV1(
        config_version=policy.config_version,
        date_from=dates[0],
        date_to=dates[-1],
        session_count=len(dates),
        config=policy,
        metrics=metrics,
        acceptance=acceptance,
        session_verdicts=tuple(
            SessionVerdictV1(
                trade_date=item.trade_date,
                verdict=item.acceptance.verdict,
                sample_count=item.metrics.coverage.sample_count,
                trading_minute_coverage_ratio=(
                    item.metrics.coverage.trading_minute_coverage_ratio
                ),
                fresh_ratio=item.metrics.freshness.fresh_ratio,
                rapid_reversal_ratio=item.metrics.noise.rapid_reversal_ratio,
            )
            for item in sessions
        ),
        calibration_hints=_calibration_hints(metrics, acceptance, policy),
    )


__all__ = [
    "AcceptanceCheckV1",
    "AcceptanceV1",
    "AlertMetricsV1",
    "CalibrationHintV1",
    "CheckStatus",
    "CoverageMetricsV1",
    "DwellMetricV1",
    "EvaluationConfigV1",
    "EvaluationMetricsV1",
    "EvaluationVerdict",
    "FreshnessMetricsV1",
    "MarketWatchMultiDayEvaluationV1",
    "MarketWatchSessionEvaluationV1",
    "NoiseMetricsV1",
    "SessionVerdictV1",
    "TransitionMetricsV1",
    "TransitionV1",
    "ValueCountV1",
    "evaluate_market_watch_history",
    "evaluate_market_watch_session",
]
