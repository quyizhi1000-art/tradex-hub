"""Tests for the isolated, rate-limited Eastmoney client."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from astock_signals import anti_ban_client
from astock_signals.anti_ban_client import (
    _em_next_slot,
    em_get,
    em_reset_session,
    reserve_em_request_slot,
    set_jitter_range,
    set_min_interval,
)
from astock_signals.smart_router import SourceBusyError


@pytest.fixture(autouse=True)
def reset_state(monkeypatch, tmp_path):
    """每个测试前后重置全局状态。"""
    monkeypatch.setenv(
        "TRADEX_RATE_LIMIT_STATE_FILE",
        str(tmp_path / "provider-rate-limits.sqlite3"),
    )
    _em_next_slot[0] = 0.0
    original_interval = anti_ban_client._EM_MIN_INTERVAL
    original_jitter = (
        anti_ban_client._EM_JITTER_MIN,
        anti_ban_client._EM_JITTER_MAX,
    )
    original_queue_wait = anti_ban_client._EM_MAX_QUEUE_WAIT
    # Keep timing tests short and deterministic.
    set_min_interval(0.03)
    set_jitter_range(0.0, 0.0)
    anti_ban_client._EM_MAX_QUEUE_WAIT = 1.0
    yield
    set_min_interval(original_interval)
    set_jitter_range(*original_jitter)
    anti_ban_client._EM_MAX_QUEUE_WAIT = original_queue_wait
    _em_next_slot[0] = 0.0
    em_reset_session()


def test_em_get_single_call_returns_response():
    """单线程调用 em_get 应返回 session.get() 的结果。"""
    mock_response = MagicMock(status_code=200)
    mock_session = MagicMock()
    mock_session.get.return_value = mock_response

    with patch(
        "astock_signals.anti_ban_client._ensure_session",
        return_value=mock_session,
    ):
        result = em_get("https://example.com/test", params={"k": "v"})

    assert result is mock_response
    mock_session.get.assert_called_once()
    # 验证参数透传
    _, kwargs = mock_session.get.call_args
    assert kwargs["params"] == {"k": "v"}
    assert kwargs["timeout"] == 15


def test_concurrent_reservations_are_atomic_and_evenly_spaced(monkeypatch):
    """Concurrent callers reserve one deterministic provider/IP timeline."""
    waits: list[float] = []
    waits_lock = threading.Lock()

    def reserve():
        wait = reserve_em_request_slot()
        with waits_lock:
            waits.append(wait)

    threads = [threading.Thread(target=reserve) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    expected = [index * anti_ban_client._EM_MIN_INTERVAL for index in range(10)]
    assert sorted(waits) == pytest.approx(expected, abs=0.03)


def test_set_min_interval_updates_global():
    """set_min_interval 应即时更新 _EM_MIN_INTERVAL。"""
    set_min_interval(0.5)
    assert anti_ban_client._EM_MIN_INTERVAL == 0.5
    set_min_interval(1.2)
    assert anti_ban_client._EM_MIN_INTERVAL == 1.2


def test_em_reset_session_closes_and_clears():
    """em_reset_session closes only the calling thread's session."""
    mock_session = MagicMock()
    anti_ban_client._session_local.session = mock_session

    em_reset_session()

    mock_session.close.assert_called_once()
    assert anti_ban_client._session_local.session is None


def test_em_reset_session_no_session_is_noop():
    """em_reset_session is a no-op without a session in this thread."""
    anti_ban_client._session_local.session = None
    # 不应抛异常
    em_reset_session()
    assert anti_ban_client._session_local.session is None


def test_em_get_first_call_after_reset_no_long_wait():
    """The first request after a slot reset does not wait."""
    _em_next_slot[0] = 0.0
    set_min_interval(1.0)

    mock_session = MagicMock()
    mock_session.get.return_value = MagicMock(status_code=200)

    start = time.time()
    with patch(
        "astock_signals.anti_ban_client._ensure_session",
        return_value=mock_session,
    ):
        em_get("https://example.com/test")
    elapsed = time.time() - start

    assert elapsed < 0.2, f"首次调用耗时 {elapsed:.3f}s 过长，可能仍持锁 sleep"


def test_sessions_are_worker_local(monkeypatch):
    """Concurrent workers must never share mutable requests.Session state."""

    created: list[MagicMock] = []
    created_lock = threading.Lock()
    barrier = threading.Barrier(2)
    seen: list[tuple[object, object]] = []

    def make_session():
        session = MagicMock()
        with created_lock:
            created.append(session)
        return session

    monkeypatch.setattr(anti_ban_client._requests, "Session", make_session)

    def worker():
        first = anti_ban_client._ensure_session()
        barrier.wait()
        second = anti_ban_client._ensure_session()
        with created_lock:
            seen.append((first, second))
        em_reset_session()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(created) == 2
    assert len(seen) == 2
    assert all(first is second for first, second in seen)
    assert seen[0][0] is not seen[1][0]


def test_busy_queue_fails_fast_without_network_call():
    """A saturated provider slot falls back instead of occupying a worker."""

    set_min_interval(1.0)
    anti_ban_client._EM_MAX_QUEUE_WAIT = 0.01
    reserve_em_request_slot()
    mock_session = MagicMock()

    with patch(
        "astock_signals.anti_ban_client._ensure_session",
        return_value=mock_session,
    ):
        with pytest.raises(SourceBusyError, match="queue is busy"):
            em_get("https://example.com/test")

    mock_session.get.assert_not_called()


def test_tradex_client_uses_the_same_process_wide_slot(monkeypatch):
    """requests and curl_cffi clients must not admit the same IP slot twice."""
    from tradex.data_sources import em_client

    _em_next_slot[0] = 0.0
    set_min_interval(1.0)
    set_jitter_range(0.0, 0.0)
    anti_ban_client._EM_MAX_QUEUE_WAIT = 0.1

    requests_session = MagicMock()
    curl_session = MagicMock()
    monkeypatch.setattr(anti_ban_client, "_ensure_session", lambda: requests_session)
    monkeypatch.setattr(em_client, "_get_session", lambda: curl_session)

    em_get("https://example.test/requests")

    assert em_client._em_next_slot is _em_next_slot
    assert em_client._EM_REQUEST_LOCK is anti_ban_client._lock
    with pytest.raises(SourceBusyError, match="queue is busy"):
        em_client.em_get("https://example.test/curl")
    requests_session.get.assert_called_once()
    curl_session.get.assert_not_called()
