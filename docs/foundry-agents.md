# Foundry-native agents — two consumption options

The four specialists are defined once in `config/agents/*.agent.md`. From that single source of
truth you can consume the system two ways, both **new** Azure AI Foundry Agent Service (Responses-
based, *not* classic Assistants/threads):

1. **Use one agent** — each routable specialist (`job-match`, `career-coach`, `company-intel`) is
   registered as a persistent, portal-visible **prompt agent**, callable by name.
2. **Use the whole solution** — the entire orchestrator (`run_turn`: classify → fan-out →
   synthesize) runs as one **hosted agent** behind a single Responses endpoint.

Everything below reuses the same pipeline the local app runs (`load_agents()` +
`build_agent_instructions()` + `mcp_tool_dict()`), so the registered agents cannot drift from the
code.

---

## Prerequisites

```bash
az login                                   # DefaultAzureCredential
pip install -r requirements-foundry.txt    # optional stack: azure-ai-projects 2.x + agentserver
# .env filled in from .env.sample (AZURE_AI_PROJECT_ENDPOINT, MONGODB_MCP_URL, Azure OpenAI, …)
```

> The project endpoint must be a **new-Foundry project** (`…services.ai.azure.com/api/projects/…`).
> `python -m scripts.provision_agents verify` confirms this before doing anything (verified on
> `azure-multiagent-proj`).

Everything Foundry-native is optional: without `requirements-foundry.txt` the app still runs with
`SPECIALIST_MODE=inline`.

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
# SPECIALIST_MODE=inline               # never use registered agents (original behavior)
```

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

## Files

| Path | Role |
|------|------|
| `src/foundry_registry.py` | The one place that calls azure-ai-projects 2.x (prompt + hosted CRUD, invoke-by-reference). |
| `scripts/provision_agents.py` | `verify` / `sync` / `list` the prompt agents; writes the lock file. |
| `src/hosted_agent.py` | Responses protocol server wrapping `run_turn` (the whole solution). |
| `scripts/provision_hosted.py` | Registers the hosted agent + polls to ACTIVE. |
| `deploy/Dockerfile.hosted`, `deploy/provision_hosted.sh` | Container + one-command hosted deploy. |
| `config/foundry_agents.lock.json` | Generated idempotency store for prompt-agent versions. |

## Notes / limits

- `azure-ai-agentserver-responses` is **beta** (2.0.0b1); it emits experimental-API warnings and,
  off-Azure, an OpenTelemetry IMDS probe (silenced via `OTEL_SDK_DISABLED=true`, set in
  `src/hosted_agent.py`; override in production).
- `requirements-foundry.txt` and `requirements-maf.txt` are **alternative** optional stacks — do
  not install both into one environment.
- The classic `career-career-coach` agent from the earlier POC lives on a separate (Assistants)
  surface and is unrelated to these new agents.
