"""Only the live Anthropic client may reach the network (SPEC §3.5; D-029).

Every module under ``src/receivables`` is parsed with ``ast``; none may import ``anthropic`` or an
HTTP client (``httpx``, ``httpx2`` and their ``httpcore`` cores; the SDK uses ``httpx2``) except
``adapters/llm/anthropic_client.py``. Nothing is imported.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "receivables"
FENCED = ("anthropic", "httpx", "httpx2", "httpcore", "httpcore2")
ALLOWED = SRC / "adapters" / "llm" / "anthropic_client.py"


def fenced_imports(source: str) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        elif (isinstance(node, ast.Name) and node.id == "__import__") or (
            isinstance(node, ast.Attribute) and node.attr == "import_module"
        ):
            out.append(f"line {node.lineno}: dynamic import")
            continue
        else:
            continue
        out.extend(
            f"line {node.lineno}: {name!r}"
            for name in names
            if any(name == root or name.startswith(root + ".") for root in FENCED)
        )
    return out


def test_the_allowed_module_exists_and_is_the_only_one_importing_the_sdk() -> None:
    assert fenced_imports(ALLOWED.read_text(encoding="utf-8"))  # the fence fences something


def test_only_the_anthropic_client_imports_anthropic_or_httpx() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 20
    problems = [
        f"{path.relative_to(SRC.parent)}: {problem}"
        for path in files
        if path != ALLOWED
        for problem in fenced_imports(path.read_text(encoding="utf-8"))
    ]
    assert problems == []


@pytest.mark.parametrize(
    "source",
    [
        "import anthropic",
        "import anthropic.types as t",
        "from anthropic import Anthropic",
        "from anthropic.types import Message",
        "import httpx",
        "from httpx import Client",
        "import httpx2",
        "from httpcore2 import ConnectionPool",
        "import os, httpx",
        "def f():\n    import anthropic",
        "m = __import__('anthropic')",
        "import importlib\nm = importlib.import_module('httpx')",
    ],
)
def test_the_fence_catches(source: str) -> None:
    assert fenced_imports(source)


@pytest.mark.parametrize(
    "source",
    [
        "from receivables.adapters.llm.base import LLMClient",
        "import anthropic_like_name",
        "import httpxx",
        "from . import anthropic_client",
        "x = 'import anthropic'",
    ],
)
def test_the_fence_allows(source: str) -> None:
    assert fenced_imports(source) == []
