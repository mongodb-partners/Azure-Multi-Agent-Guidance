"""Unit tests for orchestrator helpers + routing branches (all deps mocked)."""
from __future__ import annotations

import pytest

import src.orchestrator as orch

pytestmark = pytest.mark.unit


def test_augment_orders_sections():
    out = orch._augment("do X", history="H", facts="F", grounding="G")
    # facts → history → current request → grounding
    assert out.index("Known facts") < out.index("Recent conversation")
    assert out.index("Recent conversation") < out.index("Current request")
    assert out.index("Current request") < out.index("G")
    assert "do X" in out


def test_augment_omits_empty_sections():
    out = orch._augment("just this", history="", facts="", grounding="")
    assert "Known facts" not in out
    assert "Recent conversation" not in out
    assert "just this" in out


def test_synthesize_calls_llm(monkeypatch):
    captured = {}

    def fake_chat_text(messages, **kw):
        captured["messages"] = messages
        return "MERGED"

    monkeypatch.setattr(orch, "chat_text", fake_chat_text)
    result = orch._synthesize("q", {"job-match": "A", "career-coach": "B"})
    assert result == "MERGED"
    # Both specialist answers must be present in the user message.
    user_msg = captured["messages"][-1]["content"]
    assert "A" in user_msg and "B" in user_msg


class _Ev:
    def __init__(self, type_, **data):
        self.type = type_
        self.data = data


class _FakeSpecialist:
    def __init__(self, text, tool=None):
        self._text = text
        self._tool = tool

    def stream_query(self, _augmented):
        if self._tool:
            yield _Ev("tool_call", tool=self._tool)
        yield _Ev("message", text=self._text)


class _FakeRegistry:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, name):
        return self._m[name]


def _stub_memory(monkeypatch):
    monkeypatch.setattr(orch.memory, "history_as_text", lambda *_: "")
    monkeypatch.setattr(orch.memory, "facts_as_text", lambda *_: "")
    monkeypatch.setattr(orch, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_save_trace", lambda *a, **k: None)
    # _retrieve_context now returns (prompt_text, hits)
    monkeypatch.setattr(orch, "_retrieve_context", lambda *a, **k: ("", []))


def test_run_turn_direct_reply_when_no_specialist(monkeypatch):
    _stub_memory(monkeypatch)

    class C:
        backend = "heuristic:direct"
        specialists = []
        scores = {}

    monkeypatch.setattr(orch, "classify", lambda _m: C())
    monkeypatch.setattr(orch, "_direct_reply", lambda *a, **k: "hi there")

    events = list(orch.run_turn("s", "u", "hello"))
    types = [e["type"] for e in events]
    assert "direct" in types
    final = [e for e in events if e["type"] == "final"][0]
    assert final["text"] == "hi there"


def test_run_turn_single_specialist(monkeypatch):
    _stub_memory(monkeypatch)

    class C:
        backend = "heuristic"
        specialists = ["job-match"]
        scores = {"job-match": 3}

    monkeypatch.setattr(orch, "classify", lambda _m: C())
    reg = _FakeRegistry({"job-match": _FakeSpecialist("here are jobs", tool="find")})
    monkeypatch.setattr(orch, "get_registry", lambda: reg)

    events = list(orch.run_turn("s", "u", "find jobs"))
    types = [e["type"] for e in events]
    assert "routing" in types
    assert "specialist_start" in types
    assert "tool_call" in types
    final = [e for e in events if e["type"] == "final"][0]
    assert final["text"] == "here are jobs"


def test_run_turn_two_specialists_synthesizes(monkeypatch):
    _stub_memory(monkeypatch)

    class C:
        backend = "heuristic"
        specialists = ["job-match", "company-intel"]
        scores = {}

    monkeypatch.setattr(orch, "classify", lambda _m: C())
    reg = _FakeRegistry(
        {
            "job-match": _FakeSpecialist("JOBS"),
            "company-intel": _FakeSpecialist("COMPANIES"),
        }
    )
    monkeypatch.setattr(orch, "get_registry", lambda: reg)
    monkeypatch.setattr(orch, "_synthesize", lambda q, answers: "SYNTH(" + "+".join(sorted(answers)) + ")")

    events = list(orch.run_turn("s", "u", "jobs and companies"))
    types = [e["type"] for e in events]
    assert "synthesizing" in types
    final = [e for e in events if e["type"] == "final"][0]
    assert final["text"] == "SYNTH(company-intel+job-match)"
