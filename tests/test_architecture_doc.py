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


def _package(path: Path) -> str:
    parts = path.relative_to(SRC).with_suffix("").parts
    return f"adapters.{parts[1]}" if parts[0] == "adapters" else parts[0]


def _target(module: str) -> str | None:
    parts = module.split(".")[1:]
    if not parts:
        return None
    return f"adapters.{parts[1]}" if parts[0] == "adapters" and len(parts) > 1 else parts[0]


def import_graph() -> dict[str, set[str]]:
    """Package -> the other ``receivables`` packages it imports (migrations excluded)."""
    graph: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        source = _package(path)
        graph.setdefault(source, set())
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                continue
            for module in modules:
                target = _target(module) if module.startswith("receivables.") else None
                if target and target != source:
                    graph[source].add(target)
    return graph


def documented_graph() -> dict[str, set[str]]:
    table = DOC.split("**Dependency rules**", 1)[1].split("\n## ", 1)[0]
    graph: dict[str, set[str]] = {}
    for package, imports in re.findall(r"^\| `([\w.]+)` \| ([^|]*) \|", table, re.M):
        graph[package] = set(re.findall(r"`([\w.]+)`", imports))
    return graph


def test_the_dependency_table_equals_the_real_imports() -> None:
    real = {k: v for k, v in import_graph().items() if v}
    documented = {k: v for k, v in documented_graph().items() if v}
    assert documented == real
    # Every package that imports others is in the table, and core imports nothing.
    assert documented_graph()["core"] == set() and import_graph()["core"] == set()


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


def test_the_mermaid_diagrams_connect_only_declared_nodes() -> None:
    blocks = re.findall(r"```mermaid\n(.*?)```", DOC, re.S)
    assert len(blocks) == 3
    for block in blocks:
        declared = set(re.findall(r"\b(\w+)\s*(?:\[|\(|\{)", block))
        declared |= set(re.findall(r"^\s*subgraph (\w+)", block, re.M))
        used = set(re.findall(r"^\s*(\w+)\s+(?:-|\.)", block, re.M))
        used |= set(re.findall(r"(?:-->|\.->)\s*(\w+)", block))
        assert used - declared == set(), used - declared


def test_the_names_in_the_diagrams_exist() -> None:
    cited = {"run_tick", "evaluate_send", "TOOL_REGISTRY", "validate_plan", "plan_case",
             "LlmPlanner", "RedactingClient", "AnthropicClient", "ReplayClient", "FakeClient",
             "SandboxMailGateway", "DbAgentStore", "injection_signals", "verify_extraction",
             "check_draft", "PlanSource", "DraftWriter", "AgentStore"}  # fmt: skip
    diagrams = "".join(re.findall(r"```mermaid\n(.*?)```", DOC, re.S))
    defined: set[str] = set()
    for path in SRC.rglob("*.py"):
        defined |= _defined_names(path)
    for name in cited:
        assert name in diagrams, name
        assert name in defined, name
