"""Shared Atlas plumbing for the memory package (specs/001, R4).

Owns the MongoClient singleton, collection names, and the `chat_sessions` metadata
record that both short-term backends maintain so `list_sessions` never depends on
which backend owns the history (plan §2).
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from pymongo import MongoClient

from ..settings import get_settings

logger = logging.getLogger(__name__)

CHAT_COLLECTION = "chat_messages"
FACTS_COLLECTION = "agent_memory_facts"
TRACES_COLLECTION = "agent_traces"
SESSIONS_COLLECTION = "chat_sessions"
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


def touch_session(
    session_id: str,
    user_id: str | None = None,
    role: str | None = None,
    content: str | None = None,
    conversation_id: str | None = None,
) -> None:
    """Upsert the per-session metadata row in `chat_sessions` (one call per appended message).

    Fields: first_ts/title are insert-only (title = first user message, truncated);
    last_ts/messages move on every message; conversation_id is set when the foundry
    backend knows it (a session's conversation id never changes, so a plain $set is
    idempotent — concurrent first-turns may momentarily race, last write wins).
    """
    update: dict[str, Any] = {
        "$set": {"last_ts": _now()},
        "$inc": {"messages": 1},
        "$setOnInsert": {"session_id": session_id, "first_ts": _now()},
    }
    if user_id:
        update["$set"]["user_id"] = user_id
    if conversation_id:
        update["$set"]["conversation_id"] = conversation_id
    if role == "user" and content:
        update["$setOnInsert"]["title"] = content[:120]
    _db()[SESSIONS_COLLECTION].update_one({"session_id": session_id}, update, upsert=True)
