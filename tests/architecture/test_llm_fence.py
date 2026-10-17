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


# The standard library's own network clients: no module may import them, so the Anthropic
# client is the only way out of the process and mail stays in the sandbox (C1.04, D-046).
STDLIB_NETWORK = ("socket", "ssl", "smtplib", "ftplib", "imaplib", "poplib", "telnetlib",
                  "http.client", "urllib.request", "xmlrpc")  # fmt: skip


def stdlib_network_imports(source: str) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            # ``from http import client`` and ``from urllib import request`` reach them too.
            names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
        else:
            continue
        out.extend(
            f"line {node.lineno}: {name!r}"
            for name in names
            if any(name == root or name.startswith(root + ".") for root in STDLIB_NETWORK)
        )
    return out


def test_no_module_imports_a_stdlib_network_client() -> None:
    found = {
        str(path.relative_to(SRC)): hits
        for path in SRC.rglob("*.py")
        if (hits := stdlib_network_imports(path.read_text(encoding="utf-8")))
    }
    assert found == {}


@pytest.mark.parametrize(
    "source",
    ["import smtplib", "from http import client", "from urllib.request import urlopen",
     "import socket as s", "from urllib import request"],
)  # fmt: skip
def test_the_stdlib_fence_catches_every_form(source: str) -> None:
    assert stdlib_network_imports(source)


def test_the_stdlib_fence_allows_url_parsing() -> None:
    assert stdlib_network_imports("from urllib.parse import quote") == []
