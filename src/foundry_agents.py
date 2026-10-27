"""Foundry specialists — hosted on the **Azure AI Foundry Agent Service**.

Each routable agent in `config/agents/` is registered as a persistent Foundry prompt agent
(see `foundry_registry.py` / `scripts/provision_agents.py`) and invoked by name. The Foundry
runtime executes the agent, including its MongoDB MCP tool calls, and auto-approves them.

Foundry prompt agents are Responses-based, so they support the reasoning-class chat models
(gpt-5.x) and hosted MCP natively — no classic Agents threads+runs top_p trap.

Execution is controlled by `SPECIALIST_MODE`:
  - registered-or-inline (default) — invoke the registered Foundry agent; if the Foundry
    resource/SDK is unavailable, fall back to a raw Responses turn in-process (inline).
  - registered — strict: only the registered Foundry agent (error if unavailable).
  - inline / disabled — always run inline via the raw Responses API (no Foundry registration).

`stream_query` yields structured `Event`s (status / reasoning / delta / tool_call / message /
error) so the FastAPI layer can relay them over SSE and the UI can render a trace *and* the
answer live. `reasoning` and `delta` events carry incremental reasoning/answer token chunks as
they arrive; `message` still carries the full accumulated answer at completion. The event
interface is identical across modes, so the orchestrator and UI need no changes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterator

from .config_loader import (
    AgentConfig,
    build_agent_instructions,
    load_agents,
    load_skills,
)
from .mcp_client import mcp_tool_dict
from .settings import get_settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _responses_client():
    """OpenAI client wired to the Azure OpenAI `/openai/v1` route (Responses-capable).

    Prefers an API key; falls back to DefaultAzureCredential (az login) bearer auth via the
    AzureOpenAI client, which also exposes `.responses`.
    """
    s = get_settings()
    base = s.aoai_base or (s.project_endpoint.rstrip("/"))
    if not base:
        raise RuntimeError(
            "No Azure OpenAI endpoint configured. Set AZURE_OPENAI_ENDPOINT (or "
            "AZURE_AI_PROJECT_ENDPOINT)."
        )
    if s.aoai_api_key:
        from openai import OpenAI

        return OpenAI(base_url=f"{base}/openai/v1", api_key=s.aoai_api_key)

    # Keyless: DefaultAzureCredential → bearer token; AzureOpenAI also supports .responses.
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI

    provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    return AzureOpenAI(
        azure_endpoint=base,
        azure_ad_token_provider=provider,
        api_version="2025-03-01-preview",
    )


@dataclass
class Event:
    type: str            # "status" | "reasoning" | "delta" | "tool_call" | "message" | "error"
    agent: str
    data: dict[str, Any]


class _FallbackToInline(Exception):
    """Signals the registered path is unusable *before any event is emitted* — safe to retry inline."""


def _explain_error(err: Any) -> str:
    """Turn an opaque Responses/stream error into an actionable message.

    A 424 "Failed Dependency" means the agent's MongoDB MCP tool call was rejected by the
    self-hosted MCP server — usually the MCP endpoint is unreachable (wrong `MONGODB_MCP_URL` /
    Container App down) or the MCP API-key header is missing/incorrect. Surface that instead of a
    bare `424: None`.
    """
    text = str(err) if err is not None else ""
    code = getattr(err, "code", None) or getattr(err, "status_code", None)
    blob = f"{text} {code or ''}"
    if "424" in blob or "Failed Dependency" in blob:
        return (
            f"{text or '424 Failed Dependency'} — the MongoDB MCP tool call was rejected. "
            "Check that MONGODB_MCP_URL points at the running self-hosted MCP Container App and, "
            "if the endpoint requires it, that MCP_API_KEY / MCP_API_KEY_HEADER are set correctly."
        )
    return text or repr(err)


def _record_response_usage(resp: Any) -> None:
    """Record one specialist LLM call + its token usage into the in-memory metrics store."""
    try:
        from . import telemetry

        telemetry.record_llm_call()  # one specialist Responses turn
        usage = getattr(resp, "usage", None)
        if not usage:
            return
        # Responses API usage fields: input_tokens / output_tokens / total_tokens.
        prompt = int(getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(
            getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0) or 0
        )
        total = int(getattr(usage, "total_tokens", 0) or (prompt + completion))
        telemetry.record_tokens(prompt, kind="prompt")
        telemetry.record_tokens(completion, kind="completion")
        telemetry.note_tokens(prompt=prompt, completion=completion, total=total)
    except Exception:  # noqa: BLE001
        pass


class FoundrySpecialist:
    """One specialist: a config-driven Responses turn bound to the MongoDB MCP tool."""

    def __init__(self, cfg: AgentConfig, instructions: str, client=None):
        self.cfg = cfg
        self.instructions = instructions
        self.client = client or _responses_client()

    @property
    def name(self) -> str:
        return self.cfg.name

    def stream_query(
        self, query: str, previous_response_id: str | None = None
    ) -> Iterator[Event]:
        """Run one specialist turn, streaming events, dispatched by `SPECIALIST_MODE`.

        - ``registered-or-inline`` (default): invoke the registered Foundry prompt agent; if the
          registered path is unavailable *before any event is emitted*, fall back to inline.
        - ``registered``: strict — emit an error Event if the registered path can't start.
        - ``inline`` / ``disabled``: always run inline via the raw Responses API.

        Both paths funnel through `_consume_stream`, so the emitted `Event`s are identical and the
        orchestrator + UI need no changes. `previous_response_id` chains turns server-side
        (optional; the orchestrator threads history into the prompt, so it's unused by default).
        """
        mode = (get_settings().specialist_mode or "registered-or-inline").lower()

        if mode in ("inline", "disabled"):
            yield from self._stream_query_inline(query, previous_response_id)
            return

        # registered / registered-or-inline: try registered first.
        try:
            yield from self._stream_query_registered(query, previous_response_id)
            return
        except _FallbackToInline as fb:
            if mode == "registered":
                yield Event("error", self.name, {"error": f"registered agent unavailable: {fb}"})
                return
            logger.warning(
                "Specialist %s: registered path unavailable (%s) — falling back to inline.",
                self.name, fb,
            )
            yield from self._stream_query_inline(query, previous_response_id)

    def _stream_query_inline(
        self, query: str, previous_response_id: str | None = None
    ) -> Iterator[Event]:
        """Inline path: a raw Responses turn with the MCP tool attached to this process's client.

        The MCP tool is built from `mcp_tool_dict()` each turn. Auth to the self-hosted MCP is a
        static API-key header (no expiring token), so there is nothing to refresh.
        """
        kwargs: dict[str, Any] = {
            "model": self.cfg.model or get_settings().chat_deployment,
            "instructions": self.instructions,
            "input": query,
            "tools": [mcp_tool_dict()],
            "stream": True,
            # Ask reasoning-class models (gpt-5.x / o-series) to stream a reasoning summary so the
            # UI can show live "thinking". Non-reasoning models (e.g. gpt-4o) reject this param —
            # it is stripped and retried transparently below.
            "reasoning": {"summary": "auto"},
        }
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id

        stream = self._create_stream_with_fallback(kwargs)
        if stream is None:
            # _create_stream_with_fallback yielded an error Event already? No — it returns None
            # only on unrecoverable error, which we surface here.
            yield Event("error", self.name, {"error": self._last_stream_error or "stream failed"})
            return
        yield from self._consume_stream(stream)

    _last_stream_error: str | None = None

    def _create_stream_with_fallback(self, kwargs: dict[str, Any]) -> Any:
        """Open a Responses stream, transparently dropping params the deployment rejects.

        Reasoning-class models accept ``reasoning={"summary": "auto"}``; classic models (gpt-4o)
        return a 400 for it. Rather than branch on model name (which is brittle across gpt-4o /
        gpt-5.x / o-series), we optimistically request the summary and strip it on rejection.
        """
        from openai import BadRequestError

        self._last_stream_error = None
        for _ in range(3):
            try:
                return self.client.responses.create(**kwargs)
            except BadRequestError as e:
                msg = str(getattr(e, "message", "") or e)
                if "reasoning" in kwargs and "reasoning" in msg.lower():
                    kwargs.pop("reasoning", None)  # model doesn't support reasoning summaries
                    continue
                self._last_stream_error = _explain_error(e)
                return None
            except Exception as e:  # noqa: BLE001
                self._last_stream_error = _explain_error(e)
                return None
        # Exhausted retries.
        try:
            return self.client.responses.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            self._last_stream_error = _explain_error(e)
            return None

    def _stream_query_registered(
        self, query: str, previous_response_id: str | None = None
    ) -> Iterator[Event]:
        """Registered path: invoke the persistent Foundry prompt agent by name (identical events).

        Setup failures (SDK missing, agent not registered, resource unreachable) raise
        `_FallbackToInline` *before any Event is yielded*, so `stream_query` can retry inline
        cleanly. Once the stream opens, mid-stream failures surface as error Events like inline.
        """
        try:
            from .foundry_registry import FoundryUnavailable, get_agent_registry
        except ImportError as e:  # azure-ai-projects not installed
            raise _FallbackToInline(str(e))

        try:
            reg = get_agent_registry()
            stream = reg.invoke_by_reference(
                self.name,
                input=query,
                previous_response_id=previous_response_id,
                stream=True,
            )
        except FoundryUnavailable as e:
            raise _FallbackToInline(str(e))
        except Exception as e:  # noqa: BLE001 - not-found / auth / network → try inline
            raise _FallbackToInline(f"{type(e).__name__}: {e}")

        yield Event("status", self.name, {"status": "in_progress", "source": "registered"})
        yield from self._consume_stream(stream)

    def _consume_stream(self, stream: Any) -> Iterator[Event]:
        """Shared Responses-stream → Event mapping used by both the inline and registered paths.

        Answer text and model reasoning are forwarded *live* as they arrive:
          - ``response.output_text.delta``        → ``delta``     Event (answer token chunk)
          - ``response.reasoning*.delta``         → ``reasoning`` Event (reasoning token chunk)
        The accumulated text is still emitted as a final ``message`` Event at completion so the
        orchestrator has the full answer for persistence / synthesis.
        """
        seen_calls: set[str] = set()
        text_buf: list[str] = []
        final_text = ""
        response_id = None

        try:
            for event in stream:
                etype = getattr(event, "type", "") or ""

                if etype == "response.created":
                    response_id = getattr(getattr(event, "response", None), "id", None)
                    yield Event("status", self.name, {"status": "in_progress",
                                                       "response_id": response_id})

                elif etype == "response.output_item.added":
                    tc = self._tool_call_event(getattr(event, "item", None), seen_calls)
                    if tc:
                        yield tc

                elif etype == "response.output_item.done":
                    # mcp_call name is reliably present by "done"; catch any we missed.
                    tc = self._tool_call_event(getattr(event, "item", None), seen_calls)
                    if tc:
                        yield tc

                elif etype == "response.output_text.delta":
                    delta = getattr(event, "delta", "") or ""
                    if delta:
                        text_buf.append(delta)
                        yield Event("delta", self.name, {"text": delta})

                # Reasoning-class models (gpt-5.x) stream a reasoning summary and/or raw
                # reasoning as separate delta events; forward whichever the deployment emits.
                elif etype in (
                    "response.reasoning_summary_text.delta",
                    "response.reasoning_text.delta",
                    "response.reasoning.delta",
                ):
                    delta = getattr(event, "delta", "") or ""
                    if delta:
                        yield Event("reasoning", self.name, {"text": delta})

                elif etype == "response.completed":
                    resp = getattr(event, "response", None)
                    final_text = getattr(resp, "output_text", "") or "".join(text_buf)
                    response_id = getattr(resp, "id", response_id)
                    _record_response_usage(resp)

                elif etype in ("response.failed", "response.incomplete", "error"):
                    resp = getattr(event, "response", None)
                    err = getattr(resp, "error", None) or getattr(event, "error", None) \
                        or etype
                    yield Event("error", self.name, {"error": _explain_error(err)})
                    return
        except Exception as e:  # noqa: BLE001
            yield Event("error", self.name, {"error": _explain_error(e)})
            return

        if not final_text:
            final_text = "".join(text_buf)
        yield Event("message", self.name, {"text": final_text, "response_id": response_id})

    def _tool_call_event(self, item: Any, seen: set[str]) -> Event | None:
        """Emit a tool_call Event for a newly-seen mcp_call output item, else None."""
        if item is None:
            return None
        if getattr(item, "type", "") != "mcp_call":
            return None
        cid = getattr(item, "id", None) or ""
        if cid in seen:
            return None
        seen.add(cid)
        return Event(
            "tool_call",
            self.name,
            {"tool": getattr(item, "name", "mcp"), "id": cid,
             # mcp_call items carry the tool arguments as a JSON string; may be absent.
             "args": getattr(item, "arguments", None)},
        )

    def query(self, query: str, previous_response_id: str | None = None) -> str:
        """Non-streaming convenience: return just the final answer text."""
        text = ""
        for ev in self.stream_query(query, previous_response_id):
            if ev.type == "message":
                text = ev.data.get("text", "")
            elif ev.type == "error":
                raise RuntimeError(ev.data.get("error"))
        return text


class SpecialistRegistry:
    """Builds and caches one FoundrySpecialist per routable agent config (shared client)."""

    def __init__(self):
        self.agents_cfg = load_agents()
        self.skills = load_skills()
        self.client = _responses_client()
        self._cache: dict[str, FoundrySpecialist] = {}

    def get(self, name: str) -> FoundrySpecialist:
        if name not in self._cache:
            cfg = self.agents_cfg[name]
            instructions = build_agent_instructions(cfg, self.skills)
            self._cache[name] = FoundrySpecialist(cfg, instructions, self.client)
        return self._cache[name]

    def routable_names(self) -> list[str]:
        return [n for n, a in self.agents_cfg.items() if a.routable]


_registry: SpecialistRegistry | None = None


def get_registry() -> SpecialistRegistry:
    global _registry
    if _registry is None:
        _registry = SpecialistRegistry()
    return _registry


def _smoke_test() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    reg = get_registry()
    prompts = {
        "job-match": "Find remote senior Python roles that fit a MongoDB + Azure background.",
        "career-coach": "How do I move from backend engineer to staff engineer?",
        "company-intel": "Tell me about companies with a strong remote-first culture.",
    }
    for name in reg.routable_names():
        print(f"\n=== {name} ===")
        specialist = reg.get(name)
        tools_used: list[str] = []
        answer = ""
        for ev in specialist.stream_query(prompts.get(name, "Introduce yourself.")):
            if ev.type == "tool_call":
                tools_used.append(ev.data.get("tool", "?"))
            elif ev.type == "message":
                answer = ev.data.get("text", "")
            elif ev.type == "error":
                print("ERROR:", ev.data.get("error"))
        print("tools:", tools_used or "(none)")
        print((answer[:500] or "(no answer)"))


if __name__ == "__main__":
    import sys

    if "--smoke-test" in sys.argv:
        _smoke_test()
    else:
        print("Use --smoke-test to probe the routable specialists over the Responses API.")
