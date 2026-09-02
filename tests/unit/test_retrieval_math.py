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
