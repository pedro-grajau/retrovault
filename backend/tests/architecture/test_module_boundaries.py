from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

MODULES = {"catalog", "commerce", "rentals", "concierge", "data_governance", "quality"}
ROOT = Path(__file__).parents[2] / "app" / "modules"


def imported_names(path: Path, node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    module = node.module or ""
    if node.level:
        relative = path.relative_to(ROOT.parent.parent).with_suffix("")
        package = ".".join(relative.parts[:-1])
        module = resolve_name("." * node.level + module, package)
    return [f"{module}.{alias.name}" if module else alias.name for alias in node.names]


def test_adapters_and_repositories_do_not_import_laterally() -> None:
    violations: list[str] = []
    for path in ROOT.rglob("*.py"):
        owner = path.relative_to(ROOT).parts[0]
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            imported = imported_names(path, node)
            for name in imported:
                parts = name.split(".")
                if len(parts) >= 4 and parts[:2] == ["app", "modules"]:
                    target, boundary = parts[2], parts[3]
                    if target in MODULES - {owner} and boundary in {"adapters", "repositories"}:
                        violations.append(f"{path}: {name}")
    assert not violations, "Forbidden lateral imports:\n" + "\n".join(violations)


def test_relative_lateral_adapter_import_is_resolved() -> None:
    path = ROOT / "catalog" / "adapters" / "probe.py"
    node = ast.parse("from ...commerce.adapters import Gateway").body[0]
    assert isinstance(node, ast.ImportFrom)
    assert imported_names(path, node) == ["app.modules.commerce.adapters.Gateway"]


def test_domain_layers_do_not_depend_on_frameworks_or_adapters() -> None:
    forbidden_roots = {"fastapi", "pydantic", "sqlalchemy", "sqlmodel"}
    violations: list[str] = []
    for path in ROOT.glob("*/domain/**/*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            for name in imported:
                parts = name.split(".")
                if parts[0] in forbidden_roots or "adapters" in parts:
                    violations.append(f"{path}: {name}")
    assert not violations, "Domain dependency violations:\n" + "\n".join(violations)


def test_application_layers_do_not_depend_on_adapters_or_frameworks() -> None:
    forbidden_roots = {"fastapi", "pydantic", "sqlalchemy", "sqlmodel"}
    violations: list[str] = []
    for path in ROOT.glob("*/application/**/*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            for name in imported:
                parts = name.split(".")
                if parts[0] in forbidden_roots or "adapters" in parts:
                    violations.append(f"{path}: {name}")
    assert not violations, "Application dependency violations:\n" + "\n".join(violations)


def test_editorial_allowlists_match_between_review_and_publication() -> None:
    from app.modules.catalog.adapters.postgres_repository import _EDITORIAL_FIELDS
    from app.modules.data_governance.adapters.postgres_repository import EDITABLE_FIELDS

    assert EDITABLE_FIELDS == _EDITORIAL_FIELDS
