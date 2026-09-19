"""TuShare Pro-compatible HTTP transport with local quota protection.

The provider token is deliberately loaded only at request time and is sent in
the JSON body required by the TuShare HTTP protocol.  It is never accepted in
the configured URL and never copied into exception text.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

import requests

from astock_signals.shared_rate_limit import (
    SharedRateLimitExceeded,
    SharedRateLimitUnavailable,
    consume_shared_rate_budget,
)
from astock_signals.smart_router import SourceBusyError

from ..config import config


class TushareConfigurationError(RuntimeError):
    """TuShare is missing required local configuration."""


class TushareAPIError(RuntimeError):
    """TuShare returned a failed or malformed response."""

    def __init__(self, code: int | None, *, request_id: str | None = None) -> None:
        self.code = code
        self.request_id = request_id
        detail = f"TuShare API 业务错误 code={code}"
        super().__init__(detail)


@dataclass(frozen=True)
class TushareResult:
    """Validated tabular payload plus non-secret provider metadata."""

    records: tuple[dict[str, Any], ...]
    request_id: str | None = None


_SESSION_LOCAL = threading.local()
_TOKEN_ASSIGNMENT = re.compile(r"^TUSHARE_TOKEN\s*=\s*(.+)$", re.IGNORECASE)
_API_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

_REALTIME_DAILY_PRODUCTS = {
    "rt_k": "a_share",
    "rt_index": "index",
    "rt_etf_k": "etf",
    "rt_sw_k": "shenwan",
}
_REALTIME_MINUTE_APIS = frozenset({"rt_min", "rt_min_daily"})
_AUCTION_APIS = frozenset({"stk_auction", "stk_auction_o"})


def _configured_value(env_name: str, config_name: str, default: Any = "") -> str:
    value = os.getenv(env_name)
    if value is not None:
        return value.strip()
    return str(getattr(config, config_name, default) or "").strip()


def _positive_int(env_name: str, config_name: str, default: int) -> int:
    raw = _configured_value(env_name, config_name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return value if value > 0 else default


_REQUEST_GATE = threading.BoundedSemaphore(
    _positive_int("TUSHARE_MAX_INFLIGHT", "TUSHARE_MAX_INFLIGHT", 4)
)


def _get_session() -> requests.Session:
    session = getattr(_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        # Provider requests must not silently inherit machine proxy credentials
        # or a proxy that can inspect the token-bearing request body.
        session.trust_env = False
        _SESSION_LOCAL.session = session
    return session


def _candidate_token_paths(raw_path: str) -> list[Path]:
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


def _parse_token_file(text: str) -> str:
    lines = [
        line.strip()
        for line in text.lstrip("\ufeff").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(lines) != 1:
        raise TushareConfigurationError("TuShare token 文件必须只包含一条 token")
    value = lines[0]
    assignment = _TOKEN_ASSIGNMENT.fullmatch(value)
    if assignment:
        value = assignment.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    if not value or any(char.isspace() for char in value):
        raise TushareConfigurationError("TuShare token 文件内容无效")
    return value


def get_token() -> str:
    """Load the token only from ``TUSHARE_TOKEN`` or its configured file."""

    inline = os.getenv("TUSHARE_TOKEN")
    if inline is not None:
        inline = inline.strip()
        if inline:
            if any(char.isspace() for char in inline):
                raise TushareConfigurationError("TUSHARE_TOKEN 格式无效")
            return inline

    configured_path = _configured_value(
        "TUSHARE_TOKEN_FILE", "TUSHARE_TOKEN_FILE"
    )
    if not configured_path:
        raise TushareConfigurationError(
            "未配置 TuShare token；请设置 TUSHARE_TOKEN 或 TUSHARE_TOKEN_FILE"
        )
    for candidate in _candidate_token_paths(configured_path):
        if not candidate.is_file():
            continue
        try:
            return _parse_token_file(candidate.read_text(encoding="utf-8-sig"))
        except OSError as exc:
            raise TushareConfigurationError("无法读取 TuShare token 文件") from exc
    raise TushareConfigurationError("TuShare token 文件不存在")


def is_enabled() -> bool:
    raw = os.getenv("TUSHARE_ENABLED")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(getattr(config, "TUSHARE_ENABLED", True))


def is_configured() -> bool:
    if not is_enabled():
        return False
    try:
        return bool(get_token())
    except TushareConfigurationError:
        return False


def primary_capabilities() -> frozenset[str]:
    raw = _configured_value(
        "TUSHARE_PRIMARY_CAPABILITIES",
        "TUSHARE_PRIMARY_CAPABILITIES",
        (
            "realtime_quote,historical_kline,auction_data,market_universe,"
            "etf_quotes,stock_fund_flow,dragon_tiger_market_day,"
            "minute_data,stock_selection_calendar,stock_selection_daily,"
            "stock_selection_daily_basic,stock_selection_master,"
            "stock_selection_technicals,"
            "stock_selection_financial_period,instrument_taxonomy,"
            "limit_up_daily_membership,limit_sentiment_daily"
        ),
    )
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def provides(capability: str) -> bool:
    return is_configured() and capability in primary_capabilities()


def _rate_policy(api_name: str) -> tuple[str, int]:
    product = _REALTIME_DAILY_PRODUCTS.get(api_name)
    if product is not None:
        return (
            f"paid:tushare:realtime_daily:{product}",
            _positive_int(
                "TUSHARE_REALTIME_DAILY_RATE_LIMIT_PER_MINUTE",
                "TUSHARE_REALTIME_DAILY_RATE_LIMIT_PER_MINUTE",
                50,
            ),
        )
    if api_name in _REALTIME_MINUTE_APIS:
        return (
            "paid:tushare:realtime_minute:a_share",
            _positive_int(
                "TUSHARE_REALTIME_MINUTE_RATE_LIMIT_PER_MINUTE",
                "TUSHARE_REALTIME_MINUTE_RATE_LIMIT_PER_MINUTE",
                500,
            ),
        )
    if api_name in _AUCTION_APIS:
        return (
            "paid:tushare:auction:a_share",
            _positive_int(
                "TUSHARE_AUCTION_RATE_LIMIT_PER_MINUTE",
                "TUSHARE_AUCTION_RATE_LIMIT_PER_MINUTE",
                500,
            ),
        )
    return (
        "paid:tushare:points",
        _positive_int(
            "TUSHARE_POINTS_RATE_LIMIT_PER_MINUTE",
            "TUSHARE_POINTS_RATE_LIMIT_PER_MINUTE",
            500,
        ),
    )


def _consume_rate_budget(api_name: str) -> None:
    bucket, limit = _rate_policy(api_name)
    try:
        consume_shared_rate_budget(bucket, limit)
    except (SharedRateLimitExceeded, SharedRateLimitUnavailable):
        raise SourceBusyError(
            "tushare shared rate budget is full; use this request's fallback"
        ) from None


def _root_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise TushareConfigurationError(
            "TUSHARE_BASE_URL 必须是无凭证、查询、片段或子路径的 HTTPS 根地址"
        )
    return f"https://{parsed.netloc}/"


def _fields_value(fields: str | Iterable[str] | None) -> str:
    if fields is None:
        return ""
    if isinstance(fields, str):
        values = [part.strip() for part in fields.split(",") if part.strip()]
    else:
        values = [str(part).strip() for part in fields if str(part).strip()]
    if any(not _API_NAME.fullmatch(value) for value in values):
        raise ValueError("TuShare fields 包含无效字段名")
    return ",".join(values)


def _validated_result(payload: Any, *, secret: str = "") -> TushareResult:
    if not isinstance(payload, Mapping):
        raise TushareAPIError(None)
    request_id_value = payload.get("request_id")
    request_id = (
        str(request_id_value).strip()
        if request_id_value not in (None, "")
        else None
    )
    if request_id and secret and secret in request_id:
        request_id = None
    code = payload.get("code")
    if not isinstance(code, int) or isinstance(code, bool) or code != 0:
        safe_code = code if isinstance(code, int) and not isinstance(code, bool) else None
        raise TushareAPIError(safe_code, request_id=request_id)

    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise TushareAPIError(0, request_id=request_id)
    fields = data.get("fields")
    items = data.get("items")
    if (
        not isinstance(fields, list)
        or any(not isinstance(field, str) or not field for field in fields)
        or len(fields) != len(set(fields))
        or not isinstance(items, list)
    ):
        raise TushareAPIError(0, request_id=request_id)

    records: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) != len(fields):
            raise TushareAPIError(0, request_id=request_id)
        records.append(dict(zip(fields, item)))
    return TushareResult(records=tuple(records), request_id=request_id)


def request(
    api_name: str,
    params: Mapping[str, Any] | None = None,
    fields: str | Iterable[str] | None = None,
    *,
    base_url: str | None = None,
) -> TushareResult:
    """POST one standard TuShare request to the configured HTTPS root."""

    normalized_api = str(api_name or "").strip()
    if not _API_NAME.fullmatch(normalized_api):
        raise ValueError("TuShare api_name 无效")
    if params is not None and not isinstance(params, Mapping):
        raise TypeError("TuShare params 必须是映射")

    configured_base = base_url or _configured_value(
        "TUSHARE_BASE_URL", "TUSHARE_BASE_URL", "https://api.tushare.pro"
    )
    url = _root_url(str(configured_base or "").strip())
    timeout = _positive_int("TUSHARE_TIMEOUT", "TUSHARE_TIMEOUT", 15)
    token = get_token()
    body = {
        "api_name": normalized_api,
        "token": token,
        "params": dict(params or {}),
        "fields": _fields_value(fields),
    }

    if not _REQUEST_GATE.acquire(blocking=False):
        raise SourceBusyError(
            "tushare request capacity is busy; use this request's fallback"
        )
    try:
        _consume_rate_budget(normalized_api)
        try:
            response = _get_session().post(
                url,
                json=body,
                headers={"Accept": "application/json"},
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise RuntimeError("TuShare API HTTP 请求失败") from None

        if response.status_code != 200:
            raise TushareAPIError(response.status_code)
        try:
            payload = response.json()
        except (TypeError, ValueError):
            raise TushareAPIError(None) from None
        return _validated_result(payload, secret=token)
    finally:
        _REQUEST_GATE.release()


_request = request


__all__ = [
    "TushareAPIError",
    "TushareConfigurationError",
    "TushareResult",
    "get_token",
    "is_configured",
    "is_enabled",
    "primary_capabilities",
    "provides",
    "request",
    "_request",
]
