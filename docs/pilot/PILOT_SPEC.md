# Pilot Specification v0.1 (draft, not active)

**Status.** Draft for the maintainer's review. The championship build follows `docs/SPEC.md` and `PLAN.md` only. Nothing in this file applies to a build step until `CLAUDE.md` points to it. This file extends `docs/SPEC.md`; it never changes it. If the two conflict, stop and log it in `docs/DECISIONS.md`.

**Purpose.** State what a first paying pilot (milestone M1: one real client, a measured outcome) needs beyond the championship prototype, so that it can be built and tested on synthetic data while the championship work continues.

## 1. Boundaries (non-negotiable)

1. **This repository stays synthetic.** Every rule in `CLAUDE.md` holds unchanged. Pilot features are built and tested only on synthetic data, including synthetic exports made to look like real accounting-system layouts. No real company data, name, export, mapping file or correspondence is ever committed, pasted into a Claude Code session, or used in a test, fixture, cassette, screenshot or demo.
2. **Real data lives only in a separate deployment.** A pilot client's data is loaded only into that client's own deployment, after gate G-DATA (Section 5). Client-specific configuration and mapping files live in a separate private repository that Claude Code never reads.
3. **Assist mode only.** A pilot runs at L1 (SPEC §18.4): the agent prioritises, classifies and drafts; a person approves and sends from their own email, then marks the draft sent. No L2, no real sending from the application, no payment initiation, no ledger writes by the agent.
4. **The LLM rules of SPEC §1.4 and §7 to §8 apply unchanged.** The LLM never calculates financial facts or changes records; all output is validated and verified by code.
5. **Honest measurement.** Pilot results compare a cohort with its own prior period (SPEC §15.5). They are labelled as such and never presented as causal uplift.

## 2. Relationship to the championship build

The pilot reuses the existing core, agent, policy engine, approvals, inbox and reports. It activates work that `docs/SPEC.md` deliberately deferred (§1.3, §1.5 P3, §4.7) and adds a few operational features. It depends on three Slice 5 steps of `PLAN.md`: S5.02 (purge and tenant isolation), S5.03 (HTTPS and trusted proxies) and S5.05 (one-command start).

Decision record (2026-10-21, maintainer): keep the pilot in this repository in `docs/pilot/`, rather than amending `docs/SPEC.md` or creating a second code repository, so that the championship plan, step count and CI are untouched and the code is not duplicated.

## 3. Pilot shape (planning assumptions, to confirm with the client)

| Item | Assumption |
|---|---|
| Client | One UAE B2B company selling on credit, 50 to 500 staff |
| Duration | 8 to 12 weeks, after a baseline period of at least the prior 90 days |
| Cohort | Customers or invoices agreed with the client before the start, fixed for the pilot |
| Users | The client's collections team (1 to 5 people) |
| Mode | L1 assist (Section 1, item 3) |
| Data refresh | A daily or weekly export from the client's accounting system |

## 4. Requirements

Priority: **M** must, **S** should, **C** could.

| ID | Requirement | Pri | Test(s) |
|---|---|---|---|
| PIL-01 | Mapping layer per SPEC §4.7, with `?dry_run=true`, for the first export layout chosen from discovery interviews | M | T-IMP-06, T-MAP-01, T-MAP-02, T-MAP-03 |
| PIL-02 | Synthetic export fixtures that copy only the column layout, date and number formats of the accounting systems interviewees name; all values fictional | M | T-PIL-01 |
| PIL-03 | Baseline snapshot: at pilot start, freeze the cohort and record the §15.5 metrics for the baseline period; stored, audited and never recomputed | M | T-PIL-02 |
| PIL-04 | Outcome report: the same metrics for the same cohort at a later `as_of`, side by side with the baseline, labelled "Pilot measurement: cohort against its own prior period. Not a causal estimate." | M | T-PIL-03 |
| PIL-05 | Action timing: record when each queue item is first actioned (approval decision, draft marked sent, task closed), so that "share of queue items actioned within 2 working days" can be computed | M | T-PIL-04 |
| PIL-06 | Scheduled daily tick at a configured local time, at L1, honouring the kill switch and the follow-up gate, plus a "today" page listing the pending approvals and tasks | S | T-PIL-05 |
| PIL-07 | Single-reply intake: a person pastes one inbound reply into the inbox for a chosen customer; it takes the same untrusted-input path as imported correspondence (SPEC §7, §18.9) | S | T-PIL-06, T-INJ-01 to T-INJ-03 re-run |
| PIL-08 | No-LLM mode: a configuration that runs the whole pilot with the rule planner and templates only and no model calls, shown in the UI, for clients who do not agree to external model processing | M | T-PIL-07 |
| PIL-09 | Pilot deployment profile: one deployment per client, HTTPS required, encrypted storage, daily backups with a tested restore, admin and collector roles | M | T-PIL-08, T-SEC-06, T-SEC-07, T-SEC-08 re-run |
| PIL-10 | Data-protection pack: data-processing agreement, review against the UAE PDPL (and DIFC or ADGM rules if they apply), model-provider terms, and a pilot-specific data-protection statement that matches the deployment | M | manual (gate G-DATA) |

### 4.1 Tests

| ID | Test | Expected |
|---|---|---|
| T-PIL-01 | Load each synthetic export fixture through its mapping | Imports cleanly; every value is fictional; the fixture README names the layout it copies |
| T-PIL-02 | Freeze a baseline, then import new payments and re-run | Baseline values unchanged; one audit row for the freeze; a second freeze for the same pilot is refused |
| T-PIL-03 | Outcome report on a synthetic cohort with known baseline and later figures | Golden metric values for both periods; the label of PIL-04 present verbatim |
| T-PIL-04 | Queue items actioned on day 0, 2 and 3 working days after appearing, with a weekend in between | Actioned-within-2-working-days share matches the hand calculation |
| T-PIL-05 | Scheduler with a fixed clock: kill switch on, then off | No tick while on; one L1 tick at the configured time when off; nothing sent |
| T-PIL-06 | Paste a hostile reply | Same outcome as T-INJ-01 to T-INJ-03; text stored as data only |
| T-PIL-07 | Full demo flow with no-LLM mode on and a client that raises on any call | No model call; rule planner and templates used; UI shows the mode |
| T-PIL-08 | Deployment profile smoke test on a clean machine | Starts over HTTPS only; backup written; restore reproduces the golden numbers |

## 5. Gates

| Gate | Requirement |
|---|---|
| G-SYN — built on synthetic data | PIL-01 to PIL-09 pass on synthetic data in CI; the championship suite still passes |
| G-DATA — before any real data | Signed pilot agreement and data-processing agreement; professional legal review of PIL-10; the championship terms checked for commercial or prior use of the entry; the client deployment passes T-PIL-08; the client has agreed the cohort, baseline period and metrics in writing |
| G-START — pilot start | Baseline frozen (PIL-03) in the client deployment; the client's users trained; kill switch tested |

## 6. Out of scope for the pilot

Real sending from the application; payment initiation or bank connectivity; live ERP or accounting connectors; WhatsApp; PINT AE XML or ASP integration; multi-tenant hosting; cross-client data; legal escalation; credit or financing decisions.

## 7. Open questions (answer from discovery interviews or the first client)

1. Which accounting systems and export layouts are most common? (Decides PIL-01 and PIL-02.)
2. How common are non-AED invoices? Today they are rejected (`E_CURRENCY`); the import report already counts rows per currency.
3. Do collectors need Arabic outbound drafts for the first pilot?
4. Must data stay in the UAE, and does that constrain the hosting and the model provider? (Inferred; needs professional verification.)
5. Will the client allow customer correspondence to go to an external model, or does the pilot run in no-LLM mode (PIL-08)?
6. Pricing and the pilot fee.
