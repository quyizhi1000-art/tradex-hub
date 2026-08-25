"""SmartRouter 请求隔离与并发回归测试。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import astock_signals.smart_router as router_module
from astock_signals.smart_router import (
    RequestValidationError,
    RouteDeadlineExceeded,
    SmartRouter,
    SourceBusyError,
    SourceCapabilityError,
)


def _health_by_source(router: SmartRouter) -> dict[str, dict]:
    return {entry["source"]: entry for entry in router.get_health_report()}


def test_request_validation_stops_without_fallback_or_health_pollution():
    router = SmartRouter()
    backup_called = False

    def invalid_source(**kwargs):
        raise RequestValidationError("invalid symbol")

    def backup_source(**kwargs):
        nonlocal backup_called
        backup_called = True
        return "unexpected"

    router.register("quote", "primary", invalid_source, priority=1)
    router.register("quote", "backup", backup_source, priority=100)

    with pytest.raises(RequestValidationError, match="invalid symbol"):
        router.route("quote", symbol="bad")

    assert backup_called is False
    health = _health_by_source(router)
    assert health["quote:primary"]["total_calls"] == 0
    assert health["quote:backup"]["total_calls"] == 0


def test_legacy_value_error_falls_back_without_health_pollution():
    router = SmartRouter()

    def invalid_source(**kwargs):
        raise ValueError("unsupported period")

    def backup_source(**kwargs):
        return "fallback"

    router.register("quote", "primary", invalid_source, priority=1)
    router.register("quote", "backup", backup_source, priority=100)

    assert router.route("quote", period="invalid") == ("fallback", "backup")

    health = _health_by_source(router)
    assert health["quote:primary"]["total_calls"] == 0
    assert health["quote:backup"]["total_calls"] == 1
    assert health["quote:backup"]["fail_count"] == 0


def test_all_legacy_value_errors_surface_as_request_validation():
    router = SmartRouter()

    def legacy_validation(**kwargs):
        raise ValueError("unsupported period")

    router.register("quote", "primary", legacy_validation, priority=1)
    router.register("quote", "backup", legacy_validation, priority=100)

    with pytest.raises(RequestValidationError, match="unsupported period"):
        router.route("quote", period="invalid")

    health = _health_by_source(router)
    assert health["quote:primary"]["total_calls"] == 0
    assert health["quote:backup"]["total_calls"] == 0


@pytest.mark.parametrize("error_type", [SourceCapabilityError, SourceBusyError])
def test_request_local_skip_falls_back_without_health_pollution(error_type):
    router = SmartRouter()

    def unavailable_primary(**kwargs):
        raise error_type("not available for this request")

    router.register("quote", "primary", unavailable_primary, priority=1)
    router.register(
        "quote",
        "backup",
        lambda **kwargs: kwargs["request_id"],
        priority=100,
    )

    result, source = router.route("quote", request_id="request-b")

    assert (result, source) == ("request-b", "backup")
    health = _health_by_source(router)
    assert health["quote:primary"]["total_calls"] == 0
    assert health["quote:primary"]["fail_count"] == 0
    assert health["quote:backup"]["total_calls"] == 1


def test_global_health_never_changes_a_later_requests_fixed_priority():
    router = SmartRouter()

    def primary(request_id):
        if request_id.startswith("poison"):
            raise RuntimeError("provider failure")
        return request_id

    router.register("quote", "primary", primary, priority=1)
    router.register("quote", "backup", lambda request_id: request_id, priority=100)

    for index in range(5):
        assert router.route("quote", request_id=f"poison-{index}")[1] == "backup"

    before = _health_by_source(router)["quote:primary"]
    assert before["score"] == 0.0
    assert before["is_healthy"] is False

    assert router.route("quote", request_id="valid-request") == (
        "valid-request",
        "primary",
    )


def test_register_is_idempotent_and_copy_on_write():
    router = SmartRouter()

    def primary():
        return "primary"

    def backup():
        return "backup"

    router.register("quote", "primary", primary, priority=10)
    first_snapshot = router._sources["quote"]

    router.register("quote", "primary", primary, priority=10)
    assert router._sources["quote"] is first_snapshot

    router.register("quote", "backup", backup, priority=20)
    second_snapshot = router._sources["quote"]
    assert isinstance(second_snapshot, tuple)
    assert second_snapshot is not first_snapshot
    assert [entry[0] for entry in first_snapshot] == ["primary"]
    assert [entry[0] for entry in second_snapshot] == ["primary", "backup"]


def test_parallel_identical_registration_creates_one_entry():
    router = SmartRouter()

    def primary():
        return "primary"

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(
            pool.map(
                lambda _: router.register("quote", "primary", primary, priority=1),
                range(128),
            )
        )

    assert router._sources["quote"] == (("primary", primary, 1, False),)


def test_inflight_request_keeps_its_candidate_snapshot():
    router = SmartRouter()
    primary_started = threading.Event()
    release_primary = threading.Event()

    def primary():
        primary_started.set()
        assert release_primary.wait(timeout=2)
        raise RuntimeError("current request failure")

    router.register("quote", "primary", primary, priority=10)
    router.register("quote", "original_backup", lambda: "original", priority=20)

    with ThreadPoolExecutor(max_workers=1) as pool:
        inflight = pool.submit(router.route, "quote")
        assert primary_started.wait(timeout=2)
        router.register("quote", "new_primary", lambda: "new", priority=1)
        release_primary.set()
        assert inflight.result(timeout=2) == ("original", "original_backup")

    assert router.route("quote") == ("new", "new_primary")


def test_parallel_routes_keep_responses_and_health_updates_isolated():
    router = SmartRouter()

    def primary(request_id):
        if request_id % 2 == 0:
            raise SourceCapabilityError("even IDs use backup")
        return {"request_id": request_id}

    def backup(request_id):
        return {"request_id": request_id}

    router.register("quote", "primary", primary, priority=1)
    router.register("quote", "backup", backup, priority=100)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(
            pool.map(
                lambda request_id: router.route("quote", request_id=request_id),
                range(200),
            )
        )

    for request_id, (payload, source) in enumerate(results):
        assert payload["request_id"] == request_id
        assert source == ("backup" if request_id % 2 == 0 else "primary")

    health = _health_by_source(router)
    assert health["quote:primary"]["total_calls"] == 100
    assert health["quote:primary"]["fail_count"] == 0
    assert health["quote:backup"]["total_calls"] == 100


def test_get_router_singleton_is_thread_safe(monkeypatch):
    monkeypatch.setattr(router_module, "_global_router", None)

    with ThreadPoolExecutor(max_workers=16) as pool:
        routers = list(pool.map(lambda _: router_module.get_router(), range(128)))

    assert len({id(router) for router in routers}) == 1


def test_slow_provider_deadline_falls_back_and_late_result_never_records_success():
    router = SmartRouter(
        route_deadline_seconds=0.5,
        provider_deadline_seconds=0.05,
        max_provider_calls=2,
    )
    slow_started = threading.Event()
    release_slow = threading.Event()

    def slow_provider():
        slow_started.set()
        assert release_slow.wait(timeout=2)
        return "late"

    router.register("quote", "slow", slow_provider, priority=1)
    router.register("quote", "fast", lambda: "fast", priority=2)

    started_at = time.monotonic()
    try:
        assert router.route("quote") == ("fast", "fast")
        assert slow_started.is_set()
        assert time.monotonic() - started_at < 0.3

        health = _health_by_source(router)
        assert health["quote:slow"]["fail_count"] == 1
        assert health["quote:slow"]["success_rate"] == 0.0
    finally:
        release_slow.set()

    time.sleep(0.02)
    health = _health_by_source(router)
    assert health["quote:slow"]["fail_count"] == 1
    assert health["quote:slow"]["success_rate"] == 0.0


def test_route_deadline_caps_cumulative_provider_fallback_time():
    router = SmartRouter(
        route_deadline_seconds=0.08,
        provider_deadline_seconds=0.05,
        max_provider_calls=2,
    )
    release = threading.Event()
    third_started = threading.Event()

    def slow_provider():
        assert release.wait(timeout=2)
        return "late"

    router.register("quote", "slow-a", slow_provider, priority=1)
    router.register("quote", "slow-b", slow_provider, priority=2)
    router.register(
        "quote",
        "must-not-start",
        lambda: third_started.set(),
        priority=3,
    )

    started_at = time.monotonic()
    try:
        with pytest.raises(RouteDeadlineExceeded, match="deadline expired"):
            router.route("quote")
        assert time.monotonic() - started_at < 0.3
        assert third_started.is_set() is False
    finally:
        release.set()


def test_provider_capacity_queue_obeys_the_callers_route_deadline():
    router = SmartRouter(
        route_deadline_seconds=1.0,
        provider_deadline_seconds=1.0,
        max_provider_calls=1,
    )
    first_started = threading.Event()
    release_first = threading.Event()
    queued_started = threading.Event()

    def held_provider():
        first_started.set()
        assert release_first.wait(timeout=2)
        return "first"

    router.register("held", "provider", held_provider)
    router.register("queued", "provider", lambda: queued_started.set())

    with ThreadPoolExecutor(max_workers=1) as caller_pool:
        first = caller_pool.submit(router.route, "held")
        assert first_started.wait(timeout=1)
        started_at = time.monotonic()
        try:
            with pytest.raises(RouteDeadlineExceeded, match="deadline expired"):
                router.route(
                    "queued",
                    deadline_seconds=0.05,
                    provider_deadline_seconds=0.05,
                )
            assert time.monotonic() - started_at < 0.3
            assert queued_started.is_set() is False
        finally:
            release_first.set()
        assert first.result(timeout=1) == ("first", "provider")


def test_validator_is_included_in_each_provider_attempt_deadline():
    router = SmartRouter(
        route_deadline_seconds=0.5,
        provider_deadline_seconds=0.05,
        max_provider_calls=2,
    )
    release_validator = threading.Event()

    router.register("example", "slow-validation", lambda: "raw", priority=1)
    router.register("example", "fast-validation", lambda: "raw", priority=2)

    def validate(value, source):
        if source == "slow-validation":
            assert release_validator.wait(timeout=2)
        return f"{value}:{source}"

    try:
        assert router.route_validated("example", validate) == (
            "raw:fast-validation",
            "fast-validation",
        )
    finally:
        release_validator.set()
