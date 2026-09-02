"""Unit tests: telemetry helpers must be safe no-ops when unconfigured."""
from __future__ import annotations

import pytest

import src.telemetry as telemetry
from src.settings import Settings

pytestmark = pytest.mark.unit


def test_configure_noop_without_connection_string(monkeypatch):
    monkeypatch.setattr(telemetry, "get_settings", lambda: Settings(
        applicationinsights_connection_string="", otel_enabled=True
    ))
    telemetry._CONFIGURED = False
    assert telemetry.configure_telemetry(None) is False
    assert telemetry.is_enabled() is False


def test_helpers_are_safe_noops_when_unconfigured():
    # None of these should raise even though telemetry isn't configured.
    telemetry._tracer = None
    telemetry._metrics = {}
    telemetry.record_turn()
    telemetry.record_error("job-match")
    telemetry.record_specialist("job-match")
    telemetry.record_tool_call("find", agent="job-match")
    telemetry.record_llm_call()
    telemetry.record_tokens(123, kind="prompt")
    telemetry.record_latency_ms(42.0)
    with telemetry.span("noop", foo="bar") as sp:
        assert sp is None
        telemetry.add_event(sp, "evt", x=1)
        telemetry.record_exception(sp, ValueError("x"))


def _reset_store():
    telemetry._turns.clear()
    for k in telemetry._counters:
        telemetry._counters[k] = 0.0
    telemetry._specialist_counts.clear()
    telemetry._tool_counts.clear()


def test_snapshot_kpis_and_cost(monkeypatch):
    """The 6 headline KPIs + USD cost are computed correctly from recorded turns."""
    _reset_store()
    # Fixed pricing so the cost assertion is deterministic ($2/1M in, $8/1M out).
    monkeypatch.setattr(telemetry, "get_settings", lambda: Settings(
        price_input_per_1m=2.0, price_output_per_1m=8.0
    ))

    # Turn 1: 2 LLM calls, 2 tool calls, 100 prompt + 50 completion tokens, 1000ms.
    telemetry.record_turn()
    telemetry.record_llm_call()
    telemetry.record_llm_call()
    telemetry.note_tokens(prompt=100, completion=50, total=150)
    telemetry.record_tool_call("find")
    telemetry.record_tool_call("aggregate")
    telemetry.finish_turn(1000.0, error=False)

    # Turn 2: 1 LLM call, 1 tool call, 200 prompt + 100 completion, 3000ms.
    telemetry.record_turn()
    telemetry.record_llm_call()
    telemetry.note_tokens(prompt=200, completion=100, total=300)
    telemetry.record_tool_call("find")
    telemetry.finish_turn(3000.0, error=False)

    snap = telemetry.snapshot()
    kp = snap["kpis"]
    assert snap["totals"]["turns"] == 2
    assert snap["totals"]["llm_calls"] == 3
    assert snap["totals"]["tool_calls"] == 3
    assert kp["total_tokens"] == 450
    assert kp["avg_latency_ms"] == 2000.0
    assert kp["avg_tokens_per_request"] == 225.0
    assert kp["avg_tool_calls_per_request"] == 1.5
    assert kp["avg_llm_calls_per_request"] == 1.5
    # cost = 300/1e6*$2 + 150/1e6*$8 = 0.0006 + 0.0012 = 0.0018
    assert kp["total_cost_usd"] == 0.0018
