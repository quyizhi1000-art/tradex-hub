"""Explicit read-only MCP tools for the Tradex and CNEquity data lakes.

These tools are deliberately not registered with SmartRouter: the local lakes
are historical/replay stores, not live quote fallbacks.  All lake-specific
imports happen inside factories so an unavailable optional runtime cannot
break unrelated Tradex tools at server startup.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, is_dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from ..utils.formatter import dict_to_json, error_response


_CN_SYMBOL = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")
_PREFIXED_SYMBOL = re.compile(r"^(SH|SZ|BJ)[.]?(\d{6})$")
_MAX_SYMBOLS = 50
_MAX_WINDOW_DAYS = 366


def _data_lake_root() -> Path:
    configured = os.environ.get("TRADEX_DATA_LAKE_ROOT", "").strip()
    return Path(configured or "~/.tradex/lake").expanduser().resolve()


def _cnequity_root() -> Path | None:
    configured = os.environ.get("CNEQUITY_DATA_ROOT", "").strip()
    return Path(configured).expanduser().resolve() if configured else None


def _catalog_for_root(root: Path):
    from tradex.data_lake.catalog import Catalog

    return Catalog(root / "meta" / "catalog.sqlite3", read_only=True)


def _bridge_for_root(root: Path):
    from tradex.data_lake.cnequity_bridge import CNEquityBridge

    return CNEquityBridge(root)


def _replay_for_root(root: Path):
    catalog_path = root / "meta" / "catalog.sqlite3"
    if not catalog_path.is_file():
        raise FileNotFoundError(
            f"Tradex 数据湖目录中没有已初始化的 catalog: {catalog_path}。"
            "请先运行独立数据湖采集器完成至少一次采集。"
        )
    from tradex.data_lake.replay import ReplayEngine

    return ReplayEngine(root)


def _close_catalog(owner: Any) -> None:
    catalog = getattr(owner, "catalog", owner)
    close = getattr(catalog, "close", None)
    if callable(close):
        close()


def _canonical_symbol(raw: str) -> str:
    value = str(raw or "").strip().upper()
    match = _CN_SYMBOL.fullmatch(value)
    if match:
        return value
    prefixed = _PREFIXED_SYMBOL.fullmatch(value)
    if prefixed:
        return f"{prefixed.group(2)}.{prefixed.group(1)}"
    if len(value) == 6 and value.isdigit():
        if value.startswith("6"):
            exchange = "SH"
        elif value.startswith(("4", "8", "9")):
            exchange = "BJ"
        elif value.startswith(("0", "1", "2", "3")):
            exchange = "SZ"
        else:
            raise ValueError(f"无法判断股票代码 {raw!r} 的交易所")
        return f"{value}.{exchange}"
    raise ValueError(
        f"股票代码格式无效: {raw!r}；请使用 600519、SH600519 或 600519.SH"
    )


def _symbols(value: str) -> list[str]:
    parts = [part.strip() for part in str(value or "").split(",") if part.strip()]
    if not parts:
        raise ValueError("symbols 不能为空，请提供一个或多个逗号分隔的 A 股代码")
    normalized = list(dict.fromkeys(_canonical_symbol(part) for part in parts))
    if len(normalized) > _MAX_SYMBOLS:
        raise ValueError(f"symbols 最多允许 {_MAX_SYMBOLS} 个代码")
    return normalized


def _date_window(start: str, end: str) -> tuple[str, str]:
    try:
        start_date = date.fromisoformat(str(start or "").strip())
        end_date = date.fromisoformat(str(end or "").strip())
    except ValueError as exc:
        raise ValueError("start 和 end 必须使用 YYYY-MM-DD 格式") from exc
    if start_date > end_date:
        raise ValueError("start 不能晚于 end")
    if (end_date - start_date).days + 1 > _MAX_WINDOW_DAYS:
        raise ValueError(f"单次查询窗口最多 {_MAX_WINDOW_DAYS} 个自然日")
    return start_date.isoformat(), end_date.isoformat()


def _capture_summary(capture: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if capture is None:
        return None
    summary = {
        key: capture.get(key)
        for key in (
            "capture_id",
            "status",
            "observed_at",
            "trade_date",
            "minute_bucket",
            "market_phase",
            "collector_version",
            "completed_at",
        )
        if key in capture
    }
    artifacts = list(capture.get("artifacts") or [])
    features = list(capture.get("features") or [])
    decisions = list(capture.get("decisions") or [])
    if "artifacts" in capture:
        summary["artifact_count"] = len(artifacts)
        summary["datasets"] = sorted(
            str(item.get("dataset"))
            for item in artifacts
            if isinstance(item, Mapping) and item.get("dataset")
        )
    if "features" in capture:
        summary["features"] = [
            {
                key: item.get(key)
                for key in ("feature_id", "name", "schema_version", "config_version", "code_sha")
                if key in item
            }
            for item in features
            if isinstance(item, Mapping)
        ]
    if "decisions" in capture:
        summary["decisions"] = [
            {
                key: item.get(key)
                for key in (
                    "decision_id",
                    "mode",
                    "policy_version",
                    "offense_weight",
                    "defense_weight",
                    "cash_weight",
                    "confidence",
                    "abstain_reason",
                )
                if key in item
            }
            for item in decisions
            if isinstance(item, Mapping)
        ]
    return summary


def _replay_result(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    fields = ("capture_id", "stored_hash", "recomputed_hash", "match", "versions")
    if all(hasattr(value, field) for field in fields):
        return {field: getattr(value, field) for field in fields}
    raise TypeError("回放引擎返回了不支持的结果类型")


def register(mcp) -> None:
    """Register three explicit read-only lake tools."""

    @mcp.tool()
    async def get_data_lake_status() -> str:
        """返回 Tradex 数据湖最近一次完整发布及 CNEquity 日线代际。"""

        lake_root = _data_lake_root()
        catalog_path = lake_root / "meta" / "catalog.sqlite3"
        lake_status: dict[str, Any] = {
            "root": str(lake_root),
            "catalog_path": str(catalog_path),
            "catalog_exists": catalog_path.is_file(),
            "latest_completed": None,
        }
        if catalog_path.is_file():
            catalog = None
            try:
                catalog = _catalog_for_root(lake_root)
                latest = catalog.latest_completed()
                lake_status["latest_completed"] = _capture_summary(latest)
            except Exception as exc:  # noqa: BLE001 - isolate optional local state
                lake_status["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                if catalog is not None:
                    _close_catalog(catalog)
        else:
            lake_status["message"] = "尚无 catalog；请先运行独立数据湖采集器。"

        cn_root = _cnequity_root()
        cnequity_status: dict[str, Any] = {
            "configured": cn_root is not None,
            "data_root": str(cn_root) if cn_root is not None else None,
        }
        if cn_root is None:
            cnequity_status["message"] = (
                "未配置 CNEQUITY_DATA_ROOT；设置为 CNEquity 配置中的 [data].root 后"
                "可查看 daily_bars 代际。"
            )
        else:
            try:
                bridge = _bridge_for_root(cn_root)
                generation = bridge.describe_generation("daily_bars")
                # A status call should not return every file path in a large lake.
                cnequity_status["generation"] = {
                    key: value for key, value in generation.items() if key != "files"
                }
            except Exception as exc:  # noqa: BLE001 - Tradex status remains usable
                cnequity_status["error"] = f"{type(exc).__name__}: {exc}"

        return dict_to_json({
            "read_only": True,
            "tradex": lake_status,
            "cnequity": cnequity_status,
        })

    @mcp.tool()
    async def get_lake_daily_bars(
        symbols: str,
        start: str,
        end: str,
        adjust: str | None = None,
        strict_adj: bool = True,
        limit: int = 1000,
    ) -> str:
        """从 CNEquity 基础湖读取日线，并明确报告截断信息。

        Args:
            symbols: 逗号分隔的股票代码，例如 ``600519.SH,000001.SZ``。
            start: 起始交易日，YYYY-MM-DD，包含边界。
            end: 结束交易日，YYYY-MM-DD，包含边界。
            adjust: ``qfq``、``hfq`` 或 None。
            strict_adj: 复权因子不完整时是否直接失败。
            limit: 返回行数，1 到 5000；total 始终是截断前总行数。
        """

        tool_name = "get_lake_daily_bars"
        try:
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
                raise ValueError("limit 必须是 1 到 5000 之间的整数")
            normalized_adjust = (
                str(adjust).strip().lower() if adjust is not None and str(adjust).strip() else None
            )
            if normalized_adjust not in {None, "qfq", "hfq"}:
                raise ValueError("adjust 只能是 qfq、hfq 或留空")
            normalized_symbols = _symbols(symbols)
            start_date, end_date = _date_window(start, end)
            cn_root = _cnequity_root()
            if cn_root is None:
                raise RuntimeError(
                    "未配置 CNEQUITY_DATA_ROOT。请将它设置为 CNEquity 配置文件中的 "
                    "[data].root（例如 G:\\CNEquity\\data\\cnequity），并先完成基础湖回填。"
                )
            bridge = _bridge_for_root(cn_root)
            rows = bridge.load_daily_bars(
                normalized_symbols,
                start_date,
                end_date,
                adjust=normalized_adjust,
                strict_adj=bool(strict_adj),
            )
            total = len(rows)
            returned_rows = rows[:limit]
            return dict_to_json({
                "dataset": "daily_bars",
                "source": "cnequity",
                "data_root": str(cn_root),
                "symbols": normalized_symbols,
                "start": start_date,
                "end": end_date,
                "adjust": normalized_adjust,
                "strict_adj": bool(strict_adj),
                "limit": limit,
                "total": total,
                "returned": len(returned_rows),
                "truncated": total > len(returned_rows),
                "rows": returned_rows,
            })
        except Exception as exc:  # noqa: BLE001 - isolate optional CNEquity runtime
            return error_response(f"读取 CNEquity 日线失败: {exc}", tool_name)

    @mcp.tool()
    async def replay_lake_capture(capture_id: str) -> str:
        """仅使用已发布对象重建指定 capture，并比较持久化哈希。"""

        tool_name = "replay_lake_capture"
        try:
            normalized_id = str(capture_id or "").strip()
            if not normalized_id:
                raise ValueError("capture_id 不能为空")
            engine = _replay_for_root(_data_lake_root())
            try:
                result = engine.rebuild(normalized_id)
            finally:
                _close_catalog(engine)
            return dict_to_json(_replay_result(result))
        except Exception as exc:  # noqa: BLE001 - isolate local replay failures
            return error_response(f"数据湖回放失败: {exc}", tool_name)


__all__ = ["register"]
