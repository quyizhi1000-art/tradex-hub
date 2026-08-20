"""同花顺扶摇金融数据 REST 客户端。

只负责服务端鉴权、HTTP 传输和统一 ``ApiResponse`` 信封校验。
字段适配留给 :mod:`tradex.data_sources.fuyao_fetchers`。
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

import requests

from astock_signals.smart_router import SourceBusyError

from ..config import config


class FuyaoConfigurationError(RuntimeError):
    """扶摇客户端缺少或无法读取本地配置。"""


class FuyaoAPIError(RuntimeError):
    """扶摇 API 返回了非成功业务码。"""

    def __init__(
        self,
        code: int | None,
        message: str,
        request_id: str = "",
    ) -> None:
        self.code = code
        self.request_id = request_id
        detail = f"同花顺扶摇 API 错误 code={code}: {message or 'unknown error'}"
        if request_id:
            detail += f" (request_id={request_id})"
        super().__init__(detail)


_SESSION_LOCAL = threading.local()


def _configured_max_inflight() -> int:
    raw = os.getenv("FUYAO_MAX_INFLIGHT", "4").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 4
    return value if value > 0 else 4


_REQUEST_GATE = threading.BoundedSemaphore(_configured_max_inflight())

_KEY_ASSIGNMENT = re.compile(
    r"^(?:HITHINK_FINANCE_API_KEY|FUYAO_API_KEY|THS_FUYAO_API_KEY|API_KEY)"
    r"\s*=\s*(.+)$",
    re.IGNORECASE,
)


def _configured_value(env_name: str, config_name: str) -> str:
    """Read a value lazily so tests and long-running hosts can override it."""
    env_value = os.getenv(env_name)
    if env_value is not None:
        return env_value.strip()
    return str(getattr(config, config_name, "") or "").strip()


def _get_session() -> requests.Session:
    """Return a Session owned by the current worker thread.

    Tradex executes blocking provider calls in worker threads.  Sharing a
    mutable ``requests.Session`` between those workers can leak connection,
    cookie, or adapter state across concurrent MCP requests, so each worker
    keeps its own connection pool instead.
    """
    session = getattr(_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        _SESSION_LOCAL.session = session
    return session


def _candidate_key_paths(raw_path: str) -> list[Path]:
    expanded = Path(os.path.expandvars(os.path.expanduser(raw_path)))
    if expanded.is_absolute():
        return [expanded]

    # Relative paths in tradex/.env are naturally relative to the tradex
    # project directory.  Keep cwd as a convenience for direct CLI use.
    project_dir = Path(__file__).resolve().parents[3]
    repo_dir = project_dir.parent
    candidates = [Path.cwd() / expanded, project_dir / expanded, repo_dir / expanded]
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        marker = os.path.normcase(str(candidate.resolve(strict=False)))
        if marker not in seen:
            seen.add(marker)
            unique.append(candidate)
    return unique


def _parse_key_file(text: str) -> str:
    lines = [
        line.strip()
        for line in text.lstrip("\ufeff").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(lines) != 1:
        raise FuyaoConfigurationError("同花顺扶摇 API Key 文件必须只包含一条密钥")

    value = lines[0]
    assignment = _KEY_ASSIGNMENT.fullmatch(value)
    if assignment:
        value = assignment.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    if not value or any(char.isspace() for char in value):
        raise FuyaoConfigurationError("同花顺扶摇 API Key 文件内容无效")
    return value


def get_api_key() -> str:
    """Return the configured key without logging or embedding it in errors."""
    official_key = os.getenv("HITHINK_FINANCE_API_KEY")
    if official_key is not None:
        official_key = official_key.strip()
        if official_key:
            if any(char.isspace() for char in official_key):
                raise FuyaoConfigurationError(
                    "HITHINK_FINANCE_API_KEY 格式无效"
                )
            return official_key

    inline_key = _configured_value("FUYAO_API_KEY", "FUYAO_API_KEY")
    if inline_key:
        if any(char.isspace() for char in inline_key):
            raise FuyaoConfigurationError("FUYAO_API_KEY 格式无效")
        return inline_key

    configured_path = _configured_value(
        "FUYAO_API_KEY_FILE", "FUYAO_API_KEY_FILE"
    )
    if not configured_path:
        raise FuyaoConfigurationError(
            "未配置同花顺扶摇 API Key；请设置 HITHINK_FINANCE_API_KEY、"
            "FUYAO_API_KEY 或 FUYAO_API_KEY_FILE"
        )

    for candidate in _candidate_key_paths(configured_path):
        if not candidate.is_file():
            continue
        try:
            return _parse_key_file(candidate.read_text(encoding="utf-8-sig"))
        except OSError as exc:
            raise FuyaoConfigurationError(
                "无法读取同花顺扶摇 API Key 文件"
            ) from exc
    raise FuyaoConfigurationError("同花顺扶摇 API Key 文件不存在")


def is_configured() -> bool:
    """Whether a usable-looking key is available for conditional registration."""
    try:
        return bool(get_api_key())
    except FuyaoConfigurationError:
        return False


def request(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Perform one GET request and return the validated ``data`` object.

    The service reports business failures inside HTTP 200 responses, so both
    the transport status and the envelope ``code`` are mandatory checks.
    """
    base_url = _configured_value("FUYAO_BASE_URL", "FUYAO_BASE_URL").rstrip("/")
    if not base_url:
        raise FuyaoConfigurationError("FUYAO_BASE_URL 不能为空")

    timeout = int(getattr(config, "FUYAO_TIMEOUT", 15) or 15)
    env_timeout = os.getenv("FUYAO_TIMEOUT")
    if env_timeout is not None:
        try:
            timeout = int(env_timeout)
        except ValueError:
            timeout = 15
    if timeout <= 0:
        timeout = 15

    url = f"{base_url}/{path.lstrip('/')}"
    headers = {
        "Accept": "application/json",
        "X-api-key": get_api_key(),
    }
    if not _REQUEST_GATE.acquire(blocking=False):
        raise SourceBusyError(
            "ths_fuyao request capacity is busy; use this request's fallback"
        )
    try:
        try:
            response = _get_session().get(
                url,
                params=params or None,
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            # Never include request headers (and therefore never the key) here.
            raise RuntimeError(f"同花顺扶摇 API HTTP 请求失败: {exc}") from exc

        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise RuntimeError("同花顺扶摇 API 返回了无效 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("同花顺扶摇 API 根响应不是对象")

        code = payload.get("code")
        message = str(payload.get("message") or "")
        request_id = str(payload.get("request_id") or "")
        if not isinstance(code, int) or isinstance(code, bool) or code != 0:
            valid_code = code if isinstance(code, int) and not isinstance(code, bool) else None
            raise FuyaoAPIError(valid_code, message, request_id)

        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("同花顺扶摇 API 成功响应缺少有效 data")
        return data
    finally:
        _REQUEST_GATE.release()


# Internal-style alias kept explicit for fetcher tests and other data-source
# modules; public callers should prefer ``request``.
_request = request


__all__ = [
    "FuyaoAPIError",
    "FuyaoConfigurationError",
    "get_api_key",
    "is_configured",
    "_request",
    "request",
]
