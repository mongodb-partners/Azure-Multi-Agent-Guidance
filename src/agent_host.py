"""Per-agent Foundry HOSTED server — serves exactly ONE agent on Foundry-managed compute.

Unlike `hosted_agent.py` (one container running the whole solution with specialists inline),
this module packages a SINGLE agent per container. Four containers are built from this one
module, selected at runtime by the `CAREER_AGENT` env var (NOT `AGENT_*` — Foundry reserves the
`AGENT_*` and `FOUNDRY_*` prefixes for platform use, so hosted containers may not set them):

    CAREER_AGENT=job-match | career-coach | company-intel   → a specialist container
    CAREER_AGENT=orchestrator                                → the router container

Each container speaks the Responses protocol on `HOSTED_PORT` (default 8088) via
`azure-ai-agentserver-responses`, so Foundry runs it as a managed hosted agent and any
Responses client can invoke it by reference.

Behaviour by role:
  • specialist  — runs its config-driven `FoundrySpecialist` (a Responses turn bound to the
                  MongoDB MCP tool). Answers are grounded strictly in MongoDB (see the grounding
                  contract injected by `config_loader.build_agent_instructions`).
  • orchestrator — classifies the request and invokes the SPECIALIST HOSTED AGENTS *by reference*
                   (cross-agent invocation via `AIProjectClient.get_openai_client`), then
                   synthesizes if two specialists answered. It never answers substantive
                   questions itself; greetings/capability replies only.

No prompt agents are used anywhere in this path.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, AsyncIterator

# Silence the agent-server's OpenTelemetry Azure-IMDS probe when running off-Azure, but keep
# telemetry when App Insights is configured (mirrors hosted_agent.py).
if not os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")

logger = logging.getLogger("agent_host")

_PORT = int(os.getenv("HOSTED_PORT", "8088"))
_AGENT_NAME = os.getenv("CAREER_AGENT", "").strip()

# Canned refusal used when the router finds no specialist and the request is substantive.
_NO_DATA = "I couldn't find any relevant information in the MongoDB database for that request."


# --------------------------------------------------------------------------- #
# Specialist role — run the config-driven FoundrySpecialist (MongoDB MCP grounded)
# --------------------------------------------------------------------------- #
def _stream_specialist(agent_name: str, message: str) -> AsyncIterator[str]:
    """Stream one specialist's answer. Forced inline so this container is self-contained."""
    # This container IS the specialist; run it inline (never try to call a registered agent).
    os.environ["SPECIALIST_MODE"] = "inline"
    from .foundry_agents import get_registry

    async def _gen() -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        q: "asyncio.Queue[dict[str, Any] | object]" = asyncio.Queue()
        SENTINEL = object()

        def worker() -> None:
            try:
                spec = get_registry().get(agent_name)
                for ev in spec.stream_query(message):
                    loop.call_soon_threadsafe(q.put_nowait, {"type": ev.type, **ev.data})
            except Exception as e:  # noqa: BLE001
                loop.call_soon_threadsafe(q.put_nowait, {"type": "error", "error": str(e)})
            finally:
                loop.call_soon_threadsafe(q.put_nowait, SENTINEL)

        threading.Thread(target=worker, daemon=True).start()

        answered = False
        while True:
            ev = await q.get()
            if ev is SENTINEL:
                break
            assert isinstance(ev, dict)
            if ev.get("type") == "message":
                answered = True
                yield ev.get("text", "")
            elif ev.get("type") == "error" and not answered:
                yield f"\u26a0\ufe0f {ev.get('error')}"
        if not answered:
            yield _NO_DATA

    return _gen()


# --------------------------------------------------------------------------- #
# Orchestrator role — route to the specialist HOSTED agents by reference
# --------------------------------------------------------------------------- #
def _invoke_specialist_hosted(agent_name: str, message: str) -> str:
    """Invoke a specialist HOSTED agent by reference and return its final text.

    Uses the Foundry project's OpenAI client bound to the target hosted agent, so the call
    runs on that specialist's managed compute (true cross-agent invocation, no prompt agents).
    """
    from .foundry_registry import get_agent_registry

    reg = get_agent_registry()
    oai = reg.client().get_openai_client(agent_name=agent_name)
    resp = oai.responses.create(input=message)
    return (getattr(resp, "output_text", "") or "").strip()


def _stream_orchestrator(message: str, session_id: str, user_id: str) -> AsyncIterator[str]:
    """Classify, invoke specialist hosted agents by reference, synthesize; stream final text."""
    from . import memory
    from .classifier import classify
    from .llm import chat_text

    async def _gen() -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()

        def work() -> str:
            cls = classify(message)
            history = memory.history_as_text(session_id)
            facts = memory.facts_as_text(user_id)

            # No specialist → greeting/capability/clarify only (never substantive from own knowledge).
            if not cls.specialists:
                from .config_loader import load_agents

                orch = next((a for a in load_agents().values() if not a.routable), None)
                system = orch.instructions if orch else "You are a helpful career-assistant router."
                ctx = ""
                if facts:
                    ctx += f"\n\n## Known facts about the user\n{facts}"
                if history:
                    ctx += f"\n\n## Recent conversation\n{history}"
                reply = chat_text(
                    [
                        {"role": "system", "content": system + ctx},
                        {"role": "user", "content": message},
                    ],
                    temperature=0.4,
                    max_tokens=800,
                )
                _persist(session_id, user_id, message, reply)
                return reply

            # Augment with memory so the specialist hosted agents see conversation context.
            augmented = _augment(message, history, facts)
            answers: dict[str, str] = {}
            for spec in cls.specialists:
                try:
                    ans = _invoke_specialist_hosted(spec, augmented)
                    if ans:
                        answers[spec] = ans
                except Exception as e:  # noqa: BLE001
                    logger.warning("specialist %s failed: %s", spec, e)

            if not answers:
                final = _NO_DATA
            elif len(answers) == 1:
                final = next(iter(answers.values()))
            else:
                final = _synthesize(message, answers, chat_text)
            _persist(session_id, user_id, message, final)
            return final

        text = await loop.run_in_executor(None, work)
        yield text

    return _gen()


def _augment(query: str, history: str, facts: str) -> str:
    parts = []
    if facts:
        parts.append(f"## Known facts about the user\n{facts}")
    if history:
        parts.append(f"## Recent conversation\n{history}")
    parts.append(f"## Current request\n{query}")
    return "\n\n".join(parts)


def _synthesize(query: str, answers: dict[str, str], chat_text) -> str:
    joined = "\n\n".join(f"### {name} said:\n{ans}" for name, ans in answers.items())
    return chat_text(
        [
            {
                "role": "system",
                "content": (
                    "You merge specialist answers into ONE cohesive, non-repetitive reply for the "
                    "user. Preserve concrete details (job titles, companies, skills). Use ONLY the "
                    "content the specialists provided (which is grounded in MongoDB); do not add "
                    "any outside knowledge. Keep it tight."
                ),
            },
            {"role": "user", "content": f"User asked: {query}\n\n{joined}"},
        ],
        temperature=0.3,
        max_tokens=2000,
    )


def _persist(session_id: str, user_id: str, message: str, final: str) -> None:
    try:
        from . import memory

        memory.append_message(session_id, "user", message, user_id=user_id)
        memory.append_message(session_id, "assistant", final, user_id=user_id)
        memory.extract_and_store_facts(user_id, message, final)
    except Exception as e:  # noqa: BLE001
        logger.warning("persist failed: %s", e)


# --------------------------------------------------------------------------- #
# Responses protocol host
# --------------------------------------------------------------------------- #
def _resolve_ids(context: Any, request: Any) -> tuple[str, str]:
    meta: dict[str, Any] = {}
    try:
        meta = dict(request.get("metadata") or {}) if request is not None else {}
    except Exception:  # noqa: BLE001
        meta = {}
    session_id = getattr(context, "session_id", "") or meta.get("session_id") or "default"
    user_id = meta.get("user_id") or "hosted-user"
    return session_id, user_id


def build_host():
    """Construct the Responses host serving the single agent named by CAREER_AGENT."""
    from azure.ai.agentserver.responses import (
        ResponsesAgentServerHost,
        ResponsesServerOptions,
        TextResponse,
    )

    from .config_loader import load_agents
    from .settings import get_settings

    if not _AGENT_NAME:
        raise RuntimeError("CAREER_AGENT env var is required (job-match|career-coach|company-intel|orchestrator).")
    agents = load_agents()
    if _AGENT_NAME not in agents:
        raise RuntimeError(f"Unknown CAREER_AGENT={_AGENT_NAME!r}. Known: {', '.join(sorted(agents))}.")
    is_orchestrator = not agents[_AGENT_NAME].routable

    options = ResponsesServerOptions(default_model=get_settings().chat_deployment)
    host = ResponsesAgentServerHost(options=options)

    @host.response_handler
    async def handle(request, context, cancellation_signal):  # noqa: ANN001, ARG001
        message = (await context.get_input_text()) or ""
        session_id, user_id = _resolve_ids(context, request)
        logger.info("hosted[%s] turn: session=%s msg=%r", _AGENT_NAME, session_id, message[:80])
        if is_orchestrator:
            stream = _stream_orchestrator(message, session_id, user_id)
        else:
            stream = _stream_specialist(_AGENT_NAME, message)
        return TextResponse(context, request, text=stream)

    return host


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logger.info("Starting hosted agent %r on :%s", _AGENT_NAME, _PORT)
    build_host().run(port=_PORT)


if __name__ == "__main__":
    main()
