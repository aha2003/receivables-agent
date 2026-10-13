# Demo script

All data is the fictional company in `fixtures/` (SPEC §6.8). Never demo with real company data. The dashboard totals, aging, exposure and queue order in steps 2 to 4 are the golden results of `fixtures/expected/golden_results.json` and are checked automatically by `tests/test_slice1_exit.py`. The rest of the expected output was observed when the walkthrough was run by hand on 2026-10-13; re-check it before a demo.

## Slice 1: deterministic core and first screen

Shows: seeded synthetic data, golden numbers, a heuristic priority queue, time travel and safe CSV import. No agent or LLM yet.

### Setup (before the audience)

1. `uv sync --frozen`
2. Stop any running app, then `uv run receivables reset`. Expect `SYNTHETIC DEMO DATA`, `customers=3 invoices=14 payments=4 events=2` and a summary line for today.
3. `uv run receivables create-user demo.user` (first time only; password at least 12 characters).
4. In the shell: `export SESSION_SECRET=...` (at least 32 random characters, never committed), then `uv run uvicorn receivables.api.main:app --reload --no-proxy-headers`.

### Walkthrough

| # | Do | Expect |
|---|---|---|
| 1 | Open <http://localhost:8000/>. | Redirect to the sign-in page; the `SYNTHETIC DEMO DATA` banner is already visible. |
| 2 | Sign in as `demo.user`, then open <http://localhost:8000/?as_of=2026-10-31>. | Dashboard as of 2026-10-31: total outstanding AED 193,000.00; overdue AED 147,000.00; estimated DSO 149.7 (hover for the formula: 90-day sales proxy); unallocated cash AED 0.00. |
| 3 | Look at aging and exposure. | Aging: current 46,000.00 · 1–30 24,000.00 · 31–60 36,000.00 · 61–90 32,000.00 · 90+ 55,000.00. Exposure by band (overdue, heuristic): High 57,000.00 · Medium 81,000.00 · Low 9,000.00. |
| 4 | Open <http://localhost:8000/queue?as_of=2026-10-31> (the nav links open today's date). | 9 items in this order: INV-004, INV-011, INV-005, INV-008, INV-003, INV-014, INV-009, INV-012, INV-013. Top item INV-004: risk 81.7 (High), priority value 40833.3, "Resolve dispute: PO_AMOUNT_MISMATCH". Scores are labelled "Heuristic score. Not a probability." |
| 5 | Choose High in the Band menu. | Only the table changes (htmx, no page reload): INV-004 and INV-014. The Filter button is the no-JavaScript fallback. |
| 6 | Click INV-004. | Invoice page with facts as of 2026-10-31: open AED 50,000.00, 122 days overdue (90+), risk 81.7 (High), and the 2026-09-10 dispute in the timeline. |
| 7 | Time travel: open <http://localhost:8000/?as_of=2026-09-30>, then the queue with the same `as_of`, then INV-005. | Facts known on 2026-09-30 only: data quality shows 13 invoices and 3 payments (INV-006 not yet issued, P-003 not yet received); the queue has 6 items; INV-005 is open at AED 30,000.00 with no timeline entries. |
| 8 | Upload page: upload `fixtures/bad_invoices.csv` with the invoices form. | `invoices: rejected`, 9 rows, "Nothing was written.", and one error per row (E_FK_MISSING, E_DUP_KEY, E_BAD_DATE, E_DUE_BEFORE_ISSUE, E_BAD_AMOUNT, E_CURRENCY, E_REQUIRED_EMPTY). The dashboard is unchanged. |
| 9 | Upload `fixtures/customers.csv` with the customers form. | `customers: duplicate`, "This exact file was already imported; nothing changed." |

### What Slice 1 does not show

No agent run, plan, tools, policy, drafts, inbox, approvals or outbox; these arrive in Slices 2 and 3. Scores are heuristics for ordering work, not probabilities of payment.

### Reset after the demo

Stop the app, then `uv run receivables reset`.
