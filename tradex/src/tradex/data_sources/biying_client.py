"""必盈 REST 客户端：许可证加载、限流、并发隔离与错误脱敏。"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

import requests

from astock_signals.shared_rate_limit import (
    SharedRateLimitExceeded,
    SharedRateLimitUnavailable,
    consume_shared_rate_budget,
)
from astock_signals.smart_router import SourceBusyError

from ..config import config


class BiyingConfigurationError(RuntimeError):
    """必盈客户端缺少或无法读取配置。"""


class BiyingAPIError(RuntimeError):
    """必盈返回了失败状态或无效响应。"""


class BiyingDailyQuotaExceeded(BiyingAPIError):
    """The configured licence exhausted its provider-reported daily quota."""


class BiyingLicenceInvalid(BiyingAPIError):
    """The provider rejected the configured licence."""


class BiyingUpstreamThrottled(BiyingAPIError):
    """The upstream returned a throttle response without a safe daily code."""


_SESSION_LOCAL = threading.local()
_LICENCE_ASSIGNMENT = re.compile(
    r"^(?:BIYING_LICENCE|BIYING_KEY|LICENCE|LICENSE)\s*=\s*(.+)$",
    re.IGNORECASE,
)


def _configured_value(env_name: str, config_name: str) -> str:
    value = os.getenv(env_name)
    if value is not None:
        return value.strip()
    return str(getattr(config, config_name, "") or "").strip()


def _positive_int(env_name: str, config_name: str, default: int) -> int:
    raw = _configured_value(env_name, config_name)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return value if value > 0 else default


_REQUEST_GATE = threading.BoundedSemaphore(
    _positive_int("BIYING_MAX_INFLIGHT", "BIYING_MAX_INFLIGHT", 4)
)


def _get_session() -> requests.Session:
    session = getattr(_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        _SESSION_LOCAL.session = session
    return session


def _candidate_licence_paths(raw_path: str) -> list[Path]:
    expanded = Path(os.path.expandvars(os.path.expanduser(raw_path)))
    if expanded.is_absolute():
        return [expanded]
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


def _parse_licence_file(text: str) -> str:
    lines = [
        line.strip()
        for line in text.lstrip("\ufeff").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(lines) != 1:
        raise BiyingConfigurationError("必盈许可证文件必须只包含一条许可证")
    value = lines[0]
    assignment = _LICENCE_ASSIGNMENT.fullmatch(value)
    if assignment:
        value = assignment.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    if not value or any(char.isspace() for char in value):
        raise BiyingConfigurationError("必盈许可证文件内容无效")
    return value


def get_licence() -> str:
    """读取许可证；任何异常都不包含许可证内容或请求 URL。"""
    inline = _configured_value("BIYING_LICENCE", "BIYING_LICENCE")
    if inline:
        if any(char.isspace() for char in inline):
            raise BiyingConfigurationError("BIYING_LICENCE 格式无效")
        return inline
    configured_path = _configured_value(
        "BIYING_LICENCE_FILE", "BIYING_LICENCE_FILE"
    )
    if not configured_path:
        raise BiyingConfigurationError(
            "未配置必盈许可证；请设置 BIYING_LICENCE 或 BIYING_LICENCE_FILE"
        )
    for candidate in _candidate_licence_paths(configured_path):
        if not candidate.is_file():
            continue
        try:
            return _parse_licence_file(candidate.read_text(encoding="utf-8-sig"))
        except OSError as exc:
            raise BiyingConfigurationError("无法读取必盈许可证文件") from exc
    raise BiyingConfigurationError("必盈许可证文件不存在")


def is_enabled() -> bool:
    raw = os.getenv("BIYING_ENABLED")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(getattr(config, "BIYING_ENABLED", True))


def is_configured() -> bool:
    if not is_enabled():
        return False
    try:
        return bool(get_licence())
    except BiyingConfigurationError:
        return False


def primary_capabilities() -> frozenset[str]:
    raw = _configured_value(
        "BIYING_PRIMARY_CAPABILITIES", "BIYING_PRIMARY_CAPABILITIES"
    )
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def provides(capability: str) -> bool:
    return is_configured() and capability in primary_capabilities()


def _consume_rate_budget() -> None:
    limit = _positive_int(
        "BIYING_RATE_LIMIT_PER_MINUTE", "BIYING_RATE_LIMIT_PER_MINUTE", 300
    )
    try:
        consume_shared_rate_budget("paid:biying:standard", limit)
    except (SharedRateLimitExceeded, SharedRateLimitUnavailable):
        raise SourceBusyError(
            "biying shared rate budget is full; use this request's fallback"
        ) from None


def _normalise_segments(path: str | Iterable[str]) -> list[str]:
    if isinstance(path, str):
        segments = [part for part in path.strip("/").split("/") if part]
    else:
        segments = [str(part).strip("/") for part in path if str(part).strip("/")]
    if not segments:
        raise ValueError("必盈 API 路径不能为空")
    return segments


def _business_code(payload: Any) -> int | None:
    if not isinstance(payload, dict) or "code" not in payload:
        return None
    try:
        return int(payload.get("code"))
    except (TypeError, ValueError):
        return None


def _raise_business_error(code: int) -> None:
    if code == 101:
        raise BiyingDailyQuotaExceeded("必盈 API 当日证书额度已用尽")
    if code == 102:
        raise BiyingLicenceInvalid("必盈 API 证书无效")
    raise BiyingAPIError(f"必盈 API 返回业务错误: {code}")


def request(
    path: str | Iterable[str],
    params: dict[str, Any] | None = None,
    *,
    base_url: str | None = None,
) -> Any:
    """执行一次 GET；许可证仅在本函数内部作为最后一个路径段拼接。"""
    configured_base = base_url or _configured_value(
        "BIYING_BASE_URL", "BIYING_BASE_URL"
    )
    configured_base = str(configured_base or "").rstrip("/")
    if not configured_base:
        raise BiyingConfigurationError("BIYING_BASE_URL 不能为空")
    parsed_base = urlsplit(configured_base)
    if (
        parsed_base.scheme.lower() != "https"
        or not parsed_base.netloc
        or parsed_base.username is not None
        or parsed_base.password is not None
        or parsed_base.query
        or parsed_base.fragment
    ):
        raise BiyingConfigurationError("BIYING_BASE_URL 必须是无凭证、查询或片段的 HTTPS 地址")

    encoded_path = "/".join(quote(part, safe=".") for part in _normalise_segments(path))
    # 不得记录、返回或把这个 URL 交给 requests 的 raise_for_status。
    url = f"{configured_base}/{encoded_path}/{quote(get_licence(), safe='')}"
    timeout = _positive_int("BIYING_TIMEOUT", "BIYING_TIMEOUT", 15)

    if not _REQUEST_GATE.acquire(blocking=False):
        raise SourceBusyError(
            "biying request capacity is busy; use this request's fallback"
        )
    try:
        _consume_rate_budget()
        try:
            response = _get_session().get(
                url,
                params=params or None,
                headers={"Accept": "application/json"},
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise RuntimeError("必盈 API HTTP 请求失败") from None

        if response.status_code != 200:
            try:
                error_payload = response.json()
            except (TypeError, ValueError):
                error_payload = None
            if (code := _business_code(error_payload)) is not None:
                _raise_business_error(code)
            if response.status_code == 429:
                raise BiyingUpstreamThrottled(
                    "必盈 API 证书额度或上游限流已触发: HTTP 429"
                )
            raise BiyingAPIError(f"必盈 API HTTP 状态异常: {response.status_code}")
        try:
            payload = response.json()
        except (TypeError, ValueError):
            raise BiyingAPIError("必盈 API 返回了无效 JSON") from None
        if not isinstance(payload, (dict, list)):
            raise BiyingAPIError("必盈 API 根响应类型无效")
        # 部分失败响应仍使用 HTTP 200；避免把服务端回显的 licence/message 带出。
        code = _business_code(payload)
        if (
            code is not None
            and ("message" in payload or "msg" in payload)
            and code not in (0, 200)
        ):
            _raise_business_error(code)
        return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
    finally:
        _REQUEST_GATE.release()


_request = request

__all__ = [
    "BiyingAPIError",
    "BiyingConfigurationError",
    "BiyingDailyQuotaExceeded",
    "BiyingLicenceInvalid",
    "BiyingUpstreamThrottled",
    "get_licence",
    "is_configured",
    "is_enabled",
    "primary_capabilities",
    "provides",
    "request",
    "_request",
]
