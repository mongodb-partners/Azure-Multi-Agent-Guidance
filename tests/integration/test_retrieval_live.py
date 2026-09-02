"""Integration tests against real Atlas + Azure OpenAI.

Gated: run with `RUN_INTEGRATION=1` and a populated `.env`. These seed a temp
collection, build indexes lazily is out of scope — they assume the demo data has
been seeded (`python -m db_seeding.seed_all`).
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration]


def test_embed_text_dimensionality():
    from src.embeddings import embed_text
    from src.settings import get_settings

    vec = embed_text("senior python backend engineer")
    assert isinstance(vec, list) and len(vec) == get_settings().embedding_dimensions
    assert all(isinstance(x, (int, float)) for x in vec[:8])


def test_hybrid_search_returns_ranked_jobs():
    from src.retrieval import PRESETS, hybrid_search

    hits = hybrid_search("remote python role", PRESETS["jobs"], top_n=3)
    assert isinstance(hits, list)
    if hits:  # tolerate an unseeded cluster, but if present validate the contract
        assert "embedding" not in hits[0]  # vectors stripped before return
        scores = [h.get("_rrf_score", 0) for h in hits]
        assert scores == sorted(scores, reverse=True)


def test_memory_roundtrip():
    import uuid

    import src.memory as memory

    sid = f"itest-{uuid.uuid4().hex[:8]}"
    memory.append_message(sid, "user", "hello from integration test")
    memory.append_message(sid, "assistant", "ack")
    hist = memory.get_history(sid)
    assert [m["role"] for m in hist] == ["user", "assistant"]
    # cleanup
    memory._db()[memory.CHAT_COLLECTION].delete_many({"session_id": sid})
