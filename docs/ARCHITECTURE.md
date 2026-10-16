# Architecture

One page for evaluators and contributors: how the code is layered, how one agent tick runs, and where the policy gate and the LLM sit. Synthetic data only. `tests/test_architecture_doc.py` checks this page against the code: the dependency table must equal the real imports, and every module and name below must exist.

## 1. Ports and adapters

The pure core holds every financial rule; the agent holds the decision loop and declares the ports it needs; adapters implement them with SQLite, the Anthropic API (always behind redaction), a sandbox mail outbox and the clock. The web app and the CLI are the entry points.

```mermaid
flowchart TB
  browser([Browser]) --> api
  cliEntry([CLI: receivables]) --> cli
  subgraph entry [Entry points]
    api[api: FastAPI routes, session, CSRF]
    web[web: Jinja templates, labels]
    cli[cli and seed]
  end
  subgraph agentLayer [agent: decision loop]
    orchestrator[orchestrator.run_tick]
    ports{{Ports: PlanSource, DraftWriter, AgentStore}}
    policyEngine[policy.evaluate_send]
    registry[tools.TOOL_REGISTRY: 11 tools]
  end
  subgraph coreLayer [core: pure functions, no I/O]
    core[ledger, aging, scoring, queue, KPIs, drafts, validation, verify, redact, injection]
  end
  subgraph adapters [adapters]
    db[db: DbAgentStore, importer, approvals, allocations]
    llm[llm: LlmPlanner, draft writer, RedactingClient]
    mail[mail: SandboxMailGateway]
    clock[clock, config, log]
  end
  sqlite[(SQLite)]
  anthropic[(Anthropic API)]
  outbox[(Sandbox outbox table)]
  api --> web
  api --> db
  api --> llm
  api --> orchestrator
  cli --> db
  orchestrator --> ports
  orchestrator --> policyEngine
  orchestrator --> registry
  agentLayer --> core
  db -. implements AgentStore .-> ports
  llm -. implements PlanSource and DraftWriter .-> ports
  db --> sqlite
  llm -- redacted text only --> anthropic
  mail --> outbox
  db --> mail
  adapters --> core
```

**Dependency rules** (between packages under `src/receivables/`; the test compares this table with the real imports):

| Package | Imports from | Enforced by |
|---|---|---|
| `core` | (nothing) | T-ARCH-01 (`tests/architecture/test_core_purity.py`): no framework, I/O, network, database, clock or outer layer |
| `agent` | `core` | T-ARCH-01: no wall clock in `agent/`; time is injected |
| `adapters.config` | `core` | |
| `adapters.log` | `core` | |
| `adapters.llm` | `adapters.config`, `agent`, `core`, `settings` | `tests/architecture/test_llm_fence.py`: only `adapters/llm/anthropic_client.py` may import the Anthropic SDK or an HTTP client |
| `adapters.mail` | `adapters.clock`, `adapters.db` | T-MAIL-01: imports no network client |
| `adapters.db` | `adapters.clock`, `adapters.config`, `adapters.llm`, `adapters.mail`, `agent`, `core` | |
| `seed` | `adapters.clock`, `adapters.db` | |
| `web` | `core` | |
| `cli` | `adapters.clock`, `adapters.config`, `adapters.db`, `api`, `core`, `seed`, `settings` | |
| `api` | `adapters.clock`, `adapters.config`, `adapters.db`, `adapters.llm`, `adapters.log`, `agent`, `core`, `settings`, `web` | |

## 2. The agent loop (one tick)

`adapters/db/agent_store.py` `run_agent_tick` opens one write transaction and calls `agent/orchestrator.py` `run_tick` with the store, the clock's time and the planner and drafter of the LLM mode. Each case runs in its own savepoint, so a failing case is rolled back alone and counted.

```mermaid
flowchart TD
  start([Run agent: form, API or script]) --> queue
  queue[Top 20 overdue cases by priority value] --> kill{Kill switch on?}
  kill -- yes, planner not called --> rules
  kill -- no --> observe
  observe[Observe: get_case and get_customer_profile, message headers only] --> plan
  plan[Plan: LlmPlanner in live or replay mode] --> validate{validate_plan: known tools, strict arguments, at most 5 steps}
  validate -- invalid, timeout or error --> rules[Rule planner: plan_case]
  validate -- valid --> steps
  rules --> steps
  steps[For each step of the plan] --> stepKill{Kill switch on?}
  stepKill -- yes --> denied[Denied: POLICY_KILL_SWITCH]
  stepKill -- no --> shadow{Level L0?}
  shadow -- yes --> skipped[Skipped: recorded, not executed]
  shadow -- no --> isSend{send_email?}
  isSend -- no --> tool[Run the tool: read, draft, task, document request, follow-up, escalate]
  isSend -- yes --> gate[Policy gate: evaluate_send with every reason code]
  gate --> allowed{L2 and allowed?}
  allowed -- no --> approval[Approval item with reason codes]
  allowed -- yes --> sandbox[SandboxMailGateway: stored address only]
  denied --> record
  skipped --> record
  tool --> record
  approval --> record
  sandbox --> record
  record[Case state machine, step trace and audit row] --> next([Next step or case])
```

With the kill switch on, the planner is not called and every step is denied with `POLICY_KILL_SWITCH`; at L0 every step is recorded as skipped. A step that raises is recorded as failed and rolled back alone. Drafts come from `agent/drafting.py` `generate_draft`: the LLM draft writer is tried and checked by `core/draft_validator.py` `check_draft` against the fact block; after the allowed regenerations, the fixed template is used. People act outside the tick through the API and pages: approve or reject drafts and approval items (`adapters/db/approvals.py` `decide_approval`), mark drafts sent, accept classifications, allocate payments (`adapters/db/allocations.py` `allocate_payment`); each writes an audit row.

## 3. The policy gate and the LLM path

Inbound email is untrusted data: it reaches the classifier only, as quoted text after redaction, and its result is verified by code before a person can accept it. Plans and drafts are validated before anything happens, and every side effect passes the policy engine.

```mermaid
flowchart LR
  email[Inbound email, untrusted] --> flag[core injection_signals: flag for review]
  email -- classify --> client
  observation[Case observation: ledger facts, message headers only] -- plan --> client
  facts[Draft fact block from the ledger] -- draft --> client
  client[RedactingClient: core redact and pseudonymize first] --> mode{LLM mode}
  mode -- live --> anth[AnthropicClient]
  mode -- replay --> rep[ReplayClient: recorded answers]
  mode -- fake --> fake[FakeClient: no model; the agent gets no planner or drafter]
  fake --> fallback[Fallbacks: unclear and needs review, rule plan, fixed template]
  anth --> answer[Answer, schema-validated]
  rep --> answer
  answer -- invalid, timeout or error --> fallback
  answer -- classification --> verify[core verify_extraction: values must appear in the message and the ledger]
  verify --> review[Inbox: a person accepts or rejects]
  answer -- plan --> planCheck[validate_plan against TOOL_REGISTRY]
  answer -- draft --> draftCheck[check_draft against the fact block]
  planCheck --> policy[evaluate_send: allowlist, amount, tone, dispute, unallocated cash, recent contact, hours, daily cap, validation, recipient, kill switch]
  draftCheck --> policy
  policy --> effect[Approval item or sandbox outbox, audited]
```

Every failure has a safe fallback: an invalid or failed classification becomes "unclear, needs review"; a rejected or failed plan becomes the rule planner's; a draft that keeps failing validation becomes the fixed template. No model output can change an invoice, a payment or a recipient: no tool writes the ledger, no argument carries an amount or an address, and the gateway sends only to the customer's stored address.

## Where things live

| Module | Names |
|---|---|
| `src/receivables/agent/orchestrator.py` | `run_tick`, `PlanSource`, `AgentStore`, `recheck_draft` |
| `src/receivables/agent/drafting.py` | `DraftWriter`, `generate_draft` |
| `src/receivables/agent/plans.py` | `validate_plan` |
| `src/receivables/agent/policy.py` | `evaluate_send`, `send_facts_from` |
| `src/receivables/agent/tools.py` | `TOOL_REGISTRY` |
| `src/receivables/agent/rules_planner.py` | `plan_case` |
| `src/receivables/agent/cases.py` | `transition` |
| `src/receivables/core/draft_validator.py` | `check_draft` |
| `src/receivables/core/verify.py` | `verify_extraction` |
| `src/receivables/core/redact.py` | `redact`, `pseudonymize` |
| `src/receivables/core/injection.py` | `injection_signals` |
| `src/receivables/adapters/db/agent_store.py` | `DbAgentStore`, `run_agent_tick` |
| `src/receivables/adapters/db/approvals.py` | `decide_approval` |
| `src/receivables/adapters/db/allocations.py` | `allocate_payment` |
| `src/receivables/adapters/db/controls.py` | `kill_switch_on` |
| `src/receivables/adapters/db/classifications.py` | `classify_stored` |
| `src/receivables/adapters/llm/planner.py` | `LlmPlanner` |
| `src/receivables/adapters/llm/drafter.py` | `make_draft_writer` |
| `src/receivables/adapters/llm/redacting.py` | `RedactingClient` |
| `src/receivables/adapters/llm/anthropic_client.py` | `AnthropicClient` |
| `src/receivables/adapters/llm/replay.py` | `ReplayClient` |
| `src/receivables/adapters/llm/fake.py` | `FakeClient` |
| `src/receivables/adapters/mail/sandbox.py` | `SandboxMailGateway` |
| `src/receivables/adapters/clock.py` | `Clock`, `SimulatedClock` |
| `src/receivables/api/main.py` | `create_app` |

Behaviour and acceptance tests: `docs/SPEC.md`; decisions: `docs/DECISIONS.md`; the test report by spec test ID: `docs/TEST_REPORT.md`.
