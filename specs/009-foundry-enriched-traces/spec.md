# Spec 009: Foundry-enriched agent traces

- **Status:** draft
- **Author:** anuj.panchal
- **Created:** 2026-09-23
- **Approved:** —
- **Implemented in:** —

## 1. Summary

Keep the custom `agent_traces` collection as the system of record, but enrich each turn's
document with authoritative runtime detail fetched **from Azure AI Foundry** after the turn:
`responses.retrieve(response_id)` per specialist response. This adds the model runtime's own
view (reasoning summary items, MCP call items with arguments and server-side outputs, token
usage) to the trace without giving up host-side data (routing scores, retrieval grounding,
synthesis, raw SSE events) that Foundry never sees. The Sessions UI gains a "Foundry runtime"
view per specialist.

## 2. Context & current state

- `agent_traces` is custom-built by `src/orchestrator.py::_Trace` and persisted by
  `_save_trace()`; content: routing, reasoning steps, tool calls (names + args since spec
  008), retrievals, tokens, cost, raw events.
- Specialist streams already surface `response_id` on the final `message` Event
  (`src/foundry_agents.py:334`), for both registered and inline paths (both go through the
  Responses API). The orchestrator currently drops it.
- Gap: the trace's tool/reasoning data is what *streamed past us*; Foundry's stored response
  is the authoritative record (final tool-call arguments, server-side outputs, reasoning
  summary items, usage). When streams truncate or error mid-turn (e.g. defect D10), the
  Foundry-side record may be the only complete one.
- Non-goal analysis done in chat: fully replacing `agent_traces` with App Insights/Foundry
  loses host-side fidelity and adds minutes of ingestion latency — rejected.

## 3. Requirements

- **R1** — `_Trace` must record each specialist's `response_id` (from the `message` Event)
  keyed by specialist name.
- **R2** — After the turn's stream completes (before `_save_trace`), for each recorded
  `response_id`, fetch `responses.retrieve(response_id)` via the existing OpenAI-compatible
  client and attach a `foundry` sub-document per specialist containing:
  `response_id`, `status`, `model`, `usage` (as returned), `items` summarized as
  `{type, name?, arguments?, output? (truncated to 4 KB), reasoning summary text?}`.
- **R3** — Enrichment must be **best-effort and non-blocking**: any error (network, 404 for
  expired responses, SDK absence, inline-mode mock) is caught, logged, and recorded as
  `foundry: {"error": "<msg>"}` or omitted — the chat turn and trace persistence must never
  fail because of it. It must not delay the `final`/`done` SSE events (run after the stream
  ends; the UI already renders traces on demand).
- **R4** — Toggle: `TRACE_FOUNDRY_ENRICHMENT` env var in `src/settings.py` +
  `.env.sample`, default `true`, offline-safe (unit tests patch/mock; no live dependency in
  `pytest -m unit`).
- **R5** — Inline path included: inline specialists also go through the Responses API, so
  their `response_id`s are retrievable the same way. Hosted agents (Option 3) are out of
  scope for direct retrieval (the orchestrator container owns those response ids; its own
  trace path is unchanged).
- **R6** — Sessions UI (`ui/pages/2_Sessions.py`): the per-turn detail gains a collapsed
  "Foundry runtime (authoritative)" expander per specialist showing status/model/usage and
  the item list, rendered only when the `foundry` field exists. Older traces without the
  field render exactly as today.
- **R7** — Docs (spec 007 rule): `docs/service-map.md` §3 and README trace description
  updated to mention the enrichment field.

## 4. Non-goals

- No change to what the custom `_Trace` collects today (routing, grounding, raw events stay).
- No App Insights / KQL integration (rejected — see §2).
- No backfill of historical traces.
- No hosted-agent (cross-container) trace merging.
- No new dependencies (uses the already-installed `openai` SDK).

## 5. Contracts affected

| Contract | Today | After |
|---|---|---|
| `agent_traces` document | custom fields only | + `foundry_responses: {<specialist>: {...}}` per turn (R2) |
| `_Trace` | drops `response_id` | records `response_ids: dict[str, str]` (R1) |
| `src/orchestrator.py::_save_trace` path | persists trace as-is | calls enrichment first, best-effort (R2, R3) |
| `src/settings.py` / `.env.sample` | — | + `TRACE_FOUNDRY_ENRICHMENT` (R4) |
| Sessions UI turn detail | 5 tabs | + per-specialist "Foundry runtime" expander (R6) |

## 6. Open questions

| # | Question | Owner | Resolution |
|---|----------|-------|-----------|
| Q1 | Foundry response retention window — how long does `responses.retrieve` work after a turn? (Affects whether a "re-enrich on view" fallback is worth it.) | human | defer — enrichment happens immediately post-turn, so retention only matters if that call itself fails |
| Q2 | Store full item outputs or truncate? | — | truncate at 4 KB per item (R2); raw events already hold the streamed view |

## 7. Acceptance criteria

- **AC1** (R1, R2): a mocked specialist turn (unit) yields an `agent_traces` record with
  `foundry_responses.<specialist>.items` containing the mcp_call arguments from the fake
  retrieved response.
- **AC2** (R3): unit test where `responses.retrieve` raises → turn completes, trace saved,
  `foundry_responses` absent or error-marked; `pytest -m unit` green offline.
- **AC3** (R4): with `TRACE_FOUNDRY_ENRICHMENT=false`, no retrieve call is made (asserted in
  unit test).
- **AC4** (R6): Sessions page renders a trace with `foundry_responses` (new expander) and an
  old trace without it (no crash) — manual check.
- **AC5** (R2): live turn against the deployed stack shows Foundry usage matching the
  streamed token totals (manual, documented in PR).

## 8. Test plan

- `tests/unit/test_orchestrator.py`: extend the fake specialist to emit `response_id`; fake
  the retrieve client; assert enrichment content (AC1), error tolerance (AC2), toggle (AC3).
- No integration/e2e changes; smoke unchanged.
