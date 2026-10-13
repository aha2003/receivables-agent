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

## 2026-10-12 — [S1.05] Golden synthetic fixtures
- PR: #70 | Branch: s1-05-golden-fixtures | Commit: see PR head
- Tests: `uv run pytest -q` → 180 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 100%
- Spec / test IDs covered: §6.8, §6.9, §6.10 / fixture loader test (counts), spec-parity and consistency checks
- Deviations or decisions (link docs/DECISIONS.md): D-009 (fixture formats)
- Next step: S1.06 Ledger and aging (pure)

## 2026-10-13 — [S1.06] Ledger and aging (pure)
- PR: #71 | Branch: s1-06-ledger-aging | Commit: see PR head
- Tests: `uv run pytest -q` → 222 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 100%
- Spec / test IDs covered: §6.1, §6.2 (with §2 definitions) / T-LED-01, T-LED-02, T-AGE-01, T-AGE-02
- Deviations or decisions (link docs/DECISIONS.md): D-010 (revised: follow §2; open amount clamps at 0; write-offs never settle)
- Next step: S1.07 History, risk score, queue and next actions (pure)

## 2026-10-13 — [S1.07] History, risk score, queue and next actions (pure)
- PR: #73 | Branch: s1-07-history-risk-queue | Commit: see PR head
- Tests: `uv run pytest -q` → 283 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 100%
- Spec / test IDs covered: §6.3, §6.4, §6.5 (with §2, §5 engine defaults) / T-HIS-01, T-RSK-01, T-RSK-02, T-QUE-01, T-ACT-01
- Deviations or decisions (link docs/DECISIONS.md): D-011 (band on rounded score; EngineConfig; config.yaml loader moved; promise/dispute edge cases); D-008 updated
- Next step: S1.08 KPIs and unallocated cash (pure)

## 2026-10-13 — [S1.08] KPIs and unallocated cash (pure)
- PR: #74 | Branch: s1-08-kpis-unallocated | Commit: see PR head
- Tests: `uv run pytest -q` → 308 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 100%
- Spec / test IDs covered: §6.6, §6.7 (with §2) / T-KPI-01, T-UNA-01
- Deviations or decisions (link docs/DECISIONS.md): D-012 (half-up rounding; share precision; unallocated cash composition; queue flags)
- Next step: S1.09 Validation and strict fixed-schema CSV import

## 2026-10-13 — [S1.09] Validation and strict fixed-schema CSV import
- PR: #75 | Branch: s1-09-validation-import | Commit: see PR head
- Tests: `uv run pytest -q` → 414 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 100%; importer and engine 100%
- Spec / test IDs covered: §4.1–§4.6, §5, §9 (shape) / T-IMP-01, T-IMP-02, T-IMP-03, T-UNA-02; T-IMP-04, T-IMP-05 at service level (HTTP in S1.13)
- Deviations or decisions (link docs/DECISIONS.md): D-013 (route to S1.13; scope; write semantics; codes; review-driven rules)
- Next step: S1.10 Repository layer, audit writer and seeded reset

## 2026-10-13 — [S1.10] Repository layer, audit writer and seeded reset
- PR: #76 | Branch: s1-10-repo-audit-reset | Commit: see PR head
- Tests: `uv run pytest -q` → 449 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 100%; seed, cli, repo, importer 100%
- Spec / test IDs covered: §3.4, §5 / T-AUD-01 (imports), reset idempotent with exactly the golden data; `uv run receivables reset` reproduces the golden numbers
- Deviations or decisions (link docs/DECISIONS.md): D-014 (no prompt; check-first stage-then-swap reset; audit row shape); D-008 and D-011 updated (config.yaml loader in S1.13)
- Next step: S1.11 App shell and session login

## 2026-10-13 — [S1.11] App shell and session login
- PR: #77 | Branch: s1-11-app-shell-login | Commit: see PR head
- Tests: `uv run pytest -q` → 504 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 100%; api/ and cli 100%
- Spec / test IDs covered: §3.3, §9 / T-SEC-03, T-SEC-10
- Deviations or decisions (link docs/DECISIONS.md): D-015 (stateless cookie, client-side logout until S5.04; atomic bounded limiter; audited login/logout/lockout; X-Forwarded-For gap for S5.03)
- Next step: S1.12 Web security baseline

## 2026-10-13 — [S1.12] Web security baseline
- PR: #78 | Branch: s1-12-web-security | Commit: see PR head
- Tests: `uv run pytest -q` → 543 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 100%; api/ 100%
- Spec / test IDs covered: §3.3 / T-SEC-09, T-SEC-11
- Deviations or decisions (link docs/DECISIONS.md): D-016 (CSRF incl. /login; headers middleware + 500 handler; public /static; upstream content-type quirk)
- Next step: S1.13 Dashboard and queue (first screen)
