# MongoDB × Azure AI Foundry — Multi-Agent Career Assistant

> This project is an **Azure / Microsoft** stack based multi-agent "Career Assistant"
> with **MongoDB Atlas** at its core.  

It runs a **MongoDB-centric** "Career Assistant" scenario on the following stack:

| Component | This demo |
|---|---|
| Agent framework | **Microsoft Agent Framework (MAF)**, Python |
| Agent runtime | **Azure AI Foundry Agent Service** (one agent per specialist) |
| Models | **Azure OpenAI / Foundry** deployments (`gpt-5.6-sol`, `text-embedding-3-large`) |
| Tool gateway | Foundry **MCP tool** → MongoDB MCP on Container Apps |
| Memory | **MongoDB** short-term (`chat_messages`) + long-term facts (`agent_memory_facts`) |
| API | **FastAPI** + SSE |
| Infra-as-code | **Bicep** |

Four specialists ship: `orchestrator`, `job-match`, `career-coach`, `company-intel`.

---

## Prerequisites (must exist before running)

This demo assumes **you** have already provisioned:

1. **Azure AI Foundry project** — `azure-multiagent-proj`. Copy its **project endpoint**.
2. **Chat model deployment** — `gpt-5.6-sol` (used by all specialists + classifier/synthesizer/fact-extractor).
3. **Embedding deployment** — `text-embedding-3-large` → **3072-dim** vectors.
4. **MongoDB Atlas** — cluster `FabricCluster`, database `multi-agent`, on a tier that supports **Atlas Vector Search** (M10+ or Flex). Have the connection string ready.
5. **MongoDB MCP server** — reuse the deployed Container App:
   `https://mongo-mcp-without-auth11may.happyground-0d54aef3.eastus.azurecontainerapps.io/mcp`
   (its connection string already targets `FabricCluster`; the db/collection are passed per query, so seeding into `multi-agent` is directly queryable — no redeploy needed).
6. **Azure CLI** logged in (`az login`) with access to the tenant/subscription — used for `DefaultAzureCredential`.

> Auth is intentionally **off** for v1 (local demo). See *Production hardening* below.

---

## Quick start (local)

```bash
cd mongodb-multiagent-foundry
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.sample .env      # then fill in the blanks (project endpoint, Atlas URI, OpenAI key)
az login                 # DefaultAzureCredential picks this up

# 1) Seed Atlas + create vector/search indexes + generate embeddings
python -m db_seeding.seed_all

# 2) Create the 4 Foundry agents and smoke-test them
python -m src.foundry_agents --smoke-test

# 3) Run the API (FastAPI + SSE) and the UI
uvicorn src.app:app --reload --port ${PORT:-3000}
streamlit run ui/streamlit_app.py     # in a second terminal
```

Open the Streamlit URL and try:
> *"Find jobs matching my CV and tell me about those companies."*

---

## How it works

```
User ── Streamlit UI ──HTTP──▶ FastAPI /chat (SSE)
                                   │
                                   ├─ classifier.py        select specialist(s)  (heuristic → LLM tie-break)
                                   ├─ memory.py            load short-term history + long-term facts (Mongo)
                                   ├─ orchestrator.py      ORCHESTRATOR_MODE:
                                   │     • classifier  → invoke specialist(s) directly, synthesize if 2
                                   │     • maf-handoff → MAF triage agent hands off dynamically
                                   ├─ foundry_agents.py    each specialist = Foundry agent + MongoDB MCP tool + embed tool
                                   ├─ retrieval.py         hybrid $vectorSearch + Atlas $search, RRF + recency + MMR
                                   └─ persist turn + extract facts → Mongo
```

### Orchestration modes (`ORCHESTRATOR_MODE`)
- **`classifier`** *(default)* — custom router. Deterministic and fast. When two specialists are selected, a synthesizer (`gpt-5.6-sol`) merges their answers.
- **`maf-handoff`** — MAF's built-in handoff/group-chat orchestration; a triage agent delegates dynamically among the Foundry specialists. Showcases the framework.

Build/verify `classifier` first, then flip the toggle to demo `maf-handoff`.

### CV parsing (why text-only embeddings are enough)
CV parsing is a **separate** extract-and-structure step (text extractor + `gpt-5.6-sol` pulls skills/experience). Matching then embeds the parsed **text** against job **text** via `text-embedding-3-large`. Multimodal (Voyage) would only be needed to embed *scanned/image* CVs directly — out of scope.

---

## Deploy to Azure (optional)

```bash
cd deploy
./deploy.sh <resource-group> <location>   # e.g. ./deploy.sh mdb-maf-demo eastus
```

Provisions ACR, a Container Apps environment, the embedding Azure Function, and the API + UI Container Apps, then emits endpoints to `.env.live`. The MongoDB MCP server is **reused** (not redeployed) by default.

---

## Repo layout

```
config/agents/    orchestrator|job-match|career-coach|company-intel .agent.md
config/skills/    SKILL.md per specialist
src/              app.py, classifier.py, orchestrator.py, foundry_agents.py,
                  memory.py, retrieval.py, embeddings.py, mcp_client.py, config_loader.py
db_seeding/       seed_all.py + Atlas index definitions + sample data
embedding-function/  Azure Function (POST /embed) — reused from mongodb-foundry-agent
ui/               Streamlit chat + trace viewer
deploy/           Bicep + deploy.sh
```

---

## Production hardening (out of scope for v1)

Private Endpoint / VNET peering to Atlas · Entra ID JWT auth ·
App Insights / Azure Monitor observability · Key Vault secret hardening · rate limiting.
