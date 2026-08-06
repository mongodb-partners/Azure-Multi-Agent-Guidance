"""Conversation memory backed by MongoDB.

Two layers, mirroring the reference's AgentCore Memory:
  - short-term : per-session chat turns in `chat_messages` (session-isolated, ordered).
  - long-term  : durable user facts in `agent_memory_facts`, extracted from turns by an LLM.

For the default host-orchestrated (`classifier`) path we use plain **pymongo** (synchronous,
simple to weave into the request flow). For the `maf-handoff` path, `build_maf_providers()`
returns the async MAF `MongoDBHistoryProvider` / `MongoDBContextProvider` configured against
the same collections, so MAF agents share the same memory store.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from pymongo import ASCENDING, MongoClient

from .llm import chat_json
from .settings import get_settings

CHAT_COLLECTION = "chat_messages"
FACTS_COLLECTION = "agent_memory_facts"
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
def append_message(session_id: str, role: str, content: str, agent: str | None = None) -> None:
    _db()[CHAT_COLLECTION].insert_one(
        {
            "session_id": session_id,
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
    pipeline = [
        {"$group": {"_id": "$session_id", "turns": {"$sum": 1}, "last": {"$max": "$ts"}}},
        {"$sort": {"last": -1}},
        {"$limit": 100},
    ]
    return [
        {"session_id": d["_id"], "turns": d["turns"], "last": d["last"].isoformat()}
        for d in _db()[CHAT_COLLECTION].aggregate(pipeline)
    ]


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


# --------------------------------------------------------------------------- #
# MAF providers (used by ORCHESTRATOR_MODE=maf-handoff)
# --------------------------------------------------------------------------- #
def build_maf_providers():
    """Return (history_provider, context_provider) from agent-framework-mongodb.

    Configured against the same URI/DB/collections as the pymongo path so both
    orchestration modes share one memory store.
    """
    from agent_framework_mongodb import MongoDBContextProvider, MongoDBHistoryProvider

    s = get_settings()
    s.require("mongodb_uri")
    history = MongoDBHistoryProvider(
        mongo_uri=s.mongodb_uri,
        database_name=s.mongodb_db,
        collection_name=CHAT_COLLECTION,
    )
    context = MongoDBContextProvider(
        mongo_uri=s.mongodb_uri,
        database_name=s.mongodb_db,
        collection_name=FACTS_COLLECTION,
    )
    return history, context
