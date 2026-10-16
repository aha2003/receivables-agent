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

## 2026-10-13 — [S1.13] Dashboard and queue (first screen)
- PR: #79 | Branch: s1-13-dashboard-queue | Commit: see PR head
- Tests: `uv run pytest -q` → 650 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 100%
- Spec / test IDs covered: §6, §9, §10, §4.6, §5 / T-API-01, T-API-02, T-API-03, T-IMP-04 and T-IMP-05 over HTTP; T-UI-02 and T-UI-05 checked manually in the browser
- Deviations or decisions (link docs/DECISIONS.md): D-017 (config loader, JSON format, import refusals, invoice page, SVG bars, vendored htmx; DSO window spec conflict awaiting confirmation)
- Next step: S1.14 Slice 1 close-out

## 2026-10-13 — [S1.14] Slice 1 close-out
- PR: see branch | Branch: s1-14-slice-1-closeout | Commit: see PR head
- Tests: `uv run pytest -q` → 660 passed; ruff check and format clean; pyright 0 errors
- Spec / test IDs covered: §16 Slice 1 exit condition (golden numbers match, seeded demo starts via CLI reset + create-user, UI says synthetic; CI green on main) / full suite; D2 partially (financial totals, aging and priority match the golden fixtures; draft facts and LLM immutability wait for Slice 3); docs/DEMO.md walkthrough run by hand
- Deviations or decisions (link docs/DECISIONS.md): none; D-017 DSO window conflict still awaiting confirmation
- Next step: S2.01 Agent tables migration (tag `slice-1-complete` on the merge commit after merge)

## 2026-10-13 — [S2.01] Agent tables migration
- PR: see branch | Branch: s2-01-agent-tables-migration | Commit: see PR head
- Tests: `uv run pytest -q` → 810 passed; ruff check and format clean; pyright 0 errors; coverage core 100%; adapters/config.py and db/models.py 100%
- Spec / test IDs covered: §18.10 (DDL and config keys), §18.8 (sandbox-only gateway setting) / T-DB-01 extended (seven agent tables STRICT; every CHECK, NOT NULL and foreign key; defaults; 0001↔0002 round-trip with rows present)
- Deviations or decisions (link docs/DECISIONS.md): D-018 (date CHECKs added to §18.10 DDL; agent/mail config keys with strict loader; enabled SMTP refused until S5.09)
- Next step: S2.02 Case state machine

## 2026-10-14 — [S2.02] Case state machine
- PR: see branch | Branch: s2-02-case-state-machine | Commit: see PR head
- Tests: `uv run pytest -q` → 899 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 99% (one unreachable match exit)
- Spec / test IDs covered: §18.2 (FR-31) / T-CASE-01; every table row, guard boundaries, Hypothesis properties (transitions only from the table, deterministic, RESOLVED final)
- Deviations or decisions (link docs/DECISIONS.md): D-019 (spec conflict resolved: §18.2 row for the `escalate` tool; unmatched triggers unchanged with applied=False; persistence in S2.07)
- Next step: S2.03 Closed tool registry

## 2026-10-14 — [S2.03] Closed tool registry
- PR: see branch | Branch: s2-03-closed-tool-registry | Commit: see PR head
- Tests: `uv run pytest -q` → 972 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 99%
- Spec / test IDs covered: §18.3 (FR-30) / T-AGT-03 (names equal the 11 spec tools; no tool writes invoices, payments, customers or events; no amount/recipient/SQL/path argument); strict argument validation; read tools on golden data (as_of, flags, priority, unallocated cash, playbook defaults, message headers) with no customer PII
- Deviations or decisions (link docs/DECISIONS.md): D-020 (send_email draft_id or draft_ref, §18.3 sentence; headers only until S3.02; Dubai dating of messages; non-read handlers in later steps)
- Next step: S2.04 Policy engine

## 2026-10-14 — [S2.04] Policy engine
- PR: see branch | Branch: s2-04-policy-engine | Commit: see PR head
- Tests: `uv run pytest -q` → 1020 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 99%
- Spec / test IDs covered: §18.4, §18.5 (FR-32) / T-POL-01 to T-POL-11 (T-POL-10 with a supplied violation until S2.05; kill switch from config, toggle in S2.08); rule boundaries; Hypothesis property: reasons are exactly the failing rules, sorted (no short-circuit)
- Deviations or decisions (link docs/DECISIONS.md): D-021 (SendFacts/SendDraft/L2Limits; local calendar dates; exact recipient; fail closed on draft invoices outside the facts)
- Next step: S2.05 Template drafts and draft validator (pure)

## 2026-10-14 — [S2.05] Template drafts and draft validator (pure)
- PR: see branch | Branch: s2-05-template-drafts-validator | Commit: see PR head
- Tests: `uv run pytest -q` → 1156 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 99%
- Spec / test IDs covered: §8.1, §8.2, §8.4 (FR-09, FR-14 in part, NFR-01) / T-DRF-01, T-DRF-02, T-DRF-04; T-POL-10 through the real validator; template text checked against the spec; every template passes the validator (Hypothesis over the golden queue, tones and labels)
- Deviations or decisions (link docs/DECISIONS.md): D-022 (drafts.sender_name config key; contact_name constant; BODY_TOO_LONG; normalised superset detection; overdue-only fact block)
- Next step: S2.06 Rule planner

## 2026-10-14 — [S2.06] Rule planner
- PR: see branch | Branch: s2-06-rule-planner | Commit: see PR head
- Tests: `uv run pytest -q` → 1194 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 99% (rules_planner 100%)
- Spec / test IDs covered: §18.6 rule planner and plan schema / T-AGT-08 (tool sequences for all 9 golden queue items); every rule row and label, precedence, age boundaries, playbook gap; property: agrees with the §6.5 next action and tone
- Deviations or decisions (link docs/DECISIONS.md): D-023 (task owners and due dates, labelled drafts, measured playbook gap, plan consistency checks)
- Next step: S2.07 Orchestrator tick and trace

## 2026-10-14 — [S2.07] Orchestrator tick and trace
- PR: see branch | Branch: s2-07-orchestrator-tick-trace | Commit: see PR head
- Tests: `uv run pytest -q` → 1262 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 99% (orchestrator 100%, plans 100%), adapters 100%
- Spec / test IDs covered: §18.6 (with §18.2–§18.5, §18.8) / T-AGT-01 (unknown tool rejected, rule planner used), T-AGT-02 (denied L2 send converted to approval, outbox empty), T-AGT-04 (run and steps traced, audit rows, no PII in logs or planner observation; latency deferred to S3.08), T-AGT-05 (identical steps on fresh databases), T-AGT-07 (top 20 cases, 6-step plan rejected); L0/L1/L2 behaviour, sandbox outbox, case transitions, per-case and per-step rollback
- Deviations or decisions (link docs/DECISIONS.md): D-024 (latency deferred; minimal sandbox outbox; rejected-plan audit; events applied once; no-reply escalation deferred to S4.04/S4.06)
- Next step: S2.08 Policy audit and kill switch

## 2026-10-14 — [S2.08] Policy audit and kill switch
- PR: see branch | Branch: s2-08-policy-audit-kill-switch | Commit: see PR head
- Tests: `uv run pytest -q` → 1285 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 99%, adapters 100%
- Spec / test IDs covered: §18.5, §3.3 / T-POL-09 end to end (config switch, admin switch, on/off, beats L0; nothing executes or changes, no planner call), T-AUD-01 for kill-switch changes and every agent step (actor, entity, before and after JSON); admin rule (role, re-entry within 5 minutes, refusals audited); reset keeps an engaged switch
- Deviations or decisions (link docs/DECISIONS.md): D-025 (runtime switch in the append-only audit log; HTTP routes in S2.09; known limit: takes effect from the next tick)
- Next step: S2.09 Agent API

## 2026-10-14 — [S2.09] Agent API
- PR: see branch | Branch: s2-09-agent-api | Commit: see PR head
- Tests: `uv run pytest -q` → 1341 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 99%, api 99% (new modules 100%)
- Spec / test IDs covered: §18.11, §9, §3.3 / T-API-04 (INV-004 dispute event, PO-B-004, empty payments, audit rows); run (summary, dry run, autonomy cap, now/as_of rules), trace, approvals, kill switch (admin, server-side re-entry within 5 minutes, refusals), re-auth (rate limit, audit, cancellation, replay), session and CSRF on every route
- Deviations or decisions (link docs/DECISIONS.md): D-026 (now/as_of only for dry runs or below L2; autonomy only lowered; audit[] scope; server-side re-auth; approve/reject in S3.09, playbook in S4.05)
- Next step: S2.10 Case detail and trace UI

## 2026-10-14 — [S2.10] Case detail and trace UI
- PR: see branch | Branch: s2-10-case-detail-trace-ui | Commit: see PR head
- Tests: `uv run pytest -q` → 1385 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 99%, api and web 100% for the new code
- Spec / test IDs covered: §10, §18 / T-UI-04 (UNMATCHED_INVOICE, LEDGER_STILL_OPEN and UNALLOCATED_CASH_ON_ACCOUNT in plain language: automated with synthetic classification rows and checked by hand in the browser); case page trace (plan, tool, arguments, reason, policy decision in plain words, status, result), L0/L1 draft notice and L2 decision, approvals and run pages, run form (CSRF, dry run), kill switch banner, escaping
- Deviations or decisions (link docs/DECISIONS.md): D-027 (case page layout; approvals and run pages and run form beyond §10; flag wording and shape; ledger amounts at as_of)
- Next step: S2.11 Slice 2 close-out

## 2026-10-14 — [S2.11] Slice 2 close-out
- PR: see branch | Branch: s2-11-slice-2-closeout | Commit: see PR head
- Tests: `uv run pytest -q` → 1394 passed; ruff check and format clean; pyright 0 errors; coverage core 100%, agent 99%
- Spec / test IDs covered: §12.1 gate D1 without an LLM, §16 Slice 2 exit (`tests/test_slice2_exit.py`: from an empty database via the real CLI and the web form, the agent inspects the queue, plans with the rule planner, uses 5 tools, is denied by policy with reason codes, every case reaches a waiting state, the plan, trace and audit are visible, the kill switch stops the next tick); docs/DEMO.md Slice 2 walkthrough asserted and run by hand
- Deviations or decisions (link docs/DECISIONS.md): D-028 (form `as_of` under D-026; meaning of waiting state; no follow-up gate yet)
- Next step: S3.01 LLM port with Fake and Replay clients (tag `slice-2-complete` on the merge commit after merge)

## 2026-10-14 — [S3.01] LLM port with Fake and Replay clients
- PR: see branch | Branch: s3-01-llm-port | Commit: see PR head
- Tests: `uv run pytest -q` → 1539 passed, 1 skipped (the swallowed-attempt probe, run only in a subprocess); ruff check and format clean; pyright 0 errors; coverage 99% overall, `adapters/llm/` and `settings.py` 100%
- Spec / test IDs covered: §3.5, §7.2 / T-LLM-04 (a cassette replays byte-identically, an unrecorded request raises `CassetteMiss` without echoing content, no network is touched); `LLMClient` port and strict classify, plan and draft models; checked cassette format; FakeClient; `LLM_MODE` (default `fake`) with `LLM_REPLAY` synonym and `llm.replay` cross-check; key and model ids never shown; network blocked for the whole suite; only `adapters/llm/anthropic_client.py` may import `anthropic`/`httpx`
- Deviations or decisions (link docs/DECISIONS.md): D-029 (single mode switch; enforcement that tests never call the live API; `live` refused until S3.04; review fixes). Run command now `uv run --env-file .env uvicorn ...`
- Next step: S3.02 Redaction and pseudonymisation (pure)

## 2026-10-14 — [S3.02] Redaction and pseudonymisation (pure)
- PR: see branch | Branch: s3-02-redaction | Commit: see PR head
- Tests: `uv run pytest -q` → 1630 passed, 1 skipped (the network-guard probe); ruff check and format clean; pyright 0 errors; `core/redact.py` 100% coverage; 29 mutations of the patterns, order and name matching all killed
- Spec / test IDs covered: §7.5 / T-RED-01 (output matches the spec string exactly); TRN, IBAN (incl. printed groups, option a), email and UAE phone patterns with Arabic-Indic digits; §7.5 order; case-insensitive customer-name pseudonymisation, longest first; config switches; invoice and PO references, amounts and dates survive; Hypothesis properties (planted values never survive, idempotent, no name survives)
- Deviations or decisions (link docs/DECISIONS.md): D-030 (exact patterns, IBAN option a, accepted over-redaction of space-separated leading-0 numbers, known limits; review fixes)
- Next step: S3.03 Prompt-capture harness and PII-free logging (wires redaction into LLM calls; T-SEC-01)

## 2026-10-14 — [S3.03] Prompt-capture harness and PII-free logging
- PR: see branch | Branch: s3-03-prompt-capture-logging | Commit: see PR head
- Tests: `uv run pytest -q` → 1666 passed, 1 skipped (the network-guard probe); ruff check and format clean; pyright 0 errors; every mutation of the redacting client, the scrubber, the formatter, the handler set-up and the request ids killed (one equivalent: SQLAlchemy already defaults to WARNING)
- Spec / test IDs covered: §3.1, §13 NFR-04, NFR-07 / T-SEC-01 (M-01 with an email, phone, TRN, IBAN and customer name injected: none reaches the client for classify, plan or draft; switches; names read at each call; Hypothesis on nested payloads), T-SEC-04 (M-01 through the client, an agent tick and web requests with the server's logging: no body or personal data in the logs; `llm.call` with model, latency, tokens and prompt hash only; request ids); structlog JSON pipeline with scrubber, fail-closed paths and server entry point
- Deviations or decisions (link docs/DECISIONS.md): D-031 (option a: draft facts pseudonymised, S3.07 restores the name; log pipeline and its limits; review fixes). New dependency: structlog 26.1.0 (PROVENANCE.md)
- Next step: S3.04 Anthropic client

## 2026-10-15 — [S3.04] Anthropic client
- PR: see branch | Branch: s3-04-anthropic-client | Commit: see PR head
- Tests: `uv run pytest -q` → 1706 passed, 1 skipped (the network-guard probe); ruff check and format clean; pyright 0 errors; pip-audit: no known vulnerabilities; every mutation of the client, fallback, factory, prompt, schema, smoke script and review fixes killed (two equivalent lines removed)
- Spec / test IDs covered: §3.2, §7.1-§7.3 / T-LLM-02 (bad JSON 30 times through the real client over a mock transport and through FakeClient: UNCLEAR, confidence 0, needs_review after exactly one retry; no exception escapes; timeouts and API errors without a schema retry); §7.3 prompt verbatim with §7.1 definitions (checked against SPEC.md), cached; structured-output schema matching the Pydantic model; config timeout and retries; model ids from env, never logged; live mode in the factory; manual `scripts/live_smoke.py` (refusals tested, never run by CI or tests)
- Deviations or decisions (link docs/DECISIONS.md): D-032 (no temperature in this SDK; explicit key and base URL; fallback scope; model role logged instead of id; live plan and draft wait for S3.08 and S3.07; review fixes). New dependency: anthropic 1.14.0 with httpx2, httpcore2, jiter, docstring-parser, sniffio, truststore (PROVENANCE.md)
- Unverified: live behaviour (schema acceptance, caching) until the maintainer runs the smoke script
- Next step: S3.05 Extraction verification

## 2026-10-15 — [S3.05] Extraction verification
- PR: see branch | Branch: s3-05-extraction-verification | Commit: see PR head
- Tests: `uv run pytest -q` → 1794 passed, 1 skipped (the network-guard probe); ruff check and format clean; pyright 0 errors; `core/verify.py` 100% lines; every mutation of the normalisation, number parsing, PO role, matching, evidence, ledger, customer and review rules killed
- Spec / test IDs covered: §7.4 / T-VER-01 (difference 3,000, no flags), T-VER-02 (exactly one `UNMATCHED_INVOICE`), T-VER-03 (`LEDGER_STILL_OPEN` 20,000.00; none for INV-001), T-VER-04 (`E_EVIDENCE_NOT_VERBATIM`, review), T-VER-05 (`UNVERIFIED_EXTRACTION`, `CUSTOMER_INVOICE_MISMATCH`, review), T-LLM-03 (invented INV-777 through FakeClient and `classify_message`: `UNMATCHED_INVOICE`, review); display numbers and ambiguity; Arabic text and digits; Hypothesis properties
- Deviations or decisions (link docs/DECISIONS.md): D-033 (option 1a: evidence against original or redacted text; option 2a: ambiguous display number as `UNMATCHED_INVOICE` with `ambiguous`; message as the supporting quote; fail-safe PO role rule; added review condition for a PO mismatch without its difference; review fixes)
- Next step: S3.06 Classification service and inbox

## 2026-10-15 — [S3.06] Classification service and inbox
- PR: see branch | Branch: s3-06-classification-inbox | Commit: see PR head
- Tests: `uv run pytest -q` → 1927 passed, 2 skipped (the network-guard probe, and the T-LLM-01 gate test until its run is recorded); ruff check and format clean; pyright 0 errors; every mutation of the accept rules, state checks, date resolution, import protection and evaluation killed (one equivalent check removed); every spec-review regression test fails on the earlier code
- Spec / test IDs covered: §7, §9, §10, §4.5 / T-API-05 (M-01 classified with a mocked LLM and accepted: `DISPUTE_OPENED` with `PO_AMOUNT_MISMATCH`, audit row, status `accepted`), T-UI-04 (classification flags in plain language on the inbox and the invoice page: automated, and checked by hand in the browser against a throwaway copy of the golden data), T-LLM-01 (evaluation maths and recording round trip tested; the gate test replays `tests/cassettes/t_llm_01.json` once the maintainer records it); T-SEC-04 again on the full classify path; correspondence import and `POST /api/correspondence`; §7.1 accept rules; §7.2 year resolution
- Deviations or decisions (link docs/DECISIONS.md): D-034 (options 1a, 2a, 3a; locked text of classified messages; event invoice rule; review fixes). Tests that asserted correspondence import was "not implemented" now use a still-unknown kind
- Unverified: T-LLM-01 accuracy until the maintainer runs `LLM_MODE=live uv run --env-file .env python scripts/eval_classifier.py --record`
- Next step: S3.07 LLM drafts with validator and template fallback

## 2026-10-15 — Fix after S3.06: amounts as written (D-035)
- PR: see branch | Branch: fix-llm-amounts-as-written | Commit: see PR head
- Why: the maintainer's live T-LLM-01 run (accuracy 0.933, gate passed) missed only M-01, because the model wrote "47,000" as in the message and the schema accepted only "47000"
- Tests: `uv run pytest -q` → 1974 passed, 1 skipped (the network-guard probe); ruff check and format clean; pyright 0 errors; the recording now replays at 15/15; every mutation of the parser and the new log line killed; every spec-review regression test fails on the earlier code
- Spec / test IDs covered: §7.2, §7.3, §7.4, NFR-07 / T-LLM-01 (the gate test now runs in CI on the committed recording), T-VER-01..05 unchanged
- Deviations or decisions (link docs/DECISIONS.md): D-035
- Next step: S3.07 LLM drafts with validator and template fallback

## 2026-10-16 — [S3.07] LLM drafts with validator and template fallback
- PR: see branch | Branch: s3-07-llm-drafts | Commit: see PR head
- Tests: `uv run pytest -q` → 2058 passed, 1 skipped (the network-guard probe); ruff check and format clean; pyright 0 errors; every mutation of the drafting loop, the contact rule, the drafter adapter and the wiring killed; every spec-review regression test fails on the earlier code
- Spec / test IDs covered: §8.2, §8.3, §8.4, §18.9 / T-DRF-03 (three violating drafts: the template is used, `generator = template`, no violations), T-DRF-01 and T-DRF-02 unchanged; §8.3 prompt verbatim (checked against SPEC.md); the drafter never sees a customer name and the saved draft has it restored; dry runs, L0 and the kill switch make no model calls
- Deviations or decisions (link docs/DECISIONS.md): D-036 (options 1a, 2a, 3a; `UNKNOWN_CONTACT_OR_URL` and `UNFILLED_PLACEHOLDER`; review fixes and limits)
- Unverified: live drafting quality until a live agent run (`LLM_MODE=live`); the model calls hold the tick's database transaction (a known limit)
- Next step: S3.08 LLM planner with rule fallback

## 2026-10-16 — [S3.08] LLM planner with rule fallback
- PR: see branch | Branch: s3-08-llm-planner | Commit: see PR head
- Tests: `uv run pytest -q` → 2083 passed, 2 skipped (the network-guard probe, and the committed-recording replay test until plans and drafts are recorded); ruff check and format clean; pyright 0 errors; every mutation of the planner, its audit row, the mode wiring, the live prompt table, the schema generator and the recording script killed; every spec-review regression test fails on the earlier code
- Spec / test IDs covered: §18.6 / T-AGT-01 (an LLM plan with `wire_transfer` is rejected, the rule planner runs), T-AGT-06 (a timeout, then invalid JSON twice: rule planner, `fallback = true`, the whole queue processed), T-AGT-07 (the top 20 by priority value; a 6-step LLM plan rejected), all with the LLM planner via FakeClient; T-AGT-02, 04, 05 unchanged; per-call `agent.plan_received` (model role, prompt version, attempts, latency); prompt and schema generated from the tool registry; combined recording `scripts/record_demo.py` → `tests/cassettes/live_demo.json`, replay round trip tested offline
- Deviations or decisions (link docs/DECISIONS.md): D-037 (planner prompt written, no spec text; options 1a and 2a; plan timeout without transport retries; dry runs never call a model; combined recording; review fixes)
- Unverified: live plans and drafts, and whether the API accepts the generated plan schema, until the maintainer runs `LLM_MODE=live uv run --env-file .env python scripts/record_demo.py --record`
- Next step: S3.09 Human approval and sandbox outbox

## 2026-10-16 — S3.08 follow-up: live combined recording
- PR: see branch | Branch: s3-08-live-recording | Commit: see PR head
- Why: D-037 left live plans and drafts unverified until the maintainer recorded them
- Recorded by the maintainer: `LLM_MODE=live uv run --env-file .env python scripts/record_demo.py --record` → T-LLM-01 accuracy 1.000 (macro-F1 1.000, gate 0.80); the agent run planned 9 cases with the LLM (9 plans used, no fallback, so the API accepts the generated plan schema) and wrote 8 LLM drafts, none falling back to the template
- Cassette: `tests/cassettes/live_demo.json` (32 interactions: 15 classify, 9 plan, 8 draft; model roles only, no keys or headers; contact details redacted before sending)
- Tests: `uv run pytest -q` → 2083 passed, 2 skipped (the network-guard probe, and the "awaits plans and drafts" test, now superseded); `test_the_committed_recording_serves_the_demo_run` now runs and passes offline
- Next step: S3.10 Prompt-injection suite (after S3.09 #103)

## 2026-10-16 — [S3.09] Human approval and sandbox outbox
- PR: see branch | Branch: s3-09-approval-outbox | Commit: see PR head
- Tests: `uv run pytest -q` → 2147 passed, 2 skipped (the network-guard probe, and the "awaits plans and drafts" test, now superseded); ruff check and format clean; pyright 0 errors; every mutation of the gateway, the drafts state machine, approval decisions, the override checks, the case move, recent contact, the form routes and the template guards killed (48 in total)
- Spec / test IDs covered: §9, §18.2 (marked sent or an L2 override: `ASSESSED` → `AWAITING_REPLY`), §18.4, §18.5 (a manual send is recent contact), §18.8, §18.11 / T-API-06, T-MAIL-01 (sandbox row; the gateway imports no network client), T-MAIL-02 (a wrong recipient is a `rejected` row), T-UI-03 (also checked by hand in the browser: notice shown, no send button; that check found the Mark-sent default time bug, now fixed and tested), T-AUD-01
- Deviations or decisions (link docs/DECISIONS.md): D-038 (options 1a, 2a, 3a; the drafts API; approval decisions use the run's level; the L2 override checks the policy again now; refused overrides audited; review fixes)
- Unverified: live LLM drafts through `POST /api/drafts` until a live or recorded run; allocation approval arrives with S3.11
- Next step: S3.10 Prompt-injection suite

## 2026-10-16 — [S3.10] Prompt-injection suite
- PR: see branch | Branch: s3-10-prompt-injection-suite | Commit: see PR head
- Tests: `uv run pytest -q` → 2225 passed, 2 skipped (the network-guard probe, and the "awaits plans and drafts" test, superseded by the live recording); ruff check and format clean; pyright 0 errors; every mutation of the detector, its wiring, the audit row, the plain-language text and the plan/argument guards killed (38 in total); a timed test keeps the detector linear on crafted 20,000-character messages
- Spec / test IDs covered: §18.9, §12 gate D3 / T-INJ-01 (with a classifier, planner and drafter that obey the attacker: flagged for review whatever the model answers, plans with `to` or unknown tools rejected and the rule planner used, drafts with the address fall back to the template, the attacker in no tool call, draft or outbox row, ledger tables unchanged; English and Arabic; the live client keeps the message out of the system prompt), T-INJ-02 (no such tool, accepting "already paid" records no payment, ledger unchanged), T-INJ-03 (`https://evil.example` is `UNKNOWN_CONTACT_OR_URL`); gate D3 at L2 in `tests/test_gate_d3.py` (reason codes and audited denials, no permission, recipient or ledger change, sandbox only to the stored address); no hostile text, other customer or key in any plan or draft request
- Deviations or decisions (link docs/DECISIONS.md): D-039 (options 1a, 2a, 3a; `INSTRUCTION_INJECTION_SUSPECTED` as a routing aid, not a defence; review fixes, including the regex stall)
- Unverified: the DEMO.md injection walkthrough is covered by automated page tests, not run by hand; live model behaviour on hostile messages (not recorded, by design: option 2a)
- Next step: S3.11 Payment allocation and unallocated-cash screen

## 2026-10-16 — [S3.11] Payment allocation and unallocated-cash screen
- PR: see branch | Branch: s3-11-payment-allocation | Commit: see PR head
- Tests: `uv run pytest -q` → 2261 passed, 2 skipped (the network-guard probe, and the superseded "awaits plans and drafts" test); ruff check and format clean; pyright 0 errors; every mutation of the allocation rules, the import guard, the approval path and the page killed (31 in total; four branches with no observable effect were removed)
- Spec / test IDs covered: §6.7, §9, §10, §18.2, §18.3 / T-API-09 (P-005 to INV-008: open amount 0.00, unallocated total 0.00, audit row; another customer's invoice 409), T-API-10 (larger than the open amount: 409, payments table unchanged, still unallocated, refusal audit row); already allocated, not yet received, one fils over, an invoice dated after today, later-dated allocations counted, 404s, session and CSRF; a fully paid case resolves at once; the agent's allocation proposals confirmed by the same rules; the Unallocated cash page (checked by hand in the browser on a throwaway copy of the synthetic data: allocation, dashboard total, refused cross-customer request)
- Deviations or decisions (link docs/DECISIONS.md): D-040 (options 1a, 2a, 3a; refusal reasons; kept allocations validated as written; URL-safe payment ids; review fixes; the as_of limit of allocations)
- Unverified: none beyond the stated limit (an allocation applies from the payment's own date in earlier as_of views)
- Next step: S3.12 Receivables Health Audit (synthetic demo report)

## 2026-10-16 — [S3.12] Receivables Health Audit (synthetic demo report)
- PR: see branch | Branch: s3-12-health-audit-report | Commit: see PR head
- Tests: `uv run pytest -q` → 2280 passed, 2 skipped (the network-guard probe, and the superseded "awaits plans and drafts" test); ruff check and format clean; pyright 0 errors; every mutation of the report's figures, filters, observations and template guards killed (24 in total; two filters with no effect removed)
- Spec / test IDs covered: §11 (all seven sections in order, templated observations), §6.2/§6.4/§6.6/§6.7 formulas stated from the configured values, §9 `GET /api/report`, §10 as_of / T-RPT-01 (AED 193,000.00, AED 147,000.00, CUST-B 69.4%, "estimated DSO (90-day sales proxy)", "Heuristic score. Not a probability.", "Definitions and limits"); figures equal the dashboard's; cumulative shares; top-20 queue with flags; message statuses as of the report date; N/A for an empty ledger; escaping; session; checked in the browser at phone and desktop widths (found: shares at two decimals, a headline overflowing narrow screens; both fixed)
- Deviations or decisions (link docs/DECISIONS.md): D-041 (options 1a, 2a, 3a; review fixes)
- Unverified: the browser's print to PDF was not exercised (print styles are in place)
- Next step: S3.13 Record cassettes and live end-to-end run

## 2026-10-16 — [S3.13] Record cassettes and live end-to-end run
- PR: see branch | Branch: s3-13-live-end-to-end | Commit: see PR head
- Tests: `uv run pytest -q` → 2308 passed, 2 skipped (the network-guard probe, and the superseded "awaits plans and drafts" test); ruff check and format clean; pyright 0 errors; every check in the script is shown failing when its property is broken (tamper tests), and all 15 mutations of the check functions killed
- Spec / test IDs covered: §12.1 D1 and D3, §16 Slice 3 exit. `scripts/live_e2e.py` (manual, live, never CI): D1 live, replay of the committed recording (D1 and the same shape), D3 live at L2 with golden and hostile messages, and a safe-fallback pass with model ids that do not exist. Offline: the committed recording passes D1 and has the expected shape; an honest run, an attacker-obeying run and a model that never answers each give the expected verdicts
- Recording: `tests/cassettes/live_demo.json` (recorded by the maintainer on 2026-10-16) unchanged; prompts have not changed since
- Deviations or decisions (link docs/DECISIONS.md): D-042 (options 1a, 2a, 3a; review fixes)
- Live run by the maintainer (2026-10-16): D1 live 6/6 (9 cases, live planner and drafter, no fallback); replay passes D1 and matches the live shape 4/4; D3 live: 4/4 hostile messages flagged, only registry tools, attacker's address nowhere, ledger as seeded, one sandbox delivery to the stored address; safe fallback: every call refused, rule planner and templates, D3 and D1 held. One check failed: "policy denied something" in pass 3 (the live planner attempted no disallowed send); corrected to require a denial across passes 3 and 4 (pass 4 had one), which that output satisfies (D-042). Observed: the classifier's answers for two hostile messages (X-02, X-03) failed schema validation (`summary_en`) and fell back safely to UNCLEAR; the deterministic flag was set regardless
- After merge: the maintainer may re-run the script for a clean exit 0, then tags the merge commit `slice-3-complete`
- Next step: C1.01 Test report by test ID
