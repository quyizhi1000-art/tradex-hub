#!/usr/bin/env python3
"""Read or explicitly generate Tradex daily-stock-selection archives."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "http://127.0.0.1:8765"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _local_base_url(value: str) -> str:
    candidate = value.rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in LOCAL_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise argparse.ArgumentTypeError(
            "--base-url must be a credential-free local HTTP origin"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _bounded_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--limit must be an integer") from exc
    if not 1 <= limit <= 365:
        raise argparse.ArgumentTypeError("--limit must be between 1 and 365")
    return limit


def _positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--timeout must be a number") from exc
    if timeout <= 0 or timeout > 120:
        raise argparse.ArgumentTypeError("--timeout must be greater than 0 and at most 120")
    return timeout


def _positive_wait_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--wait-timeout must be a number") from exc
    if timeout <= 0 or timeout > 1800:
        raise argparse.ArgumentTypeError(
            "--wait-timeout must be greater than 0 and at most 1800"
        )
    return timeout


def _safe_error_message(payload: bytes) -> str:
    try:
        body = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "request failed without a structured error"
    if isinstance(body, dict):
        for key in ("message", "detail", "error"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return "request failed without a structured error"


def _request_json(request: Request, timeout: float) -> dict[str, Any]:
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except HTTPError as exc:
        message = _safe_error_message(exc.read())
        raise RuntimeError(f"HTTP {exc.code}: {message}") from exc
    except URLError as exc:
        raise RuntimeError(f"local Tradex service unavailable: {exc.reason}") from exc

    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Tradex service returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("Tradex service returned a non-object payload")
    return decoded


def _require_contract(payload: dict[str, Any], expected: str) -> dict[str, Any]:
    if payload.get("contract") != expected or payload.get("schema_version") != 1:
        raise RuntimeError(
            f"unexpected response contract: expected {expected} schema version 1"
        )
    return payload


def read_archive(
    base_url: str,
    *,
    trade_date: str | None,
    limit: int,
    timeout: float,
) -> dict[str, Any]:
    query: dict[str, str | int] = {"limit": limit}
    if trade_date:
        query["trade_date"] = trade_date
    url = f"{base_url}/api/daily-stock-selection/history?{urlencode(query)}"
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    return _require_contract(
        _request_json(request, timeout), "daily_stock_selection_archive.v1"
    )


def read_generation(base_url: str, *, timeout: float) -> dict[str, Any]:
    url = f"{base_url}/api/daily-stock-selection/generation"
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    return _require_contract(
        _request_json(request, timeout), "daily_stock_selection_generation.v1"
    )


def generate_result(
    base_url: str,
    *,
    timeout: float,
    wait_timeout: float = 900.0,
) -> dict[str, Any]:
    url = f"{base_url}/api/daily-stock-selection"
    request = Request(
        url,
        data=b"",
        headers={"Accept": "application/json", "Content-Length": "0"},
        method="POST",
    )
    generation = _require_contract(
        _request_json(request, timeout), "daily_stock_selection_generation.v1"
    )
    deadline = time.monotonic() + wait_timeout
    while generation.get("state") == "running":
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "daily stock selection is still running; read the generation status later"
            )
        time.sleep(1.0)
        generation = read_generation(base_url, timeout=timeout)
    if generation.get("state") == "failed":
        message = str(generation.get("error") or "daily stock selection failed")
        code = str(generation.get("failure_code") or "unknown")
        raise RuntimeError(f"generation failed ({code}): {message}")
    result = generation.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("generation completed without a selection result")
    return _require_contract(result, "daily_stock_selection_result.v1")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read Tradex daily-stock-selection archives. Generation is opt-in and "
            "writes through the local service."
        )
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        type=_local_base_url,
        help="credential-free local Tradex HTTP origin",
    )
    parser.add_argument(
        "--trade-date",
        help="archived trade date in YYYY-MM-DD form; validated by the service",
    )
    parser.add_argument("--limit", default=90, type=_bounded_limit)
    parser.add_argument("--timeout", default=15.0, type=_positive_timeout)
    parser.add_argument(
        "--wait-timeout",
        default=900.0,
        type=_positive_wait_timeout,
        help="maximum seconds to monitor an explicitly started background generation",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="explicitly generate the current eligible result (local write)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.generate and args.trade_date:
        parser.error("--generate cannot be combined with --trade-date")

    try:
        if args.generate:
            payload = generate_result(
                args.base_url,
                timeout=args.timeout,
                wait_timeout=args.wait_timeout,
            )
        else:
            payload = read_archive(
                args.base_url,
                trade_date=args.trade_date,
                limit=args.limit,
                timeout=args.timeout,
            )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
