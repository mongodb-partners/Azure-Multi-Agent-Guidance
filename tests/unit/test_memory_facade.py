"""Unit tests for the memory facade: import-surface parity and backend dispatch.

Covers specs/001 R1 (callers see one unchanged API) and R3 (config-driven selection
with mongo default → rollback is a config flip, AC5). Fully offline (R9/AC4).
"""
from __future__ import annotations

import pytest

import src.memory as memory
from src.memory import _mongo, _shared
from src.settings import Settings

pytestmark = pytest.mark.unit

mongomock = pytest.importorskip("mongomock")

# The public surface callers relied on before the package split (R1).
EXPECTED_PUBLIC = [
    "append_message", "get_history", "history_as_text", "list_sessions",
    "save_trace", "get_traces", "get_facts", "facts_as_text",
    "extract_and_store_facts", "ensure_indexes",
    "CHAT_COLLECTION", "FACTS_COLLECTION", "TRACES_COLLECTION", "MAX_HISTORY",
]


def _use_backend(monkeypatch, backend: str) -> None:
    """Patch the facade's settings reference (Settings field defaults are read at class
    definition time, so the repo's pattern is Settings(**overrides) injection)."""
    monkeypatch.setattr(memory, "get_settings", lambda: Settings(short_term_memory_backend=backend))


def test_public_surface_preserved():
    for name in EXPECTED_PUBLIC:
        assert hasattr(memory, name), f"missing public name: {name}"


def test_default_backend_is_mongo():
    assert Settings().short_term_memory_backend == "mongo"


def test_mongo_backend_selected_by_config(monkeypatch):
    _use_backend(monkeypatch, "mongo")
    assert memory._history_backend() is _mongo


def test_foundry_backend_selected_by_config(monkeypatch):
    _use_backend(monkeypatch, "foundry")
    from src.memory import _foundry

    assert memory._history_backend() is _foundry


def test_mongo_backend_dispatch_end_to_end(monkeypatch):
    """Facade append/get/history route to the mongo backend by default (R1/R3)."""
    db = mongomock.MongoClient()["multi-agent"]
    monkeypatch.setattr(_shared, "_db", lambda: db)
    memory.append_message("s1", "user", "hello", user_id="u1")
    memory.append_message("s1", "assistant", "hi", user_id="u1")
    assert memory.history_as_text("s1") == "user: hello\nassistant: hi"
    # mongo backend wrote chat_messages AND chat_sessions metadata (R4).
    assert db["chat_messages"].count_documents({"session_id": "s1"}) == 2
    doc = db["chat_sessions"].find_one({"session_id": "s1"})
    assert doc["messages"] == 2 and doc["user_id"] == "u1"
    assert doc["title"] == "hello"  # first user message


def test_list_sessions_includes_foundry_only_sessions(monkeypatch):
    """A session with no chat_messages rows (foundry backend) still lists (R4/AC2)."""
    db = mongomock.MongoClient()["multi-agent"]
    monkeypatch.setattr(_shared, "_db", lambda: db)
    _shared.touch_session("s-foundry", user_id="u1", role="user", content="hi",
                          conversation_id="conv_1")
    memory.append_message("s-legacy", "user", "old", user_id="u2")  # mongo path
    sessions = {s["session_id"]: s for s in memory.list_sessions()}
    assert set(sessions) == {"s-foundry", "s-legacy"}
    assert sessions["s-foundry"]["messages"] == 1
    for s in sessions.values():  # shape parity with the pre-split response
        for key in ("session_id", "user_id", "turns", "messages", "last", "first",
                    "tokens_total", "cost_usd", "tool_calls", "avg_latency_ms",
                    "last_message"):
            assert key in s, f"missing field {key}"
