"""Point-in-time-safe proxy outcome labels for completed lake decisions."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Mapping

from .contracts import SHANGHAI
from .serde import sha256_hex, to_jsonable


LABEL_NAME = "proxy_next_open_close"
_BENCHMARK_DATASETS = frozenset({"daily_bars", "index_bars"})


class OutcomeLabelDataError(RuntimeError):
    """Raised when local CNEquity data cannot define an unambiguous label."""


def _as_date(value: str | date, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO date") from exc
    raise TypeError(f"{field_name} must be a date or ISO date string")


def _positive_price(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return price


def _bar_index(
    rows: list[dict[str, Any]],
    symbols: set[str],
    start: date,
    end: date,
    *,
    dataset: str,
) -> dict[str, dict[date, dict[str, Any]]]:
    """Index canonical, audit-ready rows without choosing between duplicates."""

    indexed: dict[str, dict[date, dict[str, Any]]] = {
        symbol: {} for symbol in symbols
    }
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise OutcomeLabelDataError(f"{dataset} row {index} is not a mapping")
        symbol = row.get("symbol")
        if symbol not in symbols:
            continue
        raw_day = row.get("trade_date")
        try:
            session = _as_date(raw_day, f"{dataset}[{index}].trade_date")
        except (TypeError, ValueError) as exc:
            raise OutcomeLabelDataError(str(exc)) from exc
        if session < start or session > end:
            continue
        entry = _positive_price(row.get("open"))
        exit_price = _positive_price(row.get("close"))
        # A row without two executable prices is not available for this
        # instrument. It must never become a zero/NaN return label.
        if entry is None or exit_price is None:
            continue
        canonical_row = to_jsonable(dict(row))
        if not isinstance(canonical_row, dict):  # pragma: no cover - defensive
            raise OutcomeLabelDataError(f"{dataset} row {index} is not canonicalizable")
        evidence = {
            "dataset": dataset,
            "symbol": str(symbol),
            "trade_date": session.isoformat(),
            "open": entry,
            "close": exit_price,
            "row": canonical_row,
            "row_sha256": sha256_hex(canonical_row),
        }
        prior = indexed[str(symbol)].get(session)
        if prior is not None and prior["row_sha256"] != evidence["row_sha256"]:
            raise OutcomeLabelDataError(
                f"{dataset} has conflicting rows for {symbol} on {session.isoformat()}"
            )
        indexed[str(symbol)][session] = evidence
    return indexed


def _session_at(session: date, value: time) -> str:
    return datetime.combine(session, value, tzinfo=SHANGHAI).isoformat()


def _generation_description(bridge: Any, dataset: str) -> dict[str, Any]:
    description = bridge.describe_generation(dataset)
    if not isinstance(description, Mapping):
        raise OutcomeLabelDataError(
            f"CNEquity {dataset} generation description must be a mapping"
        )
    generation = description.get("generation_sha256")
    if not isinstance(generation, str) or not generation.strip():
        raise OutcomeLabelDataError(
            f"CNEquity {dataset} generation has no generation_sha256"
        )
    return dict(description)


def _stable_read(
    bridge: Any,
    dataset: str,
    reader: Callable[[], Any],
) -> tuple[Any, dict[str, Any]]:
    """Read one dataset only while its metadata generation stays fixed."""

    before = _generation_description(bridge, dataset)
    value = reader()
    after = _generation_description(bridge, dataset)
    if before["generation_sha256"] != after["generation_sha256"]:
        raise OutcomeLabelDataError(
            f"CNEquity {dataset} changed while outcome inputs were read; "
            "no labels were written and the decisions remain pending"
        )
    return value, after


def _generation_detail(description: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: description.get(key)
        for key in (
            "dataset",
            "generation_sha256",
            "coverage",
            "file_count",
            "total_size",
        )
        if key in description
    }


class ProxyOutcomeLabeler:
    """Label decisions with a next-calendar-session proxy open/close return.

    This is deliberately a proxy-only outcome. It neither reconstructs nor
    claims the return of the offense/defense/cash weights in the decision.
    """

    label_name = LABEL_NAME

    def __init__(
        self,
        catalog: Any,
        bridge: Any,
        proxy_symbol: str = "510300.SH",
        benchmark_symbol: str | None = None,
        *,
        benchmark_dataset: str | None = None,
        label_version: str = "proxy-next-open-close-v1",
    ) -> None:
        if not isinstance(proxy_symbol, str) or not proxy_symbol.strip():
            raise ValueError("proxy_symbol must be a non-empty string")
        if benchmark_symbol is not None and (
            not isinstance(benchmark_symbol, str) or not benchmark_symbol.strip()
        ):
            raise ValueError("benchmark_symbol must be null or a non-empty string")
        if benchmark_symbol is None and benchmark_dataset is not None:
            raise ValueError("benchmark_dataset requires benchmark_symbol")
        if benchmark_symbol is not None and benchmark_dataset is None:
            raise ValueError(
                "benchmark_dataset must be explicit: use 'daily_bars' for an "
                "ETF/security or 'index_bars' for an index code"
            )
        if benchmark_dataset is not None and benchmark_dataset not in _BENCHMARK_DATASETS:
            raise ValueError("benchmark_dataset must be 'daily_bars' or 'index_bars'")
        if not isinstance(label_version, str) or not label_version.strip():
            raise ValueError("label_version must be a non-empty string")
        self.catalog = catalog
        self.bridge = bridge
        self.proxy_symbol = proxy_symbol.strip()
        self.benchmark_symbol = (
            benchmark_symbol.strip() if benchmark_symbol is not None else None
        )
        self.benchmark_dataset = benchmark_dataset
        self.label_version = label_version.strip()

    def label_pending(self, through_date: str | date) -> list[dict[str, Any]]:
        """Persist labels only for the exchange calendar's exact next session.

        A missing proxy bar on that exact session is a data hole, not permission
        to slide a horizon-1 label to a later session. Every input dataset is
        read between two matching generation descriptions before any catalog
        write occurs.
        """

        through = _as_date(through_date, "through_date")
        pending = self.catalog.pending_labels(
            label_name=self.label_name,
            label_version=self.label_version,
        )
        candidates: list[tuple[dict[str, Any], date]] = []
        for decision in pending:
            decision_day = _as_date(decision.get("trade_date"), "decision.trade_date")
            if decision_day < through:
                candidates.append((decision, decision_day))
        if not candidates:
            return []

        calendar_start = min(day for _, day in candidates) + timedelta(days=1)
        sessions, calendar_generation = _stable_read(
            self.bridge,
            "trading_calendar",
            lambda: self.bridge.load_trading_sessions(calendar_start, through),
        )
        if not isinstance(sessions, list) or not all(
            isinstance(session, date) for session in sessions
        ):
            raise OutcomeLabelDataError(
                "CNEquity trading sessions must be returned as list[date]"
            )
        sessions = sorted(set(sessions))

        scheduled: list[tuple[dict[str, Any], date]] = []
        for decision, decision_day in candidates:
            next_sessions = [session for session in sessions if session > decision_day]
            if next_sessions:
                scheduled.append((decision, next_sessions[0]))
        if not scheduled:
            return []

        bar_start = min(session for _, session in scheduled)
        bar_end = max(session for _, session in scheduled)
        daily_symbols = [self.proxy_symbol]
        if (
            self.benchmark_symbol is not None
            and self.benchmark_dataset == "daily_bars"
            and self.benchmark_symbol not in daily_symbols
        ):
            daily_symbols.append(self.benchmark_symbol)

        daily_rows, daily_generation = _stable_read(
            self.bridge,
            "daily_bars",
            lambda: self.bridge.load_daily_bars(
                symbols=daily_symbols,
                start=bar_start,
                end=bar_end,
                adjust=None,
                strict_adj=True,
            ),
        )
        daily_index = _bar_index(
            daily_rows,
            set(daily_symbols),
            bar_start,
            bar_end,
            dataset="daily_bars",
        )

        source_descriptions: dict[str, dict[str, Any]] = {
            "trading_calendar": calendar_generation,
            "daily_bars": daily_generation,
        }
        benchmark_index: dict[str, dict[date, dict[str, Any]]] | None = None
        if self.benchmark_symbol is not None:
            if self.benchmark_dataset == "daily_bars":
                benchmark_index = daily_index
            else:
                index_rows, index_generation = _stable_read(
                    self.bridge,
                    "index_bars",
                    lambda: self.bridge.load_index_bars(
                        symbols=[self.benchmark_symbol],
                        start=bar_start,
                        end=bar_end,
                    ),
                )
                source_descriptions["index_bars"] = index_generation
                benchmark_index = _bar_index(
                    index_rows,
                    {self.benchmark_symbol},
                    bar_start,
                    bar_end,
                    dataset="index_bars",
                )

        mature: list[
            tuple[
                dict[str, Any],
                date,
                dict[str, Any],
                dict[str, Any] | None,
            ]
        ] = []
        proxy_rows = daily_index[self.proxy_symbol]
        for decision, effective_session in scheduled:
            proxy_bar = proxy_rows.get(effective_session)
            if proxy_bar is None:
                # The calendar, not a later available bar, owns horizon=1.
                continue
            benchmark_bar = None
            if self.benchmark_symbol is not None:
                assert benchmark_index is not None
                benchmark_bar = benchmark_index[self.benchmark_symbol].get(
                    effective_session
                )
                if benchmark_bar is None:
                    continue
            mature.append((decision, effective_session, proxy_bar, benchmark_bar))
        if not mature:
            return []

        source_generations = {
            dataset: description["generation_sha256"]
            for dataset, description in sorted(source_descriptions.items())
        }
        source_generation = sha256_hex(source_generations)
        generation_detail = {
            dataset: _generation_detail(description)
            for dataset, description in sorted(source_descriptions.items())
        }

        recorded: list[dict[str, Any]] = []
        for decision, session, proxy_bar, benchmark_bar in mature:
            decision_id = decision.get("decision_id")
            if not isinstance(decision_id, str) or not decision_id.strip():
                raise OutcomeLabelDataError("pending decision has no decision_id")
            entry_price = float(proxy_bar["open"])
            exit_price = float(proxy_bar["close"])
            gross = exit_price / entry_price - 1.0
            benchmark_gross = None
            excess = None
            if benchmark_bar is not None:
                benchmark_entry = float(benchmark_bar["open"])
                benchmark_exit = float(benchmark_bar["close"])
                benchmark_gross = benchmark_exit / benchmark_entry - 1.0
                excess = gross - benchmark_gross

            entry_at = _session_at(session, time(9, 30))
            exit_at = _session_at(session, time(15, 0))
            label_id = "outcome-" + sha256_hex(
                {
                    "decision_id": decision_id,
                    "label_name": self.label_name,
                    "label_version": self.label_version,
                }
            )
            label_input = {
                "decision_id": decision_id,
                "effective_session": session.isoformat(),
                "proxy_bar_sha256": proxy_bar["row_sha256"],
                "benchmark_bar_sha256": (
                    benchmark_bar["row_sha256"] if benchmark_bar is not None else None
                ),
                "source_generations": source_generations,
            }
            detail = {
                "effective_session": session.isoformat(),
                "horizon_rule": "exchange_calendar_next_session",
                "entry": entry_price,
                "exit": exit_price,
                "entry_field": "open",
                "exit_field": "close",
                "entry_at": entry_at,
                "exit_at": exit_at,
                "gross": gross,
                "excess": excess,
                "benchmark_gross": benchmark_gross,
                "proxy_bar": proxy_bar,
                "benchmark_bar": benchmark_bar,
                "label_input_sha256": sha256_hex(label_input),
                "source_generation": source_generation,
                "source_generations": source_generations,
                "source_generation_detail": generation_detail,
                "proxy_only": True,
                "return_scope": "proxy_only",
                "portfolio_return": None,
                "warning": (
                    "Proxy instrument open-to-close outcome only; this is not "
                    "the realized or simulated return of the decision portfolio."
                ),
            }
            recorded.append(
                self.catalog.record_outcome_label(
                    {
                        "label_id": label_id,
                        "decision_id": decision_id,
                        "label_name": self.label_name,
                        "horizon_sessions": 1,
                        "instrument": self.proxy_symbol,
                        "benchmark": self.benchmark_symbol,
                        "entry_at": entry_at,
                        "entry_price": entry_price,
                        "exit_at": exit_at,
                        "exit_price": exit_price,
                        "gross_return": gross,
                        "excess_return": excess,
                        "label_version": self.label_version,
                        "source_generation": source_generation,
                        "payload": detail,
                        "computed_at": datetime.now(SHANGHAI).isoformat(
                            timespec="microseconds"
                        ),
                    }
                )
            )
        return recorded


__all__ = ["LABEL_NAME", "OutcomeLabelDataError", "ProxyOutcomeLabeler"]
