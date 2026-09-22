# Spec 005: Developer trace — raw SSE event JSON in the chat UI

- **Status:** abandoned (2026-09-21 — withdrawn by author: the Sessions page already has a
  "🧾 Raw events (JSON)" tab (`ui/pages/2_Sessions.py`), which covers the need; a second
  raw-events view in the chat page isn't wanted)
- **Author:** Anuj Panchal (drafted with AI assistant)
- **Created:** 2026-09-21
- **Approved:** —
- **Implemented in:** —

## 1. Summary

Add a per-message **"🛠️ Developer trace"** section to the main chat page that shows the raw
JSON of every SSE event the API streamed to fulfil that request — verbatim, pretty-printed,
copyable, and persisted across UI reruns. Modeled on the reference project's developer trace
(`mongodb-partners/config-driven-agents-agentcore-atlas`, whose Raw-events tab renders the
streamed event array as syntax-highlighted JSON). Backend unchanged: the events already flow;
only the UI surfaces them.

## 2. Context & current state

Today, a developer using the chat UI (`ui/streamlit_app.py`) cannot see the raw event stream:

- The "🔬 Agent trace" expander (`render_trace`, `ui/streamlit_app.py:85`) renders a fixed set
  of **humanized one-liners** (routing, retrieval, specialist start/source, tool_call,
  synthesizing, error). `reasoning`, `delta`, `final`, and `usage` events are consumed by
  separate branches (`streamlit_app.py:189-205`) and never recorded — so even the humanized
  trace is incomplete relative to what crossed the wire.
- Events are discarded after the turn: `st.session_state.messages` stores only
  `{"role", "content"}`, so any Streamlit rerun (sidebar interaction, widget change) wipes
  the trace for prior turns.
- The **Sessions page** (`ui/pages/2_Sessions.py:171-174`) already has a
  "🧾 Raw events (JSON)" tab, but it renders the **persisted** trace from MongoDB, after the
  fact, in a different page — and `_Trace.event` deliberately **drops** `delta`/`reasoning`
  chunks (`src/orchestrator.py:57-63`), so even there the token-level stream is absent.

Reference behavior to mirror (`config-driven-agents-agentcore-atlas` `ui/app.py`): a
collapsed per-message expander whose raw tab is
`st.code(json.dumps(events, indent=2), language="json")` — the full event array, exactly as
streamed, with syntax highlighting and Streamlit's built-in copy button. The backend emits
event dicts; the UI renders them verbatim and does not reconstruct anything.

## 3. Requirements

- **R1** — Verbatim capture. The chat page must record **every** SSE event received for a
  turn — `routing`, `direct`, `retrieval`, `specialist_start`, `specialist_source`,
  `tool_call`, `reasoning`, `delta`, `synthesizing`, `error`, `final`, `usage`, `done` —
  as `{event, data}` pairs, exactly as parsed, without lossy transformation.
- **R2** — Delta coalescing for readability. Consecutive `delta` (or `reasoning`) chunks
  from the same agent may be merged into a single recorded event with concatenated text and
  a `"coalesced_chunks": N` marker, so the JSON stays readable (hundreds of 1-token events
  help nobody). All other event types must remain verbatim and in arrival order.
- **R3** — Presentation. Each assistant message gets a collapsed expander
  ("🛠️ Developer trace · N events") rendered as
  `st.code(json.dumps(events, indent=2, default=str), language="json")` — pretty-printed,
  syntax-highlighted, with the built-in copy button.
- **R4** — Persistence across reruns. The recorded events must be stored on the assistant
  message in `st.session_state` so the developer trace (like the answer text) survives
  Streamlit reruns.
- **R5** — No repaint storm. During streaming, the raw-JSON view must not re-render on every
  token: update it at most ~2 times/second (or only on non-token events), with one final
  authoritative render at `done`. (Reference project's `ponytail` note: Streamlit re-sends
  the whole string per repaint.)
- **R6** — Backend untouched. No changes to the API, SSE event schema, orchestrator, or
  persisted trace shape. This is a UI-only change.
- **R7** — Failure-safe. If the stream errors mid-turn, the developer trace must still show
  the events received up to the failure (plus the error event), not vanish.

## 4. Non-goals

- No changes to the Sessions page (its persisted-trace raw tab already exists and stays).
- No new tabs/metrics in the developer trace (Flow/Tokens/Memory breakdowns live on the
  Sessions page already); this spec is raw JSON only.
- No changes to what the backend emits or persists (`_Trace` still drops token deltas from
  the *persisted* trace — that is intentional and unchanged).
- No filtering/redaction layer for secrets (events carry no credentials today).
- No live "status ticker" of the last N steps — the existing 🔬 Agent trace expander covers
  humanized progress.

## 5. Contracts affected

| Contract | Today | After |
|---|---|---|
| `ui/streamlit_app.py` chat loop | consumes events; records none | accumulates `(event, data)` pairs per turn (R1), coalesces token chunks (R2), stores them on the assistant message (R4) |
| `ui/streamlit_app.py` assistant message | `{"role", "content"}` in session state | `{"role", "content", "trace_events": [...]}`; replay loop re-renders the expander for old messages (R3, R4) |
| New pure helper (e.g. `ui/trace_events.py::record_event` / `coalesce`) | — | unit-testable event accumulation/coalescing logic, no Streamlit dependency (R2, R7) |
| SSE event schema (`src/app.py`, `src/orchestrator.py`) | unchanged | unchanged (R6) |
| Persisted trace (`agent_traces` via `_save_trace`) | unchanged | unchanged (R6) |

## 6. Open questions

| # | Question | Owner | Resolution |
|---|----------|-------|-----------|
| Q1 | Should the expander also appear on turns that errored before any event arrived (empty trace)? | Implementer | Default: yes — render with a caption "no events received" so a silent failure is visibly distinguishable from a healthy turn. |
| Q2 | Live-updating raw JSON during streaming, or render once at completion? | Implementer | Default: throttled live updates per R5 (matches the "live" feel of the rest of the page); drop to completion-only if it proves janky. |

## 7. Acceptance criteria

- **AC1** (R1, R2, R3): After any chat turn, the assistant message shows a collapsed
  "🛠️ Developer trace" expander whose content is a valid JSON array containing — in arrival
  order — the turn's `routing` event, any `tool_call` events, and the `final` and `usage`
  events, each with its full original payload. Token chunks appear coalesced with
  `coalesced_chunks` markers rather than as hundreds of single-token entries.
- **AC2** (R4): Triggering a Streamlit rerun (e.g. clicking "Check API health" in the
  sidebar) leaves prior messages' developer traces intact and identical.
- **AC3** (R5): During a long specialist turn, the page remains responsive; the raw-JSON
  pane does not re-render more than ~2×/second while token chunks stream.
- **AC4** (R7): With the API unreachable or erroring mid-stream, the expander shows every
  event received before the failure and the UI displays the error (no lost context).
- **AC5** (R2, offline): `pytest -m unit` covers the coalescing/accumulation helper:
  ordering preserved, non-token events verbatim, consecutive same-agent token chunks merged
  with counts, empty-stream edge case. All offline (no Streamlit runtime required).
- **AC6** (R6): No diff in `src/` — API behavior and SSE schema byte-identical.

## 8. Test plan

- **Unit (offline, `tests/unit/`)**: the pure accumulation/coalescing helper (AC5) —
  sequence ordering, merge rules for `delta`/`reasoning` (same agent merges; agent switch
  or non-token event breaks the run), `final`/`usage` verbatim, empty input, error tail.
- **Manual/UI verification** (Streamlit pages aren't unit-tested in this repo): run the UI
  against the live API, run one direct turn and one specialist turn, verify AC1–AC4,
  including a sidebar-triggered rerun (AC2) and a killed-API mid-turn (AC4).
- **Smoke**: unchanged (`smoke/post_deploy_smoke.py` asserts the SSE contract, which this
  spec does not modify).
