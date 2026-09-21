# Spec 004: Live reasoning streaming — direct replies and registered/inline parity

- **Status:** implemented
- **Author:** Anuj Panchal (drafted with AI assistant)
- **Created:** 2026-09-21
- **Approved:** 2026-09-21 (verbal go-ahead to implement)
- **Implemented in:** local working tree, 2026-09-21 (`src/llm.py`, `src/orchestrator.py`,
  `src/foundry_registry.py`, `tests/unit/test_llm_stream.py`, `tests/unit/test_orchestrator.py`,
  `smoke/post_deploy_smoke.py`); verified live on the westus2 deployment

## 1. Summary

The chat UI's "🧠 Reasoning (live)" panel stays empty for whole classes of turns because
only one code path ever emits `reasoning` SSE events. This change makes live reasoning
streaming work uniformly: (a) for **direct orchestrator replies** (small talk / capability
questions, no specialist), and (b) for specialist turns whether they execute as
**registered Foundry prompt agents** or **inline** (`SPECIALIST_MODE=registered |
registered-or-inline | inline`).

## 2. Context & current state

The UI panel is fed exclusively by `reasoning` SSE events (`ui/streamlit_app.py:177-191`).
Today exactly one path produces them:

- **Specialist, inline mode** — `src/foundry_agents.py::_stream_query_inline` opens a
  Responses stream with `reasoning={"summary": "auto"}`; `_consume_stream` maps
  `response.reasoning_summary_text.delta` / `response.reasoning_text.delta` /
  `response.reasoning.delta` to `reasoning` events. Verified working on the live
  westus2 deployment (a job-match turn streamed 84 reasoning chunks).
- **Specialist, registered mode** — `src/foundry_registry.py::invoke_by_reference` calls
  `oai.responses.create(input=..., stream=True)` **without** a `reasoning` parameter, so
  no reasoning summary is requested and no `reasoning` events flow, even though the same
  `_consume_stream` mapper is used downstream.
- **Direct reply (no specialist)** — `src/orchestrator.py::_direct_reply_stream` calls
  `src/llm.py::chat_text_stream`, which uses the **chat.completions** API and extracts only
  `delta.content`. Reasoning is never requested or forwarded, so the panel shows
  "Waiting for the model to start reasoning…" for the whole turn. This is the
  user-reported gap.

Supporting machinery already in place and reused unchanged: `_consume_stream`'s event
mapping, `_create_stream_with_fallback`'s strip-rejected-param retry (for models that
reject `reasoning`), the orchestrator's `_Trace.reasoning_text` accumulation (it captures
any `reasoning` event regardless of origin), and the Sessions page's
"🧠 Model reasoning (streamed)" expander.

## 3. Requirements

- **R1** — Direct replies must stream reasoning. The direct-reply path must request a
  reasoning summary from reasoning-class deployments and yield `reasoning` SSE events
  (with `agent="orchestrator"`) as chunks arrive, exactly like the specialist path.
- **R2** — Registered/inline parity. Invoking a registered Foundry prompt agent
  (`SPECIALIST_MODE=registered` or the registered branch of `registered-or-inline`) must
  request a reasoning summary so `_consume_stream` emits the same `reasoning` events as
  the inline path.
- **R3** — Non-reasoning models must keep working. Any deployment that rejects the
  `reasoning` parameter (400) must be retried transparently with the parameter stripped,
  reusing the existing optimistic-request/strip-on-rejection pattern. No model-name
  branching.
- **R4** — Event contract unchanged. SSE event shapes (`reasoning`, `delta`, `final`, …)
  and the UI need no changes; `delta` answer streaming on the direct path must keep
  working as today.
- **R5** — Reasoning effort on the direct path stays cheap. Direct replies exist for
  fast small talk; the reasoning request must use low effort (Responses
  `reasoning: {"effort": "low", "summary": "auto"}`) so greeting latency is not
  materially increased. If a deployment rejects the effort hint it is stripped per R3.
- **R6** — Fallback behavior preserved. If streaming the direct reply fails, the existing
  fallback to a single non-streamed reply (`_direct_reply`) must still apply, and a turn
  must never fail because reasoning streaming failed.
- **R7** — Trace capture unchanged in shape. Reasoning streamed on the direct path must
  land in the persisted turn trace's `reasoning_text` (and the Sessions UI) via the
  existing `_Trace.event` accumulation — no schema change.

## 4. Non-goals

- No UI changes (the panel, expanders, and event handling stay as-is).
- No changes to the classifier, routing heuristics, or the multi-specialist fan-out /
  synthesis path (the synthesizer remains a non-streaming utility call).
- No change to where agents run (registered vs. inline selection logic is untouched).
- No new env vars; no changes to MCP tooling, memory, or retrieval.
- Raw (non-summary) reasoning tokens are not surfaced; summaries only.

## 5. Contracts affected

| Contract | Today | After |
|---|---|---|
| `src/llm.py::chat_text_stream` | chat.completions stream; yields answer text only | Direct-reply streaming moves to the Responses API (new or extended helper) yielding `(kind, text)` chunks or events for answer + reasoning; chat.completions helper retained for non-streaming utility calls |
| `src/orchestrator.py::_direct_reply_stream` / `_run_turn` direct branch | yields only `delta`/`final` | also yields `reasoning` events (`agent="orchestrator"`) (R1) |
| `src/foundry_registry.py::invoke_by_reference` | `responses.create(input, stream)` without `reasoning` | requests `reasoning={"summary": "auto"}` with strip-on-400 retry (R2, R3) |
| `src/foundry_agents.py::_create_stream_with_fallback` | strips `reasoning` on 400 for inline | same helper/pattern reused by the registered invocation (R3) |
| SSE event schema | `reasoning {agent, text}` emitted only by inline specialists | same schema, additionally emitted by direct path and registered path |
| Persisted trace (`reasoning_text`) | populated for specialist turns only | also populated for direct replies (R7) |
| Env vars / settings | — | none added |

## 6. Open questions

| # | Question | Owner | Resolution |
|---|----------|-------|-----------|
| Q1 | Does the agent-bound client from `AIProjectClient.get_openai_client(agent_name=...)` accept a per-request `reasoning` override, or is reasoning fixed by the registered agent definition? | Implementer | **Resolved (2026-09-21):** implemented as a per-request override with strip-on-400 fallback (R3). Live verification is gated on agents being provisioned (`scripts/provision_agents sync`) — none are registered on the current deployment, so registered mode is covered by unit tests + the gated integration test until then. |
| Q2 | Should the direct path keep using chat.completions under some toggle, or fully switch to Responses? | Implementer | **Resolved (2026-09-21):** fully switched. `chat_text_stream` (chat.completions) was removed; the direct path uses the new `chat_text_stream_events` (Responses API). |

## 7. Acceptance criteria

- **AC1** (R1, R4): When the deployment emits a reasoning summary for a direct reply,
  `POST /chat` streams it as `reasoning` events with `agent="orchestrator"` before the
  `final` event. **Amended caveat (verified 2026-09-21 against the live gpt-5.6-sol
  deployment):** whether a summary is generated at all is model-side and
  non-deterministic — the bundled orchestrator prompt makes small talk so templated that
  the model emits *no reasoning item* (confirmed via raw API probes at low/medium effort
  and `summary=auto|detailed`), so "hello"-class turns may legitimately show an empty
  panel. The forwarding mechanism itself is covered by unit tests and by the identical
  specialist-path mapping verified live.
- **AC2** (R2, R4): With agents registered on the Foundry project and
  `SPECIALIST_MODE=registered`, a specialist prompt streams `reasoning` events; the same
  prompt under `SPECIALIST_MODE=inline` streams them too (parity).
- **AC3** (R3): Against a deployment that rejects the `reasoning` parameter, all three
  paths (direct, registered, inline) complete successfully with answer text and no
  `reasoning` events — no user-visible error.
- **AC4** (R5): Direct-reply latency for "hello" remains in the same order of magnitude
  as before (no multi-second reasoning stall); effort hint is `low`.
- **AC5** (R6): If the reasoning-capable stream fails mid-turn, the direct path still
  returns an answer via the non-streamed fallback, and the SSE stream terminates cleanly
  with `final` + `done`.
- **AC6** (R7): After a direct-reply turn with reasoning, `GET /sessions/{id}/traces`
  shows non-empty `reasoning_text` for that turn.
- **AC7** (all): `pytest -m unit` passes fully offline; ruff/mypy clean on touched files.

## 8. Test plan

- **Unit (offline, `tests/unit/`)**:
  - Direct path: fake Responses stream emitting reasoning-summary deltas → orchestrator
    yields `reasoning` events with `agent="orchestrator"` and the trace accumulates
    `reasoning_text` (R1, R7).
  - Registered path: mock `invoke_by_reference`'s client; assert `reasoning` kwarg is
    requested and that a 400 mentioning `reasoning` triggers a stripped retry (R2, R3).
  - Non-reasoning deployment: stream with no reasoning events → turn completes with
    answer only (R3, R4).
  - Stream failure on direct path → fallback to `_direct_reply` and clean `final` (R6).
  - Existing inline-path reasoning tests keep passing unchanged.
- **Integration (gated, `tests/integration/`, `RUN_INTEGRATION=1`)**: live round-trip per
  mode (inline, registered-if-provisioned) asserting ≥1 `reasoning` event on a
  reasoning-class deployment.
- **Smoke (`smoke/post_deploy_smoke.py`)**: extend `/chat` check to assert the stream
  contains a `reasoning` event for a specialist-routed prompt; keep the small-talk probe
  asserting a non-empty `final` (reasoning optional per R3, so no hard assertion there).
