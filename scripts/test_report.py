"""The test report by test ID (SPEC §20.1; PLAN C1.01; D-043).

    uv run pytest -q --junitxml=junit.xml
    uv run python scripts/test_report.py             # write docs/TEST_REPORT.md
    uv run python scripts/test_report.py --check     # CI: fail if stale, missing or failing

Every spec test ID (the ``| T-… |`` rows of docs/SPEC.md) is listed with the PLAN.md step that
owns it and its status, read from the JUnit XML: tests carry ``@pytest.mark.spec("T-…")``,
which tests/conftest.py copies into a ``spec_id`` property. Statuses:

- ``passed``, ``FAILED`` or ``skipped`` from the marked tests' results (no counts or
  durations: the file changes only when an ID's status does);
- ``MISSING`` when the owning step is ticked but no test carries the ID;
- ``planned (<step>)`` when the owning step is not ticked yet;
- ``not planned`` when no PLAN.md step lists the ID;
- ``CI workflow`` for T-CI-01, which is the workflow itself, not a pytest test.

The report states plainly that passing tests are not evidence of business value. It holds no
date, commit or durations, so it is the same for the same results and ``--check`` can compare.
"""

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "SPEC.md"
PLAN = ROOT / "PLAN.md"
REPORT = ROOT / "docs" / "TEST_REPORT.md"
JUNIT = ROOT / "junit.xml"
PROPERTY = "spec_id"
WORKFLOW_IDS = {"T-CI-01": "the CI workflow on a clean checkout (all gates)"}
TEST_TYPES = {"Unit", "Integration", "Manual", "E2E", "Property", "API"}
CLAIM = (
    "Passing tests show that the software behaves as specified on synthetic data. They are "
    "not evidence of business value, of real-world collection outcomes or of fitness for "
    "real customer data."
)

_ROW = re.compile(r"^\| (T-[A-Z0-9]+-\d+) \|(.*)$", re.M)
_STEP = re.compile(r"^- \[( |x)\] \*\*(\S+)\*\*")
_ID = r"T-[A-Z0-9]+-\d+"


@dataclass(frozen=True, slots=True)
class SpecTest:
    test_id: str
    kind: str | None
    summary: str


@dataclass(frozen=True, slots=True)
class Owner:
    step: str
    done: bool


def spec_catalogue(text: str) -> dict[str, SpecTest]:
    """The spec's test IDs in order of appearance, with the type (if given) and a summary."""
    out: dict[str, SpecTest] = {}
    for test_id, rest in _ROW.findall(text):
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", rest.rstrip().rstrip("|"))]
        kind = cells[0] if cells and cells[0] in TEST_TYPES else None
        body = cells[1:] if kind else cells
        summary = re.sub(r"\s+", " ", body[0] if body else "").replace("\\|", "/")
        if len(summary) > 90:
            summary = summary[:89].rstrip() + "…"
        out.setdefault(test_id, SpecTest(test_id, kind, summary))
    return out


def _expand(text: str) -> list[str]:
    """IDs in a PLAN.md "Tests:" line, with ranges "T-X-01 to T-X-11", "T-X-01..03" and
    "T-X-04/05"."""
    found: list[str] = []
    for m in re.finditer(rf"({_ID})(?:\s+to\s+({_ID})|\.\.(\d+)|/(\d+))?", text):
        first, to_id, dots, slash = m.groups()
        prefix, start = first.rsplit("-", 1)
        width = len(start)
        end = to_id.rsplit("-", 1)[1] if to_id else dots or slash
        if end is None:
            found.append(first)
        elif slash:
            found += [first, f"{prefix}-{int(end):0{width}d}"]
        else:
            found += [f"{prefix}-{n:0{width}d}" for n in range(int(start), int(end) + 1)]
    return found


def plan_owners(text: str) -> dict[str, Owner]:
    """The first PLAN.md step whose "Tests:" line lists each ID."""
    owners: dict[str, Owner] = {}
    step: Owner | None = None
    for line in text.splitlines():
        head = _STEP.match(line)
        if head:
            step = Owner(head.group(2), head.group(1) == "x")
        elif step and line.strip().startswith("- Tests:"):
            for test_id in _expand(line):
                owners.setdefault(test_id, step)
    return owners


def junit_results(xml: bytes) -> tuple[dict[str, Counter[str]], Counter[str]]:
    """Per spec ID, the outcomes of its marked tests; and the outcomes of every test."""
    by_id: dict[str, Counter[str]] = {}
    totals: Counter[str] = Counter()
    for case in ET.fromstring(xml).iter("testcase"):  # noqa: S314 - our own pytest output
        if case.find("failure") is not None or case.find("error") is not None:
            outcome = "failed"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        totals[outcome] += 1
        for prop in case.iter("property"):
            if prop.get("name") == PROPERTY:
                by_id.setdefault(str(prop.get("value")), Counter())[outcome] += 1
    return by_id, totals


def status(test_id: str, owner: Owner | None, outcomes: Counter[str] | None) -> str:
    if test_id in WORKFLOW_IDS:
        return "CI workflow"
    if outcomes:
        if outcomes["failed"]:
            return "FAILED"
        return "passed" if outcomes["passed"] else "skipped"
    if owner is None:
        return "not planned"
    return "MISSING" if owner.done else f"planned ({owner.step})"


def render(
    catalogue: dict[str, SpecTest],
    owners: dict[str, Owner],
    by_id: dict[str, Counter[str]],
    totals: Counter[str],
) -> str:
    rows = []
    counts: Counter[str] = Counter()
    for test_id, spec in catalogue.items():
        owner = owners.get(test_id)
        outcomes = by_id.get(test_id)
        state = status(test_id, owner, outcomes)
        counts[state.split(" (")[0]] += 1
        rows.append(
            f"| {test_id} | {spec.kind or ''} | {spec.summary} | "
            f"{owner.step if owner else ''} | {state} |"
        )
    summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
    if totals["failed"]:  # a failure anywhere is reported, whether or not it has an ID
        summary += f"; {totals['failed']} failing test(s) in the run"
    return "\n".join([
        "# Test report by test ID",
        "",
        "Generated by `scripts/test_report.py` from the JUnit output of `uv run pytest` (SPEC "
        "§20.1). Synthetic data only.",
        "",
        f"> {CLAIM}",
        "",
        f"- Spec test IDs: {len(catalogue)} ({summary}).",
        "- `planned` IDs belong to PLAN.md steps not done yet; `not planned` IDs are in no "
        "step; `CI workflow` is verified by the CI run itself, not by a pytest test. "
        "`passed` means every test marked with the ID passed; CI regenerates this file from "
        "its own run and fails if it differs.",
        "",
        "| ID | Type | Test (summary from SPEC.md) | Step | Status |",
        "|---|---|---|---|---|",
        *rows,
        "",
    ])  # fmt: skip


def build(junit: Path) -> tuple[str, list[str]]:
    """The report and the problems that fail ``--check`` (an implemented ID MISSING or FAILED,
    or a marker naming an ID the spec does not have)."""
    catalogue = spec_catalogue(SPEC.read_text(encoding="utf-8"))
    owners = plan_owners(PLAN.read_text(encoding="utf-8"))
    by_id, totals = junit_results(junit.read_bytes())
    problems = [f"{i}: marker names an ID that is not in SPEC.md" for i in by_id
                if i not in catalogue]  # fmt: skip
    if totals["failed"]:
        problems.append(f"{totals['failed']} failing test(s) in the run")
    for test_id in catalogue:
        state = status(test_id, owners.get(test_id), by_id.get(test_id))
        if state in {"MISSING", "FAILED"}:
            problems.append(f"{test_id}: {state}")
    return render(catalogue, owners, by_id, totals), problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write docs/TEST_REPORT.md from JUnit XML.")
    parser.add_argument("--junit", type=Path, default=JUNIT)
    parser.add_argument("--out", type=Path, default=REPORT)
    parser.add_argument("--check", action="store_true", help="fail if stale, missing or failing")
    args = parser.parse_args(argv)
    if not args.junit.exists():
        print(
            f"no JUnit file at {args.junit}: run uv run pytest --junitxml=junit.xml",
            file=sys.stderr,
        )
        return 2
    report, problems = build(args.junit)
    for problem in problems:
        print(problem, file=sys.stderr)
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current != report:
            print(f"{args.out.name} is out of date: run scripts/test_report.py", file=sys.stderr)
            return 1
        return 1 if problems else 0
    args.out.write_text(report, encoding="utf-8")
    print(f"wrote {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
