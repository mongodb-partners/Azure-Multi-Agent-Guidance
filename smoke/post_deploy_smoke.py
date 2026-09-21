"""Post-deploy smoke check — run after deploy.sh against the live API.

Reads API_URL from the environment or ../.env.live, then:
  1. GET /health          → status ok
  2. GET /agents          → the four specialists present
  3. POST /chat (SSE)     → a non-empty final answer streams back

Exits non-zero on any failure so deploy.sh can fail loudly.

Usage:
  python smoke/post_deploy_smoke.py                # uses .env.live
  API_URL=https://... python smoke/post_deploy_smoke.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

REQUIRED_AGENTS = {"orchestrator", "job-match", "career-coach", "company-intel"}


def _load_api_url() -> str:
    if os.getenv("API_URL"):
        return os.environ["API_URL"].rstrip("/")
    live = Path(__file__).resolve().parent.parent / ".env.live"
    if live.exists():
        for line in live.read_text().splitlines():
            if line.startswith("API_URL="):
                return line.split("=", 1)[1].strip().rstrip("/")
    print("ERROR: API_URL not set and .env.live has no API_URL", file=sys.stderr)
    sys.exit(2)


def _check_health(base: str) -> None:
    r = httpx.get(f"{base}/health", timeout=20)
    r.raise_for_status()
    assert r.json().get("status") == "ok", "health status != ok"
    print("  ✓ /health ok")


def _check_agents(base: str) -> None:
    r = httpx.get(f"{base}/agents", timeout=20)
    r.raise_for_status()
    names = {a["name"] for a in r.json()["agents"]}
    missing = REQUIRED_AGENTS - names
    assert not missing, f"missing agents: {missing}"
    print(f"  ✓ /agents ok ({len(names)} agents)")


def _chat_events(base: str, payload: dict):
    """Yield (event, data) pairs from one /chat SSE stream."""
    with httpx.stream("POST", f"{base}/chat", json=payload, timeout=240) as r:
        r.raise_for_status()
        current = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                current = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                yield current, json.loads(line.split(":", 1)[1].strip())


def _check_chat(base: str) -> None:
    """Two probes (specs/004 §8):
    1. small talk → non-empty final answer (reasoning optional: non-reasoning
       deployments legitimately emit none);
    2. specialist-routed → at least one `reasoning` event plus a non-empty final."""
    payload = {"message": "hello", "session_id": "smoke", "user_id": "smoke"}
    saw_final = any(
        ev == "final" and data.get("text") for ev, data in _chat_events(base, payload)
    )
    assert saw_final, "no non-empty final answer streamed"
    print("  ✓ /chat streamed a final answer")

    payload = {
        "message": "Which remote senior Python jobs mention MongoDB?",
        "session_id": "smoke-specialist",
        "user_id": "smoke",
    }
    saw_reasoning = saw_final = False
    for ev, data in _chat_events(base, payload):
        if ev == "reasoning" and data.get("text"):
            saw_reasoning = True
        elif ev == "final" and data.get("text"):
            saw_final = True
    assert saw_final, "specialist probe: no non-empty final answer streamed"
    assert saw_reasoning, "specialist probe: no reasoning events streamed"
    print("  ✓ /chat specialist turn streamed live reasoning")


def main() -> int:
    base = _load_api_url()
    print(f"Smoke testing {base} …")
    try:
        _check_health(base)
        _check_agents(base)
        _check_chat(base)
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ SMOKE FAILED: {e}", file=sys.stderr)
        return 1
    print("Smoke test PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
