"""Hybrid retrieval over MongoDB Atlas — port of the reference `vector-retrieval.ts`.

Combines Atlas Vector Search (`$vectorSearch`) with Atlas Search BM25 (`$search`), fuses
the two ranked lists with Reciprocal Rank Fusion (RRF, k=60), applies an optional recency
decay, and diversifies with MMR (λ=0.7). The host calls this before invoking a specialist
so the agent gets grounded candidates; the agent may still issue its own MCP queries.

Runs directly via pymongo (read-only) rather than through MCP, so the fusion/MMR math stays
in Python where we can port it faithfully.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from pymongo import MongoClient

from .embeddings import embed_text
from .settings import get_settings

RRF_K = 60
MMR_LAMBDA = 0.7
RECENCY_HALFLIFE_DAYS = 90.0

_client: MongoClient | None = None


def _db():
    global _client
    if _client is None:
        s = get_settings()
        s.require("mongodb_uri")
        _client = MongoClient(s.mongodb_uri)
    return _client[get_settings().mongodb_db]


@dataclass
class RetrievalConfig:
    collection: str
    vector_index: str = "vector_index"
    search_index: str = "search_index"
    embedding_field: str = "embedding"
    text_fields: list[str] = field(default_factory=lambda: ["title", "description"])
    projection: dict[str, int] | None = None
    recency_field: str | None = None  # e.g. "posted_at" for jobs


def _vector_search(cfg: RetrievalConfig, query_vec: list[float], k: int) -> list[dict[str, Any]]:
    pipeline = [
        {
            "$vectorSearch": {
                "index": cfg.vector_index,
                "path": cfg.embedding_field,
                "queryVector": query_vec,
                "numCandidates": max(k * 10, 100),
                "limit": k,
            }
        },
        {"$addFields": {"_vs_score": {"$meta": "vectorSearchScore"}}},
    ]
    if cfg.projection:
        proj = dict(cfg.projection)
        proj["_vs_score"] = 1
        pipeline.append({"$project": proj})
    return list(_db()[cfg.collection].aggregate(pipeline))


def _text_search(cfg: RetrievalConfig, query: str, k: int) -> list[dict[str, Any]]:
    pipeline = [
        {
            "$search": {
                "index": cfg.search_index,
                "text": {"query": query, "path": cfg.text_fields},
            }
        },
        {"$limit": k},
        {"$addFields": {"_ts_score": {"$meta": "searchScore"}}},
    ]
    if cfg.projection:
        proj = dict(cfg.projection)
        proj["_ts_score"] = 1
        pipeline.append({"$project": proj})
    try:
        return list(_db()[cfg.collection].aggregate(pipeline))
    except Exception:
        # BM25 index may not exist in a minimal setup — degrade to vector-only.
        return []


def _rrf_fuse(
    vector_hits: list[dict[str, Any]],
    text_hits: list[dict[str, Any]],
    k: int = RRF_K,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion: score = Σ 1/(k + rank) across the two lists."""
    scores: dict[Any, float] = {}
    docs: dict[Any, dict[str, Any]] = {}
    for hits in (vector_hits, text_hits):
        for rank, doc in enumerate(hits):
            _id = doc.get("_id", id(doc))
            scores[_id] = scores.get(_id, 0.0) + 1.0 / (k + rank + 1)
            docs.setdefault(_id, doc)
    fused = []
    for _id, score in scores.items():
        doc = docs[_id]
        doc["_rrf_score"] = score
        fused.append(doc)
    fused.sort(key=lambda d: d["_rrf_score"], reverse=True)
    return fused


def _apply_recency(docs: list[dict[str, Any]], cfg: RetrievalConfig) -> list[dict[str, Any]]:
    """Exponential decay boost by document age (if a recency field is configured)."""
    if not cfg.recency_field:
        return docs
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    for d in docs:
        ts = d.get(cfg.recency_field)
        if isinstance(ts, _dt.datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_dt.timezone.utc)
            age_days = max((now - ts).total_seconds() / 86400.0, 0.0)
            decay = math.exp(-math.log(2) * age_days / RECENCY_HALFLIFE_DAYS)
            d["_rrf_score"] = d.get("_rrf_score", 0.0) * (0.5 + 0.5 * decay)
    docs.sort(key=lambda d: d.get("_rrf_score", 0.0), reverse=True)
    return docs


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(y * y for y in b)) or 1e-9
    return dot / (na * nb)


def _mmr(
    docs: list[dict[str, Any]],
    query_vec: list[float],
    cfg: RetrievalConfig,
    top_n: int,
    lam: float = MMR_LAMBDA,
) -> list[dict[str, Any]]:
    """Maximal Marginal Relevance diversification over the fused set."""
    candidates = [d for d in docs if isinstance(d.get(cfg.embedding_field), list)]
    if not candidates:
        return docs[:top_n]
    selected: list[dict[str, Any]] = []
    while candidates and len(selected) < top_n:
        best, best_score = None, -1e9
        for d in candidates:
            relevance = _cosine(query_vec, d[cfg.embedding_field])
            diversity = max(
                (_cosine(d[cfg.embedding_field], s[cfg.embedding_field]) for s in selected),
                default=0.0,
            )
            score = lam * relevance - (1 - lam) * diversity
            if score > best_score:
                best, best_score = d, score
        if best is None:
            break
        selected.append(best)
        candidates.remove(best)
    return selected


def hybrid_search(
    query: str,
    cfg: RetrievalConfig,
    top_n: int = 5,
    pool: int = 50,
) -> list[dict[str, Any]]:
    """Full hybrid pipeline: vector + BM25 → RRF → recency → MMR. Returns top_n docs."""
    query_vec = embed_text(query)
    vhits = _vector_search(cfg, query_vec, pool)
    thits = _text_search(cfg, query, pool)
    fused = _rrf_fuse(vhits, thits)
    fused = _apply_recency(fused, cfg)
    ranked = _mmr(fused, query_vec, cfg, top_n)
    # Strip bulky embedding vectors before handing to the LLM.
    for d in ranked:
        d.pop(cfg.embedding_field, None)
    return ranked


# Collection-specific presets used by the specialists.
PRESETS: dict[str, RetrievalConfig] = {
    "jobs": RetrievalConfig(
        collection="jobs",
        text_fields=["title", "description", "skills"],
        recency_field="posted_at",
        projection={"title": 1, "company": 1, "location": 1, "remote": 1,
                    "seniority": 1, "skills": 1, "description": 1,
                    "salary_min": 1, "salary_max": 1, "embedding": 1, "posted_at": 1},
    ),
    "companies": RetrievalConfig(
        collection="companies",
        text_fields=["name", "summary", "culture", "mission"],
        projection={"name": 1, "industry": 1, "size": 1, "hq": 1, "funding_stage": 1,
                    "total_funding": 1, "tech_stack": 1, "culture": 1, "mission": 1,
                    "glassdoor_rating": 1, "summary": 1, "embedding": 1},
    ),
    "interview_prep": RetrievalConfig(
        collection="interview_prep",
        text_fields=["role", "question", "category"],
        projection={"role": 1, "question": 1, "category": 1, "sample_answer": 1,
                    "difficulty": 1, "embedding": 1},
    ),
}


if __name__ == "__main__":
    res = hybrid_search("senior python backend role, remote", PRESETS["jobs"], top_n=3)
    for r in res:
        print(r.get("title"), "@", r.get("company"), "score=", round(r.get("_rrf_score", 0), 4))
