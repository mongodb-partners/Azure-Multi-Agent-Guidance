"""E2E tests against a running API (local or deployed).

Gated: run with `RUN_E2E=1 API_URL=http://localhost:3000 pytest -m e2e`.
Validates the HTTP surface and the SSE event contract the UI depends on.
"""
from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.e2e]

httpx = pytest.importorskip("httpx")


def test_health(api_url):
    r = httpx.get(f"{api_url}/health", timeout=15)
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "ok"
    assert "specialist_mode" in body


def test_agents_listed(api_url):
    r = httpx.get(f"{api_url}/agents", timeout=15)
    r.raise_for_status()
    names = {a["name"] for a in r.json()["agents"]}
    # The four shipped specialists.
    assert {"orchestrator", "job-match", "career-coach", "company-intel"} <= names


def test_chat_sse_contract(api_url):
    """A /chat turn must stream a `final` event and end with `done`."""
    payload = {"message": "hello", "session_id": "e2e-test", "user_id": "e2e"}
    seen_types: list[str] = []
    saw_final = False
    with httpx.stream("POST", f"{api_url}/chat", json=payload, timeout=120) as r:
        r.raise_for_status()
        current_event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
                seen_types.append(current_event)
            elif line.startswith("data:") and current_event == "final":
                data = json.loads(line.split(":", 1)[1].strip())
                assert data.get("text")
                saw_final = True
    assert saw_final, f"no final event; saw {seen_types}"
    assert "done" in seen_types
