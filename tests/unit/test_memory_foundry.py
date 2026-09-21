"""Unit tests for the Foundry Conversations short-term memory backend (specs/001, R2/R9).

Fully offline: the conversations client is faked and Atlas is mongomock — no live
Foundry or Atlas dependency (AC4).
"""
from __future__ import annotations

import pytest

from src.memory import _foundry, _shared

pytestmark = pytest.mark.unit

mongomock = pytest.importorskip("mongomock")


class _FakeConversation:
    def __init__(self, cid: str):
        self.id = cid


class _FakeItems:
    """Mirrors the SDK's conversations.items sub-resource."""

    def __init__(self, store: dict[str, list[dict]]):
        self._store = store

    def create(self, conversation_id: str, items: list[dict]):
        self._store.setdefault(conversation_id, []).extend(items)

    def list(self, conversation_id: str):
        # Mirror the live Foundry API: items.list returns NEWEST-first.
        return list(reversed(self._store.get(conversation_id, [])))


class _FakeConversations:
    """In-memory stand-in for openai.conversations (create / items.create / items.list)."""

    def __init__(self):
        self._store: dict[str, list[dict]] = {}
        self._n = 0
        self.items = _FakeItems(self._store)
        self.fail = False  # simulate a Foundry outage

    def create(self):
        if self.fail:
            raise RuntimeError("foundry down")
        self._n += 1
        return _FakeConversation(f"conv_{self._n}")


@pytest.fixture
def backend(monkeypatch):
    db = mongomock.MongoClient()["multi-agent"]
    monkeypatch.setattr(_shared, "_db", lambda: db)
    fake = _FakeConversations()
    monkeypatch.setattr(_foundry, "_conversations", fake)
    return fake, db


def test_append_creates_and_maps_conversation(backend):
    fake, db = backend
    _foundry.append_message("s1", "user", "hello", user_id="u1")
    doc = db[_shared.SESSIONS_COLLECTION].find_one({"session_id": "s1"})
    assert doc["conversation_id"] == "conv_1"
    assert fake._store["conv_1"] == [{"type": "message", "role": "user", "content": "hello"}]


def test_conversation_reused_across_turns(backend):
    fake, _ = backend
    _foundry.append_message("s1", "user", "hi")
    _foundry.append_message("s1", "assistant", "hello")
    assert fake._n == 1  # one conversation per session
    assert len(fake._store["conv_1"]) == 2


def test_get_history_order_and_limit(backend):
    for i in range(5):
        _foundry.append_message("s1", "user", f"m{i}")
    hist = _foundry.get_history("s1", limit=3)
    assert [m["content"] for m in hist] == ["m2", "m3", "m4"]
    assert all(m["role"] == "user" for m in hist)


def test_get_history_unknown_session_returns_empty(backend):
    assert _foundry.get_history("nope") == []


def test_non_message_items_are_skipped(backend):
    fake, db = backend
    _foundry.append_message("s1", "user", "real")
    cid = db[_shared.SESSIONS_COLLECTION].find_one({"session_id": "s1"})["conversation_id"]
    fake._store[cid].append({"type": "mcp_call", "name": "find"})  # tool-call item
    fake._store[cid].append({"type": "message", "role": "system", "content": "skip me"})
    hist = _foundry.get_history("s1")
    assert [m["content"] for m in hist] == ["real"]


def test_history_text_shape_matches_mongo_contract(backend):
    """Facade `history_as_text` consumes {role, content} — assert the same line format."""
    _foundry.append_message("s1", "user", "hello")
    _foundry.append_message("s1", "assistant", "hi")
    lines = [f"{m['role']}: {m['content']}" for m in _foundry.get_history("s1")]
    assert "\n".join(lines) == "user: hello\nassistant: hi"


def test_read_failure_degrades_to_empty(backend):
    """Mapping exists but Foundry is down → [] (no-context answer), never an exception."""
    fake, _db = backend
    _foundry.append_message("s1", "user", "hello")
    fake.fail = True

    class _BoomItems:
        def list(self, conversation_id: str):
            raise RuntimeError("foundry down")

        def create(self, conversation_id: str, items: list[dict]):
            raise RuntimeError("foundry down")

    fake.items = _BoomItems()
    assert _foundry.get_history("s1") == []


def test_append_failure_is_swallowed_and_session_still_tracked(backend):
    fake, db = backend
    fake.fail = True
    _foundry.append_message("s1", "user", "hello", user_id="u1")  # must not raise
    doc = db[_shared.SESSIONS_COLLECTION].find_one({"session_id": "s1"})
    assert doc is not None and doc["messages"] == 1  # /sessions still lists it
    assert _foundry.get_history("s1") == []  # degraded to no-context, not an error
