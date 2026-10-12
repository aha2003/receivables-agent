## Step
<!-- e.g. [S1.06] Ledger and aging (pure) — Closes #12 -->

## Spec and tests covered
- Spec sections:
- Test IDs:

## Verification (paste actual output summaries)
- [ ] `uv run ruff check .` clean
- [ ] `uv run ruff format --check .` clean
- [ ] `uv run pyright` clean
- [ ] `uv run pytest -q` all pass (counts: )
- [ ] CI green

## Safety checklist
- [ ] Synthetic data only; no secrets, `.env` or database files committed
- [ ] `core/` still pure; money via `core.money`; time via Clock
- [ ] No LLM output can change ledger values; tools only from the closed registry; side effects policy-gated
- [ ] Tests and fixtures not weakened
- [ ] New dependencies recorded in PROVENANCE.md

## Tracking
- [ ] Step ticked in PLAN.md
- [ ] docs/PROGRESS_LOG.md entry added
- [ ] `python scripts/progress.py` run; docs/PROGRESS.md and README block updated

## Deviations / decisions
<!-- link docs/DECISIONS.md entries, or "none" -->
