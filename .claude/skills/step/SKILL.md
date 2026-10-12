---
name: step
description: Implement the next unchecked step in PLAN.md (or the step id given), end to end, with tracking.
disable-model-invocation: true
---
Target: $ARGUMENTS (if empty, the first unchecked step in PLAN.md, in order).

1. Read CLAUDE.md, the step in PLAN.md and only the spec sections it lists. If the previous step is not ticked, stop and say so.
2. `git switch main && git pull`; create branch `<step-id-lowercase>-<slug>`.
3. Write a short plan (files, tests, commands, risks) and WAIT for my approval.
4. Implement in small commits prefixed with the step id. Write the tests named for the step first or alongside them. Never weaken tests or fixtures.
5. Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest -q`. Report the actual results.
6. Run the spec-reviewer subagent on the diff; fix correctness findings only.
7. Tick the step in PLAN.md, append docs/PROGRESS_LOG.md, run `python scripts/progress.py`, commit.
8. Push the branch and open a PR from the template (`Closes #<issue>` if it exists). Do not merge.
9. Report: branch, PR URL, tests and results, deviations (also in docs/DECISIONS.md), unverified items, next step. Stop.
