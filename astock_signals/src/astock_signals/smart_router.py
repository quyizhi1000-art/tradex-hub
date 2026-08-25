"""
请求隔离的数据源路由引擎。

核心逻辑：
1. 每次请求按注册时的固定 priority 获取不可变候选快照
2. 当前请求失败时只在该快照内降级，不改变后续请求的选源
3. 健康评分仅用于观测和看板，绝不参与路由排序或过滤
4. 请求校验、源能力和源繁忙错误显式分类，不污染源健康
5. 独占源（exclusive=True）失败不降级
"""

from __future__ import annotations

import contextvars
import functools
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

DEFAULT_ROUTE_DEADLINE_SECONDS = 30.0
DEFAULT_PROVIDER_DEADLINE_SECONDS = 15.0
DEFAULT_MAX_PROVIDER_CALLS = 8
ROUTE_DEADLINE_ENV = "TRADEX_ROUTE_DEADLINE_SECONDS"
PROVIDER_DEADLINE_ENV = "TRADEX_PROVIDER_DEADLINE_SECONDS"
MAX_PROVIDER_CALLS_ENV = "TRADEX_MAX_PROVIDER_CALLS"


def _configured_positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _configured_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_seconds(value: float, name: str) -> float:
    seconds = float(value)
    if seconds <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return seconds


class RequestValidationError(ValueError):
    """请求参数不符合 data_type 公共契约。

    这类错误与数据源健康无关，路由器会立即停止，不尝试其他源。
    """


class SourceCapabilityError(RuntimeError):
    """当前源不支持这个合法请求，只影响当前请求的降级。"""


class SourceBusyError(RuntimeError):
    """当前源的并发/限流预算已满，只影响当前请求的降级。"""


class SourcePayloadError(RuntimeError):
    """当前源的返回值未通过调用方的数据契约校验。"""


class RouteDeadlineExceeded(TimeoutError):
    """SmartRouter 的请求总 deadline 已耗尽。"""


class ProviderDeadlineExceeded(TimeoutError):
    """单个供应商调用（包括排队）超过本次调用的 deadline。"""

    def __init__(self, message: str, *, started: bool) -> None:
        super().__init__(message)
        self.started = started


SourceEntry = tuple[str, Callable[..., Any], int, bool]
ResultValidator = Callable[[Any, str], Any]

_current_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "tradex_current_deadline",
    default=None,
)


def current_deadline() -> float | None:
    """返回当前调用继承的绝对 monotonic deadline。"""

    return _current_deadline.get()


@contextmanager
def deadline_scope(deadline_at: float) -> Iterator[None]:
    """向线程和嵌套路由传播一个只会收紧的绝对 deadline。"""

    inherited = current_deadline()
    effective = min(deadline_at, inherited) if inherited is not None else deadline_at
    token = _current_deadline.set(effective)
    try:
        yield
    finally:
        _current_deadline.reset(token)


class _BoundedProviderExecutor:
    """用固定线程数执行供应商调用，并让排队计入总 deadline。

    Python/Windows 不能安全强杀正在执行的线程。调用方到期后立即返回，
    但 admission token 只在真实 Future 结束时释放，因此慢调用不会通过
    超时不断制造替代线程或无界队列。
    """

    def __init__(self, max_calls: int) -> None:
        if max_calls <= 0:
            raise ValueError("max_calls must be greater than zero")
        self._slots = threading.BoundedSemaphore(max_calls)
        self._pool = ThreadPoolExecutor(
            max_workers=max_calls,
            thread_name_prefix="tradex-provider",
        )

    def run(
        self,
        func: Callable[[], Any],
        *,
        deadline_at: float,
        label: str,
    ) -> Any:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0 or not self._slots.acquire(timeout=max(0.0, remaining)):
            raise ProviderDeadlineExceeded(
                f"{label} deadline expired while waiting for provider capacity",
                started=False,
            )

        context = contextvars.copy_context()
        call = functools.partial(context.run, func)
        try:
            future = self._pool.submit(call)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _future: self._slots.release())

        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            started = future.running()
            future.cancel()
            raise ProviderDeadlineExceeded(
                f"{label} deadline expired before provider execution",
                started=started,
            )
        try:
            return future.result(timeout=remaining)
        except FutureTimeoutError as exc:
            if future.done():
                return future.result()
            started = future.running()
            future.cancel()
            raise ProviderDeadlineExceeded(
                f"{label} exceeded its total provider deadline",
                started=started,
            ) from exc


@dataclass
class SourceHealth:
    """单个数据源的健康状态。"""
    name: str
    score: float = 100.0       # 0-100 健康评分
    total_calls: int = 0
    success_count: int = 0
    fail_count: int = 0
    avg_latency_ms: float = 0.0
    last_success_ts: float = 0.0
    last_fail_ts: float = 0.0
    consecutive_fails: int = 0
    _latency_window: list[float] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return self.success_count / self.total_calls

    @property
    def is_healthy(self) -> bool:
        return self.score >= 20.0 and self.consecutive_fails < 5

    def record_success(self, latency_ms: float):
        self.total_calls += 1
        self.success_count += 1
        self.consecutive_fails = 0
        self.last_success_ts = time.time()
        # 滑动窗口记录延迟（最近50次）
        self._latency_window.append(latency_ms)
        if len(self._latency_window) > 50:
            self._latency_window = self._latency_window[-50:]
        self.avg_latency_ms = sum(self._latency_window) / len(self._latency_window)
        # 成功恢复评分
        self.score = min(100.0, self.score + 5.0)
        # 延迟惩罚：>5s扣分，>10s重罚
        if latency_ms > 10000:
            self.score = max(0, self.score - 10)
        elif latency_ms > 5000:
            self.score = max(0, self.score - 5)

    def record_failure(self):
        self.total_calls += 1
        self.fail_count += 1
        self.consecutive_fails += 1
        self.last_fail_ts = time.time()
        # 失败惩罚：连续失败加重
        penalty = 20 * min(self.consecutive_fails, 5)
        self.score = max(0, self.score - penalty)
        # 连续5次失败直接归零
        if self.consecutive_fails >= 5:
            self.score = 0.0

    def recover(self, amount: float = 10.0):
        """定时恢复评分（用于间歇性故障的源）。"""
        if self.consecutive_fails == 0 and self.score < 100:
            self.score = min(100.0, self.score + amount)

    def to_dict(self) -> dict:
        """转换为字典（供看板使用）。"""
        return {
            "name": self.name,
            "score": round(self.score, 1),
            "success_rate": round(self.success_rate * 100, 1),
            "avg_latency_ms": round(self.avg_latency_ms, 0),
            "total_calls": self.total_calls,
            "fail_count": self.fail_count,
            "consecutive_fails": self.consecutive_fails,
            "last_success_ts": self.last_success_ts,
            "last_fail_ts": self.last_fail_ts,
            "is_healthy": self.is_healthy,
        }


class SmartRouter:
    """智能路由引擎。

    Usage:
        router = SmartRouter()
        router.register("quote", "akshare", akshare_fetch_fn, priority=1)
        router.register("quote", "tencent", tencent_fetch_fn, priority=2)
        result = router.route("quote", code="600519")

        # 独占源（失败不降级）
        router.register("auction", "eltdx", eltdx_fetch_fn, priority=1, exclusive=True)
        result = router.route("auction", code="600519")
    """

    def __init__(
        self,
        *,
        route_deadline_seconds: float | None = None,
        provider_deadline_seconds: float | None = None,
        max_provider_calls: int | None = None,
    ):
        # Copy-on-write tuples let every request retain a stable candidate snapshot.
        # _sources: data_type -> tuple[(source_name, fetch_fn, priority, exclusive), ...]
        self._sources: dict[str, tuple[SourceEntry, ...]] = {}
        self._health: dict[str, SourceHealth] = {}
        self._lock = threading.Lock()
        self._route_deadline_seconds = _positive_seconds(
            route_deadline_seconds
            if route_deadline_seconds is not None
            else _configured_positive_float(
                ROUTE_DEADLINE_ENV,
                DEFAULT_ROUTE_DEADLINE_SECONDS,
            ),
            "route_deadline_seconds",
        )
        self._provider_deadline_seconds = _positive_seconds(
            provider_deadline_seconds
            if provider_deadline_seconds is not None
            else _configured_positive_float(
                PROVIDER_DEADLINE_ENV,
                DEFAULT_PROVIDER_DEADLINE_SECONDS,
            ),
            "provider_deadline_seconds",
        )
        provider_limit = (
            max_provider_calls
            if max_provider_calls is not None
            else _configured_positive_int(
                MAX_PROVIDER_CALLS_ENV,
                DEFAULT_MAX_PROVIDER_CALLS,
            )
        )
        self._provider_executor = _BoundedProviderExecutor(provider_limit)

    def register(
        self,
        data_type: str,
        source_name: str,
        fetch_fn: Callable,
        priority: int = 100,
        exclusive: bool = False,
    ):
        """注册数据源。

        Args:
            data_type: 数据类型（如 "quote", "kline", "auction"）
            source_name: 数据源名称（如 "akshare", "eltdx", "tencent"）
            fetch_fn: 获取数据的可调用对象，接受 **kwargs，返回数据
            priority: 优先级（越小越高，1=主源, 100=备源, 200=兜底）
            exclusive: 是否独占源（True=失败不降级，直接返回错误）
        """
        key = f"{data_type}:{source_name}"
        entry: SourceEntry = (source_name, fetch_fn, priority, exclusive)
        with self._lock:
            current = self._sources.get(data_type, ())
            for existing in current:
                if existing[0] != source_name:
                    continue
                if existing == entry:
                    return
                raise ValueError(
                    f"Data source '{source_name}' is already registered for "
                    f"'{data_type}' with a different definition"
                )

            if key not in self._health:
                self._health[key] = SourceHealth(name=source_name)
            # Copy-on-write: existing route calls keep their original tuple.
            self._sources[data_type] = tuple(
                sorted((*current, entry), key=lambda item: item[2])
            )

    def route(
        self,
        data_type: str,
        *,
        deadline_seconds: float | None = None,
        provider_deadline_seconds: float | None = None,
        **kwargs,
    ) -> tuple[Any, str]:
        """请求隔离路由：按固定优先级快照选源，失败仅在本请求降级。

        独占源（exclusive=True）失败后不降级，直接 raise。

        Returns:
            (data, source_name) — 数据内容和来源名称
        Raises:
            RuntimeError: 所有数据源都失败（或独占源失败）
        """
        return self._route(
            data_type,
            validator=None,
            deadline_seconds=deadline_seconds,
            provider_deadline_seconds=provider_deadline_seconds,
            **kwargs,
        )

    def route_validated(
        self,
        data_type: str,
        validator: ResultValidator,
        *,
        deadline_seconds: float | None = None,
        provider_deadline_seconds: float | None = None,
        **kwargs,
    ) -> tuple[Any, str]:
        """路由并在记录成功前把源返回值转换为调用方的规范契约。

        校验或映射失败属于源返回值失败，会计入该源健康度并继续尝试
        当前请求的下一个候选源。请求参数校验仍应在进入本方法前完成，
        或由 fetcher 显式抛出 :class:`RequestValidationError`。
        """
        if not callable(validator):
            raise TypeError("validator must be callable")
        return self._route(
            data_type,
            validator=validator,
            deadline_seconds=deadline_seconds,
            provider_deadline_seconds=provider_deadline_seconds,
            **kwargs,
        )

    def _route(
        self,
        data_type: str,
        *,
        validator: ResultValidator | None,
        deadline_seconds: float | None,
        provider_deadline_seconds: float | None,
        **kwargs,
    ) -> tuple[Any, str]:
        route_window = _positive_seconds(
            deadline_seconds
            if deadline_seconds is not None
            else self._route_deadline_seconds,
            "deadline_seconds",
        )
        provider_window = _positive_seconds(
            provider_deadline_seconds
            if provider_deadline_seconds is not None
            else self._provider_deadline_seconds,
            "provider_deadline_seconds",
        )
        route_deadline = time.monotonic() + route_window
        inherited_deadline = current_deadline()
        if inherited_deadline is not None:
            route_deadline = min(route_deadline, inherited_deadline)

        # Take an immutable snapshot under the lock. Registration after this point
        # cannot alter the order or membership observed by the current request.
        with self._lock:
            candidates = self._sources.get(data_type, ())
        if not candidates:
            raise RuntimeError(f"No data source registered for '{data_type}'")

        errors = []
        legacy_validation_errors: list[ValueError] = []
        for source_name, fetch_fn, _priority, exclusive in candidates:
            if time.monotonic() >= route_deadline:
                raise RouteDeadlineExceeded(
                    f"SmartRouter deadline expired for '{data_type}'"
                )
            key = f"{data_type}:{source_name}"
            t0 = time.perf_counter()
            try:
                def invoke_provider() -> Any:
                    result = fetch_fn(**kwargs)
                    if validator is not None:
                        try:
                            return validator(result, source_name)
                        except Exception as exc:
                            raise SourcePayloadError(str(exc)) from exc
                    return result

                provider_deadline = min(
                    route_deadline,
                    time.monotonic() + provider_window,
                )
                with deadline_scope(provider_deadline):
                    result = self._provider_executor.run(
                        invoke_provider,
                        deadline_at=provider_deadline,
                        label=f"{data_type}:{source_name}",
                    )
                latency_ms = (time.perf_counter() - t0) * 1000
                with self._lock:
                    self._health[key].record_success(latency_ms)
                logger.debug(
                    "SmartRouter: %s via %s OK (%.0fms)", data_type, source_name, latency_ms
                )
                return result, source_name
            except RequestValidationError:
                logger.debug(
                    "SmartRouter: %s request validation rejected by %s",
                    data_type,
                    source_name,
                )
                raise
            except ValueError as exc:
                # Legacy fetchers use ValueError for both request validation
                # and provider parsing.  It is therefore unsafe to terminate
                # fallback here.  Keep it request-local and health-neutral;
                # explicit RequestValidationError remains the fail-fast path.
                legacy_validation_errors.append(exc)
                errors.append(f"{source_name}: {exc}")
                logger.info(
                    "SmartRouter: %s via %s raised legacy ValueError; "
                    "trying this request's next source: %s",
                    data_type,
                    source_name,
                    exc,
                )
                if exclusive:
                    raise RequestValidationError(str(exc)) from exc
            except (SourceCapabilityError, SourceBusyError) as exc:
                errors.append(f"{source_name}: {exc}")
                logger.info(
                    "SmartRouter: %s via %s skipped for this request: %s",
                    data_type,
                    source_name,
                    exc,
                )
                if exclusive:
                    raise
            except ProviderDeadlineExceeded as exc:
                if exc.started:
                    with self._lock:
                        self._health[key].record_failure()
                errors.append(f"{source_name}: {exc}")
                logger.warning(
                    "SmartRouter: %s via %s DEADLINE: %s",
                    data_type,
                    source_name,
                    exc,
                )
                if exclusive:
                    raise RuntimeError(
                        f"Exclusive source '{source_name}' for '{data_type}' "
                        f"exceeded its deadline: {exc}"
                    ) from exc
                if time.monotonic() >= route_deadline:
                    raise RouteDeadlineExceeded(
                        f"SmartRouter deadline expired for '{data_type}': "
                        f"{'; '.join(errors)}"
                    ) from exc
            except Exception as e:
                with self._lock:
                    self._health[key].record_failure()
                errors.append(f"{source_name}: {e}")
                logger.warning(
                    "SmartRouter: %s via %s FAILED: %s", data_type, source_name, e
                )
                # 独占源失败不降级，直接 raise
                if exclusive:
                    raise RuntimeError(
                        f"Exclusive source '{source_name}' for '{data_type}' failed: {e}"
                    ) from e

        if time.monotonic() >= route_deadline:
            raise RouteDeadlineExceeded(
                f"SmartRouter deadline expired for '{data_type}': "
                f"{'; '.join(errors)}"
            )

        if legacy_validation_errors and len(legacy_validation_errors) == len(candidates):
            first = legacy_validation_errors[0]
            raise RequestValidationError(str(first)) from first

        raise RuntimeError(
            f"All sources for '{data_type}' failed: {'; '.join(errors)}"
        )

    def get_health_report(self) -> list[dict]:
        """获取所有数据源的健康报告。"""
        report = []
        with self._lock:
            for key, health in self._health.items():
                report.append({
                    "source": key,
                    "score": round(health.score, 1),
                    "success_rate": round(health.success_rate * 100, 1),
                    "avg_latency_ms": round(health.avg_latency_ms, 0),
                    "total_calls": health.total_calls,
                    "fail_count": health.fail_count,
                    "consecutive_fails": health.consecutive_fails,
                    "last_success_ts": health.last_success_ts,
                    "last_fail_ts": health.last_fail_ts,
                    "is_healthy": health.is_healthy,
                })
        return sorted(report, key=lambda x: x["score"], reverse=True)

    def get_registry_report(self) -> list[dict]:
        """获取全量注册表（供看板使用）。

        返回每个注册的数据源及其优先级/独占标记/健康状态。
        """
        report = []
        with self._lock:
            for data_type, sources in self._sources.items():
                for source_name, _, priority, exclusive in sources:
                    key = f"{data_type}:{source_name}"
                    health = self._health.get(key)
                    report.append({
                        "data_type": data_type,
                        "source_name": source_name,
                        "priority": priority,
                        "exclusive": exclusive,
                        "health": health.to_dict() if health else None,
                    })
        # 按 data_type + priority 排序
        report.sort(key=lambda x: (x["data_type"], x["priority"]))
        return report

    def recover_all(self, amount: float = 10.0):
        """定时调用：为所有健康的源恢复评分。"""
        with self._lock:
            for health in self._health.values():
                health.recover(amount)


# 全局单例
_global_router: SmartRouter | None = None
_global_router_lock = threading.Lock()

def get_router() -> SmartRouter:
    global _global_router
    if _global_router is None:
        with _global_router_lock:
            if _global_router is None:
                _global_router = SmartRouter()
    return _global_router
