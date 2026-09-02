"""Unit tests for memory read/format logic using an in-memory Mongo (mongomock)."""
from __future__ import annotations

import pytest

import src.memory as memory

pytestmark = pytest.mark.unit

mongomock = pytest.importorskip("mongomock")


@pytest.fixture
def fake_db(monkeypatch):
    client = mongomock.MongoClient()
    db = client["multi-agent"]
    monkeypatch.setattr(memory, "_db", lambda: db)
    return db


def test_append_and_get_history_order(fake_db):
    memory.append_message("s1", "user", "hello")
    memory.append_message("s1", "assistant", "hi")
    hist = memory.get_history("s1")
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert [m["content"] for m in hist] == ["hello", "hi"]


def test_history_is_session_isolated(fake_db):
    memory.append_message("s1", "user", "a")
    memory.append_message("s2", "user", "b")
    assert len(memory.get_history("s1")) == 1
    assert memory.get_history("s2")[0]["content"] == "b"


def test_history_respects_limit(fake_db):
    for i in range(30):
        memory.append_message("s1", "user", f"m{i}")
    hist = memory.get_history("s1", limit=5)
    assert len(hist) == 5
    assert hist[-1]["content"] == "m29"  # keeps the most recent


def test_history_as_text_format(fake_db):
    memory.append_message("s1", "user", "hello")
    memory.append_message("s1", "assistant", "hi")
    text = memory.history_as_text("s1")
    assert text == "user: hello\nassistant: hi"


def test_list_sessions_aggregates(fake_db):
    memory.append_message("s1", "user", "a", user_id="u1")
    memory.append_message("s1", "assistant", "b", user_id="u1")
    memory.append_message("s2", "user", "c", user_id="u2")
    sessions = memory.list_sessions()
    by_id = {s["session_id"]: s for s in sessions}
    # With no traces, `turns` falls back to messages // 2.
    assert by_id["s1"]["messages"] == 2
    assert by_id["s1"]["turns"] == 1
    assert by_id["s2"]["messages"] == 1
    # New aggregate fields are present.
    assert "tokens_total" in by_id["s1"] and "cost_usd" in by_id["s1"]


def test_list_sessions_uses_trace_stats(fake_db):
    memory.append_message("s1", "user", "a", user_id="u1")
    memory.append_message("s1", "assistant", "b", user_id="u1")
    memory.save_trace("s1", "u1", {
        "user_message": "a", "final": "b",
        "tokens": {"prompt": 100, "completion": 50, "total": 150},
        "cost_usd": 0.001, "tool_calls": [{"tool": "find"}], "tool_call_count": 1,
        "latency_ms": 1200.0,
    })
    s = {x["session_id"]: x for x in memory.list_sessions()}["s1"]
    assert s["turns"] == 1
    assert s["tokens_total"] == 150
    assert s["cost_usd"] == 0.001
    assert s["tool_calls"] == 1


def test_save_and_get_traces_roundtrip(fake_db):
    memory.save_trace("s9", "u9", {
        "user_message": "hi", "final": "hello", "reasoning": [{"step": "route", "detail": "x"}],
        "tool_calls": [], "events": [{"type": "routing"}], "tokens": {"total": 10}, "cost_usd": 0.0,
    })
    traces = memory.get_traces("s9")
    assert len(traces) == 1
    assert traces[0]["user_message"] == "hi"
    assert traces[0]["reasoning"][0]["step"] == "route"
    assert isinstance(traces[0]["ts"], str)  # JSON-safe
