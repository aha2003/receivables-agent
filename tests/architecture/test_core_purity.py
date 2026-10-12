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
    "io",
    "pathlib",
    "shutil",
    "tempfile",
    "glob",
    "logging",
    "asyncio",
    "smtplib",
    "alembic",
    "importlib",
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
CLOCK_CLASSES = frozenset({"datetime", "date"})
# Importing these anywhere in core/ or agent/ counts as reaching for the clock.
WALL_CLOCK_MODULES = ("time", "importlib")


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
        if isinstance(node, ast.Name) and node.id in {"open", "__import__"}:
            out.append(f"line {node.lineno}: builtin {node.id!r} (I/O or dynamic import)")
    return out


def _clock_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Local names bound to the datetime/date classes, and to the datetime module."""
    classes: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "datetime":
            classes.update(a.asname or a.name for a in node.names if a.name in CLOCK_CLASSES)
        elif isinstance(node, ast.Import):
            modules.update(a.asname or a.name for a in node.names if a.name == "datetime")
    return classes, modules


def _is_clock_class(node: ast.expr, classes: set[str], modules: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in classes
    return (
        isinstance(node, ast.Attribute)
        and node.attr in CLOCK_CLASSES
        and isinstance(node.value, ast.Name)
        and node.value.id in modules
    )


def wall_clock_violations(source: str) -> list[str]:
    """Flag datetime/date ``.now``/``.utcnow``/``.today`` (called or referenced), time imports
    and dynamic imports. An injected ``clock.now()`` is allowed (SPEC §3.5, D-006)."""
    tree = ast.parse(source)
    classes, modules = _clock_aliases(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in WALL_CLOCK_ATTRS
            and _is_clock_class(node.value, classes, modules)
        ):
            out.append(f"line {node.lineno}: wall-clock access .{node.attr}")
        elif isinstance(node, ast.Name) and node.id == "__import__":
            out.append(f"line {node.lineno}: dynamic import via __import__")
    for lineno, module in _imported_modules(tree):
        if _matches(module, WALL_CLOCK_MODULES):
            out.append(f"line {lineno}: wall-clock or dynamic-import module {module!r}")
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
        "from pathlib import Path\nx = Path('x').read_text()",
        "import io",
        "import shutil",
        "import tempfile",
        "import glob",
        "import logging",
        "import asyncio",
        "import smtplib",
        "from alembic import op",
        "import importlib",
        "m = __import__('os')",
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
        "import datetime as d\nx = d.date.today()",
        "from datetime import datetime\nnow = datetime.now\nx = now()",
        "from dataclasses import dataclass, field\nfrom datetime import datetime\n"
        "@dataclass\nclass A:\n    at: datetime = field(default_factory=datetime.now)",
        "from datetime import date as d\ndef f():\n    return d.today()",
        "import importlib\nt = importlib.import_module('time')",
        "t = __import__('time')",
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
        "def f(clock):\n    return clock.now(), clock.today()",
        "class Run:\n    def tick(self):\n        return self._clock.now()",
        "from datetime import datetime\ndef f(now: datetime) -> datetime:\n    return now",
        "from datetime import datetime\nx = datetime.fromisoformat('2026-10-31T09:00:00+04:00')",
    ],
)
def test_guard_accepts_pure_code(source: str) -> None:
    assert import_violations(source, CORE_FORBIDDEN_MODULES) == []
    assert wall_clock_violations(source) == []
