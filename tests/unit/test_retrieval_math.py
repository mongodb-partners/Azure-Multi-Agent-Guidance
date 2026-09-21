"""Unit tests for the pure ranking math in src/retrieval.py (no Mongo, no Azure)."""
from __future__ import annotations

import datetime as dt

import pytest

from src.retrieval import (
    RetrievalConfig,
    _apply_recency,
    _cosine,
    _mmr,
    _rrf_fuse,
)

pytestmark = pytest.mark.unit


def test_rrf_fuse_rewards_agreement():
    # Same doc ranked #1 in both lists should outrank docs seen in only one list.
    vhits = [{"_id": "a"}, {"_id": "b"}]
    thits = [{"_id": "a"}, {"_id": "c"}]
    fused = _rrf_fuse(vhits, thits)
    assert fused[0]["_id"] == "a"
    ids = [d["_id"] for d in fused]
    assert set(ids) == {"a", "b", "c"}
    # Scores are strictly descending.
    scores = [d["_rrf_score"] for d in fused]
    assert scores == sorted(scores, reverse=True)


def test_rrf_fuse_rank_position_matters():
    vhits = [{"_id": "x"}, {"_id": "y"}]
    thits = [{"_id": "y"}, {"_id": "x"}]
    fused = _rrf_fuse(vhits, thits)
    # x and y appear at rank0+rank1 symmetrically → equal scores.
    by_id = {d["_id"]: d["_rrf_score"] for d in fused}
    assert by_id["x"] == pytest.approx(by_id["y"])


def test_cosine_bounds():
    assert _cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert _cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert _cosine([1, 0], [-1, 0]) == pytest.approx(-1.0)


def test_apply_recency_no_field_is_noop():
    cfg = RetrievalConfig(collection="jobs")  # recency_field=None
    docs = [{"_id": "a", "_rrf_score": 1.0}]
    out = _apply_recency(list(docs), cfg)
    assert out[0]["_rrf_score"] == 1.0


def test_apply_recency_boosts_fresher_docs():
    cfg = RetrievalConfig(collection="jobs", recency_field="posted_at")
    now = dt.datetime.now(dt.timezone.utc)
    fresh = {"_id": "fresh", "_rrf_score": 1.0, "posted_at": now}
    old = {"_id": "old", "_rrf_score": 1.0, "posted_at": now - dt.timedelta(days=3650)}
    out = _apply_recency([old, fresh], cfg)
    assert out[0]["_id"] == "fresh"
    assert out[0]["_rrf_score"] > out[1]["_rrf_score"]


def test_mmr_selects_diverse_set():
    cfg = RetrievalConfig(collection="c", embedding_field="embedding")
    q = [1.0, 0.0]
    # Two near-duplicates aligned with the query + one orthogonal doc.
    docs = [
        {"_id": "dup1", "embedding": [1.0, 0.0]},
        {"_id": "dup2", "embedding": [0.99, 0.01]},
        {"_id": "div", "embedding": [0.0, 1.0]},
    ]
    # Low lambda weights diversity heavily → the orthogonal doc must be chosen
    # over the near-duplicate of the top hit.
    picked = _mmr(docs, q, cfg, top_n=2, lam=0.3)
    ids = [d["_id"] for d in picked]
    assert ids[0] in {"dup1", "dup2"}          # most relevant first
    assert "div" in ids                         # diversity pulls in the orthogonal doc


def test_mmr_without_embeddings_falls_back_to_head():
    cfg = RetrievalConfig(collection="c", embedding_field="embedding")
    docs = [{"_id": "a"}, {"_id": "b"}, {"_id": "c"}]  # no embedding field
    picked = _mmr(docs, [1.0, 0.0], cfg, top_n=2)
    assert [d["_id"] for d in picked] == ["a", "b"]


# --------------------------------------------------------------------------- #
# specs/002 — provider-aware query stages + relevance floor resolution
# --------------------------------------------------------------------------- #
import src.retrieval as retrieval
from src.retrieval import (
    _auto_embed_stage,
    _vector_stage,
    hybrid_search,
    resolve_min_score,
)
from src.settings import Settings


def test_vector_stage_uses_query_vector():
    cfg = RetrievalConfig(collection="c")
    stage = _vector_stage(cfg, [0.1, 0.2], k=5)["$vectorSearch"]
    assert stage["path"] == "embedding"
    assert stage["queryVector"] == [0.1, 0.2]
    assert "query" not in stage


def test_auto_embed_stage_uses_query_text_on_embed_text_field():
    cfg = RetrievalConfig(collection="c")
    stage = _auto_embed_stage(cfg, "find me a job", k=5)["$vectorSearch"]
    assert stage["path"] == "embed_text"
    assert stage["query"] == "find me a job"
    assert "queryVector" not in stage


def _patch_settings(monkeypatch, **overrides):
    monkeypatch.setattr(retrieval, "get_settings", lambda: Settings(**overrides))


def test_resolve_min_score_explicit_override_wins(monkeypatch):
    _patch_settings(monkeypatch, embedding_provider="voyage", retrieval_min_score=0.42)
    assert resolve_min_score() == 0.42


def test_resolve_min_score_foundry_default_preserves_legacy_behavior(monkeypatch):
    _patch_settings(monkeypatch, embedding_provider="foundry", retrieval_min_score=None)
    assert resolve_min_score() == 0.60


def test_resolve_min_score_uncalibrated_providers_disable_floor(monkeypatch):
    for provider in ("voyage", "atlas_auto"):
        _patch_settings(monkeypatch, embedding_provider=provider, retrieval_min_score=None)
        retrieval._floor_warned = False
        assert resolve_min_score() is None


def test_hybrid_search_atlas_auto_never_embeds_client_side(monkeypatch):
    """R3/R-Q5: text query goes straight to $vectorSearch; no embed call, no MMR."""
    _patch_settings(monkeypatch, embedding_provider="atlas_auto")
    monkeypatch.setattr(retrieval, "embed_text",
                        lambda *a, **k: pytest.fail("client-side embedding forbidden"))
    monkeypatch.setattr(retrieval, "_mmr",
                        lambda *a, **k: pytest.fail("MMR must be skipped under atlas_auto"))
    captured = {}

    def _fake_vs(cfg, stage, k):
        captured["stage"] = stage
        return [{"_id": "v1", "_vs_score": 0.9}]

    monkeypatch.setattr(retrieval, "_vector_search", _fake_vs)
    monkeypatch.setattr(retrieval, "_text_search", lambda cfg, q, k: [])
    cfg = RetrievalConfig(collection="c")
    hits = hybrid_search("senior python role", cfg, top_n=3)
    assert captured["stage"]["$vectorSearch"]["query"] == "senior python role"
    assert hits[0]["_id"] == "v1"


def test_hybrid_search_foundry_pipeline_unchanged(monkeypatch):
    """AC1: embed query → vector stage with queryVector → RRF → MMR."""
    _patch_settings(monkeypatch, embedding_provider="foundry")
    calls = []
    monkeypatch.setattr(retrieval, "embed_text",
                        lambda text, input_type=None: calls.append(("embed", input_type)) or [1.0])
    monkeypatch.setattr(retrieval, "_vector_search",
                        lambda cfg, stage, k: calls.append(("vs", stage)) or [])
    monkeypatch.setattr(retrieval, "_text_search", lambda cfg, q, k: [])
    monkeypatch.setattr(retrieval, "_mmr",
                        lambda docs, qv, cfg, top_n: calls.append(("mmr", qv)) or docs[:top_n])
    hybrid_search("q", RetrievalConfig(collection="c"))
    assert calls[0] == ("embed", "query")
    assert calls[1][0] == "vs" and "queryVector" in calls[1][1]["$vectorSearch"]
    assert calls[2] == ("mmr", [1.0])
