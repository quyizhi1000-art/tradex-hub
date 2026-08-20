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
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, ParamSpec, TypeVar, cast
from uuid import uuid4

import anyio
from mcp.server.fastmcp import FastMCP


P = ParamSpec("P")
R = TypeVar("R")

DEFAULT_MAX_BLOCKING_CALLS = 8
DEFAULT_MAX_REQUEST_BLOCKING_CALLS = 2
MAX_BLOCKING_CALLS_ENV = "TRADEX_MCP_MAX_BLOCKING_CALLS"
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


class BlockingExecutor:
    """Run synchronous work in a bounded AnyIO worker pool.

    Cancellation is deliberately delayed until the worker returns.  Python
    cannot safely stop a running thread; abandoning it would let cancelled MCP
    requests continue mutating caches and source-health state while also
    allowing replacement workers to grow without a real bound.
    """

    def __init__(self, max_calls: int) -> None:
        if max_calls <= 0:
            raise ValueError("max_calls must be greater than zero")
        self._max_calls = max_calls
        self._limiter = anyio.CapacityLimiter(max_calls)
        self._thread_limiter = anyio.CapacityLimiter(max_calls)

    @property
    def max_calls(self) -> int:
        return self._max_calls

    @property
    def statistics(self) -> anyio.CapacityLimiterStatistics:
        return self._limiter.statistics()

    async def run(
        self,
        func: Callable[P, R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        call = functools.partial(func, *args, **kwargs)
        # Acquire our service capacity before starting a worker.  A request
        # cancelled while still queued can then leave immediately without ever
        # running its obsolete function.
        async with self._limiter:
            worker = asyncio.create_task(
                anyio.to_thread.run_sync(
                    call,
                    abandon_on_cancel=False,
                    limiter=self._thread_limiter,
                )
            )
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError as cancellation:
                # ``asyncio.wait_for`` and direct Task.cancel() can bypass
                # AnyIO's shielding. Drain a started worker before propagating
                # cancellation so it never becomes an untracked orphan.
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue

                if not worker.cancelled():
                    try:
                        worker.result()
                    except BaseException:
                        # Cancellation of the MCP request takes precedence, but
                        # consume the worker exception to avoid task warnings.
                        pass
                raise cancellation


_default_executor = BlockingExecutor(_configured_blocking_limit())


@dataclass(frozen=True)
class RequestExecutionContext:
    """Execution state owned by exactly one MCP tool request."""

    request_id: str
    executor: BlockingExecutor
    request_limiter: anyio.CapacityLimiter


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
    if context is None:
        return await executor.run(func, *args, **kwargs)

    # Native async tools may fan out several blocking subcalls.  This request
    # limiter prevents one composite request from consuming the global pool.
    async with context.request_limiter:
        return await executor.run(func, *args, **kwargs)


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

        context = RequestExecutionContext(
            request_id=uuid4().hex,
            executor=self._blocking_executor,
            request_limiter=anyio.CapacityLimiter(self._max_request_blocking_calls),
        )
        token = _request_execution_context.set(context)
        try:
            return await super().call_tool(name, arguments)
        finally:
            _request_execution_context.reset(token)
