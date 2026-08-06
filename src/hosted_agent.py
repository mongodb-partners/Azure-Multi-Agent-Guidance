"""Hosted whole-solution agent — the entire orchestrator behind one Responses endpoint.

This is the "use the whole solution" option: the full `run_turn` pipeline (classify → fan-out
specialists → synthesize) packaged as a single **Foundry hosted agent**. It speaks the Responses
protocol on port 8088 via `azure-ai-agentserver-responses`, so any Responses client (the portal,
another agent, external code) can call it by reference and get one synthesized answer.

Design:
- One `response_handler` bridges the *sync* `run_turn` generator into an async text stream that
  `TextResponse` turns into a proper SSE lifecycle (created → in_progress → output_text deltas →
  completed). `run_turn` runs on a worker thread; its events are marshalled onto an asyncio queue.
- By default only the final synthesized answer is streamed as the message (clean output). Set
  `HOSTED_STREAM_TRACE=true` to also stream the live routing/tool-call trace the UI shows.
- Inside the container we default `SPECIALIST_MODE=inline` so the whole-solution agent is
  self-contained (it does not depend on the separately-registered prompt agents). Override with
  `SPECIALIST_MODE=registered` to have the hosted agent delegate to the registered specialists.

Run locally:  python -m src.hosted_agent   →   curl -N localhost:8088/responses -d '{"input":"..."}'
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, AsyncIterator

# Self-contained by default: the whole-solution agent runs specialists inline unless told otherwise.
os.environ.setdefault("SPECIALIST_MODE", "inline")
# Silence the agent-server's OpenTelemetry Azure-IMDS resource probe when running off-Azure
# (it spams connection errors to 169.254.169.254). Override with OTEL_SDK_DISABLED=false in prod.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

from .orchestrator import run_turn  # noqa: E402  (after env default so settings pick it up)

logger = logging.getLogger("hosted_agent")

_STREAM_TRACE = os.getenv("HOSTED_STREAM_TRACE", "false").lower() == "true"
_PORT = int(os.getenv("HOSTED_PORT", "8088"))


def _trace_line(ev: dict[str, Any]) -> str:
    """Render a trace event as a short human-readable status line (only when trace streaming is on)."""
    t = ev.get("type")
    if t == "routing":
        return f"· routing → {', '.join(ev.get('specialists') or []) or 'direct'}\n"
    if t == "specialist_start":
        return f"· {ev.get('agent')} working…\n"
    if t == "tool_call":
        return f"·   {ev.get('agent')} → MongoDB `{ev.get('tool')}`\n"
    if t == "synthesizing":
        return f"· synthesizing from {', '.join(ev.get('agents') or [])}\n"
    return ""


def _resolve_ids(context: Any, request: Any) -> tuple[str, str]:
    """Derive (session_id, user_id) from the Responses request/context, with safe defaults."""
    meta: dict[str, Any] = {}
    try:
        meta = dict(request.get("metadata") or {}) if request is not None else {}
    except Exception:  # noqa: BLE001
        meta = {}
    session_id = getattr(context, "session_id", "") or meta.get("session_id") or "default"
    user_id = meta.get("user_id") or "hosted-user"
    return session_id, user_id


async def _stream_answer(session_id: str, user_id: str, message: str) -> AsyncIterator[str]:
    """Bridge the sync `run_turn` generator into an async text stream for `TextResponse`."""
    loop = asyncio.get_running_loop()
    q: "asyncio.Queue[dict[str, Any] | object]" = asyncio.Queue()
    SENTINEL = object()

    def worker() -> None:
        try:
            for ev in run_turn(session_id, user_id, message):
                loop.call_soon_threadsafe(q.put_nowait, ev)
        except Exception as e:  # noqa: BLE001
            loop.call_soon_threadsafe(q.put_nowait, {"type": "error", "error": str(e)})
        finally:
            loop.call_soon_threadsafe(q.put_nowait, SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    final_seen = False
    while True:
        ev = await q.get()
        if ev is SENTINEL:
            break
        assert isinstance(ev, dict)
        etype = ev.get("type")
        if etype == "final":
            final_seen = True
            yield ev.get("text", "")
        elif etype == "error" and not final_seen:
            yield f"\n\n⚠️ {ev.get('agent', '')}: {ev.get('error')}".rstrip()
        elif _STREAM_TRACE:
            line = _trace_line(ev)
            if line:
                yield line


def build_host():
    """Construct the Responses protocol host with our single whole-solution handler."""
    from azure.ai.agentserver.responses import (
        ResponsesAgentServerHost,
        ResponsesServerOptions,
        TextResponse,
    )
    from .settings import get_settings

    options = ResponsesServerOptions(default_model=get_settings().chat_deployment)
    host = ResponsesAgentServerHost(options=options)

    # The Responses host requires exactly three positional params, in this order:
    #   (request, context, cancellation_signal).
    @host.response_handler
    async def handle(request, context, cancellation_signal):  # noqa: ANN001, ARG001
        message = (await context.get_input_text()) or ""
        session_id, user_id = _resolve_ids(context, request)
        logger.info("hosted turn: session=%s user=%s msg=%r", session_id, user_id, message[:80])
        return TextResponse(
            context, request, text=_stream_answer(session_id, user_id, message)
        )

    return host


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logger.info(
        "Starting hosted whole-solution agent on :%s (SPECIALIST_MODE=%s, trace=%s)",
        _PORT, os.getenv("SPECIALIST_MODE"), _STREAM_TRACE,
    )
    build_host().run(port=_PORT)


if __name__ == "__main__":
    main()
