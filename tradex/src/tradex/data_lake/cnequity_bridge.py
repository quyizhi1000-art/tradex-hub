"""Read-only bridge from Tradex to a local CNEquity lake.

The bridge deliberately owns neither CNEquity configuration nor ingestion.  It
uses CNEquity's public reader for data and filesystem metadata for a cheap,
stable generation identifier.  No manifest, state store, or ``list_datasets``
call is needed, so constructing or describing the bridge cannot write to the
upstream lake.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .serde import sha256_hex


_DATASET_NAME = re.compile(r"^[A-Za-z0-9_]+$")
_HIVE_DAY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=(\d{4}-\d{2}-\d{2})$")


def _query_date(value: str | date, field_name: str) -> date:
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


class CNEquityBridge:
    """Expose narrowly scoped, read-only access to a CNEquity data root."""

    def __init__(self, data_root: str | Path):
        self.data_root = Path(data_root).expanduser().resolve()
        if not self.data_root.exists():
            raise FileNotFoundError(
                f"CNEquity data root does not exist: {self.data_root}. "
                "Create it with `cne init --config <config>` or point "
                "CNEquityBridge at the configured [data].root."
            )
        if not self.data_root.is_dir():
            raise NotADirectoryError(
                f"CNEquity data root is not a directory: {self.data_root}. "
                "Point CNEquityBridge at the configured [data].root."
            )

    @staticmethod
    def _public_reader() -> Callable[..., Any]:
        """Import CNEquity's public reader only when a query is requested."""

        try:
            from cnequity.query.reader import load
        except ModuleNotFoundError as exc:
            missing = exc.name or "an unknown dependency"
            raise RuntimeError(
                "CNEquity is unavailable in the Tradex Python environment "
                f"(missing module: {missing}). Install it with "
                "`python -m pip install cnequity`, or install the local checkout "
                "with `python -m pip install -e <CNEquity checkout>`."
            ) from exc
        except ImportError as exc:
            raise RuntimeError(
                "CNEquity could not be imported. Reinstall a compatible CNEquity "
                "runtime in the same Python environment as Tradex."
            ) from exc
        return load

    def _load_rows(self, dataset: str, **kwargs: Any) -> list[dict[str, Any]]:
        frame = self._public_reader()(
            dataset,
            data_root=self.data_root,
            **kwargs,
        )
        to_dicts = getattr(frame, "to_dicts", None)
        if not callable(to_dicts):
            raise TypeError(
                "CNEquity reader returned an unsupported value; expected a "
                "Polars DataFrame with to_dicts()."
            )
        rows = to_dicts()
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise TypeError("CNEquity DataFrame.to_dicts() did not return list[dict].")
        return rows

    def load_daily_bars(
        self,
        symbols: list[str],
        start: str | date,
        end: str | date,
        adjust: str | None = None,
        strict_adj: bool = True,
    ) -> list[dict[str, Any]]:
        """Load daily bars through CNEquity's public reader API.

        CNEquity remains optional for the Tradex process until this method is
        called.  This keeps unrelated live-data tools usable in environments
        that have not installed the lake runtime.
        """

        return self._load_rows(
            "daily_bars",
            symbols=list(symbols),
            start=start,
            end=end,
            adjust=adjust,
            strict_adj=strict_adj,
        )

    def load_index_bars(
        self,
        symbols: list[str],
        start: str | date,
        end: str | date,
    ) -> list[dict[str, Any]]:
        """Load index levels explicitly, never through ``daily_bars``."""

        return self._load_rows(
            "index_bars",
            symbols=list(symbols),
            start=start,
            end=end,
        )

    def load_trading_sessions(
        self,
        start: str | date,
        end: str | date,
    ) -> list[date]:
        """Return unique open sessions through CNEquity's public reader.

        Conflicting duplicate calendar rows are rejected instead of allowing a
        filesystem-order-dependent choice of the label horizon.
        """

        start_day = _query_date(start, "start")
        end_day = _query_date(end, "end")
        if start_day > end_day:
            raise ValueError("start must be on or before end")
        rows = self._load_rows("trading_calendar", start=start, end=end)
        states: dict[date, bool] = {}
        for index, row in enumerate(rows):
            raw_day = row.get("trade_date")
            if isinstance(raw_day, datetime):
                session = raw_day.date()
            elif isinstance(raw_day, date):
                session = raw_day
            elif isinstance(raw_day, str):
                try:
                    session = date.fromisoformat(raw_day.strip())
                except ValueError as exc:
                    raise ValueError(
                        f"trading_calendar row {index} has an invalid trade_date"
                    ) from exc
            else:
                raise TypeError(
                    f"trading_calendar row {index} has no valid trade_date"
                )
            is_trading = row.get("is_trading")
            if not isinstance(is_trading, bool):
                raise TypeError(
                    f"trading_calendar row {index} has non-boolean is_trading"
                )
            prior = states.get(session)
            if prior is not None and prior != is_trading:
                raise ValueError(
                    "trading_calendar has conflicting rows for "
                    f"{session.isoformat()}"
                )
            states[session] = is_trading
        expected_days: list[date] = []
        cursor = start_day
        while cursor <= end_day:
            expected_days.append(cursor)
            cursor += timedelta(days=1)
        missing_days = [session for session in expected_days if session not in states]
        if missing_days:
            preview = ", ".join(session.isoformat() for session in missing_days[:3])
            raise ValueError(
                "trading_calendar does not densely cover the requested window; "
                f"missing {len(missing_days)} day(s), starting with {preview}"
            )
        return sorted(session for session, is_trading in states.items() if is_trading)

    def describe_generation(self, dataset: str = "daily_bars") -> dict[str, Any]:
        """Describe the immutable-looking Parquet generation for *dataset*.

        The digest is intentionally metadata based.  A correction or compact
        that replaces a file changes its size or nanosecond mtime; a backfill
        changes the sorted relative-path set.  Absolute paths are excluded so
        moving an otherwise identical lake does not invent a new generation.
        """

        if not _DATASET_NAME.fullmatch(dataset):
            raise ValueError(
                "CNEquity dataset must contain only letters, numbers, and underscores."
            )

        roots = (
            self.data_root / "curated" / dataset,
            self.data_root / "derived" / dataset,
        )
        parquet_paths = sorted(
            (path for root in roots if root.is_dir() for path in root.rglob("*.parquet")),
            key=lambda path: path.relative_to(self.data_root).as_posix(),
        )
        if not parquet_paths:
            searched = ", ".join(str(root) for root in roots)
            raise FileNotFoundError(
                f"No CNEquity Parquet files found for dataset {dataset!r}; searched {searched}. "
                "Run the owning `cne init`/`cne backfill` step and ensure it compacted "
                "successfully before connecting Tradex."
            )

        files: list[dict[str, int | str]] = []
        coverage_days: list[date] = []
        try:
            for path in parquet_paths:
                stat = path.stat()
                relative_path = path.relative_to(self.data_root).as_posix()
                files.append(
                    {
                        "relative_path": relative_path,
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                    }
                )
                for part in path.relative_to(self.data_root).parts:
                    match = _HIVE_DAY.fullmatch(part)
                    if match:
                        coverage_days.append(date.fromisoformat(match.group(1)))
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"CNEquity dataset {dataset!r} changed while its generation was read. "
                "Retry after the active `cne compact` run finishes."
            ) from exc

        coverage_min = min(coverage_days).isoformat() if coverage_days else None
        coverage_max = max(coverage_days).isoformat() if coverage_days else None
        return {
            "dataset": dataset,
            "generation_sha256": sha256_hex(files),
            "coverage": {"min": coverage_min, "max": coverage_max},
            "file_count": len(files),
            "total_size": sum(int(item["size"]) for item in files),
            "files": files,
        }


__all__ = ["CNEquityBridge"]
