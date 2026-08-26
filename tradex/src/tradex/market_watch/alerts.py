"""Thread-safe alert confirmation and deduplication for ``market_watch.v1``.

The domain builder supplies *candidate* alerts on every snapshot.  This module
turns those candidates into notification events without knowing anything about
providers or dashboard delivery.  Market conclusions need two consecutive
fresh samples; data-freshness failures are surfaced immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Any, Callable, Mapping

from .policy import DEFAULT_MARKET_WATCH_POLICY


@dataclass(frozen=True)
class StructuredAlert:
    """Fallback alert shape used by tests and during partial package imports.

    At runtime the engine lazily constructs ``contracts.AlertV1`` when that
    class is available.  Keeping the same five fields also makes the engine
    usable with simple fixtures.
    """

    code: str
    severity: str
    title: str
    message: str
    dedupe_key: str
    kind: str = "market_state"


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _enum_value(value: Any) -> str:
    if isinstance(value, Enum):
        value = value.value
    return str(value).strip().lower()


def freshness_status(snapshot: Any) -> str:
    """Return a normalized snapshot freshness status using duck typing."""

    freshness = _field(snapshot, "freshness")
    return _enum_value(_field(freshness, "status", "unavailable"))


def _candidate_identity(candidate: Any) -> tuple[str, tuple[str, str]] | None:
    key = str(_field(candidate, "dedupe_key", "")).strip()
    code = str(_field(candidate, "code", "")).strip()
    severity = _enum_value(_field(candidate, "severity", "caution"))
    if not key or not code:
        return None
    # Titles and messages may contain changing market values.  The stable key,
    # state code and severity are the semantic state that must be confirmed.
    return key, (code, severity)


def _risk_alert(code: str) -> Any:
    if code == "data_unavailable":
        values = {
            "kind": "data_quality",
            "code": code,
            "severity": "stop",
            "title": "盘面数据暂不可用",
            "message": "数据不可依赖，请暂停追单并重新核对盘面。",
            "dedupe_key": "market_watch:data_unavailable",
        }
    elif code == "data_degraded":
        values = {
            "kind": "data_quality",
            "code": code,
            "severity": "caution",
            "title": "盘面数据部分降级",
            "message": "部分证据暂不完整，请降低结论强度并避免依据单一信号行动。",
            "dedupe_key": "market_watch:data_degraded",
        }
    else:
        values = {
            "kind": "data_quality",
            "code": "data_stale",
            "severity": "stop",
            "title": "盘面数据已陈旧",
            "message": "当前结论可能已过期，请暂停追单并重新核对盘面。",
            "dedupe_key": "market_watch:data_stale",
        }

    # Avoid a hard import cycle while contracts and runtime are imported.  A
    # production snapshot receives its own strict AlertV1 instance.
    try:
        from .contracts import AlertV1

        return AlertV1(**values)
    except (ImportError, AttributeError):
        return StructuredAlert(**values)


class MarketAlertEngine:
    """Confirm candidate state changes and emit deduplicated alerts.

    ``evaluate`` is safe to call from concurrent request threads.  The engine
    intentionally keeps only process-local notification state; the owning
    :class:`MarketWatchService` is the sole lifecycle owner.
    """

    def __init__(
        self,
        *,
        confirmation_samples: int = (
            DEFAULT_MARKET_WATCH_POLICY.alert_confirmation_samples
        ),
        cooldown_seconds: float = DEFAULT_MARKET_WATCH_POLICY.alert_cooldown_seconds,
        clock: Callable[[], datetime] | None = None,
        muted: bool = False,
    ) -> None:
        if confirmation_samples < 1:
            raise ValueError("confirmation_samples must be at least one")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative")
        self.confirmation_samples = confirmation_samples
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._muted = bool(muted)
        self._pending: dict[str, tuple[tuple[str, str], int, Any]] = {}
        self._confirmed: dict[str, tuple[str, str]] = {}
        self._missing_samples: dict[str, int] = {}
        self._last_emitted_at: dict[str, float] = {}
        self._lock = RLock()

    @property
    def muted(self) -> bool:
        with self._lock:
            return self._muted

    def set_muted(self, muted: bool) -> None:
        with self._lock:
            self._muted = bool(muted)

    def reset(self) -> None:
        """Forget confirmation and cooldown state (primarily for lifecycle use)."""

        with self._lock:
            self._pending.clear()
            self._confirmed.clear()
            self._missing_samples.clear()
            self._last_emitted_at.clear()

    def evaluate(self, snapshot: Any, *, now: datetime | None = None) -> tuple[Any, ...]:
        """Return alert events for one snapshot.

        Market-state candidates require globally fresh samples.  Sector-move
        candidates may still confirm on a globally degraded snapshot because
        their own canonical trajectory has already passed its feature-level
        quality gate.  Stale or unavailable snapshots interrupt everything.
        """

        observed_at = now or self._clock()
        timestamp = _timestamp(observed_at)
        status = freshness_status(snapshot)

        with self._lock:
            if status in {"stale", "unavailable"}:
                self._pending.clear()
                self._confirmed.clear()
                self._missing_samples.clear()
                risk_code = (
                    "data_unavailable"
                    if status == "unavailable"
                    else "data_stale"
                )
                alert = _risk_alert(risk_code)
                if self._muted or not self._cooldown_allows(
                    _field(alert, "dedupe_key"), timestamp
                ):
                    return ()
                self._record_emission(_field(alert, "dedupe_key"), timestamp)
                return (alert,)

            snapshot_candidates = tuple(_field(snapshot, "alerts", ()) or ())
            candidates = (
                tuple(
                    candidate
                    for candidate in snapshot_candidates
                    if _enum_value(_field(candidate, "kind", "")) == "sector_move"
                )
                if status == "degraded"
                else snapshot_candidates
            )
            active_keys: set[str] = set()
            emitted: list[Any] = []

            if status == "degraded":
                for key in tuple(self._pending):
                    if not key.startswith("market_watch:sector_move:"):
                        self._pending.pop(key, None)
                for key in tuple(self._confirmed):
                    if not key.startswith("market_watch:sector_move:"):
                        self._confirmed.pop(key, None)
                        self._missing_samples.pop(key, None)
                risk_alert = _risk_alert("data_degraded")
                risk_key = _field(risk_alert, "dedupe_key")
                if not self._muted and self._cooldown_allows(risk_key, timestamp):
                    self._record_emission(risk_key, timestamp)
                    emitted.append(risk_alert)

            for candidate in candidates:
                identity = _candidate_identity(candidate)
                if identity is None:
                    continue
                key, state = identity
                active_keys.add(key)
                self._missing_samples.pop(key, None)

                if self._confirmed.get(key) == state:
                    self._pending.pop(key, None)
                    continue

                pending = self._pending.get(key)
                count = pending[1] + 1 if pending and pending[0] == state else 1
                self._pending[key] = (state, count, candidate)
                if count < self.confirmation_samples:
                    continue

                # Confirm even while muted or inside cooldown.  This prevents a
                # delayed notification about a state that changed in the past.
                self._confirmed[key] = state
                self._pending.pop(key, None)
                if self._muted or not self._cooldown_allows(key, timestamp):
                    continue
                self._record_emission(key, timestamp)
                emitted.append(candidate)

            self._clear_resolved_candidates(active_keys)
            return tuple(emitted)

    def _clear_resolved_candidates(self, active_keys: set[str]) -> None:
        # Two consecutive fresh absences establish that an active condition has
        # resolved.  A later re-entry can then be confirmed and alerted again.
        for key in tuple(self._confirmed):
            if key in active_keys:
                continue
            count = self._missing_samples.get(key, 0) + 1
            if count >= self.confirmation_samples:
                self._confirmed.pop(key, None)
                self._missing_samples.pop(key, None)
            else:
                self._missing_samples[key] = count

        for key in tuple(self._pending):
            if key not in active_keys:
                self._pending.pop(key, None)

    def _cooldown_allows(self, key: str, timestamp: float) -> bool:
        previous = self._last_emitted_at.get(key)
        return previous is None or timestamp - previous >= self.cooldown_seconds

    def _record_emission(self, key: str, timestamp: float) -> None:
        self._last_emitted_at[key] = timestamp


def _timestamp(value: datetime) -> float:
    if not isinstance(value, datetime):
        raise TypeError("alert clock must return datetime")
    if value.tzinfo is None:
        # Unit-test clocks occasionally use a naive value.  Treating it as UTC
        # is deterministic while production contracts still require timezone.
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


__all__ = ["MarketAlertEngine", "StructuredAlert", "freshness_status"]
