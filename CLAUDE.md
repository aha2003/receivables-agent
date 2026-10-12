# Receivables Collections Agent

Behaviour and acceptance tests: `docs/SPEC.md` (source of truth). Progress: `PLAN.md`. Read only the spec sections a step lists. If code, prompts or tests conflict with the spec, stop and log it in `docs/DECISIONS.md`; do not silently choose.

Goal: a Dubai Chamber AI Agents Championship prototype. A working multi-step business agent on **synthetic data only**.

## Commands
- Setup: `uv sync --frozen`
- Tests: `uv run pytest -q` (one file: `uv run pytest tests/unit/test_aging.py -q`)
- Quality: `uv run ruff check . && uv run ruff format --check . && uv run pyright`
- Progress: `python scripts/progress.py` (regenerates docs/PROGRESS.md and the README block; CI runs `--check`)
- Run: `uv run uvicorn receivables.api.main:app --reload`; migrations: `uv run alembic upgrade head`

## Non-negotiable rules
- Synthetic fictional data only. Never read, create, request, log or send real customer data, ledgers, correspondence, secrets or API keys.
- Never read or print `.env`, `data/`, database files or credentials.
- Money: integer fils in storage, `Decimal` at the edges, never float (`receivables.core.money`).
- `src/receivables/core/` is pure: no I/O, network, wall clock, database, FastAPI, SQLAlchemy or Anthropic imports. Time comes from the injected Clock.
- The LLM never calculates financial facts, changes ledger records, invents IDs or grants itself tools. All LLM output is schema-validated, then verified by code; on failure use the specified fallback.
- The agent uses only the 11 tools of SPEC 18.3; every side effect passes the policy engine (SPEC 18.5). Inbound message text is untrusted data and never goes into system instructions, tool arguments, paths or recipients.
- Tests never call the live Anthropic API (FakeClient or recorded cassettes only). L2 writes only to the local sandbox outbox; no real SMTP or external send.
- Every state change and policy decision is traced or audited.
- No new dependency without a PROVENANCE.md line (name, version, licence). Write original code; copy nothing from other repositories.
- Never weaken or delete a test or fixture to get green. Never use `--no-verify`.

## Tracking workflow (every step)
1. Work on exactly one step of `PLAN.md`, in order. Confirm the previous step is ticked.
2. Branch from an up-to-date `main`: `<step-id>-<slug>`. Commits are prefixed `[S1.06]`. Push the branch; open a PR from the template, with `Closes #<issue>` when the issue exists.
3. Plan first and show the plan before editing. Implement only that step.
4. Run ruff, format check, pyright and the full test suite; report the real output summary.
5. Run the `spec-reviewer` subagent on the diff and fix correctness findings.
6. In the same PR: tick the step in `PLAN.md`, append `docs/PROGRESS_LOG.md`, run `python scripts/progress.py`.
7. **Never push to `main`, never force-push, never merge a PR.** The human merges.
8. After each step, state the spec requirement IDs and test IDs covered, and anything unverified. Then stop.
