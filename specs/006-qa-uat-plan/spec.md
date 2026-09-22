# Spec 006: QA & User Acceptance Testing Plan

- **Status:** approved
- **Author:** Anuj Panchal (drafted by agent)
- **Created:** 2026-09-22
- **Approved:** 2026-09-22
- **Implemented in:** —

## 1. Summary

Define and execute a complete, layered QA and User Acceptance Testing (UAT) programme for the
multi-agent Career Assistant: offline static/unit QA, live integration tests against Atlas +
Azure OpenAI, end-to-end SSE/API tests (local and Azure-deployed), manual UAT of the Streamlit
UI, and post-deploy smoke verification. Every scenario is documented with **input, expected
output, and actual output** in a single shareable report
([`qa-uat-report.md`](qa-uat-report.md)) suitable for review by colleagues and management.
This spec makes **no behaviour changes** to the application; it is a testing-only work item.

## 2. Context & current state

The repo already has layered pytest infrastructure (`pytest.ini`, `tests/unit/`,
`tests/integration/`, `tests/e2e/`, `smoke/post_deploy_smoke.py`) — see README §Testing.
What is missing:

- No single **prerequisite/credential checklist** (Azure login, Foundry endpoint, Atlas URI,
  MCP URL/key, optional Voyage key, App Insights) that states exactly what is needed to run
  each layer.
- No **formal scenario catalogue** covering all four agents, routing/classifier modes,
  embedding providers, memory behaviours, fallback paths, and negative/edge cases.
- No **manual UAT script** for the Streamlit UI (chat, Sessions/traces, Metrics, cost sidebar).
- No **industry-standard QA/UAT report** with input / expected / actual outputs, pass/fail
  status, evidence, defect log, and sign-off.

The full scenario catalogue and report template live in
[`qa-uat-report.md`](qa-uat-report.md) in this folder. It is filled in with actual results
during execution (after this spec is approved).

## 3. Requirements

- **R1** — The plan must include an explicit **environment & credentials checklist** covering,
  per test layer, exactly which of the following are required: Azure CLI login (`az login`)
  with subscription + tenant access, Foundry project endpoint (`AZURE_AI_PROJECT_ENDPOINT`),
  chat deployment (`gpt-5.6-sol`), Azure OpenAI endpoint + key (embeddings), MongoDB Atlas
  connection string + user + Vector-Search-capable tier, Docker (local MCP) or the deployed
  MCP URL + `MCP_API_KEY` (cloud), optional `VOYAGE_API_KEY`, optional
  `APPLICATIONINSIGHTS_CONNECTION_STRING`.
- **R2** — **Offline QA** must run with zero credentials: `pytest -m unit`, `ruff check .`,
  `mypy src`. All must pass before any live layer begins.
- **R3** — **Live integration tests** must run against real Atlas + Azure OpenAI via
  `RUN_INTEGRATION=1 pytest -m integration`, with seeded data (`python -m db_seeding.seed_all`)
  verified as a precondition.
- **R4** — **API/e2e tests** must validate the SSE contract (`POST /chat` event sequence
  ending in `done`) and all REST endpoints (`/health`, `/agents`, `/sessions`,
  `/sessions/{id}/traces`, `/metrics`) against a running API — locally and, if deployed, on
  the Azure Container Apps stack.
- **R5** — **Functional agent scenarios** must cover: orchestrator small talk and capability
  questions (direct reply, no specialist), each specialist individually (job-match,
  career-coach, company-intel), multi-specialist fan-out with synthesis, short-term memory
  continuity across turns, long-term fact persistence across sessions, the MongoDB-only
  grounding contract (no general-knowledge fallback), `SPECIALIST_MODE` variants
  (`registered-or-inline` fallback, `registered` strict, `inline`), and classifier backends
  (`heuristic`, `llm`, `auto`).
- **R6** — **Negative/edge scenarios** must cover: empty/malformed input, Atlas unreachable,
  MCP unreachable, wrong MCP API key, invalid Azure OpenAI key, unknown session ID, and
  prompt-injection attempts via user message.
- **R7** — **Manual UAT** must exercise the Streamlit UI end to end: chat page with live
  agent trace, Usage & Cost sidebar (6 KPIs), Sessions page with per-turn traces (all five
  tabs), and the Metrics dashboard.
- **R8** — If the Azure stack is deployed during the test window, `deploy/deploy.sh`'s
  post-deploy smoke (`smoke/post_deploy_smoke.py`) must pass, and `/health` must report the
  expected effective config.
- **R9** — All results must be recorded in `specs/006-qa-uat-plan/qa-uat-report.md` in the
  industry-standard format: document control, scope, environment, per-test
  **input / expected output / actual output / status / evidence**, defect log, exit
  criteria, and sign-off. No secrets may appear in the report (endpoints/keys redacted).
- **R10** — Testing must be **non-destructive**: no `--drop` reseeding, no `destroy.sh`, and
  MCP writes stay disabled (`MONGODB_ALLOW_WRITE=false`) unless explicitly approved.

## 4. Non-goals

- Load, performance, soak, or chaos testing (beyond recording observed latency).
- Security penetration testing — the known-vulnerability register (README §Security) already
  documents accepted demo risks; we only verify documented mitigations behave as described.
- CI/CD pipeline integration of these tests.
- Any application behaviour change, new feature, or bug fix. Defects found are logged in the
  report and (if a fix is wanted) get their own spec per the SDD workflow.

## 5. Contracts affected

None — no module, endpoint, env var, collection, or UI page is modified. The tests *exercise*:

| Contract | Exercised by |
|---|---|
| `POST /chat` (SSE), `GET /health|/agents|/sessions|/sessions/{id}/traces|/metrics` | API/e2e scenarios |
| `config/agents/*.agent.md`, `config/skills/*.skill.md` (agent behaviour) | Functional agent scenarios |
| `chat_messages`, `agent_memory_facts`, `agent_traces` (Atlas) | Memory + trace scenarios |
| Env vars: `SPECIALIST_MODE`, `CLASSIFIER_BACKEND`, `EMBEDDING_PROVIDER`, `MONGODB_MCP_URL`, `MCP_API_KEY`, `SHORT_TERM_MEMORY_BACKEND` | Mode/fallback scenarios |
| Streamlit pages `ui/streamlit_app.py`, `ui/pages/2_Sessions.py`, `ui/pages/3_Metrics.py` | Manual UAT |
| `smoke/post_deploy_smoke.py`, `deploy/deploy.sh` outputs (`.env.live`) | Post-deploy smoke |

## 6. Open questions

| # | Question | Owner | Resolution |
|---|----------|-------|-----------|
| Q1 | Which environment(s) are in scope for the executed report: local-only, or local + the deployed Azure Container Apps stack? | Anuj | **Both** — local + deployed Azure Container Apps stack (DEP-xx in scope) |
| Q2 | Which embedding provider configuration(s) to certify: `foundry` only, or also `voyage` / `atlas_auto` (each switch requires reseed + recalibration)? | Anuj | **All three** — `foundry`, `voyage`, and `atlas_auto` |
| Q3 | Is re-seeding Atlas (`db_seeding.seed_all`) permitted, or must testing run against current data as-is? | Anuj | **Tester's judgement** — choose the optimal path per scenario (reseed permitted where a provider switch requires it) |
| Q4 | Who are the named UAT sign-off approvers (manager/colleagues) for the report's approval table? | Anuj | **Anuj Panchal** named in the sign-off list; other reviewers TBD |
| Q5 | May the tester restart local services / toggle env vars (`SPECIALIST_MODE`, `CLASSIFIER_BACKEND`) between scenarios on the dev machine? | Anuj | **Yes** — restart/toggle permitted between scenarios |

## 7. Acceptance criteria

- **AC1** (R1): The report's Environment section contains a completed checklist mapping every
  required credential/login to the test layers that need it, with each item verified
  (e.g. `az account show`, Atlas ping, `/health` fields) before execution.
- **AC2** (R2): `pytest -m unit`, `ruff check .`, and `mypy src` results are recorded with
  pass/fail and command output evidence — run with no network credentials.
- **AC3** (R3, R4): Integration and e2e suites are executed with results recorded, including
  the full SSE event sequence for at least one chat turn per specialist.
- **AC4** (R5): Every functional scenario in the report catalogue (E2E-xx) has input,
  expected output, actual output, and pass/fail — including the two-specialist synthesis
  turn and both memory behaviours.
- **AC5** (R6): Every negative/edge scenario (NEG-xx) shows graceful degradation (error
  surfaced, no crash/hang, no general-knowledge answer) with actual output recorded.
- **AC6** (R7): All UAT scenarios (UAT-xx) are executed in the Streamlit UI with evidence
  (screenshot or captured output) and status.
- **AC7** (R8, if deployed): Post-deploy smoke passes against the Container Apps endpoints in
  `.env.live`, recorded in the report.
- **AC8** (R9): The report is complete in the industry-standard format with summary counts,
  defect log (or explicit "none"), exit-criteria evaluation, and sign-off block; zero secrets
  present (checked before sharing).
- **AC9** (R10): Atlas data and Azure resources are unchanged after the run, except for
  chat/memory/trace documents written by normal test traffic.

## 8. Test plan

| Layer | Where | Credentials needed | Gate |
|---|---|---|---|
| Static + unit | `ruff check .`, `mypy src`, `pytest -m unit` | none | none — always run |
| Integration | `RUN_INTEGRATION=1 pytest -m integration` | Atlas URI, Azure OpenAI key/endpoint, `az login` (Foundry memory test) | `RUN_INTEGRATION=1` |
| e2e (SSE) | `RUN_E2E=1 API_URL=… pytest -m e2e` | all of the above + running API + MCP (Docker local or cloud URL+key) | `RUN_E2E=1` |
| Manual functional + UAT | Streamlit UI + API, per `qa-uat-report.md` scenario tables | same as e2e | human executor |
| Post-deploy smoke | `python smoke/post_deploy_smoke.py` | deployed stack, `.env.live` | Azure deployment exists |

Execution order: offline layer first (blocks everything on failure) → integration → local
e2e + functional → manual UAT → post-deploy smoke. Results are written into
`qa-uat-report.md` in place (its per-test tables have empty "Actual output / Status" cells
until execution).
