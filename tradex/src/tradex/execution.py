"""Bounded execution and request isolation for Tradex MCP tools.

Most Tradex tools are declared with ``async def`` for FastMCP compatibility but
perform synchronous HTTP or CPU work.  FastMCP awaits such functions on its
event-loop thread, so one slow tool can otherwise block every MCP session.

``IsolatedFastMCP`` keeps the public FastMCP registration API while wrapping
those blocking handlers at the single ``add_tool()`` boundary.  Truly
asynchronous handlers must opt out explicitly with :func:`native_async`.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, ParamSpec, TypeVar, cast
from uuid import uuid4

import anyio
from mcp.server.fastmcp import FastMCP

from astock_signals.smart_router import current_deadline, deadline_scope


P = ParamSpec("P")
R = TypeVar("R")

DEFAULT_MAX_BLOCKING_CALLS = 8
DEFAULT_MAX_REQUEST_BLOCKING_CALLS = 2
DEFAULT_REQUEST_DEADLINE_SECONDS = 45.0
MAX_BLOCKING_CALLS_ENV = "TRADEX_MCP_MAX_BLOCKING_CALLS"
REQUEST_DEADLINE_ENV = "TRADEX_MCP_REQUEST_DEADLINE_SECONDS"
_NATIVE_ASYNC_ATTRIBUTE = "__tradex_native_async__"


def _configured_blocking_limit() -> int:
    """Return the configured process-wide blocking-call limit."""

    raw = os.getenv(MAX_BLOCKING_CALLS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_BLOCKING_CALLS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_BLOCKING_CALLS
    return value if value > 0 else DEFAULT_MAX_BLOCKING_CALLS


def _configured_request_deadline() -> float:
    """Return the configured end-to-end MCP request deadline."""

    raw = os.getenv(REQUEST_DEADLINE_ENV, "").strip()
    if not raw:
        return DEFAULT_REQUEST_DEADLINE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_REQUEST_DEADLINE_SECONDS
    return value if value > 0 else DEFAULT_REQUEST_DEADLINE_SECONDS


class BlockingDeadlineExceeded(TimeoutError):
    """Blocking work exhausted its total queue-and-execution deadline."""


class RequestDeadlineExceeded(TimeoutError):
    """A complete MCP tool request exhausted its deadline."""


class BlockingExecutor:
    """Run synchronous work in a bounded AnyIO worker pool.

    A caller deadline includes time spent waiting for capacity. Python cannot
    safely stop a running thread, so an expired caller returns immediately but
    the detached worker keeps its capacity token until the real thread ends.
    This preserves a hard concurrency bound without turning timeout into an
    unbounded wait.
    """

    def __init__(self, max_calls: int) -> None:
        if max_calls <= 0:
            raise ValueError("max_calls must be greater than zero")
        self._max_calls = max_calls
        self._limiter = anyio.CapacityLimiter(max_calls)
        self._thread_limiter = anyio.CapacityLimiter(max_calls)
        self._workers: set[asyncio.Task[Any]] = set()

    @property
    def max_calls(self) -> int:
        return self._max_calls

    @property
    def statistics(self) -> anyio.CapacityLimiterStatistics:
        return self._limiter.statistics()

    def _track_worker(self, worker: asyncio.Task[Any]) -> None:
        self._workers.add(worker)

        def consume_result(completed: asyncio.Task[Any]) -> None:
            self._workers.discard(completed)
            if completed.cancelled():
                return
            try:
                completed.exception()
            except BaseException:
                pass

        worker.add_done_callback(consume_result)

    async def _run_worker(
        self,
        call: Callable[[], R],
        started: asyncio.Event,
    ) -> R:
        # This task, rather than the request task, owns the token. If the caller
        # times out after start, the token remains borrowed until the real
        # thread returns.
        async with self._limiter:
            started.set()
            return await anyio.to_thread.run_sync(
                call,
                abandon_on_cancel=False,
                limiter=self._thread_limiter,
            )

    async def run(
        self,
        func: Callable[P, R],
        *args: P.args,
        deadline_at: float | None = None,
        **kwargs: P.kwargs,
    ) -> R:
        call = functools.partial(func, *args, **kwargs)
        if deadline_at is None:
            inherited = current_deadline()
            deadline_at = (
                inherited
                if inherited is not None
                else time.monotonic() + _configured_request_deadline()
            )

        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise BlockingDeadlineExceeded(
                "blocking deadline expired before entering the worker queue"
            )

        started = asyncio.Event()
        worker = asyncio.create_task(self._run_worker(call, started))
        self._track_worker(worker)
        try:
            return await asyncio.wait_for(
                asyncio.shield(worker),
                timeout=remaining,
            )
        except asyncio.TimeoutError as exc:
            if not started.is_set():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                stage = "queue"
            else:
                stage = "execution"
            raise BlockingDeadlineExceeded(
                f"blocking {stage} exhausted the total deadline"
            ) from exc
        except asyncio.CancelledError:
            if not started.is_set():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            raise


_default_executor = BlockingExecutor(_configured_blocking_limit())


@dataclass(frozen=True)
class RequestExecutionContext:
    """Execution state owned by exactly one MCP tool request."""

    request_id: str
    executor: BlockingExecutor
    request_limiter: anyio.CapacityLimiter
    deadline_at: float


_request_execution_context: ContextVar[RequestExecutionContext | None] = ContextVar(
    "tradex_request_execution_context",
    default=None,
)


def current_execution_context() -> RequestExecutionContext | None:
    """Return the current request state, including inside worker threads."""

    return _request_execution_context.get()


def current_request_id() -> str | None:
    """Return the unique Tradex request id for logging and diagnostics."""

    context = current_execution_context()
    return context.request_id if context is not None else None


async def run_blocking(
    func: Callable[P, R],
    *args: P.args,
    _executor: BlockingExecutor | None = None,
    **kwargs: P.kwargs,
) -> R:
    """Run blocking work through the shared bounded execution gateway."""

    context = current_execution_context()
    executor = context.executor if context is not None else (_executor or _default_executor)
    inherited_deadline = current_deadline()
    deadline_at = (
        context.deadline_at
        if context is not None
        else inherited_deadline
        if inherited_deadline is not None
        else time.monotonic() + _configured_request_deadline()
    )
    if context is None:
        return await executor.run(func, *args, deadline_at=deadline_at, **kwargs)

    # Native async tools may fan out several blocking subcalls.  This request
    # limiter prevents one composite request from consuming the global pool.
    async with context.request_limiter:
        return await executor.run(func, *args, deadline_at=deadline_at, **kwargs)


def native_async(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Mark a tool as genuinely asynchronous so it remains on the MCP loop."""

    if not inspect.iscoroutinefunction(func):
        raise TypeError("@native_async can only be applied to async functions")
    setattr(func, _NATIVE_ASYNC_ATTRIBUTE, True)
    return func


async def _resolve_awaitable(awaitable: Awaitable[R]) -> R:
    return await awaitable


def _invoke_tool(func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Invoke a possibly fake-async tool entirely inside a worker thread."""

    result = func(*args, **kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(_resolve_awaitable(cast(Awaitable[Any], result)))
    return result


class IsolatedFastMCP(FastMCP):
    """FastMCP server with bounded blocking tools and request-local state."""

    def __init__(
        self,
        *args: Any,
        max_blocking_calls: int | None = None,
        max_request_blocking_calls: int = DEFAULT_MAX_REQUEST_BLOCKING_CALLS,
        request_deadline_seconds: float | None = None,
        **kwargs: Any,
    ) -> None:
        if max_request_blocking_calls <= 0:
            raise ValueError("max_request_blocking_calls must be greater than zero")

        if max_blocking_calls is None:
            configured_limit = _configured_blocking_limit()
            self._blocking_executor = (
                _default_executor
                if configured_limit == _default_executor.max_calls
                else BlockingExecutor(configured_limit)
            )
        else:
            self._blocking_executor = BlockingExecutor(max_blocking_calls)
        self._max_request_blocking_calls = max_request_blocking_calls
        self._request_deadline_seconds = (
            _configured_request_deadline()
            if request_deadline_seconds is None
            else float(request_deadline_seconds)
        )
        if self._request_deadline_seconds <= 0:
            raise ValueError("request_deadline_seconds must be greater than zero")
        super().__init__(*args, **kwargs)

    @property
    def max_blocking_calls(self) -> int:
        return self._blocking_executor.max_calls

    @property
    def blocking_statistics(self) -> anyio.CapacityLimiterStatistics:
        return self._blocking_executor.statistics

    def add_tool(self, fn: Any, *args: Any, **kwargs: Any) -> None:
        """Register a tool, offloading it unless explicitly marked native."""

        if getattr(fn, _NATIVE_ASYNC_ATTRIBUTE, False):
            super().add_tool(fn, *args, **kwargs)
            return

        original = fn

        @functools.wraps(original)
        async def isolated_tool(*tool_args: Any, **tool_kwargs: Any) -> Any:
            return await run_blocking(
                _invoke_tool,
                original,
                tool_args,
                tool_kwargs,
                _executor=self._blocking_executor,
            )

        super().add_tool(isolated_tool, *args, **kwargs)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Execute one tool inside a fresh request-local context."""

        deadline_at = time.monotonic() + self._request_deadline_seconds
        context = RequestExecutionContext(
            request_id=uuid4().hex,
            executor=self._blocking_executor,
            request_limiter=anyio.CapacityLimiter(self._max_request_blocking_calls),
            deadline_at=deadline_at,
        )
        token = _request_execution_context.set(context)
        try:
            with deadline_scope(deadline_at):
                request = asyncio.create_task(super().call_tool(name, arguments))
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(request),
                        timeout=max(0.0, deadline_at - time.monotonic()),
                    )
                except asyncio.TimeoutError as exc:
                    # A completed task raised its own TimeoutError; preserve it.
                    # Otherwise this wait exhausted the outer request budget.
                    if request.done():
                        return await request
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)
                    raise RequestDeadlineExceeded(
                        f"Tradex tool '{name}' exceeded the request deadline"
                    ) from exc
                except asyncio.CancelledError:
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)
                    raise
        finally:
            _request_execution_context.reset(token)
