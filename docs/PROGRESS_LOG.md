# Progress log

Append one entry per completed step (newest last). Format:

## YYYY-MM-DD — [S1.01] Title
- PR: #n | Branch: s1-01-slug | Commit: abc1234
- Tests: command → result (counts)
- Spec / test IDs covered:
- Deviations or decisions (link docs/DECISIONS.md):
- Next step:

## 2026-10-12 — [S1.01] Project scaffold, tooling and CI
- PR: #65 | Branch: s1-01-scaffold-tooling-ci | Commit: see PR head
- Tests: `uv run pytest -q` → 2 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 100% (floor 85% each); pip-audit no known vulnerabilities
- Spec / test IDs covered: §3.1, §3.4, §3.6, §16.1 / T-CI-01 (via PR CI), smoke test
- Deviations or decisions (link docs/DECISIONS.md): D-001, D-002, D-003, D-004
- Next step: S1.02 Money helpers
