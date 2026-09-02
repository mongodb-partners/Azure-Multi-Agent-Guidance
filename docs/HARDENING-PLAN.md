# Production Hardening Plan

Implementation plan for four workstreams that bring the project toward the AWS
Bedrock AgentCore Guidance's enterprise ring, mapped onto **this** stack
(FastAPI + Foundry + Container Apps + Bicep + MongoDB Atlas).

## Status (implemented in this pass)

| Workstream | Status | Key artifacts |
|---|---|---|
| Secret rotation + Key Vault | ✅ Done | `deploy/keyvault.bicep`, `main.bicep` KV refs + UAMI, `deploy.sh` secret push, `docs/secrets.md` |
| Destroy script | ✅ Done | `deploy/destroy.sh`, README "Cleanup" |
| Testing | ✅ Done | `pytest.ini`, `requirements-dev.txt`, `tests/{unit,integration,e2e}/`, `tests/conftest.py`, `smoke/post_deploy_smoke.py` (45 unit tests passing) |
| Observability | ✅ Done | `src/telemetry.py`, wiring in `app.py`/`orchestrator.py`, `deploy/monitoring.bicep` (App Insights + workbook + 7 alerts) |

> ⚠️ **Action still required by you:** rotate the live Azure OpenAI key + Atlas
> password currently in your local `.env` (they are not in git, but they are
> live). Steps in `docs/secrets.md`.

Remaining from the original gap analysis (not in this pass): Entra ID JWT auth,
private networking to Atlas, rate limiting, CI/CD.

**MCP-runtime ownership — done:** the MongoDB MCP server is now **self-hosted** on Azure Container
Apps (`deploy/mcp.bicep`), stack-owned and read-only, with a static API-key header. This replaced the
external managed remote MCP. It also introduces new security items (external MCP ingress, shared API
key, DB connection string in the MCP container) — tracked in the **Security** section of the README
(S1–S14).


1. [Observability — App Insights + OpenTelemetry + dashboards + alarms](#1-observability)
2. [Testing — unit + integration + e2e + smoke](#2-testing)
3. [Secret rotation + Key Vault](#3-secret-rotation--key-vault)
4. [Destroy script for the entire stack](#4-destroy-script)

Each workstream lists: goal, AWS-guidance parallel, concrete file changes in
this repo, and acceptance criteria. Estimates assume one engineer.

---

## 1. Observability

**Goal:** Emit distributed traces, metrics, and structured logs from the API,
orchestrator, classifier, retrieval, and MCP calls into **Application Insights /
Azure Monitor**, with dashboards and metric alerts.

**AWS parallel:** OTel/ADOT → X-Ray + CloudWatch, fleet dashboards, 7+ alarms,
`observability-runbook.md`.

**Current state:**
- `src/app.py:25` uses `logging.basicConfig` only.
- `src/hosted_agent.py:32` sets `OTEL_SDK_DISABLED=true` (must become conditional).
- `deploy/main.bicep:64` provisions Log Analytics but nothing exports traces/metrics.
- The orchestrator already emits rich `_event(...)` dicts (`src/orchestrator.py:37`)
  — these are the natural source for spans.

### Estimate: ~5–7 days

### Tasks

**1.1 Dependencies & settings (0.5d)**
- Add to `requirements.txt`: `azure-monitor-opentelemetry`,
  `opentelemetry-instrumentation-fastapi`, `opentelemetry-instrumentation-httpx`,
  `opentelemetry-instrumentation-pymongo`.
- Add to `src/settings.py`:
  - `applicationinsights_connection_string` (env `APPLICATIONINSIGHTS_CONNECTION_STRING`)
  - `otel_enabled: bool` (env `OTEL_ENABLED`, default `true`)
  - `otel_service_name` (default `career-assistant-api`)

**1.2 Telemetry bootstrap module — new `src/telemetry.py` (1.5d)**
- `configure_telemetry(app)` that:
  - calls `configure_azure_monitor(connection_string=...)` when the connection
    string is present; otherwise no-ops (keeps local/offline runs clean).
  - instruments FastAPI, httpx, pymongo.
  - returns a module-level `tracer` and `meter`.
- Define custom metrics:
  - `chat.turns` (counter), `chat.latency_ms` (histogram),
    `chat.errors` (counter), `specialist.invocations{agent}` (counter),
    `tool.calls{tool}` (counter), `llm.tokens{kind}` (counter, from `src/llm.py`).
- Call `configure_telemetry(app)` in `src/app.py` right after `app = FastAPI(...)`
  (`src/app.py:28`).

**1.3 Span the orchestrator pipeline (1.5d)**
- Wrap `run_turn` in a root span `chat.turn` with attributes
  `session_id`, `user_id`, `orchestrator_mode`.
- Child spans: `classify`, `retrieve` (per preset), `specialist.<name>`,
  `synthesize`.
- Bridge existing `_event(...)` types to span events / attributes:
  - `routing` → span attribute `specialists`, `backend`.
  - `tool_call` → span event + `tool.calls` metric increment.
  - `error` → `span.record_exception` + `chat.errors` increment.
- Emit `chat.latency_ms` at the `final` event.
- Do this without changing the SSE event contract (UI stays unchanged).

**1.4 Instrument LLM + retrieval token/latency (0.5d)**
- In `src/llm.py`, record `llm.tokens` from response usage and a
  `llm.call` span with model/deployment attributes.
- In `src/retrieval.py`, span `hybrid_search` with `hits`, `preset`, `latency`.

**1.5 Re-enable OTel in hosted agent (0.25d)**
- Change `src/hosted_agent.py:32` to only disable OTel when
  `APPLICATIONINSIGHTS_CONNECTION_STRING` is absent (keep the IMDS-noise guard for
  off-Azure local runs).

**1.6 Bicep: App Insights + wiring (1d)**
- Add an `Microsoft.Insights/components` (App Insights) resource in
  `deploy/main.bicep`, `WorkspaceResourceId` = the existing Log Analytics
  workspace (`logAnalytics.id`, main.bicep:64).
- Inject `APPLICATIONINSIGHTS_CONNECTION_STRING` into the API container app env
  (main.bicep:157) and the embedding Function app settings (main.bicep:120).
- Output the connection string / App Insights name.

**1.7 Dashboards + alerts as IaC (1d)**
- New `deploy/monitoring.bicep` (module referenced from `main.bicep`) provisioning:
  - An **Azure Monitor Workbook** (`Microsoft.Insights/workbooks`) with tiles:
    request rate, p50/p95 latency, error rate, tokens/turn, specialist mix,
    MCP tool-call volume, MongoDB op latency.
  - **Metric alerts** (`Microsoft.Insights/metricAlerts` /
    `scheduledQueryRules`) mirroring the AWS 7-alarm set:
    1. API 5xx rate > threshold
    2. p95 chat latency > threshold
    3. Chat error-event rate > threshold
    4. Specialist failure rate > threshold
    5. LLM token spend/hour > budget
    6. Embedding Function failures
    7. Container App replica restarts / unhealthy
  - An **Action Group** (`Microsoft.Insights/actionGroups`) for email/webhook.

**1.8 Runbook doc (0.25d)**
- `docs/observability-runbook.md`: dashboard links, alert meanings, triage steps
  (port of the AWS runbook).

### Acceptance criteria
- A `/chat` request produces an end-to-end trace in App Insights with
  classify → specialist → synthesize spans and MCP/Mongo child spans.
- Workbook renders live metrics; all 7 alerts exist and can be manually triggered
  in a test.
- Local runs with no connection string still work (telemetry no-ops silently).

---

## 2. Testing

**Goal:** Layered test suite — unit (fast, offline), integration (real backends,
env-gated), e2e (against running API), and post-deploy smoke.

**AWS parallel:** `tests/unit`, `tests/integration`, `e2e/` (Playwright),
`e2e-smoke/` (Python post-deploy), CI running all layers.

**Current state:** one offline unit test (`tests/test_classifier.py`). No
integration, e2e, or smoke; `pytest` not pinned.

### Estimate: ~4–6 days

### Tasks

**2.1 Test tooling & layout (0.5d)**
- Add `requirements-dev.txt`: `pytest`, `pytest-asyncio`, `pytest-cov`,
  `httpx`, `respx` (HTTP mocking), `mongomock` (or a Mongo test container),
  `ruff`, `mypy`.
- Add `pyproject.toml`/`pytest.ini` markers: `unit`, `integration`, `e2e`, `smoke`.
  Default run = `unit` only; others opt-in via `-m`.
- Layout:
  ```
  tests/
    unit/          # offline, no creds (classifier, augment, rrf/mmr, event mapping)
    integration/   # env-gated: real Mongo + Azure OpenAI
    e2e/           # against a running API (SSE)
    conftest.py    # fixtures + skip guards for missing creds
  ```
- Move existing `tests/test_classifier.py` → `tests/unit/`.

**2.2 Unit tests — offline, no creds (1.5d)**
- `retrieval`: `_rrf_fuse`, `_apply_recency`, `_mmr` with synthetic vectors.
- `orchestrator`: `_augment`, `_synthesize` (mock `chat_text`),
  `_fan_out_specialists` event ordering (mock registry / fake specialist),
  `run_turn` routing branches (mock `classify`, `get_registry`, `memory`).
- `memory`: `history_as_text`, `facts_as_text`, cap logic (`mongomock`).
- `mcp_client`: `mcp_tool_dict` shape + write-gate toggling `MONGODB_ALLOW_WRITE`.
- `settings`: `aoai_base` normalizer + `require()` error paths.
- `llm`: reasoning-model retry / `_unsupported_param` fallback (mock the client).

**2.3 Integration tests — env-gated (1.5d)**
- Skip unless `RUN_INTEGRATION=1` and required env present (`conftest.py` guard).
- `embeddings`: real embed call returns correct dimensionality.
- `retrieval`: seed a temp collection + index, assert hybrid ranking.
- `memory`: round-trip append/read/fact-extract against a real Atlas test DB.
- `foundry_agents`: one live specialist turn returns a non-empty message
  (marked slow).

**2.4 E2E tests — against running API (1d)**
- `httpx`-based SSE client hitting `/health`, `/agents`, `/sessions`, `/chat`.
- Assert event sequence: `routing` → `specialist_start` → `final` → `done`.
- Provide `RUN_E2E=1` + `API_URL` gate. (Playwright optional for the Streamlit UI;
  `httpx` is sufficient and matches the Python stack — recommend `httpx`.)

**2.5 Post-deploy smoke — new `smoke/` (0.5d)**
- `smoke/post_deploy_smoke.py`: reads `.env.live`, hits deployed `/health`
  and one `/chat` turn, asserts a synthesized answer; non-zero exit on failure.
- `smoke/memory_recall_diagnostic.py`: two-turn recall check (mirrors AWS).
- Hook into `deploy/deploy.sh` at the end (after `.env.live` is written).

**2.6 Coverage & docs (0.5d)**
- `pytest --cov=src` gate (start ~60% on unit, ratchet up).
- `docs/testing.md`: how to run each layer + required env.

### Acceptance criteria
- `pytest -m unit` runs offline with zero credentials and passes in CI.
- `RUN_INTEGRATION=1 pytest -m integration` passes against a test Atlas + Azure.
- `RUN_E2E=1 API_URL=... pytest -m e2e` validates the SSE contract.
- `deploy.sh` runs the smoke test and fails loudly on a broken deploy.

---

## 3. Secret rotation + Key Vault

**Goal:** Remove plaintext secrets from source/env; store them in **Azure Key
Vault**; have Container Apps + Function read them via **managed identity**;
document/automate rotation.

**AWS parallel:** Secrets Manager / SSM + IAM-scoped access.

**Current state (security issue):**
- `.env` reportedly contains **live** Azure OpenAI keys + Atlas connection string
  — committed to git.
- `deploy/main.bicep` passes `azureOpenAIKey` / `mongoUri` as `@secure()` params
  into container-app `secrets` (main.bicep:145-149) — better than env, but still
  sourced from local `.env` and not centrally rotatable.

### Estimate: ~1–2 days (P0 — do the rotation part immediately)

### Tasks

**3.1 Immediate remediation (0.5d) — do first**
- **Rotate** the exposed Azure OpenAI key and Atlas DB credentials now.
- Purge from git history (`git filter-repo` or BFG), force-push, invalidate old.
- Ensure `.env`, `.env.live` are in `.gitignore`; keep only `.env.sample`.

**3.2 Key Vault in Bicep (0.5d)**
- New `deploy/keyvault.bicep` module: `Microsoft.KeyVault/vaults` with RBAC
  authorization enabled.
- Secrets: `azure-openai-api-key`, `mongodb-uri` (and later Entra client secret).
- Grant the API Container App's **system-assigned managed identity** the
  `Key Vault Secrets User` role (`roleAssignments`).
- Enable `identity: { type: 'SystemAssigned' }` on the API container app
  (main.bicep:137) and the Function app (main.bicep:111).

**3.3 Reference secrets from Key Vault, not plaintext (0.5d)**
- Change container-app secrets to Key Vault references:
  ```bicep
  secrets: [
    { name: 'aoai-key', keyVaultUrl: '${kv.properties.vaultUri}secrets/azure-openai-api-key', identity: 'system' }
    { name: 'mongo-uri', keyVaultUrl: '${kv.properties.vaultUri}secrets/mongodb-uri', identity: 'system' }
  ]
  ```
- Function app: use `@Microsoft.KeyVault(SecretUri=...)` app-setting references.
- `deploy.sh`: instead of passing raw key/URI as Bicep params, **push** them into
  Key Vault (`az keyvault secret set`) and pass only the vault name.

**3.4 Rotation runbook + optional automation (0.25d)**
- `docs/secrets.md`: how to rotate each secret (`az keyvault secret set` + restart
  revision), and Atlas credential rotation steps.
- Optional: Key Vault event-grid → revision restart, or scheduled reminder.

### Acceptance criteria
- No secret values in git, `.env` untracked, only `.env.sample` present.
- Deployed API/Function read secrets from Key Vault via managed identity (no keys
  in container env or Bicep params).
- Rotating a secret in Key Vault + restarting the revision picks up the new value.

---

## 4. Destroy script

**Goal:** One command to tear down everything `deploy.sh` created, with a safe
confirmation and mode to also remove the resource group.

**AWS parallel:** `deploy/destroy/*.sh` + `scripts/destroy.sh --mode ...`.

**Current state:** No teardown; only `deploy/deploy.sh` exists.

### Estimate: ~0.5–1 day

### Tasks

**4.1 `deploy/destroy.sh` (0.5d)**
- Usage: `./destroy.sh <resource-group> [--yes] [--keep-rg]`.
- Behavior:
  - Load `.env` / `.env.live` for names (ACR, prefix).
  - Confirm interactively unless `--yes` (echo what will be deleted).
  - Default: `az group delete -n "$RG" --yes --no-wait` (removes Container Apps
    env, API/UI apps, ACR, Function, storage, Log Analytics, App Insights,
    Key Vault*, monitoring resources in one shot).
  - `--keep-rg`: delete only the resources this stack created (Container Apps,
    ACR, Function, storage, App Insights, workbook/alerts), leaving the RG and any
    pre-existing resources intact.
- **Explicitly NOT deleted** (documented in the script header + README): the
  **Foundry project/model deployments** and **MongoDB Atlas** data — these are
  prerequisites, not stack-owned. (The self-hosted **MongoDB MCP** Container App IS
  stack-owned now and is torn down with the rest.)
  - Offer an opt-in `--purge-foundry-agents` that calls the existing
    provisioning teardown (delete registered Foundry agents via
    `scripts/provision_agents.py` / lock file) and `--drop-atlas` (drops the
    seeded collections only, behind an extra confirmation).

**4.2 Key Vault soft-delete note (0.25d)**
- Key Vault has soft-delete/purge protection; document `az keyvault purge` for a
  full clean, or `--purge-keyvault` flag.

**4.3 README + docs (0.25d)**
- Add a "Cleanup" section to `README.md` (mirrors the AWS repo's Cleanup section)
  and cross-link `docs/`.

### Acceptance criteria
- `./deploy/destroy.sh <rg> --yes` removes all stack-created Azure resources.
- Reused MCP server, Foundry project, and Atlas data are preserved by default.
- Script is idempotent (safe to re-run; no error if already gone).

---

## Suggested sequencing

| Order | Workstream | Why first |
|---|---|---|
| 1 | **3.1 Secret rotation** | Active exposure — remediate immediately (hours). |
| 2 | **4. Destroy script** | Cheap; makes iterating on infra safe/repeatable. |
| 3 | **2. Testing (unit)** | Fast feedback before touching orchestrator/telemetry. |
| 4 | **3.2–3.4 Key Vault** | Infra secret plumbing before adding more infra. |
| 5 | **1. Observability** | Larger; benefits from tests + KV already in place. |
| 6 | **2. Testing (integration/e2e/smoke)** | Layer on once infra + telemetry stabilize. |

**Total estimate: ~11–16 engineer-days.**

## Notes on fit with existing structure
- All infra changes stay in **Bicep** (`deploy/`), invoked by the existing
  `deploy.sh` flow — no new IaC tool introduced.
- Telemetry uses **Azure Monitor OpenTelemetry Distro**, the Azure-native OTel
  path, so no ADOT/collector sidecar is needed (Container Apps + App Insights
  handle it).
- Testing stays **pytest + httpx** (pure Python), matching the codebase; Playwright
  is optional and only for the Streamlit UI if desired.
- The SSE event contract and `.agent.md`/`.skill.md` config model are untouched,
  so the UI and agent-authoring workflow are unaffected.
