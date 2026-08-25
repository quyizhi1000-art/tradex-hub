"""Cross-process provider admission budgets."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from astock_signals.shared_rate_limit import (
    SharedRateLimitExceeded,
    consume_shared_rate_budget,
    reserve_shared_request_slot,
)


def test_paid_and_free_buckets_do_not_consume_each_other(tmp_path):
    state_file = tmp_path / "provider-rate-limits.sqlite3"

    consume_shared_rate_budget(
        "paid:tushare:points", 1, state_file=state_file, now=100.0
    )
    consume_shared_rate_budget(
        "free:eastmoney:ip", 1, state_file=state_file, now=100.0
    )

    with pytest.raises(SharedRateLimitExceeded):
        consume_shared_rate_budget(
            "paid:tushare:points", 1, state_file=state_file, now=100.1
        )


def test_separate_python_processes_share_one_rate_budget(tmp_path):
    state_file = tmp_path / "provider-rate-limits.sqlite3"
    script = (
        "from astock_signals.shared_rate_limit import consume_shared_rate_budget;"
        "consume_shared_rate_budget('paid:test:shared', 1)"
    )
    environment = os.environ.copy()
    environment["TRADEX_RATE_LIMIT_STATE_FILE"] = str(state_file)

    first = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    second = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode != 0
    assert "SharedRateLimitExceeded" in second.stderr


def test_shared_spacing_returns_bounded_wait_without_reserving_rejected_slot(tmp_path):
    state_file = tmp_path / "provider-rate-limits.sqlite3"

    assert reserve_shared_request_slot(
        "free:eastmoney:ip",
        min_interval=1.0,
        max_wait=2.0,
        state_file=state_file,
        now=100.0,
    ) == 0.0
    assert reserve_shared_request_slot(
        "free:eastmoney:ip",
        min_interval=1.0,
        max_wait=2.0,
        state_file=state_file,
        now=100.0,
    ) == pytest.approx(1.0)

    with pytest.raises(SharedRateLimitExceeded):
        reserve_shared_request_slot(
            "free:eastmoney:ip",
            min_interval=1.0,
            max_wait=0.5,
            state_file=state_file,
            now=100.0,
        )

    # The rejected reservation must not extend the shared queue.
    assert reserve_shared_request_slot(
        "free:eastmoney:ip",
        min_interval=1.0,
        max_wait=2.0,
        state_file=state_file,
        now=101.0,
    ) == pytest.approx(1.0)
