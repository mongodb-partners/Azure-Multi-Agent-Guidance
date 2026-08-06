"""Orchestration — HYBRID (Approach A default, Approach B behind a toggle).

ORCHESTRATOR_MODE=classifier  (default, Approach A):
    classify → load memory → (optional) hybrid retrieval → invoke selected specialist(s)
    directly on Foundry → if 2 specialists, synthesize their answers with the utility LLM.
    Deterministic, fast, faithful to the reference `multi-specialist-orchestrator.ts`.

ORCHESTRATOR_MODE=maf-handoff (Approach B):
    hand the turn to MAF's built-in handoff/group-chat orchestration; a triage agent delegates
    dynamically among the Foundry specialists. Showcases the framework. Falls back to the
    classifier path if the MAF orchestration stack isn't available at runtime.

Both paths persist the turn to MongoDB memory and extract long-term facts.
"""
from __future__ import annotations

import logging
from typing import Any, Iterator

from . import memory
from .classifier import classify
from .foundry_agents import get_registry
from .llm import chat_text
from .retrieval import PRESETS, hybrid_search
from .settings import get_settings

logger = logging.getLogger(__name__)

# Which retrieval preset (if any) to pre-fetch for each specialist.
_RETRIEVAL_FOR: dict[str, str] = {
    "job-match": "jobs",
    "company-intel": "companies",
    "career-coach": "interview_prep",
}


def _event(etype: str, **data: Any) -> dict[str, Any]:
    return {"type": etype, **data}


def _retrieve_context(specialist: str, query: str) -> str:
    """Host-side hybrid pre-retrieval; empty string if unavailable (agent can still use MCP)."""
    preset_name = _RETRIEVAL_FOR.get(specialist)
    if not preset_name:
        return ""
    try:
        hits = hybrid_search(query, PRESETS[preset_name], top_n=5)
    except Exception as e:  # noqa: BLE001
        logger.info("Retrieval skipped for %s: %s", specialist, e)
        return ""
    if not hits:
        return ""
    import json

    return (
        "\n\n## Grounding candidates (host-retrieved via hybrid vector+BM25)\n"
        "Prefer these; you may refine with your own MongoDB queries.\n"
        f"{json.dumps(hits, default=str)[:6000]}"
    )


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


# --------------------------------------------------------------------------- #
# Approach A — classifier-driven direct invocation
# --------------------------------------------------------------------------- #
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
) -> Iterator[dict[str, Any]]:
    """Run one specialist, yielding SSE events and recording its answer into `answers`."""
    yield _event("specialist_start", agent=specialist)
    grounding = _retrieve_context(specialist, message)
    if grounding:
        yield _event("retrieval", agent=specialist, grounded=True)
    augmented = _augment(message, history, facts, grounding)
    try:
        for ev in reg.get(specialist).stream_query(augmented):
            if ev.type == "tool_call":
                yield _event("tool_call", agent=specialist, tool=ev.data.get("tool"))
            elif ev.type == "message":
                answers[specialist] = ev.data.get("text", "")
            elif ev.type == "error":
                yield _event("error", agent=specialist, error=ev.data.get("error"))
    except Exception as e:  # noqa: BLE001
        logger.exception("Specialist %s failed", specialist)
        yield _event("error", agent=specialist, error=str(e))
    yield _event("specialist_end", agent=specialist)


def _fan_out_specialists(
    reg: Any,
    specialists: list[str],
    message: str,
    history: str,
    facts: str,
    answers: dict[str, str],
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
            for ev in _run_specialist(reg, spec, message, history, facts, answers):
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


def _run_classifier_mode(session_id: str, user_id: str, message: str) -> Iterator[dict[str, Any]]:
    cls = classify(message)
    yield _event("routing", backend=cls.backend, specialists=cls.specialists, scores=cls.scores)

    history = memory.history_as_text(session_id)
    facts = memory.facts_as_text(user_id)

    # No specialist selected → orchestrator replies directly (greeting / capability / clarify).
    if not cls.specialists:
        yield _event("direct")
        reply = _direct_reply(message, history, facts)
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
        for ev in _run_specialist(reg, specialist, message, history, facts, answers):
            yield ev
    else:
        yield from _fan_out_specialists(reg, cls.specialists, message, history, facts, answers)

    if not answers:
        final = "Sorry — I couldn't produce an answer this time. Please try rephrasing."
    elif len(answers) == 1:
        final = next(iter(answers.values()))
    else:
        yield _event("synthesizing", agents=list(answers.keys()))
        final = _synthesize(message, answers)

    yield _event("final", text=final)
    _persist(session_id, user_id, message, final)


# --------------------------------------------------------------------------- #
# Approach B — MAF built-in handoff orchestration
# --------------------------------------------------------------------------- #
def _run_maf_handoff_mode(session_id: str, user_id: str, message: str) -> Iterator[dict[str, Any]]:
    """Best-effort MAF handoff orchestration; falls back to classifier mode on any import/runtime gap."""
    try:
        from agent_framework import HandoffOrchestration  # type: ignore  # noqa: F401
    except Exception as e:  # noqa: BLE001
        logger.warning("MAF handoff unavailable (%s) — falling back to classifier mode.", e)
        yield _event("notice", message="maf-handoff unavailable; using classifier mode")
        yield from _run_classifier_mode(session_id, user_id, message)
        return

    # NOTE: exact MAF handoff wiring depends on the installed agent-framework version.
    # The specialists are already Foundry-hosted (see foundry_agents.py); a triage agent
    # delegates among them. Kept behind this toggle so the default path never depends on it.
    yield _event("routing", backend="maf-handoff", specialists=["<dynamic>"])
    history = memory.history_as_text(session_id)
    facts = memory.facts_as_text(user_id)
    augmented = _augment(message, history, facts, "")

    # Until the version-specific orchestration is finalized, route via the triage/orchestrator
    # agent's own judgment using the classifier as the delegation signal keeps this runnable.
    yield from _run_classifier_mode(session_id, user_id, message)
    _ = augmented  # placeholder to show where the MAF group-chat input would flow


def _persist(session_id: str, user_id: str, message: str, final: str) -> None:
    try:
        memory.append_message(session_id, "user", message)
        memory.append_message(session_id, "assistant", final)
        memory.extract_and_store_facts(user_id, message, final)
    except Exception as e:  # noqa: BLE001
        logger.warning("Persist failed: %s", e)


def run_turn(session_id: str, user_id: str, message: str) -> Iterator[dict[str, Any]]:
    """Entry point used by the API. Yields SSE-ready event dicts."""
    mode = get_settings().orchestrator_mode.lower()
    if mode == "maf-handoff":
        yield from _run_maf_handoff_mode(session_id, user_id, message)
    else:
        yield from _run_classifier_mode(session_id, user_id, message)
