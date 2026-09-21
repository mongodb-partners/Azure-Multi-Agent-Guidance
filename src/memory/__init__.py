"""Conversation memory facade (specs/001-foundry-short-term-memory).

Layers:
  - short-term : per-session chat history, backend-selected by SHORT_TERM_MEMORY_BACKEND
                 (`mongo` → `chat_messages`, the original; `foundry` → Foundry
                 Conversations). R1: callers see one unchanged API.
  - long-term  : durable user facts in `agent_memory_facts`, LLM-extracted per turn
                 (unchanged, Atlas — R5).
  - traces     : per-turn audit records in `agent_traces` (unchanged, Atlas — R6).
  - sessions   : `chat_sessions` metadata maintained by BOTH backends so `list_sessions`
                 works regardless of the active backend (R4).

Callers (`orchestrator.py`, `agent_host.py`, `app.py`) import this module as before:
    from . import memory
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from pymongo import ASCENDING

from ..llm import chat_json
from ..settings import get_settings
from . import _mongo, _shared

# Public constants (unchanged import surface — also used by tests/integration).
CHAT_COLLECTION = _shared.CHAT_COLLECTION
FACTS_COLLECTION = _shared.FACTS_COLLECTION
TRACES_COLLECTION = _shared.TRACES_COLLECTION
SESSIONS_COLLECTION = _shared.SESSIONS_COLLECTION
MAX_HISTORY = _shared.MAX_HISTORY

# Backwards-compatible seam (live tests use memory._db(); unit tests patch
# src.memory._shared._db, which this alias points at).
_db = _shared._db


def _history_backend():
    """Dispatch short-term history to the configured backend (R3)."""
    if get_settings().short_term_memory_backend == "foundry":
        from . import _foundry  # lazy: azure-ai-projects is an optional (foundry) dep

        return _foundry
    return _mongo


# --------------------------------------------------------------------------- #
# Short-term history (backend-dispatched)
# --------------------------------------------------------------------------- #
def append_message(
    session_id: str, role: str, content: str, agent: str | None = None,
    user_id: str | None = None,
) -> None:
    _history_backend().append_message(session_id, role, content, agent=agent, user_id=user_id)


def get_history(session_id: str, limit: int = MAX_HISTORY) -> list[dict[str, Any]]:
    return _history_backend().get_history(session_id, limit)


def history_as_text(session_id: str, limit: int = MAX_HISTORY) -> str:
    lines = [f"{m['role']}: {m['content']}" for m in get_history(session_id, limit)]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Session listing (backend-agnostic: chat_sessions metadata + trace stats — R4)
# --------------------------------------------------------------------------- #
_EPOCH = dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def list_sessions() -> list[dict[str, Any]]:
    """List sessions with turn counts, last-activity, and aggregate token/cost/latency.

    Response shape is unchanged from the pre-split implementation (AC2). Membership is
    the union of legacy `chat_messages`-derived sessions (pre-`chat_sessions` data and
    the mongo backend) and `chat_sessions` metadata (both backends); trace aggregates
    still come from `agent_traces`.
    """
    db = _shared._db()
    by_id: dict[str, dict[str, Any]] = {}
    for s in db[CHAT_COLLECTION].aggregate([
        {"$group": {"_id": "$session_id", "messages": {"$sum": 1}, "last": {"$max": "$ts"},
                    "first": {"$min": "$ts"}, "user_id": {"$last": "$user_id"}}},
    ]):
        by_id[s["_id"]] = {
            "session_id": s["_id"], "user_id": s.get("user_id"),
            "messages": s.get("messages", 0), "first": s.get("first"), "last": s.get("last"),
        }
    for d in db[SESSIONS_COLLECTION].find({}, {"_id": 0}):
        sid = d["session_id"]
        entry = by_id.get(sid) or {"session_id": sid}
        entry.update({
            "user_id": d.get("user_id") or entry.get("user_id"),
            "messages": max(d.get("messages", 0), entry.get("messages", 0)),
            "first": d.get("first_ts") or entry.get("first"),
            "last": d.get("last_ts") or entry.get("last"),
        })
        by_id[sid] = entry

    trace_stats = {
        d["_id"]: d
        for d in db[TRACES_COLLECTION].aggregate([
            {"$group": {
                "_id": "$session_id",
                "turns": {"$sum": 1},
                "tokens": {"$sum": "$tokens.total"},
                "cost_usd": {"$sum": "$cost_usd"},
                "tool_calls": {"$sum": "$tool_call_count"},
                "avg_latency_ms": {"$avg": "$latency_ms"},
                "last_message": {"$last": "$user_message"},
            }},
        ])
    }
    out = []
    ordered = sorted(
        by_id.values(), key=lambda s: s.get("last") or _EPOCH, reverse=True
    )[:200]
    for s in ordered:
        sid = s["session_id"]
        ts = trace_stats.get(sid, {})
        out.append({
            "session_id": sid,
            "user_id": s.get("user_id"),
            "turns": ts.get("turns", 0) or (s.get("messages", 0) // 2),
            "messages": s.get("messages", 0),
            "last": s["last"].isoformat() if s.get("last") else None,
            "first": s["first"].isoformat() if s.get("first") else None,
            "tokens_total": int(ts.get("tokens", 0) or 0),
            "cost_usd": round(ts.get("cost_usd", 0) or 0, 4),
            "tool_calls": int(ts.get("tool_calls", 0) or 0),
            "avg_latency_ms": round(ts.get("avg_latency_ms", 0) or 0, 1),
            "last_message": ts.get("last_message", ""),
        })
    return out


# --------------------------------------------------------------------------- #
# Agent traces (per-turn, for the Sessions detail view) — unchanged, Atlas (R6)
# --------------------------------------------------------------------------- #
def save_trace(session_id: str, user_id: str, trace: dict[str, Any]) -> None:
    """Persist one turn's full trace (events, reasoning, retrieval, tokens, cost, tools)."""
    doc = {"session_id": session_id, "user_id": user_id, "ts": _shared._now(), **trace}
    _shared._db()[TRACES_COLLECTION].insert_one(doc)


def get_traces(session_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """Return all persisted turn traces for a session, oldest first (JSON-safe)."""
    cur = (
        _shared._db()[TRACES_COLLECTION]
        .find({"session_id": session_id}, {"_id": 0})
        .sort("ts", ASCENDING)
        .limit(limit)
    )
    out = []
    for d in cur:
        ts = d.get("ts")
        if hasattr(ts, "isoformat"):
            d["ts"] = ts.isoformat()
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Long-term facts — unchanged, Atlas (R5)
# --------------------------------------------------------------------------- #
def get_facts(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    cur = (
        _shared._db()[FACTS_COLLECTION]
        .find({"user_id": user_id}, {"_id": 0, "fact": 1, "kind": 1, "ts": 1})
        .sort("ts", ASCENDING)
    )
    return list(cur)[-limit:]


def facts_as_text(user_id: str, limit: int = 20) -> str:
    return "\n".join(f"- {f['fact']}" for f in get_facts(user_id, limit))


def _dedupe_store_facts(user_id: str, facts: list[dict[str, str]]) -> int:
    coll = _shared._db()[FACTS_COLLECTION]
    stored = 0
    for f in facts:
        fact = (f.get("fact") or "").strip()
        if not fact:
            continue
        # Upsert to avoid duplicate facts per user.
        res = coll.update_one(
            {"user_id": user_id, "fact": fact},
            {"$setOnInsert": {"user_id": user_id, "fact": fact,
                              "kind": f.get("kind", "general"), "ts": _shared._now()}},
            upsert=True,
        )
        if res.upserted_id is not None:
            stored += 1
    return stored


def extract_and_store_facts(user_id: str, user_message: str, assistant_message: str) -> int:
    """LLM-extract durable facts about the user from a turn and persist new ones."""
    prompt = (
        "Extract durable facts about the USER worth remembering across sessions "
        "(skills, target role, location, preferences, constraints). "
        "Ignore transient/one-off details. Return JSON: "
        '{"facts": [{"fact": "...", "kind": "skill|goal|location|preference|general"}]}. '
        "Empty list if nothing durable.\n\n"
        f"USER: {user_message}\nASSISTANT: {assistant_message}"
    )
    try:
        data = chat_json(
            [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=300
        )
    except Exception:
        return 0
    facts = data.get("facts") or []
    if not isinstance(facts, list):
        return 0
    return _dedupe_store_facts(user_id, facts)


def ensure_indexes() -> None:
    """Create the supporting indexes for memory collections (idempotent)."""
    db = _shared._db()
    db[CHAT_COLLECTION].create_index([("session_id", ASCENDING), ("ts", ASCENDING)])
    db[FACTS_COLLECTION].create_index([("user_id", ASCENDING), ("fact", ASCENDING)], unique=True)
    db[TRACES_COLLECTION].create_index([("session_id", ASCENDING), ("ts", ASCENDING)])
    db[SESSIONS_COLLECTION].create_index([("session_id", ASCENDING)], unique=True)
    db[SESSIONS_COLLECTION].create_index([("last_ts", ASCENDING)])
