"""docs/ARCHITECTURE.md matches the code (SPEC §20.1; C1.03; D-045).

The dependency table must equal the real imports between packages under ``src/receivables``,
every module and name the page cites must exist, and each Mermaid diagram may connect only
nodes it declares. Source is parsed with ``ast``; nothing is imported.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "receivables"
DOC = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")


def _package(path: Path) -> str | None:
    """The package a module belongs to (``adapters.db``, ``agent``, …); None for the
    ``__init__`` of ``receivables`` and ``receivables.adapters``, which hold no code."""
    parts = path.relative_to(SRC).with_suffix("").parts
    if parts in {("__init__",), ("adapters", "__init__")}:
        return None
    return f"adapters.{parts[1]}" if parts[0] == "adapters" else parts[0]


def _targets(node: ast.ImportFrom | ast.Import) -> list[str]:
    """The ``receivables`` packages one import statement reaches."""
    if isinstance(node, ast.Import):
        modules = [a.name for a in node.names]
    else:
        assert node.level == 0, "relative imports would hide dependencies"
        base = node.module or ""
        # ``from receivables import settings`` and ``from receivables.adapters import db``
        # name the package in the imported names.
        modules = (
            [f"{base}.{a.name}" for a in node.names]
            if base in {"receivables", "receivables.adapters"}
            else [base]
        )
    out = []
    for module in modules:
        parts = module.split(".")
        if parts[0] != "receivables" or len(parts) < 2:
            continue
        out.append(f"adapters.{parts[2]}" if parts[1] == "adapters" and len(parts) > 2
                   else parts[1])  # fmt: skip
    return out


def import_graph() -> dict[str, set[str]]:
    """Package -> the other ``receivables`` packages it imports (migrations excluded)."""
    graph: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        source = _package(path)
        if source is None or "migrations" in path.parts:
            continue
        graph.setdefault(source, set())
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import | ast.ImportFrom):
                graph[source] |= {t for t in _targets(node) if t != source}
    return graph


def documented_graph() -> dict[str, set[str]]:
    table = DOC.split("**Dependency rules**", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| `([\w.]+)` \| ([^|]*) \|", table, re.M)
    packages = [package for package, _ in rows]
    assert len(packages) == len(set(packages)), "a package is listed twice"
    return {package: set(re.findall(r"`([\w.]+)`", imports)) for package, imports in rows}


def test_the_dependency_table_equals_the_real_imports() -> None:
    # Every package is a row (those importing nothing too), no row is invented, and each
    # row lists exactly what the package imports.
    assert documented_graph() == import_graph()
    assert import_graph()["core"] == set() and import_graph()["agent"] == {"core"}


def _defined_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef | ast.ClassDef | ast.TypeAlias):
            names.add(node.name if not isinstance(node, ast.TypeAlias) else node.name.id)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_every_module_and_name_it_cites_exists() -> None:
    table = DOC.split("## Where things live", 1)[1]
    rows = re.findall(r"^\| `(src/[\w/]+\.py)` \| (.*) \|$", table, re.M)
    assert len(rows) >= 20
    for path, names in rows:
        module = ROOT / path
        assert module.exists(), path
        missing = set(re.findall(r"`(\w+)`", names)) - _defined_names(module)
        assert not missing, (path, missing)


def test_every_path_in_the_text_exists() -> None:
    paths = set(re.findall(r"`((?:src|docs|tests)/[\w./-]+)`", DOC))
    paths |= {f"src/receivables/{p}" for p in re.findall(
        r"`((?:agent|core|adapters|api)/[\w/]+\.py)`", DOC)}  # fmt: skip
    assert paths
    assert sorted(p for p in paths if not (ROOT / p).exists()) == []


_ARROW = re.compile(r"<?-->|<?==>|-\.->|---|-\.-")
_LABELLED = re.compile(r"--\s[^>]*?-->|-\.\s[^>]*?\.->|==\s[^>]*?==>")
_SHAPE = re.compile(
    r"\{\{[^}]*\}\}|\[\([^)]*\)\]|\(\([^)]*\)\)|\(\[[^\]]*\]\)"
    r"|\[[^\]]*\]|\([^)]*\)|\{[^}]*\}|\|[^|]*\|"
)


def _edges(block: str) -> tuple[set[str], set[str]]:
    """(declared node ids, ids used on edge lines) of one Mermaid flowchart."""
    declared: set[str] = set(re.findall(r"^\s*subgraph (\w+)", block, re.M))
    used: set[str] = set()
    for line in block.splitlines()[1:]:
        stripped = line.strip()
        if not stripped or stripped == "end" or stripped.startswith(("subgraph", "%%")):
            continue
        plain = _LABELLED.sub("-->", stripped)
        for m in re.finditer(r"(\w+)\s*(?=\{\{|\[\(|\(\(|\(\[|\[|\(|\{)", plain):
            before = plain[: m.start()]
            if not before or re.search(r"(-->|\.->|==>|---|-\.-|&)\s*$", before):
                declared.add(m.group(1))
        bare = _SHAPE.sub("", plain)
        if _ARROW.search(bare):
            used |= {w for part in _ARROW.split(bare) for w in re.findall(r"\w+", part)}
    return declared, used


def test_the_mermaid_diagrams_connect_only_declared_nodes() -> None:
    blocks = re.findall(r"```mermaid\n(.*?)```", DOC, re.S)
    assert len(blocks) == 3
    for block in blocks:
        declared, used = _edges(block)
        assert used, "no edges found"
        assert used - declared == set(), used - declared


def test_the_node_check_catches_a_typo() -> None:
    declared, used = _edges("flowchart TD\n  a[A] --> b[B]\n  b -- label --> c\n  a ==> d")
    assert used - declared == {"c", "d"}


def test_the_names_in_the_diagrams_exist_where_the_table_says() -> None:
    cited = {"run_tick", "evaluate_send", "TOOL_REGISTRY", "validate_plan", "plan_case",
             "LlmPlanner", "RedactingClient", "AnthropicClient", "ReplayClient", "FakeClient",
             "SandboxMailGateway", "DbAgentStore", "injection_signals", "verify_extraction",
             "check_draft", "PlanSource", "DraftWriter", "AgentStore",
             "run_agent_tick"}  # fmt: skip
    diagrams = "".join(re.findall(r"```mermaid\n(.*?)```", DOC, re.S))
    table = DOC.split("## Where things live", 1)[1]
    listed = set(re.findall(r"`(\w+)`", "".join(re.findall(r"^\| `src/.*$", table, re.M))))
    for name in cited:
        assert re.search(rf"\b{name}\b", diagrams), name  # a whole word, not a substring
        assert name in listed, name  # and the table ties it to the module that defines it
