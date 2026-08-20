"""Concurrency and request-isolation regression tests for the MCP executor."""

from __future__ import annotations

import asyncio
import ast
import functools
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from tradex.execution import (
    DEFAULT_MAX_BLOCKING_CALLS,
    IsolatedFastMCP,
    current_execution_context,
    current_request_id,
    native_async,
)


async def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout=timeout)


def _result_text(result) -> str:
    content, _structured = result
    return content[0].text


def _async_test(func):
    @functools.wraps(func)
    def run(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return run


@_async_test
async def test_fake_async_tool_is_offloaded_with_schema_and_context_preserved():
    mcp = IsolatedFastMCP("execution-test", max_blocking_calls=2)
    main_thread = threading.get_ident()
    observed: dict[str, object] = {}

    @mcp.tool()
    async def echo(tag: str, count: int = 1) -> str:
        """Return a tagged value."""
        observed["thread"] = threading.get_ident()
        observed["request_id"] = current_request_id()
        return f"{tag}:{count}"

    tools = await mcp.list_tools()
    tool = next(item for item in tools if item.name == "echo")
    assert tool.description == "Return a tagged value."
    assert tool.inputSchema["required"] == ["tag"]
    assert tool.inputSchema["properties"]["tag"]["type"] == "string"
    assert tool.inputSchema["properties"]["count"]["default"] == 1

    result = await mcp.call_tool("echo", {"tag": "alpha", "count": 3})
    assert _result_text(result) == "alpha:3"
    assert observed["thread"] != main_thread
    assert isinstance(observed["request_id"], str)
    assert current_execution_context() is None


@_async_test
async def test_native_async_tool_stays_on_event_loop():
    mcp = IsolatedFastMCP("native-test", max_blocking_calls=1)
    main_thread = threading.get_ident()
    observed: dict[str, object] = {}

    @mcp.tool()
    @native_async
    async def native_echo(tag: str) -> str:
        await asyncio.sleep(0)
        observed["thread"] = threading.get_ident()
        observed["request_id"] = current_request_id()
        return tag

    result = await mcp.call_tool("native_echo", {"tag": "native"})
    assert _result_text(result) == "native"
    assert observed["thread"] == main_thread
    assert isinstance(observed["request_id"], str)


@_async_test
async def test_slow_request_does_not_block_fast_request_or_share_context():
    mcp = IsolatedFastMCP("head-of-line-test", max_blocking_calls=2)
    slow_started = threading.Event()
    release_slow = threading.Event()
    request_ids: dict[str, str | None] = {}

    @mcp.tool()
    async def slow(tag: str) -> str:
        request_ids[tag] = current_request_id()
        slow_started.set()
        if not release_slow.wait(2):
            raise TimeoutError("test did not release slow tool")
        return tag

    @mcp.tool()
    async def fast(tag: str) -> str:
        request_ids[tag] = current_request_id()
        return tag

    slow_task = asyncio.create_task(mcp.call_tool("slow", {"tag": "slow"}))
    await _wait_until(slow_started.is_set)
    try:
        fast_result = await asyncio.wait_for(
            mcp.call_tool("fast", {"tag": "fast"}),
            timeout=1,
        )
        assert _result_text(fast_result) == "fast"
        assert not release_slow.is_set()
    finally:
        release_slow.set()

    slow_result = await asyncio.wait_for(slow_task, timeout=1)
    assert _result_text(slow_result) == "slow"
    assert request_ids["slow"] != request_ids["fast"]


@_async_test
async def test_global_blocking_concurrency_is_bounded():
    mcp = IsolatedFastMCP("limit-test", max_blocking_calls=2)
    entered: set[str] = set()
    entered_lock = threading.Lock()
    releases = {tag: threading.Event() for tag in ("a", "b", "c")}

    @mcp.tool()
    async def held(tag: str) -> str:
        with entered_lock:
            entered.add(tag)
        if not releases[tag].wait(2):
            raise TimeoutError(f"test did not release {tag}")
        return tag

    tasks = [
        asyncio.create_task(mcp.call_tool("held", {"tag": tag}))
        for tag in releases
    ]
    try:
        await _wait_until(
            lambda: len(entered) == 2
            and mcp.blocking_statistics.tasks_waiting == 1
        )
        assert mcp.blocking_statistics.borrowed_tokens == 2

        first = next(iter(entered))
        releases[first].set()
        await _wait_until(lambda: len(entered) == 3)
    finally:
        for release in releases.values():
            release.set()

    results = await asyncio.gather(*tasks)
    assert sorted(_result_text(result) for result in results) == ["a", "b", "c"]
    assert mcp.blocking_statistics.borrowed_tokens == 0


@_async_test
async def test_cancellation_drains_worker_before_releasing_capacity():
    mcp = IsolatedFastMCP("cancellation-test", max_blocking_calls=1)
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()

    @mcp.tool()
    async def held(tag: str) -> str:
        if tag == "first":
            first_started.set()
            release = release_first
        else:
            second_started.set()
            release = release_second
        if not release.wait(2):
            raise TimeoutError(f"test did not release {tag}")
        return tag

    first = asyncio.create_task(mcp.call_tool("held", {"tag": "first"}))
    await _wait_until(first_started.is_set)
    first.cancel()
    second = asyncio.create_task(mcp.call_tool("held", {"tag": "second"}))

    try:
        await _wait_until(
            lambda: second_started.is_set()
            or mcp.blocking_statistics.tasks_waiting == 1
        )
        assert not second_started.is_set()
        assert not first.done()

        release_first.set()
        await _wait_until(second_started.is_set)
        release_second.set()

        with pytest.raises(asyncio.CancelledError):
            await first
        assert _result_text(await second) == "second"
    finally:
        release_first.set()
        release_second.set()
        await asyncio.gather(first, second, return_exceptions=True)

    assert mcp.blocking_statistics.borrowed_tokens == 0


def test_blocking_limit_is_configurable_from_environment(monkeypatch):
    monkeypatch.setenv("TRADEX_MCP_MAX_BLOCKING_CALLS", "3")
    assert IsolatedFastMCP("configured-test").max_blocking_calls == 3

    monkeypatch.setenv("TRADEX_MCP_MAX_BLOCKING_CALLS", "invalid")
    assert (
        IsolatedFastMCP("fallback-test").max_blocking_calls
        == DEFAULT_MAX_BLOCKING_CALLS
    )


@_async_test
async def test_add_tool_keeps_fastmcp_public_fn_keyword_compatible():
    mcp = IsolatedFastMCP("keyword-api-test", max_blocking_calls=1)

    async def probe(value: str) -> str:
        return value

    mcp.add_tool(fn=probe)
    result = await mcp.call_tool("probe", {"value": "ok"})

    assert _result_text(result) == "ok"


def test_tradex_server_uses_stateless_streamable_http():
    from tradex.server import mcp

    assert isinstance(mcp, IsolatedFastMCP)
    assert mcp.settings.stateless_http is True


def test_awaiting_tools_are_explicit_and_use_the_bounded_gateway():
    """New true-async tools must not silently bypass request isolation."""

    tools_dir = Path(__file__).parents[1] / "src" / "tradex" / "tools"
    missing_marker: list[str] = []
    bypasses: list[str] = []

    for path in tools_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            is_tool = any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "mcp"
                and decorator.func.attr == "tool"
                for decorator in node.decorator_list
            )
            if not is_tool:
                continue

            has_await = any(isinstance(child, ast.Await) for child in ast.walk(node))
            has_native_marker = any(
                isinstance(decorator, ast.Name) and decorator.id == "native_async"
                for decorator in node.decorator_list
            )
            if has_await and not has_native_marker:
                missing_marker.append(f"{path.name}:{node.lineno}:{node.name}")

            for child in ast.walk(node):
                if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Attribute):
                    continue
                owner = child.func.value
                if (
                    child.func.attr == "to_thread"
                    and isinstance(owner, ast.Name)
                    and owner.id == "asyncio"
                ) or child.func.attr == "run_in_executor":
                    bypasses.append(f"{path.name}:{child.lineno}:{node.name}")

    assert missing_marker == []
    assert bypasses == []
