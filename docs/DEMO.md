# Demo script

All data is the fictional company in `fixtures/` (SPEC §6.8). Never demo with real company data. For Slice 1, the dashboard totals, aging, exposure and queue order in steps 2 to 4 are the golden results of `fixtures/expected/golden_results.json` and are checked automatically by `tests/test_slice1_exit.py`. The rest of the expected output was observed when the walkthrough was run by hand on 2026-10-13; re-check it before a demo.

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

## Slice 2: the visible agent loop (no LLM)

Shows gate D1 (SPEC §12.1): the agent inspects the queue, plans with the deterministic rule planner, calls tools, is denied by policy, reaches waiting states and shows a complete trace. No LLM is involved. The expected output below is checked automatically by `tests/test_slice2_exit.py` (clock fixed at Saturday 2026-10-31 09:00 Dubai) and was observed by hand on 2026-10-14; the policy reasons depend on the time you run it (see step 4).

### Setup

As for Slice 1: `uv run receivables reset` (stop the app first), sign in as `demo.user`. The configured autonomy level is L1 (assist): nothing is sent by the system.

### Walkthrough

| # | Do | Expect |
|---|---|---|
| 1 | On the dashboard, enter **As of** 2026-10-31 in the "Run agent" form and press **Run agent now (level L1)**. | The run page: planner `rules`, 9 cases, each with its steps. |
| 2 | On the run page, open **INV-004**. | Flags: "Open dispute: po amount mismatch." Case: dispute open, next follow-up 2026-11-03. Trace: `request_internal_document` (po_copy from finance), `draft_message` (tone 3 with the dispute label), `schedule_followup` (3 days), each "done". Draft panel: "Review before sending. Nothing is sent automatically." and the template text. |
| 3 | Open **INV-011** from the run page. | Case escalated; trace `draft_message` (tone 3), `escalate` (to finance, with the task it created), `schedule_followup`. |
| 4 | Open **INV-003**. | Trace: `draft_message`, `send_email`, `schedule_followup`. The send shows "denied" and "waiting for human approval" with its reasons in plain words: "This customer is not on the autopilot allowlist." and, outside Monday to Friday 09:00 to 17:00 Dubai, "Outside business hours". |
| 5 | Open **Approvals** in the nav. | Two pending items, INV-003 and INV-012, each with the draft subject and the reasons. Approve and Reject are shown but arrive with Slice 3. |
| 6 | Back on the dashboard, tick **Dry run** and run again. | A "Dry run" page: "Nothing was saved." with the counts (9 cases, 22 steps would execute, 2 denied by policy); no new run appears in Approvals or the runs. |

Kill switch: an admin turns it on with `POST /api/reauth` (password re-entry) and `POST /api/agent/kill-switch {"on": true}`; every page then shows "KILL SWITCH ON" and the next run records every step as denied with "The kill switch is on: nothing is sent." There is no button for it yet; the automated test exercises it.

### What Slice 2 does not show

No LLM planner, classification, LLM drafts, inbox, human approval, or sending from the sandbox outbox (Slice 3); no simulator or learning (Slice 4). At L1 nothing is ever sent by the system.
