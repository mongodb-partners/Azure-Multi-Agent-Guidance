"""Unit tests for specs/004: Responses-based direct-reply streaming (R1, R3, R5)
and registered-path reasoning parity (R2, R3). No network / Azure creds required.
"""
from __future__ import annotations

import httpx
import pytest
from openai import BadRequestError

import src.foundry_registry as fr
from src import llm
from src.settings import Settings

pytestmark = pytest.mark.unit


class _Ev:
    """Minimal stand-in for an OpenAI Responses streaming event."""

    def __init__(self, type_, **attrs):
        self.type = type_
        for k, v in attrs.items():
            setattr(self, k, v)


class _Resp:
    def __init__(self, usage=None):
        self.usage = usage


def _use(monkeypatch, **overrides):
    monkeypatch.setattr(llm, "get_settings",
                        lambda: Settings(classifier_deployment="gpt-x", **overrides))


def _bad_request(msg: str) -> BadRequestError:
    resp = httpx.Response(400, request=httpx.Request("POST", "https://test"))
    return BadRequestError(msg, response=resp, body={"error": {"param": "reasoning"}})


def _client_returning(stream):
    class _Responses:
        def create(self, **kwargs):
            return iter(stream)

    return type("C", (), {"responses": _Responses()})()


# --- chat_text_stream_events (R1) ------------------------------------------- #
def test_stream_events_yields_reasoning_and_delta(monkeypatch):
    _use(monkeypatch)
    monkeypatch.setattr(llm, "_record_usage", lambda *_a, **_k: None)
    monkeypatch.setattr(llm, "_responses_client", lambda: _client_returning([
        _Ev("response.reasoning_summary_text.delta", delta="thinking "),
        _Ev("response.reasoning_text.delta", delta="hard"),
        _Ev("response.output_text.delta", delta="Hello"),
        _Ev("response.output_text.delta", delta="!"),
        _Ev("response.completed", response=_Resp()),
    ]))

    chunks = list(llm.chat_text_stream_events([{"role": "user", "content": "hi"}]))
    assert chunks == [("reasoning", "thinking "), ("reasoning", "hard"),
                      ("delta", "Hello"), ("delta", "!")]


def test_stream_events_requests_low_effort_reasoning_summary(monkeypatch):
    """R5: the reasoning request goes out with effort=low + summary=auto."""
    _use(monkeypatch)
    monkeypatch.setattr(llm, "_record_usage", lambda *_a, **_k: None)
    seen = {}

    class _Responses:
        def create(self, **kwargs):
            seen.update(kwargs)
            return iter([_Ev("response.completed", response=_Resp())])

    monkeypatch.setattr(llm, "_responses_client",
                        lambda: type("C", (), {"responses": _Responses()})())
    list(llm.chat_text_stream_events([{"role": "user", "content": "hi"}]))
    assert seen["reasoning"] == {"effort": "low", "summary": "auto"}
    assert seen["stream"] is True


def test_stream_events_strips_reasoning_on_400(monkeypatch):
    """R3: a deployment that rejects `reasoning` is retried with it stripped."""
    _use(monkeypatch)
    monkeypatch.setattr(llm, "_record_usage", lambda *_a, **_k: None)
    calls: list[dict] = []

    class _Responses:
        def create(self, **kwargs):
            calls.append(dict(kwargs))
            if "reasoning" in kwargs:
                raise _bad_request("Unrecognized request argument supplied: reasoning")
            return iter([_Ev("response.output_text.delta", delta="ok"),
                         _Ev("response.completed", response=_Resp())])

    monkeypatch.setattr(llm, "_responses_client",
                        lambda: type("C", (), {"responses": _Responses()})())
    chunks = list(llm.chat_text_stream_events([{"role": "user", "content": "hi"}]))
    assert chunks == [("delta", "ok")]
    assert "reasoning" in calls[0] and "reasoning" not in calls[1]


# --- registered path reasoning parity (R2, R3) ------------------------------- #
class _FakeProjectClient:
    def __init__(self, oai):
        self._oai = oai

    def get_openai_client(self, agent_name):
        return self._oai


def _registry_with(oai, monkeypatch):
    reg = fr.FoundryAgentRegistry.__new__(fr.FoundryAgentRegistry)
    reg.endpoint = "https://example"
    reg._credential = None
    monkeypatch.setattr(fr.FoundryAgentRegistry, "client", lambda self: _FakeProjectClient(oai))
    return reg


def test_invoke_by_reference_requests_reasoning_summary(monkeypatch):
    seen = {}

    class _Responses:
        def create(self, **kwargs):
            seen.update(kwargs)
            return "stream-obj"

    oai = type("O", (), {"responses": _Responses()})()
    reg = _registry_with(oai, monkeypatch)
    out = reg.invoke_by_reference("job-match", input="hi", stream=True)
    assert out == "stream-obj"
    assert seen["reasoning"] == {"summary": "auto"}
    assert seen["input"] == "hi" and seen["stream"] is True


def test_invoke_by_reference_strips_reasoning_on_400(monkeypatch):
    calls: list[dict] = []

    class _Responses:
        def create(self, **kwargs):
            calls.append(dict(kwargs))
            if "reasoning" in kwargs:
                raise _bad_request("reasoning is not supported for this agent")
            return "stream-obj"

    oai = type("O", (), {"responses": _Responses()})()
    reg = _registry_with(oai, monkeypatch)
    out = reg.invoke_by_reference("job-match", input="hi")
    assert out == "stream-obj"
    assert "reasoning" in calls[0] and "reasoning" not in calls[1]
