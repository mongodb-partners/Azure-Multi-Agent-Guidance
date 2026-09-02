"""Conversation memory backed by MongoDB.

Two layers, mirroring the reference's AgentCore Memory:
  - short-term : per-session chat turns in `chat_messages` (session-isolated, ordered).
  - long-term  : durable user facts in `agent_memory_facts`, extracted from turns by an LLM.

The host orchestrator uses plain **pymongo** (synchronous, simple to weave into the request
flow) so the Foundry-hosted specialists and the orchestrator share one memory store.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from pymongo import ASCENDING, MongoClient

from .llm import chat_json
from .settings import get_settings

CHAT_COLLECTION = "chat_messages"
FACTS_COLLECTION = "agent_memory_facts"
TRACES_COLLECTION = "agent_traces"
MAX_HISTORY = 20

_client: MongoClient | None = None


def _db():
    global _client
    if _client is None:
        s = get_settings()
        s.require("mongodb_uri")
        _client = MongoClient(s.mongodb_uri)
    return _client[get_settings().mongodb_db]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------- #
# Short-term history
# --------------------------------------------------------------------------- #
def append_message(
    session_id: str, role: str, content: str, agent: str | None = None,
    user_id: str | None = None,
) -> None:
    _db()[CHAT_COLLECTION].insert_one(
        {
            "session_id": session_id,
            "user_id": user_id,
            "role": role,
            "content": content,
            "agent": agent,
            "ts": _now(),
        }
    )


def get_history(session_id: str, limit: int = MAX_HISTORY) -> list[dict[str, Any]]:
    cur = (
        _db()[CHAT_COLLECTION]
        .find({"session_id": session_id}, {"_id": 0, "role": 1, "content": 1, "agent": 1, "ts": 1})
        .sort("ts", ASCENDING)
    )
    msgs = list(cur)
    return msgs[-limit:]


def history_as_text(session_id: str, limit: int = MAX_HISTORY) -> str:
    lines = [f"{m['role']}: {m['content']}" for m in get_history(session_id, limit)]
    return "\n".join(lines)


def list_sessions() -> list[dict[str, Any]]:
    """List sessions with turn counts, last-activity, and aggregate token/cost/latency from traces."""
    pipeline = [
        {"$group": {"_id": "$session_id", "messages": {"$sum": 1}, "last": {"$max": "$ts"},
                    "first": {"$min": "$ts"}, "user_id": {"$last": "$user_id"}}},
        {"$sort": {"last": -1}},
        {"$limit": 200},
    ]
    sessions = list(_db()[CHAT_COLLECTION].aggregate(pipeline))

    # Aggregate trace stats per session (turns, tokens, cost, latency, tool calls).
    trace_stats = {
        d["_id"]: d
        for d in _db()[TRACES_COLLECTION].aggregate([
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
    for s in sessions:
        sid = s["_id"]
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
# Agent traces (per-turn, for the Sessions detail view)
# --------------------------------------------------------------------------- #
def save_trace(session_id: str, user_id: str, trace: dict[str, Any]) -> None:
    """Persist one turn's full trace (events, reasoning, retrieval, tokens, cost, tools)."""
    doc = {"session_id": session_id, "user_id": user_id, "ts": _now(), **trace}
    _db()[TRACES_COLLECTION].insert_one(doc)


def get_traces(session_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """Return all persisted turn traces for a session, oldest first (JSON-safe)."""
    cur = (
        _db()[TRACES_COLLECTION]
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
# Long-term facts
# --------------------------------------------------------------------------- #
def get_facts(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    cur = (
        _db()[FACTS_COLLECTION]
        .find({"user_id": user_id}, {"_id": 0, "fact": 1, "kind": 1, "ts": 1})
        .sort("ts", ASCENDING)
    )
    return list(cur)[-limit:]


def facts_as_text(user_id: str, limit: int = 20) -> str:
    return "\n".join(f"- {f['fact']}" for f in get_facts(user_id, limit))


def _dedupe_store_facts(user_id: str, facts: list[dict[str, str]]) -> int:
    coll = _db()[FACTS_COLLECTION]
    stored = 0
    for f in facts:
        fact = (f.get("fact") or "").strip()
        if not fact:
            continue
        # Upsert to avoid duplicate facts per user.
        res = coll.update_one(
            {"user_id": user_id, "fact": fact},
            {"$setOnInsert": {"user_id": user_id, "fact": fact,
                              "kind": f.get("kind", "general"), "ts": _now()}},
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
    _db()[CHAT_COLLECTION].create_index([("session_id", ASCENDING), ("ts", ASCENDING)])
    _db()[FACTS_COLLECTION].create_index([("user_id", ASCENDING), ("fact", ASCENDING)], unique=True)
    _db()[TRACES_COLLECTION].create_index([("session_id", ASCENDING), ("ts", ASCENDING)])
