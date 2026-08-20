"""Command line entry point for the independent Tradex observation collector."""

from __future__ import annotations

import argparse
import json
import os
import signal
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .cnequity_bridge import CNEquityBridge
from .labels import LABEL_NAME, ProxyOutcomeLabeler
from .pipeline import CapturePipeline
from .replay import ReplayEngine
from .serde import to_jsonable


def _default_root() -> Path:
    configured = os.environ.get("TRADEX_DATA_LAKE_ROOT")
    return Path(configured).expanduser() if configured else Path.home() / ".tradex" / "lake"


def _default_cnequity_root() -> Path | None:
    configured = os.environ.get("CNEQUITY_DATA_ROOT", "").strip()
    return Path(configured).expanduser() if configured else None


def _emit(payload: Any) -> None:
    print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tradex.data_lake",
        description="Persist Tradex live observations and shadow decisions independently of the dashboard.",
    )
    parser.add_argument("--root", type=Path, default=_default_root(), help="Tradex lake root")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("collect-once", help="capture and atomically publish one observation")
    run = commands.add_parser("collect-run", help="sample during Shanghai trading sessions")
    run.add_argument("--interval", type=float, default=60.0, help="sampling interval in seconds")
    commands.add_parser("status", help="show the latest completed observation")
    replay = commands.add_parser("replay", help="recompute one capture from immutable inputs")
    replay.add_argument("capture_id")
    cne = commands.add_parser("cne-status", help="describe the immutable generation of a CNEquity dataset")
    cne.add_argument("--data-root", type=Path, required=True)
    cne.add_argument("--dataset", default="daily_bars")
    labels = commands.add_parser(
        "label-pending",
        help="label mature shadow decisions from the next CNEquity session",
    )
    labels.add_argument("--through-date", required=True, help="inclusive YYYY-MM-DD maturity bound")
    labels.add_argument(
        "--data-root",
        type=Path,
        default=_default_cnequity_root(),
        help="CNEquity [data].root (defaults to CNEQUITY_DATA_ROOT)",
    )
    labels.add_argument("--proxy", default="510300.SH", help="proxy instrument in daily_bars")
    labels.add_argument("--benchmark", default=None, help="optional benchmark instrument")
    labels.add_argument(
        "--benchmark-dataset",
        choices=("daily_bars", "index_bars"),
        default=None,
        help=(
            "required with --benchmark: daily_bars for an ETF/security or "
            "index_bars for an index code"
        ),
    )
    labels.add_argument("--version", default="proxy-next-open-close-v1")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.expanduser().resolve()
    if args.command == "collect-once":
        result = CapturePipeline(root).capture_once()
        _emit(asdict(result))
        return 0 if result.status in {"completed", "already_completed"} else 1
    if args.command == "collect-run":
        stop_event = threading.Event()

        def request_stop(signum=None, frame=None) -> None:  # noqa: ARG001
            stop_event.set()

        signal.signal(signal.SIGINT, request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, request_stop)
        CapturePipeline(root).run(stop_event, interval_seconds=args.interval)
        return 0
    if args.command == "status":
        catalog_path = root / "meta" / "catalog.sqlite3"
        if not catalog_path.is_file():
            _emit({"status": "empty", "root": str(root)})
            return 0
        catalog = Catalog(catalog_path, read_only=True)
        try:
            _emit(catalog.latest_completed() or {"status": "empty", "root": str(root)})
        finally:
            catalog.close()
        return 0
    if args.command == "replay":
        result = ReplayEngine(root).rebuild(args.capture_id)
        _emit(asdict(result))
        return 0 if result.match else 1
    if args.command == "cne-status":
        _emit(CNEquityBridge(args.data_root).describe_generation(args.dataset))
        return 0
    if args.command == "label-pending":
        if args.data_root is None:
            raise SystemExit(
                "label-pending requires --data-root or CNEQUITY_DATA_ROOT pointing "
                "to CNEquity [data].root"
            )
        if args.benchmark is not None and args.benchmark_dataset is None:
            raise SystemExit(
                "--benchmark requires --benchmark-dataset daily_bars for an "
                "ETF/security or index_bars for an index code"
            )
        if args.benchmark is None and args.benchmark_dataset is not None:
            raise SystemExit("--benchmark-dataset requires --benchmark")
        catalog_path = root / "meta" / "catalog.sqlite3"
        if not catalog_path.is_file():
            raise SystemExit(
                f"Tradex lake catalog does not exist: {catalog_path}. "
                "Run collect-once first."
            )
        catalog = Catalog(catalog_path)
        try:
            labeler = ProxyOutcomeLabeler(
                catalog,
                CNEquityBridge(args.data_root),
                proxy_symbol=args.proxy,
                benchmark_symbol=args.benchmark,
                benchmark_dataset=args.benchmark_dataset,
                label_version=args.version,
            )
            recorded = labeler.label_pending(args.through_date)
            _emit(
                {
                    "label_name": LABEL_NAME,
                    "label_version": args.version,
                    "through_date": args.through_date,
                    "proxy": args.proxy,
                    "benchmark": args.benchmark,
                    "benchmark_dataset": args.benchmark_dataset,
                    "recorded": len(recorded),
                    "labels": recorded,
                }
            )
        finally:
            catalog.close()
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
