"""Conservative shared admission for zero-auth web data sources."""

from __future__ import annotations

import os
import time

from .shared_rate_limit import (
    SharedRateLimitExceeded,
    SharedRateLimitUnavailable,
    reserve_shared_request_slot,
)
from .smart_router import SourceBusyError


def _number(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return value if value >= 0 else default


def wait_for_free_source(
    bucket: str,
    *,
    interval_env: str,
    default_interval: float,
    max_wait_env: str,
    default_max_wait: float,
    label: str,
) -> None:
    try:
        wait = reserve_shared_request_slot(
            bucket,
            min_interval=_number(interval_env, default_interval),
            max_wait=_number(max_wait_env, default_max_wait),
        )
    except (SharedRateLimitExceeded, SharedRateLimitUnavailable):
        raise SourceBusyError(f"{label} request queue is busy") from None
    if wait > 0:
        time.sleep(wait)


__all__ = ["wait_for_free_source"]
