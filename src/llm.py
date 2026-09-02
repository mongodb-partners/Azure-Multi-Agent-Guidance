"""Thin Azure OpenAI chat helper shared by classifier / synthesizer / fact-extractor.

These three "utility LLM" calls (routing tie-break, multi-answer synthesis, fact extraction)
are small, synchronous, and deployment-based — so we use the `openai` AzureOpenAI client
directly rather than a full agent. Auth prefers an API key when provided, else falls back
to DefaultAzureCredential (az login) via a bearer-token provider.

The specialist *agents* themselves run on Foundry Agent Service (see foundry_agents.py); this
helper is only for the lightweight orchestration glue.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from .settings import get_settings


@lru_cache(maxsize=1)
def _chat_client():
    from openai import AzureOpenAI

    s = get_settings()
    # Prefer explicit Azure OpenAI endpoint (normalized); fall back to the Foundry project route.
    endpoint = s.aoai_base or s.project_endpoint
    if not endpoint:
        raise RuntimeError(
            "No chat endpoint configured. Set AZURE_OPENAI_ENDPOINT (or AZURE_AI_PROJECT_ENDPOINT)."
        )
    if s.aoai_api_key:
        return AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=s.aoai_api_key,
            api_version=s.aoai_api_version,
        )
    # Keyless: DefaultAzureCredential → bearer token provider
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    return AzureOpenAI(
        azure_endpoint=endpoint,
        azure_ad_token_provider=provider,
        api_version=s.aoai_api_version,
    )


def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    max_tokens: int = 2000,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    deployment: str | None = None,
    reasoning_effort: str | None = "low",
) -> Any:
    """Return the raw first-choice message object (has .content and .tool_calls).

    Reasoning-class deployments (e.g. gpt-5.x) reject `max_tokens` and non-default
    `temperature`. Critically, their `max_completion_tokens` budget covers *reasoning
    tokens + output* — so a small cap can be fully consumed by reasoning, leaving empty
    content (`finish_reason="length"`). For these lightweight utility calls we therefore
    default to `reasoning_effort="low"` (≈0 reasoning tokens → the budget goes to output)
    and a generous cap, then transparently retry with any rejected parameter stripped so
    the same helper works across classic and reasoning models.
    """
    s = get_settings()
    kwargs: dict[str, Any] = {
        "model": deployment or s.classifier_deployment,
        "messages": messages,
        "temperature": temperature,
        # Reasoning models use max_completion_tokens; classic models accept it too.
        "max_completion_tokens": max_tokens,
    }
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    if tools:
        kwargs["tools"] = tools
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice

    from openai import BadRequestError

    for _ in range(4):
        try:
            resp = _chat_client().chat.completions.create(**kwargs)
            choice = resp.choices[0]
            # Reasoning ate the whole budget → empty output. Retry with a bigger cap
            # (and low effort, if the deployment ignored/​rejected the effort hint).
            if (
                not (choice.message.content or "").strip()
                and not choice.message.tool_calls
                and choice.finish_reason == "length"
            ):
                kwargs["max_completion_tokens"] = min(
                    kwargs["max_completion_tokens"] * 2, 16000
                )
                kwargs.setdefault("reasoning_effort", "low")
                continue
            _record_usage(getattr(resp, "usage", None))
            return choice.message
        except BadRequestError as e:
            param = _unsupported_param(e)
            if param and param in kwargs:
                # Drop the parameter this model doesn't accept and retry.
                kwargs.pop(param, None)
                continue
            # Some models reject only *non-default* temperature — retry at default.
            if param == "temperature" or "temperature" in str(e):
                if kwargs.get("temperature") not in (None, 1):
                    kwargs["temperature"] = 1
                    continue
            raise
    # Final attempt (raises if still failing).
    resp = _chat_client().chat.completions.create(**kwargs)
    return resp.choices[0].message


def _record_usage(usage: Any) -> None:
    """Emit token usage to telemetry (OTel metric) + the in-memory metrics store."""
    try:
        from . import telemetry

        telemetry.record_llm_call()  # one utility LLM invocation happened
        if not usage:
            return
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        total = int(getattr(usage, "total_tokens", 0) or (prompt + completion))
        telemetry.record_tokens(prompt, kind="prompt")
        telemetry.record_tokens(completion, kind="completion")
        telemetry.note_tokens(prompt=prompt, completion=completion, total=total)
    except Exception:  # noqa: BLE001
        pass


def _unsupported_param(err: Any) -> str | None:
    """Extract the offending parameter name from an OpenAI 400 error, if present.

    Prefers the structured `param` field. Some Azure OpenAI models (e.g. gpt-4o)
    return `param: null` with a message like "Unrecognized request argument
    supplied: reasoning_effort", so we also parse the parameter name out of the
    message text as a fallback.
    """
    body = getattr(err, "body", None)
    if isinstance(body, dict):
        p = body.get("param") or (body.get("error") or {}).get("param")
        if p:
            return str(p)

    import re

    msg = str(getattr(err, "message", "") or err)
    m = re.search(r"[Uu]nrecognized request argument supplied:\s*([A-Za-z0-9_]+)", msg)
    if m:
        return m.group(1)
    m = re.search(r"[Uu]nknown parameter:\s*'?([A-Za-z0-9_]+)'?", msg)
    if m:
        return m.group(1)
    return None


def chat_text(messages: list[dict[str, str]], **kwargs: Any) -> str:
    """Convenience: return just the assistant text content."""
    msg = chat(messages, **kwargs)
    return (msg.content or "").strip()


def chat_json(messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
    """Ask for and parse a JSON object response (best-effort)."""
    txt = chat_text(messages, **kwargs)
    txt = txt.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return {}
