"""Orchestration — classifier-driven routing over Azure AI Foundry Agent Service.

classify → load memory → (optional) hybrid retrieval → invoke selected specialist(s) on the
Foundry Agent Service → if 2 specialists, synthesize their answers with the utility LLM.
Deterministic and fast. The turn is persisted to MongoDB memory and long-term facts are
extracted.

Each specialist is a persistent Foundry prompt agent (see `foundry_agents.py` /
`foundry_registry.py`); the Foundry runtime executes the agent and its MongoDB MCP tool calls.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterator

from . import memory, telemetry
from .classifier import classify
from .foundry_agents import get_registry
from .llm import chat_text, chat_text_stream_events
from .retrieval import PRESETS, hybrid_search

logger = logging.getLogger(__name__)

# Which retrieval preset (if any) to pre-fetch for each specialist.
_RETRIEVAL_FOR: dict[str, str] = {
    "job-match": "jobs",
    "company-intel": "companies",
    "career-coach": "interview_prep",
}


def _event(etype: str, **data: Any) -> dict[str, Any]:
    return {"type": etype, **data}


class _Trace:
    """Accumulates a full, persistable trace of one turn for the Sessions detail view."""

    def __init__(self, user_message: str) -> None:
        self.user_message = user_message
        self.events: list[dict[str, Any]] = []          # every raw event (for the JSON tab)
        self.reasoning: list[dict[str, Any]] = []        # human-readable reasoning-flow steps
        self.tool_calls: list[dict[str, Any]] = []       # MCP tool calls to MongoDB Atlas
        self.retrievals: list[dict[str, Any]] = []       # host-side hybrid retrieval hits
        self.memory: dict[str, Any] = {}                 # context (history + facts) loaded
        self.routing: dict[str, Any] = {}
        self.specialists: list[str] = []
        self.final: str = ""
        self.reasoning_text: str = ""            # accumulated live reasoning (token deltas)
        self.citations: dict[str, Any] = {}      # cited-vs-supplied grounding ids for this turn

    def event(self, ev: dict[str, Any]) -> None:
        # Accumulate live reasoning into one field instead of storing hundreds of tiny
        # `delta`/`reasoning` events (they'd bloat the persisted trace; the final answer and
        # reasoning summary are captured separately).
        etype = ev.get("type")
        if etype == "reasoning":
            self.reasoning_text += ev.get("text", "")
            return
        if etype == "delta":
            return
        self.events.append(ev)

    def step(self, label: str, detail: str = "", **extra: Any) -> None:
        self.reasoning.append({"step": label, "detail": detail, **extra})


GROUNDING_BUDGET_CHARS = 6000
_CITE_PREFIX = {"jobs": "j", "companies": "c", "interview_prep": "q"}
_CITE_RE = re.compile(r"\[([a-z]{1,2}\d{1,2})\]")


def _relevant(hit: dict[str, Any]) -> bool:
    """Is this candidate actually similar to the query, or just the least-bad row available?

    RRF scores are rank-based — rank 1 of garbage scores identically to rank 1 of gold — so the
    floor has to come from the cosine `_vs_score`. Note Atlas normalises cosine to (1+cos)/2, so
    0.5 means *orthogonal*, not "half relevant"; a floor below ~0.6 filters almost nothing.

    A BM25-only hit has no `_vs_score` and is dropped. `$search` ORs the query terms, so one
    common word ("shift", "senior") is enough to match, and BM25 scores have no query-comparable
    scale to threshold on. Little is lost: this block is only a pre-fetch, and the agent still has
    MongoDB MCP to run its own keyword queries.
    """
    from .retrieval import resolve_min_score

    # specs/002 R7: provider-aware floor; None (uncalibrated provider) = no filtering.
    # ponytail: one global cosine floor; make it per-preset if collections differ in scale.
    floor = resolve_min_score()
    return floor is None or float(hit.get("_vs_score") or 0.0) >= floor


def _grounding_block(preset_name: str, hits: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Serialize hits for the prompt: short cite ids, no internals, whole-document budget.

    Budgeting by document (not by character) matters — slicing the serialized array at a fixed
    offset hands the model a half-written JSON document, which it will happily complete for you.
    """
    import json

    prefix = _CITE_PREFIX.get(preset_name, "d")
    rows: list[dict[str, Any]] = []
    used = 0
    for i, h in enumerate(hits, 1):
        cite = f"{prefix}{i}"
        h["_cite_id"] = cite  # retained on the trace so citations can be verified after the turn
        doc = {"id": cite, **{k: v for k, v in h.items() if not k.startswith("_")}}
        blob = json.dumps(doc, default=str)
        if rows and used + len(blob) > GROUNDING_BUDGET_CHARS:
            break
        rows.append(doc)
        used += len(blob)
    if not rows:
        return "", []
    text = (
        "\n\n## Grounding candidates (host-retrieved from MongoDB via hybrid vector+BM25)\n"
        "These rows are MongoDB data — the grounding contract applies to them unchanged.\n"
        "Cite the `id` of every row you use, inline, like [j1]. If none of them answer the "
        "request, say so and query MongoDB yourself rather than stretching a row to fit.\n"
        + "\n".join(json.dumps(d, default=str) for d in rows)
    )
    return text, hits[: len(rows)]


def _retrieve_context(specialist: str, query: str) -> tuple[str, list[dict[str, Any]]]:
    """Host-side hybrid pre-retrieval. Returns (prompt_text, hits). Empty if unavailable.

    Returning nothing when nothing clears the relevance floor is the point: an empty grounding
    block lets the abstain rule in GROUNDING_RULES fire, instead of handing the agent five
    irrelevant rows under a header that tells it to prefer them.
    """
    preset_name = _RETRIEVAL_FOR.get(specialist)
    if not preset_name:
        return "", []
    try:
        hits = hybrid_search(query, PRESETS[preset_name], top_n=5)
    except Exception as e:  # noqa: BLE001
        logger.info("Retrieval skipped for %s: %s", specialist, e)
        return "", []
    hits = [h for h in hits if _relevant(h)]
    if not hits:
        return "", []
    return _grounding_block(preset_name, hits)


def _check_citations(final: str, hits: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare the ids cited in the answer against the ids actually supplied to the agent."""
    supplied = {h["_cite_id"] for h in hits if h.get("_cite_id")}
    cited = set(_CITE_RE.findall(final))
    invalid = cited - supplied
    return {
        "supplied": sorted(supplied),
        "cited": sorted(cited),
        "invalid": sorted(invalid),
        "grounded": bool(cited) and not invalid,
    }


def _augment(query: str, history: str, facts: str, grounding: str) -> str:
    """Assemble the turn context. Section order is fixed so the same state builds the same prompt.

    The request goes LAST, directly after its evidence — and prior assistant turns are marked as
    context rather than evidence, so the model does not re-ground on its own earlier output.
    """
    parts = []
    if facts:
        parts.append(f"## Known facts about the user\n{facts}")
    if history:
        parts.append(
            "## Recent conversation (context only — not evidence; verify claims against MongoDB)\n"
            + history
        )
    if grounding:
        parts.append(grounding)
    parts.append(f"## Current request\n{query}")
    return "\n\n".join(parts)


def _synthesize(query: str, answers: dict[str, str]) -> str:
    """Merge multiple specialist answers into one coherent reply (port of the synthesizer)."""
    # Sorted, not insertion-ordered: under fan-out the dict order is whichever thread finished
    # first, which would build a different prompt from identical answers.
    joined = "\n\n".join(f"### {name} said:\n{ans}" for name, ans in sorted(answers.items()))
    return chat_text(
        [
            {
                "role": "system",
                "content": (
                    "You merge specialist answers into ONE cohesive, non-repetitive reply for the "
                    "user. Preserve concrete details (job titles, companies, skills). Keep it tight."
                ),
            },
            {"role": "user", "content": f"User asked: {query}\n\n{joined}"},
        ],
        temperature=0.3,
        max_tokens=2000,
    )


def _direct_reply_messages(message: str, history: str, facts: str) -> list[dict[str, str]]:
    """Build the chat messages for a direct (no-specialist) orchestrator reply."""
    from .config_loader import load_agents

    orch = next((a for a in load_agents().values() if not a.routable), None)
    system = orch.instructions if orch else "You are a helpful career-assistant router."
    ctx = ""
    if facts:
        ctx += f"\n\n## Known facts about the user\n{facts}"
    if history:
        ctx += f"\n\n## Recent conversation\n{history}"
    return [
        {"role": "system", "content": system + ctx},
        {"role": "user", "content": message},
    ]


def _direct_reply(message: str, history: str, facts: str) -> str:
    """Orchestrator answers small talk / capability questions directly (no specialist)."""
    return chat_text(
        _direct_reply_messages(message, history, facts),
        temperature=0.4,
        max_tokens=1200,
    )


def _direct_reply_stream(message: str, history: str, facts: str) -> Iterator[tuple[str, str]]:
    """Streaming variant: yield ``(kind, text)`` chunks for live rendering (specs/004 R1).

    ``kind`` is ``"reasoning"`` (live reasoning summary) or ``"delta"`` (answer text)."""
    yield from chat_text_stream_events(
        _direct_reply_messages(message, history, facts),
        temperature=0.4,
        max_tokens=1200,
    )


def _run_specialist(
    reg: Any,
    specialist: str,
    message: str,
    history: str,
    facts: str,
    answers: dict[str, str],
    trace: "_Trace | None" = None,
) -> Iterator[dict[str, Any]]:
    """Run one specialist, yielding SSE events and recording its answer into `answers`."""
    yield _event("specialist_start", agent=specialist)
    telemetry.record_specialist(specialist)
    if trace is not None:
        trace.step("invoke specialist", f"Routing to **{specialist}** on the Foundry Agent Service.",
                   agent=specialist)
    with telemetry.span(f"specialist.{specialist}", **{"agent": specialist}) as sp:
        grounding, hits = _retrieve_context(specialist, message)
        if grounding:
            telemetry.add_event(sp, "retrieval", agent=specialist)
            if trace is not None:
                trace.retrievals.append({"agent": specialist, "hits": hits})
                trace.step("hybrid retrieval",
                           f"Pre-fetched {len(hits)} grounding candidate(s) from MongoDB Atlas "
                           f"(vector + BM25) for **{specialist}**.", agent=specialist)
            yield _event("retrieval", agent=specialist, grounded=True, hits=len(hits))
        augmented = _augment(message, history, facts, grounding)
        try:
            for ev in reg.get(specialist).stream_query(augmented):
                if ev.type == "status":
                    # Surface which runtime served this specialist ("registered" = Foundry
                    # managed agent runtime, else inline) so the UI/trace can show it.
                    source = ev.data.get("source")
                    if source:
                        if trace is not None:
                            trace.step("runtime",
                                       f"**{specialist}** served by the **{source}** runtime"
                                       + (" (Foundry managed agent)" if source == "registered"
                                          else " (in-process)") + ".",
                                       agent=specialist, source=source)
                        yield _event("specialist_source", agent=specialist, source=source)
                elif ev.type == "tool_call":
                    tool = ev.data.get("tool")
                    telemetry.record_tool_call(tool, agent=specialist)
                    telemetry.add_event(sp, "tool_call", tool=tool)
                    if trace is not None:
                        trace.tool_calls.append({"agent": specialist, "tool": tool,
                                                 "id": ev.data.get("id")})
                        trace.step("MCP tool call",
                                   f"**{specialist}** called MongoDB MCP tool `{tool}`.",
                                   agent=specialist, tool=tool)
                    yield _event("tool_call", agent=specialist, tool=tool)
                elif ev.type == "reasoning":
                    # Live reasoning token chunk — forward for progressive rendering.
                    yield _event("reasoning", agent=specialist, text=ev.data.get("text", ""))
                elif ev.type == "delta":
                    # Live answer token chunk — forward for progressive rendering.
                    yield _event("delta", agent=specialist, text=ev.data.get("text", ""))
                elif ev.type == "message":
                    answers[specialist] = ev.data.get("text", "")
                    if trace is not None:
                        trace.step("specialist answered",
                                   f"**{specialist}** produced an answer "
                                   f"({len(ev.data.get('text','')):,} chars).", agent=specialist)
                elif ev.type == "error":
                    telemetry.record_error(specialist)
                    if trace is not None:
                        trace.step("specialist error", str(ev.data.get("error")), agent=specialist)
                    yield _event("error", agent=specialist, error=ev.data.get("error"))
        except Exception as e:  # noqa: BLE001
            logger.exception("Specialist %s failed", specialist)
            telemetry.record_exception(sp, e)
            telemetry.record_error(specialist)
            if trace is not None:
                trace.step("specialist error", str(e), agent=specialist)
            yield _event("error", agent=specialist, error=str(e))
    yield _event("specialist_end", agent=specialist)


def _fan_out_specialists(
    reg: Any,
    specialists: list[str],
    message: str,
    history: str,
    facts: str,
    answers: dict[str, str],
    trace: "_Trace | None" = None,
) -> Iterator[dict[str, Any]]:
    """Run specialists concurrently in threads; drain their events into one ordered stream.

    Each specialist has its own `answers` slot, so the shared dict is written key-disjointly
    across threads (no lock needed). Events are multiplexed through a queue and yielded on the
    main thread as they land, so the UI still sees live tool_call / specialist progress.
    """
    import queue
    import threading

    q: "queue.Queue[dict[str, Any] | None]" = queue.Queue()

    def worker(spec: str) -> None:
        try:
            for ev in _run_specialist(reg, spec, message, history, facts, answers, trace):
                q.put(ev)
        finally:
            q.put(None)  # one sentinel per worker signals its completion

    threads = [threading.Thread(target=worker, args=(s,), daemon=True) for s in specialists]
    for t in threads:
        t.start()

    remaining = len(threads)
    while remaining:
        ev = q.get()
        if ev is None:
            remaining -= 1
            continue
        yield ev
    for t in threads:
        t.join()


def _run_turn(
    session_id: str, user_id: str, message: str, trace: "_Trace | None" = None
) -> Iterator[dict[str, Any]]:
    cls = classify(message)
    if trace is not None:
        trace.routing = {"backend": cls.backend, "specialists": list(cls.specialists),
                         "scores": cls.scores}
        trace.specialists = list(cls.specialists)
        trace.step("classify / route",
                   ("No specialist matched — the orchestrator will answer directly."
                    if not cls.specialists
                    else f"Selected specialist(s): **{', '.join(cls.specialists)}** "
                         f"(backend: `{cls.backend}`)."),
                   scores=cls.scores)
    yield _event("routing", backend=cls.backend, specialists=cls.specialists, scores=cls.scores)

    history = memory.history_as_text(session_id)
    facts = memory.facts_as_text(user_id)
    if trace is not None:
        trace.memory = {"history": history, "facts": facts}
        trace.step("load memory (MongoDB Atlas)",
                   f"Loaded {len(history.splitlines())} history line(s) from `chat_messages` and "
                   f"{len(facts.splitlines()) if facts else 0} long-term fact(s) from "
                   "`agent_memory_facts`.")

    # No specialist selected → orchestrator replies directly (greeting / capability / clarify).
    if not cls.specialists:
        yield _event("direct")
        parts: list[str] = []
        try:
            for kind, piece in _direct_reply_stream(message, history, facts):
                if not piece:
                    continue
                if kind == "reasoning":
                    # Live reasoning summary chunk (specs/004 R1) — forward like specialists.
                    yield _event("reasoning", agent="orchestrator", text=piece)
                else:
                    parts.append(piece)
                    yield _event("delta", agent="orchestrator", text=piece)
        except Exception as e:  # noqa: BLE001 — fall back to a single non-streamed reply
            logger.warning("Direct reply streaming failed (%s); falling back.", e)
            parts = []
        reply = "".join(parts) or _direct_reply(message, history, facts)
        if trace is not None:
            trace.step("direct reply", "Orchestrator answered directly (no specialist).")
            trace.final = reply
        yield _event("final", text=reply, streamed=bool(parts))
        _persist(session_id, user_id, message, reply)
        return

    reg = get_registry()

    # Run the selected specialists concurrently and stream their events as they arrive.
    # A single specialist runs inline (no thread overhead); 2+ fan out over threads so the
    # slower one (career-coach's MCP tool loop) doesn't serialize behind the faster one.
    answers: dict[str, str] = {}
    # `streamed` is True when the final answer text already reached the client live via `delta`
    # events (single specialist). For synthesis (2 specialists) the merged reply differs from the
    # streamed deltas, so it is NOT considered pre-streamed and the UI must render it fresh.
    streamed = False
    if len(cls.specialists) == 1:
        specialist = cls.specialists[0]
        for ev in _run_specialist(reg, specialist, message, history, facts, answers, trace):
            yield ev
        streamed = bool(answers)
    else:
        yield from _fan_out_specialists(
            reg, cls.specialists, message, history, facts, answers, trace
        )

    if not answers:
        final = "Sorry — I couldn't produce an answer this time. Please try rephrasing."
        streamed = False
    elif len(answers) == 1:
        final = next(iter(answers.values()))
    else:
        yield _event("synthesizing", agents=list(answers.keys()))
        if trace is not None:
            trace.step("synthesize",
                       f"Merged {len(answers)} specialist answers into one reply via the "
                       "synthesizer LLM.", agents=list(answers.keys()))
        final = _synthesize(message, answers)
        streamed = False

    if trace is not None:
        trace.final = final
        # Verify the answer only cites rows it was actually given (invalid ids = invented source).
        supplied = [h for r in trace.retrievals for h in r.get("hits", [])]
        trace.citations = _check_citations(final, supplied)
        if trace.citations["invalid"]:
            logger.warning(
                "Ungrounded citation(s) %s in session %s", trace.citations["invalid"], session_id
            )
            trace.step("citation check",
                       f"Answer cited unknown grounding id(s): "
                       f"{', '.join(trace.citations['invalid'])}.")
    yield _event("final", text=final, streamed=streamed)
    _persist(session_id, user_id, message, final)


def _persist(session_id: str, user_id: str, message: str, final: str) -> None:
    try:
        memory.append_message(session_id, "user", message, user_id=user_id)
        memory.append_message(session_id, "assistant", final, user_id=user_id)
        memory.extract_and_store_facts(user_id, message, final)
    except Exception as e:  # noqa: BLE001
        logger.warning("Persist failed: %s", e)


def _estimate_cost(tokens: dict[str, Any]) -> float:
    from .settings import get_settings

    s = get_settings()
    return round(
        (tokens.get("prompt", 0) / 1_000_000.0) * s.price_input_per_1m
        + (tokens.get("completion", 0) / 1_000_000.0) * s.price_output_per_1m,
        6,
    )


def _save_trace(session_id: str, user_id: str, trace: "_Trace", record: dict[str, Any]) -> None:
    """Persist the collected turn trace to MongoDB for the Sessions detail view."""
    try:
        tokens = record.get("tokens", {}) or {}
        memory.save_trace(session_id, user_id, {
            "user_message": trace.user_message,
            "final": trace.final,
            "routing": trace.routing,
            "specialists": trace.specialists,
            "reasoning": trace.reasoning,
            "reasoning_text": trace.reasoning_text,
            "tool_calls": trace.tool_calls,
            "retrievals": trace.retrievals,
            "memory": trace.memory,
            "citations": trace.citations,
            "events": trace.events,
            "tokens": tokens,
            "cost_usd": _estimate_cost(tokens),
            "latency_ms": record.get("latency_ms", 0),
            "llm_calls": record.get("llm_calls", 0),
            # Derive from the collected list — reliable across fan-out worker threads (the
            # thread-local telemetry counter only sees the thread it ran on).
            "tool_call_count": len(trace.tool_calls),
            "error": record.get("error", False),
        })
    except Exception as e:  # noqa: BLE001
        logger.warning("Trace persist failed: %s", e)


def run_turn(session_id: str, user_id: str, message: str) -> Iterator[dict[str, Any]]:
    """Entry point used by the API. Yields SSE-ready event dicts."""
    import time

    telemetry.record_turn()
    start = time.perf_counter()
    errored = False
    trace = _Trace(message)
    with telemetry.span(
        "chat.turn", session_id=session_id, user_id=user_id
    ) as sp:
        try:
            for ev in _run_turn(session_id, user_id, message, trace):
                trace.event(ev)
                yield ev
        except Exception as e:  # noqa: BLE001
            errored = True
            telemetry.record_exception(sp, e)
            telemetry.record_error()
            raise
        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            record = telemetry.finish_turn(latency_ms, error=errored)
            usage_ev = _event(
                "usage",
                latency_ms=record["latency_ms"],
                tokens=record["tokens"],
                specialists=record["specialists"],
            )
            trace.event(usage_ev)
            _save_trace(session_id, user_id, trace, record)
            # Surface per-turn usage to the UI (tokens + latency + specialists).
            yield usage_ev
