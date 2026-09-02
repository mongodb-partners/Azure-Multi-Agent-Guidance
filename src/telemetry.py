"""OpenTelemetry / Azure Monitor wiring.

Design goals:
  * ZERO-config-safe: if APPLICATIONINSIGHTS_CONNECTION_STRING is unset (local dev,
    offline tests), everything here degrades to a no-op — no exporters, no spans
    shipped, no import-time failures if the optional deps aren't installed.
  * Single entry point `configure_telemetry(app)` called once from app startup.
  * A module-level `tracer` and helper `record_*` metric functions the rest of the
    code can call unconditionally (they no-op until configured).

Deps (optional, add to requirements.txt for prod):
    azure-monitor-opentelemetry
    opentelemetry-instrumentation-fastapi
    opentelemetry-instrumentation-httpx
    opentelemetry-instrumentation-pymongo
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Iterator

from .settings import get_settings

logger = logging.getLogger(__name__)

_CONFIGURED = False

# --------------------------------------------------------------------------- #
# In-memory metrics store (so the UI can render a dashboard without needing
# direct App Insights access). Bounded ring buffer of recent turns + counters.
# This is complementary to the OTel export, not a replacement.
# --------------------------------------------------------------------------- #
_LOCK = threading.Lock()
_MAX_TURNS = 500
_turns: "deque[dict[str, Any]]" = deque(maxlen=_MAX_TURNS)
_counters: dict[str, float] = {
    "turns": 0.0,
    "errors": 0.0,
    "tokens_total": 0.0,
    "tokens_prompt": 0.0,
    "tokens_completion": 0.0,
    "llm_calls": 0.0,
    "tool_calls": 0.0,
}
_specialist_counts: dict[str, int] = {}
_tool_counts: dict[str, int] = {}
# Shared per-turn accumulator (NOT thread-local) so specialist worker threads spawned during
# fan-out accumulate tokens/tool/LLM counts into the SAME turn. `_LOCK` guards all mutation.
_current: dict[str, Any] | None = None


def note_tokens(prompt: int = 0, completion: int = 0, total: int = 0) -> None:
    """Called by llm.py after each LLM call; accumulates into the current turn."""
    with _LOCK:
        _counters["tokens_total"] += total
        _counters["tokens_prompt"] += prompt
        _counters["tokens_completion"] += completion
        if _current is not None:
            tok = _current["tokens"]
            tok["prompt"] += prompt
            tok["completion"] += completion
            tok["total"] += total


def _turn_begin() -> None:
    global _current
    with _LOCK:
        _current = {
            "tokens": {"prompt": 0, "completion": 0, "total": 0},
            "specialists": [],
            "llm_calls": 0,
            "tool_calls": 0,
        }


def _turn_end(latency_ms: float, error: bool) -> dict[str, Any]:
    global _current
    with _LOCK:
        cur = _current or {"tokens": {"prompt": 0, "completion": 0, "total": 0},
                           "specialists": [], "llm_calls": 0, "tool_calls": 0}
        record = {
            "ts": time.time(),
            "latency_ms": round(latency_ms, 1),
            "tokens": dict(cur["tokens"]),
            "specialists": list(cur["specialists"]),
            "llm_calls": int(cur["llm_calls"]),
            "tool_calls": int(cur["tool_calls"]),
            "error": error,
        }
        _turns.append(record)
        _current = None
    return record


def snapshot() -> dict[str, Any]:
    """Aggregate view for the UI dashboard / GET /metrics."""
    with _LOCK:
        turns = list(_turns)
        counters = dict(_counters)
        specialist_counts = dict(_specialist_counts)
        tool_counts = dict(_tool_counts)
    latencies = sorted(t["latency_ms"] for t in turns)
    n = len(latencies)

    def pct(p: float) -> float:
        if not latencies:
            return 0.0
        idx = min(n - 1, int(round((p / 100.0) * (n - 1))))
        return latencies[idx]

    turns_n = int(counters["turns"]) or len(turns)
    tokens_total = int(counters["tokens_total"])
    tokens_prompt = int(counters["tokens_prompt"])
    tokens_completion = int(counters["tokens_completion"])
    llm_calls = int(counters["llm_calls"])
    tool_calls = int(counters["tool_calls"])
    avg_latency = round(sum(latencies) / n, 1) if n else 0.0

    # Approximate USD cost from token totals and configurable per-1M pricing.
    s = get_settings()
    cost_usd = round(
        (tokens_prompt / 1_000_000.0) * s.price_input_per_1m
        + (tokens_completion / 1_000_000.0) * s.price_output_per_1m,
        4,
    )

    def per_req(x: int) -> float:
        return round(x / turns_n, 2) if turns_n else 0.0

    return {
        "enabled": _CONFIGURED,
        "totals": {
            "turns": int(counters["turns"]),
            "errors": int(counters["errors"]),
            "tokens_total": tokens_total,
            "tokens_prompt": tokens_prompt,
            "tokens_completion": tokens_completion,
            "llm_calls": llm_calls,
            "tool_calls": tool_calls,
            "cost_usd": cost_usd,
        },
        # Derived headline KPIs for the sidebar (computed once per request/refresh).
        "kpis": {
            "total_tokens": tokens_total,
            "total_cost_usd": cost_usd,
            "avg_latency_ms": avg_latency,
            "avg_tokens_per_request": per_req(tokens_total),
            "avg_tool_calls_per_request": per_req(tool_calls),
            "avg_llm_calls_per_request": per_req(llm_calls),
        },
        "latency_ms": {
            "count": n,
            "p50": pct(50),
            "p95": pct(95),
            "avg": avg_latency,
        },
        "specialist_counts": specialist_counts,
        "tool_counts": tool_counts,
        "recent_turns": turns[-50:],
    }

# These are populated by configure_telemetry(); until then they stay None and all
# helpers below no-op so callers never need to guard.
_tracer: Any = None
_metrics: dict[str, Any] = {}


def is_enabled() -> bool:
    return _CONFIGURED


def configure_telemetry(app: Any = None) -> bool:
    """Configure Azure Monitor + OTel. Returns True if telemetry is active.

    Idempotent and safe to call when the connection string / deps are absent.
    """
    global _CONFIGURED, _tracer, _metrics
    if _CONFIGURED:
        return True

    s = get_settings()
    if not s.otel_enabled or not s.applicationinsights_connection_string:
        logger.info("Telemetry disabled (no connection string or OTEL_ENABLED=false).")
        return False

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry import metrics, trace

        configure_azure_monitor(
            connection_string=s.applicationinsights_connection_string,
            service_name=s.otel_service_name,
        )

        # Instrument frameworks (best-effort; skip any that aren't installed).
        _instrument_frameworks(app)

        _tracer = trace.get_tracer(s.otel_service_name)
        meter = metrics.get_meter(s.otel_service_name)
        _metrics = {
            "turns": meter.create_counter("chat.turns", description="chat turns started"),
            "errors": meter.create_counter("chat.errors", description="chat errors"),
            "latency": meter.create_histogram(
                "chat.latency_ms", unit="ms", description="end-to-end chat latency"
            ),
            "specialist": meter.create_counter(
                "specialist.invocations", description="specialist invocations"
            ),
            "tool": meter.create_counter("tool.calls", description="MCP tool calls"),
            "tokens": meter.create_counter("llm.tokens", description="LLM tokens used"),
        }
        _CONFIGURED = True
        logger.info("Telemetry configured → Application Insights (%s).", s.otel_service_name)
        return True
    except Exception as e:  # noqa: BLE001
        # Never let telemetry break the app.
        logger.warning("Telemetry configuration failed (continuing without it): %s", e)
        return False


def _instrument_frameworks(app: Any) -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        if app is not None:
            FastAPIInstrumentor.instrument_app(app)
    except Exception as e:  # noqa: BLE001
        logger.debug("FastAPI instrumentation skipped: %s", e)
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as e:  # noqa: BLE001
        logger.debug("httpx instrumentation skipped: %s", e)
    try:
        from opentelemetry.instrumentation.pymongo import PymongoInstrumentor

        PymongoInstrumentor().instrument()
    except Exception as e:  # noqa: BLE001
        logger.debug("pymongo instrumentation skipped: %s", e)


# --------------------------------------------------------------------------- #
# Helpers — all no-op until configured, so callers use them unconditionally.
# --------------------------------------------------------------------------- #
@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any]:
    """Start a span if telemetry is active; otherwise a transparent no-op."""
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            if v is not None:
                try:
                    sp.set_attribute(k, v)
                except Exception:  # noqa: BLE001
                    pass
        yield sp


def add_event(sp: Any, name: str, **attrs: Any) -> None:
    if sp is None:
        return
    try:
        sp.add_event(name, {k: v for k, v in attrs.items() if v is not None})
    except Exception:  # noqa: BLE001
        pass


def record_exception(sp: Any, exc: BaseException) -> None:
    if sp is None:
        return
    try:
        sp.record_exception(exc)
    except Exception:  # noqa: BLE001
        pass


def _inc(metric: str, amount: int = 1, **attrs: Any) -> None:
    m = _metrics.get(metric)
    if m is None:
        return
    try:
        m.add(amount, {k: v for k, v in attrs.items() if v is not None})
    except Exception:  # noqa: BLE001
        pass


def record_turn() -> None:
    _inc("turns")
    with _LOCK:
        _counters["turns"] += 1
    _turn_begin()


def record_error(agent: str | None = None) -> None:
    _inc("errors", agent=agent)
    with _LOCK:
        _counters["errors"] += 1


def record_specialist(agent: str) -> None:
    _inc("specialist", agent=agent)
    with _LOCK:
        _specialist_counts[agent] = _specialist_counts.get(agent, 0) + 1
        if _current is not None and agent not in _current["specialists"]:
            _current["specialists"].append(agent)


def record_tool_call(tool: str | None, agent: str | None = None) -> None:
    _inc("tool", tool=tool, agent=agent)
    with _LOCK:
        _counters["tool_calls"] += 1
        if tool:
            _tool_counts[tool] = _tool_counts.get(tool, 0) + 1
        if _current is not None:
            _current["tool_calls"] += 1


def record_llm_call(count: int = 1) -> None:
    """Count one LLM/model invocation (utility calls + specialist Responses turns)."""
    with _LOCK:
        _counters["llm_calls"] += count
        if _current is not None:
            _current["llm_calls"] += count


def record_tokens(amount: int, kind: str = "total") -> None:
    if amount:
        _inc("tokens", amount=amount, kind=kind)


def record_latency_ms(ms: float) -> None:
    h = _metrics.get("latency")
    if h is None:
        return
    try:
        h.record(ms)
    except Exception:  # noqa: BLE001
        pass


def finish_turn(latency_ms: float, error: bool = False) -> dict[str, Any]:
    """Record OTel latency + finalize the in-memory per-turn record. Returns it."""
    record_latency_ms(latency_ms)
    return _turn_end(latency_ms, error)
