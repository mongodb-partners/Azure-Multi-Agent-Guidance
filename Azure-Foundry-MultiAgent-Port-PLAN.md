
## Context

The reference repo [`mongodb-partners/Multi-Agent-Architectural-Guidance-Bedrock-AgentCore`](https://github.com/mongodb-partners/Multi-Agent-Architectural-Guidance-Bedrock-AgentCore) is a config-driven multi-agent "Career Assistant" demo on AWS: a Bun/Hono API routes user questions to specialist agents built with the **Strands Agents SDK**, running on **Bedrock AgentCore Runtime**, using **Bedrock Claude** models, **Cognito** auth, and **MongoDB Atlas** for hybrid (vector + BM25) search, short/long-term memory, and facts. Four agents ship: `orchestrator`, `job-match`, `career-coach`, `company-intel`, plus a MongoDB MCP runtime.

We want an equivalent **Microsoft/Azure** demo that keeps the same MongoDB-centric design and the same Career Assistant scenario, but swaps the AWS/Strands stack for:
- **Microsoft Agent Framework (MAF), Python** — replaces Strands as the agent/orchestration SDK.
- **Azure AI Foundry Agent Service** — hosts each specialist agent (replaces AgentCore Runtime/Gateway/Memory).
- **Azure OpenAI / Foundry model deployments** — replace Bedrock Claude/Titan.
- **MongoDB Atlas (kept as-is)** — Atlas Vector Search + Atlas Search hybrid.

Decisions (confirmed with user): **Python + MAF**, **Foundry Agent Service** hosting, **runnable demo scope** (skip Private Endpoint/VNET peering and full observability parity), **keep MongoDB Atlas**, **no auth for v1**.

Outcome: a runnable end-to-end demo (agents + MongoDB MCP + embeddings + routing + memory + chat UI) plus Bicep to deploy the supporting services on Azure.

## Prerequisites (user-provided, must exist before running)

The demo assumes these are already provisioned by the user (the README will list them explicitly):

- **Azure AI Foundry project**: `azure-multiagent-proj` (use its project endpoint).
- **Chat model deployment**: `gpt-5.6-sol` — used for all specialist agents AND the classifier tie-breaker / synthesizer / fact-extractor (single chat deployment reused everywhere).
- **Embedding model deployment**: `text-embedding-3-large` → **3072-dim** vectors (drives Atlas vector index dimensions; optionally reduce via the `dimensions` param if we want smaller indexes).
- **MongoDB Atlas**: cluster `FabricCluster`, database `multi-agent` (must support Atlas Vector Search — M10+ or Flex). Connection string supplied via `.env`.
- **MongoDB MCP server (reused, no change needed)**: `https://mongo-mcp-without-auth11may.happyground-0d54aef3.eastus.azurecontainerapps.io/mcp` — its connection string already targets **FabricCluster**, and the database is not baked in (db/collection are passed per-query as MCP tool arguments). Seeding into `multi-agent` on FabricCluster is therefore directly queryable by the reused MCP.

### CV parsing vs embeddings (Q6 resolution)
Text-only embeddings are sufficient: CV parsing (extract skills/experience) is a separate step handled by a text extractor + `gpt-5.6-sol` structuring; matching then embeds the parsed **text** against job **text** via `text-embedding-3-large`. Voyage multimodal is only needed for embedding scanned/image CVs directly — out of scope.

## Reusable local assets (do NOT rebuild these)

| Asset | Path | Reuse for |
|---|---|---|
| Foundry agent + MCP tool pattern | `AzureFoundry_MongMCP_IntegrationPOC/agent_asst.py` | `AIProjectClient` / `McpTool` wiring, agent create-or-reuse, run/tool-approval loop |
| MCP server + embedding function Bicep | `mongodb-foundry-agent/deploy/{mcp-server,embedding-function}/main.bicep`, `src/embedding-function/function_app.py` | Deploy MongoDB MCP on Container Apps + Azure OpenAI embedding endpoint |
| MAF MongoDB memory providers | `agent-framework-packages/python/packages/mongodb/agent_framework_mongodb/` | `MongoDBContextProvider` (long-term facts) + `MongoDBHistoryProvider` (short-term chat) |
| Agent decision instructions | `mongodb-foundry-agent/docs/agent-instructions.md` | Base for skill/agent prompt authoring |

## Target architecture (component mapping)

| Reference (AWS/Strands) | Azure/MAF replacement | Notes |
|---|---|---|
| Strands Agents SDK | **Microsoft Agent Framework** (`agent-framework`, `agent-framework-foundry`) | Agents, tools, orchestration/handoffs |
| Bedrock Claude (agents) | Foundry model deployment (e.g. `gpt-4o` / `gpt-4o-mini`) via `AzureAIAgentClient` | Set `model:` in agent config to deployment name |
| AgentCore Runtime (per specialist) | **Foundry Agent Service** agent (one per specialist) | Created via `AIProjectClient.agents` / MAF `AzureAIAgentClient` |
| AgentCore Gateway (tool dispatch) | Foundry **MCP tool** (`McpTool`) → MongoDB MCP on Container Apps | Reuse POC pattern |
| AgentCore Memory (short-term) | `MongoDBHistoryProvider` (MongoDB `chat_messages`) | From `agent-framework-packages` |
| Long-term facts (Mongo) | `MongoDBContextProvider` (MongoDB `agent_memory_facts`) | Keep RRF/recency logic in retrieval layer |
| Bedrock Titan / SageMaker Voyage embeddings | Azure OpenAI embeddings (`text-embedding-3-*`) via embedding Function | Reuse `function_app.py` |
| Bedrock Knowledge Base (company-intel RAG) | MongoDB Atlas vector collection (drop KB dependency) | Keep it Atlas-only for the demo |
| Bedrock Haiku classifier / fact extractor / synthesizer | `AzureOpenAIChatClient` with tool-forced routing | Small/fast deployment (`gpt-4o-mini`) |
| Cognito JWT auth | Demo: local/no-auth or simple Entra ID app (optional) | Out of core scope per "runnable demo" |
| Bun/Hono API + SSE | Python **FastAPI** app with SSE (`text/event-stream`) | New; mirrors `routes/chat.ts` behavior |
| Streamlit UI | Keep Streamlit chat UI (Python) | Point at new FastAPI endpoint |
| Terraform (21 modules) | **Bicep** (Container Apps, Functions, ACR, Azure OpenAI/Foundry) | Reuse/extend `mongodb-foundry-agent` Bicep |

## Proposed project layout

Create a new folder `mongodb-multiagent-foundry/` under `/Users/dianaannie.jenosh/Work/`:

```
mongodb-multiagent-foundry/
├── config/
│   ├── agents/            # orchestrator|job-match|career-coach|company-intel .agent.md (ported, model → Foundry deployment)
│   └── skills/            # SKILL.md per specialist (ported from reference)
├── src/
│   ├── app.py             # FastAPI entry, /chat (SSE), /agents, /sessions
│   ├── config_loader.py   # parse .agent.md / SKILL.md frontmatter (port of config-scan)
│   ├── foundry_agents.py  # create-or-reuse Foundry agents via AzureAIAgentClient (from POC pattern)
│   ├── classifier.py      # heuristic + AzureOpenAIChatClient tie-breaker (port of agent-classifier.ts)
│   ├── orchestrator.py    # MAF routing/handoff + multi-specialist synthesis
│   ├── memory.py          # wires MongoDBContextProvider + MongoDBHistoryProvider
│   ├── retrieval.py       # hybrid vector+BM25 + RRF/recency/MMR (port of vector-retrieval.ts)
│   ├── embeddings.py      # calls Azure OpenAI embedding Function
│   └── mcp_client.py      # MongoDB MCP tool config (McpTool)
├── mcp-server/            # (reuse mongodb/mongodb-mcp-server image; config only)
├── embedding-function/    # copied from mongodb-foundry-agent/src/embedding-function
├── db-seeding/            # Python seeders + Atlas index definitions (vector + search)
├── ui/                    # Streamlit chat UI (adapted from reference ui/)
├── deploy/                # Bicep: ACR, Container Apps (MCP + API + UI), Function, Azure OpenAI/Foundry
├── requirements.txt
├── .env.sample
└── README.md
```

## Implementation steps

1. **Scaffold + deps.** Create the layout above. `requirements.txt`: `agent-framework`, `agent-framework-foundry`, `agent-framework-mongodb`, `azure-ai-projects`, `azure-ai-agents`, `azure-identity`, `openai`/`azure` OpenAI client, `fastapi`, `uvicorn`, `sse-starlette`, `pymongo`/`motor`, `streamlit`. Write `.env.sample` (see below).

2. **Port agent/skill configs.** Copy the 4 `.agent.md` and 3 `SKILL.md` from the reference `config/`. Change frontmatter `model:` from `bedrock.anthropic.claude-*` to Foundry deployment names; drop `bedrock_kb_retrieve` tool, keep `mongodb_*` + `embed` tools. Implement `config_loader.py` to read frontmatter (mirror reference `config-scan.ts`/agent-authoring format).

3. **Foundry agents (`foundry_agents.py`).** ~~Port the create-or-reuse + MCP-tool wiring using the classic Agents threads+runs API.~~ **REVISED during build (2026-07-31):** the deployed chat model `gpt-5.6-sol` is a **reasoning-class** deployment, and the classic Azure AI **Agents threads+runs** API force-injects `top_p`, which the model rejects (`invalid_prompt: 'top_p' is not supported`) — unavoidable on that API. Specialists therefore run on the **Azure OpenAI Responses API** (`client.responses.create(model, instructions, input, tools=[<hosted MCP>], stream=True)`), which supports reasoning models and **hosted MCP** natively. The MongoDB MCP server is attached as a hosted-MCP tool dict (`mcp_client.py:mcp_tool_dict()`, `require_approval="never"`) rather than the Foundry `McpTool` object. Verified live: all 3 specialists call MongoDB MCP tools and answer, streaming a tool trace over the same `Event` interface the orchestrator consumes. Legacy `build_mcp_tool()` retained for reference / non-reasoning deployments.

4. **Classifier (`classifier.py`).** Port `agent-classifier.ts`: heuristic token-overlap scoring over agent descriptions, with an `AzureOpenAIChatClient` tool-forced `select_specialist` call as tie-breaker; keep multi-select thresholds. Env-controlled backend (`heuristic|llm|auto`).

5. **Orchestration + synthesis (`orchestrator.py`) — HYBRID (A default + B toggle).** Controlled by `ORCHESTRATOR_MODE`:
   - `classifier` (default, Approach A): invoke the classifier-selected specialist(s) directly, stream events, and when 2 specialists are chosen run a synthesizer (`gpt-5.6-sol`) that merges answers — port `multi-specialist-orchestrator.ts` + `specialist-answer-synthesizer.ts` behavior. Fast, deterministic, faithful to the reference.
   - `maf-handoff` (Approach B): use MAF's built-in handoff/group-chat orchestration with a triage agent that delegates dynamically among the Foundry-hosted specialists — showcases the framework. Mirrors the reference's own `ORCHESTRATOR_MODE` switch.
   Build A first and get it working end-to-end, then layer B behind the toggle.

6. **Memory (`memory.py`).** Wire `MongoDBHistoryProvider` (short-term, `chat_messages`, session-isolated) and `MongoDBContextProvider` (long-term facts, `agent_memory_facts`) from `agent-framework-packages`. Keep LLM fact-extraction per turn via `AzureOpenAIChatClient`.

7. **Retrieval + embeddings (`retrieval.py`, `embeddings.py`).** Port hybrid search: `$vectorSearch` + Atlas `$search` (BM25), fused with RRF (k=60), recency decay, MMR (λ=0.7) — logic from `vector-retrieval.ts`. `embeddings.py` calls the Azure OpenAI embedding Function (reuse `function_app.py`).

8. **API (`app.py`).** FastAPI with SSE: `POST /chat` (classify → retrieve memory → invoke specialist(s) via Foundry → stream tokens/tool-calls/handoffs → persist), `GET /agents`, `GET /sessions`. Mirror the event types the reference streams so the UI works unchanged.

9. **UI (`ui/`).** Adapt reference Streamlit chat + trace viewer to the new endpoint; strip Cognito (local demo).

10. **DB seeding (`db-seeding/`).** Python seeders for `candidates`, `jobs`, `companies`, `interview_prep`, `career_paths`, `skills_taxonomy`, `compensation_data`, `chat_*`, `agent_memory_facts` into Atlas `FabricCluster`/`multi-agent`; create Atlas Vector Search indexes (**3072-dim** for `text-embedding-3-large`, cosine) + Atlas Search (BM25) indexes; generate embeddings via the embedding Function.

11. **Deploy (`deploy/` Bicep).** Extend `mongodb-foundry-agent` Bicep: ACR, Container Apps env, MongoDB MCP container (`mongodb/mongodb-mcp-server`, Atlas conn string as secret, read-only), embedding Azure Function, API + UI Container Apps, and Azure OpenAI/Foundry model deployments. A `deploy.sh` orchestrates and emits endpoints/`.env.live`.

12. **README + `.env.sample`.** Document Foundry project endpoint, model deployment names, Atlas URI, MCP URL, embedding URL; local-run and deploy instructions.

## `.env.sample` (key vars)

```
AZURE_AI_PROJECT_ENDPOINT=            # endpoint of Foundry project "azure-multiagent-proj"
AZURE_TENANT_ID=
AZURE_AI_MODEL_DEPLOYMENT_NAME=gpt-5.6-sol     # specialist agents
CLASSIFIER_MODEL_DEPLOYMENT=gpt-5.6-sol        # classifier/synthesizer/fact-extractor (same deployment)
AZURE_OPENAI_ENDPOINT=                # for embedding function
AZURE_OPENAI_API_KEY=
EMBEDDING_MODEL=text-embedding-3-large         # 3072-dim
EMBEDDING_DIMENSIONS=3072
MONGODB_URI=mongodb+srv://...FabricCluster...   # user-supplied
MONGODB_DB=multi-agent
MONGODB_MCP_URL=https://mongo-mcp-without-auth11may.happyground-0d54aef3.eastus.azurecontainerapps.io/mcp
CLASSIFIER_BACKEND=auto
ORCHESTRATOR_MODE=classifier          # classifier | maf-handoff (see orchestration decision)
MONGODB_ALLOW_WRITE=false
PORT=3000
```

## Verification

- **Unit/local first:** run MongoDB MCP container locally (or point to deployed Container App); `python db-seeding/seed_all.py` then confirm Atlas indexes exist and `embeddings.py` returns a vector of expected dim.
- **Agent smoke test:** a script that creates the 4 Foundry agents and issues a single query per specialist through `foundry_agents.py`, asserting a non-empty streamed answer and at least one `mongodb_*` tool call (mirrors the POC's `query_agent` loop).
- **Classifier test:** feed 4–5 canned prompts; assert each routes to the expected specialist and that a multi-intent prompt selects 2.
- **End-to-end:** `uvicorn src.app:app` + `streamlit run ui/...`; ask the reference demo prompts (e.g. "Find jobs matching my CV and tell me about those companies") and confirm SSE streaming, hybrid-search-backed answers, memory persistence across turns (`chat_messages` populated, facts in `agent_memory_facts`).
- **Deploy check:** run `deploy/deploy.sh` into a test resource group; hit the deployed API `/agents` and one `/chat` SSE call end-to-end.

## Out of scope (per runnable-demo decision)

Private Endpoint/VNET peering to Atlas, Entra ID/Cognito-parity JWT auth, App Insights/Azure Monitor full observability, Key Vault hardening, rate limiting. Note these as "production hardening" follow-ups in the README.

---

## Open questions — ALL RESOLVED ✅

Summary of resolutions (see full plan sections above):
1. **Foundry project** — user-provisioned; documented as prereqs. Project `azure-multiagent-proj`, chat `gpt-5.6-sol`, embeddings `text-embedding-3-large` (3072-dim).
2. **MCP server** — reuse existing Container App; connection string already targets FabricCluster, db passed per-query → no change.
3. **Atlas** — FabricCluster / `multi-agent` DB; connection string via `.env`.
4. **Orchestration** — **Hybrid**: custom classifier default + MAF handoff behind `ORCHESTRATOR_MODE` toggle.
5. **Auth** — none for v1 (production hardening later).
6. **Embeddings** — text-only OK; CV parsing is a separate extract+structure step, then text↔text matching via `text-embedding-3-large`.

---

## Original answers (as jotted):

1. **Foundry project** — Do you already have an Azure AI Foundry project + model deployments (`gpt-4o`, `gpt-4o-mini`, `text-embedding-3-*`) provisioned, or should the Bicep create them?
Ans: No, we will assume that the user has created and deployed the models. We should instruct exactly what all the pre-reqs are. In my case use the project I created today - "azure-multiagent-proj" . I have also deployed "gpt-5.6-sol" and "text-embedding-3-large" models.
2. **MongoDB MCP server** — Reuse the already-deployed Container App from `AzureFoundry_MongMCP_IntegrationPOC` (the `blackcoast-…eastus` URL), or deploy a fresh one against the demo Atlas cluster?
Ans: Reuse Container App . Use "https://mongo-mcp-without-auth11may.happyground-0d54aef3.eastus.azurecontainerapps.io/mcp"
3. **Atlas cluster** — Which cluster/DB to seed the Career Assistant data into? (Needs Atlas Vector Search — M10+ or Flex.)
Ans: Cluster is FabricCluster. Created a DB called multi-agent and a test collection within it. Connection string I can provide in the .env file
4. **MAF orchestration primitive** — Confirm which MAF pattern to use for routing (custom classifier + direct specialist invoke, vs MAF's built-in handoff/group-chat orchestration). Plan currently favors the ported custom classifier for fidelity to the reference. 
Ans (resolved): **Hybrid.** Use the ported custom classifier as the **default** (`ORCHESTRATOR_MODE=classifier`) for fidelity/determinism, with a **toggle** to MAF's built-in handoff/group-chat orchestrator (`ORCHESTRATOR_MODE=maf-handoff`) to showcase the framework. Build the classifier path first end-to-end, then layer the MAF path behind the toggle.
5. **Auth** — OK to run the demo with no auth locally, or do you want a minimal Entra ID app registration wired in from the start?
Ans: Lets start the first version without auth
6. **Multimodal embeddings** — Reference uses Voyage multimodal. Demo plan uses text-only Azure OpenAI embeddings. Confirm text-only is acceptable (drops image/CV-photo matching).
Ans (resolved): **Yes, text-only suffices.** CV parsing is a separate extract-and-structure step (text extractor + `gpt-5.6-sol` to pull skills/experience), after which matching embeds the parsed **text** against job **text** via `text-embedding-3-large`. Multimodal (Voyage) would only be needed to embed scanned/image CVs directly — out of scope. See "CV parsing vs embeddings (Q6 resolution)" above.
