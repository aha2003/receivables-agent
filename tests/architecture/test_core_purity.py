"""T-ARCH-01: static purity check of core/ (SPEC §3.5; extended by docs/DECISIONS.md D-006).

core/ may not import frameworks, I/O, network or outer application layers, and neither core/
nor agent/ may read the wall clock. The checks parse source with ``ast``; nothing is imported.
"""

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "receivables"

# SPEC §3.5 core purity list.
SPEC_FORBIDDEN = ("sqlalchemy", "fastapi", "starlette", "anthropic", "httpx", "structlog")
# D-006: I/O, network, database, wall-clock modules and the outer application layers.
EXTRA_FORBIDDEN = (
    "sqlite3",
    "socket",
    "http",
    "urllib",
    "requests",
    "subprocess",
    "os",
    "time",
    "receivables.adapters",
    "receivables.api",
    "receivables.web",
    "receivables.sim",
    "receivables.agent",
    "receivables.settings",
    "receivables.cli",
)
CORE_FORBIDDEN_MODULES = SPEC_FORBIDDEN + EXTRA_FORBIDDEN
WALL_CLOCK_ATTRS = frozenset({"now", "utcnow", "today"})
WALL_CLOCK_MODULES = ("time",)


def _matches(module: str, roots: tuple[str, ...]) -> bool:
    return any(module == root or module.startswith(root + ".") for root in roots)


def _imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level >= 2:  # relative import that leaves the package
                found.append((node.lineno, "." * node.level + (node.module or "")))
            elif node.level == 0 and node.module:
                found.append((node.lineno, node.module))
    return found


def import_violations(source: str, forbidden: tuple[str, ...]) -> list[str]:
    tree = ast.parse(source)
    out: list[str] = []
    for lineno, module in _imported_modules(tree):
        if module.startswith(".."):
            out.append(f"line {lineno}: relative import {module!r} leaves the package")
        elif _matches(module, forbidden):
            out.append(f"line {lineno}: forbidden import {module!r}")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "open"
        ):
            out.append(f"line {node.lineno}: file I/O via open()")
    return out


def wall_clock_violations(source: str) -> list[str]:
    tree = ast.parse(source)
    out: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in WALL_CLOCK_ATTRS
        ):
            out.append(f"line {node.lineno}: wall-clock call .{node.func.attr}()")
    for lineno, module in _imported_modules(tree):
        if _matches(module, WALL_CLOCK_MODULES):
            out.append(f"line {lineno}: wall-clock module {module!r}")
    return out


def _sources(package: str) -> list[Path]:
    files = sorted((SRC / package).rglob("*.py"))
    assert files, f"no sources found under {package}/"
    return files


def _check(files: list[Path], checker: Callable[[str], list[str]]) -> list[str]:
    problems: list[str] = []
    for path in files:
        rel = path.relative_to(SRC.parent)
        problems.extend(f"{rel}: {p}" for p in checker(path.read_text(encoding="utf-8")))
    return problems


class TestArch01:
    def test_core_has_no_forbidden_imports(self) -> None:
        problems = _check(_sources("core"), lambda s: import_violations(s, CORE_FORBIDDEN_MODULES))
        assert problems == []

    @pytest.mark.parametrize("package", ["core", "agent"])
    def test_no_wall_clock_calls(self, package: str) -> None:
        assert _check(_sources(package), wall_clock_violations) == []


# The guard must actually catch what it is meant to catch.
@pytest.mark.parametrize(
    "source",
    [
        "import sqlalchemy",
        "import sqlalchemy.orm as orm",
        "from fastapi import FastAPI",
        "from starlette.requests import Request",
        "import anthropic",
        "import httpx",
        "import structlog",
        "import os",
        "from os import path",
        "import sqlite3",
        "from urllib.request import urlopen",
        "import socket",
        "import subprocess",
        "from receivables.adapters.clock import SystemClock",
        "import receivables.api",
        "from ..adapters import clock",
        "data = open('x.csv').read()",
    ],
)
def test_guard_flags_forbidden_import_or_io(source: str) -> None:
    assert import_violations(source, CORE_FORBIDDEN_MODULES)


@pytest.mark.parametrize(
    "source",
    [
        "from datetime import datetime\nx = datetime.now()",
        "import datetime\nx = datetime.datetime.now()",
        "from datetime import datetime as dt\nx = dt.now()",
        "from datetime import datetime, UTC\nx = datetime.now(UTC)",
        "from datetime import datetime\nx = datetime.utcnow()",
        "from datetime import date\nx = date.today()",
        "import time\nx = time.time()",
        "from time import time\nx = time()",
        "import time as t\nx = t.monotonic()",
    ],
)
def test_guard_flags_wall_clock(source: str) -> None:
    assert wall_clock_violations(source)


@pytest.mark.parametrize(
    "source",
    [
        "from decimal import Decimal",
        "from datetime import date, timedelta\nx = date(2026, 10, 31) + timedelta(days=1)",
        "from . import money\nfrom .money import to_fils",
        "import receivables.core.money",
        "from typing import cast",
        "osmium = 1\nimport timeit_like_name_is_not_time",
    ],
)
def test_guard_accepts_pure_code(source: str) -> None:
    assert import_violations(source, CORE_FORBIDDEN_MODULES) == []
    assert wall_clock_violations(source) == []
