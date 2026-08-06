"""Foundry specialists — Azure OpenAI **Responses API** + hosted MongoDB MCP tool.

Each routable agent in `config/agents/` becomes one specialist that runs a single Responses
turn: `client.responses.create(model, instructions, input, tools=[mongodb-mcp])`. The service
connects to the MongoDB MCP Container App itself and auto-approves tool calls.

Why Responses and not the classic Agents threads+runs API: the deployed chat model is a
reasoning-class deployment (gpt-5.x), and the Agents run loop force-injects `top_p`, which
those models reject (`invalid_prompt: 'top_p' is not supported`). The Responses API supports
reasoning models and hosted MCP natively, so specialists use it directly.

`stream_query` yields structured `Event`s (status / tool_call / message / error) so the
FastAPI layer can relay them over SSE and the UI can render a trace. The event interface is
unchanged from the previous implementation, so the orchestrator needs no changes.
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
    type: str            # "status" | "tool_call" | "message" | "error"
    agent: str
    data: dict[str, Any]


class _FallbackToInline(Exception):
    """Signals the registered path is unusable *before any event is emitted* — safe to retry inline."""


class FoundrySpecialist:
    """One specialist: a config-driven Responses turn bound to the MongoDB MCP tool."""

    def __init__(self, cfg: AgentConfig, instructions: str, client=None):
        self.cfg = cfg
        self.instructions = instructions
        self.client = client or _responses_client()
        self.mcp = mcp_tool_dict()

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
        """Inline path: a raw Responses turn with the MCP tool attached to this process's client."""
        kwargs: dict[str, Any] = {
            "model": self.cfg.model or get_settings().chat_deployment,
            "instructions": self.instructions,
            "input": query,
            "tools": [self.mcp],
            "stream": True,
        }
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id

        try:
            stream = self.client.responses.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            yield Event("error", self.name, {"error": str(e)})
            return
        yield from self._consume_stream(stream)

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
        """Shared Responses-stream → Event mapping used by both the inline and registered paths."""
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
                    text_buf.append(delta)

                elif etype == "response.completed":
                    resp = getattr(event, "response", None)
                    final_text = getattr(resp, "output_text", "") or "".join(text_buf)
                    response_id = getattr(resp, "id", response_id)

                elif etype in ("response.failed", "response.incomplete", "error"):
                    resp = getattr(event, "response", None)
                    err = getattr(resp, "error", None) or getattr(event, "error", None) \
                        or etype
                    yield Event("error", self.name, {"error": str(err)})
                    return
        except Exception as e:  # noqa: BLE001
            yield Event("error", self.name, {"error": str(e)})
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
            {"tool": getattr(item, "name", "mcp"), "id": cid},
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
