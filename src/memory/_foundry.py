"""Foundry Conversations short-term memory backend (specs/001, R2).

One Foundry conversation per chat session; the `session_id -> conversation_id` mapping
is persisted in Atlas (`chat_sessions.conversation_id`) so history survives API process
restarts (AC6). The Foundry conversation is the persistence layer only — prompts keep
the existing `history_as_text(MAX_HISTORY)` injection (plan §0/Q2).

Failure policy (plan §2): history failures degrade to a no-context answer — append
logs and continues, get_history returns [] — and never fail the chat turn.

Selected when SHORT_TERM_MEMORY_BACKEND=foundry. Requires the optional Foundry stack
(`pip install -r requirements-foundry.txt`) and AZURE_AI_PROJECT_ENDPOINT.
"""
from __future__ import annotations

import logging
from typing import Any

from . import _shared

logger = logging.getLogger(__name__)

_conversations = None  # lazy client; monkeypatchable in tests (R9)


def _conv():
    """Return the Foundry conversations client, creating it on first use.

    Lazy import: azure-ai-projects ships in requirements-foundry.txt, not core, so
    importing this module (or the package) must not require it — only *using* the
    foundry backend does.
    """
    global _conversations
    if _conversations is None:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential

        from ..settings import get_settings

        s = get_settings()
        s.require("project_endpoint")
        _conversations = AIProjectClient(
            endpoint=s.project_endpoint, credential=DefaultAzureCredential()
        ).get_openai_client().conversations
    return _conversations


def _conversation_id(session_id: str) -> str:
    """Resolve the Foundry conversation for a session, creating and mapping it lazily."""
    coll = _shared._db()[_shared.SESSIONS_COLLECTION]
    doc = coll.find_one({"session_id": session_id}, {"conversation_id": 1})
    if doc and doc.get("conversation_id"):
        return doc["conversation_id"]
    conv = _conv().create()
    cid = conv.id if hasattr(conv, "id") else conv["id"]
    # Race-safe mapping: insert-only; a concurrent first-turn's id wins, ours is dropped.
    coll.update_one(
        {"session_id": session_id},
        {"$setOnInsert": {"conversation_id": cid}},
        upsert=True,
    )
    return coll.find_one({"session_id": session_id})["conversation_id"]


def append_message(
    session_id: str, role: str, content: str, agent: str | None = None,
    user_id: str | None = None,
) -> None:
    # `agent` has no conversation-item representation; it is tracked via traces instead.
    cid = None
    try:
        cid = _conversation_id(session_id)
        _conv().items.create(
            conversation_id=cid,
            items=[{"type": "message", "role": role, "content": content}],
        )
    except Exception as e:  # noqa: BLE001 — history loss must not fail the turn
        logger.warning("Foundry history append failed for session %s: %s", session_id, e)
    _shared.touch_session(
        session_id, user_id=user_id, role=role, content=content, conversation_id=cid
    )


def _text_of(content: Any) -> str:
    """Extract plain text from a message item's content (str or list of parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            text = getattr(p, "text", None)
            if text is None and isinstance(p, dict):
                text = p.get("text", "")
            parts.append(text or "")
        return "".join(parts)
    return "" if content is None else str(content)


def _as_message(item: Any) -> dict[str, Any] | None:
    """Map a conversation item to the mongo backend's {role, content, ts} shape."""
    itype = getattr(item, "type", None) if not isinstance(item, dict) else item.get("type")
    if itype != "message":
        return None
    role = getattr(item, "role", None) if not isinstance(item, dict) else item.get("role")
    if role not in ("user", "assistant"):
        return None
    content = getattr(item, "content", None) if not isinstance(item, dict) else item.get("content")
    ts = getattr(item, "created_at", None) if not isinstance(item, dict) else item.get("created_at")
    return {"role": role, "content": _text_of(content), "ts": ts}


def get_history(session_id: str, limit: int = _shared.MAX_HISTORY) -> list[dict[str, Any]]:
    try:
        doc = _shared._db()[_shared.SESSIONS_COLLECTION].find_one(
            {"session_id": session_id}, {"conversation_id": 1}
        )
        cid = doc.get("conversation_id") if doc else None
        if not cid:
            return []
        # Live API behavior (verified 2026-09-18, specs/001 Phase 5): items.list returns
        # NEWEST-first. Filter to messages, keep the most recent `limit`, then reverse
        # into the facade's oldest-first contract (matches the mongo backend).
        items = list(_conv().items.list(conversation_id=cid))
    except Exception as e:  # noqa: BLE001 — degrade to a no-context answer
        logger.warning("Foundry history read failed for session %s: %s", session_id, e)
        return []
    msgs = [m for m in (_as_message(it) for it in items) if m is not None][:limit]
    msgs.reverse()
    return msgs
