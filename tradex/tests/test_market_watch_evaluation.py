from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from tradex.market_watch.contracts import AlertV1, MarketWatchSnapshotV1
from tradex.market_watch.evaluation import (
    EvaluationConfigV1,
    EvaluationVerdict,
    evaluate_market_watch_history,
    evaluate_market_watch_session,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _alert(code: str = "market_divergence", severity: str = "caution") -> AlertV1:
    return AlertV1(
        code=code,
        severity=severity,
        title=f"alert:{code}",
        message="仅用于市场级回放验收。",
        dedupe_key=f"market_watch:{code}",
    )


def _snapshot(
    observed_at: datetime,
    sequence: int,
    *,
    regime: str = "attack",
    severity: str = "calm",
    freshness: str = "fresh",
    alerts: tuple[AlertV1, ...] = (),
) -> MarketWatchSnapshotV1:
    if freshness == "fresh":
        component_statuses = ("fresh", "fresh", "fresh", "fresh")
        component_qualities = ("accepted", "accepted", "accepted", "accepted")
    elif freshness == "degraded":
        component_statuses = ("fresh", "degraded", "fresh", "fresh")
        component_qualities = ("accepted", "degraded", "accepted", "accepted")
        severity = "caution"
    elif freshness == "stale":
        component_statuses = ("stale", "fresh", "fresh", "fresh")
        component_qualities = ("degraded", "accepted", "accepted", "accepted")
        regime, severity = "uncertain", "stop"
    else:
        component_statuses = ("unavailable",) * 4
        component_qualities = ("unavailable",) * 4
        regime, severity = "uncertain", "stop"

    strength = (
        "abstain"
        if freshness in {"stale", "unavailable"}
        else "weak"
        if freshness == "degraded"
        else "moderate"
    )
    components = [
        {
            "component": component,
            "status": status,
            "quality": quality,
            "fetched_at": observed_at.isoformat()
            if status != "unavailable"
            else None,
        }
        for component, status, quality in zip(
            ("indices", "breadth", "turnover", "rotation"),
            component_statuses,
            component_qualities,
            strict=True,
        )
    ]
    roles = (
        ("broad_market", "000001.SH", "上证指数"),
        ("large_cap", "000300.SH", "沪深300"),
        ("small_cap", "000852.SH", "中证1000"),
        ("growth", "399006.SZ", "创业板指"),
    )
    payload = {
        "snapshot_id": f"snapshot-{observed_at.date()}-{sequence}",
        "sequence": sequence,
        "as_of": observed_at.isoformat(),
        "market_state": {
            "phase": "trading",
            "is_open": True,
            "trading_date": observed_at.date().isoformat(),
        },
        "freshness": {
            "status": freshness,
            "components": components,
        },
        "guardrail": {
            "regime": regime,
            "severity": severity,
            "conclusion_strength": strength,
            "current_state": f"state:{regime}",
            "supporting_evidence": [],
            "counter_evidence": [],
            "behavioral_constraint": "只观察市场结构并保持纪律。",
        },
        "indices": [
            {
                "role": role,
                "instrument_id": instrument,
                "name": name,
                "available": True,
                "level": 100.0,
                "change_pct": 0.5,
                "provider_as_of": observed_at.isoformat(),
                "quality": "accepted",
            }
            for role, instrument, name in roles
        ],
        "breadth": {
            "available": True,
            "up_count": 3000,
            "down_count": 2000,
            "flat_count": 0,
            "unclassified_count": 0,
            "total_count": 5000,
            "advance_ratio": 0.6,
            "provider_as_of": observed_at.isoformat(),
            "quality": "accepted",
        },
        "turnover": {
            "available": True,
            "today_date": observed_at.date().isoformat(),
            "previous_date": (observed_at.date() - timedelta(days=1)).isoformat(),
            "as_of": observed_at.strftime("%H:%M"),
            "today_amount_cny": 105.0,
            "previous_same_time_amount_cny": 100.0,
            "difference_cny": 5.0,
            "difference_ratio": 0.05,
            "neutral_band_ratio": 0.03,
            "direction": "expand",
        },
        "rotation": {
            "regime": regime if regime != "uncertain" else "uncertain",
            "sectors": [],
            "leading_tags": [],
            "summary": f"rotation:{regime}",
        },
        "scenarios": [],
        "change": {
            "available": False,
            "reason": "no_confirmed_change_evidence",
        },
        "alerts": [item.model_dump(mode="json") for item in alerts],
    }
    return MarketWatchSnapshotV1.model_validate(payload)


def _session_times(trade_date: date) -> tuple[datetime, ...]:
    morning = datetime.combine(trade_date, datetime.min.time(), SHANGHAI).replace(
        hour=9, minute=30
    )
    afternoon = morning.replace(hour=13, minute=0)
    return tuple(morning + timedelta(minutes=index) for index in range(120)) + tuple(
        afternoon + timedelta(minutes=index) for index in range(120)
    )


def _complete_session(
    trade_date: date,
    *,
    start_sequence: int = 1,
    stale_every: int | None = None,
) -> tuple[MarketWatchSnapshotV1, ...]:
    return tuple(
        _snapshot(
            observed_at,
            start_sequence + index,
            freshness=(
                "stale"
                if stale_every and (index + 1) % stale_every == 0
                else "fresh"
            ),
        )
        for index, observed_at in enumerate(_session_times(trade_date))
    )


def _metric_value(items, value: str):
    return next(item for item in items if item.value == value)


def test_complete_fresh_session_passes_and_reports_both_coverage_units() -> None:
    samples = _complete_session(date(2026, 8, 24))

    report = evaluate_market_watch_session(samples)

    assert report.contract == "market_watch_evaluation.v1"
    assert report.schema_version == 1
    assert report.config_version == "market-watch-policy.v1"
    assert report.acceptance.verdict == EvaluationVerdict.PASSED
    assert report.metrics.coverage.sample_count == 240
    assert report.metrics.coverage.expected_sample_count == 960
    assert report.metrics.coverage.sample_coverage_ratio == pytest.approx(0.25)
    assert report.metrics.coverage.covered_trading_minutes == 240
    assert report.metrics.coverage.trading_minute_coverage_ratio == 1.0
    assert report.metrics.coverage.longest_data_gap_seconds == 60.0
    assert report.metrics.freshness.fresh_ratio == 1.0
    attack = _metric_value(report.metrics.regime_dwell, "attack")
    assert attack.covered_trading_minutes == 240
    assert attack.covered_minute_ratio == 1.0
    confirmation = next(
        item
        for item in report.acceptance.checks
        if item.code == "confirmation_observability"
    )
    assert confirmation.status.value == "not_applicable"


def test_partial_session_is_honestly_insufficient_and_never_passes() -> None:
    samples = _complete_session(date(2026, 8, 24))[:30]

    report = evaluate_market_watch_session(samples)

    assert report.acceptance.verdict == EvaluationVerdict.INSUFFICIENT
    assert report.metrics.coverage.trading_minute_coverage_ratio == pytest.approx(
        30 / 240
    )
    assert "insufficient:trading_minute_coverage" in report.acceptance.reasons
    assert report.calibration_hints[0].code == "collect_more_session_data"
    assert report.calibration_hints[0].auto_apply is False
    assert report.calibration_hints[0].requires_manual_review is True


def test_complete_session_with_stale_samples_fails_quality_acceptance() -> None:
    samples = _complete_session(date(2026, 8, 24), stale_every=20)

    report = evaluate_market_watch_session(samples)

    assert report.acceptance.verdict == EvaluationVerdict.FAILED
    assert report.metrics.freshness.stale_ratio == pytest.approx(0.05)
    assert _metric_value(report.metrics.freshness.by_status, "stale").count == 12
    assert any(
        reason == "failed:stale_unavailable_ratio"
        for reason in report.acceptance.reasons
    )
    assert any(
        hint.code == "repair_data_quality_before_tuning"
        for hint in report.calibration_hints
    )


def test_alert_confirmation_and_rapid_reversal_are_observable_proxies() -> None:
    start = datetime(2026, 8, 24, 9, 30, tzinfo=SHANGHAI)
    states = (
        ("defense", "caution"),
        ("defense", "caution"),
        ("mixed", "caution"),
        ("defense", "caution"),
        ("defense", "caution"),
        ("attack", "calm"),
    )
    samples = tuple(
        _snapshot(
            start + timedelta(minutes=index),
            index + 1,
            regime=regime,
            severity=severity,
            alerts=(_alert("defense_dominant"),) if index == 1 else (),
        )
        for index, (regime, severity) in enumerate(states)
    )

    report = evaluate_market_watch_session(samples)

    assert report.metrics.alerts.source == "snapshot"
    assert report.metrics.alerts.emitted_alert_count == 1
    assert _metric_value(
        report.metrics.alerts.by_code, "defense_dominant"
    ).count == 1
    assert report.metrics.alerts.candidate_run_count == 3
    assert report.metrics.alerts.confirmed_candidate_run_count == 2
    assert report.metrics.alerts.unconfirmed_candidate_run_count == 1
    assert report.metrics.alerts.confirmation_observability_ratio == pytest.approx(
        2 / 3
    )
    assert report.metrics.regime_transitions.transition_count == 3
    assert report.metrics.noise.regime_rapid_reversal_count == 1
    assert report.metrics.noise.total_rapid_reversal_count == 1
    # The denominator includes three regime transitions plus the final
    # caution->calm severity transition.
    assert report.metrics.noise.total_state_transition_count == 4
    assert report.metrics.noise.rapid_reversal_ratio == pytest.approx(1 / 4)


def test_explicit_alert_stream_overrides_embedded_alert_events() -> None:
    first = datetime(2026, 8, 24, 9, 30, tzinfo=SHANGHAI)
    samples = (
        _snapshot(first, 1, alerts=(_alert("market_caution"),)),
        _snapshot(first + timedelta(minutes=1), 2),
    )
    explicit = {
        "alert": _alert("data_stale", "stop").model_dump(mode="json"),
        "emitted_at": (first + timedelta(seconds=10)).isoformat(),
    }

    report = evaluate_market_watch_session(samples, alerts=(explicit,))

    assert report.metrics.alerts.source == "explicit"
    assert report.metrics.alerts.emitted_alert_count == 1
    assert tuple(item.value for item in report.metrics.alerts.by_code) == (
        "data_stale",
    )

    leaked = {**explicit, "alert": {**explicit["alert"], "provider": "raw"}}
    with pytest.raises(ValidationError):
        evaluate_market_watch_session(samples, alerts=(leaked,))


def test_multi_day_summary_aggregates_sessions_and_requires_enough_days() -> None:
    first = _complete_session(date(2026, 8, 24), start_sequence=1)
    second = _complete_session(date(2026, 8, 25), start_sequence=241)
    samples = first + second

    default_report = evaluate_market_watch_history(samples)
    assert default_report.session_count == 2
    assert default_report.metrics.coverage.expected_trading_minutes == 480
    assert default_report.metrics.coverage.covered_trading_minutes == 480
    assert default_report.acceptance.verdict == EvaluationVerdict.INSUFFICIENT
    assert len(default_report.session_verdicts) == 2
    assert all(
        item.verdict == EvaluationVerdict.PASSED
        for item in default_report.session_verdicts
    )

    two_day_policy = EvaluationConfigV1(minimum_sessions_for_multi_day=2)
    accepted = evaluate_market_watch_history(samples, config=two_day_policy)
    assert accepted.acceptance.verdict == EvaluationVerdict.PASSED


def test_multi_day_explicit_alerts_must_be_datable_and_are_grouped() -> None:
    first = _complete_session(date(2026, 8, 24), start_sequence=1)
    second = _complete_session(date(2026, 8, 25), start_sequence=241)
    undated = _alert("market_caution").model_dump(mode="json")

    with pytest.raises(ValueError, match="require trade_date"):
        evaluate_market_watch_history(first + second, alerts=(undated,))

    dated = {
        "alert": undated,
        "trade_date": "2026-08-25",
    }
    report = evaluate_market_watch_history(first + second, alerts=(dated,))
    assert report.metrics.alerts.source == "explicit"
    assert report.metrics.alerts.emitted_alert_count == 1


def test_session_can_filter_one_date_from_a_complete_history_timeline() -> None:
    first = _complete_session(date(2026, 8, 24), start_sequence=1)
    second = _complete_session(date(2026, 8, 25), start_sequence=241)

    report = evaluate_market_watch_session(
        first + second,
        trade_date="2026-08-25",
    )

    assert report.trade_date == date(2026, 8, 25)
    assert report.metrics.coverage.sample_count == 240
    with pytest.raises(ValueError, match="multiple trading dates"):
        evaluate_market_watch_session(first + second)


def test_empty_requested_session_is_insufficient_and_json_safe() -> None:
    report = evaluate_market_watch_session([], trade_date="2026-08-24")

    assert report.acceptance.verdict == EvaluationVerdict.INSUFFICIENT
    assert report.metrics.coverage.sample_count == 0
    assert report.metrics.coverage.longest_data_gap_seconds == 14_400.0
    rendered = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
    assert "market_watch_evaluation.v1" in rendered
    assert "NaN" not in rendered
    assert "Infinity" not in rendered
    assert all(
        forbidden not in rendered
        for forbidden in ("收益预测", "目标价", "买入", "卖出", "加仓", "减仓")
    )


def test_rejects_unsorted_duplicate_or_noncanonical_samples() -> None:
    first = _snapshot(datetime(2026, 8, 24, 9, 30, tzinfo=SHANGHAI), 1)
    second = _snapshot(datetime(2026, 8, 24, 9, 31, tzinfo=SHANGHAI), 2)

    with pytest.raises(ValueError, match="strictly ordered"):
        evaluate_market_watch_session((second, first))
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_market_watch_session((first, first))

    invalid = first.model_dump(mode="json")
    invalid["provider"] = "must-not-leak"
    with pytest.raises(ValidationError):
        evaluate_market_watch_session((invalid,))
