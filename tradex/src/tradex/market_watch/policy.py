"""Shared immutable runtime and replay policy for ``market_watch.v1``.

Changing any live confirmation or acceptance default requires a new
``config_version``.  History, alerting, refresh cadence and replay evaluation
all import the same object so a stored session cannot silently claim a policy
version whose thresholds differ from the live runtime.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class MarketWatchPolicyV1:
    config_version: str = "market-watch-policy.v1"
    trading_refresh_interval_seconds: float = 15.0
    off_session_refresh_interval_seconds: float = 60.0
    force_min_interval_seconds: float = 2.0
    failure_retry_seconds: float = 5.0
    alert_confirmation_samples: int = 2
    alert_cooldown_seconds: float = 300.0
    history_retention_trade_days: int = 250
    continuity_gap_seconds: float = 90.0
    rapid_reversal_window_seconds: float = 600.0
    minimum_samples_per_session: int = 120
    minimum_trading_minute_coverage_ratio: float = 0.90
    minimum_fresh_ratio: float = 0.95
    maximum_degraded_ratio: float = 0.05
    maximum_stale_unavailable_ratio: float = 0.02
    maximum_data_gap_seconds: float = 180.0
    maximum_rapid_reversal_ratio: float = 0.20
    minimum_sessions_for_multi_day: int = 3
    contract: str = field(default="market_watch_policy.v1", init=False)
    schema_version: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        if not self.config_version.strip():
            raise ValueError("config_version must not be empty")
        positive = (
            self.trading_refresh_interval_seconds,
            self.off_session_refresh_interval_seconds,
            self.force_min_interval_seconds,
            self.failure_retry_seconds,
            self.alert_cooldown_seconds,
            self.continuity_gap_seconds,
            self.rapid_reversal_window_seconds,
            self.maximum_data_gap_seconds,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("policy durations must be positive")
        if self.alert_confirmation_samples < 1:
            raise ValueError("alert_confirmation_samples must be positive")
        if self.history_retention_trade_days < 1:
            raise ValueError("history_retention_trade_days must be positive")
        if self.minimum_samples_per_session < 1:
            raise ValueError("minimum_samples_per_session must be positive")
        if self.minimum_sessions_for_multi_day < 1:
            raise ValueError("minimum_sessions_for_multi_day must be positive")
        ratios = (
            self.minimum_trading_minute_coverage_ratio,
            self.minimum_fresh_ratio,
            self.maximum_degraded_ratio,
            self.maximum_stale_unavailable_ratio,
            self.maximum_rapid_reversal_ratio,
        )
        if any(not 0 <= value <= 1 for value in ratios):
            raise ValueError("policy ratios must be between zero and one")
        if self.continuity_gap_seconds < self.trading_refresh_interval_seconds:
            raise ValueError("continuity gap cannot be shorter than refresh interval")
        if self.maximum_data_gap_seconds < self.continuity_gap_seconds:
            raise ValueError("maximum data gap cannot be shorter than continuity gap")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_MARKET_WATCH_POLICY = MarketWatchPolicyV1()


__all__ = ["DEFAULT_MARKET_WATCH_POLICY", "MarketWatchPolicyV1"]
