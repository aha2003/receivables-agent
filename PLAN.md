# PLAN.md — trackable build plan

Source of truth for behaviour: `docs/SPEC.md`. This file is the source of truth for **progress**. Working rules: `CLAUDE.md`. Synthetic data only.

## How tracking works
- Each step is one branch, one PR, one issue. Branch: `<step-id>-<slug>` (for example `s1-06-ledger-aging`). Commit prefix: `[S1.06]`.
- A step is ticked `[x]` **only in the PR that completes it**, after its listed tests pass and CI is green. Merging the PR is the human approval.
- Every completed step also appends an entry to `docs/PROGRESS_LOG.md` and regenerates `docs/PROGRESS.md` and the README progress block with `python scripts/progress.py` (CI runs `--check`).
- Do not skip ahead. Do not tick a step with failing or skipped tests. Spec conflicts and deviations go to `docs/DECISIONS.md`, and the step stops until resolved.
- Step tests are IDs from the spec (Sections 3.6, 13 to 15, 18, 19). Manual checks are stated as such.
- Deferred (not counted): mapping layer and T-IMP-06, T-MAP-01..03, T-API-08 (post-competition); real shadow mode; T-PERF-01 (optional).

Total tracked steps: 64.

## Slice 1 — Deterministic core + first screen

Planning window Oct–Nov 2026 [A]. Priority P0. Exit: golden numbers match, seeded demo starts, CI green, UI says synthetic.

- [x] **S1.01** Project scaffold, tooling and CI
  - Goal: uv project (Python 3.12, src layout `receivables`), ruff, pyright, pytest with cov and hypothesis, pre-commit, PROVENANCE.md, and a GitHub Actions workflow running `uv sync --frozen`, ruff, format check, pyright, pytest and `python scripts/progress.py --check`; add gitleaks and pip-audit jobs.
  - Spec: §3.1, §3.4, §3.6, §16.1
  - Tests: T-CI-01
  - Done when: CI is green on the PR; `uv run pytest -q` passes with one smoke test; PROVENANCE.md lists every dependency with version and licence.
- [x] **S1.02** Money helpers
  - Goal: `core/money.py` with `to_fils` and `from_fils`; floats rejected; more than two decimals rejected.
  - Spec: §3.5
  - Tests: T-MONEY-01
  - Done when: All assertions pass, including the hypothesis round-trip property.
- [x] **S1.03** Clock port and architecture guard
  - Goal: `adapters/clock.py` (system, fixed and simulated clocks) and a static test that `core/` has no forbidden imports or wall-clock calls.
  - Spec: §3.5
  - Tests: T-ARCH-01
  - Done when: The guard test fails if a forbidden import or `datetime.now` is added to `core/`.
- [x] **S1.04** Database schema and migrations
  - Goal: pydantic-settings; SQLAlchemy models and the first Alembic migration from the Section 5 DDL; per-connection PRAGMAs; append-only audit triggers.
  - Spec: §5, §3.5
  - Tests: T-DB-01
  - Done when: Migrations apply to a fresh file; foreign keys are on for every connection; `audit_log` UPDATE and DELETE raise; a bad date and a zero amount are rejected.
- [x] **S1.05** Golden synthetic fixtures
  - Goal: Write the fixtures of Sections 6.8 to 6.10 (customers, invoices, payments, events, bad invoices, 15 labelled messages) and the expected results under `fixtures/`, with loader helpers.
  - Spec: §6.8 to §6.10
  - Tests: fixture loader test: 3 customers, 14 invoices, 4 payments, 2 events, 15 labelled messages
  - Done when: Fixture content matches the spec exactly; all data is synthetic.
- [x] **S1.06** Ledger and aging (pure)
  - Goal: Open amounts, as-of time travel, days overdue and buckets.
  - Spec: §6.1, §6.2
  - Tests: T-LED-01, T-LED-02, T-AGE-01, T-AGE-02
  - Done when: Total open 193,000.00 and overdue 147,000.00 at 2026-10-31; boundary buckets correct.
- [x] **S1.07** History, risk score, queue and next actions (pure)
  - Goal: Customer history with portfolio fallback, heuristic risk score and bands, priority value, queue ordering, next-action rules.
  - Spec: §6.3 to §6.5
  - Tests: T-HIS-01, T-RSK-01, T-RSK-02, T-QUE-01, T-ACT-01
  - Done when: Default-config and `min_history_invoices = 2` queue tables match exactly; outputs are labelled heuristic.
- [x] **S1.08** KPIs and unallocated cash (pure)
  - Goal: Estimated DSO (90-day sales proxy), overdue by customer, exposure by band, unallocated cash and its queue flag.
  - Spec: §6.6, §6.7
  - Tests: T-KPI-01, T-UNA-01
  - Done when: DSO 149.7; customer shares match; unallocated cash never changes open amounts.
- [x] **S1.09** Validation and strict fixed-schema CSV import
  - Goal: Validation of the fixed canonical CSV schema with the error codes of Section 4; atomic, idempotent import (mapping hash `fixed-schema-v1`). The mapping layer and T-IMP-06 are deferred.
  - Spec: §4.1 to §4.6, §5
  - Tests: T-IMP-01, T-IMP-02, T-IMP-03, T-IMP-04, T-IMP-05, T-UNA-02
  - Done when: A bad file returns 422 with line, code and field and changes nothing; a repeat upload is a duplicate.
- [x] **S1.10** Repository layer, audit writer and seeded reset
  - Goal: Repository functions, an audit writer using the injected clock, and CLI commands `seed` and `reset` that load the synthetic company.
  - Spec: §3.4, §5
  - Tests: T-AUD-01 (import audit rows); reset is idempotent and leaves exactly the golden data
  - Done when: `uv run receivables reset` reproduces the golden numbers with no manual database edits.
- [x] **S1.11** App shell and session login
  - Goal: FastAPI app factory, `/healthz`, Argon2id login and logout, signed session cookie, `create-user` CLI, login rate limit.
  - Spec: §3.3, §9
  - Tests: T-SEC-03, T-SEC-10
  - Done when: Unauthenticated requests are rejected; the sixth failed login in a minute returns 429.
- [x] **S1.12** Web security baseline
  - Goal: CSRF token on every state-changing request, security headers with a strict CSP, vendored static assets (no CDN).
  - Spec: §3.3
  - Tests: T-SEC-09, T-SEC-11
  - Done when: POST without a valid token returns 403; headers present on pages and API.
- [x] **S1.13** Dashboard and queue (first screen)
  - Goal: `/api/dashboard`, `/api/queue`, `POST /api/imports/{kind}` (wraps the S1.09 importer; D-013), HTMX pages, a persistent `SYNTHETIC DEMO DATA` banner, heuristic labels and the DSO tooltip.
  - Spec: §6, §9, §10, §4.6
  - Tests: T-API-01, T-API-02, T-API-03, T-UI-02, T-UI-05; T-IMP-04 and T-IMP-05 over HTTP
  - Done when: The seeded demo shows the golden totals and queue order; labels say heuristic and synthetic; a bad upload returns HTTP 422 and a repeat upload is a duplicate.
- [x] **S1.14** Slice 1 close-out
  - Goal: Full suite green; README states what works; demo steps recorded; tag `slice-1-complete`.
  - Spec: §16
  - Tests: full test suite; D2 partially
  - Done when: Exit condition of Slice 1 met: golden numbers match, seeded demo starts, CI green, UI says synthetic.

## Slice 2 — Visible agent loop

Planning window Nov–Dec 2026 [A]. Priority P0. Exit: the agent inspects the queue, plans, calls tools, is denied by policy and shows a complete trace, with no LLM required.

- [x] **S2.01** Agent tables migration
  - Goal: Second Alembic migration with the agent tables of Section 18.10.
  - Spec: §18.10
  - Tests: T-DB-01 extended
  - Done when: All agent tables exist with their constraints; foreign keys enforced.
- [x] **S2.02** Case state machine
  - Goal: States and deterministic transitions per 18.2.
  - Spec: §18.2
  - Tests: T-CASE-01
  - Done when: The case walks the full state path in the spec.
- [x] **S2.03** Closed tool registry
  - Goal: Exactly 11 typed tools with Pydantic argument models; read tools implemented; no tool can change amounts or free-form recipients.
  - Spec: §18.3
  - Tests: T-AGT-03
  - Done when: Registered tool names equal the 11 in the spec; none writes invoice or payment amounts.
- [x] **S2.04** Policy engine
  - Goal: `evaluate_send` with every reason code, evaluating all rules without short-circuit.
  - Spec: §18.4, §18.5
  - Tests: T-POL-01 to T-POL-11 (except the kill-switch wiring in S2.08)
  - Done when: Reason lists match the spec tables exactly.
- [x] **S2.05** Template drafts and draft validator (pure)
  - Goal: Fact block, deterministic templates, validator (unknown invoice, amount, date, forbidden phrases), tone mapping.
  - Spec: §8.1, §8.2, §8.4
  - Tests: T-DRF-01, T-DRF-02, T-DRF-04
  - Done when: Valid draft passes; violating draft yields exactly the four violation codes.
- [x] **S2.06** Rule planner
  - Goal: Deterministic planner mirroring 6.5.
  - Spec: §18.6
  - Tests: T-AGT-08
  - Done when: Tool sequences match the table for every golden queue item.
- [x] **S2.07** Orchestrator tick and trace
  - Goal: Select up to 20 cases, up to 5 steps each, policy-check every side effect, convert denied or L1 sends to approvals, record runs and steps with the injected clock.
  - Spec: §18.6
  - Tests: T-AGT-01, T-AGT-02, T-AGT-04, T-AGT-05, T-AGT-07
  - Done when: A tick is deterministic, budget-limited and fully traced; an unknown tool is rejected.
- [x] **S2.08** Policy audit and kill switch
  - Goal: Audit every executed and denied step; admin-only, audited kill switch.
  - Spec: §18.5, §3.3
  - Tests: T-POL-09, T-AUD-01
  - Done when: With the kill switch on nothing executes; every decision is auditable.
- [x] **S2.09** Agent API
  - Goal: Agent run, trace, approvals list, kill-switch and invoice detail endpoints.
  - Spec: §18.11, §9
  - Tests: T-API-04
  - Done when: Endpoints return the documented shapes and require a session and CSRF.
- [x] **S2.10** Case detail and trace UI
  - Goal: Case page showing facts, flags, plan, tool calls, policy reasons and results; approvals view.
  - Spec: §10, §18
  - Tests: T-UI-04
  - Done when: An evaluator can follow plan, tool, policy decision and result for one case.
- [x] **S2.11** Slice 2 close-out
  - Goal: Demonstrate gate D1 without any LLM; tag `slice-2-complete`.
  - Spec: §12.1, §16
  - Tests: D1 (no LLM)
  - Done when: The agent inspects the queue, plans, calls tools, is denied by policy and shows a complete trace.

## Slice 3 — Claude integration + human approval

Planning window Dec 2026–Jan 2027 [A]. Priority P0/P1. Exit: end-to-end demo with live Claude; safe fallback on failure; injection and policy tests pass.

- [x] **S3.01** LLM port with Fake and Replay clients
  - Goal: `LLMClient` protocol, Pydantic models for classify, plan and draft, cassette format, FakeClient and ReplayClient.
  - Spec: §3.5, §7.2
  - Tests: T-LLM-04
  - Done when: A cassette replays byte-identically; an unrecorded request raises; no network is touched.
- [x] **S3.02** Redaction and pseudonymisation (pure)
  - Goal: Redact TRN, IBAN, email and phone; pseudonymise customer names.
  - Spec: §7.5
  - Tests: T-RED-01
  - Done when: Output matches the spec string exactly.
- [x] **S3.03** Prompt-capture harness and PII-free logging
  - Goal: structlog scrubbing processor and a test harness capturing prompts sent to the client.
  - Spec: §3.1, §13
  - Tests: T-SEC-01, T-SEC-04
  - Done when: No email, phone, TRN, IBAN or customer name in captured prompts or logs.
- [x] **S3.04** Anthropic client
  - Goal: Official SDK adapter with structured outputs or strict tool use plus Pydantic validation, one retry, 30 s timeout, prompt caching, model ids from env; manual `scripts/live_smoke.py` (never run in CI).
  - Spec: §3.2, §7.2, §7.3
  - Tests: T-LLM-02
  - Done when: Bad output becomes UNCLEAR with needs_review; CI never calls the network; with a key the smoke script classifies M-01.
- [x] **S3.05** Extraction verification
  - Goal: Deterministic verification of every extraction.
  - Spec: §7.4
  - Tests: T-VER-01, T-VER-02, T-VER-03, T-VER-04, T-VER-05, T-LLM-03
  - Done when: Fabricated invoices, non-verbatim evidence and customer mismatches are flagged and routed to review.
- [x] **S3.06** Classification service and inbox
  - Goal: Correspondence import, classify endpoint, accept and reject creating events, inbox page.
  - Spec: §7, §9, §10
  - Tests: T-API-05, T-LLM-01 (15-message smoke gate), T-UI-04
  - Done when: Accepting a classification creates the right event and an audit row.
- [x] **S3.07** LLM drafts with validator and template fallback
  - Goal: LLM drafting, regeneration limit, deterministic template fallback, URL and contact rule.
  - Spec: §8.2, §8.3, §18.9
  - Tests: T-DRF-03
  - Done when: After three violating drafts the template is used and passes.
- [x] **S3.08** LLM planner with rule fallback
  - Goal: Plan schema validation, step and case budgets, timeout and invalid-JSON fallback to the rule planner.
  - Spec: §18.6
  - Tests: T-AGT-01, T-AGT-06, T-AGT-07 (with the LLM planner via FakeClient)
  - Done when: Failure never stops the tick; fallback is recorded.
- [x] **S3.09** Human approval and sandbox outbox
  - Goal: Approve, reject and mark-sent; SandboxMailGateway writing only to the outbox; L2 policy-gated sends; recipient must equal the customer's stored address.
  - Spec: §18.4, §18.8, §9
  - Tests: T-API-06, T-MAIL-01, T-MAIL-02, T-UI-03, T-AUD-01
  - Done when: Nothing can leave the machine; denied sends show reason codes.
- [ ] **S3.10** Prompt-injection suite
  - Goal: Hostile inbound messages cannot add tools, recipients or ledger changes.
  - Spec: §18.9
  - Tests: T-INJ-01, T-INJ-02, T-INJ-03
  - Done when: Gate D3 demonstrable.
- [ ] **S3.11** Payment allocation and unallocated-cash screen
  - Goal: Allocation endpoint and page with the 409 rules.
  - Spec: §6.7, §9
  - Tests: T-API-09, T-API-10
  - Done when: Allocation is audited; overpayment and cross-customer attempts return 409.
- [ ] **S3.12** Receivables Health Audit (synthetic demo report)
  - Goal: Printable report with definitions and limits, labelled synthetic.
  - Spec: §11
  - Tests: T-RPT-01
  - Done when: Report contains the golden figures, the DSO label and the heuristic sentence.
- [ ] **S3.13** Record cassettes and live end-to-end run
  - Goal: Record cassettes manually on synthetic data with a real key from the environment; run the full demo with live Claude; tag `slice-3-complete`.
  - Spec: §12.1, §16
  - Tests: D1 and D3 with live Claude; replay run identical in shape
  - Done when: End-to-end demo works live; failure falls back safely; no real data anywhere.

## Checkpoint C1 — January 2027 submission pack

Confirm the exact date with the organisers. Exit: submission pack complete, demo video synthetic-only.

- [ ] **C1.01** Test report by test ID
  - Goal: `scripts/test_report.py` turning JUnit output into `docs/TEST_REPORT.md` (pass/fail per ID, no value claims).
  - Spec: §20.1
  - Tests: test report generated in CI
  - Done when: Report lists every implemented test ID.
- [ ] **C1.02** README
  - Goal: Setup, controls, synthetic-data notice, known limitations and the demo script.
  - Spec: §20.1
  - Tests: manual review
  - Done when: A new evaluator can start the demo from the README.
- [ ] **C1.03** Architecture and agent-flow diagram
  - Goal: One page in `docs/ARCHITECTURE.md` (Mermaid): ports and adapters, agent loop, policy gate.
  - Spec: §20.1
  - Tests: manual review
  - Done when: Diagram matches the code.
- [ ] **C1.04** Provenance and data-protection statement
  - Goal: Verify PROVENANCE.md and the Section 20.2 statement against the implementation.
  - Spec: §20.2
  - Tests: manual review
  - Done when: Every statement is true of the code.
- [ ] **C1.05** Demo video (manual)
  - Goal: Record the synthetic-only demo following 19.4 steps 1 to 5.
  - Spec: §19.4
  - Tests: manual
  - Done when: Video uses synthetic data only and fits the organisers' length limit.
- [ ] **C1.06** Pitch deck and summary report (manual)
  - Goal: Use the venture plan; present business claims as hypotheses.
  - Spec: §12.2, §20
  - Tests: manual
  - Done when: No unsupported uplift or adoption claims.
- [ ] **C1.07** Submission requirements re-check (manual)
  - Goal: Re-read the official page and terms: dates, limits, format, AI-coding disclosure, IP wording.
  - Spec: §17, §20
  - Tests: manual
  - Done when: Decisions logged in docs/DECISIONS.md.

## Slice 4 — Simulator + learning proof

Planning window Jan–Mar 2027 [A]. Priority P1. Exit: same seed, same results; labelled simulation; plan → tool → policy → action → outcome → playbook demonstrated.

- [ ] **S4.01** Scenario config and seeded demo-company generator
  - Goal: Seed 42; 12 customers, 60 invoices; its own `sim_<seed>.db`.
  - Spec: §19.2
  - Tests: T-SIM-01 (generated data), T-SIM-04
  - Done when: Same seed gives identical data; the real database is untouched.
- [ ] **S4.02** Customer behaviour engine
  - Goal: The four archetypes producing replies and payments on a simulated clock.
  - Spec: §19.2
  - Tests: archetype behaviour unit tests
  - Done when: Each archetype behaves as specified.
- [ ] **S4.03** Arms A0 to A3 and metrics
  - Goal: Runner for the four arms and day-60 metrics.
  - Spec: §19.2
  - Tests: T-SIM-01, T-SIM-02, T-SIM-03
  - Done when: Results are reproducible; every message passes the validator and policy.
- [ ] **S4.04** Outcome inference
  - Goal: Promise kept, broken or pending with the 2-day grace.
  - Spec: §18.7
  - Tests: T-OUT-01
  - Done when: Outcomes match the spec cases.
- [ ] **S4.05** Customer playbook
  - Goal: Promise-kept rate, reply speed and follow-up gap; planner uses them.
  - Spec: §18.7
  - Tests: T-LRN-01, T-LRN-02, T-LRN-03
  - Done when: Playbook updates are deterministic and auditable.
- [ ] **S4.06** Escalation and follow-up scheduling in the loop
  - Goal: Transitions to ESCALATED, scheduled follow-ups, tasks.
  - Spec: §18.2, §18.6
  - Tests: T-CASE-01 in simulation
  - Done when: Escalations create tasks; follow-ups use the playbook gap.
- [ ] **S4.07** Synthetic evaluation mode (L0) and capture rate
  - Goal: L0 runs record skipped steps only; capture-rate mechanics labelled SIMULATED.
  - Spec: §19.3
  - Tests: T-SHD-01, T-SHD-02
  - Done when: No side effects in L0.
- [ ] **S4.08** Simulation UI
  - Goal: Scenario controls, 14-day fast-forward, A0 to A3 comparison, assumptions panel, persistent `SIMULATION — MECHANICS, NOT EVIDENCE` label.
  - Spec: §19, §10
  - Tests: manual check of labels
  - Done when: The label is visible on every simulation view.
- [ ] **S4.09** Run-of-show and Slice 4 close-out
  - Goal: Demo run-of-show page and rehearsal script; tag `slice-4-complete`.
  - Spec: §19.4
  - Tests: D4, D5
  - Done when: A full run shows plan, tool, policy, action, outcome and updated playbook.

## Slice 5 — Hardening + final rehearsal

Planning window Mar–May 2027 [A]. Priority P2. Exit: gates C2 to C4 pass; kill switch verified; no real data in any artifact.

- [ ] **S5.01** Security review and scans
  - Goal: Spec-reviewer plus manual review; gitleaks and pip-audit clean.
  - Spec: §3.6, §13
  - Tests: T-SEC-02, T-SEC-05
  - Done when: No findings left open.
- [ ] **S5.02** Purge/reseed and tenant isolation
  - Goal: Admin re-authentication, typed confirmation, audited purge or reseed; database path never from request input.
  - Spec: §3.3, §13
  - Tests: T-SEC-06, T-SEC-08
  - Done when: Path-traversal attempt reaches nothing.
- [ ] **S5.03** HTTPS requirement and trusted proxies
  - Goal: `require_https` outside localhost; forwarded headers trusted only from configured proxies.
  - Spec: §3.3, §13
  - Tests: T-SEC-07
  - Done when: Non-HTTPS non-local requests are rejected.
- [ ] **S5.04** Route-by-route auth, CSRF and rate-limit audit
  - Goal: Re-run T-SEC-03, 09, 10, 11 across every route including new ones.
  - Spec: §3.3
  - Tests: T-SEC-03, T-SEC-09, T-SEC-10, T-SEC-11
  - Done when: No unprotected state-changing route.
- [ ] **S5.05** One-command start
  - Goal: Multi-stage non-root Dockerfile, compose file and one documented command with seeded data and the sandbox mailbox.
  - Spec: §3.1, §19.4
  - Tests: T-DEMO-02
  - Done when: A clean checkout on another machine starts with one command.
- [ ] **S5.06** Replay mode and banners
  - Goal: Disclosed replay backup with a visible REPLAY MODE banner; SYNTHETIC and SIMULATION banners verified.
  - Spec: §19.4
  - Tests: T-DEMO-01
  - Done when: Replay is deterministic and visibly labelled.
- [ ] **S5.07** Latency checks
  - Goal: Measure tick and plan latency with the live model; optional performance test.
  - Spec: §19.4, §13
  - Tests: T-DEMO-03 (T-PERF-01 optional)
  - Done when: Within the spec limits or documented.
- [ ] **S5.08** Accessibility and responsive polish
  - Goal: Empty and error states, keyboard focus, contrast, narrow screens.
  - Spec: §10
  - Tests: manual check
  - Done when: Demo flow usable without a mouse and on a laptop projector resolution.
- [ ] **S5.09** Optional Mailpit SMTP sandbox
  - Goal: Only if time allows; the sandbox must remain incapable of external sending.
  - Spec: §3.1, §18.8
  - Tests: T-MAIL-01 re-run
  - Done when: No path to a real recipient.
- [ ] **S5.10** Final rehearsals and submission refresh
  - Goal: Two clean-machine rehearsals, kill-switch test, backup machine, refreshed submission pack; tag `v0.1.0-championship`.
  - Spec: §12.4, §20.1
  - Tests: C3 checklist
  - Done when: All gates C2 to C4 met; no real data in any artifact.

## Competition gates (checked at the close-out steps; not counted in progress)

- [ ] D1 End-to-end agent on a clean checkout (multi-step tick, at least two tools, plan and trace visible)
- [ ] D2 Deterministic truth (golden totals; no LLM output can modify ledger values)
- [ ] D3 Policy and safety (reason codes; injection cannot change tools, recipient or ledger; L2 only to the sandbox outbox)
- [ ] D4 Reproducible simulator (same seed, same results; labelled simulation)
- [ ] D5 Usable demo (an evaluator can follow the story without a developer)
- [ ] D6 Submission integrity (no real data anywhere; provenance and data-protection statements match the code)

## Open items to settle with the organisers (do not block the build)

- Judging criteria and weights, submission format, pitch and video length limits, exact submission and registration dates, AI-assisted-coding disclosure, IP and prior-use wording. Log answers in `docs/DECISIONS.md`.
