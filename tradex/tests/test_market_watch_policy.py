from __future__ import annotations

from tradex.market_watch.alerts import MarketAlertEngine
from tradex.market_watch.evaluation import EvaluationConfigV1
from tradex.market_watch.history import (
    DEFAULT_CONFIG_VERSION,
    DEFAULT_RETENTION_TRADE_DAYS,
)
from tradex.market_watch.policy import DEFAULT_MARKET_WATCH_POLICY
from tradex.market_watch.service import MarketWatchService


def test_live_history_and_replay_share_one_versioned_policy() -> None:
    policy = DEFAULT_MARKET_WATCH_POLICY
    alerts = MarketAlertEngine()
    service = MarketWatchService(lambda: {}, lambda value: value)
    evaluation = EvaluationConfigV1()

    assert policy.contract == "market_watch_policy.v1"
    assert policy.schema_version == 1
    assert DEFAULT_CONFIG_VERSION == policy.config_version
    assert DEFAULT_RETENTION_TRADE_DAYS == policy.history_retention_trade_days
    assert alerts.confirmation_samples == policy.alert_confirmation_samples
    assert alerts.cooldown_seconds == policy.alert_cooldown_seconds
    assert service.trading_ttl_seconds == policy.trading_refresh_interval_seconds
    assert service.off_session_ttl_seconds == policy.off_session_refresh_interval_seconds
    assert service.force_min_interval_seconds == policy.force_min_interval_seconds
    assert service.failure_retry_seconds == policy.failure_retry_seconds
    assert evaluation.config_version == policy.config_version
    assert evaluation.expected_interval_seconds == policy.trading_refresh_interval_seconds
    assert evaluation.confirmation_samples == policy.alert_confirmation_samples
    assert evaluation.rapid_reversal_window_seconds == (
        policy.rapid_reversal_window_seconds
    )


def test_policy_serialization_is_explicit_and_immutable() -> None:
    payload = DEFAULT_MARKET_WATCH_POLICY.as_dict()

    assert payload["config_version"] == "market-watch-policy.v1"
    assert payload["alert_confirmation_samples"] == 2
    assert payload["alert_cooldown_seconds"] == 300.0
