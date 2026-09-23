"""Domain purity, enforced rather than hoped for.

Everything under ``feeder_station.domain`` decides things from what it is
handed, not from the outside world: no FastAPI, no serial port, no database,
no socket, no filesystem, and no wall-clock read. Time is always a parameter,
exactly as ``traps.py`` already does it (design.md, section 1, DD1).

This test parses every module in the package with ``ast`` rather than
importing it, so a banned import fails the test even if the module happens to
tolerate the import at runtime.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BANNED_MODULES = {"fastapi", "serial", "sqlite3", "socket", "pathlib", "os"}

DOMAIN_DIR = Path(__file__).resolve().parents[2] / "src" / "feeder_station" / "domain"


def _domain_modules() -> list[Path]:
    return sorted(DOMAIN_DIR.glob("*.py"))


def _imported_top_level_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module.split(".")[0])
    return names


def _calls_datetime_now(tree: ast.AST) -> bool:
    """True if the module calls ``datetime.now()`` anywhere in its body.

    Matches both ``datetime.now()`` (``from datetime import datetime``) and
    ``datetime.datetime.now()`` (``import datetime``).
    """
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "now":
            continue
        target = node.func.value
        if isinstance(target, ast.Name) and target.id == "datetime":
            return True
        if isinstance(target, ast.Attribute) and target.attr == "datetime":
            return True
    return False


@pytest.mark.parametrize("module_path", _domain_modules(), ids=lambda p: p.name)
def test_domain_module_stays_pure(module_path: Path) -> None:
    tree = ast.parse(module_path.read_text(), filename=str(module_path))

    forbidden_imports = _imported_top_level_names(tree) & BANNED_MODULES
    assert not forbidden_imports, f"{module_path.name} imports {sorted(forbidden_imports)}"

    assert not _calls_datetime_now(tree), (
        f"{module_path.name} calls datetime.now() instead of taking time as a parameter"
    )
