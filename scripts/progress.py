#!/usr/bin/env python3
"""Regenerate docs/PROGRESS.md and the README progress block from PLAN.md.

Usage: python scripts/progress.py [--check]
--check exits 1 if the generated files are out of date (used in CI).
Output is deterministic (no timestamps).
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLAN, OUT, README = ROOT / "PLAN.md", ROOT / "docs" / "PROGRESS.md", ROOT / "README.md"
STEP = re.compile(r"^- \[( |x)\] \*\*([SC]\d\.\d{2})\*\* (.*)$")
HEAD = re.compile(r"^## (Slice \d|Checkpoint C\d)\b.*$")
START, END = "<!-- PROGRESS:START -->", "<!-- PROGRESS:END -->"


def parse(text):
    sections, current = [], None
    for line in text.splitlines():
        h = HEAD.match(line)
        if h:
            current = {"name": line[3:].strip(), "steps": []}
            sections.append(current)
            continue
        if line.startswith("## "):
            current = None
            continue
        m = STEP.match(line)
        if m and current is not None:
            current["steps"].append((m.group(2), m.group(3).strip(), m.group(1) == "x"))
    return sections


def bar(done, total, width=20):
    filled = round(width * done / total) if total else 0
    return "█" * filled + "░" * (width - filled)


def render(sections):
    total = sum(len(s["steps"]) for s in sections)
    done = sum(1 for s in sections for st in s["steps"] if st[2])
    pct = round(100 * done / total) if total else 0
    nxt = next(((i, t) for s in sections for (i, t, d) in s["steps"] if not d), None)
    rows = ["| Section | Done | Total | Progress |", "|---|---|---|---|"]
    for s in sections:
        d = sum(1 for st in s["steps"] if st[2])
        n = len(s["steps"])
        rows.append(f"| {s['name']} | {d} | {n} | {bar(d, n)} {round(100 * d / n) if n else 0}% |")
    summary = f"**Overall: {done}/{total} steps ({pct}%)** {bar(done, total)}"
    nxt_line = f"**Next up:** {nxt[0]} {nxt[1]}" if nxt else "**All tracked steps complete.**"
    block = "\n".join([summary, "", *rows, "", nxt_line])
    full = "# Build progress (generated from PLAN.md; do not edit)\n\n" + block + "\n"
    return block, full


def main():
    sections = parse(PLAN.read_text(encoding="utf-8"))
    block, full = render(sections)
    readme = README.read_text(encoding="utf-8")
    if START not in readme or END not in readme:
        sys.exit("README.md is missing the PROGRESS markers")
    new_readme = re.sub(
        re.escape(START) + r".*?" + re.escape(END),
        START + "\n" + block + "\n" + END,
        readme,
        flags=re.S,
    )
    if "--check" in sys.argv:
        stale = (
            (not OUT.exists()) or OUT.read_text(encoding="utf-8") != full or new_readme != readme
        )
        if stale:
            sys.exit("Progress files are out of date: run `python scripts/progress.py`")
        print("progress up to date")
        return
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(full, encoding="utf-8")
    README.write_text(new_readme, encoding="utf-8")
    print(block)


if __name__ == "__main__":
    main()
