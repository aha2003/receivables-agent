# Pilot plan (draft, not active)

Behaviour: `docs/pilot/PILOT_SPEC.md`. This plan is **not** part of `PLAN.md`: it is not counted by `scripts/progress.py`, and no step here starts until the maintainer adds a pointer to it in `CLAUDE.md`. The working rules of `CLAUDE.md` apply to every step: synthetic data only, one step per branch and PR, the tests listed, the spec reviewer, and the human merges.

Commit prefix: `[P1.03]`. Branch: `<step-id>-<slug>`, for example `p1-03-mapping-layer`.

## Slice P1 — Pilot features on synthetic data

Exit: gate G-SYN. Every step is built and tested on synthetic data only.

- [ ] **P1.01** No-LLM mode
  - Goal: One configuration value that disables every model call; rule planner and templates only; the mode shown in the app chrome.
  - Spec: PIL-08
  - Tests: T-PIL-07
  - Done when: The full demo flow runs with a client that raises on any call.
- [ ] **P1.02** Synthetic export fixtures
  - Goal: Fictional fixtures in the column layouts, date and number formats of the accounting systems named in discovery interviews.
  - Spec: PIL-02
  - Tests: T-PIL-01
  - Blocked on: discovery interviews naming at least one common export layout.
  - Done when: Each fixture's README names the layout it copies; every value is fictional.
- [ ] **P1.03** Mapping layer and dry run
  - Goal: SPEC §4.7 mapping files, `?dry_run=true`, mapping hash in the idempotency key, explicit reprocess.
  - Spec: PIL-01, SPEC §4.7
  - Tests: T-IMP-06, T-MAP-01, T-MAP-02, T-MAP-03, T-PIL-01
  - Done when: The P1.02 fixtures import through their mappings; ambiguous dates and bad number formats are rejected as specified.
- [ ] **P1.04** Action timing
  - Goal: Record when each queue item is first actioned.
  - Spec: PIL-05
  - Tests: T-PIL-04
  - Done when: The actioned-within-2-working-days share matches the hand calculation.
- [ ] **P1.05** Baseline snapshot
  - Goal: Freeze a pilot cohort and its baseline metrics; audited; immutable.
  - Spec: PIL-03, SPEC §15.5
  - Tests: T-PIL-02
  - Done when: Later imports never change the frozen baseline.
- [ ] **P1.06** Outcome report
  - Goal: Baseline against current period for the cohort, with the PIL-04 label; printable like the Health Audit.
  - Spec: PIL-04
  - Tests: T-PIL-03
  - Done when: Golden values match for both periods.
- [ ] **P1.07** Single-reply intake
  - Goal: Paste one inbound reply into the inbox for a chosen customer.
  - Spec: PIL-07
  - Tests: T-PIL-06, T-INJ-01 to T-INJ-03 re-run
  - Done when: A pasted reply is classified, verified and reviewed exactly like an imported one.
- [ ] **P1.08** Scheduled daily tick and today page
  - Goal: L1 tick at a configured local time; a page of today's approvals and tasks.
  - Spec: PIL-06
  - Tests: T-PIL-05
  - Done when: The kill switch stops the scheduled tick; nothing is sent.
- [ ] **P1.09** Pilot deployment profile
  - Goal: One deployment per client: HTTPS only, encrypted storage, daily backup and tested restore, admin and collector roles.
  - Spec: PIL-09
  - Tests: T-PIL-08, T-SEC-06, T-SEC-07, T-SEC-08
  - Depends on: `PLAN.md` S5.02, S5.03, S5.05.
  - Done when: A clean machine starts the profile; restore reproduces the golden numbers.

## Slice P2 — Before real data (maintainer, outside the code)

Exit: gate G-DATA, then G-START. These steps are manual. Claude Code does not take part in loading or handling real data.

- [ ] **P2.01** Data-protection pack reviewed by a professional (PIL-10)
- [ ] **P2.02** Championship terms checked for commercial or prior use of the entry
- [ ] **P2.03** Pilot agreement signed: cohort, baseline period, metrics, duration, fee
- [ ] **P2.04** Client deployment set up from the P1.09 profile, with client configuration in a separate private repository
- [ ] **P2.05** Baseline frozen in the client deployment; users trained; kill switch tested
