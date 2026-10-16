# Receivables Collections Agent

Competition prototype (Dubai Chamber of Digital Economy AI Agents Championship). **Synthetic data only. Simulation shows mechanics, not evidence.**

## Progress
<!-- PROGRESS:START -->
**Overall: 35/64 steps (55%)** ███████████░░░░░░░░░

| Section | Done | Total | Progress |
|---|---|---|---|
| Slice 1 — Deterministic core + first screen | 14 | 14 | ████████████████████ 100% |
| Slice 2 — Visible agent loop | 11 | 11 | ████████████████████ 100% |
| Slice 3 — Claude integration + human approval | 10 | 13 | ███████████████░░░░░ 77% |
| Checkpoint C1 — January 2027 submission pack | 0 | 7 | ░░░░░░░░░░░░░░░░░░░░ 0% |
| Slice 4 — Simulator + learning proof | 0 | 9 | ░░░░░░░░░░░░░░░░░░░░ 0% |
| Slice 5 — Hardening + final rehearsal | 0 | 10 | ░░░░░░░░░░░░░░░░░░░░ 0% |

**Next up:** S3.11 Payment allocation and unallocated-cash screen
<!-- PROGRESS:END -->

Details: `docs/PROGRESS.md` · plan: `PLAN.md` · spec: `docs/SPEC.md` · log: `docs/PROGRESS_LOG.md` · demo: `docs/DEMO.md`

## What works today (Slices 1 and 2)

Slice 1 is the deterministic core and the first screen; Slice 2 is a visible agent loop that needs **no LLM**. Claude arrives in Slice 3.

- **Deterministic core** (`src/receivables/core/`, pure Python, no I/O): money in integer fils, ledger and open amounts, aging buckets, payment history, heuristic risk score, priority queue, KPIs (total open, overdue, estimated DSO on a 90-day sales proxy, unallocated cash, exposure by band); the draft fact block, the deterministic draft templates and the draft validator. Every number matches the golden fixtures in `fixtures/expected/golden_results.json` (SPEC §6.9).
- **Agent** (`src/receivables/agent/`): case state machine; a closed registry of 11 tools (none can change amounts or choose recipients); a deterministic rule planner; a policy engine that checks every rule and returns reason codes; an orchestrator that runs a tick at L0 (shadow), L1 (assist, default) or L2 (policy-gated autopilot, sandbox outbox only), turns denied or L1 sends into approvals and records every run and step; an admin-only, audited kill switch.
- **Data**: SQLite schema with two Alembic migrations (ledger and agent tables); fixed-schema CSV import (all-or-nothing per file, duplicates detected, audited); seeded reset from the golden synthetic company. Every state change and policy decision is in the append-only audit log.
- **Web app**: session login (Argon2id, rate limited, CSRF, strict security headers); dashboard, queue, case page (facts, flags in plain language, case state, drafts, the agent trace with each tool call and policy decision), approvals, run pages, upload; a "Run agent" form; a kill-switch banner. JSON API: dashboard, queue, invoice detail, imports, agent run, trace, approvals, kill switch and password re-entry. Every page shows the `SYNTHETIC DEMO DATA` banner and the `as_of` date of its numbers.
- **Time travel**: the dashboard, queue and case pages and `/api/dashboard`, `/api/queue` and `/api/invoices/{n}` accept `?as_of=YYYY-MM-DD`; agent runs may use another date only below L2 or as a dry run.
- **Quality gates**: ruff, pyright, pytest with an 85% coverage floor on the core and the agent, pip-audit, gitleaks and a progress check in CI.

**Not built yet:** Claude (planner, classification, drafts), redaction, the inbox, human approve/reject and the full sandbox mail gateway (Slice 3); the simulator, outcome inference and playbook (Slice 4); one-command start and hardening (Slice 5).

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

`reset` builds (or replaces) `data/demo.db` (migrations plus the golden synthetic data); run it with the app stopped. `create-user` prompts twice for a password of at least 12 characters.

Settings and secrets come from a local `.env` file (git-ignored; start from `.env.example`). Set `SESSION_SECRET` there to at least 32 random characters, for example the output of `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`. Never commit it. Then start the app; uv loads `.env` into the app process only:

```bash
uv run --env-file .env uvicorn receivables.api.main:app --reload --no-proxy-headers
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
