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
from typing import Any, Iterator

from . import memory, telemetry
from .classifier import classify
from .foundry_agents import get_registry
from .llm import chat_text
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

    def event(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)

    def step(self, label: str, detail: str = "", **extra: Any) -> None:
        self.reasoning.append({"step": label, "detail": detail, **extra})


def _retrieve_context(specialist: str, query: str) -> tuple[str, list[dict[str, Any]]]:
    """Host-side hybrid pre-retrieval. Returns (prompt_text, hits). Empty if unavailable."""
    preset_name = _RETRIEVAL_FOR.get(specialist)
    if not preset_name:
        return "", []
    try:
        hits = hybrid_search(query, PRESETS[preset_name], top_n=5)
    except Exception as e:  # noqa: BLE001
        logger.info("Retrieval skipped for %s: %s", specialist, e)
        return "", []
    if not hits:
        return "", []
    import json

    text = (
        "\n\n## Grounding candidates (host-retrieved via hybrid vector+BM25)\n"
        "Prefer these; you may refine with your own MongoDB queries.\n"
        f"{json.dumps(hits, default=str)[:6000]}"
    )
    return text, hits


def _augment(query: str, history: str, facts: str, grounding: str) -> str:
    parts = []
    if facts:
        parts.append(f"## Known facts about the user\n{facts}")
    if history:
        parts.append(f"## Recent conversation\n{history}")
    parts.append(f"## Current request\n{query}")
    if grounding:
        parts.append(grounding)
    return "\n\n".join(parts)


def _synthesize(query: str, answers: dict[str, str]) -> str:
    """Merge multiple specialist answers into one coherent reply (port of the synthesizer)."""
    joined = "\n\n".join(f"### {name} said:\n{ans}" for name, ans in answers.items())
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


def _direct_reply(message: str, history: str, facts: str) -> str:
    """Orchestrator answers small talk / capability questions directly (no specialist)."""
    from .config_loader import load_agents

    orch = next((a for a in load_agents().values() if not a.routable), None)
    system = orch.instructions if orch else "You are a helpful career-assistant router."
    ctx = ""
    if facts:
        ctx += f"\n\n## Known facts about the user\n{facts}"
    if history:
        ctx += f"\n\n## Recent conversation\n{history}"
    return chat_text(
        [
            {"role": "system", "content": system + ctx},
            {"role": "user", "content": message},
        ],
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
                if ev.type == "tool_call":
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
        reply = _direct_reply(message, history, facts)
        if trace is not None:
            trace.step("direct reply", "Orchestrator answered directly (no specialist).")
            trace.final = reply
        yield _event("final", text=reply)
        _persist(session_id, user_id, message, reply)
        return

    reg = get_registry()

    # Run the selected specialists concurrently and stream their events as they arrive.
    # A single specialist runs inline (no thread overhead); 2+ fan out over threads so the
    # slower one (career-coach's MCP tool loop) doesn't serialize behind the faster one.
    answers: dict[str, str] = {}
    if len(cls.specialists) == 1:
        specialist = cls.specialists[0]
        for ev in _run_specialist(reg, specialist, message, history, facts, answers, trace):
            yield ev
    else:
        yield from _fan_out_specialists(
            reg, cls.specialists, message, history, facts, answers, trace
        )

    if not answers:
        final = "Sorry — I couldn't produce an answer this time. Please try rephrasing."
    elif len(answers) == 1:
        final = next(iter(answers.values()))
    else:
        yield _event("synthesizing", agents=list(answers.keys()))
        if trace is not None:
            trace.step("synthesize",
                       f"Merged {len(answers)} specialist answers into one reply via the "
                       "synthesizer LLM.", agents=list(answers.keys()))
        final = _synthesize(message, answers)

    if trace is not None:
        trace.final = final
    yield _event("final", text=final)
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
            "tool_calls": trace.tool_calls,
            "retrievals": trace.retrievals,
            "memory": trace.memory,
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
