# Plan 001: Foundry-managed short-term memory

Technical plan for [spec.md](spec.md) (status: approved). Traces to requirement IDs (`R#`)
and acceptance criteria (`AC#`) from the spec.

## 0. Open-question resolutions (proposed; folded back into spec.md §6)

| Q | Resolution |
|---|---|
| **Q1** (retention/cleanup) | v1 performs **no explicit cleanup** — Foundry conversations are durable objects and are left to platform retention. The `chat_sessions` doc stores `conversation_id` so a future cleanup job can delete abandoned conversations. Region/quota verification happens in Phase 5 before the default flips. |
| **Q2** (history cap) | **Keep `MAX_HISTORY=20` text injection.** Prompts continue to receive `history_as_text()` output; the Foundry conversation is the *persistence* layer, not (yet) the model-input mechanism. Passing `conversation=<id>` natively into specialist Responses calls changes invocation semantics (MCP tool loop, per-specialist forking) and is deferred to a future spec. |
| **Q3** (topology ownership) | **The host-side conversation mapping always owns cross-turn history**, in all four topologies. Hosted-runtime session state is treated as per-invocation tool-loop state and stays internal to the hosted agent; it is never read back for history. One owner, no divergence. |
| **Q4** (cross-backend transition) | **Hard split by backend flag** — no read-through fallback. Sessions created under `mongo` stay readable via `agent_traces`/`chat_sessions` metadata in the Sessions UI, but their *history context* is only loaded by the backend that created them. Switching backends starts history fresh. |
| **Q5** (region availability) | `SHORT_TERM_MEMORY_BACKEND` ships defaulting to `mongo`. The flip to `foundry` is a config change made only after Phase 5 verification in the deployed region. |

## 1. Target architecture

```
                        ┌────────────────────────────────────────────┐
 callers (unchanged)    │ src/memory/__init__.py  (facade)           │
 orchestrator.py        │                                            │
 agent_host.py          │  append_message / get_history /            │
 app.py (/sessions)     │  history_as_text  ──► backend dispatch     │
 ui/pages/2_Sessions.py │      on settings.short_term_memory_backend │
                        │                                            │
                        │  list_sessions ──► chat_sessions + traces  │
                        │  save_trace / get_traces ──► agent_traces  │ (Atlas, unchanged — R6)
                        │  facts_* / extract_and_store_facts         │ (Atlas, unchanged — R5)
                        └───────┬────────────────────┬───────────────┘
                                │                    │
                ┌───────────────▼───────┐  ┌─────────▼──────────────┐
   backend=mongo│ src/memory/_mongo.py  │  │ src/memory/_foundry.py │backend=foundry
                │ chat_messages (today) │  │ Foundry Conversations  │
                └───────────────────────┘  │ + chat_sessions mapping│
                                           └────────────────────────┘
```

Key design choices:

1. **Facade, not rewrite.** `src/memory.py` becomes `src/memory/__init__.py` re-exporting
   the exact same public names. Call sites (`orchestrator.py`, `agent_host.py`, `app.py`,
   tests) keep working untouched (R1). Import surface is the test for this.
2. **Only history is backend-dispatched.** Facts, traces, and `list_sessions` stay
   pymongo/Atlas in the facade regardless of backend (R4–R6).
3. **`chat_sessions` is written by both backends** on every `append_message`, so
   `list_sessions` never depends on which backend owns history (R4, Q4).

## 2. Module design

### `src/memory/__init__.py` (facade)

- Re-exports: `append_message`, `get_history`, `history_as_text`, `list_sessions`,
  `save_trace`, `get_traces`, `get_facts`, `facts_as_text`, `extract_and_store_facts`,
  `ensure_indexes`, `MAX_HISTORY`.
- `append_message` / `get_history` dispatch:
  ```python
  def _history_backend():
      return _foundry if get_settings().short_term_memory_backend == "foundry" else _mongo
  ```
- `list_sessions`: aggregation over `chat_sessions` (messages/first/last/user_id) joined
  in Python with the existing `agent_traces` stats pipeline — **response shape identical
  to today** (AC2). Fallback `messages // 2` turn heuristic is preserved.
- `ensure_indexes`: adds `chat_sessions` unique index on `session_id` (plus
  `last_active` desc for the 200-session listing).

### `src/memory/_mongo.py`

- Today's `append_message` / `get_history` / `history_as_text` moved verbatim, plus a call
  to `_shared.touch_session(...)` (below) inside `append_message`.

### `src/memory/_foundry.py`

- **Client:** lazy `_conversations_client()` following the repo's existing auth pattern
  (`src/llm.py:21`, `src/foundry_agents.py:42`): `AIProjectClient(endpoint, credential)`
  → `project.get_openai_client().conversations`. Requires `project_endpoint`;
  `settings.require("project_endpoint")` at first use, not import (keeps unit tests and
  `mongo`-backend runs from needing Foundry env).
- **Mapping:** `session_id → conversation_id` in `chat_sessions`
  (`{"$setOnInsert": {conversation_id}}`, upsert; race-safe for concurrent first-turns by
  re-reading on duplicate-key). Conversation created lazily on first turn.
- **`append_message`** → `conversations.items.create(conversation_id, items=[{type:
  "message", role, content}])`; then `_shared.touch_session(...)`.
  - Roles map 1:1 (`user`/`assistant`); `agent` metadata is not representable as a
    conversation item — it goes to `chat_sessions.last_agent` instead (only consumer was
    history text, which doesn't use it).
- **`get_history(limit)`** → `conversations.items.list(conversation_id)`; filter to
  message items, map to today's `{role, content, ts}` dicts, return last `limit`.
  Ordered oldest-first to match the mongo backend's contract.
- **Failure policy:** mirror `_persist`'s existing resilience — history append failures
  log-and-continue (a lost turn of history must not 500 the chat); `get_history` failure
  returns `[]` (agent answers without context rather than erroring).

### `src/memory/_shared.py`

- `touch_session(session_id, user_id, role, content, conversation_id=None)` — one upsert
  per `append_message` into `chat_sessions`:
  `{session_id, user_id, conversation_id, first_ts (setOnInsert), last_ts, messages += 1,
  title = first user message (truncated 120 chars, setOnInsert)}`.
- Owns the `MongoClient` singleton and `_now()` (moved from `memory.py`).

### Settings & env (R3)

- `src/settings.py`: `short_term_memory_backend: str = os.getenv("SHORT_TERM_MEMORY_BACKEND", "mongo")`
  with a comment documenting `mongo|foundry`.
- `.env.sample`: add `SHORT_TERM_MEMORY_BACKEND=mongo` with a one-line comment.
- `GET /health` already reflects effective config — add the backend to its payload if the
  shape allows without breaking the UI (check `app.py` health handler; additive field only).

## 3. Files touched

| File | Change | Traces to |
|---|---|---|
| `src/memory.py` → `src/memory/{__init__,_mongo,_foundry,_shared}.py` | split + facade | R1–R4 |
| `src/settings.py` | +1 setting | R3 |
| `.env.sample` | +1 var | R3 |
| `src/app.py` | health payload (additive, optional) | R3 |
| `tests/unit/test_memory.py` | move/extend for package layout + `chat_sessions` | R4, R9, AC3 |
| `tests/unit/test_memory_foundry.py` | new, mocked conversations client | R2, R9, AC4 |
| `tests/unit/test_memory_facade.py` | new: backend dispatch, import-surface parity | R1, R3 |
| `tests/integration/test_memory_foundry_live.py` | new, `RUN_INTEGRATION=1` | AC6 |
| `docs/memory.md` (or README section) | document backends + flag | — |

Not touched: `src/orchestrator.py`, `src/agent_host.py`, `ui/` (proof of R1/AC2).

## 4. Phases & tasks

1. **Refactor (no behavior change).** Split `memory.py` into the package; `mongo` backend
   is a byte-for-byte move; `list_sessions` still reads `chat_messages`. Existing unit
   tests must pass unmodified. *(R1)*

   > **Implementation note (deviation recorded per specs/README rules):** the unit-test
   > fixture's monkeypatch target moved from `memory._db` to `src.memory._shared._db`
   > (the client singleton now lives in `_shared` so all backends share it). One-line
   > fixture change; test bodies and assertions are unmodified. Two additive additions
   > beyond the plan: `GET /health` gained a `memory_backend` field, and
   > `list_sessions` unions legacy `chat_messages`-derived sessions with `chat_sessions`
   > (needed so pre-existing sessions keep listing — the plan's "chat_sessions only"
   > switch would have hidden them).
2. **`chat_sessions` metadata.** Add `_shared.touch_session` calls (mongo backend first),
   switch `list_sessions` to `chat_sessions`, add indexes. Parity test: same `/sessions`
   shape before/after. *(R4, AC2, AC3)*
3. **Foundry backend.** `_foundry.py` + settings + `.env.sample`. Unit tests with a fake
   conversations client (constructor-injected, mirroring `FoundrySpecialist(client=...)`).
   *(R2, R3, R8, R9, AC4)*
4. **Facade dispatch + rollback drill.** `test_memory_facade.py`; manually verify
   `mongo → foundry → mongo` flip locally. *(R3, AC5)*
5. **Live verification.** `RUN_INTEGRATION=1` against a dev Foundry project: two-turn
   context recall (AC1), process-restart continuity (AC6), region/quota check (Q5).
   Then update `docs/` and propose flipping the default in a follow-up PR.

## 5. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Conversation-items API shape differs from docs (preview drift) | Live-gated integration test in Phase 5 is the gate; unit tests mock at the client boundary so internals can change freely |
| Duplicate conversation on concurrent first-turns | upsert + duplicate-key re-read (§2) |
| Foundry outage blocks chat | `get_history` → `[]`, append → log-and-continue (matches today's `_persist` resilience); rollback flag (AC5) |
| `chat_messages` orphaned docs confuse operators | Docs note: legacy collection retained read-only; no migration by design (spec §4) |
| History text parity (ordering/limits) | Facade test asserts identical `history_as_text` output shape for both backends on the same scripted turns |
