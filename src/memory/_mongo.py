"""MongoDB short-term memory backend — the original `chat_messages` implementation (R3).

Selected when SHORT_TERM_MEMORY_BACKEND=mongo (the default).
"""
from __future__ import annotations

from typing import Any

from pymongo import ASCENDING

from . import _shared


def append_message(
    session_id: str, role: str, content: str, agent: str | None = None,
    user_id: str | None = None,
) -> None:
    _shared._db()[_shared.CHAT_COLLECTION].insert_one(
        {
            "session_id": session_id,
            "user_id": user_id,
            "role": role,
            "content": content,
            "agent": agent,
            "ts": _shared._now(),
        }
    )
    _shared.touch_session(session_id, user_id=user_id, role=role, content=content)


def get_history(session_id: str, limit: int = _shared.MAX_HISTORY) -> list[dict[str, Any]]:
    cur = (
        _shared._db()[_shared.CHAT_COLLECTION]
        .find({"session_id": session_id}, {"_id": 0, "role": 1, "content": 1, "agent": 1, "ts": 1})
        .sort("ts", ASCENDING)
    )
    msgs = list(cur)
    return msgs[-limit:]
