# Receivables Collections Agent

Competition prototype (Dubai Chamber of Digital Economy AI Agents Championship). **Synthetic data only. Simulation shows mechanics, not evidence.**

## Progress
<!-- PROGRESS:START -->
**Overall: 24/64 steps (38%)** ████████░░░░░░░░░░░░

| Section | Done | Total | Progress |
|---|---|---|---|
| Slice 1 — Deterministic core + first screen | 14 | 14 | ████████████████████ 100% |
| Slice 2 — Visible agent loop | 10 | 11 | ██████████████████░░ 91% |
| Slice 3 — Claude integration + human approval | 0 | 13 | ░░░░░░░░░░░░░░░░░░░░ 0% |
| Checkpoint C1 — January 2027 submission pack | 0 | 7 | ░░░░░░░░░░░░░░░░░░░░ 0% |
| Slice 4 — Simulator + learning proof | 0 | 9 | ░░░░░░░░░░░░░░░░░░░░ 0% |
| Slice 5 — Hardening + final rehearsal | 0 | 10 | ░░░░░░░░░░░░░░░░░░░░ 0% |

**Next up:** S2.11 Slice 2 close-out
<!-- PROGRESS:END -->

Details: `docs/PROGRESS.md` · plan: `PLAN.md` · spec: `docs/SPEC.md` · log: `docs/PROGRESS_LOG.md` · demo: `docs/DEMO.md`

## What works today (Slice 1)

Slice 1 is the deterministic core and the first screen. There is **no agent and no LLM yet**.

- **Deterministic core** (`src/receivables/core/`, pure Python, no I/O): money in integer fils, ledger and open amounts, aging buckets, payment history, heuristic risk score, priority queue, KPIs (total open, overdue, estimated DSO on a 90-day sales proxy, unallocated cash, exposure by band). Every number matches the golden fixtures in `fixtures/expected/golden_results.json` (SPEC §6.9).
- **Data**: SQLite schema with Alembic migrations; fixed-schema CSV import for customers, invoices, payments and events (all-or-nothing per file, duplicates detected, every import audited); seeded reset from the golden synthetic company.
- **Web app**: session login (Argon2id, rate limited, CSRF, strict security headers); dashboard, priority queue (band and customer filters), invoice page, CSV upload page; JSON API `/api/dashboard`, `/api/queue`, `POST /api/imports/{kind}`. Every page shows the `SYNTHETIC DEMO DATA` banner, the `as_of` date of its numbers, and labels scores as heuristic.
- **Time travel**: the dashboard, queue and invoice pages and `/api/dashboard` and `/api/queue` accept `?as_of=YYYY-MM-DD` and rebuild the facts known on that date.
- **Quality gates**: ruff, pyright, pytest with an 85% coverage floor on the core, pip-audit, gitleaks and a progress check in CI.

**Not built yet:** the agent loop, policy engine and trace (Slice 2); Claude, drafts, inbox, approvals and the sandbox outbox (Slice 3); the simulator (Slice 4); one-command start and hardening (Slice 5).

## Quickstart

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/). All data is the fictional company in `fixtures/`.

```bash
uv sync --frozen
```

```bash
uv run receivables reset
```

```bash
uv run receivables create-user demo.user
```

`reset` builds (or replaces) `data/demo.db` (migrations plus the golden synthetic data); run it with the app stopped. `create-user` prompts twice for a password of at least 12 characters. Then set a session secret of at least 32 characters in your shell (never commit it) and start the app:

```bash
export SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

```bash
uv run uvicorn receivables.api.main:app --reload --no-proxy-headers
```

Open <http://localhost:8000/?as_of=2026-10-31> and sign in. The golden numbers are defined for 2026-10-31; without `as_of` the dashboard uses today's date. The session cookie is `Secure`; Chromium accepts it on `localhost` (other browsers not yet checked). The full walkthrough is in `docs/DEMO.md`.

## Development

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

```bash
uv run pytest -q
```

Rules for contributors (human or Claude Code) are in `CLAUDE.md`; behaviour and acceptance tests are in `docs/SPEC.md`; decisions and deviations are in `docs/DECISIONS.md`; dependencies and licences are in `PROVENANCE.md`.
