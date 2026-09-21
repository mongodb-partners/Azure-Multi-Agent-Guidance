"""Unit tests for orchestrator helpers + routing branches (all deps mocked)."""
from __future__ import annotations

import pytest

import src.orchestrator as orch

pytestmark = pytest.mark.unit


def test_augment_orders_sections():
    out = orch._augment("do X", history="HIST", facts="FACT", grounding="GROUND")
    # facts → history → grounding → current request (the ask lands last, next to its evidence)
    assert out.index("Known facts") < out.index("Recent conversation")
    assert out.index("Recent conversation") < out.index("GROUND")
    assert out.index("GROUND") < out.index("Current request")
    assert "do X" in out


def test_augment_is_deterministic():
    args = ("do X", "HIST", "FACT", "GROUND")
    assert orch._augment(*args) == orch._augment(*args)


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
    def __init__(self, text, tool=None, reasoning=None):
        self._text = text
        self._tool = tool
        self._reasoning = reasoning

    def stream_query(self, _augmented):
        if self._tool:
            yield _Ev("tool_call", tool=self._tool)
        if self._reasoning:
            for chunk in self._reasoning:
                yield _Ev("reasoning", text=chunk)
        # Stream the answer token-by-token, then emit the final accumulated message.
        for ch in self._text:
            yield _Ev("delta", text=ch)
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
    # Direct reply streams (kind, text) chunks; stub the streaming helper (specs/004 R1).
    monkeypatch.setattr(
        orch,
        "_direct_reply_stream",
        lambda *a, **k: iter([("reasoning", "thinking… "), ("delta", "hi "), ("delta", "there")]),
    )

    events = list(orch.run_turn("s", "u", "hello"))
    types = [e["type"] for e in events]
    assert "direct" in types
    # Direct reply streams live delta chunks too.
    assert "delta" in types
    streamed = "".join(e["text"] for e in events if e["type"] == "delta")
    assert streamed == "hi there"
    final = [e for e in events if e["type"] == "final"][0]
    assert final["text"] == "hi there"
    assert final["streamed"] is True


def test_run_turn_direct_reply_streams_reasoning(monkeypatch):
    """specs/004 R1/R7: the direct path forwards reasoning chunks as `reasoning` events
    (agent="orchestrator") and the trace accumulates them into `reasoning_text`."""
    _stub_memory(monkeypatch)

    class C:
        backend = "heuristic:direct"
        specialists = []
        scores = {}

    monkeypatch.setattr(orch, "classify", lambda _m: C())
    monkeypatch.setattr(
        orch,
        "_direct_reply_stream",
        lambda *a, **k: iter([("reasoning", "let me "), ("reasoning", "think"),
                              ("delta", "hi")]),
    )
    monkeypatch.setattr(orch, "_direct_reply", lambda *a, **k: "hi")

    captured: dict = {}
    monkeypatch.setattr(
        orch, "_save_trace",
        lambda _s, _u, trace, _r: captured.update(reasoning_text=trace.reasoning_text),
    )

    events = list(orch.run_turn("s", "u", "hello"))
    reasoning = [e for e in events if e["type"] == "reasoning"]
    assert reasoning and all(e["agent"] == "orchestrator" for e in reasoning)
    assert "".join(e["text"] for e in reasoning) == "let me think"
    assert captured["reasoning_text"] == "let me think"


def test_run_turn_direct_reply_falls_back_when_stream_fails(monkeypatch):
    _stub_memory(monkeypatch)

    class C:
        backend = "heuristic:direct"
        specialists = []
        scores = {}

    def _boom(*_a, **_k):
        raise RuntimeError("stream down")
        yield  # pragma: no cover — make it a generator

    monkeypatch.setattr(orch, "classify", lambda _m: C())
    monkeypatch.setattr(orch, "_direct_reply_stream", _boom)
    monkeypatch.setattr(orch, "_direct_reply", lambda *a, **k: "fallback answer")

    events = list(orch.run_turn("s", "u", "hello"))
    final = [e for e in events if e["type"] == "final"][0]
    assert final["text"] == "fallback answer"
    # Fallback path did not stream → flagged as not-streamed so the UI renders it.
    assert final["streamed"] is False


def test_run_turn_single_specialist(monkeypatch):
    _stub_memory(monkeypatch)

    class C:
        backend = "heuristic"
        specialists = ["job-match"]
        scores = {"job-match": 3}

    monkeypatch.setattr(orch, "classify", lambda _m: C())
    reg = _FakeRegistry(
        {"job-match": _FakeSpecialist("here are jobs", tool="find", reasoning=["think", "ing"])}
    )
    monkeypatch.setattr(orch, "get_registry", lambda: reg)

    events = list(orch.run_turn("s", "u", "find jobs"))
    types = [e["type"] for e in events]
    assert "routing" in types
    assert "specialist_start" in types
    assert "tool_call" in types
    # Live streaming: reasoning + answer deltas are forwarded.
    assert "reasoning" in types
    assert "delta" in types
    # Reasoning chunks reassemble to the full reasoning text.
    reasoning = "".join(e["text"] for e in events if e["type"] == "reasoning")
    assert reasoning == "thinking"
    # Answer deltas reassemble to the full answer.
    streamed = "".join(e["text"] for e in events if e["type"] == "delta")
    assert streamed == "here are jobs"
    final = [e for e in events if e["type"] == "final"][0]
    assert final["text"] == "here are jobs"
    # Single specialist streamed the answer live → final is flagged as already-streamed.
    assert final["streamed"] is True


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
    # Both specialists still stream their deltas live (progress), even though the final
    # answer is the synthesized merge.
    assert "delta" in types
    final = [e for e in events if e["type"] == "final"][0]
    assert final["text"] == "SYNTH(company-intel+job-match)"
    # Synthesized reply differs from streamed deltas → NOT flagged as already-streamed.
    assert final["streamed"] is False


# --------------------------------------------------------------------------- #
# Grounding block: relevance floor, stable cite ids, whole-document budget
# --------------------------------------------------------------------------- #
def test_relevant_applies_cosine_floor_to_every_hit():
    assert orch._relevant({"_vs_score": 0.9})
    assert not orch._relevant({"_vs_score": 0.1})
    # A BM25-only hit has no cosine score to threshold on, so it does not clear the floor —
    # `$search` ORs query terms, so a single common word would otherwise wave anything through.
    assert not orch._relevant({"_ts_score": 3.2})
    # Atlas normalises cosine to (1+cos)/2, so an orthogonal match scores 0.5 and must fail.
    assert not orch._relevant({"_vs_score": 0.5})


def test_grounding_block_never_truncates_mid_document():
    import json

    # 10 fat docs against a 6000-char budget — the block must drop whole docs, not bytes.
    hits = [{"_id": i, "_vs_score": 0.9, "embedding_junk": "x" * 900, "title": f"t{i}"}
            for i in range(10)]
    text, kept = orch._grounding_block("jobs", hits)

    rows = [ln for ln in text.splitlines() if ln.startswith("{")]
    assert 0 < len(rows) < 10                       # budget actually bit
    for ln in rows:
        json.loads(ln)                              # every emitted row is valid JSON
    assert len(kept) == len(rows)


def test_grounding_block_stamps_cite_ids_and_hides_internals():
    hits = [{"_id": "abc", "_vs_score": 0.9, "_rrf_score": 0.03, "title": "Dev"}]
    text, kept = orch._grounding_block("jobs", hits)
    assert '"id": "j1"' in text
    assert kept[0]["_cite_id"] == "j1"
    # Internal scoring fields and the raw ObjectId stay out of the prompt.
    assert "_vs_score" not in text and "_rrf_score" not in text and "abc" not in text


def test_retrieve_context_returns_nothing_when_all_hits_are_noise(monkeypatch):
    monkeypatch.setattr(orch, "hybrid_search", lambda *a, **k: [{"_vs_score": 0.05, "t": "x"}])
    assert orch._retrieve_context("job-match", "unrelated query") == ("", [])


def test_check_citations_flags_invented_ids():
    hits = [{"_cite_id": "j1"}, {"_cite_id": "j2"}]
    assert orch._check_citations("Try [j1] and [j2].", hits)["grounded"]
    bad = orch._check_citations("Also consider [j7].", hits)
    assert bad["invalid"] == ["j7"] and not bad["grounded"]
    # No citation at all is not "grounded" either.
    assert not orch._check_citations("Some vibes.", hits)["grounded"]


def test_synthesize_prompt_is_order_independent(monkeypatch):
    seen = []
    monkeypatch.setattr(orch, "chat_text", lambda messages, **kw: seen.append(
        messages[-1]["content"]) or "M")
    orch._synthesize("q", {"job-match": "A", "career-coach": "B"})
    orch._synthesize("q", {"career-coach": "B", "job-match": "A"})
    assert seen[0] == seen[1]
