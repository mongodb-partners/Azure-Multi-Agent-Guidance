"""Specialist routing — port of the reference `agent-classifier.ts`.

Two backends, selected by CLASSIFIER_BACKEND:
  - `heuristic` : token-overlap scoring of the query against each routable agent's
     keywords + description. Fast, deterministic, no LLM call.
  - `llm`       : a single tool-forced `select_specialist` call.
  - `auto`      : heuristic first; only fall back to the LLM when the top scores are tied
     or all below threshold (the reference's default behavior).

Supports multi-select: when two specialists both clear the selection threshold, both are
returned (the orchestrator then synthesizes their answers).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config_loader import AgentConfig, routable_agents
from .settings import get_settings

_TOKEN_RE = re.compile(r"[a-z0-9]+")
SELECT_THRESHOLD = 0.12          # min normalized score to be selected
MULTI_SELECT_MARGIN = 0.75       # 2nd place must be >= this fraction of 1st to co-select
MAX_SPECIALISTS = 2


@dataclass
class Classification:
    specialists: list[str]
    scores: dict[str, float]
    backend: str
    reason: str = ""


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _singular(tok: str) -> str:
    """Lightweight singularizer so plurals match singular keywords (roles→role, companies→company)."""
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def _match_tokens(query: str) -> set[str]:
    """Query tokens plus their singular forms (additive — only ever adds keyword matches)."""
    toks = _tokens(query)
    return toks | {_singular(t) for t in toks}


def _heuristic_scores(query: str, agents: dict[str, AgentConfig]) -> dict[str, float]:
    q = _tokens(query)
    if not q:
        return {name: 0.0 for name in agents}
    q_match = _match_tokens(query)
    scores: dict[str, float] = {}
    for name, a in agents.items():
        # Singularize keywords so plural/singular duplicates (company + companies) collapse to
        # one concept and don't double-count; multi-word phrases pass through unchanged.
        kw = {_singular(k) for k in a.keywords}
        desc = _tokens(a.description)
        # Keyword hits weigh more than description overlap; plural-insensitive on keywords.
        kw_hits = len(q_match & kw)
        desc_overlap = len(q & desc) / (len(q) or 1)
        scores[name] = kw_hits * 0.6 + desc_overlap * 0.4
    # Normalize to [0,1] for stable thresholds.
    top = max(scores.values(), default=0.0)
    if top > 0:
        scores = {n: s / top for n, s in scores.items()}
    return scores


def _select_from_scores(scores: dict[str, float]) -> list[str]:
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if not ranked or ranked[0][1] < SELECT_THRESHOLD:
        return []
    chosen = [ranked[0][0]]
    top = ranked[0][1]
    for name, s in ranked[1:]:
        if len(chosen) >= MAX_SPECIALISTS:
            break
        if s >= SELECT_THRESHOLD and s >= MULTI_SELECT_MARGIN * top:
            chosen.append(name)
    return chosen


def _llm_select(query: str, agents: dict[str, AgentConfig]) -> list[str]:
    from .llm import chat

    names = list(agents.keys())
    catalog = "\n".join(f"- {n}: {a.description}" for n, a in agents.items())
    tool = {
        "type": "function",
        "function": {
            "name": "select_specialist",
            "description": "Select the 1-2 best specialists to answer the user's request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "specialists": {
                        "type": "array",
                        "items": {"type": "string", "enum": names},
                        "minItems": 1,
                        "maxItems": MAX_SPECIALISTS,
                    },
                    "reason": {"type": "string"},
                },
                "required": ["specialists"],
            },
        },
    }
    msg = chat(
        messages=[
            {"role": "system", "content": f"You route career-assistant requests.\nSpecialists:\n{catalog}"},
            {"role": "user", "content": query},
        ],
        tools=[tool],
        tool_choice={"type": "function", "function": {"name": "select_specialist"}},
        temperature=0.0,
        max_tokens=200,
    )
    import json

    if not msg.tool_calls:
        return []
    args = json.loads(msg.tool_calls[0].function.arguments or "{}")
    picked = [s for s in args.get("specialists", []) if s in agents]
    return picked[:MAX_SPECIALISTS]


def _is_smalltalk(query: str, all_agents: dict[str, AgentConfig]) -> bool:
    """True for greetings / capability questions the orchestrator should answer directly.

    Matches the non-routable orchestrator's keywords, but only when no specialist also
    scores (so "help me find a job" still routes to job-match).
    """
    orchestrators = [a for a in all_agents.values() if not a.routable]
    if not orchestrators:
        return False
    kws: set[str] = set()
    for a in orchestrators:
        kws |= set(a.keywords)
    ql = query.lower().strip()
    q_tokens = _tokens(query)
    # A multi-word phrase match ("what can you do", "who are you") is a strong signal — trust it.
    if any(k in ql for k in kws if " " in k):
        return True
    # A single-token hit (hi / hello / help) is weak: only small talk if no specialist applies.
    if not (q_tokens & {k for k in kws if " " not in k}):
        return False
    routable = routable_agents(all_agents)
    return not _select_from_scores(_heuristic_scores(query, routable))


def classify(query: str, agents: dict[str, AgentConfig] | None = None) -> Classification:
    """Route a query to 1-2 specialists, or return an empty list for direct-reply small talk."""
    from .config_loader import load_agents

    all_agents = agents or load_agents()
    routable = routable_agents(all_agents)
    backend = get_settings().classifier_backend.lower()

    scores = _heuristic_scores(query, routable)

    # Greetings / "what can you do" → orchestrator handles directly (no specialist, no LLM call).
    if _is_smalltalk(query, all_agents):
        return Classification([], scores, f"{backend}:direct", "small talk / capability question")

    heuristic_pick = _select_from_scores(scores)

    if backend == "heuristic":
        return Classification(heuristic_pick, scores, "heuristic")

    if backend == "llm":
        return Classification(_llm_select(query, routable) or heuristic_pick, scores, "llm")

    # auto: heuristic unless ambiguous (no clear pick, or top two effectively tied)
    ranked = sorted(scores.values(), reverse=True)
    ambiguous = (
        not heuristic_pick
        or (len(ranked) >= 2 and ranked[0] > 0 and ranked[1] >= 0.9 * ranked[0])
    )
    if ambiguous:
        return Classification(_llm_select(query, routable) or heuristic_pick, scores, "auto:llm")
    return Classification(heuristic_pick, scores, "auto:heuristic")


if __name__ == "__main__":
    for q in [
        "find remote python jobs that match my resume",
        "how do I move from data analyst to data scientist?",
        "what's the culture like at MongoDB?",
        "find jobs matching my CV and tell me about those companies",
        "hello",
    ]:
        c = classify(q)
        print(f"{c.backend:16s} {c.specialists}  <-  {q!r}")
