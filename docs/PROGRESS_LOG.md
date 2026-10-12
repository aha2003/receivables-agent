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

## 2026-10-12 — [S1.02] Money helpers
- PR: #66 | Branch: s1-02-money-helpers | Commit: see PR head
- Tests: `uv run pytest -q` → 47 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 100%
- Spec / test IDs covered: §3.5 / T-MONEY-01
- Deviations or decisions (link docs/DECISIONS.md): D-005 (value-based decimal places; S1.09 strict-format note; 4300-digit bound)
- Next step: S1.03 Clock port and architecture guard

## 2026-10-12 — [S1.03] Clock port and architecture guard
- PR: #68 | Branch: s1-03-clock-arch-guard | Commit: see PR head
- Tests: `uv run pytest -q` → 115 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 100%
- Spec / test IDs covered: §3.5, §3.1 / T-ARCH-01, clock unit tests
- Deviations or decisions (link docs/DECISIONS.md): D-006 (revised after spec review)
- Next step: S1.04 Database schema and migrations

## 2026-10-12 — [S1.04] Database schema and migrations
- PR: #69 | Branch: s1-04-db-schema-migrations | Commit: see PR head
- Tests: `uv run pytest -q` → 154 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 100%
- Spec / test IDs covered: §5, §3.5 / T-DB-01
- Deviations or decisions (link docs/DECISIONS.md): D-007 (SPEC §5 date CHECKs fixed to `IS`), D-008 (env settings only; tenant `demo`; tests/db)
- Next step: S1.05 Golden synthetic fixtures
