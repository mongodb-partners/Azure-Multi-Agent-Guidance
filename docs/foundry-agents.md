# Foundry-native agents — consumption options

The agents are defined once in `config/agents/*.agent.md`. From that single source of truth you can
consume the system three ways, all on the **new** Azure AI Foundry Agent Service (Responses-based,
*not* classic Assistants/threads):

1. **Use one agent** — each routable specialist (`job-match`, `career-coach`, `company-intel`) is
   registered as a persistent, portal-visible **prompt agent**, callable by name.
2. **Use the whole solution** — the entire orchestrator (`run_turn`: classify → fan-out →
   synthesize) runs as one **hosted agent** behind a single Responses endpoint.
3. **Four separate hosted agents** *(current deployment model)* — every agent, including the
   `orchestrator`, runs as its **own hosted agent** on Foundry-managed compute. See
   [Option 3](#option-3--four-separate-hosted-agents-managed-compute) below.

Everything below reuses the same pipeline the local app runs (`load_agents()` +
`build_agent_instructions()` + `mcp_tool_dict()`), so the registered agents cannot drift from the
code.

> ✅ **MCP auth is now a static API-key header** (self-hosted MCP; see
> **[docs/mcp-server.md](mcp-server.md)**). The header **name is not `Authorization`** (`X-MCP-Api-Key`
> by default), which the Foundry runtime **accepts** on a registered MCP tool — unlike an
> `Authorization` header, which it rejects with *"Headers that can include sensitive information are
> not allowed… Use `project_connection_id` instead."* And because the key is **long-lived** (not a
> ~1h OAuth token), the registered/hosted paths no longer break with a `424 Failed Dependency` when a
> token expires. All consumption options below work. Rotate the key by re-provisioning
> (`scripts.provision_agents sync`) after updating it in Key Vault.

> **MongoDB-only grounding (enforced everywhere).** `build_agent_instructions()` appends a strict
> grounding contract to every agent: answer only from data retrieved via the MongoDB MCP tools; if
> a query returns nothing, reply *"I couldn't find any relevant information in the MongoDB database
> for that request."* and never fall back on general/pretrained knowledge. This applies to all
> three consumption options.

---

## Prerequisites

```bash
az login                                   # DefaultAzureCredential
pip install -r requirements-foundry.txt    # Foundry stack: azure-ai-projects 2.x + agentserver
# .env filled in from .env.sample (AZURE_AI_PROJECT_ENDPOINT, MONGODB_MCP_URL, Azure OpenAI, …)
```

> The project endpoint must be a **new-Foundry project** (`…services.ai.azure.com/api/projects/…`).
> `python -m scripts.provision_agents verify` confirms this before doing anything (verified on
> `azure-multiagent-proj`).

### RBAC — required to read/register agents

Provisioning and invoking agents needs the **`Microsoft.CognitiveServices/accounts/AIServices/agents/*`**
data actions on the Foundry account that backs the project. The role that grants them is
**`Cognitive Services User`** (its dataAction is `Microsoft.CognitiveServices/*`). Note:
`Azure AI Developer` and `Azure AI Administrator` do **not** grant the agent data actions, and the
role literally named "Azure AI User" is not present in every subscription — use
`Cognitive Services User`.

Symptom if this is missing: `verify`/`list` fail (and the app silently falls back to inline) with:

```
(UserError) Identity(object id: …) does not have permissions for
Microsoft.CognitiveServices/accounts/AIServices/agents/read actions.
```

Grant it (scope is the Cognitive Services *account*, not the project sub-path). Find the account
with `az cognitiveservices account list -o table`, then:

```bash
ACCOUNT_ID="/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<account>"
az role assignment create \
  --assignee <your-object-id-or-managed-identity-object-id> \
  --role "Cognitive Services User" \
  --scope "$ACCOUNT_ID"
```

Assign the same role to the **deployed API container app's managed identity** so the cloud app can
invoke the registered agents instead of falling back to inline (see "Cloud app identity" below).

Everything Foundry-native requires `requirements-foundry.txt`; without it (or without the RBAC
above) the app still runs by falling back to `SPECIALIST_MODE=inline`.

---

## Option 1 — Register the specialists as prompt agents

```bash
python -m scripts.provision_agents verify   # resource accepts agent ops?
python -m scripts.provision_agents sync      # create/update the 3 routable specialists
python -m scripts.provision_agents list      # name + versions
```

- Idempotent: a lock file `config/foundry_agents.lock.json` records each agent's
  `{version, hash}`. A re-run skips unchanged agents; changed instructions bump a version.
  `--force` bumps everything.
- After `sync`, the three agents appear in the **Foundry portal as new prompt agents** (editable,
  not the classic read-only kind).

### Invoke one agent by reference (external code)

```python
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

client = AIProjectClient(endpoint="<AZURE_AI_PROJECT_ENDPOINT>",
                         credential=DefaultAzureCredential(), allow_preview=True)
oai = client.get_openai_client(agent_name="job-match")
resp = oai.responses.create(input="Find remote senior Python roles for a MongoDB+Azure background.")
print(resp.output_text)
```

The Foundry runtime executes the agent (including its MongoDB MCP tool calls) — the caller needs
no orchestration code.

### Point the local app at the registered agents

Set in `.env`:

```
SPECIALIST_MODE=registered-or-inline   # default: use registered agents, fall back to inline
# SPECIALIST_MODE=registered           # strict: error if a registered agent is unavailable
# SPECIALIST_MODE=inline               # always run inline via the raw Responses API
```

> With the self-hosted MCP's static API-key header, the registered path is stable (no expiring
> token), so `registered-or-inline` is the default again. `registered-or-inline` only falls back to
> inline when the registered path fails *before any event is emitted*.

The `Event` interface is identical across modes, so the API, UI, and orchestrator are unchanged.

---

## Option 2 — Deploy the whole solution as a hosted agent

```bash
# Build the image in ACR + register + poll to ACTIVE. Reuses ACR_NAME from .env.live if deploy.sh
# created one, else pass ACR_NAME=<registry>.
./deploy/provision_hosted.sh <resource-group> [image-tag]
```

What it does:
1. Grants the **Foundry project managed identity** pull rights on the ACR + enables ARM AAD auth
   on the registry (see "Image-pull identity" below) — idempotent.
2. `az acr build` of `deploy/Dockerfile.hosted` → `<acr>.azurecr.io/career-hosted:<tag>`.
3. Registers it as a `HostedAgentDefinition` (Responses protocol **version `2.0.0`**, port 8088)
   via `scripts/provision_hosted.py`, passing the runtime env (Mongo URI, Azure OpenAI, MCP URL, …).
4. Polls `get_version(...).status` until **ACTIVE** (or reports FAILED/timeout).

> **Image-pull identity (the one non-obvious gotcha).** The hosted image is pulled by the Foundry
> **project sub-resource's system-assigned managed identity** — *not* the account identity and *not*
> any per-agent runtime identity. If it lacks access the version fails provisioning with a generic
> `ImageError: Container registry authentication failed`. Two things are required and the wrapper
> now sets both automatically: (a) `az acr config authentication-as-arm update --status enabled`,
> and (b) the project MI granted **`AcrPull`** + **`Container Registry Repository Reader`** on the
> ACR. RBAC/ARM-auth changes take a few minutes to propagate, so a first deploy may need a re-run.
> The SDK's `get_version(...).error` field lags the `status` flip — the authoritative failure reason
> is the raw REST `GET {endpoint}/agents/<name>/versions/<n>?api-version=v1`.

Inside the container `SPECIALIST_MODE=inline` by default, so the hosted agent is self-contained
(it does not depend on the Option-1 prompt agents). Set `SPECIALIST_MODE=registered` in `.env` to
have it delegate to them instead.

### Invoke the whole solution

Same as Option 1, but `agent_name="career-hosted-solution"`. One call returns the fully
synthesized multi-specialist answer.

### Local smoke test (before any cloud deploy)

```bash
python -m src.hosted_agent          # serves the Responses protocol on :8088
# in another shell:
curl -N -X POST localhost:8088/responses -H 'Content-Type: application/json' \
  -d '{"input":"How do I grow from backend engineer to staff engineer?","stream":true}'
```

The endpoint streams the standard Responses SSE lifecycle (`response.created` → `output_text.delta`
→ `response.completed`); the final answer is the assistant `message` item. Set
`HOSTED_STREAM_TRACE=true` to also stream the live routing/tool-call trace. (A container build via
`docker build -f deploy/Dockerfile.hosted .` works too, but `provision_hosted.sh` builds in ACR and
needs no local Docker daemon.)

---

## Option 3 — Four separate hosted agents (managed compute)

Every agent — `orchestrator`, `job-match`, `career-coach`, `company-intel` — runs as its **own
Foundry hosted agent** (one managed-compute container each). No prompt agents are used. The
`orchestrator` container classifies the request and invokes the specialist **hosted** agents *by
reference* (cross-agent invocation via `AIProjectClient.get_openai_client(agent_name=…)`), then
synthesizes when two specialists answer.

```bash
# Build one image per agent (deploy/Dockerfile.agent, CAREER_AGENT build arg) + register all four.
./deploy/provision_agents_hosted.sh <resource-group> [image-tag]
```

What it does, per agent (`orchestrator`, `job-match`, `career-coach`, `company-intel`):
1. `az acr build` of `deploy/Dockerfile.agent` with `--build-arg CAREER_AGENT=<agent>` →
   `<acr>.azurecr.io/career-agent-<agent>:<tag>`.
2. Registers it as a `HostedAgentDefinition` (Responses protocol, port 8088) via
   `scripts/provision_hosted.py`, passing `CAREER_AGENT=<agent>` + runtime env (Mongo URI, Azure
   OpenAI, MCP URL, …). `src/agent_host.py` reads `CAREER_AGENT` to decide what to serve.
3. Polls `get_version(...).status` until **ACTIVE**.

The same ACR image-pull identity gotcha as Option 2 applies (the Foundry **project** managed
identity pulls the images) — the wrapper grants `AcrPull` + `Container Registry Repository Reader`
and enables ARM AAD auth automatically. It resolves the Foundry account's **actual** resource group
(which may differ from the deploy RG) via `az cognitiveservices account list`, and grants both the
project and account managed identities. RBAC propagation can take a few minutes; a first run may
need a re-run.

> **Reserved env vars.** Foundry rejects hosted-agent containers that set any `AGENT_*` or
> `FOUNDRY_*` environment variable. That is why the per-agent selector is `CAREER_AGENT`, not
> `AGENT_NAME`.

### How a turn flows

```
caller ──Responses──▶ orchestrator (hosted)
                         │  classify()
                         ├─▶ job-match     (hosted) ─┐  each: Responses turn + MongoDB MCP,
                         ├─▶ career-coach   (hosted) ─┤  answering ONLY from MongoDB data
                         └─▶ company-intel  (hosted) ─┘
                         │  synthesize (if 2 answered)
                         ▼
                      final answer  (or the "no data in MongoDB" refusal)
```

### Invoke it

```python
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

client = AIProjectClient(endpoint="<AZURE_AI_PROJECT_ENDPOINT>",
                         credential=DefaultAzureCredential(), allow_preview=True)
oai = client.get_openai_client(agent_name="orchestrator")
resp = oai.responses.create(input="Find remote senior Python roles and tell me about those companies.")
print(resp.output_text)
```

### Local smoke test (one agent at a time)

```bash
CAREER_AGENT=job-match python -m src.agent_host    # serves that agent on :8088
# in another shell:
curl -N -X POST localhost:8088/responses -H 'Content-Type: application/json' \
  -d '{"input":"Find remote senior Python roles for a MongoDB+Azure background."}'
```

> The `orchestrator` container needs `az login` / a managed identity with the `Cognitive Services
> User` role so it can invoke the specialist hosted agents by reference.

---

## Files

| Path | Role |
|------|------|
| `src/foundry_registry.py` | The one place that calls azure-ai-projects 2.x (prompt + hosted CRUD, invoke-by-reference). |
| `scripts/provision_agents.py` | `verify` / `sync` / `list` / `delete-all` the prompt agents; writes the lock file. |
| `src/hosted_agent.py` | Responses server wrapping `run_turn` (Option 2, the whole solution in one container). |
| `src/agent_host.py` | Responses server for ONE agent (Option 3); `CAREER_AGENT` selects specialist vs orchestrator. |
| `scripts/provision_hosted.py` | Registers a hosted agent + polls to ACTIVE (used by both Option 2 and Option 3). |
| `deploy/Dockerfile.hosted`, `deploy/provision_hosted.sh` | Option 2: whole-solution container + deploy. |
| `deploy/Dockerfile.agent`, `deploy/provision_agents_hosted.sh` | Option 3: per-agent image + register all four. |
| `src/config_loader.py` (`GROUNDING_RULES`) | The strict MongoDB-only grounding contract injected into every agent. |
| `config/foundry_agents.lock.json` | Generated idempotency store for prompt-agent versions. |

## Current deployed state (project `azure-multiagent-proj`)

Verified live and `ACTIVE` on this project:

| Agent | Kind | Model | Notes |
|-------|------|-------|-------|
| `job-match` | prompt | `gpt-5.6-sol` | routable specialist |
| `career-coach` | prompt | `gpt-5.6-sol` | routable specialist |
| `company-intel` | prompt | `gpt-5.6-sol` | routable specialist |
| `career-hosted-solution` | hosted | — | whole-solution container agent (Option 2) |

All three specialists are Foundry-managed prompt agents (Foundry runs the model turn + MongoDB
MCP tool calls when invoked by reference); `orchestrator` is intentionally **not** registered
(`routable: false`, runs host-side). Re-create/refresh them any time with
`python -m scripts.provision_agents sync`.

> Model note: the registered prompt agents use `gpt-5.6-sol` (the provisioning default /
> `cfg.model`), while the deployed container reads `AZURE_AI_MODEL_DEPLOYMENT_NAME` from `.env`
> (currently `gpt-4o`) for its host-side classifier/synthesizer/inline-fallback. Keep these
> aligned if you want one model across the board.

## Notes / limits

- `azure-ai-agentserver-responses` is **beta** (2.0.0b1); it emits experimental-API warnings and,
  off-Azure, an OpenTelemetry IMDS probe (silenced via `OTEL_SDK_DISABLED=true`, set in
  `src/hosted_agent.py`; override in production).
- `requirements-foundry.txt` is the Foundry Agent Service stack, installed on top of
  `requirements.txt`.
- The classic `career-career-coach` agent from the earlier POC lives on a separate (Assistants)
  surface and is unrelated to these new agents.
