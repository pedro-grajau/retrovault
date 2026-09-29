from __future__ import annotations

import ast
from pathlib import Path

MODULES = {"catalog", "commerce", "rentals", "concierge", "data_governance", "quality"}
ROOT = Path(__file__).parents[2] / "app" / "modules"


def test_adapters_and_repositories_do_not_import_laterally() -> None:
    violations: list[str] = []
    for path in ROOT.rglob("*.py"):
        owner = path.relative_to(ROOT).parts[0]
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [f"{node.module}.{alias.name}" for alias in node.names]
            for name in imported:
                parts = name.split(".")
                if len(parts) >= 4 and parts[:2] == ["app", "modules"]:
                    target, boundary = parts[2], parts[3]
                    if target in MODULES - {owner} and boundary in {"adapters", "repositories"}:
                        violations.append(f"{path}: {name}")
    assert not violations, "Forbidden lateral imports:\n" + "\n".join(violations)


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
