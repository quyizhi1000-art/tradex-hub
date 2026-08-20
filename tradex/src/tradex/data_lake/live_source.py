"""Adapter from the existing dashboard aggregation into durable lake contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Mapping

from .contracts import CaptureBundle, CapturedDataset


class LiveCaptureError(RuntimeError):
    """Raised when a live calculation cannot expose the inputs it actually used."""


@dataclass(frozen=True)
class LiveCaptureResult:
    bundle: CaptureBundle
    feature_payload: Mapping[str, Any]


def _records(value: Any) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return (value,)
    if isinstance(value, (str, bytes)):
        raise LiveCaptureError("captured component records must be mappings, not text")
    try:
        rows = tuple(value)
    except TypeError as exc:
        raise LiveCaptureError("captured component records must be iterable") from exc
    if any(not isinstance(row, Mapping) for row in rows):
        raise LiveCaptureError("captured component contains a non-mapping record")
    return rows


def _provider_as_of(status: Mapping[str, Any]) -> str | None:
    for key in ("provider_as_of", "data_date", "fetched_at", "as_of"):
        value = status.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


class TradexLiveSource:
    """Capture one risk calculation without introducing a second network sweep."""

    def __init__(
        self,
        *,
        market_fetcher: Callable[..., Mapping[str, Any]] | None = None,
        risk_fetcher: Callable[..., Mapping[str, Any]] | None = None,
    ) -> None:
        self._market_fetcher = market_fetcher
        self._risk_fetcher = risk_fetcher

    def _resolve_fetchers(self) -> tuple[Callable[..., Any], Callable[..., Any]]:
        market_fetcher = self._market_fetcher
        risk_fetcher = self._risk_fetcher
        if market_fetcher is None:
            from tradex.dashboard.__main__ import get_market_data

            market_fetcher = get_market_data
        if risk_fetcher is None:
            from tradex.dashboard.risk_service import get_risk_appetite_data

            risk_fetcher = get_risk_appetite_data
        return market_fetcher, risk_fetcher

    def capture(self, capture_id: str) -> LiveCaptureResult:
        market_fetcher, risk_fetcher = self._resolve_fetchers()
        market_data = dict(market_fetcher(force=True) or {})
        observed: dict[str, Any] = {}

        def observe(payload: dict[str, Any]) -> None:
            if observed:
                raise LiveCaptureError("risk calculation invoked capture observer more than once")
            observed.update(payload)

        feature_payload = dict(
            risk_fetcher(
                market_data,
                force=True,
                record_trajectory=True,
                capture_observer=observe,
            )
            or {}
        )
        if not observed:
            raise LiveCaptureError(
                "risk calculation did not expose its inputs; ensure the capture_observer hook is installed"
            )

        observed_at = observed.get("observed_at")
        minute_bucket = observed.get("minute_bucket")
        if not isinstance(observed_at, datetime) or not isinstance(minute_bucket, datetime):
            raise LiveCaptureError("capture observer returned invalid timestamps")
        raw_trade_date = observed.get("trade_date")
        trade_date = raw_trade_date if isinstance(raw_trade_date, date) else date.fromisoformat(
            str(raw_trade_date)
        )
        values = observed.get("values") or {}
        statuses = observed.get("statuses") or {}
        if not isinstance(values, Mapping) or not isinstance(statuses, Mapping):
            raise LiveCaptureError("capture observer returned invalid values/statuses")

        datasets: dict[str, CapturedDataset] = {}
        for name, value in values.items():
            status = statuses.get(name) or {}
            if not isinstance(status, Mapping):
                status = {"error": "invalid component status", "raw_status": status}
            datasets[str(name)] = CapturedDataset(
                name=str(name),
                records=_records(value),
                source=str(status.get("source")) if status.get("source") else None,
                provider_as_of=_provider_as_of(status),
                status=dict(status),
            )

        market_status = {
            "status": "ready" if market_data else "missing",
            "source": market_data.get("source"),
            "provider_as_of": market_data.get("provider_as_of"),
        }
        datasets["market_overview"] = CapturedDataset(
            name="market_overview",
            records=(market_data,) if market_data else (),
            source=str(market_data.get("source")) if market_data.get("source") else None,
            provider_as_of=(
                str(market_data.get("provider_as_of"))
                if market_data.get("provider_as_of")
                else None
            ),
            status=market_status,
        )

        bundle = CaptureBundle(
            capture_id=capture_id,
            observed_at=observed_at,
            trade_date=trade_date,
            minute_bucket=minute_bucket,
            market_phase=str(observed.get("market_phase") or "unknown"),
            market_data=dict(observed.get("market_data") or market_data),
            datasets=datasets,
        )
        return LiveCaptureResult(bundle=bundle, feature_payload=feature_payload)


__all__ = ["LiveCaptureError", "LiveCaptureResult", "TradexLiveSource"]
