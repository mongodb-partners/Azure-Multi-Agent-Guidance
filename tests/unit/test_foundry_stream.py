"""Unit tests for the Responses-stream → Event mapping in foundry_agents._consume_stream.

Verifies that answer text and model reasoning are forwarded *live* as `delta` / `reasoning`
Events (not buffered), while the full accumulated answer is still emitted as a final `message`
Event. No network / Azure creds required — the stream is a plain list of fake event objects.
"""
from __future__ import annotations

import pytest

import src.foundry_agents as fa

pytestmark = pytest.mark.unit


class _RespEv:
    """Minimal stand-in for an OpenAI Responses streaming event."""

    def __init__(self, type_, **attrs):
        self.type = type_
        for k, v in attrs.items():
            setattr(self, k, v)


class _Resp:
    def __init__(self, id_="resp_1", output_text="", error=None, usage=None):
        self.id = id_
        self.output_text = output_text
        self.error = error
        self.usage = usage


def _specialist():
    # Build a FoundrySpecialist without touching config/clients.
    spec = fa.FoundrySpecialist.__new__(fa.FoundrySpecialist)
    spec.cfg = type("Cfg", (), {"name": "job-match"})()
    return spec


def test_tool_call_event_includes_arguments():
    # spec 008 R8: mcp_call items carry tool arguments; they must surface on the Event.
    spec = _specialist()
    item = _RespEv("mcp_call", id="call_1", name="find",
                   arguments='{"collection": "jobs", "filter": {"remote": true}}')
    ev = spec._tool_call_event(item, set())
    assert ev is not None
    assert ev.type == "tool_call"
    assert ev.data["tool"] == "find"
    assert ev.data["args"] == '{"collection": "jobs", "filter": {"remote": true}}'
    # Already-seen id → no duplicate event.
    assert spec._tool_call_event(item, {ev.data["id"]}) is None
    # Item without arguments → args is None, event still emitted.
    bare = spec._tool_call_event(_RespEv("mcp_call", id="call_2", name="count"), set())
    assert bare is not None and bare.data["args"] is None


def test_consume_stream_forwards_text_and_reasoning_deltas(monkeypatch):
    monkeypatch.setattr(fa, "_record_response_usage", lambda *_a, **_k: None)
    spec = _specialist()

    stream = [
        _RespEv("response.created", response=_Resp(id_="resp_9")),
        _RespEv("response.reasoning_summary_text.delta", delta="Let me "),
        _RespEv("response.reasoning_summary_text.delta", delta="think."),
        _RespEv("response.output_text.delta", delta="Here "),
        _RespEv("response.output_text.delta", delta="are jobs."),
        _RespEv("response.completed", response=_Resp(id_="resp_9", output_text="Here are jobs.")),
    ]

    events = list(spec._consume_stream(iter(stream)))
    types = [e.type for e in events]

    # Live reasoning deltas.
    reasoning = [e for e in events if e.type == "reasoning"]
    assert "".join(e.data["text"] for e in reasoning) == "Let me think."
    # Live answer deltas.
    deltas = [e for e in events if e.type == "delta"]
    assert "".join(e.data["text"] for e in deltas) == "Here are jobs."
    # Deltas arrive BEFORE the final message (i.e. streamed, not buffered-then-sent).
    assert types.index("delta") < types.index("message")
    # Final accumulated message.
    msg = [e for e in events if e.type == "message"][-1]
    assert msg.data["text"] == "Here are jobs."
    assert msg.data["response_id"] == "resp_9"


def test_consume_stream_falls_back_to_buffer_when_no_output_text(monkeypatch):
    """If `response.completed` lacks output_text, the message uses the buffered deltas."""
    monkeypatch.setattr(fa, "_record_response_usage", lambda *_a, **_k: None)
    spec = _specialist()

    stream = [
        _RespEv("response.output_text.delta", delta="partial "),
        _RespEv("response.output_text.delta", delta="answer"),
        _RespEv("response.completed", response=_Resp(output_text="")),
    ]
    events = list(spec._consume_stream(iter(stream)))
    msg = [e for e in events if e.type == "message"][-1]
    assert msg.data["text"] == "partial answer"


def test_consume_stream_reasoning_text_variant(monkeypatch):
    """`response.reasoning_text.delta` (raw reasoning) is also forwarded as `reasoning`."""
    monkeypatch.setattr(fa, "_record_response_usage", lambda *_a, **_k: None)
    spec = _specialist()

    stream = [
        _RespEv("response.reasoning_text.delta", delta="raw reasoning"),
        _RespEv("response.output_text.delta", delta="ok"),
        _RespEv("response.completed", response=_Resp(output_text="ok")),
    ]
    events = list(spec._consume_stream(iter(stream)))
    reasoning = [e for e in events if e.type == "reasoning"]
    assert reasoning and reasoning[0].data["text"] == "raw reasoning"


def test_consume_stream_error_event(monkeypatch):
    monkeypatch.setattr(fa, "_record_response_usage", lambda *_a, **_k: None)
    spec = _specialist()

    stream = [
        _RespEv("response.output_text.delta", delta="starting"),
        _RespEv("response.failed", response=_Resp(error="boom")),
    ]
    events = list(spec._consume_stream(iter(stream)))
    # A delta streamed before the failure, then an error Event terminates the stream.
    assert any(e.type == "delta" for e in events)
    err = [e for e in events if e.type == "error"]
    assert err and "boom" in err[0].data["error"]
    # No trailing message event after an error.
    assert events[-1].type == "error"


class _FakeBadRequest(Exception):
    """Stand-in for openai.BadRequestError with a `.message` attribute."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _patch_badrequest(monkeypatch):
    """Make `from openai import BadRequestError` resolve to our fake in the module under test."""
    import openai
    monkeypatch.setattr(openai, "BadRequestError", _FakeBadRequest, raising=False)


def test_reasoning_summary_requested_for_reasoning_model(monkeypatch):
    """A reasoning-capable client keeps `reasoning={'summary':'auto'}` (single successful call)."""
    _patch_badrequest(monkeypatch)
    spec = _specialist()
    calls = []

    class _Client:
        class responses:
            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                return iter([])  # empty stream; we only assert on the kwargs
    spec.client = _Client()

    stream = spec._create_stream_with_fallback(
        {"model": "gpt-5.6-sol", "input": "hi", "stream": True, "reasoning": {"summary": "auto"}}
    )
    assert stream is not None
    assert len(calls) == 1
    assert calls[0].get("reasoning") == {"summary": "auto"}


def test_reasoning_summary_stripped_for_non_reasoning_model(monkeypatch):
    """gpt-4o rejects the reasoning param → it's stripped and the call retried once."""
    _patch_badrequest(monkeypatch)
    spec = _specialist()
    calls = []

    class _Client:
        class responses:
            @staticmethod
            def create(**kwargs):
                calls.append(dict(kwargs))
                if "reasoning" in kwargs:
                    raise _FakeBadRequest("Unknown parameter: 'reasoning.summary'.")
                return iter([])  # succeeds once reasoning is gone
    spec.client = _Client()

    stream = spec._create_stream_with_fallback(
        {"model": "gpt-4o", "input": "hi", "stream": True, "reasoning": {"summary": "auto"}}
    )
    assert stream is not None
    # First attempt with reasoning (rejected), second attempt without (succeeds).
    assert len(calls) == 2
    assert "reasoning" in calls[0]
    assert "reasoning" not in calls[1]


def test_create_stream_returns_none_on_unrecoverable_error(monkeypatch):
    """A non-reasoning 400 (e.g. bad tool) is unrecoverable → returns None with an error captured."""
    _patch_badrequest(monkeypatch)
    spec = _specialist()

    class _Client:
        class responses:
            @staticmethod
            def create(**kwargs):
                raise _FakeBadRequest("some other 400")
    spec.client = _Client()

    stream = spec._create_stream_with_fallback(
        {"model": "gpt-5.6-sol", "input": "hi", "stream": True, "reasoning": {"summary": "auto"}}
    )
    assert stream is None
    assert spec._last_stream_error and "some other 400" in spec._last_stream_error
