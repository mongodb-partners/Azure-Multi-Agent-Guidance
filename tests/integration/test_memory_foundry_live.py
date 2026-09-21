"""Live integration tests for the Foundry Conversations memory backend (specs/001, R2).

Gated: run with `RUN_INTEGRATION=1`, a populated `.env` (MONGODB_URI +
AZURE_AI_PROJECT_ENDPOINT), the Foundry stack installed
(`pip install -r requirements-foundry.txt`), and `az login` (DefaultAzureCredential).

These exercise plan Phase 5 acceptance criteria directly against real services:
  AC1-ish  two-turn round-trip: history written in one "turn" is readable for the next
  AC6      a fresh process/module state still resolves the same conversation
           (mapping persists in Atlas `chat_sessions`)
Cleanup removes the Atlas mapping/metadata; the empty Foundry conversation itself is
left behind (spec Q1: no cleanup in v1, platform retention applies).
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.integration]

pytest.importorskip("azure.ai.projects", reason="Foundry stack not installed")


@pytest.fixture
def sid():
    return f"itest-foundry-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _force_foundry_backend(monkeypatch):
    from src.settings import Settings

    # Facade dispatch reads src.memory.get_settings; _foundry's client factory reads the
    # real settings (project_endpoint) — patch only the facade seam.
    monkeypatch.setattr("src.memory.get_settings",
                        lambda: Settings(short_term_memory_backend="foundry"))


def _cleanup(sid: str) -> None:
    import src.memory as memory

    db = memory._db()
    db[memory.SESSIONS_COLLECTION].delete_many({"session_id": sid})


def test_foundry_history_roundtrip_and_restart_continuity(sid):
    import src.memory as memory
    from src.memory import _foundry

    try:
        # Turn 1: creates the conversation + mapping lazily.
        memory.append_message(sid, "user", "my name is Ada", user_id="itest")
        memory.append_message(sid, "assistant", "Nice to meet you, Ada.", user_id="itest")

        hist = memory.get_history(sid)
        assert [m["role"] for m in hist] == ["user", "assistant"]
        assert "Ada" in hist[0]["content"]

        # AC6: simulate an API restart — drop the cached client; mapping in Atlas must
        # still resolve the same conversation (no new conversation is created).
        conv_id = _foundry._conversation_id(sid)
        _foundry._conversations = None
        assert _foundry._conversation_id(sid) == conv_id
        hist2 = memory.get_history(sid)
        assert [m["content"] for m in hist2] == [m["content"] for m in hist]

        # R4: session metadata exists for /sessions regardless of backend.
        doc = memory._db()[memory.SESSIONS_COLLECTION].find_one({"session_id": sid})
        assert doc["conversation_id"] == conv_id
        assert doc["messages"] == 2
    finally:
        _cleanup(sid)
