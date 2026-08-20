from __future__ import annotations

import argparse
import ctypes
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path


DATA_ROOT = Path(r"G:\CNEquity\data\cnequity")
STATE_PATH = DATA_ROOT / "meta" / "automation" / "seed_90d_status.json"
MANIFEST_PATH = DATA_ROOT / "meta" / "manifest.db"
FACTOR_CACHE = DATA_ROOT / "meta" / "adj_factors_cache"


def process_alive(pid: object) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information, False, value
    )
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return {}


def daily_symbol_total() -> int:
    pattern = str(DATA_ROOT / "curated" / "daily_bars" / "**" / "*.parquet").replace(
        "\\", "/"
    )
    try:
        import duckdb

        connection = duckdb.connect()
        try:
            row = connection.execute(
                "SELECT count(DISTINCT symbol) FROM read_parquet(?, hive_partitioning=true)",
                [pattern],
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            connection.close()
    except Exception:
        return 0


def manifest_snapshot(run_id: str) -> dict:
    result: dict = {"run_status": "unknown", "daily": {}, "running": None}
    if not MANIFEST_PATH.exists():
        return result
    try:
        connection = sqlite3.connect(MANIFEST_PATH, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            if run_id:
                row = connection.execute(
                    "SELECT status FROM ingestion_runs WHERE run_id=?", (run_id,)
                ).fetchone()
                result["run_status"] = row["status"] if row else "missing"
                counts = connection.execute(
                    """
                    SELECT status, count(*) AS n
                    FROM ingestion_batches
                    WHERE run_id=? AND dataset='daily_bars'
                    GROUP BY status
                    """,
                    (run_id,),
                ).fetchall()
                result["daily"] = {row["status"]: int(row["n"]) for row in counts}
            running = connection.execute(
                """
                SELECT task_id, dataset, status, heartbeat_at, started_at
                FROM ingestion_batches
                WHERE status='running'
                ORDER BY started_at DESC
                LIMIT 1
                """
            ).fetchone()
            result["running"] = dict(running) if running else None
        finally:
            connection.close()
    except sqlite3.Error:
        pass
    return result


def progress_bar(done: int, total: int, width: int = 42) -> tuple[str, float]:
    ratio = min(1.0, max(0.0, done / total)) if total else 0.0
    filled = min(width, int(width * ratio))
    return "[" + "#" * filled + "-" * (width - filled) + "]", ratio * 100.0


def log_tail(path_text: object, count: int = 8) -> list[str]:
    if not path_text:
        return []
    try:
        lines = Path(str(path_text)).read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return []
    useful = [line for line in lines if line.strip() and not line.startswith("***")]
    return useful[-count:]


def render(symbol_total: int) -> tuple[str, bool]:
    state = load_state()
    run_id = str(state.get("resume_run_id") or "")
    manifest = manifest_snapshot(run_id)
    controller_alive = process_alive(state.get("controller_pid"))
    factor_done = len(list(FACTOR_CACHE.glob("*_hfq.parquet"))) if FACTOR_CACHE.exists() else 0
    bar, percent = progress_bar(factor_done, symbol_total)
    running = manifest.get("running") or {}

    daily = manifest.get("daily") or {}
    daily_total = sum(daily.values())
    daily_success = int(daily.get("success", 0))
    if daily_total and daily_success == daily_total:
        if manifest.get("run_status") == "success":
            daily_gate = f"complete ({daily_success}/{daily_total} batches)"
        else:
            daily_gate = f"source complete ({daily_success}/{daily_total}); finalizing run"
    else:
        daily_gate = f"pending ({daily_success}/{daily_total} success)"

    lines = [
        "CNEquity 90-Day Lake Seed - Live Progress",
        "=" * 62,
        f"Time             : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Window           : {state.get('start_date', '?')} .. {state.get('end_date', '?')}",
        f"Controller       : {state.get('status', 'unknown')} / alive={controller_alive}",
        f"Controller step  : {state.get('step', 'unknown')}",
        f"Current inner job: {running.get('task_id') or running.get('dataset') or '-'}",
        f"Daily data gate  : {daily_gate}",
    ]

    lines.append(
        "Daily batches    : "
        + ", ".join(f"{key}={daily[key]}" for key in sorted(daily))
        if daily
        else "Daily batches    : unavailable"
    )
    if symbol_total:
        lines.extend(
            [
                "",
                "Current first-run adj-factor cache:",
                f"{bar} {percent:5.1f}%",
                f"Cached symbols   : {factor_done:,} / {symbol_total:,}",
                "Note             : fetch failures may make the final cache count slightly lower.",
            ]
        )

    tail = log_tail(state.get("log_path"))
    if tail:
        lines.extend(["", "Recent controller log:", "-" * 62, *tail])
    lines.extend(
        [
            "",
            "This window is read-only. Closing it does not stop the seed task.",
            "Press Ctrl+C to close this monitor.",
        ]
    )
    terminal = state.get("status") in {"complete", "failed"} and not controller_alive
    return "\n".join(lines), terminal


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--refresh", type=int, default=5)
    args = parser.parse_args()

    symbol_total = daily_symbol_total()
    try:
        while True:
            if not args.once:
                os.system("cls")
            output, terminal = render(symbol_total)
            print(output, flush=True)
            if args.once or terminal:
                return 0
            time.sleep(max(2, int(args.refresh)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
