"""Repository-wide dependency rules for the decomposed architecture."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1] / "src" / "ctrlrtn"

# These domains are below the user-facing adapters. Letting them import the CLI
# or gateway recreates the dependency tangles that the refactor removed.
FORBIDDEN = {
    "analysis": {"cli", "gateway"},
    "config": {"cli", "gateway"},
    "control": {"cli", "gateway"},
    "control_config": {"cli", "gateway"},
    "eval": {"cli", "gateway"},
    "identify": {"cli", "gateway"},
    "jobs": {"cli", "gateway"},
    "policy": {"cli", "gateway"},
    "recorder": {"cli", "gateway"},
    "sdk": {"cli", "gateway"},
    "telemetry": {"cli", "gateway"},
    "workflow": {"cli", "gateway"},
}


def _router_imports(path: Path) -> set[str]:
    imports: set[str] = set()
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return {
        name.removeprefix("ctrlrtn.").split(".", 1)[0]
        for name in imports
        if name.startswith("ctrlrtn.")
    }


@pytest.mark.contract
def test_core_domains_do_not_depend_on_user_facing_adapters():
    violations = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        domain = relative.parts[0]
        forbidden = FORBIDDEN.get(domain, set())
        for dependency in sorted(_router_imports(path) & forbidden):
            violations.append(f"{relative}: imports {dependency}")
    assert not violations, "forbidden dependencies:\n" + "\n".join(violations)


@pytest.mark.contract
def test_gateway_uses_storage_contracts_not_sqlite_implementation():
    violations = []
    for path in (ROOT / "gateway").rglob("*.py"):
        source = path.read_text()
        if "ctrlrtn.recorder.sqlite" in source:
            violations.append(str(path.relative_to(ROOT)))
    assert not violations, "gateway imports concrete SQLite: " + ", ".join(
        violations
    )
