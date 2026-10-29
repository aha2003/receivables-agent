#!/usr/bin/env python3
"""Bring PROVENANCE.md's version cells in line with uv.lock (D-055).

Usage: uv run python scripts/sync_provenance.py [--check]

Rewrites only the version cell of each package row whose locked version changed; every other
cell (licence, purpose, step) is left as written. ``--check`` changes nothing and exits 1 if a
version is stale (``tests/test_data_protection.py`` checks the same in CI).

It also prints what it will not fix by itself, for a person to decide:

- a package whose version changed and whose row's licence differs from the installed package's
  metadata (run ``uv sync --frozen`` first; a package that is not installed is not checked),
  since a new version can change its licence (anthropic 2.x moved from MIT to Apache-2.0).
  Unchanged packages are not compared: their metadata often uses free-text names that this
  file normalised to SPDX by hand (C1.04), and case, spaces and hyphens are ignored;
- a locked package with no row (a new dependency needs a row written by hand, CLAUDE.md), apart
  from those named in the "Locked but not installed" note;
- a row for a package that is no longer locked.

Exit codes: 0 in sync (or synced), 1 stale with ``--check``, 2 when a person must act.
"""

import importlib.metadata
import pathlib
import re
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROVENANCE, LOCK, PYPROJECT = ROOT / "PROVENANCE.md", ROOT / "uv.lock", ROOT / "pyproject.toml"
# A package row: "| name | version | licence |...". The same pattern the provenance test reads.
ROW = re.compile(r"^\| ([a-z0-9][a-z0-9._-]*) \| ([0-9][^ |]*) \| ([^|]+?) \|", re.M)
NOTE = "Locked but not"


def locked_versions(lock_text: str, project: str) -> dict[str, str]:
    lock = tomllib.loads(lock_text)
    return {p["name"]: p["version"] for p in lock["package"] if p["name"] != project}


def sync(text: str, locked: dict[str, str]) -> tuple[str, list[tuple[str, str, str]]]:
    """``text`` with stale version cells replaced, and (name, old, new) for each change."""
    changes: list[tuple[str, str, str]] = []

    def replace(match: re.Match[str]) -> str:
        name, version = match.group(1), match.group(2)
        new = locked.get(name)
        if new is None or new == version:
            return match.group(0)
        changes.append((name, version, new))
        start, end = match.span(2)
        offset = match.start(0)
        line = match.group(0)
        return line[: start - offset] + new + line[end - offset :]

    return ROW.sub(replace, text), changes


def installed_licence(name: str) -> str | None:
    try:
        meta = importlib.metadata.metadata(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    return meta.get("License-Expression") or meta.get("License") or None


def _same_licence(a: str, b: str) -> bool:
    return re.sub(r"[\s._-]", "", a.lower()) == re.sub(r"[\s._-]", "", b.lower())


def problems(text: str, locked: dict[str, str], bumped: set[str]) -> list[str]:
    """What a person must decide: licence changes of bumped packages, missing rows, rows for
    removed packages."""
    found: list[str] = []
    rows = {m.group(1): m.group(3).strip() for m in ROW.finditer(text)}
    note = next((line for line in text.splitlines() if line.startswith(NOTE)), "")
    for name in sorted(set(locked) - set(rows)):
        if f"`{name}`" not in note:
            found.append(f"{name} {locked[name]} is locked but has no row: add one by hand")
    for name in sorted(set(rows) - set(locked)):
        found.append(f"{name} has a row but is no longer locked: remove it by hand")
    for name in sorted(bumped & set(rows)):
        actual = installed_licence(name)
        stated = rows[name].split(" (", 1)[0]  # a bracketed note may follow the SPDX expression
        if actual is not None and len(actual) <= 40 and not _same_licence(actual, stated):
            found.append(f"{name}: PROVENANCE.md says {stated!r}, the installed package {actual!r}")
    return found


def main(argv: list[str]) -> int:
    check = "--check" in argv
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["name"]
    locked = locked_versions(LOCK.read_text(encoding="utf-8"), project)
    text = PROVENANCE.read_text(encoding="utf-8")
    synced, changes = sync(text, locked)
    for name, old, new in changes:
        print(f"{name}: {old} -> {new}")
    if check and changes:
        print("PROVENANCE.md is out of date: run `uv run python scripts/sync_provenance.py`")
        return 1
    if changes:
        PROVENANCE.write_text(synced, encoding="utf-8")
    to_decide = problems(synced, locked, {name for name, _, _ in changes})
    for line in to_decide:
        print(f"CHECK BY HAND: {line}")
    if not changes and not to_decide:
        print("PROVENANCE.md matches uv.lock")
    return 2 if to_decide else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
