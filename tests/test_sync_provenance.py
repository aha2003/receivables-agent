"""scripts/sync_provenance.py (D-055): PROVENANCE.md's version cells follow uv.lock; licences,
new and removed packages are left to a person."""

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "sync_provenance", ROOT / "scripts" / "sync_provenance.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SYNC = _module()
TEXT = """# Provenance

| Package | Version | Licence | Purpose | Added in |
|---|---|---|---|---|
| anthropic | 1.15.0 | MIT | Official Claude SDK, imported only by `x.py` (§3.1) | S3.04 |
| fastapi | 0.143.5 | MIT | HTTP API and pages | S1.11 |

| Package | Version | Licence |
|---|---|---|
| defusedxml | 0.7.1 | PSF-2.0 |
| gone | 1.0.0 | MIT |

Locked but not installed on any supported platform: `httpx2-jsfetch` (only for emscripten).

| Tool | Version | Licence | Use |
|---|---|---|---|
| actions/checkout | v7 | MIT | CI checkout |
"""
LOCKED = {"anthropic": "2.3.0", "fastapi": "0.143.5", "defusedxml": "0.7.1",
          "httpx2-jsfetch": "0.1.0", "newdep": "3.0.0"}  # fmt: skip


def test_only_stale_version_cells_change() -> None:
    synced, changes = SYNC.sync(TEXT, LOCKED)
    assert changes == [("anthropic", "1.15.0", "2.3.0")]
    tail = " | MIT | Official Claude SDK, imported only by `x.py` (§3.1) | S3.04 |"
    old, new = "| anthropic | 1.15.0" + tail, "| anthropic | 2.3.0" + tail
    assert synced == TEXT.replace(old, new)  # nothing else moved, the licence included
    assert SYNC.sync(synced, LOCKED) == (synced, [])  # running again changes nothing


def test_a_bumped_package_whose_licence_changed_is_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    licences = {"anthropic": "Apache-2.0", "fastapi": "MIT", "defusedxml": "PSFL"}
    monkeypatch.setattr(SYNC, "installed_licence", licences.get)
    synced, changes = SYNC.sync(TEXT, LOCKED)
    found = SYNC.problems(synced, LOCKED, {name for name, _, _ in changes})
    assert "anthropic: PROVENANCE.md says 'MIT', the installed package 'Apache-2.0'" in found
    # Unchanged packages are not compared: "PSFL" was normalised to PSF-2.0 by hand.
    assert not any(line.startswith("defusedxml") for line in found)


def test_new_and_removed_packages_are_left_to_a_person() -> None:
    found = SYNC.problems(TEXT, LOCKED, set())
    assert "newdep 3.0.0 is locked but has no row: add one by hand" in found
    assert "gone has a row but is no longer locked: remove it by hand" in found
    assert not any("httpx2-jsfetch" in line for line in found)  # named in the platform note
    assert not any("actions/checkout" in line for line in found)  # tools are not packages


@pytest.mark.parametrize(("a", "b"), [("Apache 2.0", "Apache-2.0"), ("mit", "MIT"),
                                      ("BSD-3-Clause", "bsd 3 clause")])  # fmt: skip
def test_licence_names_compare_without_case_spaces_or_hyphens(a: str, b: str) -> None:
    assert SYNC._same_licence(a, b)
    assert not SYNC._same_licence("MIT", "Apache-2.0")


def _copy_repo(tmp_path: Path, lock_edit: tuple[str, str] | None) -> Path:
    for name in ("PROVENANCE.md", "uv.lock", "pyproject.toml"):
        shutil.copy(ROOT / name, tmp_path / name)
    if lock_edit:
        lock = tmp_path / "uv.lock"
        old, new = lock_edit
        text = lock.read_text(encoding="utf-8")
        assert text.count(old) == 1
        lock.write_text(text.replace(old, new), encoding="utf-8")
    return tmp_path


def _run(monkeypatch: pytest.MonkeyPatch, root: Path, *args: str) -> int:
    monkeypatch.setattr(SYNC, "PROVENANCE", root / "PROVENANCE.md")
    monkeypatch.setattr(SYNC, "LOCK", root / "uv.lock")
    monkeypatch.setattr(SYNC, "PYPROJECT", root / "pyproject.toml")
    return SYNC.main(list(args))


def test_the_repository_is_in_sync(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(monkeypatch, ROOT, "--check") == 0
    assert "PROVENANCE.md matches uv.lock" in capsys.readouterr().out


def test_check_reports_a_bump_and_changes_nothing_then_sync_fixes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import re

    version = re.search(r'name = "ruff"\nversion = "([^"]+)"', (ROOT / "uv.lock").read_text())
    assert version
    root = _copy_repo(tmp_path, (f'name = "ruff"\nversion = "{version.group(1)}"',
                                 'name = "ruff"\nversion = "99.0.0"'))  # fmt: skip
    before = (root / "PROVENANCE.md").read_text(encoding="utf-8")
    assert _run(monkeypatch, root, "--check") == 1
    assert (root / "PROVENANCE.md").read_text(encoding="utf-8") == before
    assert f"ruff: {version.group(1)} -> 99.0.0" in capsys.readouterr().out
    assert _run(monkeypatch, root) == 0  # ruff's installed licence (MIT) still matches its row
    after = (root / "PROVENANCE.md").read_text(encoding="utf-8")
    assert "| ruff | 99.0.0 | MIT |" in after
    assert after.replace("| ruff | 99.0.0 |", f"| ruff | {version.group(1)} |") == before
    assert _run(monkeypatch, root, "--check") == 0
