"""scripts/test_report.py: the test report by test ID (SPEC §20.1; C1.01; D-043).

Small synthetic JUnit files and spec or plan text drive the parser and the status rules; the
last tests check the real suite: every ID of a ticked PLAN.md step has a marked test.
"""

import importlib.util
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_report", ROOT / "scripts" / "test_report.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TR = _module()

SPEC = """
| ID | Input | Assertions |
|---|---|---|
| T-ABC-01 | the first input | first assertions |
| T-ABC-02 | a `piped \\| cell` | second |

| ID | Type | Steps | Expected |
|---|---|---|---|
| T-UI-01 | Manual | Upload a bad file | an error table |
"""

PLAN = """
- [x] **S1.01** First step
  - Tests: T-ABC-01, T-CI-01
- [ ] **S2.01** Later step
  - Tests: T-ABC-02; T-POL-01 to T-POL-03, T-INJ-01..02 and T-IMP-04/05
"""


def junit(*cases: tuple[str, str, list[str]]) -> bytes:
    """``(name, outcome, ids)`` test cases as pytest's JUnit XML writes them."""
    body = []
    for name, outcome, ids in cases:
        props = "".join(f'<property name="spec_id" value="{i}"/>' for i in ids)
        inner = f"<properties>{props}</properties>" if props else ""
        inner += {"failed": "<failure message='x'/>", "skipped": "<skipped/>",
                  "passed": ""}[outcome]  # fmt: skip
        body.append(f'<testcase classname="tests.test_x" name="{name}">{inner}</testcase>')
    return f'<testsuites><testsuite name="pytest">{"".join(body)}</testsuite></testsuites>'.encode()


def test_the_catalogue_reads_ids_types_and_summaries() -> None:
    catalogue = TR.spec_catalogue(SPEC)
    assert list(catalogue) == ["T-ABC-01", "T-ABC-02", "T-UI-01"]
    assert catalogue["T-ABC-01"].summary == "the first input"
    assert catalogue["T-ABC-01"].kind is None
    assert (catalogue["T-UI-01"].kind, catalogue["T-UI-01"].summary) == (
        "Manual",
        "Upload a bad file",
    )


def test_the_real_spec_has_every_catalogue_id_once() -> None:
    catalogue = TR.spec_catalogue((ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8"))
    assert len(catalogue) == 106
    assert {"T-RPT-01", "T-INJ-03", "T-CI-01", "T-API-10"} <= set(catalogue)


def test_plan_owners_expand_every_range_form() -> None:
    owners = TR.plan_owners(PLAN)
    assert owners["T-ABC-01"] == TR.Owner("S1.01", True)
    assert {i for i, o in owners.items() if o.step == "S2.01"} == {
        "T-ABC-02", "T-POL-01", "T-POL-02", "T-POL-03", "T-INJ-01", "T-INJ-02", "T-IMP-04",
        "T-IMP-05",
    }  # fmt: skip


def test_junit_outcomes_per_id_and_overall() -> None:
    by_id, totals = TR.junit_results(junit(
        ("a", "passed", ["T-ABC-01"]), ("b", "failed", ["T-ABC-01", "T-ABC-02"]),
        ("c", "skipped", ["T-ABC-02"]), ("d", "passed", []),
    ))  # fmt: skip
    assert by_id["T-ABC-01"] == Counter(passed=1, failed=1)
    assert by_id["T-ABC-02"] == Counter(failed=1, skipped=1)
    assert totals == Counter(passed=2, failed=1, skipped=1)


@pytest.mark.parametrize(
    ("test_id", "owner", "outcomes", "expected"),
    [
        ("T-ABC-01", TR.Owner("S1.01", True), Counter(passed=3), "passed"),
        ("T-ABC-01", TR.Owner("S1.01", True), Counter(passed=2, failed=1), "FAILED"),
        ("T-ABC-01", TR.Owner("S1.01", True), Counter(skipped=2), "skipped"),
        ("T-ABC-01", TR.Owner("S1.01", True), None, "MISSING"),
        ("T-ABC-02", TR.Owner("S2.01", False), None, "planned (S2.01)"),
        ("T-ABC-02", TR.Owner("S2.01", False), Counter(passed=1), "passed"),
        ("T-UI-01", None, None, "not planned"),
        ("T-CI-01", TR.Owner("S1.01", True), None, "CI workflow"),
    ],
)
def test_status_rules(test_id: str, owner: object, outcomes: object, expected: str) -> None:
    assert TR.status(test_id, owner, outcomes) == expected


def test_render_is_deterministic_and_makes_no_value_claim() -> None:
    catalogue = TR.spec_catalogue(SPEC)
    owners = TR.plan_owners(PLAN)
    by_id, totals = TR.junit_results(junit(("a", "passed", ["T-ABC-01"])))
    first = TR.render(catalogue, owners, by_id, totals)
    assert first == TR.render(catalogue, owners, by_id, totals)
    assert TR.CLAIM in first and "not evidence of business value" in first
    assert "| T-ABC-01 |  | the first input | S1.01 | passed |" in first
    assert "| T-ABC-02 |  | a `piped / cell` | S2.01 | planned (S2.01) |" in first  # no stray |
    assert "| T-UI-01 | Manual | Upload a bad file |  | not planned |" in first
    assert "time" not in first.lower()  # no durations or timestamps


def test_a_failure_anywhere_is_reported() -> None:
    catalogue = TR.spec_catalogue(SPEC)
    by_id, totals = TR.junit_results(junit(("a", "passed", ["T-ABC-01"]), ("b", "failed", [])))
    assert "1 failing test(s) in the run" in TR.render(catalogue, {}, by_id, totals)


def write_inputs(tmp_path: Path, xml: bytes) -> Path:
    path = tmp_path / "junit.xml"
    path.write_bytes(xml)
    return path


@pytest.fixture
def small_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(TR, "SPEC", tmp_path / "SPEC.md")
    monkeypatch.setattr(TR, "PLAN", tmp_path / "PLAN.md")
    (tmp_path / "SPEC.md").write_text(SPEC)
    (tmp_path / "PLAN.md").write_text(PLAN)
    return tmp_path


def test_check_fails_when_the_report_is_stale_and_passes_when_current(
    small_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    xml = write_inputs(small_repo, junit(("a", "passed", ["T-ABC-01"])))
    out = small_repo / "TEST_REPORT.md"
    assert TR.main(["--junit", str(xml), "--out", str(out)]) == 0
    assert TR.main(["--junit", str(xml), "--out", str(out), "--check"]) == 0
    out.write_text(out.read_text() + "edited\n")
    assert TR.main(["--junit", str(xml), "--out", str(out), "--check"]) == 1
    assert "out of date" in capsys.readouterr().err
    out.unlink()
    assert TR.main(["--junit", str(xml), "--out", str(out), "--check"]) == 1


def test_check_fails_on_a_missing_or_failing_implemented_id(
    small_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = small_repo / "TEST_REPORT.md"
    xml = write_inputs(
        small_repo, junit(("a", "failed", ["T-ABC-02"]), ("b", "passed", ["T-ZZZ-09"]))
    )
    assert TR.main(["--junit", str(xml), "--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "T-ABC-01: MISSING" in err  # S1.01 is ticked but nothing carries the ID
    assert "T-ABC-02: FAILED" in err
    assert "T-ZZZ-09: marker names an ID that is not in SPEC.md" in err


def test_no_junit_file_is_a_usage_error(tmp_path: Path) -> None:
    assert TR.main(["--junit", str(tmp_path / "none.xml")]) == 2


# --- the real suite -----------------------------------------------------------------------------


def test_every_id_of_a_ticked_step_has_a_marked_test(request: pytest.FixtureRequest) -> None:
    """The collected suite itself: no implemented ID is left without a test (CI workflow
    excepted). Markers are copied into ``spec_id`` properties by tests/conftest.py."""
    catalogue = TR.spec_catalogue((ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8"))
    owners = TR.plan_owners((ROOT / "PLAN.md").read_text(encoding="utf-8"))
    session = request.session
    marked = {
        value for item in session.items for name, value in item.user_properties
        if name == "spec_id"
    }  # fmt: skip
    if len(session.items) < 1000:
        pytest.skip("run with the whole suite to see every marker")
    implemented = {i for i, o in owners.items() if o.done and i in catalogue} - set(TR.WORKFLOW_IDS)
    assert sorted(implemented - marked) == []


def test_the_marker_hook_refuses_an_unknown_id(monkeypatch: pytest.MonkeyPatch) -> None:
    import conftest  # tests/conftest.py

    class Item:
        nodeid = "tests/test_x.py::test_y"

        def __init__(self) -> None:
            self.user_properties: list[tuple[str, object]] = []

        def iter_markers(self, name: str) -> list[pytest.Mark]:
            return [pytest.mark.spec("T-ZZZ-09").mark]

    with pytest.raises(pytest.UsageError, match="T-ZZZ-09"):
        conftest.pytest_collection_modifyitems(None, [Item()])  # type: ignore[arg-type]


def test_the_marker_hook_copies_ids_into_junit_properties() -> None:
    import conftest

    class Item:
        nodeid = "tests/test_x.py::test_y"

        def __init__(self) -> None:
            self.user_properties: list[tuple[str, object]] = []

        def iter_markers(self, name: str) -> list[pytest.Mark]:
            return [pytest.mark.spec("T-RPT-01", "T-UI-05").mark, pytest.mark.spec("T-RPT-01").mark]

    item = Item()
    conftest.pytest_collection_modifyitems(None, [item])  # type: ignore[arg-type]
    assert item.user_properties == [("spec_id", "T-RPT-01"), ("spec_id", "T-UI-05")]
