# Spec 001: Foundry-managed short-term memory, Atlas-retained long-term memory

- **Status:** implemented
- **Author:** Anuj Panchal
- **Created:** 2026-09-18
- **Approved:** 2026-09-18
- **Implemented in:** working tree 2026-09-18 (Phases 1–4 + Phase-5 artifacts; live Phase-5
  verification pending — see plan.md §4)

## 1. Summary

Move **short-term (session) memory** off the hand-rolled pymongo implementation and onto
**Microsoft Foundry managed conversational state** (the Conversations API), while keeping
**long-term memory** (extracted user facts) and **audit traces** in **MongoDB Atlas** as today.
This aligns the project with the Foundry platform's managed capabilities, removes custom
session-history plumbing, and keeps Atlas as the system of record for durable, queryable
long-term data.

> Terminology note: Foundry's *Memory Store* (preview) is the managed **long-term** offering;
> the managed **short-term** primitive is the **Conversations** API (durable server-side
> conversation objects with items). This spec adopts Conversations for short-term state.
> Adopting the Memory Store for long-term facts is explicitly out of scope (see §4).

## 2. Context & current state

All memory is hand-rolled in `src/memory.py` (pymongo, db `multi-agent`):

| Layer | Collection | Public API | Consumers |
|---|---|---|---|
| Short-term session history | `chat_messages` | `append_message`, `get_history`, `history_as_text`, `list_sessions` | `src/orchestrator.py` (370, 451–452), `src/agent_host.py` (119, 204–205), `src/app.py` (96), `ui/pages/2_Sessions.py` |
| Long-term user facts | `agent_memory_facts` | `extract_and_store_facts`, `get_facts`, `facts_as_text` | `src/orchestrator.py` (371, 453), `src/agent_host.py` (120, 206) |
| Per-turn audit traces | `agent_traces` | `save_trace`, `get_traces` | `src/orchestrator.py` (473), `src/app.py` (102), Sessions UI |

`list_sessions` additionally aggregates per-session token/cost/latency stats via a pymongo
pipeline — the Sessions and Metrics UI pages depend on this shape.

Problems with the status quo:

- Session history is re-implemented plumbing that Foundry already manages (Conversations
  persist items server-side, survive process restarts, and feed the model directly via
  `conversation=<id>` on the Responses call).
- Hosted agents (Option 2/3) get **session-level state persistence from the platform**,
  so the DIY layer is redundant — and divergent — across the four execution topologies.
- History is injected as flattened text (`history_as_text`, last 20 turns) rather than as
  structured conversation items the model consumes natively.

## 3. Requirements

- **R1** — A `ShortTermMemory` backend abstraction must be introduced behind the existing
  call surface (`append_message`, `get_history`, `history_as_text`, `list_sessions`), so
  `src/orchestrator.py` and `src/agent_host.py` require **no signature changes** at call sites.
- **R2** — A `foundry` backend must implement the abstraction using the Foundry
  **Conversations API**: one conversation per `session_id` (mapping persisted so the same
  session always resolves to the same conversation), items appended per turn, history read
  back from conversation items.
- **R3** — Backend selection must be config-driven via `SHORT_TERM_MEMORY_BACKEND`
  (`mongo` | `foundry`), defaulting to `mongo` until this spec is implemented and validated,
  then flipping to `foundry` (see AC5 rollback requirement).
- **R4** — `GET /sessions` and the Streamlit Sessions page must keep working with the
  `foundry` backend. Because conversation objects carry no token/cost aggregates, a thin
  Atlas-side `chat_sessions` metadata record (session_id, user_id, created_at, last_active,
  title/first-message preview, turn aggregates) must be maintained per turn regardless of
  backend. Aggregates continue to be sourced from `agent_traces`.
- **R5** — Long-term memory (`agent_memory_facts`, `extract_and_store_facts`, `get_facts`,
  `facts_as_text`) must remain in MongoDB Atlas, functionally unchanged.
- **R6** — Audit traces (`agent_traces`, `save_trace`, `get_traces`) must remain in MongoDB
  Atlas, functionally unchanged.
- **R7** — When `SPECIALIST_MODE` routes through Foundry hosted agents (Options 2/3), the
  platform's own session state must not conflict with the conversation mapping in R2; the
  spec implementation must define which layer owns history per topology (see Q3).
- **R8** — `store=false` must never be used in a way that loses history: if a Responses call
  opts out of storage, the turn must still be appended to the conversation explicitly.
- **R9** — Unit tests must run fully offline (no live Foundry/Atlas): the Foundry
  conversations client must be mockable at the module boundary, mirroring the existing
  mongomock-based tests for the `mongo` backend.

## 4. Non-goals

- Migrating long-term facts to the Foundry **Memory Store** (preview) — a separate future spec.
- Changing the fact-extraction LLM flow, grounding rules, or classifier behavior.
- Changing the trace schema, SSE event taxonomy, or the Metrics page.
- PII redaction / memory governance controls (deferred; note Foundry Memory's preview
  security guidance when that spec is written).
- Data migration of existing `chat_messages` documents into Foundry conversations
  (existing sessions stay readable via the `mongo` backend during transition; see Q4).

## 5. Contracts affected

| Contract | Today | After |
|---|---|---|
| `src/memory.py` | Single pymongo module | Split: backend-agnostic facade + `mongo` backend + `foundry` backend (e.g., `src/memory/__init__.py`, `src/memory/mongo.py`, `src/memory/foundry.py`) |
| Env / settings (`src/settings.py`, `.env.sample`) | — | `SHORT_TERM_MEMORY_BACKEND=mongo|foundry` |
| Atlas collections | `chat_messages`, `agent_memory_facts`, `agent_traces` | `chat_messages` (legacy/mongo backend only), new `chat_sessions` metadata, `agent_memory_facts`, `agent_traces` unchanged |
| Foundry resources | none for memory | One conversation per session (created lazily); mapping `session_id → conversation_id` stored in `chat_sessions` |
| `GET /sessions` (`src/app.py:96`) | pymongo aggregation over `chat_messages` + `agent_traces` | Reads `chat_sessions` + `agent_traces`; response shape unchanged |
| `src/orchestrator.py`, `src/agent_host.py` call sites | direct `memory.*` calls | unchanged signatures |
| `ui/pages/2_Sessions.py` | consumes `/sessions` | unchanged (API shape preserved) |

## 6. Open questions

Resolved at approval (2026-09-18) — see `plan.md` §0 for rationale:

| # | Question | Resolution |
|---|----------|-----------|
| Q1 | Conversation retention/TTL and quotas — cleanup needed? | No explicit cleanup in v1; `conversation_id` persisted in `chat_sessions` to enable a future cleanup job. Quotas verified in plan Phase 5. |
| Q2 | Keep `MAX_HISTORY=20` cap or platform truncation? | Keep the cap. Conversations are the persistence layer only; native `conversation=` model input is deferred to a future spec. |
| Q3 | Per-topology history ownership? | Host-side conversation mapping owns cross-turn history in all topologies; hosted-runtime session state stays internal to the tool loop. |
| Q4 | Cross-backend transition behavior? | Hard split by backend flag, no read-through fallback. Legacy sessions remain visible in the UI via `chat_sessions`/`agent_traces`. |
| Q5 | Region/preview availability? | Flag defaults to `mongo`; flip to `foundry` only after Phase-5 live verification in the deployed region. |

## 7. Acceptance criteria

- **AC1** (R1, R2): With `SHORT_TERM_MEMORY_BACKEND=foundry`, a two-turn chat over `/chat`
  resolves the follow-up turn with prior-turn context (verifiable via the turn trace's
  memory-context section), with no code changes in `orchestrator.py`/`agent_host.py`.
- **AC2** (R4): `GET /sessions` returns the same JSON shape as before for both backends;
  the Sessions UI page renders session list and per-turn traces without modification.
- **AC3** (R5, R6): After any turn, `agent_memory_facts` and `agent_traces` contain the same
  document shapes as pre-change (verified by existing unit tests plus a new parity test).
- **AC4** (R9): `pytest -m unit` passes with network access disabled; new `foundry` backend
  tests mock the conversations client.
- **AC5** (R3): Flipping `SHORT_TERM_MEMORY_BACKEND=mongo` restores prior behavior without
  data loss (rollback path).
- **AC6** (R2): Restarting the API process mid-session does not lose conversation history
  under the `foundry` backend.

## 8. Test plan

- `tests/unit/test_memory_foundry.py` — new: mocked conversations client; covers create/append/
  read/list, mapping persistence, error paths.
- `tests/unit/test_memory.py` (existing mongomock suite) — extended: `chat_sessions` metadata
  writes for the `mongo` backend.
- `tests/unit/test_orchestrator.py` — unchanged (facade preserves call surface; add one
  parametrized case per backend if cheap).
- `tests/integration/test_memory_foundry_live.py` — new, gated by `RUN_INTEGRATION=1`:
  real Foundry conversation round-trip.
- `smoke/post_deploy_smoke.py` — unchanged; implicitly exercises the active backend via `/chat`.
