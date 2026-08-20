"""Prevent new Tradex cross-layer imports while legacy debt is removed gradually."""

from __future__ import annotations

import ast
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "tradex" / "src" / "tradex"
BASELINE_PATH = Path(__file__).with_name("architecture_boundary_baseline.json")


@dataclass(frozen=True)
class ImportRecord:
    source: str
    source_module: str
    imported_module: str
    imported_name: str | None

    @property
    def target(self) -> str:
        if self.imported_name:
            return f"{self.imported_module}:{self.imported_name}"
        return self.imported_module

    @property
    def description(self) -> str:
        return f"{self.source_module} -> {self.target}"


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT)
    parts = ["tradex", *relative.with_suffix("").parts]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_from_module(source_path: Path, node: ast.ImportFrom) -> str:
    module = _module_name(source_path)
    package = module if source_path.name == "__init__.py" else module.rpartition(".")[0]
    if node.level:
        relative = "." * node.level + (node.module or "")
        return importlib.util.resolve_name(relative, package)
    return node.module or ""


def _imports() -> tuple[ImportRecord, ...]:
    records: list[ImportRecord] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        source_module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        source = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                records.extend(
                    ImportRecord(source, source_module, alias.name, None)
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                imported_module = _resolve_from_module(path, node)
                records.extend(
                    ImportRecord(
                        source,
                        source_module,
                        imported_module,
                        alias.name,
                    )
                    for alias in node.names
                )
    return tuple(records)


def _is_interface(module: str) -> bool:
    return any(
        module == package or module.startswith(f"{package}.")
        for package in ("tradex.dashboard", "tradex.tools", "tradex.data_lake")
    )


def _is_in_package(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def _violations() -> dict[str, list[str]]:
    result: dict[str, set[str]] = {
        "interfaces_must_not_import_data_sources": set(),
        "gateway_must_not_import_provider_implementations": set(),
        "data_lake_must_not_import_dashboard": set(),
    }
    for record in _imports():
        if _is_interface(record.source_module) and (
            record.imported_module == "tradex.data_sources"
            or record.imported_module.startswith("tradex.data_sources.")
        ):
            result["interfaces_must_not_import_data_sources"].add(
                record.description
            )

        if _is_in_package(record.source_module, "tradex.data_gateway"):
            provider_implementation = record.imported_module.startswith(
                "tradex.data_sources."
            )
            unsupported_facade_name = (
                record.imported_module == "tradex.data_sources"
                and record.imported_name not in {"get_router", "register_all_sources"}
            )
            if provider_implementation or unsupported_facade_name:
                result["gateway_must_not_import_provider_implementations"].add(
                    record.description
                )

        if (
            _is_in_package(record.source_module, "tradex.data_lake")
            and (
                record.imported_module == "tradex.dashboard"
                or record.imported_module.startswith("tradex.dashboard.")
            )
        ):
            result["data_lake_must_not_import_dashboard"].add(record.description)

    return {name: sorted(items) for name, items in result.items()}


def test_architecture_boundary_debt_does_not_change_silently() -> None:
    expected = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    actual = _violations()
    if actual == expected:
        return

    delta = {
        rule: {
            "added": sorted(set(actual[rule]) - set(expected.get(rule, []))),
            "resolved": sorted(set(expected.get(rule, [])) - set(actual[rule])),
        }
        for rule in actual
        if set(actual[rule]) != set(expected.get(rule, []))
    }
    unexpected_rules = sorted(set(expected) - set(actual))
    if unexpected_rules:
        delta["unknown_baseline_rules"] = unexpected_rules
    raise AssertionError(
        "Tradex architecture boundary baseline changed:\n"
        + json.dumps(delta, ensure_ascii=False, indent=2)
        + "\nRemove added dependencies instead of expanding the baseline. "
        "When debt is resolved, delete its stale baseline entry in the same change."
    )
