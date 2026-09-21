"""Live integration tests for embedding providers (specs/002, R2/R3/R8).

Gated: `RUN_INTEGRATION=1` plus provider-specific env:
  - foundry   : AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY (default provider)
  - voyage    : VOYAGE_API_KEY (+ optional VOYAGE_MODEL / VOYAGE_DIMENSIONS)
  - atlas_auto: seeded Atlas cluster with autoEmbed indexes (no credentials needed;
                Atlas embeds server-side) — runs a real hybrid search

Run one provider at a time, e.g.:
  EMBEDDING_PROVIDER=voyage RUN_INTEGRATION=1 pytest -m integration tests/integration/test_embeddings_live.py
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration]


def _provider() -> str:
    from src.settings import get_settings

    return get_settings().embedding_provider


def test_embed_roundtrip_foundry_or_voyage():
    """AC2: embed_text returns a non-empty vector under client-side providers."""
    if _provider() == "atlas_auto":
        pytest.skip("atlas_auto embeds server-side — no client-side round-trip")
    from src.embeddings import embed_dimensions, embed_text

    vec = embed_text("senior python backend engineer", input_type="query")
    assert isinstance(vec, list) and vec and all(isinstance(x, (int, float)) for x in vec[:8])
    dims = embed_dimensions()
    assert dims == len(vec), f"index dims ({dims}) != embedded dims ({len(vec)})"


def test_atlas_auto_embed_text_guard():
    """AC3: client-side embedding must raise under atlas_auto."""
    if _provider() != "atlas_auto":
        pytest.skip("provider-specific")
    from src.embeddings import embed_text

    with pytest.raises(RuntimeError, match="atlas_auto"):
        embed_text("should never be embedded client-side")


def test_atlas_auto_hybrid_search_live():
    """AC3: text-query hybrid search over autoEmbed indexes returns ranked hits."""
    if _provider() != "atlas_auto":
        pytest.skip("provider-specific")
    from src.retrieval import PRESETS, hybrid_search

    hits = hybrid_search("remote python role", PRESETS["jobs"], top_n=3)
    assert isinstance(hits, list)
    if hits:  # tolerate an index still in initial sync (status != READY)
        assert "_vs_score" in hits[0] or "_rrf_score" in hits[0]
        assert "embedding" not in hits[0]  # docs carry no vectors under atlas_auto
