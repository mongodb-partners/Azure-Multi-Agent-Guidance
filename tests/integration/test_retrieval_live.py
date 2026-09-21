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
        # NOT asserted sorted by _rrf_score: `_mmr` deliberately re-orders the fused list for
        # diversity, which is the entire point of the MMR stage. RRF's own ordering invariant is
        # covered where it belongs, as a pure unit test — see test_retrieval_math.py.
        assert all(h.get("_rrf_score", 0) > 0 for h in hits)


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


# --------------------------------------------------------------------------- #
# Grounding contract against real Atlas data
# --------------------------------------------------------------------------- #
def test_off_corpus_query_yields_no_grounding_block():
    """Nothing relevant → empty block, so the abstain rule in GROUNDING_RULES can fire.

    Before the relevance floor this always returned 5 rows under a header telling the agent to
    prefer them, which is how ungrounded answers got manufactured from an unrelated corpus.
    """
    import src.orchestrator as orch

    text, hits = orch._retrieve_context("job-match", "pediatric nursing night shift rota")
    assert (text, hits) == ("", []), (
        f"off-corpus query still produced {len(hits)} grounding row(s) — lower "
        f"RETRIEVAL_MIN_SCORE is too permissive for this corpus "
        f"(run: python -m scripts.calibrate_retrieval_floor)"
    )


def test_on_corpus_query_yields_citable_valid_json_rows():
    import json

    import src.orchestrator as orch

    text, hits = orch._retrieve_context("job-match", "senior python backend engineer, remote")
    if not hits:
        import pytest
        pytest.skip("cluster unseeded or floor too high for this corpus")

    rows = [json.loads(ln) for ln in text.splitlines() if ln.startswith("{")]
    assert rows, "grounding block emitted no document rows"
    assert [r["id"] for r in rows] == [f"j{i}" for i in range(1, len(rows) + 1)]
    assert all(h.get("_cite_id") for h in hits)          # every kept hit is verifiable
    assert "_vs_score" not in text and "embedding" not in text   # no internals leak to the model

    # The verifier accepts real ids and rejects invented ones.
    real = hits[0]["_cite_id"]
    assert orch._check_citations(f"Consider [{real}].", hits)["grounded"]
    assert orch._check_citations("Consider [j99].", hits)["invalid"] == ["j99"]
